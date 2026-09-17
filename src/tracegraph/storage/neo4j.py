import re

from tracegraph.core.contracts import (
    Entity,
    FrontierExpansion,
    GraphStatistics,
    PathStep,
    Relation,
    TraversalDirection,
)
from tracegraph.storage.graph import rank_entities


_SAFE_RELATION_TYPE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 逐层扩展的固定模式片段；方向只在这三个常量之间选择，不接受外部输入。
_PATTERNS = {
    TraversalDirection.OUTGOING: "-[relation]->",
    TraversalDirection.INCOMING: "<-[relation]-",
    None: "-[relation]-",
}

_EXPAND_CYPHER = """
UNWIND $node_ids AS node_id
MATCH (node:TraceEntity {id: node_id})%s(neighbor:TraceEntity)
WHERE node <> neighbor
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

    def close(self) -> None:
        self._driver.close()

    def upsert_entity(self, entity: Entity) -> None:
        self._driver.execute_query(
            """
            MERGE (e:TraceEntity {id: $id})
            SET e.name = $name, e.entity_type = $entity_type
            """,
            id=entity.id,
            name=entity.name,
            entity_type=entity.type,
            database_=self._database,
        )

    def upsert_relation(self, relation: Relation) -> None:
        if not _SAFE_RELATION_TYPE.fullmatch(relation.type):
            raise ValueError("关系类型只能包含大写字母、数字和下划线")
        self._driver.execute_query(
            f"""
            MATCH (source:TraceEntity {{id: $source_id}})
            MATCH (target:TraceEntity {{id: $target_id}})
            MERGE (source)-[relation:{relation.type} {{id: $id}}]->(target)
            SET relation.evidence_chunk_ids = $evidence_chunk_ids
            """,
            source_id=relation.source_entity_id,
            target_id=relation.target_entity_id,
            id=relation.id,
            evidence_chunk_ids=list(relation.evidence_chunk_ids),
            database_=self._database,
        )

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
            if not _SAFE_RELATION_TYPE.fullmatch(relation.type):
                raise ValueError("关系类型只能包含大写字母、数字和下划线")
        self._driver.execute_query(
            """
            UNWIND $entities AS entity
            MERGE (node:TraceEntity {id: entity.id})
            SET node.name = entity.name, node.entity_type = entity.type
            WITH count(*) AS entity_count
            MATCH (source:TraceEntity {id: $source_id})
            CALL (source) {
                MATCH (source)-[existing]->()
                DELETE existing
            }
            WITH source
            UNWIND $relations AS relation
            MATCH (source:TraceEntity {id: relation.source_id})
            MATCH (target:TraceEntity {id: relation.target_id})
            MERGE (source)-[edge:$(relation.type) {id: relation.id}]->(target)
            SET edge.evidence_chunk_ids = relation.evidence_chunk_ids
            """,
            source_id=source.id,
            entities=[
                {"id": entity.id, "name": entity.name, "type": entity.type}
                for entity in entities.values()
            ],
            relations=[
                {
                    "id": relation.id,
                    "source_id": relation.source_entity_id,
                    "target_id": relation.target_entity_id,
                    "type": relation.type,
                    "evidence_chunk_ids": list(relation.evidence_chunk_ids),
                }
                for relation in relations
            ],
            database_=self._database,
        )

    def get_entity(self, entity_id: str) -> Entity | None:
        records, _, _ = self._driver.execute_query(
            """
            MATCH (e:TraceEntity {id: $id})
            RETURN e.id AS id, e.name AS name, e.entity_type AS type
            """,
            id=entity_id,
            database_=self._database,
        )
        if not records:
            return None
        record = records[0]
        return Entity(id=record["id"], name=record["name"], type=record["type"])

    def search_entities(self, query: str, limit: int = 5) -> tuple[Entity, ...]:
        # 排序必须与 SQLite / 内存后端共用同一份 rank_entities：换后端不能换结果。
        # Cypher 侧的 CONTAINS 预筛会把「脂肪尿和乳糜尿检查」排在「乳糜尿」之前，
        # 起点一变，整条多跳路径和答案都会跟着变。
        records, _, _ = self._driver.execute_query(
            "MATCH (e:TraceEntity) RETURN e.id AS id, e.name AS name,"
            " e.entity_type AS type",
            database_=self._database,
        )
        return rank_entities(
            tuple(
                Entity(id=record["id"], name=record["name"], type=record["type"])
                for record in records
            ),
            query,
            limit,
        )

    def list_relations(
        self, entity_ids: tuple[str, ...], limit: int = 20
    ) -> tuple[Relation, ...]:
        if not entity_ids:
            return ()
        records, _, _ = self._driver.execute_query(
            """
            MATCH (source:TraceEntity)-[relation]->(target:TraceEntity)
            WHERE source.id IN $entity_ids OR target.id IN $entity_ids
            RETURN relation.id AS id,
                   source.id AS source_id,
                   target.id AS target_id,
                   type(relation) AS type,
                   relation.evidence_chunk_ids AS evidence_chunk_ids
            ORDER BY relation.id
            LIMIT $limit
            """,
            entity_ids=list(entity_ids),
            limit=limit,
            database_=self._database,
        )
        return tuple(
            Relation(
                id=record["id"],
                source_entity_id=record["source_id"],
                target_entity_id=record["target_id"],
                type=record["type"],
                evidence_chunk_ids=tuple(record["evidence_chunk_ids"] or ()),
            )
            for record in records
        )

    def get_relation(self, relation_id: str) -> Relation | None:
        records, _, _ = self._driver.execute_query(
            """
            MATCH (source:TraceEntity)-[relation]->(target:TraceEntity)
            WHERE relation.id = $id
            RETURN relation.id AS id,
                   source.id AS source_id,
                   target.id AS target_id,
                   type(relation) AS type,
                   relation.evidence_chunk_ids AS evidence_chunk_ids
            """,
            id=relation_id,
            database_=self._database,
        )
        if not records:
            return None
        record = records[0]
        return Relation(
            id=record["id"],
            source_entity_id=record["source_id"],
            target_entity_id=record["target_id"],
            type=record["type"],
            evidence_chunk_ids=tuple(record["evidence_chunk_ids"] or ()),
        )

    def statistics(self) -> GraphStatistics:
        entity_types, _, _ = self._driver.execute_query(
            """
            MATCH (entity:TraceEntity)
            RETURN entity.entity_type AS type, count(*) AS total
            ORDER BY type
            """,
            database_=self._database,
        )
        relation_types, _, _ = self._driver.execute_query(
            """
            MATCH (:TraceEntity)-[relation]->(:TraceEntity)
            RETURN type(relation) AS type, count(*) AS total
            ORDER BY type
            """,
            database_=self._database,
        )
        orphans, _, _ = self._driver.execute_query(
            """
            MATCH (entity:TraceEntity)
            WHERE NOT (entity)--()
            RETURN count(entity) AS total
            """,
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
        self, entity_id: str, relation_types: tuple[str, str]
    ) -> tuple[Relation, ...]:
        first, second = relation_types
        records, _, _ = self._driver.execute_query(
            """
            MATCH (source:TraceEntity {id: $entity_id})-[first]->(target:TraceEntity)
            MATCH (source)-[second]->(target)
            WHERE type(first) = $first AND type(second) = $second
            RETURN first.id AS first_id, second.id AS second_id
            """,
            entity_id=entity_id,
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
                self.get_relation(relation_id) for relation_id in relation_ids
            )
            if relation is not None
        )

    def expand_frontier(
        self,
        node_ids: tuple[str, ...],
        *,
        fanout: int,
        relation_types: tuple[str, ...] | None = None,
        direction: TraversalDirection | None = None,
    ) -> tuple[FrontierExpansion, ...]:
        if fanout < 1:
            raise ValueError("fanout 必须大于 0")
        if not node_ids:
            return ()
        for relation_type in relation_types or ():
            if not _SAFE_RELATION_TYPE.fullmatch(relation_type):
                raise ValueError("关系类型只能包含大写字母、数字和下划线")
        records, _, _ = self._driver.execute_query(
            _EXPAND_CYPHER % _PATTERNS[direction],
            node_ids=list(node_ids),
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
                        ),
                        direction=TraversalDirection(edge["direction"]),
                        target=Entity(
                            id=edge["neighbor_id"],
                            name=edge["neighbor_name"],
                            type=edge["neighbor_type"],
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

    def remove_evidence(self, chunk_ids: tuple[str, ...]) -> None:
        if not chunk_ids:
            return
        self._driver.execute_query(
            """
            MATCH ()-[relation]->()
            SET relation.evidence_chunk_ids = [
                chunk_id IN coalesce(relation.evidence_chunk_ids, [])
                WHERE NOT chunk_id IN $chunk_ids
            ]
            WITH relation
            WHERE size(relation.evidence_chunk_ids) = 0
            DELETE relation
            """,
            chunk_ids=list(chunk_ids),
            database_=self._database,
        )

    def delete_outgoing_relations(self, entity_id: str) -> None:
        self._driver.execute_query(
            """
            MATCH (:TraceEntity {id: $entity_id})-[relation]->()
            DELETE relation
            """,
            entity_id=entity_id,
            database_=self._database,
        )
