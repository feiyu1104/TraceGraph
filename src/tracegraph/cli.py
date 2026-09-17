import argparse
import os
from pathlib import Path

from tracegraph.config import load_local_env
from tracegraph.core.contracts import GraphStatistics
from tracegraph.domains.medical.importer import MedicalRecordImporter
from tracegraph.graph_backend import create_graph_repository
from tracegraph.storage.consistency import check_sqlite_consistency
from tracegraph.storage.sqlite import SQLiteDocumentRepository


DEFAULT_DATABASE = Path("data/local/tracegraph.db")


def main() -> None:
    load_local_env()
    parser = argparse.ArgumentParser(description="TraceGraph 管理命令")
    commands = parser.add_subparsers(dest="command", required=True)

    medical = commands.add_parser("import-medical", help="导入 DUTMed JSONL 数据")
    medical.add_argument("--source", type=Path, required=True)
    medical.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    medical.add_argument("--limit", type=int)

    stats = commands.add_parser("graph-stats", help="打印图后端规模概览与类型分布")
    stats.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    consistency = commands.add_parser(
        "check-consistency", help="检查图侧证据引用与文档侧 chunk 是否一致"
    )
    consistency.add_argument("--database", type=Path, default=DEFAULT_DATABASE)

    resync = commands.add_parser(
        "resync-graph", help="按当前图后端重跑导入，并对比导入前后的图统计"
    )
    resync.add_argument("--source", type=Path, required=True)
    resync.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    resync.add_argument("--limit", type=int)

    args = parser.parse_args()

    if args.command == "import-medical":
        _import_medical(parser, args.database, args.source, args.limit)
    elif args.command == "graph-stats":
        _graph_stats(parser, args.database)
    elif args.command == "check-consistency":
        _check_consistency(parser, args.database)
    elif args.command == "resync-graph":
        _resync_graph(parser, args.database, args.source, args.limit)


def _import_medical(
    parser: argparse.ArgumentParser, database: Path, source: Path, limit: int | None
) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    documents = SQLiteDocumentRepository(database)
    graph = _open_graph(parser, database)
    try:
        result = MedicalRecordImporter(documents, graph).import_jsonl(source, limit)
    finally:
        documents.close()
        graph.close()
    print(
        f"导入完成：{result.records} 条记录，{result.documents} 份文档，"
        f"{result.entities} 个实体，{result.relations} 条关系，"
        f"跳过 {result.skipped} 条无有效正文记录。"
    )


def _graph_stats(parser: argparse.ArgumentParser, database: Path) -> None:
    graph = _open_graph(parser, database)
    try:
        _print_statistics(graph.statistics(), f"图后端 {graph.name}")
    finally:
        graph.close()


def _check_consistency(parser: argparse.ArgumentParser, database: Path) -> None:
    if not database.exists():
        parser.error(f"SQLite 库不存在：{database}")
    report = check_sqlite_consistency(database)
    _print_statistics(report.statistics, f"SQLite 库 {database}")

    dangling = report.dangling_evidence
    print(f"悬空证据引用：{len(dangling)} 条")
    for relation_id, chunk_id in dangling[:5]:
        print(f"  关系 {relation_id} 引用了不存在的 chunk {chunk_id}")
    without_evidence = report.relations_without_evidence
    print(f"无证据关系：{len(without_evidence)} 条")
    for relation_id in without_evidence[:5]:
        print(f"  关系 {relation_id} 没有任何证据 chunk")

    if os.getenv("TRACEGRAPH_GRAPH_BACKEND", "sqlite").casefold() == "neo4j":
        graph = _open_graph(parser, database)
        try:
            _compare_statistics(report.statistics, graph.statistics(), graph.name)
        finally:
            graph.close()

    if not report.is_consistent:
        raise SystemExit(1)


def _compare_statistics(
    local: GraphStatistics, remote: GraphStatistics, backend: str
) -> None:
    if (local.entities, local.relations) == (remote.entities, remote.relations):
        print(f"与 {backend} 的计数一致：{local.entities} 个实体，{local.relations} 条关系")
        return
    print(
        f"与 {backend} 的计数**不一致**："
        f"SQLite {local.entities} 个实体 / {local.relations} 条关系，"
        f"{backend} {remote.entities} 个实体 / {remote.relations} 条关系"
    )
    raise SystemExit(1)


def _resync_graph(
    parser: argparse.ArgumentParser, database: Path, source: Path, limit: int | None
) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    documents = SQLiteDocumentRepository(database)
    graph = _open_graph(parser, database)
    try:
        before = graph.statistics()
        result = MedicalRecordImporter(documents, graph).import_jsonl(source, limit)
        after = graph.statistics()
    finally:
        documents.close()
        graph.close()
    print(
        f"重新同步完成：{result.records} 条记录，{result.documents} 份文档，"
        f"{result.entities} 个实体，{result.relations} 条关系，"
        f"跳过 {result.skipped} 条（正文未变，靠内容哈希判定）。"
    )
    _print_statistics(before, "导入前")
    _print_statistics(after, "导入后")


def _open_graph(parser: argparse.ArgumentParser, database: Path):
    try:
        graph, _ = create_graph_repository(database)
    except Exception as error:
        parser.error(str(error))
    return graph


def _print_statistics(statistics: GraphStatistics, title: str) -> None:
    print(
        f"{title}：{statistics.entities} 个实体，{statistics.relations} 条关系，"
        f"{statistics.orphan_entities} 个孤立实体"
    )
    for label, counts in (
        ("实体类型", statistics.entity_types),
        ("关系类型", statistics.relation_types),
    ):
        if counts:
            print(f"  {label}：" + "，".join(f"{name} {total}" for name, total in counts))


if __name__ == "__main__":
    main()
