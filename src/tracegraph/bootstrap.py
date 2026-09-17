import os
import sys
from pathlib import Path

from fastapi import FastAPI

from tracegraph.api import create_app
from tracegraph.config import load_local_env
from tracegraph.domains.medical.adapter import MedicalDomainAdapter
from tracegraph.feedback.storage import SQLiteFeedbackRepository
from tracegraph.generation.config import FALLBACK_EXTRACTIVE, load_generation_fallback
from tracegraph.generation.models import ModelRegistry, load_model_registry
from tracegraph.generation.providers import (
    AnswerGenerator,
    ExtractiveAnswerGenerator,
)
from tracegraph.graph_backend import create_graph_repository
from tracegraph.retrieval.graph import GraphRetriever
from tracegraph.retrieval.hybrid import HybridRetriever
from tracegraph.retrieval.keyword import KeywordRetriever
from tracegraph.storage.sqlite import SQLiteDocumentRepository


load_local_env()


def create_local_app(database: str | Path = "data/local/tracegraph.db") -> FastAPI:
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    registry = load_model_registry()
    fallback_name = load_generation_fallback()
    documents = SQLiteDocumentRepository(database_path)
    graph, selection = create_graph_repository(database_path)
    if selection.degraded:
        print(
            f"警告：请求的图后端是 {selection.requested}，已降级为 {selection.active}。"
            f"原因：{selection.detail}",
            file=sys.stderr,
        )
    feedback = SQLiteFeedbackRepository(database_path)
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
        model_registry=registry,
        graph_status=selection.to_dict(),
        generation_status=registry.system_status(fallback_name),
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
