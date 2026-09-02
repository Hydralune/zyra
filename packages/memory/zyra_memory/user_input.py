from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


_QUESTION_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class UserInputContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_user_input_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def normalize_user_input_questions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise UserInputContractError(
            "user_input_questions_invalid",
            "questions must be an array",
        )
    if not 1 <= len(value) <= 3:
        raise UserInputContractError(
            "user_input_questions_limit",
            "request_user_input requires one to three questions",
        )
    result: list[dict[str, Any]] = []
    identities: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise UserInputContractError(
                "user_input_question_invalid",
                f"question {index + 1} must be an object",
            )
        question_id = str(raw.get("id") or "").strip()
        header = str(raw.get("header") or "").strip()
        question = str(raw.get("question") or "").strip()
        options = raw.get("options")
        if not _QUESTION_ID.fullmatch(question_id) or question_id in identities:
            raise UserInputContractError(
                "user_input_question_id_invalid",
                "question ids must be unique snake_case identifiers",
            )
        if not header or len(header) > 12 or any(ord(char) < 32 for char in header):
            raise UserInputContractError(
                "user_input_question_header_invalid",
                "question headers must contain at most 12 visible characters",
            )
        if not question or len(question) > 4096 or any(char in "\x00\r" for char in question):
            raise UserInputContractError(
                "user_input_question_text_invalid",
                "question text is empty or exceeds the safe limit",
            )
        if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
            raise UserInputContractError(
                "user_input_options_invalid",
                "question options must be an array",
            )
        if not 2 <= len(options) <= 3:
            raise UserInputContractError(
                "user_input_options_limit",
                "each question requires two or three options",
            )
        normalized_options: list[dict[str, str]] = []
        labels: set[str] = set()
        for option_index, option in enumerate(options):
            if not isinstance(option, Mapping):
                raise UserInputContractError(
                    "user_input_option_invalid",
                    f"option {option_index + 1} must be an object",
                )
            label = str(option.get("label") or "").strip()
            description = str(option.get("description") or "").strip()
            if (
                not label
                or len(label) > 80
                or label in labels
                or any(char in "\x00\r\n" for char in label)
            ):
                raise UserInputContractError(
                    "user_input_option_label_invalid",
                    "option labels must be unique, non-empty single-line text",
                )
            if not description or len(description) > 1024 or "\x00" in description:
                raise UserInputContractError(
                    "user_input_option_description_invalid",
                    "option descriptions are empty or exceed the safe limit",
                )
            labels.add(label)
            normalized_options.append(
                {"label": label, "description": description}
            )
        identities.add(question_id)
        result.append(
            {
                "id": question_id,
                "header": header,
                "question": question,
                "options": normalized_options,
            }
        )
    return result


def normalize_user_input_answers(
    questions: Sequence[Mapping[str, Any]],
    value: Any,
) -> dict[str, dict[str, list[str]]]:
    if not isinstance(value, Mapping):
        raise UserInputContractError(
            "user_input_answers_invalid",
            "answers must be an object keyed by question id",
        )
    expected = {str(question.get("id") or ""): question for question in questions}
    if set(map(str, value.keys())) != set(expected):
        raise UserInputContractError(
            "user_input_answers_incomplete",
            "answers must contain exactly one response for every question",
        )
    normalized: dict[str, dict[str, list[str]]] = {}
    for question_id, question in expected.items():
        raw = value.get(question_id)
        if isinstance(raw, Mapping):
            raw = raw.get("answers")
        answers = (
            list(raw)
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
            else [raw]
        )
        selected = [str(item or "").strip() for item in answers]
        if len(selected) != 1 or not selected[0] or len(selected[0]) > 4096:
            raise UserInputContractError(
                "user_input_answer_invalid",
                "each question requires one non-empty answer",
            )
        if any(char in "\x00\r" for char in selected[0]):
            raise UserInputContractError(
                "user_input_answer_invalid",
                "answers contain unsupported control characters",
            )
        normalized[question_id] = {"answers": selected}
    return normalized


__all__ = [
    "UserInputContractError",
    "canonical_user_input_digest",
    "normalize_user_input_answers",
    "normalize_user_input_questions",
]
