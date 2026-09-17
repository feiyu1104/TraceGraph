from datetime import UTC, datetime
import uuid

from tracegraph.core.contracts import (
    AnswerStatus,
    EvaluationCase,
    Feedback,
    FeedbackKind,
)
from tracegraph.core.ports import FeedbackRepository


class FeedbackService:
    def __init__(self, repository: FeedbackRepository) -> None:
        self.repository = repository

    def submit(
        self,
        question: str,
        answer_status: AnswerStatus,
        kind: FeedbackKind,
        evidence_ids: tuple[str, ...] = (),
        comment: str | None = None,
    ) -> Feedback:
        normalized_question = " ".join(question.strip().split())
        if not normalized_question:
            raise ValueError("question 不能为空")
        feedback = Feedback(
            id=f"fb-{uuid.uuid4().hex}",
            question=normalized_question,
            answer_status=answer_status,
            kind=kind,
            evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            comment=comment.strip() if comment and comment.strip() else None,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.repository.save_feedback(feedback)
        return feedback

    def promote(
        self, feedback_id: str, expected_status: AnswerStatus
    ) -> EvaluationCase:
        feedback = self.repository.get_feedback(feedback_id)
        if feedback is None:
            raise KeyError(feedback_id)
        return EvaluationCase(
            id=f"eval-{feedback.id.removeprefix('fb-')}",
            question=feedback.question,
            expected_status=expected_status,
            required_evidence_ids=feedback.evidence_ids,
            tags=("feedback", feedback.kind.value),
            source="user_feedback",
        )
