from pathlib import Path
import sqlite3
from threading import RLock

from tracegraph.core.contracts import AnswerStatus, Feedback, FeedbackKind


class InMemoryFeedbackRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self._feedback: dict[str, Feedback] = {}

    def save_feedback(self, feedback: Feedback) -> None:
        with self._lock:
            self._feedback[feedback.id] = feedback

    def get_feedback(self, feedback_id: str) -> Feedback | None:
        with self._lock:
            return self._feedback.get(feedback_id)

    def list_feedback(self) -> tuple[Feedback, ...]:
        with self._lock:
            return tuple(sorted(self._feedback.values(), key=lambda item: item.created_at))


class SQLiteFeedbackRepository:
    def __init__(self, database: str | Path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    question TEXT NOT NULL,
                    answer_status TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    evidence_ids TEXT NOT NULL,
                    comment TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save_feedback(self, feedback: Feedback) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO feedback (
                    id, question, answer_status, kind, evidence_ids, comment, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback.id,
                    feedback.question,
                    feedback.answer_status.value,
                    feedback.kind.value,
                    "\x1f".join(feedback.evidence_ids),
                    feedback.comment,
                    feedback.created_at,
                ),
            )

    def get_feedback(self, feedback_id: str) -> Feedback | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM feedback WHERE id = ?", (feedback_id,)
            ).fetchone()
        return _from_row(row) if row else None

    def list_feedback(self) -> tuple[Feedback, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM feedback ORDER BY created_at"
            ).fetchall()
        return tuple(_from_row(row) for row in rows)


def _from_row(row: sqlite3.Row) -> Feedback:
    return Feedback(
        id=row["id"],
        question=row["question"],
        answer_status=AnswerStatus(row["answer_status"]),
        kind=FeedbackKind(row["kind"]),
        evidence_ids=tuple(filter(None, row["evidence_ids"].split("\x1f"))),
        comment=row["comment"],
        created_at=row["created_at"],
    )
