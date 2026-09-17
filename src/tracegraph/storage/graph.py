from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
import sqlite3
from threading import RLock

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


_OUTGOING_BRANCH = """
    SELECT frontier.node_id AS node_id,
           relations.id AS relation_id,
           relations.type AS relation_type,
           relations.source_entity_id AS source_entity_id,
           relations.target_entity_id AS target_entity_id,
           relations.target_entity_id AS neighbor_id,
           'outgoing' AS direction
    FROM frontier
    JOIN relations ON relations.source_entity_id = frontier.node_id
    WHERE relations.source_entity_id <> relations.target_entity_id
      AND relations.workspace_id = ?
"""

_INCOMING_BRANCH = """
    SELECT frontier.node_id AS node_id,
           relations.id AS relation_id,
           relations.type AS relation_type,
           relations.source_entity_id AS source_entity_id,
           relations.target_entity_id AS target_entity_id,
           relations.source_entity_id AS neighbor_id,
           'incoming' AS direction
    FROM frontier
    JOIN relations ON relations.target_entity_id = frontier.node_id
    WHERE relations.source_entity_id <> relations.target_entity_id
      AND relations.workspace_id = ?
"""


def _direction_flags(direction: TraversalDirection | None) -> tuple[bool, bool]:
    """返回 (允许出向, 允许入向)；None 表示双向。"""
    if direction is None:
        return True, True
    return direction is TraversalDirection.OUTGOING, direction is TraversalDirection.INCOMING


def _step_order(step: PathStep) -> tuple[str, str, str]:
    return (step.relation.type, step.target.id, step.relation.id)


def _step_from(
    node: Entity, relation: Relation, entities: dict[str, Entity]
) -> PathStep | None:
    """把一条关系解释为从 node 出发的一跳；node 不在关系两端时返回 None。"""
    if _is_self_loop(relation):
        return None
    if relation.source_entity_id == node.id:
        target = entities.get(relation.target_entity_id)
        direction = TraversalDirection.OUTGOING
    elif relation.target_entity_id == node.id:
        target = entities.get(relation.source_entity_id)
        direction = TraversalDirection.INCOMING
    else:
        return None
    if target is None:
        return None
    return PathStep(relation=relation, direction=direction, target=target)


def _is_self_loop(relation: Relation) -> bool:
    """自环永远无法构成简单路径，逐层扩展直接忽略。"""
    return relation.source_entity_id == relation.target_entity_id


class InMemoryGraphRepository:
    name = "memory"

    def __init__(self) -> None:
        self._lock = RLock()
        self._entities: dict[str, Entity] = {}
        self._relations: dict[str, Relation] = {}

    def upsert_entity(self, entity: Entity) -> None:
        with self._lock:
            self._entities[entity.id] = entity

    def upsert_relation(self, relation: Relation) -> None:
        with self._lock:
            source = self._entities.get(relation.source_entity_id)
            target = self._entities.get(relation.target_entity_id)
            if source is None or source.workspace_id != relation.workspace_id:
                raise ValueError("关系的源实体不存在或不属于同一个 Workspace")
            if target is None or target.workspace_id != relation.workspace_id:
                raise ValueError("关系的目标实体不存在或不属于同一个 Workspace")
            self._relations[relation.id] = relation

    def replace_outgoing_graph(
        self,
        source: Entity,
        targets: tuple[Entity, ...],
        relations: tuple[Relation, ...],
    ) -> None:
        with self._lock:
            self._entities[source.id] = source
            self._entities.update({entity.id: entity for entity in targets})
            # 只清掉这个 Workspace 里从 source 出发的边：同名实体在别的
            # Workspace 里的出边与本次导入无关。
            self._relations = {
                relation_id: relation
                for relation_id, relation in self._relations.items()
                if not (
                    relation.source_entity_id == source.id
                    and relation.workspace_id == source.workspace_id
                )
            }
            self._relations.update({relation.id: relation for relation in relations})

    def get_entity(self, entity_id: str, workspace_id: str) -> Entity | None:
        with self._lock:
            entity = self._entities.get(entity_id)
        return entity if entity is not None and entity.workspace_id == workspace_id else None

    def find_entity_by_key(
        self, workspace_id: str, entity_type: str, normalized_name: str
    ) -> Entity | None:
        with self._lock:
            matches = sorted(
                (
                    entity
                    for entity in self._entities.values()
                    if entity.workspace_id == workspace_id
                    and entity.type.casefold() == entity_type.casefold()
                    and normalize_name(entity.name) == normalized_name
                ),
                key=lambda entity: entity.id,
            )
        return matches[0] if matches else None

    def search_entities(
        self, query: str, workspace_id: str, limit: int = 5
    ) -> tuple[Entity, ...]:
        with self._lock:
            entities = tuple(
                entity
                for entity in self._entities.values()
                if entity.workspace_id == workspace_id
            )
        return rank_entities(entities, query, limit)

    def list_relations(
        self, entity_ids: tuple[str, ...], workspace_id: str, limit: int = 20
    ) -> tuple[Relation, ...]:
        known = set(entity_ids)
        with self._lock:
            relations = (
                relation
                for relation in self._relations.values()
                if relation.workspace_id == workspace_id
                and (
                    relation.source_entity_id in known
                    or relation.target_entity_id in known
                )
            )
            return tuple(sorted(relations, key=lambda item: item.id)[:limit])

    def get_relation(self, relation_id: str, workspace_id: str) -> Relation | None:
        with self._lock:
            relation = self._relations.get(relation_id)
        return (
            relation
            if relation is not None and relation.workspace_id == workspace_id
            else None
        )

    def find_relation_by_key(
        self,
        workspace_id: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
    ) -> Relation | None:
        with self._lock:
            matches = sorted(
                (
                    relation
                    for relation in self._relations.values()
                    if relation.workspace_id == workspace_id
                    and relation.source_entity_id == source_entity_id
                    and relation.target_entity_id == target_entity_id
                    and relation.type.casefold() == relation_type.casefold()
                ),
                key=lambda relation: relation.id,
            )
        return matches[0] if matches else None

    def statistics(self, workspace_id: str) -> GraphStatistics:
        with self._lock:
            entities = tuple(
                entity
                for entity in self._entities.values()
                if entity.workspace_id == workspace_id
            )
            relations = tuple(
                relation
                for relation in self._relations.values()
                if relation.workspace_id == workspace_id
            )
        connected = {
            entity_id
            for relation in relations
            for entity_id in (relation.source_entity_id, relation.target_entity_id)
        }
        return _statistics(entities, relations, connected)

    def find_opposing_relations(
        self, entity_id: str, relation_types: tuple[str, str], workspace_id: str
    ) -> tuple[Relation, ...]:
        first, second = relation_types
        with self._lock:
            relations = tuple(
                relation
                for relation in self._relations.values()
                if relation.workspace_id == workspace_id
            )
        first_targets = set()
        second_targets = set()
        for relation in relations:
            if relation.source_entity_id != entity_id:
                continue
            if relation.type == first:
                first_targets.add(relation.target_entity_id)
            elif relation.type == second:
                second_targets.add(relation.target_entity_id)
        shared = first_targets.intersection(second_targets)
        if not shared:
            return ()
        return tuple(
            sorted(
                (
                    relation
                    for relation in relations
                    if relation.source_entity_id == entity_id
                    and relation.type in relation_types
                    and relation.target_entity_id in shared
                ),
                key=lambda relation: relation.id,
            )
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
        allow_outgoing, allow_incoming = _direction_flags(direction)
        wanted = set(relation_types) if relation_types is not None else None
        with self._lock:
            entities = dict(self._entities)
            relations = tuple(
                relation
                for relation in self._relations.values()
                if relation.workspace_id == workspace_id
            )
        incident: dict[str, list[Relation]] = {}
        for relation in relations:
            incident.setdefault(relation.source_entity_id, []).append(relation)
            incident.setdefault(relation.target_entity_id, []).append(relation)
        expansions = []
        for node_id in node_ids:
            node = entities.get(node_id)
            steps = []
            if node is not None:
                for relation in incident.get(node_id, ()):
                    if wanted is not None and relation.type not in wanted:
                        continue
                    step = _step_from(node, relation, entities)
                    if step is None:
                        continue
                    if step.direction is TraversalDirection.OUTGOING:
                        if not allow_outgoing:
                            continue
                    elif not allow_incoming:
                        continue
                    steps.append(step)
            steps.sort(key=_step_order)
            expansions.append(
                FrontierExpansion(
                    node_id=node_id,
                    steps=tuple(steps[:fanout]),
                    total_edges=len(steps),
                )
            )
        return tuple(expansions)

    def remove_evidence(self, chunk_ids: tuple[str, ...], workspace_id: str) -> None:
        removed = set(chunk_ids)
        with self._lock:
            updated = {}
            for relation_id, relation in self._relations.items():
                if relation.workspace_id != workspace_id:
                    updated[relation_id] = relation
                    continue
                remaining = tuple(
                    chunk_id
                    for chunk_id in relation.evidence_chunk_ids
                    if chunk_id not in removed
                )
                if remaining:
                    updated[relation_id] = replace(
                        relation, evidence_chunk_ids=remaining
                    )
            self._relations = updated

    def delete_outgoing_relations(self, entity_id: str, workspace_id: str) -> None:
        with self._lock:
            self._relations = {
                relation_id: relation
                for relation_id, relation in self._relations.items()
                if not (
                    relation.source_entity_id == entity_id
                    and relation.workspace_id == workspace_id
                )
            }


class SQLiteGraphRepository:
    name = "sqlite"

    def __init__(self, database: str | Path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteGraphRepository":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def upsert_entity(self, entity: Entity) -> None:
        with self._lock, self._connection:
            self._write_entities((entity,))

    def upsert_relation(self, relation: Relation) -> None:
        with self._lock, self._connection:
            self._write_relations((relation,))

    def replace_outgoing_graph(
        self,
        source: Entity,
        targets: tuple[Entity, ...],
        relations: tuple[Relation, ...],
    ) -> None:
        entities = {source.id: source, **{entity.id: entity for entity in targets}}
        with self._lock, self._connection:
            self._write_entities(tuple(entities.values()))
            # 只清掉这个 Workspace 里从 source 出发的边：同名实体在别的
            # Workspace 里的出边与本次导入无关。
            self._connection.execute(
                "DELETE FROM relations WHERE source_entity_id = ? AND workspace_id = ?",
                (source.id, source.workspace_id),
            )
            self._write_relations(relations)

    def get_entity(self, entity_id: str, workspace_id: str) -> Entity | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, name, type, workspace_id FROM entities
                WHERE id = ? AND workspace_id = ?
                """,
                (entity_id, workspace_id),
            ).fetchone()
        return _entity_from_row(row) if row else None

    def find_entity_by_key(
        self, workspace_id: str, entity_type: str, normalized_name: str
    ) -> Entity | None:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, name, type, workspace_id FROM entities
                WHERE workspace_id = ? AND LOWER(type) = LOWER(?)
                ORDER BY id
                """,
                (workspace_id, entity_type),
            ).fetchall()
        # 规范化只在 Python 侧有一份实现（去首尾标点需要它），所以这里按 Workspace
        # 与类型先缩到很小的候选集，再逐个比对规范名称；换后端不会换匹配结果。
        # 类型大小写不敏感：稳定 ID 也是把类型 casefold 之后算出来的，两个口径
        # 必须一致，否则同一个实体在「按 ID 找」与「按名称找」下会落到两个节点。
        for row in rows:
            if normalize_name(row["name"]) == normalized_name:
                return _entity_from_row(row)
        return None

    def search_entities(
        self, query: str, workspace_id: str, limit: int = 5
    ) -> tuple[Entity, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, name, type, workspace_id FROM entities WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
        return rank_entities(tuple(_entity_from_row(row) for row in rows), query, limit)

    def list_relations(
        self, entity_ids: tuple[str, ...], workspace_id: str, limit: int = 20
    ) -> tuple[Relation, ...]:
        if not entity_ids:
            return ()
        placeholders = ",".join("?" for _ in entity_ids)
        # workspace_id 必须写进 OR 的每一支。写成 `(... OR ...) AND workspace_id = ?`
        # 时 SQLite 用不上「多索引 OR」，只能退化成按 workspace_id 全表扫一遍
        # relations —— 真实图谱有几十万条边，每次检索都会卡住一秒。
        parameters = (*entity_ids, workspace_id, *entity_ids, workspace_id, limit)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT id, source_entity_id, target_entity_id, type
                FROM relations
                WHERE (source_entity_id IN ({placeholders}) AND workspace_id = ?)
                   OR (target_entity_id IN ({placeholders}) AND workspace_id = ?)
                ORDER BY id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            evidence = _load_relation_evidence(
                self._connection, tuple(row["id"] for row in rows)
            )
        return tuple(
            _relation_from_row(row, workspace_id, evidence.get(row["id"], ()))
            for row in rows
        )

    def get_relation(self, relation_id: str, workspace_id: str) -> Relation | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, source_entity_id, target_entity_id, type
                FROM relations WHERE id = ? AND workspace_id = ?
                """,
                (relation_id, workspace_id),
            ).fetchone()
            if row is None:
                return None
            evidence = _load_relation_evidence(self._connection, (relation_id,))
        return _relation_from_row(row, workspace_id, evidence.get(relation_id, ()))

    def find_relation_by_key(
        self,
        workspace_id: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
    ) -> Relation | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, source_entity_id, target_entity_id, type
                FROM relations
                WHERE workspace_id = ?
                  AND source_entity_id = ?
                  AND target_entity_id = ?
                  AND LOWER(type) = LOWER(?)
                ORDER BY id
                LIMIT 1
                """,
                (workspace_id, source_entity_id, target_entity_id, relation_type),
            ).fetchone()
            if row is None:
                return None
            evidence = _load_relation_evidence(self._connection, (row["id"],))
        return _relation_from_row(row, workspace_id, evidence.get(row["id"], ()))

    def find_opposing_relations(
        self, entity_id: str, relation_types: tuple[str, str], workspace_id: str
    ) -> tuple[Relation, ...]:
        first, second = relation_types
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT DISTINCT first.id AS first_id, second.id AS second_id
                FROM relations AS first
                JOIN relations AS second
                  ON second.source_entity_id = first.source_entity_id
                 AND second.target_entity_id = first.target_entity_id
                WHERE first.source_entity_id = ?
                  AND first.type = ?
                  AND second.type = ?
                  AND first.workspace_id = ?
                  AND second.workspace_id = ?
                """,
                (entity_id, first, second, workspace_id, workspace_id),
            ).fetchall()
            if not rows:
                return ()
            relation_ids = tuple(
                sorted(
                    {row["first_id"] for row in rows}
                    | {row["second_id"] for row in rows}
                )
            )
            evidence = _load_relation_evidence(self._connection, relation_ids)
            placeholders = ",".join("?" for _ in relation_ids)
            details = self._connection.execute(
                f"""
                SELECT id, source_entity_id, target_entity_id, type
                FROM relations WHERE id IN ({placeholders})
                ORDER BY id
                """,
                relation_ids,
            ).fetchall()
        return tuple(
            _relation_from_row(row, workspace_id, evidence.get(row["id"], ()))
            for row in details
        )

    def statistics(self, workspace_id: str) -> GraphStatistics:
        with self._lock:
            return sqlite_graph_statistics(self._connection, workspace_id)

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
        allow_outgoing, allow_incoming = _direction_flags(direction)
        type_clause = ""
        type_params: tuple[str, ...] = ()
        if relation_types is not None:
            type_clause = f" AND relations.type IN ({','.join('?' for _ in relation_types)})"
            type_params = tuple(relation_types)
        branches = []
        if allow_outgoing:
            branches.append(_OUTGOING_BRANCH + type_clause)
        if allow_incoming:
            branches.append(_INCOMING_BRANCH + type_clause)
        if not branches:
            return _empty_expansions(node_ids)

        parameters: list[object] = [*node_ids]
        for _ in branches:
            parameters.append(workspace_id)
            parameters.extend(type_params)
        parameters.append(fanout)
        with self._lock:
            rows = self._connection.execute(
                f"""
                WITH frontier(node_id) AS (VALUES {",".join("(?)" for _ in node_ids)})
                SELECT node_id, relation_id, relation_type, source_entity_id,
                       target_entity_id, neighbor_id, direction, total_edges
                FROM (
                    SELECT *,
                        ROW_NUMBER() OVER (
                            PARTITION BY node_id
                            ORDER BY relation_type, neighbor_id, relation_id
                        ) AS row_number,
                        COUNT(*) OVER (PARTITION BY node_id) AS total_edges
                    FROM ({" UNION ALL ".join(branches)})
                )
                WHERE row_number <= ?
                """,
                parameters,
            ).fetchall()
            return _build_expansions(self._connection, node_ids, rows, workspace_id)

    def remove_evidence(self, chunk_ids: tuple[str, ...], workspace_id: str) -> None:
        if not chunk_ids:
            return
        placeholders = ",".join("?" for _ in chunk_ids)
        with self._lock, self._connection:
            # 只删这个 Workspace 的关系证据：别的 Workspace 可能引用同一批
            # Chunk ID（同一份文档被两个 Workspace 各自入库时）。
            self._connection.execute(
                f"""
                DELETE FROM relation_evidence
                WHERE chunk_id IN ({placeholders})
                  AND relation_id IN (
                      SELECT id FROM relations WHERE workspace_id = ?
                  )
                """,
                (*chunk_ids, workspace_id),
            )
            self._connection.execute(
                """
                DELETE FROM relations
                WHERE workspace_id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM relation_evidence
                    WHERE relation_evidence.relation_id = relations.id
                )
                """,
                (workspace_id,),
            )

    def delete_outgoing_relations(self, entity_id: str, workspace_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM relations WHERE source_entity_id = ? AND workspace_id = ?",
                (entity_id, workspace_id),
            )

    def _write_entities(self, entities: tuple[Entity, ...]) -> None:
        if not entities:
            return
        self._connection.executemany(
            """
            INSERT INTO entities (id, name, type, workspace_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                workspace_id = excluded.workspace_id
            """,
            (
                (entity.id, entity.name, entity.type, entity.workspace_id)
                for entity in entities
            ),
        )

    def _require_endpoints(self, relations: tuple[Relation, ...]) -> None:
        """关系两端必须存在，且与关系属于同一个 Workspace。

        外键只保证实体「存在」，拦不住「拿 A Workspace 的实体去接 B Workspace
        的关系」—— 那样写出来的边在两个 Workspace 里各露一半。内存后端一直
        在写之前拒绝这种输入，SQLite 必须给出同样的结果。
        """
        for workspace_id in {relation.workspace_id for relation in relations}:
            endpoint_ids = tuple(
                {
                    entity_id
                    for relation in relations
                    if relation.workspace_id == workspace_id
                    for entity_id in (
                        relation.source_entity_id,
                        relation.target_entity_id,
                    )
                }
            )
            known = set(_load_entities(self._connection, endpoint_ids, workspace_id))
            for relation in relations:
                if relation.workspace_id != workspace_id:
                    continue
                if not {
                    relation.source_entity_id,
                    relation.target_entity_id,
                } <= known:
                    raise ValueError("关系的源实体不存在或不属于同一个 Workspace")

    def _write_relations(self, relations: tuple[Relation, ...]) -> None:
        if not relations:
            return
        self._require_endpoints(relations)
        relation_ids = tuple(relation.id for relation in relations)
        placeholders = ",".join("?" for _ in relation_ids)
        self._connection.execute(
            f"DELETE FROM relation_evidence WHERE relation_id IN ({placeholders})",
            relation_ids,
        )
        self._connection.executemany(
            """
            INSERT INTO relations (id, source_entity_id, target_entity_id, type, workspace_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                source_entity_id = excluded.source_entity_id,
                target_entity_id = excluded.target_entity_id,
                type = excluded.type,
                workspace_id = excluded.workspace_id
            """,
            (
                (
                    relation.id,
                    relation.source_entity_id,
                    relation.target_entity_id,
                    relation.type,
                    relation.workspace_id,
                )
                for relation in relations
            ),
        )
        self._connection.executemany(
            """
            INSERT OR IGNORE INTO relation_evidence (relation_id, chunk_id)
            VALUES (?, ?)
            """,
            (
                (relation.id, chunk_id)
                for relation in relations
                for chunk_id in relation.evidence_chunk_ids
            ),
        )

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    workspace_id TEXT NOT NULL DEFAULT 'ws-default'
                );

                CREATE TABLE IF NOT EXISTS relations (
                    id TEXT PRIMARY KEY,
                    source_entity_id TEXT NOT NULL REFERENCES entities(id),
                    target_entity_id TEXT NOT NULL REFERENCES entities(id),
                    type TEXT NOT NULL,
                    workspace_id TEXT NOT NULL DEFAULT 'ws-default'
                );

                CREATE TABLE IF NOT EXISTS relation_evidence (
                    relation_id TEXT NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
                    chunk_id TEXT NOT NULL,
                    PRIMARY KEY (relation_id, chunk_id)
                );

                CREATE INDEX IF NOT EXISTS relations_source_idx
                    ON relations(source_entity_id);
                CREATE INDEX IF NOT EXISTS relations_target_idx
                    ON relations(target_entity_id);
                """
            )
            self._migrate_workspace_columns()
            self._create_workspace_indexes()

    def _migrate_workspace_columns(self) -> None:
        """给 Workspace 概念出现之前的图索引补上归属列。

        迁移只做加法：加列、把旧行归入默认 Workspace、补索引，不动任何一行
        既有数据，也不重新导入。列定义带 `DEFAULT 'ws-default'` 是这条迁移
        规则的物化（既有 DUTMed 数据全部属于默认 Workspace），新库用的是同一
        份定义，因此两条路径得到完全相同的 schema；仓储的每个写入方法都显式
        写出 workspace_id，这个默认值在代码路径上取不到。

        重复执行不做任何事：列已经存在时直接返回。
        """
        columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(entities)")
        }
        if "workspace_id" in columns:
            return
        self._connection.executescript(
            f"""
            ALTER TABLE entities
                ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '{DEFAULT_WORKSPACE_ID}';
            ALTER TABLE relations
                ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '{DEFAULT_WORKSPACE_ID}';
            UPDATE entities SET workspace_id = '{DEFAULT_WORKSPACE_ID}'
                WHERE workspace_id IS NULL;
            UPDATE relations SET workspace_id = '{DEFAULT_WORKSPACE_ID}'
                WHERE workspace_id IS NULL;
            """
        )

    def _create_workspace_indexes(self) -> None:
        self._connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS entities_workspace_idx
                ON entities(workspace_id, type);
            CREATE INDEX IF NOT EXISTS relations_workspace_source_idx
                ON relations(workspace_id, source_entity_id);
            CREATE INDEX IF NOT EXISTS relations_workspace_target_idx
                ON relations(workspace_id, target_entity_id);
            """
        )


def _empty_expansions(node_ids: tuple[str, ...]) -> tuple[FrontierExpansion, ...]:
    return tuple(
        FrontierExpansion(node_id=node_id, steps=(), total_edges=0)
        for node_id in node_ids
    )


def _load_entities(
    connection: sqlite3.Connection, entity_ids: tuple[str, ...], workspace_id: str
) -> dict[str, Entity]:
    if not entity_ids:
        return {}
    placeholders = ",".join("?" for _ in entity_ids)
    rows = connection.execute(
        f"""
        SELECT id, name, type, workspace_id FROM entities
        WHERE id IN ({placeholders}) AND workspace_id = ?
        """,
        (*entity_ids, workspace_id),
    ).fetchall()
    return {row["id"]: _entity_from_row(row) for row in rows}


def _load_relation_evidence(
    connection: sqlite3.Connection, relation_ids: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    if not relation_ids:
        return {}
    placeholders = ",".join("?" for _ in relation_ids)
    rows = connection.execute(
        f"""
        SELECT relation_id, chunk_id FROM relation_evidence
        WHERE relation_id IN ({placeholders})
        ORDER BY relation_id, chunk_id
        """,
        relation_ids,
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["relation_id"], []).append(row["chunk_id"])
    return {relation_id: tuple(chunk_ids) for relation_id, chunk_ids in grouped.items()}


def _build_expansions(
    connection: sqlite3.Connection,
    node_ids: tuple[str, ...],
    rows: list[sqlite3.Row],
    workspace_id: str,
) -> tuple[FrontierExpansion, ...]:
    totals = {node_id: 0 for node_id in node_ids}
    steps_by_node: dict[str, list[PathStep]] = {node_id: [] for node_id in node_ids}
    if rows:
        entities = _load_entities(
            connection,
            tuple(sorted({row["neighbor_id"] for row in rows})),
            workspace_id,
        )
        evidence = _load_relation_evidence(
            connection, tuple(sorted({row["relation_id"] for row in rows}))
        )
        for row in rows:
            totals[row["node_id"]] = row["total_edges"]
            target = entities.get(row["neighbor_id"])
            chunk_ids = evidence.get(row["relation_id"])
            if target is None or not chunk_ids:
                continue
            steps_by_node[row["node_id"]].append(
                PathStep(
                    relation=Relation(
                        id=row["relation_id"],
                        source_entity_id=row["source_entity_id"],
                        target_entity_id=row["target_entity_id"],
                        type=row["relation_type"],
                        evidence_chunk_ids=chunk_ids,
                        workspace_id=workspace_id,
                    ),
                    direction=TraversalDirection(row["direction"]),
                    target=target,
                )
            )
    return tuple(
        FrontierExpansion(
            node_id=node_id,
            steps=tuple(steps_by_node[node_id]),
            total_edges=totals[node_id],
        )
        for node_id in node_ids
    )


def rank_entities(
    entities: tuple[Entity, ...], query: str, limit: int
) -> tuple[Entity, ...]:
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    normalized = "".join(query.casefold().split())
    if not normalized:
        return ()
    scored = []
    for entity in entities:
        name = "".join(entity.name.casefold().split())
        if name in normalized:
            scored.append((2.0 + len(name) / max(len(normalized), 1), entity))
            continue
        query_pairs = {normalized[index : index + 2] for index in range(len(normalized) - 1)}
        name_pairs = {name[index : index + 2] for index in range(len(name) - 1)}
        overlap = len(query_pairs.intersection(name_pairs))
        similarity = overlap / max(len(name_pairs), 1)
        if similarity >= 0.5:
            scored.append((similarity, entity))
    scored.sort(key=lambda item: (-item[0], item[1].name, item[1].id))
    return tuple(entity for _, entity in scored[:limit])


def _statistics(
    entities: tuple[Entity, ...],
    relations: tuple[Relation, ...],
    connected_entity_ids: set[str],
) -> GraphStatistics:
    return GraphStatistics(
        entities=len(entities),
        relations=len(relations),
        orphan_entities=sum(
            1 for entity in entities if entity.id not in connected_entity_ids
        ),
        entity_types=_tally(entity.type for entity in entities),
        relation_types=_tally(relation.type for relation in relations),
    )


def sqlite_graph_statistics(
    connection: sqlite3.Connection, workspace_id: str
) -> GraphStatistics:
    """某个 Workspace 的图规模概览；一致性检查命令复用同一份查询。"""
    entity_rows = connection.execute(
        "SELECT type, COUNT(*) AS total FROM entities WHERE workspace_id = ? GROUP BY type",
        (workspace_id,),
    ).fetchall()
    relation_rows = connection.execute(
        "SELECT type, COUNT(*) AS total FROM relations WHERE workspace_id = ? GROUP BY type",
        (workspace_id,),
    ).fetchall()
    # 两个 NOT EXISTS 而不是一个带 OR 的：`NOT EXISTS(A OR B)` 与
    # `NOT EXISTS(A) AND NOT EXISTS(B)` 等价，但 SQLite 拆不开 OR 里共用的
    # workspace_id，只能对每个实体全表扫一遍 relations —— 真实图谱上这个查询
    # 会跑上几分钟。拆开之后两支都走 (workspace_id, 端点) 覆盖索引。
    orphans = connection.execute(
        """
        SELECT COUNT(*) AS total FROM entities
        WHERE workspace_id = ?
          AND NOT EXISTS (
            SELECT 1 FROM relations
            WHERE relations.workspace_id = entities.workspace_id
              AND relations.source_entity_id = entities.id
        )
          AND NOT EXISTS (
            SELECT 1 FROM relations
            WHERE relations.workspace_id = entities.workspace_id
              AND relations.target_entity_id = entities.id
        )
        """,
        (workspace_id,),
    ).fetchone()["total"]
    return GraphStatistics(
        entities=sum(row["total"] for row in entity_rows),
        relations=sum(row["total"] for row in relation_rows),
        orphan_entities=orphans,
        entity_types=_type_counts(entity_rows),
        relation_types=_type_counts(relation_rows),
    )


def _tally(values: Iterable[str]) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return tuple(sorted(counts.items()))


def _type_counts(rows: Iterable[sqlite3.Row]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((row["type"], row["total"]) for row in rows))


def _entity_from_row(row: sqlite3.Row) -> Entity:
    return Entity(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        workspace_id=row["workspace_id"],
    )


def _relation_from_row(
    row: sqlite3.Row, workspace_id: str, evidence_chunk_ids: tuple[str, ...]
) -> Relation:
    return Relation(
        id=row["id"],
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        type=row["type"],
        evidence_chunk_ids=evidence_chunk_ids,
        workspace_id=workspace_id,
    )
