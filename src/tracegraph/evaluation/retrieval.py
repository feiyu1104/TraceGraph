from dataclasses import dataclass
import json
from pathlib import Path
from statistics import fmean

from tracegraph.core.contracts import Evidence
from tracegraph.core.ports import DocumentRepository, Retriever


# 评测固定单跳：金标准是「原文事实」chunk，多跳推导出的关联会占用 top-k 名额
# 并把真正的金标准挤出，使 recall 指标下降 —— 那是口径变化，不是质量变化。
# 需要单独衡量多跳效果时另跑 max_hops=2/3，不要改这里的默认。
_EVALUATION_MAX_HOPS = 1


@dataclass(frozen=True, slots=True)
class ChunkReference:
    source_name: str
    locator: str


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationCase:
    id: str
    query: str
    relevant: tuple[ChunkReference, ...]
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.query.strip():
            raise ValueError("评测案例需要非空 id 和 query")
        if not self.relevant:
            raise ValueError("评测案例至少需要一个相关 Chunk")


@dataclass(frozen=True, slots=True)
class RetrievalCaseResult:
    id: str
    query: str
    relevant_chunk_ids: tuple[str, ...]
    retrieved_chunk_ids: tuple[str, ...]
    relevant_references: tuple[ChunkReference, ...]
    retrieved_evidences: tuple[Evidence, ...]
    recall_at_k: dict[int, float]


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationReport:
    retriever: str
    case_count: int
    cutoffs: tuple[int, ...]
    evidence_recall_at_k: dict[int, float]
    cases: tuple[RetrievalCaseResult, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "retriever": self.retriever,
            "case_count": self.case_count,
            "cutoffs": list(self.cutoffs),
            "metrics": {
                f"evidence_recall_at_{cutoff}": score
                for cutoff, score in self.evidence_recall_at_k.items()
            },
            "cases": [
                {
                    "id": case.id,
                    "query": case.query,
                    "relevant": [
                        {
                            "source_name": reference.source_name,
                            "locator": reference.locator,
                            "chunk_id": chunk_id,
                        }
                        for reference, chunk_id in zip(
                            case.relevant_references,
                            case.relevant_chunk_ids,
                            strict=True,
                        )
                    ],
                    "retrieved": [
                        {
                            "source_name": evidence.source_name,
                            "locator": evidence.locator,
                            "chunk_id": evidence.chunk_id,
                            "score": evidence.retrieval_score,
                        }
                        for evidence in case.retrieved_evidences
                    ],
                    "recall_at_k": {
                        str(cutoff): score
                        for cutoff, score in case.recall_at_k.items()
                    },
                }
                for case in self.cases
            ],
        }


def load_retrieval_cases(path: Path) -> tuple[RetrievalEvaluationCase, ...]:
    raw_cases = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        RetrievalEvaluationCase(
            id=item["id"],
            query=item["query"],
            relevant=tuple(
                ChunkReference(
                    source_name=reference["source_name"],
                    locator=reference["locator"],
                )
                for reference in item["relevant"]
            ),
            tags=tuple(item.get("tags", ())),
        )
        for item in raw_cases
    )


def evaluate_retriever(
    retriever: Retriever,
    repository: DocumentRepository,
    cases: tuple[RetrievalEvaluationCase, ...],
    cutoffs: tuple[int, ...] = (1, 3, 5),
) -> RetrievalEvaluationReport:
    normalized_cutoffs = tuple(sorted(set(cutoffs)))
    if not cases:
        raise ValueError("至少需要一个评测案例")
    if not normalized_cutoffs or normalized_cutoffs[0] < 1:
        raise ValueError("cutoffs 必须是正整数")

    chunk_ids_by_reference = _latest_chunk_ids(repository)
    results: list[RetrievalCaseResult] = []
    for case in cases:
        relevant_chunk_ids = tuple(
            _resolve_reference(reference, chunk_ids_by_reference)
            for reference in case.relevant
        )
        evidences = retriever.retrieve(
            case.query, limit=normalized_cutoffs[-1], max_hops=_EVALUATION_MAX_HOPS
        )
        retrieved_chunk_ids = tuple(evidence.chunk_id for evidence in evidences)
        recall_at_k = {
            cutoff: _recall(relevant_chunk_ids, retrieved_chunk_ids[:cutoff])
            for cutoff in normalized_cutoffs
        }
        results.append(
            RetrievalCaseResult(
                id=case.id,
                query=case.query,
                relevant_chunk_ids=relevant_chunk_ids,
                retrieved_chunk_ids=retrieved_chunk_ids,
                relevant_references=case.relevant,
                retrieved_evidences=evidences,
                recall_at_k=recall_at_k,
            )
        )

    return RetrievalEvaluationReport(
        retriever=retriever.name,
        case_count=len(results),
        cutoffs=normalized_cutoffs,
        evidence_recall_at_k={
            cutoff: round(fmean(result.recall_at_k[cutoff] for result in results), 6)
            for cutoff in normalized_cutoffs
        },
        cases=tuple(results),
    )


def _latest_chunk_ids(
    repository: DocumentRepository,
) -> dict[tuple[str, str], list[str]]:
    chunk_ids: dict[tuple[str, str], list[str]] = {}
    for document in repository.list_documents():
        versions = repository.list_versions(document.id)
        if not versions:
            continue
        for chunk in repository.list_chunks(versions[-1].id):
            chunk_ids.setdefault((document.source_name, chunk.locator), []).append(chunk.id)
    return chunk_ids


def _resolve_reference(
    reference: ChunkReference,
    chunk_ids_by_reference: dict[tuple[str, str], list[str]],
) -> str:
    matches = chunk_ids_by_reference.get((reference.source_name, reference.locator), [])
    if len(matches) != 1:
        raise ValueError(
            f"相关 Chunk 必须唯一匹配：{reference.source_name} / {reference.locator}"
        )
    return matches[0]


def _recall(relevant: tuple[str, ...], retrieved: tuple[str, ...]) -> float:
    return round(len(set(relevant).intersection(retrieved)) / len(set(relevant)), 6)
