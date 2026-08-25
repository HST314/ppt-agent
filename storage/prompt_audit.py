from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any
from uuid import uuid4

from agent_core.models import utc_now
from storage.persistence import json_text


_SENSITIVE_KEY_PATTERN = r"""
    (?<![\w-])
    [\"']?(?:[A-Za-z0-9]+[-_])*(?:
        api[-_ ]?key
        | authorization
        | auth[-_ ]?token
        | access[-_ ]?token
        | refresh[-_ ]?token
        | id[-_ ]?token
        | client[-_ ]?secret
        | private[-_ ]?key
        | credentials?
        | secret
        | password
        | passwd
        | token
    )[\"']?
    \s*[:=]\s*
    """
_SENSITIVE_VALUE = re.compile(
    rf"(?P<prefix>{_SENSITIVE_KEY_PATTERN})"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|(?:(?:Bearer|Basic|Token)\s+)?[^\s,;}\]\r\n]+)",
    re.IGNORECASE | re.VERBOSE,
)
_AUTHORIZATION_SCHEME = re.compile(
    r"(?i)\b(Bearer|Basic)\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[A-Za-z0-9._~+/=-]+)"
)
_SENSITIVE_NORMALIZED_KEYS = {
    "apikey",
    "authorization",
    "authtoken",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "clientsecret",
    "privatekey",
    "credential",
    "credentials",
    "secret",
    "password",
    "passwd",
    "token",
}


def _redact_text(value: str) -> str:
    value = _SENSITIVE_VALUE.sub(
        lambda match: match.group("prefix") + "[REDACTED]",
        value,
    )
    return _AUTHORIZATION_SCHEME.sub(r"\1 [REDACTED]", value)


def _is_sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in _SENSITIVE_NORMALIZED_KEYS or any(
        normalized.endswith(suffix)
        for suffix in (
            "apikey", "authtoken", "accesstoken", "refreshtoken", "idtoken",
            "clientsecret", "privatekey", "credential", "credentials", "secret",
            "password", "passwd", "token",
        )
    )


def redact_for_audit(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [redact_for_audit(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = redact_for_audit(item)
        return result
    return value


class PromptAuditMixin:
    project_id: str

    def start_prompt_call(
        self,
        *,
        state: str,
        messages: list[dict[str, Any]],
        template_id: str,
        template_version: int,
        template_hash: str,
        model_config_hash: str,
        runtime_config_hash: str,
        skills_hash: str,
        parameters: dict[str, Any],
        parent_prompt_call_id: str | None = None,
    ) -> str:
        self._ensure_database()
        prompt_call_id = "prompt_" + uuid4().hex
        started_at = utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO prompt_calls(
                    prompt_call_id, parent_prompt_call_id, project_id, state, status,
                    messages_json, template_id, template_version, template_hash,
                    model_config_hash, runtime_config_hash, skills_hash, parameters_json,
                    tool_calls_json, started_at
                ) VALUES(?, ?, ?, ?, 'started', ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)
                """,
                (
                    prompt_call_id, parent_prompt_call_id, self.project_id, state,
                    json_text(redact_for_audit(messages)), template_id, template_version, template_hash,
                    model_config_hash, runtime_config_hash, skills_hash,
                    json_text(redact_for_audit(parameters)), started_at,
                ),
            )
            connection.execute(
                "INSERT INTO prompt_call_events(prompt_call_id, at, status, details_json) VALUES(?, ?, 'started', '{}')",
                (prompt_call_id, started_at),
            )
        return prompt_call_id

    def finish_prompt_call(
        self,
        prompt_call_id: str,
        *,
        status: str,
        traces: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        output_ref: str | None = None,
        output_hash: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"completed", "failed", "conflicted"}:
            raise ValueError("invalid prompt call status")
        completed_at = utc_now()
        tool_calls = [item for item in traces or [] if item.get("type") == "tool_call"]
        audited_messages = deepcopy(messages) if messages is not None else None
        if audited_messages:
            for message in reversed(audited_messages):
                content = message.get("content")
                if message.get("role") != "assistant" or not isinstance(content, str) or not content:
                    continue
                referenced_hash = output_hash or (
                    "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
                )
                message["content"] = f"[OUTPUT_REF {output_ref or 'uncommitted'} {referenced_hash}]"
                break
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT status FROM prompt_calls WHERE prompt_call_id = ?",
                (prompt_call_id,),
            ).fetchone()
            if current is None:
                raise FileNotFoundError(prompt_call_id)
            if current["status"] != "started":
                raise RuntimeError("prompt_call_terminal")
            connection.execute(
                """
                UPDATE prompt_calls SET
                    status = ?, messages_json = COALESCE(?, messages_json),
                    tool_calls_json = ?, completed_at = ?, error_json = ?,
                    output_ref = ?, output_hash = ?
                WHERE prompt_call_id = ?
                """,
                (
                    status,
                    json_text(redact_for_audit(audited_messages)) if audited_messages is not None else None,
                    json_text(redact_for_audit(tool_calls)), completed_at,
                    json_text(redact_for_audit(error)) if error else None,
                    output_ref, output_hash, prompt_call_id,
                ),
            )
            connection.execute(
                "INSERT INTO prompt_call_events(prompt_call_id, at, status, details_json) VALUES(?, ?, ?, ?)",
                (
                    prompt_call_id, completed_at, status,
                    json_text({
                        "output_ref": output_ref,
                        "output_hash": output_hash,
                        "error": redact_for_audit(error),
                    }),
                ),
            )

    def prompt_calls(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        self._ensure_database()
        with self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT * FROM prompt_calls ORDER BY started_at, prompt_call_id"
                ).fetchall()
            else:
                if limit < 1 or limit > 500:
                    raise ValueError("prompt call limit out of range")
                rows = connection.execute(
                    """
                    SELECT * FROM prompt_calls
                    ORDER BY started_at DESC, prompt_call_id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("messages_json", "parameters_json", "tool_calls_json", "error_json"):
                value = item.pop(key)
                item[key.removesuffix("_json")] = json.loads(value) if value else None
            result.append(item)
        return result

    def export_prompt_calls_jsonl(self) -> str:
        return "".join(json_text(item) + "\n" for item in self.prompt_calls())
