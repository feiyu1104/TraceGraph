from collections.abc import Iterable
from pathlib import Path
import sqlite3
from threading import RLock

from tracegraph.core.contracts import (
    Entity,
    FrontierExpansion,
    GraphStatistics,
    PathStep,
    Relation,
    TraversalDirection,
)


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
            if relation.source_entity_id not in self._entities:
                raise ValueError("关系的源实体不存在")
            if relation.target_entity_id not in self._entities:
                raise ValueError("关系的目标实体不存在")
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
            self._relations = {
                relation_id: relation
                for relation_id, relation in self._relations.items()
                if relation.source_entity_id != source.id
            }
            self._relations.update({relation.id: relation for relation in relations})

    def get_entity(self, entity_id: str) -> Entity | None:
        with self._lock:
            return self._entities.get(entity_id)

    def search_entities(self, query: str, limit: int = 5) -> tuple[Entity, ...]:
        with self._lock:
            return rank_entities(tuple(self._entities.values()), query, limit)

    def list_relations(
        self, entity_ids: tuple[str, ...], limit: int = 20
    ) -> tuple[Relation, ...]:
        known = set(entity_ids)
        with self._lock:
            relations = (
                relation
                for relation in self._relations.values()
                if relation.source_entity_id in known
                or relation.target_entity_id in known
            )
            return tuple(sorted(relations, key=lambda item: item.id)[:limit])

    def get_relation(self, relation_id: str) -> Relation | None:
        with self._lock:
            return self._relations.get(relation_id)

    def statistics(self) -> GraphStatistics:
        with self._lock:
            entities = tuple(self._entities.values())
            relations = tuple(self._relations.values())
        connected = {
            entity_id
            for relation in relations
            for entity_id in (relation.source_entity_id, relation.target_entity_id)
        }
        return _statistics(entities, relations, connected)

    def find_opposing_relations(
        self, entity_id: str, relation_types: tuple[str, str]
    ) -> tuple[Relation, ...]:
        first, second = relation_types
        with self._lock:
            relations = tuple(self._relations.values())
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
            relations = tuple(self._relations.values())
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

    def remove_evidence(self, chunk_ids: tuple[str, ...]) -> None:
        removed = set(chunk_ids)
        with self._lock:
            updated = {}
            for relation_id, relation in self._relations.items():
                remaining = tuple(
                    chunk_id
                    for chunk_id in relation.evidence_chunk_ids
                    if chunk_id not in removed
                )
                if remaining:
                    updated[relation_id] = Relation(
                        id=relation.id,
                        source_entity_id=relation.source_entity_id,
                        target_entity_id=relation.target_entity_id,
                        type=relation.type,
                        evidence_chunk_ids=remaining,
                    )
            self._relations = updated

    def delete_outgoing_relations(self, entity_id: str) -> None:
        with self._lock:
            self._relations = {
                relation_id: relation
                for relation_id, relation in self._relations.items()
                if relation.source_entity_id != entity_id
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
            self._connection.execute(
                """
                INSERT INTO entities (id, name, type)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name = excluded.name, type = excluded.type
                """,
                (entity.id, entity.name, entity.type),
            )

    def upsert_relation(self, relation: Relation) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO relations (id, source_entity_id, target_entity_id, type)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_entity_id = excluded.source_entity_id,
                    target_entity_id = excluded.target_entity_id,
                    type = excluded.type
                """,
                (
                    relation.id,
                    relation.source_entity_id,
                    relation.target_entity_id,
                    relation.type,
                ),
            )
            self._connection.execute(
                "DELETE FROM relation_evidence WHERE relation_id = ?", (relation.id,)
            )
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO relation_evidence (relation_id, chunk_id)
                VALUES (?, ?)
                """,
                ((relation.id, chunk_id) for chunk_id in relation.evidence_chunk_ids),
            )

    def replace_outgoing_graph(
        self,
        source: Entity,
        targets: tuple[Entity, ...],
        relations: tuple[Relation, ...],
    ) -> None:
        entities = {source.id: source, **{entity.id: entity for entity in targets}}
        with self._lock, self._connection:
            self._connection.executemany(
                """
                INSERT INTO entities (id, name, type)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name = excluded.name, type = excluded.type
                """,
                ((entity.id, entity.name, entity.type) for entity in entities.values()),
            )
            self._connection.execute(
                "DELETE FROM relations WHERE source_entity_id = ?", (source.id,)
            )
            self._connection.executemany(
                """
                INSERT INTO relations (id, source_entity_id, target_entity_id, type)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        relation.id,
                        relation.source_entity_id,
                        relation.target_entity_id,
                        relation.type,
                    )
                    for relation in relations
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO relation_evidence (relation_id, chunk_id)
                VALUES (?, ?)
                """,
                (
                    (relation.id, chunk_id)
                    for relation in relations
                    for chunk_id in relation.evidence_chunk_ids
                ),
            )

    def get_entity(self, entity_id: str) -> Entity | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, name, type FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return _entity_from_row(row) if row else None

    def search_entities(self, query: str, limit: int = 5) -> tuple[Entity, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, name, type FROM entities"
            ).fetchall()
        return rank_entities(tuple(_entity_from_row(row) for row in rows), query, limit)

    def list_relations(
        self, entity_ids: tuple[str, ...], limit: int = 20
    ) -> tuple[Relation, ...]:
        if not entity_ids:
            return ()
        placeholders = ",".join("?" for _ in entity_ids)
        parameters = (*entity_ids, *entity_ids, limit)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT id, source_entity_id, target_entity_id, type
                FROM relations
                WHERE source_entity_id IN ({placeholders})
                   OR target_entity_id IN ({placeholders})
                ORDER BY id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            relations = []
            for row in rows:
                evidence_rows = self._connection.execute(
                    """
                    SELECT chunk_id FROM relation_evidence
                    WHERE relation_id = ? ORDER BY chunk_id
                    """,
                    (row["id"],),
                ).fetchall()
                relations.append(
                    Relation(
                        id=row["id"],
                        source_entity_id=row["source_entity_id"],
                        target_entity_id=row["target_entity_id"],
                        type=row["type"],
                        evidence_chunk_ids=tuple(
                            evidence["chunk_id"] for evidence in evidence_rows
                        ),
                    )
                )
        return tuple(relations)

    def get_relation(self, relation_id: str) -> Relation | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, source_entity_id, target_entity_id, type
                FROM relations WHERE id = ?
                """,
                (relation_id,),
            ).fetchone()
            if row is None:
                return None
            evidence = _load_relation_evidence(self._connection, (relation_id,))
        return Relation(
            id=row["id"],
            source_entity_id=row["source_entity_id"],
            target_entity_id=row["target_entity_id"],
            type=row["type"],
            evidence_chunk_ids=evidence.get(relation_id, ()),
        )

    def find_opposing_relations(
        self, entity_id: str, relation_types: tuple[str, str]
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
                """,
                (entity_id, first, second),
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
            Relation(
                id=row["id"],
                source_entity_id=row["source_entity_id"],
                target_entity_id=row["target_entity_id"],
                type=row["type"],
                evidence_chunk_ids=evidence.get(row["id"], ()),
            )
            for row in details
        )

    def statistics(self) -> GraphStatistics:
        with self._lock:
            return sqlite_graph_statistics(self._connection)

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
            return _build_expansions(self._connection, node_ids, rows)

    def remove_evidence(self, chunk_ids: tuple[str, ...]) -> None:
        if not chunk_ids:
            return
        placeholders = ",".join("?" for _ in chunk_ids)
        with self._lock, self._connection:
            self._connection.execute(
                f"DELETE FROM relation_evidence WHERE chunk_id IN ({placeholders})",
                chunk_ids,
            )
            self._connection.execute(
                """
                DELETE FROM relations
                WHERE NOT EXISTS (
                    SELECT 1 FROM relation_evidence
                    WHERE relation_evidence.relation_id = relations.id
                )
                """
            )

    def delete_outgoing_relations(self, entity_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM relations WHERE source_entity_id = ?", (entity_id,)
            )

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS relations (
                    id TEXT PRIMARY KEY,
                    source_entity_id TEXT NOT NULL REFERENCES entities(id),
                    target_entity_id TEXT NOT NULL REFERENCES entities(id),
                    type TEXT NOT NULL
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


def _empty_expansions(node_ids: tuple[str, ...]) -> tuple[FrontierExpansion, ...]:
    return tuple(
        FrontierExpansion(node_id=node_id, steps=(), total_edges=0)
        for node_id in node_ids
    )


def _load_entities(
    connection: sqlite3.Connection, entity_ids: tuple[str, ...]
) -> dict[str, Entity]:
    if not entity_ids:
        return {}
    placeholders = ",".join("?" for _ in entity_ids)
    rows = connection.execute(
        f"SELECT id, name, type FROM entities WHERE id IN ({placeholders})",
        entity_ids,
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
) -> tuple[FrontierExpansion, ...]:
    totals = {node_id: 0 for node_id in node_ids}
    steps_by_node: dict[str, list[PathStep]] = {node_id: [] for node_id in node_ids}
    if rows:
        entities = _load_entities(
            connection, tuple(sorted({row["neighbor_id"] for row in rows}))
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


def sqlite_graph_statistics(connection: sqlite3.Connection) -> GraphStatistics:
    """图表的规模概览；一致性检查命令复用同一份查询。"""
    entity_rows = connection.execute(
        "SELECT type, COUNT(*) AS total FROM entities GROUP BY type"
    ).fetchall()
    relation_rows = connection.execute(
        "SELECT type, COUNT(*) AS total FROM relations GROUP BY type"
    ).fetchall()
    orphans = connection.execute(
        """
        SELECT COUNT(*) AS total FROM entities
        WHERE NOT EXISTS (
            SELECT 1 FROM relations
            WHERE relations.source_entity_id = entities.id
               OR relations.target_entity_id = entities.id
        )
        """
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
    return Entity(id=row["id"], name=row["name"], type=row["type"])
