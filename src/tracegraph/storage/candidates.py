"""候选知识的持久化：抽取任务、候选实体、候选关系，以及它们的 Chunk 证据关联。

候选只写进这五张表。本模块不引用图仓储，也没有任何一条通往图后端的路径：
未经审核的候选不允许进入正式图谱，因此这里连「可能写图」的入口都不存在。
"""

from dataclasses import replace
from pathlib import Path
import sqlite3
from threading import RLock

from tracegraph.core.contracts import (
    CandidateEntity,
    CandidateEvidence,
    CandidateKind,
    CandidateRelation,
    CandidateStatus,
    CandidateTally,
    ExtractionRun,
    ExtractionStatus,
)


# 判断「该文档名下有没有已发布的候选」：实体与关系各查一次，任何一个命中
# 就算命中。参数顺序是 (workspace_id, document_id) 重复两遍。
_PUBLISHED_QUERY = """
    SELECT 1 FROM candidate_entities
    WHERE workspace_id = ? AND document_id = ? AND published_at IS NOT NULL
    UNION ALL
    SELECT 1 FROM candidate_relations
    WHERE workspace_id = ? AND document_id = ? AND published_at IS NOT NULL
"""


def _validate_batch(
    run: ExtractionRun,
    entities: tuple[CandidateEntity, ...],
    relations: tuple[CandidateRelation, ...],
) -> None:
    """两个实现共用的批次校验。

    同一抽取任务内的重复实体与重复关系在这里被拒绝：去重是抽取服务的职责，
    存储层只保证「真的发过来两遍」不会变成两条记录（SQLite 侧还有唯一约束
    兜底）。关系两端必须在同一批候选实体里，否则关系就指向了不存在的东西。
    """
    seen_entities: set[tuple[str, str]] = set()
    entity_ids: set[str] = set()
    for entity in entities:
        if entity.extraction_run_id != run.id:
            raise ValueError("候选实体必须属于待保存的抽取任务")
        key = (entity.normalized_name, entity.type)
        if key in seen_entities:
            raise ValueError(f"同一抽取任务内出现重复实体：{entity.name}")
        seen_entities.add(key)
        entity_ids.add(entity.id)

    seen_relations: set[tuple[str, str, str]] = set()
    for relation in relations:
        if relation.extraction_run_id != run.id:
            raise ValueError("候选关系必须属于待保存的抽取任务")
        if (
            relation.source_entity_id not in entity_ids
            or relation.target_entity_id not in entity_ids
        ):
            raise ValueError("候选关系的两端必须是本批候选实体")
        key = (relation.source_entity_id, relation.type, relation.target_entity_id)
        if key in seen_relations:
            raise ValueError(f"同一抽取任务内出现重复关系：{relation.type}")
        seen_relations.add(key)


class InMemoryCandidateRepository:
    """开发与测试用的候选存储；行为与 SQLite 实现逐条对齐。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._runs: dict[str, ExtractionRun] = {}
        self._entities: dict[str, CandidateEntity] = {}
        self._relations: dict[str, CandidateRelation] = {}

    def save_run(self, run: ExtractionRun) -> None:
        with self._lock:
            self._runs[run.id] = run

    def save_extraction(
        self,
        run: ExtractionRun,
        entities: tuple[CandidateEntity, ...],
        relations: tuple[CandidateRelation, ...],
    ) -> None:
        # 先整体校验、再一次性写入：校验失败时一个字都不落，与 SQLite 侧
        # 「一个事务」的语义一致。
        _validate_batch(run, entities, relations)
        with self._lock:
            for entity in entities:
                self._entities[entity.id] = entity
            for relation in relations:
                self._relations[relation.id] = relation
            self._runs[run.id] = run

    def get_run(self, run_id: str) -> ExtractionRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_entities(self, run_id: str) -> tuple[CandidateEntity, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        entity
                        for entity in self._entities.values()
                        if entity.extraction_run_id == run_id
                    ),
                    key=lambda entity: (entity.normalized_name, entity.id),
                )
            )

    def list_relations(self, run_id: str) -> tuple[CandidateRelation, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        relation
                        for relation in self._relations.values()
                        if relation.extraction_run_id == run_id
                    ),
                    key=lambda relation: (relation.type, relation.id),
                )
            )

    def list_evidence(self, run_id: str) -> tuple[CandidateEvidence, ...]:
        with self._lock:
            entities = self.list_entities(run_id)
            relations = self.list_relations(run_id)
        return (
            *(
                CandidateEvidence(
                    candidate_id=entity.id,
                    candidate_kind=CandidateKind.ENTITY,
                    chunk_id=chunk_id,
                )
                for entity in entities
                for chunk_id in entity.evidence_chunk_ids
            ),
            *(
                CandidateEvidence(
                    candidate_id=relation.id,
                    candidate_kind=CandidateKind.RELATION,
                    chunk_id=chunk_id,
                )
                for relation in relations
                for chunk_id in relation.evidence_chunk_ids
            ),
        )

    def list_workspace_entities(
        self,
        workspace_id: str,
        *,
        document_id: str | None = None,
        status: CandidateStatus | None = None,
        entity_type: str | None = None,
    ) -> tuple[CandidateEntity, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        entity
                        for entity in self._entities.values()
                        if _matches(
                            entity.workspace_id,
                            entity.document_id,
                            entity.status,
                            entity.type,
                            workspace_id,
                            document_id,
                            status,
                            entity_type,
                        )
                    ),
                    key=lambda entity: (entity.extraction_run_id, entity.normalized_name, entity.id),
                )
            )

    def list_workspace_relations(
        self,
        workspace_id: str,
        *,
        document_id: str | None = None,
        status: CandidateStatus | None = None,
        relation_type: str | None = None,
    ) -> tuple[CandidateRelation, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        relation
                        for relation in self._relations.values()
                        if _matches(
                            relation.workspace_id,
                            relation.document_id,
                            relation.status,
                            relation.type,
                            workspace_id,
                            document_id,
                            status,
                            relation_type,
                        )
                    ),
                    key=lambda relation: (relation.extraction_run_id, relation.type, relation.id),
                )
            )

    def get_entity(self, candidate_id: str) -> CandidateEntity | None:
        with self._lock:
            return self._entities.get(candidate_id)

    def get_relation(self, candidate_id: str) -> CandidateRelation | None:
        with self._lock:
            return self._relations.get(candidate_id)

    def list_published_relations(
        self, workspace_id: str, graph_relation_id: str
    ) -> tuple[CandidateRelation, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        relation
                        for relation in self._relations.values()
                        if relation.workspace_id == workspace_id
                        and relation.graph_id == graph_relation_id
                        and relation.is_published
                    ),
                    key=lambda relation: (relation.extraction_run_id, relation.id),
                )
            )

    def save_entity(self, entity: CandidateEntity) -> None:
        # 只替换可变字段：归属、文档与证据由抽取产生，服务层即使传进来一个
        # 改过 document_id 的对象，落库的仍是原来那一份。
        with self._lock:
            stored = self._entities.get(entity.id)
            if stored is None or stored.workspace_id != entity.workspace_id:
                raise KeyError(entity.id)
            self._entities[entity.id] = replace(
                stored,
                name=entity.name,
                normalized_name=entity.normalized_name,
                type=entity.type,
                status=entity.status,
                updated_at=entity.updated_at,
                published_at=entity.published_at,
                graph_id=entity.graph_id,
            )

    def save_relation(self, relation: CandidateRelation) -> None:
        with self._lock:
            stored = self._relations.get(relation.id)
            if stored is None or stored.workspace_id != relation.workspace_id:
                raise KeyError(relation.id)
            self._relations[relation.id] = replace(
                stored,
                source_entity_id=relation.source_entity_id,
                target_entity_id=relation.target_entity_id,
                type=relation.type,
                status=relation.status,
                updated_at=relation.updated_at,
                published_at=relation.published_at,
                graph_id=relation.graph_id,
            )

    def apply_review(
        self,
        workspace_id: str,
        *,
        entity_ids: tuple[str, ...],
        relation_ids: tuple[str, ...],
        status: CandidateStatus,
        updated_at: str,
    ) -> None:
        # 先整体检查、再整体写入：任何一个不在这个 Workspace 里，整批一个字
        # 都不改，与 SQLite 侧「一个事务」的语义一致。
        with self._lock:
            unowned = self._unowned(workspace_id, entity_ids, relation_ids)
            if unowned is not None:
                raise KeyError(unowned)
            for candidate_id in entity_ids:
                self._entities[candidate_id] = replace(
                    self._entities[candidate_id], status=status, updated_at=updated_at
                )
            for candidate_id in relation_ids:
                self._relations[candidate_id] = replace(
                    self._relations[candidate_id], status=status, updated_at=updated_at
                )

    def _unowned(
        self,
        workspace_id: str,
        entity_ids: tuple[str, ...],
        relation_ids: tuple[str, ...],
    ) -> str | None:
        """返回第一个不存在或不属于该 Workspace 的候选 ID。"""
        for candidate_id in entity_ids:
            entity = self._entities.get(candidate_id)
            if entity is None or entity.workspace_id != workspace_id:
                return candidate_id
        for candidate_id in relation_ids:
            relation = self._relations.get(candidate_id)
            if relation is None or relation.workspace_id != workspace_id:
                return candidate_id
        return None

    def mark_published(
        self,
        workspace_id: str,
        *,
        entities: tuple[tuple[str, str], ...],
        relations: tuple[tuple[str, str], ...],
        published_at: str,
    ) -> None:
        with self._lock:
            unowned = self._unowned(
                workspace_id,
                tuple(candidate_id for candidate_id, _ in entities),
                tuple(candidate_id for candidate_id, _ in relations),
            )
            if unowned is not None:
                raise KeyError(unowned)
            for candidate_id, graph_id in entities:
                self._entities[candidate_id] = replace(
                    self._entities[candidate_id],
                    published_at=published_at,
                    graph_id=graph_id,
                )
            for candidate_id, graph_id in relations:
                self._relations[candidate_id] = replace(
                    self._relations[candidate_id],
                    published_at=published_at,
                    graph_id=graph_id,
                )

    def has_published_candidates(self, workspace_id: str, document_id: str) -> bool:
        with self._lock:
            return any(
                candidate.workspace_id == workspace_id
                and candidate.document_id == document_id
                and candidate.is_published
                for candidate in (*self._entities.values(), *self._relations.values())
            )

    def tally_documents(
        self, workspace_id: str, document_ids: tuple[str, ...]
    ) -> dict[str, CandidateTally]:
        with self._lock:
            # 请求里的每个文档都要有一份计数：没有候选的文档计为零，而不是
            # 缺席，调用方不必为「查不到」单独分支。
            totals = {
                document_id: {"pending": 0, "approved": 0, "rejected": 0, "published": 0}
                for document_id in document_ids
            }
            for candidate in (*self._entities.values(), *self._relations.values()):
                if candidate.workspace_id != workspace_id:
                    continue
                counts = totals.get(candidate.document_id)
                if counts is None:
                    continue
                counts[candidate.status.value] += 1
                if candidate.is_published:
                    counts["published"] += 1
        return {
            document_id: CandidateTally(**counts) for document_id, counts in totals.items()
        }

    def documents_with_runs(
        self, workspace_id: str, document_ids: tuple[str, ...]
    ) -> frozenset[str]:
        wanted = set(document_ids)
        with self._lock:
            return frozenset(
                run.document_id
                for run in self._runs.values()
                if run.workspace_id == workspace_id and run.document_id in wanted
            )

    def delete_document(self, document_id: str) -> None:
        with self._lock:
            self._relations = {
                relation_id: relation
                for relation_id, relation in self._relations.items()
                if relation.document_id != document_id
            }
            self._entities = {
                entity_id: entity
                for entity_id, entity in self._entities.items()
                if entity.document_id != document_id
            }
            self._runs = {
                run_id: run
                for run_id, run in self._runs.items()
                if run.document_id != document_id
            }


def _matches(
    workspace_id: str,
    document_id: str,
    status: CandidateStatus,
    candidate_type: str,
    wanted_workspace: str,
    wanted_document: str | None,
    wanted_status: CandidateStatus | None,
    wanted_type: str | None,
) -> bool:
    """Workspace 是硬条件，其余筛选条件为 None 时不过滤。"""
    return (
        workspace_id == wanted_workspace
        and (wanted_document is None or document_id == wanted_document)
        and (wanted_status is None or status is wanted_status)
        and (wanted_type is None or candidate_type == wanted_type)
    )


class SQLiteCandidateRepository:
    """候选知识的 SQLite 存储。

    迁移是幂等的：五张表都用 CREATE TABLE IF NOT EXISTS 建，既有库打开时
    只做加法，重复构造同一个文件不会改动已有数据。
    """

    def __init__(self, database: str | Path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteCandidateRepository":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def save_run(self, run: ExtractionRun) -> None:
        with self._lock, self._connection:
            self._write_run(run)

    def save_extraction(
        self,
        run: ExtractionRun,
        entities: tuple[CandidateEntity, ...],
        relations: tuple[CandidateRelation, ...],
    ) -> None:
        _validate_batch(run, entities, relations)
        with self._lock, self._connection:
            # 一个事务：候选、证据关联与任务终态一起提交，失败整批回滚。任务行
            # 必须最先写 —— 候选的 extraction_run_id 引用它。
            self._write_run(run)
            self._connection.executemany(
                """
                INSERT INTO candidate_entities (
                    id, extraction_run_id, workspace_id, document_id,
                    document_version_id, adapter_id, name, normalized_name,
                    type, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        entity.id,
                        entity.extraction_run_id,
                        entity.workspace_id,
                        entity.document_id,
                        entity.document_version_id,
                        entity.adapter_id,
                        entity.name,
                        entity.normalized_name,
                        entity.type,
                        entity.status.value,
                        entity.created_at,
                        entity.updated_at,
                    )
                    for entity in entities
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO candidate_entity_evidence (candidate_entity_id, chunk_id)
                VALUES (?, ?)
                """,
                (
                    (entity.id, chunk_id)
                    for entity in entities
                    for chunk_id in entity.evidence_chunk_ids
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO candidate_relations (
                    id, extraction_run_id, workspace_id, document_id,
                    document_version_id, adapter_id, source_entity_id,
                    target_entity_id, type, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        relation.id,
                        relation.extraction_run_id,
                        relation.workspace_id,
                        relation.document_id,
                        relation.document_version_id,
                        relation.adapter_id,
                        relation.source_entity_id,
                        relation.target_entity_id,
                        relation.type,
                        relation.status.value,
                        relation.created_at,
                        relation.updated_at,
                    )
                    for relation in relations
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO candidate_relation_evidence (candidate_relation_id, chunk_id)
                VALUES (?, ?)
                """,
                (
                    (relation.id, chunk_id)
                    for relation in relations
                    for chunk_id in relation.evidence_chunk_ids
                ),
            )

    def get_run(self, run_id: str) -> ExtractionRun | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM extraction_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(row) if row else None

    def list_entities(self, run_id: str) -> tuple[CandidateEntity, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM candidate_entities
                WHERE extraction_run_id = ?
                ORDER BY normalized_name, id
                """,
                (run_id,),
            ).fetchall()
            evidence = self._entity_evidence(tuple(row["id"] for row in rows))
        return tuple(
            _entity_from_row(row, evidence.get(row["id"], ())) for row in rows
        )

    def list_relations(self, run_id: str) -> tuple[CandidateRelation, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM candidate_relations
                WHERE extraction_run_id = ?
                ORDER BY type, id
                """,
                (run_id,),
            ).fetchall()
            evidence = self._relation_evidence(tuple(row["id"] for row in rows))
        return tuple(
            _relation_from_row(row, evidence.get(row["id"], ())) for row in rows
        )

    def list_evidence(self, run_id: str) -> tuple[CandidateEvidence, ...]:
        with self._lock:
            entity_rows = self._connection.execute(
                """
                SELECT evidence.candidate_entity_id AS candidate_id, evidence.chunk_id
                FROM candidate_entity_evidence AS evidence
                JOIN candidate_entities AS candidate
                  ON candidate.id = evidence.candidate_entity_id
                WHERE candidate.extraction_run_id = ?
                ORDER BY evidence.candidate_entity_id, evidence.chunk_id
                """,
                (run_id,),
            ).fetchall()
            relation_rows = self._connection.execute(
                """
                SELECT evidence.candidate_relation_id AS candidate_id, evidence.chunk_id
                FROM candidate_relation_evidence AS evidence
                JOIN candidate_relations AS candidate
                  ON candidate.id = evidence.candidate_relation_id
                WHERE candidate.extraction_run_id = ?
                ORDER BY evidence.candidate_relation_id, evidence.chunk_id
                """,
                (run_id,),
            ).fetchall()
        return (
            *(
                CandidateEvidence(
                    candidate_id=row["candidate_id"],
                    candidate_kind=CandidateKind.ENTITY,
                    chunk_id=row["chunk_id"],
                )
                for row in entity_rows
            ),
            *(
                CandidateEvidence(
                    candidate_id=row["candidate_id"],
                    candidate_kind=CandidateKind.RELATION,
                    chunk_id=row["chunk_id"],
                )
                for row in relation_rows
            ),
        )

    def list_workspace_entities(
        self,
        workspace_id: str,
        *,
        document_id: str | None = None,
        status: CandidateStatus | None = None,
        entity_type: str | None = None,
    ) -> tuple[CandidateEntity, ...]:
        query, parameters = _workspace_query(
            "candidate_entities", workspace_id, document_id, status, entity_type
        )
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
            evidence = self._entity_evidence(tuple(row["id"] for row in rows))
        return tuple(
            _entity_from_row(row, evidence.get(row["id"], ())) for row in rows
        )

    def list_workspace_relations(
        self,
        workspace_id: str,
        *,
        document_id: str | None = None,
        status: CandidateStatus | None = None,
        relation_type: str | None = None,
    ) -> tuple[CandidateRelation, ...]:
        query, parameters = _workspace_query(
            "candidate_relations", workspace_id, document_id, status, relation_type
        )
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
            evidence = self._relation_evidence(tuple(row["id"] for row in rows))
        return tuple(
            _relation_from_row(row, evidence.get(row["id"], ())) for row in rows
        )

    def get_entity(self, candidate_id: str) -> CandidateEntity | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM candidate_entities WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                return None
            evidence = self._entity_evidence((candidate_id,))
        return _entity_from_row(row, evidence.get(candidate_id, ()))

    def get_relation(self, candidate_id: str) -> CandidateRelation | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM candidate_relations WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                return None
            evidence = self._relation_evidence((candidate_id,))
        return _relation_from_row(row, evidence.get(candidate_id, ()))

    def list_published_relations(
        self, workspace_id: str, graph_relation_id: str
    ) -> tuple[CandidateRelation, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM candidate_relations
                WHERE workspace_id = ? AND graph_id = ? AND published_at IS NOT NULL
                ORDER BY extraction_run_id, id
                """,
                (workspace_id, graph_relation_id),
            ).fetchall()
            evidence = self._relation_evidence(tuple(row["id"] for row in rows))
        return tuple(
            _relation_from_row(row, evidence.get(row["id"], ())) for row in rows
        )

    def save_entity(self, entity: CandidateEntity) -> None:
        # 可写列就是这几列：workspace_id 与 document_id 等既不在 SET 里，也在
        # WHERE 里参与匹配，因此「把候选改到别的 Workspace / 别的文档去」在
        # SQL 层面写不出来，不需要再靠调用方自觉。
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE candidate_entities
                SET name = ?, normalized_name = ?, type = ?, status = ?,
                    updated_at = ?, published_at = ?, graph_id = ?
                WHERE id = ? AND workspace_id = ?
                """,
                (
                    entity.name,
                    entity.normalized_name,
                    entity.type,
                    entity.status.value,
                    entity.updated_at,
                    entity.published_at,
                    entity.graph_id,
                    entity.id,
                    entity.workspace_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(entity.id)

    def save_relation(self, relation: CandidateRelation) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE candidate_relations
                SET source_entity_id = ?, target_entity_id = ?, type = ?, status = ?,
                    updated_at = ?, published_at = ?, graph_id = ?
                WHERE id = ? AND workspace_id = ?
                """,
                (
                    relation.source_entity_id,
                    relation.target_entity_id,
                    relation.type,
                    relation.status.value,
                    relation.updated_at,
                    relation.published_at,
                    relation.graph_id,
                    relation.id,
                    relation.workspace_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(relation.id)

    def apply_review(
        self,
        workspace_id: str,
        *,
        entity_ids: tuple[str, ...],
        relation_ids: tuple[str, ...],
        status: CandidateStatus,
        updated_at: str,
    ) -> None:
        # 一个事务：批量审核里只要有一条对不上 Workspace，抛出的 KeyError 会
        # 让整个 with 块回滚，不存在「改了一半」的中间状态。
        with self._lock, self._connection:
            for table, candidate_ids in (
                ("candidate_entities", entity_ids),
                ("candidate_relations", relation_ids),
            ):
                for candidate_id in candidate_ids:
                    cursor = self._connection.execute(
                        f"""
                        UPDATE {table} SET status = ?, updated_at = ?
                        WHERE id = ? AND workspace_id = ?
                        """,
                        (status.value, updated_at, candidate_id, workspace_id),
                    )
                    if cursor.rowcount != 1:
                        raise KeyError(candidate_id)

    def mark_published(
        self,
        workspace_id: str,
        *,
        entities: tuple[tuple[str, str], ...],
        relations: tuple[tuple[str, str], ...],
        published_at: str,
    ) -> None:
        with self._lock, self._connection:
            for table, pairs in (
                ("candidate_entities", entities),
                ("candidate_relations", relations),
            ):
                for candidate_id, graph_id in pairs:
                    cursor = self._connection.execute(
                        f"""
                        UPDATE {table} SET published_at = ?, graph_id = ?
                        WHERE id = ? AND workspace_id = ?
                        """,
                        (published_at, graph_id, candidate_id, workspace_id),
                    )
                    if cursor.rowcount != 1:
                        raise KeyError(candidate_id)

    def has_published_candidates(self, workspace_id: str, document_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT EXISTS({_PUBLISHED_QUERY}) AS found
                """,
                (workspace_id, document_id, workspace_id, document_id),
            ).fetchone()
        return bool(row["found"])

    def tally_documents(
        self, workspace_id: str, document_ids: tuple[str, ...]
    ) -> dict[str, CandidateTally]:
        if not document_ids:
            return {}
        placeholders = ",".join("?" for _ in document_ids)
        counts: dict[str, dict[str, int]] = {
            document_id: {"pending": 0, "approved": 0, "rejected": 0, "published": 0}
            for document_id in document_ids
        }
        with self._lock:
            for table in ("candidate_entities", "candidate_relations"):
                # published 与审核状态正交：一条已批准的候选发布后仍然计在
                # approved 里，因此两个计数分开累加，而不是互相排斥。
                rows = self._connection.execute(
                    f"""
                    SELECT document_id, status,
                           COUNT(*) AS total,
                           SUM(CASE WHEN published_at IS NULL THEN 0 ELSE 1 END)
                               AS published
                    FROM {table}
                    WHERE workspace_id = ? AND document_id IN ({placeholders})
                    GROUP BY document_id, status
                    """,
                    (workspace_id, *document_ids),
                ).fetchall()
                for row in rows:
                    bucket = counts[row["document_id"]]
                    bucket[row["status"]] += row["total"]
                    bucket["published"] += row["published"]
        return {
            document_id: CandidateTally(**bucket)
            for document_id, bucket in counts.items()
        }

    def documents_with_runs(
        self, workspace_id: str, document_ids: tuple[str, ...]
    ) -> frozenset[str]:
        if not document_ids:
            return frozenset()
        placeholders = ",".join("?" for _ in document_ids)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT DISTINCT document_id FROM extraction_runs
                WHERE workspace_id = ? AND document_id IN ({placeholders})
                """,
                (workspace_id, *document_ids),
            ).fetchall()
        return frozenset(row["document_id"] for row in rows)

    def delete_document(self, document_id: str) -> None:
        with self._lock, self._connection:
            # 顺序由外键决定：证据关联引用候选，候选引用抽取任务，因此从小到大删。
            self._connection.execute(
                """
                DELETE FROM candidate_relation_evidence
                WHERE candidate_relation_id IN (
                    SELECT id FROM candidate_relations WHERE document_id = ?
                )
                """,
                (document_id,),
            )
            self._connection.execute(
                """
                DELETE FROM candidate_entity_evidence
                WHERE candidate_entity_id IN (
                    SELECT id FROM candidate_entities WHERE document_id = ?
                )
                """,
                (document_id,),
            )
            self._connection.execute(
                "DELETE FROM candidate_relations WHERE document_id = ?", (document_id,)
            )
            self._connection.execute(
                "DELETE FROM candidate_entities WHERE document_id = ?", (document_id,)
            )
            self._connection.execute(
                "DELETE FROM extraction_runs WHERE document_id = ?", (document_id,)
            )

    def _entity_evidence(
        self, entity_ids: tuple[str, ...]
    ) -> dict[str, tuple[str, ...]]:
        return self._evidence_by(
            "candidate_entity_evidence", "candidate_entity_id", entity_ids
        )

    def _relation_evidence(
        self, relation_ids: tuple[str, ...]
    ) -> dict[str, tuple[str, ...]]:
        return self._evidence_by(
            "candidate_relation_evidence", "candidate_relation_id", relation_ids
        )

    def _evidence_by(
        self, table: str, column: str, candidate_ids: tuple[str, ...]
    ) -> dict[str, tuple[str, ...]]:
        if not candidate_ids:
            return {}
        grouped: dict[str, list[str]] = {}
        for start in range(0, len(candidate_ids), 400):
            batch = candidate_ids[start : start + 400]
            rows = self._connection.execute(
                f"""
                SELECT {column} AS candidate_id, chunk_id FROM {table}
                WHERE {column} IN ({",".join("?" for _ in batch)})
                ORDER BY chunk_id
                """,
                batch,
            ).fetchall()
            for row in rows:
                grouped.setdefault(row["candidate_id"], []).append(row["chunk_id"])
        return {candidate_id: tuple(ids) for candidate_id, ids in grouped.items()}

    def _write_run(self, run: ExtractionRun) -> None:
        self._connection.execute(
            """
            INSERT INTO extraction_runs (
                id, workspace_id, document_id, document_version_id, adapter_id,
                model_id, status, entity_count, relation_count, error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                entity_count = excluded.entity_count,
                relation_count = excluded.relation_count,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                run.id,
                run.workspace_id,
                run.document_id,
                run.document_version_id,
                run.adapter_id,
                run.model_id,
                run.status.value,
                run.entity_count,
                run.relation_count,
                run.error,
                run.created_at,
                run.updated_at,
            ),
        )

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS extraction_runs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
                    adapter_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entity_count INTEGER NOT NULL DEFAULT 0 CHECK (entity_count >= 0),
                    relation_count INTEGER NOT NULL DEFAULT 0 CHECK (relation_count >= 0),
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS extraction_runs_by_document
                    ON extraction_runs (workspace_id, document_id);

                CREATE TABLE IF NOT EXISTS candidate_entities (
                    id TEXT PRIMARY KEY,
                    extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
                    adapter_id TEXT NOT NULL,
                    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                    normalized_name TEXT NOT NULL CHECK (length(trim(normalized_name)) > 0),
                    type TEXT NOT NULL CHECK (length(trim(type)) > 0),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    -- 发布结果与审核状态分开存：published_at 非空即已发布，
                    -- graph_id 是它在图后端的落点。两者同时为空表示还没发布。
                    published_at TEXT,
                    graph_id TEXT,
                    -- 同一抽取任务内，同名同类型的实体只有一条。
                    UNIQUE (extraction_run_id, normalized_name, type)
                );

                CREATE INDEX IF NOT EXISTS candidate_entities_by_workspace
                    ON candidate_entities (workspace_id, document_id, status, type);

                CREATE TABLE IF NOT EXISTS candidate_relations (
                    id TEXT PRIMARY KEY,
                    extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
                    adapter_id TEXT NOT NULL,
                    source_entity_id TEXT NOT NULL REFERENCES candidate_entities(id),
                    target_entity_id TEXT NOT NULL REFERENCES candidate_entities(id),
                    type TEXT NOT NULL CHECK (length(trim(type)) > 0),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT,
                    graph_id TEXT,
                    CHECK (source_entity_id <> target_entity_id),
                    UNIQUE (extraction_run_id, source_entity_id, target_entity_id, type)
                );

                CREATE INDEX IF NOT EXISTS candidate_relations_by_workspace
                    ON candidate_relations (workspace_id, document_id, status, type);

                -- 证据关联的 chunk_id 指向真实的 chunks 行：没有真实 Chunk 的
                -- 候选连写都写不进来，而不是靠调用方自觉。
                CREATE TABLE IF NOT EXISTS candidate_entity_evidence (
                    candidate_entity_id TEXT NOT NULL REFERENCES candidate_entities(id),
                    chunk_id TEXT NOT NULL REFERENCES chunks(id),
                    PRIMARY KEY (candidate_entity_id, chunk_id)
                );

                CREATE TABLE IF NOT EXISTS candidate_relation_evidence (
                    candidate_relation_id TEXT NOT NULL REFERENCES candidate_relations(id),
                    chunk_id TEXT NOT NULL REFERENCES chunks(id),
                    PRIMARY KEY (candidate_relation_id, chunk_id)
                );
                """
            )
            self._migrate_publication_columns()

    def _migrate_publication_columns(self) -> None:
        """给发布功能出现之前的候选表补上发布结果两列。

        只做加法：既有行的 published_at 与 graph_id 为空，读出来就是「还没
        发布」，与它们实际的处境一致。逐列判断，因此重复打开同一个文件不会
        重复添加，也不会改动任何已有的候选、证据或审核状态。
        """
        for table in ("candidate_entities", "candidate_relations"):
            columns = {
                row["name"]
                for row in self._connection.execute(f"PRAGMA table_info({table})")
            }
            for column in ("published_at", "graph_id"):
                if column not in columns:
                    self._connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} TEXT"
                    )


def _workspace_query(
    table: str,
    workspace_id: str,
    document_id: str | None,
    status: CandidateStatus | None,
    candidate_type: str | None,
) -> tuple[str, tuple[str, ...]]:
    """Workspace 是写死在 WHERE 里的硬条件，其余条件按需追加。

    筛选在 SQL 里完成，因此别的 Workspace 的候选根本不进结果集，
    而不是「先全量取出、再在末尾过滤」。
    """
    query = f"SELECT * FROM {table} WHERE workspace_id = ?"
    parameters = [workspace_id]
    if document_id is not None:
        query += " AND document_id = ?"
        parameters.append(document_id)
    if status is not None:
        query += " AND status = ?"
        parameters.append(status.value)
    if candidate_type is not None:
        query += " AND type = ?"
        parameters.append(candidate_type)
    query += " ORDER BY extraction_run_id, normalized_name, id" if table.endswith(
        "entities"
    ) else " ORDER BY extraction_run_id, type, id"
    return query, tuple(parameters)


def _run_from_row(row: sqlite3.Row) -> ExtractionRun:
    return ExtractionRun(
        id=row["id"],
        workspace_id=row["workspace_id"],
        document_id=row["document_id"],
        document_version_id=row["document_version_id"],
        adapter_id=row["adapter_id"],
        model_id=row["model_id"],
        status=ExtractionStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        entity_count=row["entity_count"],
        relation_count=row["relation_count"],
        error=row["error"],
    )


def _entity_from_row(
    row: sqlite3.Row, evidence_chunk_ids: tuple[str, ...]
) -> CandidateEntity:
    return CandidateEntity(
        id=row["id"],
        extraction_run_id=row["extraction_run_id"],
        workspace_id=row["workspace_id"],
        document_id=row["document_id"],
        document_version_id=row["document_version_id"],
        adapter_id=row["adapter_id"],
        name=row["name"],
        normalized_name=row["normalized_name"],
        type=row["type"],
        evidence_chunk_ids=evidence_chunk_ids,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=CandidateStatus(row["status"]),
        published_at=row["published_at"],
        graph_id=row["graph_id"],
    )


def _relation_from_row(
    row: sqlite3.Row, evidence_chunk_ids: tuple[str, ...]
) -> CandidateRelation:
    return CandidateRelation(
        id=row["id"],
        extraction_run_id=row["extraction_run_id"],
        workspace_id=row["workspace_id"],
        document_id=row["document_id"],
        document_version_id=row["document_version_id"],
        adapter_id=row["adapter_id"],
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        type=row["type"],
        evidence_chunk_ids=evidence_chunk_ids,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=CandidateStatus(row["status"]),
        published_at=row["published_at"],
        graph_id=row["graph_id"],
    )
