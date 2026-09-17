import os
from dataclasses import dataclass
from pathlib import Path

from tracegraph.core.ports import GraphRepository
from tracegraph.storage.graph import SQLiteGraphRepository
from tracegraph.storage.neo4j import Neo4jGraphRepository


BACKENDS = ("sqlite", "neo4j")
FALLBACKS = ("none", "sqlite")


@dataclass(frozen=True, slots=True)
class GraphSelection:
    """实际生效的图后端，以及它与配置请求之间的差异。"""

    requested: str
    active: str
    degraded: bool = False
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            "graph_requested": self.requested,
            "graph_degraded": "true" if self.degraded else "false",
            "graph_detail": self.detail or "",
        }


def create_graph_repository(
    database: str | Path,
) -> tuple[GraphRepository, GraphSelection]:
    """按配置选择图后端。

    默认**不降级**：Neo4j 连不上就直接失败，而不是悄悄换成 SQLite ——
    换了后端就可能换掉查询结果，用户必须显式同意。只有把
    `TRACEGRAPH_GRAPH_FALLBACK` 设为 `sqlite` 时才降级，且降级事实由返回值
    记录，调用方负责写进 `/system` 与启动日志。
    """
    requested = _read_env("TRACEGRAPH_GRAPH_BACKEND", "sqlite")
    fallback = _read_env("TRACEGRAPH_GRAPH_FALLBACK", "none")
    if requested not in BACKENDS:
        raise RuntimeError(f"TRACEGRAPH_GRAPH_BACKEND 只能是 {' 或 '.join(BACKENDS)}")
    if fallback not in FALLBACKS:
        raise RuntimeError(f"TRACEGRAPH_GRAPH_FALLBACK 只能是 {' 或 '.join(FALLBACKS)}")
    if requested == "sqlite":
        return SQLiteGraphRepository(database), GraphSelection(requested, "sqlite")

    try:
        return _neo4j_repository(), GraphSelection(requested, "neo4j")
    except Exception as error:
        if fallback != "sqlite":
            raise
        return (
            SQLiteGraphRepository(database),
            GraphSelection(requested, "sqlite", degraded=True, detail=str(error)),
        )


def _neo4j_repository() -> GraphRepository:
    password = os.getenv("NEO4J_PASSWORD")
    if not password:
        raise RuntimeError("使用 Neo4j 时必须设置 NEO4J_PASSWORD")
    return Neo4jGraphRepository(
        uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_USER", "neo4j"),
        password=password,
        database=os.getenv("NEO4J_DATABASE", "neo4j"),
    )


def _read_env(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip().casefold() or default
