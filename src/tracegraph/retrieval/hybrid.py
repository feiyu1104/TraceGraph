from dataclasses import replace
import hashlib

from tracegraph.core.contracts import (
    DEFAULT_MAX_HOPS,
    DEFAULT_WORKSPACE_ID,
    Evidence,
)
from tracegraph.core.ports import Retriever


class HybridRetriever:
    """使用 reciprocal-rank fusion 合并多个检索器。"""

    name = "hybrid"

    def __init__(
        self,
        retrievers: tuple[Retriever, ...],
        weights: dict[str, float] | None = None,
        rank_constant: int = 1,
    ) -> None:
        if len(retrievers) < 2:
            raise ValueError("混合检索至少需要两个检索器")
        if rank_constant < 0:
            raise ValueError("rank_constant 不能小于 0")
        self.retrievers = retrievers
        self.weights = {"graph": 4.0, **(weights or {})}
        self.rank_constant = rank_constant

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        max_hops: int = DEFAULT_MAX_HOPS,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> tuple[Evidence, ...]:
        if limit < 1:
            raise ValueError("limit 必须大于 0")
        scores: dict[str, float] = {}
        evidence_by_chunk: dict[str, Evidence] = {}
        for retriever in self.retrievers:
            seen_chunks: set[str] = set()
            weight = self.weights.get(retriever.name, 1.0)
            for rank, evidence in enumerate(
                retriever.retrieve(query, limit, max_hops, workspace_id), start=1
            ):
                if evidence.chunk_id in seen_chunks:
                    continue
                seen_chunks.add(evidence.chunk_id)
                scores[evidence.chunk_id] = scores.get(
                    evidence.chunk_id, 0.0
                ) + weight / (self.rank_constant + rank)
                current = evidence_by_chunk.get(evidence.chunk_id)
                if current is None or (not current.graph_path and evidence.graph_path):
                    evidence_by_chunk[evidence.chunk_id] = evidence
        ranked_ids = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
        if not ranked_ids:
            return ()
        maximum = sum(
            self.weights.get(retriever.name, 1.0) / (self.rank_constant + 1)
            for retriever in self.retrievers
        )
        return tuple(
            replace(
                evidence_by_chunk[chunk_id],
                id=_stable_evidence_id(chunk_id),
                retrieval_method="hybrid",
                retrieval_score=round(scores[chunk_id] / maximum, 6),
            )
            for chunk_id in ranked_ids[:limit]
        )


def _stable_evidence_id(chunk_id: str) -> str:
    raw = f"hybrid\x1f{chunk_id}".encode("utf-8")
    return f"ev-{hashlib.sha256(raw).hexdigest()[:20]}"
