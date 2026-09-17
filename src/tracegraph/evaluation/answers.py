from dataclasses import dataclass
import argparse
import json
from pathlib import Path
from statistics import fmean

from tracegraph.core.contracts import AnswerStatus
from tracegraph.domains.medical.adapter import MedicalDomainAdapter
from tracegraph.domains.medical.importer import MedicalRecordImporter
from tracegraph.generation.service import AnswerService
from tracegraph.retrieval import GraphRetriever, HybridRetriever, KeywordRetriever
from tracegraph.storage.graph import InMemoryGraphRepository
from tracegraph.storage.memory import InMemoryDocumentRepository


@dataclass(frozen=True, slots=True)
class AnswerEvaluationCase:
    id: str
    question: str
    expected_status: AnswerStatus


def evaluate_answers(
    service: AnswerService, cases: tuple[AnswerEvaluationCase, ...]
) -> dict[str, object]:
    results = []
    for case in cases:
        # 与检索评测同口径：固定单跳，多跳效果另行测量。
        answer = service.answer(case.question, max_hops=1)
        known_evidence_ids = {evidence.id for evidence in answer.evidences}
        cited_ids = {
            evidence_id
            for claim in answer.claims
            for evidence_id in claim.evidence_ids
        }
        citation_integrity = cited_ids.issubset(known_evidence_ids)
        citation_completeness = all(claim.evidence_ids for claim in answer.claims)
        results.append(
            {
                "id": case.id,
                "question": case.question,
                "expected_status": case.expected_status.value,
                "actual_status": answer.status.value,
                "status_correct": answer.status is case.expected_status,
                "citation_integrity": citation_integrity,
                "citation_completeness": citation_completeness,
                "evidence_count": len(answer.evidences),
            }
        )
    return {
        "case_count": len(results),
        "metrics": {
            "status_accuracy": round(fmean(item["status_correct"] for item in results), 6),
            "citation_integrity": round(
                fmean(item["citation_integrity"] for item in results), 6
            ),
            "citation_completeness": round(
                fmean(item["citation_completeness"] for item in results), 6
            ),
        },
        "cases": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 TraceGraph 回答行为评测")
    parser.add_argument("--medical-records", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    documents = InMemoryDocumentRepository()
    graph = InMemoryGraphRepository()
    MedicalRecordImporter(documents, graph).import_jsonl(args.medical_records)
    retriever = HybridRetriever(
        (KeywordRetriever(documents), GraphRetriever(documents, graph))
    )
    service = AnswerService(retriever, MedicalDomainAdapter(), graph_repository=graph)
    raw_cases = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = tuple(
        AnswerEvaluationCase(
            id=item["id"],
            question=item["question"],
            expected_status=AnswerStatus(item["expected_status"]),
        )
        for item in raw_cases
    )
    report = evaluate_answers(service, cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        f"{json.dumps(report, ensure_ascii=False, indent=2)}\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
