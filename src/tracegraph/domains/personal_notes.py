from dataclasses import dataclass

from tracegraph.core.contracts import AnswerStatus, ExtractionVocabulary


@dataclass(frozen=True, slots=True)
class PersonalNotesDomainAdapter:
    """个人笔记、项目记录与日常资料。

    与通用适配器一样不做范围判断和急症规则。回答只能来自笔记里写明的
    内容，缺证据时如实说没找到，不去补全没写过的事实。
    """

    name: str = "personal-notes"
    version: str = "0.1.0"

    def entity_types(self) -> tuple[str, ...]:
        return ("Person", "Project", "Task", "Event", "Topic", "Place", "Resource")

    def relation_types(self) -> tuple[str, ...]:
        return (
            "RELATED_TO",
            "PART_OF",
            "ASSIGNED_TO",
            "DEPENDS_ON",
            "MENTIONS",
            "OCCURRED_AT",
        )

    def extraction_vocabulary(self) -> ExtractionVocabulary | None:
        """笔记的章节写法因人而异，没有可依赖的约定，因此不猜类型。"""
        return None

    def normalize_question(self, question: str) -> str:
        return " ".join(question.strip().split())

    def preflight_status(self, question: str) -> AnswerStatus | None:
        """个人笔记没有需要提前拦截的问题。"""
        return None

    def status_message(self, status: AnswerStatus, question: str) -> str:
        if status is AnswerStatus.INSUFFICIENT_EVIDENCE:
            return "当前笔记中没有找到足够证据支持回答。"
        return "系统无法完成本次回答。"
