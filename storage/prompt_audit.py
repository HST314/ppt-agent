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

    def update_prompt_call_context(
        self,
        prompt_call_id: str,
        context: dict[str, Any],
    ) -> None:
        """Attach validated model declarations while an audit call is still open."""

        if not context:
            return
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status, parameters_json FROM prompt_calls WHERE prompt_call_id = ?",
                (prompt_call_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(prompt_call_id)
            if row["status"] != "started":
                raise RuntimeError("prompt_call_terminal")
            parameters = json.loads(row["parameters_json"]) if row["parameters_json"] else {}
            parameters.update(redact_for_audit(context))
            connection.execute(
                "UPDATE prompt_calls SET parameters_json = ? WHERE prompt_call_id = ?",
                (json_text(parameters), prompt_call_id),
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

    def sample_attempts(self) -> list[dict[str, Any]]:
        """Return a bounded public summary of the newest sample repair chain."""

        self._ensure_database()
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH RECURSIVE newest_sample_chain AS (
                    SELECT prompt_calls.*, 1 AS chain_depth FROM prompt_calls
                    WHERE prompt_call_id = (
                        SELECT prompt_call_id FROM prompt_calls
                        WHERE state = 'ppt_sample' AND parent_prompt_call_id IS NULL
                        ORDER BY started_at DESC, prompt_call_id DESC LIMIT 1
                    )
                    UNION ALL
                    SELECT child.*, parent.chain_depth + 1 FROM prompt_calls child
                    JOIN newest_sample_chain parent
                      ON child.parent_prompt_call_id = parent.prompt_call_id
                    WHERE child.state = 'ppt_sample' AND parent.chain_depth < 3
                )
                SELECT * FROM newest_sample_chain
                ORDER BY started_at, prompt_call_id LIMIT 3
                """
            ).fetchall()
        result: list[dict[str, Any]] = []
        for attempt_number, row in enumerate(rows, start=1):
            messages = json.loads(row["messages_json"]) if row["messages_json"] else []
            declared_tool_calls = [
                call
                for message in messages
                if message.get("role") == "assistant"
                for call in (message.get("tool_calls") or [])
            ]
            tool_rounds = sum(
                bool(message.get("tool_calls"))
                for message in messages
                if message.get("role") == "assistant"
            )
            skill_reads = sum(
                (call.get("function") or {}).get("name") == "read"
                for call in declared_tool_calls
            )
            parameters = json.loads(row["parameters_json"]) if row["parameters_json"] else {}
            error = json.loads(row["error_json"]) if row["error_json"] else None
            published = row["status"] == "completed" and bool(row["output_ref"])
            if published:
                reason = "通过结构与安全契约校验，已发布为 PPT 样品。"
            elif row["status"] == "started":
                reason = "正在生成，尚未进入发布校验。"
            elif row["status"] == "conflicted":
                reason = "生成完成时工程版本已变化，因此未发布。"
            elif error and "maximum tool rounds exceeded" in str(error.get("message", "")):
                reason = "达到最大工具轮次，尚未返回最终包清单。"
            elif error and error.get("code") in {
                "sample_json_incomplete", "sample_output_invalid", "sample_package_invalid",
                "sample_html_rejected",
            }:
                reason = " ".join(str(error.get("message") or "生成结果未通过契约校验。").split())[:300]
            else:
                reason = "生成未完成，因此未发布。"
            result.append({
                "attempt": attempt_number,
                "prompt_call_id": row["prompt_call_id"],
                "status": row["status"],
                "published": published,
                "reason": reason,
                "failure_code": (error or {}).get("code"),
                "tool_rounds": tool_rounds,
                "tool_call_count": len(declared_tool_calls),
                "skill_read_count": skill_reads,
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "provider": parameters.get("provider"),
                "model": parameters.get("model"),
            })
        return result

    def full_deck_attempts(self) -> list[dict[str, Any]]:
        """Return the newest full-deck generation's segment/repair audit summary."""

        self._ensure_database()
        with self._connect() as connection:
            latest = connection.execute(
                """
                SELECT parameters_json FROM prompt_calls
                WHERE state = 'ppt_full'
                ORDER BY started_at DESC, prompt_call_id DESC LIMIT 1
                """
            ).fetchone()
            if latest is None:
                return []
            latest_parameters = json.loads(latest["parameters_json"])
            generation_id = latest_parameters.get("generation_id")
            if not generation_id:
                return []
            rows = connection.execute(
                """
                SELECT prompt_call_id, parent_prompt_call_id, status, messages_json,
                       parameters_json, started_at, completed_at, error_json,
                       output_ref
                FROM prompt_calls
                WHERE state = 'ppt_full'
                  AND json_extract(parameters_json, '$.generation_id') = ?
                ORDER BY started_at, prompt_call_id LIMIT 240
                """,
                (generation_id,),
            ).fetchall()
        decoded: list[tuple[Any, dict[str, Any]]] = []
        for row in rows:
            parameters = json.loads(row["parameters_json"]) if row["parameters_json"] else {}
            decoded.append((row, parameters))
        attempt_by_segment: dict[str, int] = {}
        result: list[dict[str, Any]] = []
        for row, parameters in decoded:
            operation = parameters.get("operation") or "generate_full_deck"
            target = (
                parameters.get("changed_source_slide_numbers")
                or parameters.get("target_slide_numbers")
                or []
            )
            segment_key = ",".join(str(number) for number in target)
            attempt_by_segment[segment_key] = attempt_by_segment.get(segment_key, 0) + 1
            messages = json.loads(row["messages_json"]) if row["messages_json"] else []
            declared_tool_calls = [
                call
                for message in messages
                if message.get("role") == "assistant"
                for call in (message.get("tool_calls") or [])
            ]
            error = json.loads(row["error_json"]) if row["error_json"] else None
            published = row["status"] == "completed" and bool(row["output_ref"])
            if published:
                reason = (
                    "修改页通过声明范围与来源保真校验，新全稿修订已发布。"
                    if operation == "revise_full_deck"
                    else "目标页通过契约校验，完整全稿已通过 Composer 发布。"
                )
            elif row["status"] == "started":
                reason = "页段正在生成，尚未进入最终组装。"
            elif row["status"] == "conflicted":
                reason = "组装完成时工程版本已变化，因此未发布。"
            elif error and error.get("code") in {
                "full_deck_segment_invalid",
                "full_deck_target_mismatch",
                "full_deck_composition_failed",
                "full_deck_package_invalid",
            }:
                reason = " ".join(
                    str(error.get("message") or "页段未通过契约校验。").split()
                )[:300]
            else:
                reason = "页段生成未完成，因此未发布全稿。"
            result.append({
                "attempt": attempt_by_segment[segment_key],
                "prompt_call_id": row["prompt_call_id"],
                "status": row["status"],
                "published": published,
                "reason": reason,
                "failure_code": (error or {}).get("code"),
                "operation": operation,
                "changed_slot_ids": parameters.get("changed_slot_ids") or [],
                "target_slide_numbers": target,
                "segment_range": (
                    f"{target[0]}–{target[-1]} 页" if target else "未知页段"
                ),
                "tool_rounds": sum(
                    bool(message.get("tool_calls"))
                    for message in messages
                    if message.get("role") == "assistant"
                ),
                "tool_call_count": len(declared_tool_calls),
                "skill_read_count": sum(
                    (call.get("function") or {}).get("name") == "read"
                    for call in declared_tool_calls
                ),
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "provider": parameters.get("provider"),
                "model": parameters.get("model"),
                "composer_version": parameters.get("composer_version"),
            })
        return result

    def export_prompt_calls_jsonl(self) -> str:
        return "".join(json_text(item) + "\n" for item in self.prompt_calls())
