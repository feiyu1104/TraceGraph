import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tracegraph.core.contracts import GraphStatistics
from tracegraph.storage.graph import sqlite_graph_statistics


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    """SQLite 库内部的引用完整性检查结果。"""

    dangling_evidence: tuple[tuple[str, str], ...]
    relations_without_evidence: tuple[str, ...]
    statistics: GraphStatistics

    @property
    def is_consistent(self) -> bool:
        return not self.dangling_evidence and not self.relations_without_evidence


def check_sqlite_consistency(database: str | Path) -> ConsistencyReport:
    """检查图侧引用是否都落在文档侧的 chunks 上。

    `relation_evidence.chunk_id` 刻意没有外键 —— 证据 chunk 由文档侧管理，
    图侧只保存 ID。这条边界的代价就是删除文档后可能留下悬空引用，因此需要
    一个显式的检查命令，而不是假设它永远成立。
    """
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        dangling = tuple(
            (row["relation_id"], row["chunk_id"])
            for row in connection.execute(
                """
                SELECT relation_evidence.relation_id, relation_evidence.chunk_id
                FROM relation_evidence
                LEFT JOIN chunks ON chunks.id = relation_evidence.chunk_id
                WHERE chunks.id IS NULL
                ORDER BY relation_evidence.relation_id, relation_evidence.chunk_id
                """
            )
        )
        without_evidence = tuple(
            row["id"]
            for row in connection.execute(
                """
                SELECT relations.id FROM relations
                WHERE NOT EXISTS (
                    SELECT 1 FROM relation_evidence
                    WHERE relation_evidence.relation_id = relations.id
                )
                ORDER BY relations.id
                """
            )
        )
        statistics = sqlite_graph_statistics(connection)
    finally:
        connection.close()
    return ConsistencyReport(
        dangling_evidence=dangling,
        relations_without_evidence=without_evidence,
        statistics=statistics,
    )
