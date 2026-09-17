import hashlib
import re

from tracegraph.core.contracts import (
    DEFAULT_MAX_HOPS,
    Chunk,
    Document,
    DocumentVersion,
    Evidence,
)
from tracegraph.core.ports import DocumentRepository


_LATIN_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_CJK_BLOCK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


class KeywordRetriever:
    """扫描最新文档版本的轻量关键词检索基线。"""

    name = "keyword"

    def __init__(self, repository: DocumentRepository) -> None:
        self.repository = repository

    def retrieve(
        self, query: str, limit: int = 5, max_hops: int = DEFAULT_MAX_HOPS
    ) -> tuple[Evidence, ...]:
        # 关键词检索没有图结构，接受 max_hops 仅为满足 Retriever 端口。
        del max_hops
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query 不能为空")
        if limit < 1:
            raise ValueError("limit 必须大于 0")

        query_terms = _terms(normalized_query)
        matches: list[tuple[float, Document, DocumentVersion, Chunk]] = []
        for document in self.repository.list_documents():
            versions = self.repository.list_versions(document.id)
            if not versions:
                continue
            version = versions[-1]
            for chunk in self.repository.list_chunks(version.id):
                score = _score(normalized_query, query_terms, chunk.content)
                if score > 0:
                    matches.append((score, document, version, chunk))

        matches.sort(key=lambda match: (-match[0], match[3].id))
        return tuple(
            _to_evidence(document, version, chunk, score)
            for score, document, version, chunk in matches[:limit]
        )


def _terms(text: str) -> frozenset[str]:
    normalized = text.casefold()
    terms = set(_LATIN_WORD_PATTERN.findall(normalized))
    for block in _CJK_BLOCK_PATTERN.findall(normalized):
        if len(block) == 1:
            terms.add(block)
        else:
            terms.update(block[index : index + 2] for index in range(len(block) - 1))
    return frozenset(terms)


def _score(query: str, query_terms: frozenset[str], content: str) -> float:
    if not query_terms:
        return 0.0
    overlap = query_terms.intersection(_terms(content))
    if not overlap:
        return 0.0
    score = len(overlap) / len(query_terms)
    if query.casefold() in content.casefold():
        score += 0.25
    return round(min(score, 1.0), 6)


def _to_evidence(
    document: Document,
    version: DocumentVersion,
    chunk: Chunk,
    score: float,
) -> Evidence:
    raw_id = f"keyword\x1f{chunk.id}".encode("utf-8")
    return Evidence(
        id=f"ev-{hashlib.sha256(raw_id).hexdigest()[:20]}",
        content=chunk.content,
        document_id=document.id,
        document_version=version.id,
        source_name=document.source_name,
        locator=chunk.locator,
        chunk_id=chunk.id,
        retrieval_method="keyword",
        retrieval_score=score,
    )
