import re

from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ID,
    Entity,
    FrontierExpansion,
    GraphStatistics,
    PathStep,
    Relation,
    TraversalDirection,
)
from tracegraph.core.identity import normalize_name
from tracegraph.storage.graph import rank_entities


_SAFE_RELATION_TYPE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 逐层扩展的固定模式片段；方向只在这三个常量之间选择，不接受外部输入。
_PATTERNS = {
    TraversalDirection.OUTGOING: "-[relation]->",
    TraversalDirection.INCOMING: "<-[relation]-",
    None: "-[relation]-",
}

# 起点、邻居与关系三处都钉死 workspace_id：实体 ID 不是隔离手段，
# 拿到别的 Workspace 的 ID 也扩展不出东西。
_EXPAND_CYPHER = """
UNWIND $node_ids AS node_id
MATCH (node:TraceEntity {id: node_id, workspace_id: $workspace_id})%s
      (neighbor:TraceEntity {workspace_id: $workspace_id})
WHERE node <> neighbor
  AND relation.workspace_id = $workspace_id
  AND ($relation_types IS NULL OR type(relation) IN $relation_types)
WITH node_id, relation, neighbor
ORDER BY type(relation), neighbor.id, relation.id
WITH node_id, collect({
        relation_id: relation.id,
        relation_type: type(relation),
        source_id: startNode(relation).id,
        target_id: endNode(relation).id,
        evidence_chunk_ids: coalesce(relation.evidence_chunk_ids, []),
        neighbor_id: neighbor.id,
        neighbor_name: neighbor.name,
        neighbor_type: neighbor.entity_type,
        direction: CASE WHEN startNode(relation).id = node_id
                        THEN 'outgoing' ELSE 'incoming' END
     }) AS edges
RETURN node_id, size(edges) AS total_edges, edges[0..$fanout] AS shown
"""

_PROPERTIES = "e.id AS id, e.name AS name, e.entity_type AS type, e.workspace_id AS workspace_id"

_RETURN_RELATION = """
RETURN relation.id AS id,
       source.id AS source_id,
       target.id AS target_id,
       type(relation) AS type,
       relation.evidence_chunk_ids AS evidence_chunk_ids
"""


class Neo4jGraphRepository:
    """可选的 Neo4j 图存储；安装 `tracegraph[graph]` 后使用。"""

    name = "neo4j"

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        try:
            from neo4j import GraphDatabase
        except ImportError as error:
            raise RuntimeError("使用 Neo4j 需要安装 tracegraph[graph]") from error
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database
        self._driver.verify_connectivity()
        self._driver.execute_query(
            """
            CREATE CONSTRAINT trace_entity_id IF NOT EXISTS
            FOR (entity:TraceEntity) REQUIRE entity.id IS UNIQUE
            """,
            database_=self._database,
        )
        self._driver.execute_query(
            """
            CREATE INDEX trace_entity_workspace IF NOT EXISTS
            FOR (entity:TraceEntity) ON (entity.workspace_id)
            """,
            database_=self._database,
        )
        self._migrate_workspace_properties()

    def _migrate_workspace_properties(self) -> None:
        """给 Workspace 概念出现之前的节点与关系补上归属。

        只填 `workspace_id IS NULL` 的那些：已经有归属的数据一个字节都不改，
        因此重复执行不会覆盖任何东西，也不会产生重复的节点或边。原始 DUTMed
        图谱在迁移后完整保留，全部属于默认 Workspace。
        """
        for statement in (
            """
            MATCH (entity:TraceEntity)
            WHERE entity.workspace_id IS NULL
            SET entity.workspace_id = $workspace_id
            """,
            """
            MATCH ()-[relation]->()
            WHERE relation.workspace_id IS NULL
            SET relation.workspace_id = $workspace_id
            """,
        ):
            self._driver.execute_query(
                statement,
                workspace_id=DEFAULT_WORKSPACE_ID,
                database_=self._database,
            )

    def close(self) -> None:
        self._driver.close()

    def upsert_entity(self, entity: Entity) -> None:
        self._driver.execute_query(
            """
            MERGE (e:TraceEntity {id: $id})
            SET e.name = $name, e.entity_type = $entity_type,
                e.workspace_id = $workspace_id
            """,
            id=entity.id,
            name=entity.name,
            entity_type=entity.type,
            workspace_id=entity.workspace_id,
            database_=self._database,
        )

    def upsert_relation(self, relation: Relation) -> None:
        self._check_relation_type(relation.type)
        # MATCH 两端都钉死了 workspace_id，因此端点不存在或属于别的 Workspace
        # 时一条记录也返回不了。两个 MATCH 都失败与其中一个失败在这里是同一种
        # 情况，都按「端点不合规」处理，与另外两个后端一致。
        records, _, _ = self._driver.execute_query(
            f"""
            MATCH (source:TraceEntity {{id: $source_id, workspace_id: $workspace_id}})
            MATCH (target:TraceEntity {{id: $target_id, workspace_id: $workspace_id}})
            MERGE (source)-[relation:{relation.type} {{id: $id}}]->(target)
            SET relation.evidence_chunk_ids = $evidence_chunk_ids,
                relation.workspace_id = $workspace_id
            RETURN relation.id AS id
            """,
            source_id=relation.source_entity_id,
            target_id=relation.target_entity_id,
            id=relation.id,
            workspace_id=relation.workspace_id,
            evidence_chunk_ids=list(relation.evidence_chunk_ids),
            database_=self._database,
        )
        if not records:
            raise ValueError("关系的源实体不存在或不属于同一个 Workspace")

    def replace_outgoing_graph(
        self,
        source: Entity,
        targets: tuple[Entity, ...],
        relations: tuple[Relation, ...],
    ) -> None:
        entities = {source.id: source, **{entity.id: entity for entity in targets}}
        for relation in relations:
            if relation.source_entity_id != source.id:
                raise ValueError("批量关系必须从 source 实体出发")
            self._check_relation_type(relation.type)
        self._driver.execute_query(
            f"""
            UNWIND $entities AS entity
            MERGE (node:TraceEntity {{id: entity.id}})
            SET node.name = entity.name, node.entity_type = entity.type,
                node.workspace_id = entity.workspace_id
            WITH count(*) AS entity_count
            MATCH (source:TraceEntity {{id: $source_id, workspace_id: $workspace_id}})
            CALL (source) {{
                MATCH (source)-[existing]->()
                WHERE existing.workspace_id = $workspace_id
                DELETE existing
            }}
            WITH source
            UNWIND $relations AS relation
            MATCH (source:TraceEntity {{id: relation.source_id,
                                       workspace_id: relation.workspace_id}})
            MATCH (target:TraceEntity {{id: relation.target_id,
                                       workspace_id: relation.workspace_id}})
            MERGE (source)-[edge:$(relation.type) {{id: relation.id}}]->(target)
            SET edge.evidence_chunk_ids = relation.evidence_chunk_ids,
                edge.workspace_id = relation.workspace_id
            """,
            source_id=source.id,
            workspace_id=source.workspace_id,
            entities=[
                {
                    "id": entity.id,
                    "name": entity.name,
                    "type": entity.type,
                    "workspace_id": entity.workspace_id,
                }
                for entity in entities.values()
            ],
            relations=[
                {
                    "id": relation.id,
                    "source_id": relation.source_entity_id,
                    "target_id": relation.target_entity_id,
                    "type": relation.type,
                    "workspace_id": relation.workspace_id,
                    "evidence_chunk_ids": list(relation.evidence_chunk_ids),
                }
                for relation in relations
            ],
            database_=self._database,
        )

    def get_entity(self, entity_id: str, workspace_id: str) -> Entity | None:
        records, _, _ = self._driver.execute_query(
            f"""
            MATCH (e:TraceEntity {{id: $id, workspace_id: $workspace_id}})
            RETURN {_PROPERTIES}
            """,
            id=entity_id,
            workspace_id=workspace_id,
            database_=self._database,
        )
        return _entity_from_record(records[0]) if records else None

    def find_entity_by_key(
        self, workspace_id: str, entity_type: str, normalized_name: str
    ) -> Entity | None:
        records, _, _ = self._driver.execute_query(
            f"""
            MATCH (e:TraceEntity {{workspace_id: $workspace_id}})
            WHERE toLower(e.entity_type) = toLower($entity_type)
            RETURN {_PROPERTIES}
            ORDER BY e.id
            """,
            workspace_id=workspace_id,
            entity_type=entity_type,
            database_=self._database,
        )
        # 规范化只有一份 Python 实现（去首尾标点需要它），所以这里按 Workspace
        # 与类型缩到很小的候选集后再比对；换后端不会换匹配结果。类型大小写不
        # 敏感：稳定 ID 也是把类型 casefold 之后算出来的，两个口径必须一致。
        for record in records:
            if normalize_name(record["name"]) == normalized_name:
                return _entity_from_record(record)
        return None

    def search_entities(
        self, query: str, workspace_id: str, limit: int = 5
    ) -> tuple[Entity, ...]:
        # 排序必须与 SQLite / 内存后端共用同一份 rank_entities：换后端不能换结果。
        # Cypher 侧的 CONTAINS 预筛会把「脂肪尿和乳糜尿检查」排在「乳糜尿」之前，
        # 起点一变，整条多跳路径和答案都会跟着变。
        records, _, _ = self._driver.execute_query(
            f"MATCH (e:TraceEntity {{workspace_id: $workspace_id}}) RETURN {_PROPERTIES}",
            workspace_id=workspace_id,
            database_=self._database,
        )
        return rank_entities(
            tuple(_entity_from_record(record) for record in records), query, limit
        )

    def list_relations(
        self, entity_ids: tuple[str, ...], workspace_id: str, limit: int = 20
    ) -> tuple[Relation, ...]:
        if not entity_ids:
            return ()
        records, _, _ = self._driver.execute_query(
            f"""
            MATCH (source:TraceEntity)-[relation]->(target:TraceEntity)
            WHERE relation.workspace_id = $workspace_id
              AND (source.id IN $entity_ids OR target.id IN $entity_ids)
            {_RETURN_RELATION}
            ORDER BY relation.id
            LIMIT $limit
            """,
            entity_ids=list(entity_ids),
            workspace_id=workspace_id,
            limit=limit,
            database_=self._database,
        )
        return tuple(_relation_from_record(record, workspace_id) for record in records)

    def get_relation(self, relation_id: str, workspace_id: str) -> Relation | None:
        records, _, _ = self._driver.execute_query(
            f"""
            MATCH (source:TraceEntity)-[relation]->(target:TraceEntity)
            WHERE relation.id = $id AND relation.workspace_id = $workspace_id
            {_RETURN_RELATION}
            """,
            id=relation_id,
            workspace_id=workspace_id,
            database_=self._database,
        )
        return _relation_from_record(records[0], workspace_id) if records else None

    def find_relation_by_key(
        self,
        workspace_id: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
    ) -> Relation | None:
        self._check_relation_type(relation_type)
        records, _, _ = self._driver.execute_query(
            """
            MATCH (source:TraceEntity {id: $source_id})-[relation]->(target:TraceEntity {id: $target_id})
            WHERE relation.workspace_id = $workspace_id
              AND source.workspace_id = $workspace_id
              AND target.workspace_id = $workspace_id
              AND toLower(type(relation)) = toLower($relation_type)
            RETURN relation.id AS id,
                   source.id AS source_id,
                   target.id AS target_id,
                   type(relation) AS type,
                   relation.evidence_chunk_ids AS evidence_chunk_ids
            ORDER BY relation.id
            LIMIT 1
            """,
            source_id=source_entity_id,
            target_id=target_entity_id,
            relation_type=relation_type,
            workspace_id=workspace_id,
            database_=self._database,
        )
        return _relation_from_record(records[0], workspace_id) if records else None

    def statistics(self, workspace_id: str) -> GraphStatistics:
        entity_types, _, _ = self._driver.execute_query(
            """
            MATCH (entity:TraceEntity {workspace_id: $workspace_id})
            RETURN entity.entity_type AS type, count(*) AS total
            ORDER BY type
            """,
            workspace_id=workspace_id,
            database_=self._database,
        )
        relation_types, _, _ = self._driver.execute_query(
            """
            MATCH (:TraceEntity)-[relation]->(:TraceEntity)
            WHERE relation.workspace_id = $workspace_id
            RETURN type(relation) AS type, count(*) AS total
            ORDER BY type
            """,
            workspace_id=workspace_id,
            database_=self._database,
        )
        # 用 EXISTS 子查询而不是模式表达式：`NOT (entity)-[r {...}]-()` 里的
        # 属性写在模式表达式上，Cypher 25（本机 db.query.default_language 就是
        # 它）直接判为语法错误，而 SQLite 侧同一件事是能跑的。
        orphans, _, _ = self._driver.execute_query(
            """
            MATCH (entity:TraceEntity {workspace_id: $workspace_id})
            WHERE NOT EXISTS {
                MATCH (entity)-[relation]-()
                WHERE relation.workspace_id = $workspace_id
            }
            RETURN count(entity) AS total
            """,
            workspace_id=workspace_id,
            database_=self._database,
        )
        return GraphStatistics(
            entities=sum(record["total"] for record in entity_types),
            relations=sum(record["total"] for record in relation_types),
            orphan_entities=orphans[0]["total"],
            entity_types=tuple(
                (record["type"], record["total"]) for record in entity_types
            ),
            relation_types=tuple(
                (record["type"], record["total"]) for record in relation_types
            ),
        )

    def find_opposing_relations(
        self, entity_id: str, relation_types: tuple[str, str], workspace_id: str
    ) -> tuple[Relation, ...]:
        first, second = relation_types
        self._check_relation_type(first)
        self._check_relation_type(second)
        records, _, _ = self._driver.execute_query(
            """
            MATCH (source:TraceEntity {id: $entity_id, workspace_id: $workspace_id})-[first]->(target:TraceEntity)
            MATCH (source)-[second]->(target)
            WHERE first.workspace_id = $workspace_id
              AND second.workspace_id = $workspace_id
              AND type(first) = $first
              AND type(second) = $second
            RETURN first.id AS first_id, second.id AS second_id
            """,
            entity_id=entity_id,
            workspace_id=workspace_id,
            first=first,
            second=second,
            database_=self._database,
        )
        relation_ids = sorted(
            {record["first_id"] for record in records}
            | {record["second_id"] for record in records}
        )
        return tuple(
            relation
            for relation in (
                self.get_relation(relation_id, workspace_id)
                for relation_id in relation_ids
            )
            if relation is not None
        )

    def expand_frontier(
        self,
        node_ids: tuple[str, ...],
        *,
        workspace_id: str,
        fanout: int,
        relation_types: tuple[str, ...] | None = None,
        direction: TraversalDirection | None = None,
    ) -> tuple[FrontierExpansion, ...]:
        if fanout < 1:
            raise ValueError("fanout 必须大于 0")
        if not node_ids:
            return ()
        for relation_type in relation_types or ():
            self._check_relation_type(relation_type)
        records, _, _ = self._driver.execute_query(
            _EXPAND_CYPHER % _PATTERNS[direction],
            node_ids=list(node_ids),
            workspace_id=workspace_id,
            relation_types=list(relation_types) if relation_types is not None else None,
            fanout=fanout,
            database_=self._database,
        )
        by_node = {record["node_id"]: record for record in records}
        expansions = []
        for node_id in node_ids:
            record = by_node.get(node_id)
            if record is None:
                expansions.append(
                    FrontierExpansion(node_id=node_id, steps=(), total_edges=0)
                )
                continue
            steps = []
            for edge in record["shown"]:
                chunk_ids = tuple(edge["evidence_chunk_ids"] or ())
                if not chunk_ids:
                    continue
                steps.append(
                    PathStep(
                        relation=Relation(
                            id=edge["relation_id"],
                            source_entity_id=edge["source_id"],
                            target_entity_id=edge["target_id"],
                            type=edge["relation_type"],
                            evidence_chunk_ids=chunk_ids,
                            workspace_id=workspace_id,
                        ),
                        direction=TraversalDirection(edge["direction"]),
                        target=Entity(
                            id=edge["neighbor_id"],
                            name=edge["neighbor_name"],
                            type=edge["neighbor_type"],
                            workspace_id=workspace_id,
                        ),
                    )
                )
            expansions.append(
                FrontierExpansion(
                    node_id=node_id,
                    steps=tuple(steps),
                    total_edges=record["total_edges"],
                )
            )
        return tuple(expansions)

    def remove_evidence(self, chunk_ids: tuple[str, ...], workspace_id: str) -> None:
        if not chunk_ids:
            return
        self._driver.execute_query(
            """
            MATCH ()-[relation]->()
            WHERE relation.workspace_id = $workspace_id
            SET relation.evidence_chunk_ids = [
                chunk_id IN coalesce(relation.evidence_chunk_ids, [])
                WHERE NOT chunk_id IN $chunk_ids
            ]
            WITH relation
            WHERE size(relation.evidence_chunk_ids) = 0
            DELETE relation
            """,
            chunk_ids=list(chunk_ids),
            workspace_id=workspace_id,
            database_=self._database,
        )

    def delete_outgoing_relations(self, entity_id: str, workspace_id: str) -> None:
        self._driver.execute_query(
            """
            MATCH (:TraceEntity {id: $entity_id, workspace_id: $workspace_id})-[relation]->()
            WHERE relation.workspace_id = $workspace_id
            DELETE relation
            """,
            entity_id=entity_id,
            workspace_id=workspace_id,
            database_=self._database,
        )

    @staticmethod
    def _check_relation_type(relation_type: str) -> None:
        """关系类型会拼进 Cypher，因此只能是固定形状的标识符。"""
        if not _SAFE_RELATION_TYPE.fullmatch(relation_type):
            raise ValueError("关系类型只能包含大写字母、数字和下划线")


def _entity_from_record(record: object) -> Entity:
    return Entity(
        id=record["id"],
        name=record["name"],
        type=record["type"],
        workspace_id=record["workspace_id"],
    )


def _relation_from_record(record: object, workspace_id: str) -> Relation:
    return Relation(
        id=record["id"],
        source_entity_id=record["source_id"],
        target_entity_id=record["target_id"],
        type=record["type"],
        evidence_chunk_ids=tuple(record["evidence_chunk_ids"] or ()),
        workspace_id=workspace_id,
    )
