from __future__ import annotations

from typing import Any

from agent_core.models import Question
from configs.runtime import RuntimePolicy


def clarification_directive(preference: str) -> str:
    if preference == "minimal":
        return (
            "Only ask questions whose answers materially block a sound presentation. "
            "Return an empty questions array as soon as the task is sufficiently specified."
        )
    return (
        "Actively seek high-value missing decisions about audience, occasion, scope, "
        "narrative emphasis, visual direction, length, language, and source constraints. "
        "Use the available per-round budget when useful, but return an empty questions "
        "array as soon as no additional answer would materially improve the result."
    )


def clarification_prompt_history(
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    context: list[dict[str, Any]] = []
    for entry in manifest.get("clarification_history", []):
        answers = entry.get("answers", {})
        questions = entry.get("questions", [])
        context.append({
            "round": entry.get("round"),
            "question_card_id": entry.get("question_card_id"),
            "question_answers": [
                {
                    "question_id": question.get("question_id"),
                    "field": question.get("field"),
                    "question": question.get("prompt"),
                    "answer": answers.get(question.get("question_id")),
                }
                for question in questions
            ],
        })
    return context


def select_new_questions(
    payload: dict[str, Any],
    manifest: dict[str, Any],
    per_round_limit: int,
) -> list[Question]:
    raw_questions = payload.get("questions", [])
    if not isinstance(raw_questions, list):
        raise ValueError("model returned invalid clarification questions")
    needs_clarification = payload.get("needs_clarification", bool(raw_questions))
    if not isinstance(needs_clarification, bool):
        raise ValueError("model returned invalid clarification decision")
    if not needs_clarification:
        return []

    history = manifest.get("clarification_history", [])
    seen_fields = {
        question.get("field")
        for entry in history
        for question in entry.get("questions", [])
    }
    seen_question_ids = {
        question.get("question_id")
        for entry in history
        for question in entry.get("questions", [])
    }
    known_fields = {
        field
        for field, value in manifest["task_card"].items()
        if value not in (None, "", (), [])
    }
    questions: list[Question] = []
    for item in raw_questions:
        question = Question.model_validate(item)
        if (
            question.field in seen_fields
            or question.field in known_fields
            or question.question_id in seen_question_ids
        ):
            continue
        questions.append(question)
        seen_fields.add(question.field)
        seen_question_ids.add(question.question_id)
        if len(questions) >= per_round_limit:
            break
    return questions


def clarification_history_with_answers(
    manifest: dict[str, Any],
    card: dict[str, Any],
    answers: dict[str, str],
) -> list[dict[str, Any]]:
    history = list(manifest.get("clarification_history", []))
    history.append({
        "round": card.get("round", len(history) + 1),
        "question_card_id": card["question_card_id"],
        "questions": card["questions"],
        "answers": dict(answers),
        "prompt_call_id": (card.get("provenance") or {}).get("prompt_call_id"),
    })
    return history


def should_continue_clarification(
    card: dict[str, Any],
    history: list[dict[str, Any]],
    policy: RuntimePolicy,
) -> bool:
    total_asked = sum(len(entry.get("questions", [])) for entry in history)
    return (
        policy.question_preference != "none"
        and card.get("round", 1) < policy.max_clarification_rounds
        and total_asked < policy.clarification_total_budget
        and policy.max_auto_questions > 0
    )
