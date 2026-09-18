import os
import sys
from pathlib import Path

from fastapi import FastAPI

from tracegraph.api import create_app
from tracegraph.config import load_local_env
from tracegraph.domains.medical.adapter import MedicalDomainAdapter
from tracegraph.domains.registry import build_default_adapter_registry
from tracegraph.feedback.storage import SQLiteFeedbackRepository
from tracegraph.generation.config import FALLBACK_EXTRACTIVE, load_generation_fallback
from tracegraph.generation.connections import (
    ConnectionStore,
    ModelConnectionService,
    load_connections_path,
)
from tracegraph.generation.models import ModelRegistry, load_model_registry
from tracegraph.generation.providers import (
    AnswerGenerator,
    ExtractiveAnswerGenerator,
)
from tracegraph.graph_backend import create_graph_repository
from tracegraph.retrieval.graph import GraphRetriever
from tracegraph.retrieval.hybrid import HybridRetriever
from tracegraph.retrieval.keyword import KeywordRetriever
from tracegraph.storage.candidates import SQLiteCandidateRepository
from tracegraph.storage.originals import (
    FileSystemOriginalStore,
    load_original_store_root,
)
from tracegraph.storage.sqlite import SQLiteDocumentRepository


load_local_env()


def create_local_app(database: str | Path = "data/local/tracegraph.db") -> FastAPI:
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    # 先按现有配置（配置文件 / 环境变量 / 离线摘录）装配基础清单，再把运行期
    # 保存的模型连接叠加上去。没有连接文件时这一层完全不存在，行为与改造前一致。
    registry = load_model_registry()
    model_connections = ModelConnectionService(
        base=registry, store=ConnectionStore(load_connections_path())
    )
    fallback_name = load_generation_fallback()
    documents = SQLiteDocumentRepository(database_path)
    originals = FileSystemOriginalStore(load_original_store_root())
    graph, selection = create_graph_repository(database_path)
    if selection.degraded:
        print(
            f"警告：请求的图后端是 {selection.requested}，已降级为 {selection.active}。"
            f"原因：{selection.detail}",
            file=sys.stderr,
        )
    feedback = SQLiteFeedbackRepository(database_path)
    # 与文档、反馈一样是同一个库文件上的独立仓储：候选表由它自己幂等迁移，
    # 不会改动既有表。
    candidates = SQLiteCandidateRepository(database_path)
    retriever = HybridRetriever(
        (KeywordRetriever(documents), GraphRetriever(documents, graph))
    )
    return create_app(
        repository=documents,
        retriever=retriever,
        domain=MedicalDomainAdapter(),
        feedback_repository=feedback,
        graph_repository=graph,
        answer_generator=registry.default_generator,
        fallback_answer_generator=_fallback_generator(fallback_name),
        # 交给应用的是可刷新的句柄：改完默认模型或增删连接之后，后续请求、
        # 抽取与 /models、/system 立刻看到新表，不需要重启。
        model_registry=model_connections.registry,
        model_connections=model_connections,
        graph_status=selection.to_dict(),
        # 只传降级策略名，模型状态由 /system 按当下生效的注册表现算。
        generation_fallback=fallback_name,
        original_store=originals,
        adapter_registry=build_default_adapter_registry(),
        candidate_repository=candidates,
    )


def _fallback_generator(fallback: str) -> AnswerGenerator | None:
    """降级生成器只在显式配置 TRACEGRAPH_LLM_FALLBACK=extractive 时存在。

    默认的 none 表示模型失败就让请求失败，不去悄悄换一份结果。
    """
    return (
        ExtractiveAnswerGenerator()
        if fallback == FALLBACK_EXTRACTIVE
        else None
    )


app = create_local_app(os.getenv("TRACEGRAPH_DATABASE", "data/local/tracegraph.db"))
