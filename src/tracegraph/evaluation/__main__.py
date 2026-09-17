import argparse
import json
from pathlib import Path

from tracegraph.evaluation.retrieval import evaluate_retriever, load_retrieval_cases
from tracegraph.domains.medical.importer import MedicalRecordImporter
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.retrieval import GraphRetriever, HybridRetriever, KeywordRetriever
from tracegraph.storage.graph import InMemoryGraphRepository
from tracegraph.storage.memory import InMemoryDocumentRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 TraceGraph 文本检索 baseline")
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--corpus", type=Path)
    sources.add_argument("--medical-records", type=Path)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--retriever", choices=("keyword", "graph", "hybrid"), default="keyword"
    )
    args = parser.parse_args()

    repository = InMemoryDocumentRepository()
    graph = InMemoryGraphRepository()
    if args.corpus:
        ingestion = TextIngestionService(repository)
        corpus_files = sorted((*args.corpus.glob("*.md"), *args.corpus.glob("*.txt")))
        if not corpus_files:
            parser.error("corpus 目录中没有 TXT 或 Markdown 文档")
        for path in corpus_files:
            ingestion.ingest_file(path)
    else:
        MedicalRecordImporter(repository, graph).import_jsonl(args.medical_records)

    cases = load_retrieval_cases(args.cases)
    keyword = KeywordRetriever(repository)
    graph_retriever = GraphRetriever(repository, graph)
    retriever = {
        "keyword": keyword,
        "graph": graph_retriever,
        "hybrid": HybridRetriever((keyword, graph_retriever)),
    }[args.retriever]
    report = evaluate_retriever(retriever, repository, cases)
    serialized = json.dumps(report.as_dict(), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{serialized}\n", encoding="utf-8")
    else:
        print(serialized)


if __name__ == "__main__":
    main()
