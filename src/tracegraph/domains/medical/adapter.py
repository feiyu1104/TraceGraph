from dataclasses import dataclass

from tracegraph.core.contracts import AnswerStatus


_EMERGENCY_SIGNALS = {
    "胸痛": ("胸痛", "胸口剧痛", "胸部压榨感"),
    "呼吸困难": ("呼吸困难", "喘不上气", "无法呼吸", "窒息"),
    "意识障碍": ("意识不清", "失去意识", "昏迷", "叫不醒"),
    "抽搐": ("抽搐", "抽搐不停"),
    "严重出血": ("大出血", "大量出血", "血流不止", "严重出血", "出血止不住"),
    "卒中征象": ("口角歪斜", "一侧无力", "言语不清", "突然偏瘫"),
    "严重过敏": ("喉咙肿胀", "过敏性休克", "全身风团呼吸困难"),
    "自伤风险": ("想自杀", "想轻生", "想伤害自己"),
}

_OUT_OF_SCOPE_SIGNALS = ("天气", "股票", "编程", "写代码", "旅游", "电影票")


@dataclass(frozen=True, slots=True)
class MedicalDomainAdapter:
    name: str = "medical"
    version: str = "0.1.0"

    def entity_types(self) -> tuple[str, ...]:
        return (
            "Disease",
            "Category",
            "Symptom",
            "Department",
            "Treatment",
            "Check",
            "Drug",
            "Food",
            "Recipe",
        )

    def relation_types(self) -> tuple[str, ...]:
        return (
            "BELONGS_TO",
            "HAS_SYMPTOM",
            "ACCOMPANIES",
            "TREATED_BY",
            "USES_TREATMENT",
            "REQUIRES_CHECK",
            "RECOMMENDS_DRUG",
            "COMMONLY_USES_DRUG",
            "SHOULD_EAT",
            "SHOULD_NOT_EAT",
            "RECOMMENDS_RECIPE",
        )

    def normalize_question(self, question: str) -> str:
        return " ".join(question.strip().split())

    def preflight_status(self, question: str) -> AnswerStatus | None:
        normalized = "".join(question.casefold().split())
        if any(
            "".join(phrase.casefold().split()) in normalized
            for phrases in _EMERGENCY_SIGNALS.values()
            for phrase in phrases
        ):
            return AnswerStatus.EMERGENCY_ESCALATION
        if any(signal in normalized for signal in _OUT_OF_SCOPE_SIGNALS):
            return AnswerStatus.OUT_OF_SCOPE
        return None

    def status_message(self, status: AnswerStatus, question: str) -> str:
        if status is AnswerStatus.EMERGENCY_ESCALATION:
            matched = self.emergency_signals(question)
            signals = "、".join(matched) or "可能的急症征象"
            return (
                f"你描述的情况包含{signals}，可能需要紧急处理。"
                "请立即拨打 120 或前往最近的急诊，不要等待系统回复，也不要独自驾车。"
                "如果患者失去意识或无法正常呼吸，请让身边的人立即联系急救人员。"
                "本提示不构成诊断。"
            )
        if status is AnswerStatus.OUT_OF_SCOPE:
            return "这个问题超出当前医疗知识库范围。"
        if status is AnswerStatus.INSUFFICIENT_EVIDENCE:
            return "当前知识库没有找到足够证据支持回答。"
        return "系统无法完成本次回答。"

    def emergency_signals(self, question: str) -> tuple[str, ...]:
        normalized = "".join(question.casefold().split())
        return tuple(
            name
            for name, phrases in _EMERGENCY_SIGNALS.items()
            if any("".join(phrase.casefold().split()) in normalized for phrase in phrases)
        )
