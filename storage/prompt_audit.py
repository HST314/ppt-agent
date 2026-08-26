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
        self.finish_prompt_calls([{
            "prompt_call_id": prompt_call_id,
            "status": status,
            "traces": traces,
            "messages": messages,
            "output_ref": output_ref,
            "output_hash": output_hash,
            "error": error,
        }])

    def finish_prompt_calls(self, calls: list[dict[str, Any]]) -> None:
        """Move a related set of started prompt calls to terminal states atomically."""

        if not calls:
            return
        completed_at = utc_now()
        prepared: list[dict[str, Any]] = []
        prompt_call_ids: set[str] = set()
        for call in calls:
            prompt_call_id = call["prompt_call_id"]
            status = call["status"]
            if status not in {"completed", "failed", "conflicted"}:
                raise ValueError("invalid prompt call status")
            if prompt_call_id in prompt_call_ids:
                raise ValueError("duplicate prompt call id")
            prompt_call_ids.add(prompt_call_id)
            output_ref = call.get("output_ref")
            output_hash = call.get("output_hash")
            audited_messages = (
                deepcopy(call.get("messages")) if call.get("messages") is not None else None
            )
            if audited_messages:
                for message in reversed(audited_messages):
                    content = message.get("content")
                    if (
                        message.get("role") != "assistant"
                        or not isinstance(content, str)
                        or not content
                    ):
                        continue
                    referenced_hash = output_hash or (
                        "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
                    )
                    message["content"] = (
                        f"[OUTPUT_REF {output_ref or 'uncommitted'} {referenced_hash}]"
                    )
                    break
            prepared.append({
                "prompt_call_id": prompt_call_id,
                "status": status,
                "tool_calls": [
                    item
                    for item in call.get("traces") or []
                    if item.get("type") == "tool_call"
                ],
                "messages": audited_messages,
                "output_ref": output_ref,
                "output_hash": output_hash,
                "error": call.get("error"),
            })
        with self._transaction() as connection:
            for call in prepared:
                current = connection.execute(
                    "SELECT status FROM prompt_calls WHERE prompt_call_id = ?",
                    (call["prompt_call_id"],),
                ).fetchone()
                if current is None:
                    raise FileNotFoundError(call["prompt_call_id"])
                if current["status"] != "started":
                    raise RuntimeError("prompt_call_terminal")
            for call in prepared:
                connection.execute(
                    """
                    UPDATE prompt_calls SET
                        status = ?, messages_json = COALESCE(?, messages_json),
                        tool_calls_json = ?, completed_at = ?, error_json = ?,
                        output_ref = ?, output_hash = ?
                    WHERE prompt_call_id = ?
                    """,
                    (
                        call["status"],
                        json_text(redact_for_audit(call["messages"]))
                        if call["messages"] is not None
                        else None,
                        json_text(redact_for_audit(call["tool_calls"])),
                        completed_at,
                        json_text(redact_for_audit(call["error"]))
                        if call["error"]
                        else None,
                        call["output_ref"],
                        call["output_hash"],
                        call["prompt_call_id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO prompt_call_events(
                        prompt_call_id, at, status, details_json
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        call["prompt_call_id"],
                        completed_at,
                        call["status"],
                        json_text({
                            "output_ref": call["output_ref"],
                            "output_hash": call["output_hash"],
                            "error": redact_for_audit(call["error"]),
                        }),
                    ),
                )

    def append_prompt_call_progress(
        self,
        prompt_call_id: str,
        *,
        status: str,
        details: dict[str, Any],
        traces: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> None:
        """Persist one live, redacted model/tool round without closing the call."""

        if status != "tool_round_completed":
            raise ValueError("invalid prompt progress status")
        at = utc_now()
        tool_calls = [item for item in traces if item.get("type") == "tool_call"]
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
                UPDATE prompt_calls
                SET messages_json = ?, tool_calls_json = ?
                WHERE prompt_call_id = ?
                """,
                (
                    json_text(redact_for_audit(messages)),
                    json_text(redact_for_audit(tool_calls)),
                    prompt_call_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO prompt_call_events(prompt_call_id, at, status, details_json)
                VALUES(?, ?, ?, ?)
                """,
                (
                    prompt_call_id,
                    at,
                    status,
                    json_text(redact_for_audit(details)),
                ),
            )

    def prompt_call_events(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return bounded live/terminal prompt events for the status console."""

        if limit < 1 or limit > 2000:
            raise ValueError("prompt event limit out of range")
        self._ensure_database()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.event_id, e.prompt_call_id, e.at, e.status, e.details_json,
                       p.state, p.parameters_json
                FROM prompt_call_events e
                JOIN prompt_calls p ON p.prompt_call_id = e.prompt_call_id
                ORDER BY e.event_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "prompt_call_id": row["prompt_call_id"],
                "at": row["at"],
                "status": row["status"],
                "state": row["state"],
                "details": json.loads(row["details_json"]) if row["details_json"] else {},
                "parameters": json.loads(row["parameters_json"]) if row["parameters_json"] else {},
            }
            for row in rows
        ]

    def sample_resume_context(
        self,
        prompt_call_id: str,
        *,
        checkpoint_id: str,
    ) -> dict[str, Any]:
        """Load and validate a persisted leaf checkpoint for sample continuation."""

        self._ensure_database()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM prompt_calls WHERE prompt_call_id = ?",
                (prompt_call_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(prompt_call_id)
            child = connection.execute(
                "SELECT 1 FROM prompt_calls WHERE parent_prompt_call_id = ? LIMIT 1",
                (prompt_call_id,),
            ).fetchone()
            chain_rows = connection.execute(
                """
                WITH RECURSIVE ancestors AS (
                    SELECT prompt_call_id, parent_prompt_call_id, started_at,
                           tool_calls_json
                    FROM prompt_calls WHERE prompt_call_id = ?
                    UNION ALL
                    SELECT parent.prompt_call_id, parent.parent_prompt_call_id,
                           parent.started_at, parent.tool_calls_json
                    FROM prompt_calls parent
                    JOIN ancestors child
                      ON child.parent_prompt_call_id = parent.prompt_call_id
                )
                SELECT prompt_call_id, tool_calls_json
                FROM ancestors ORDER BY started_at, prompt_call_id
                """,
                (prompt_call_id,),
            ).fetchall()
        error = json.loads(row["error_json"]) if row["error_json"] else {}
        parameters = json.loads(row["parameters_json"]) if row["parameters_json"] else {}
        messages = json.loads(row["messages_json"]) if row["messages_json"] else []
        if row["state"] != "ppt_sample" or row["status"] != "failed":
            raise RuntimeError("sample_resume_not_available:该尝试不是可续跑的失败样品任务。")
        if error.get("code") != "max_tool_rounds_exceeded":
            raise RuntimeError("sample_resume_not_available:只有达到工具轮次上限的任务可以续跑。")
        if child:
            raise RuntimeError("sample_resume_superseded:该尝试已有后续，请刷新后继续最新尝试。")
        if parameters.get("generation_checkpoint_id") != checkpoint_id:
            raise RuntimeError("sample_resume_stale:工程检查点已变化，不能继续旧生成。")
        cumulative_rounds = sum(
            bool(message.get("tool_calls"))
            for message in messages
            if message.get("role") == "assistant"
        )
        if cumulative_rounds >= 100:
            raise RuntimeError("sample_resume_limit:整条生成链已达到 100 个工具轮次。")
        chain_tool_calls = [
            tool_call
            for chain_row in chain_rows
            for tool_call in (
                json.loads(chain_row["tool_calls_json"])
                if chain_row["tool_calls_json"] else []
            )
        ]
        return {
            "prompt_call_id": prompt_call_id,
            "parent_prompt_call_id": row["parent_prompt_call_id"],
            "messages": messages,
            "parameters": parameters,
            "tool_calls": chain_tool_calls,
            "cumulative_tool_call_count": len(chain_tool_calls),
            "cumulative_skill_read_count": sum(
                item.get("tool") == "read" for item in chain_tool_calls
            ),
            "cumulative_tool_rounds": cumulative_rounds,
            "remaining_tool_rounds": 100 - cumulative_rounds,
            "prompt_call_ids": [item["prompt_call_id"] for item in chain_rows],
            "template_id": row["template_id"],
            "template_hash": row["template_hash"],
            "skills_hash": row["skills_hash"],
        }

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

    def prompt_calls(
        self,
        *,
        limit: int | None = None,
        include_messages: bool = True,
    ) -> list[dict[str, Any]]:
        self._ensure_database()
        columns = "*" if include_messages else """
            prompt_call_id, parent_prompt_call_id, project_id, state, status,
            template_id, template_version, template_hash, model_config_hash,
            runtime_config_hash, skills_hash, parameters_json, tool_calls_json,
            started_at, completed_at, error_json, output_ref, output_hash
        """
        with self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    f"SELECT {columns} FROM prompt_calls "
                    "ORDER BY started_at, prompt_call_id"
                ).fetchall()
            else:
                if limit < 1 or limit > 500:
                    raise ValueError("prompt call limit out of range")
                rows = connection.execute(
                    f"SELECT {columns} FROM prompt_calls "
                    "ORDER BY started_at DESC, prompt_call_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("messages_json", "parameters_json", "tool_calls_json", "error_json"):
                if key not in item:
                    continue
                value = item.pop(key)
                item[key.removesuffix("_json")] = json.loads(value) if value else None
            result.append(item)
        return result

    def sample_attempts(
        self,
        *,
        current_checkpoint_id: str | None = None,
    ) -> list[dict[str, Any]]:
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
                    WHERE child.state = 'ppt_sample' AND parent.chain_depth < 100
                )
                SELECT * FROM newest_sample_chain
                ORDER BY started_at, prompt_call_id LIMIT 100
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
            elif error and error.get("code") == "max_tool_rounds_exceeded":
                reason = "达到最大工具轮次，尚未返回最终包清单。"
            elif error and error.get("code") in {
                "sample_json_incomplete", "sample_output_invalid", "sample_package_invalid",
                "sample_html_rejected",
            }:
                reason = " ".join(str(error.get("message") or "生成结果未通过契约校验。").split())[:300]
            else:
                reason = "生成未完成，因此未发布。"
            remaining_rounds = max(0, 100 - tool_rounds)
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
                "round_limit": parameters.get("round_limit"),
                "remaining_tool_rounds": remaining_rounds,
                "resume_available": False,
                "resume_blocked_reason": None,
                "resume_options": [
                    value for value in (5, 10, 20) if value <= remaining_rounds
                ],
            })
        if result:
            latest = result[-1]
            parameters = json.loads(rows[-1]["parameters_json"]) if rows[-1]["parameters_json"] else {}
            latest["resume_available"] = bool(
                latest["status"] == "failed"
                and latest["failure_code"] == "max_tool_rounds_exceeded"
                and latest["resume_options"]
                and current_checkpoint_id is not None
                and parameters.get("generation_checkpoint_id") == current_checkpoint_id
            )
            for item in result:
                if item["failure_code"] != "max_tool_rounds_exceeded":
                    continue
                if item is not latest:
                    item["resume_blocked_reason"] = "已有后续尝试，请在最新尝试处继续。"
                elif not item["resume_options"]:
                    item["resume_blocked_reason"] = "整条生成链剩余不足 5 轮，已达到续跑保护上限。"
                elif parameters.get("generation_checkpoint_id") != current_checkpoint_id:
                    item["resume_blocked_reason"] = "工程检查点已变化，不能继续旧生成。"
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
            generation_id = (
                latest_parameters.get("generation_id")
                or latest_parameters.get("generation_session_id")
            )
            if not generation_id:
                return []
            rows = connection.execute(
                """
                SELECT prompt_call_id, parent_prompt_call_id, status, messages_json,
                       parameters_json, started_at, completed_at, error_json,
                       output_ref
                FROM prompt_calls
                WHERE state = 'ppt_full'
                  AND COALESCE(
                    json_extract(parameters_json, '$.generation_id'),
                    json_extract(parameters_json, '$.generation_session_id')
                  ) = ?
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
