from dataclasses import dataclass

from tracegraph.core.contracts import AnswerStatus, ExtractionVocabulary


@dataclass(frozen=True, slots=True)
class GeneralDomainAdapter:
    """普通资料、说明文档与通用知识。

    刻意不做范围判断，也没有急症规则：通用语料里的「胸痛」「股票」都是正常
    话题，套用医疗规则只会把能回答的问题挡在检索之外。
    """

    name: str = "general"
    version: str = "0.1.0"

    def entity_types(self) -> tuple[str, ...]:
        return (
            "Concept",
            "Person",
            "Organization",
            "Place",
            "Event",
            "Document",
            "Topic",
        )

    def relation_types(self) -> tuple[str, ...]:
        return (
            "RELATED_TO",
            "MENTIONS",
            "PART_OF",
            "CREATED_BY",
            "LOCATED_IN",
            "OCCURRED_AT",
        )

    def extraction_vocabulary(self) -> ExtractionVocabulary | None:
        """通用资料没有固定的章节约定，不猜类型：摘录式抽取如实产出 0 条候选。"""
        return None

    def normalize_question(self, question: str) -> str:
        return " ".join(question.strip().split())

    def preflight_status(self, question: str) -> AnswerStatus | None:
        """通用场景没有需要提前拦截的问题。"""
        return None

    def status_message(self, status: AnswerStatus, question: str) -> str:
        if status is AnswerStatus.INSUFFICIENT_EVIDENCE:
            return "当前知识库没有找到足够证据支持回答。"
        return "系统无法完成本次回答。"
