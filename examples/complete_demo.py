import argparse
from pathlib import Path

from tracegraph.domains.medical import MedicalDomainAdapter, MedicalRecordImporter
from tracegraph.generation import AnswerService, describe_path
from tracegraph.retrieval import GraphRetriever, HybridRetriever, KeywordRetriever
from tracegraph.storage import InMemoryDocumentRepository, InMemoryGraphRepository


def main() -> None:
    default_source = Path(__file__).resolve().parents[2] / "DUTMed" / "data" / "症状.json"
    parser = argparse.ArgumentParser(description="使用真实 DUTMed 数据运行完整链路")
    parser.add_argument("--source", type=Path, default=default_source)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--query", default="苯中毒需要做什么检查")
    parser.add_argument("--max-hops", type=int, default=2)
    args = parser.parse_args()

    if not args.source.is_file():
        parser.error(f"找不到 DUTMed 数据文件：{args.source}")

    documents = InMemoryDocumentRepository()
    graph = InMemoryGraphRepository()
    result = MedicalRecordImporter(documents, graph).import_jsonl(args.source, args.limit)
    retriever = HybridRetriever(
        (KeywordRetriever(documents), GraphRetriever(documents, graph))
    )
    service = AnswerService(retriever, MedicalDomainAdapter(), graph_repository=graph)
    answer = service.answer(args.query, max_hops=args.max_hops)

    print(f"已读取 DUTMed 真实记录：{result.records} 条，跳过：{result.skipped} 条")
    print(f"回答状态：{answer.status.value}（最多 {args.max_hops} 跳）")
    print(answer.text)
    for evidence in answer.evidences:
        path = describe_path(evidence.graph_path) if evidence.graph_path else "（关键词证据）"
        print(f"- {evidence.source_name} · {evidence.locator} · {path}")


if __name__ == "__main__":
    main()
