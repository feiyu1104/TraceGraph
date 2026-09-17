from dataclasses import FrozenInstanceError

import pytest

from tracegraph.core.contracts import (
    Answer,
    AnswerStatus,
    Claim,
    EvaluationCase,
    Evidence,
)


def test_evidence_is_immutable_and_traceable() -> None:
    evidence = Evidence(
        id="ev-1",
        content="高血压患者应定期监测血压。",
        document_id="doc-1",
        document_version="2024",
        source_name="guide.pdf",
        locator="page 18",
        chunk_id="chunk-34",
        retrieval_method="graph",
        retrieval_score=0.87,
    )

    with pytest.raises(FrozenInstanceError):
        evidence.content = "changed"


def test_answer_rejects_claim_with_unknown_evidence() -> None:
    with pytest.raises(ValueError, match="unknown evidence"):
        Answer(
            status=AnswerStatus.ANSWERED,
            text="应定期监测血压。",
            claims=(Claim(text="需要监测血压", evidence_ids=("ev-missing",)),),
            evidences=(),
        )


def test_answered_status_requires_text() -> None:
    with pytest.raises(ValueError, match="requires non-empty text"):
        Answer(status=AnswerStatus.ANSWERED, text=None)


def test_evaluation_case_can_define_expected_behavior() -> None:
    case = EvaluationCase(
        id="medical-001",
        question="高血压需要做什么检查？",
        expected_status=AnswerStatus.ANSWERED,
        expected_entities=("高血压",),
        expected_relations=("REQUIRES_CHECK",),
        required_evidence_ids=("ev-1",),
        tags=("medical", "graph"),
    )

    assert case.expected_status is AnswerStatus.ANSWERED
    assert "graph" in case.tags
