"""Session transcript persistence and recovery, independent of Agent orchestration."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
import uuid

from scidev.agent.attachments import image_content_parts
from scidev.runtime.errors import PermanentError
from scidev.runtime.helpers import new_id, now_iso

if TYPE_CHECKING:
    from scidev.workspace.git import GitManager
    from scidev.runtime.ledger import EventLedger


def session_path(project_root: Path, session_id: str) -> Path:
    return project_root / ".research" / "sessions" / f"{session_id}.json"


def save_session(
    session: dict[str, Any],
    *,
    path: Path,
    project_root: Path,
    ledger: EventLedger,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(session, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    ledger.update_session(
        session["session_id"],
        session["status"],
        session["prompt"],
        path.relative_to(project_root).as_posix(),
    )


def recover_interrupted_tool_calls(
    session: dict[str, Any],
    *,
    ledger: EventLedger,
    save: Callable[[dict[str, Any]], None],
    emit: Callable[[str, dict[str, Any]], None],
) -> None:
    """Reconcile transcript tool calls before another model request can replay them."""
    messages = session.get("messages")
    if not isinstance(messages, list):
        return
    session_id = str(session.get("session_id") or "")
    journal_records = ledger.tool_calls_for_session(session_id)
    recovered_events: list[dict[str, Any]] = []
    clear_rows: set[tuple[int, int]] = set()
    rebuilt: list[dict[str, Any]] = []
    changed = False
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            index += 1
            continue
        rebuilt.append(message)
        calls = message.get("tool_calls")
        if message.get("role") != "assistant" or not isinstance(calls, list) or not calls:
            index += 1
            continue

        assistant_message_index = index
        index += 1
        existing: dict[str, list[dict[str, Any]]] = {}
        while index < len(messages):
            candidate = messages[index]
            if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                break
            existing.setdefault(str(candidate.get("tool_call_id") or ""), []).append(candidate)
            index += 1

        for call_index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            if not call_id:
                call_id = new_id("call")
                call["id"] = call_id
                changed = True
            prior = existing.get(call_id)
            if prior:
                rebuilt.append(prior.pop(0))
                record = journal_records.get((assistant_message_index, call_index))
                if record is not None and record.get("call_id") == call_id:
                    clear_rows.add((assistant_message_index, call_index))
                continue

            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(function.get("name") or "unknown")[:120]
            record = journal_records.get((assistant_message_index, call_index))
            status = str(record.get("status")) if record else "legacy_unknown"
            if record and (record.get("call_id") != call_id or record.get("name") != name):
                status = "legacy_unknown"
            if (
                status in {"started", "completed", "legacy_unknown"}
                and name in {"write_file", "replace_in_file", "apply_patch", "delegate_research", "run_command"}
            ):
                session["verification_code_changed"] = True
                session["verification_edit_revision"] = int(
                    session.get("verification_edit_revision", 0) or 0
                ) + 1
            if status == "completed":
                content = str(record.get("result") or "")
            elif status == "pending":
                content = (
                    f"[Harness 恢复] 工具 {name} 已记录但尚未启动；本次没有执行该调用。"
                    "请按当前状态继续。"
                )
            elif status == "started":
                content = (
                    f"[Harness 恢复] 工具 {name} 已启动，但结果未能持久化；它可能已经产生副作用。"
                    "为避免重复执行，Harness 没有重放该调用。先检查当前工作区/外部状态；"
                    "确认确有必要后再发起新的调用。"
                )
            else:
                content = (
                    f"[Harness 恢复] 工具 {name} 的旧会话缺少执行记录，是否执行过无法确定。"
                    "Harness 没有重放该调用。先检查当前状态，再决定是否发起新的调用。"
                )
            rebuilt.append({"role": "tool", "tool_call_id": call_id, "content": content})
            changed = True
            if record is not None and (
                record.get("call_id") == call_id or record.get("status") == "pending"
            ):
                clear_rows.add((assistant_message_index, call_index))
            recovered_events.append({
                "session_id": session_id,
                "assistant_message_index": assistant_message_index,
                "call_index": call_index,
                "call_id": call_id,
                "name": name,
                "status": status,
            })
        for remaining in existing.values():
            rebuilt.extend(remaining)

    if changed:
        session["messages"] = rebuilt
        session["updated_at"] = now_iso()
        save(session)
    for assistant_message_index, call_index in clear_rows:
        ledger.clear_tool_call(session_id, assistant_message_index, call_index)
    for event in recovered_events:
        ledger.append("tool_call_recovered", event)
        emit("tool_call_recovered", event)


def load_session(
    session_id: str,
    prompt: str,
    images: list[dict[str, Any]] | None = None,
    *,
    path: Path,
    project_root: Path,
    ledger: EventLedger,
    git: GitManager,
    system_prompt_for_task: Callable[[str], str],
    save: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PermanentError(f"无法恢复编码会话：{exc}") from exc
    content: str | list[dict[str, Any]] = f"用户任务：\n{prompt}"
    image_parts = image_content_parts(images or [])
    if image_parts:
        content = [{"type": "text", "text": content}, *image_parts]
    session = {
        "session_id": session_id,
        "prompt": prompt,
        "status": "active",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "last_prompt": prompt,
        "git_base_sha": git.head_sha(),
        "git_preexisting_paths": sorted(git.status_paths()),
        "agent_changed_paths": [],
        "git_result_sha": "",
        "messages": [
            {"role": "system", "content": system_prompt_for_task(prompt)},
            {"role": "user", "content": content},
        ],
    }
    if images:
        session["attachments"] = images
    ledger.create_session(
        session_id,
        prompt,
        path.relative_to(project_root).as_posix(),
    )
    ledger.append("coding_session_started", {"session_id": session_id, "prompt": prompt})
    save(session)
    return session


def attach_images_to_last_user_message(
    messages: list[dict[str, Any]],
    images: list[dict[str, Any]],
) -> None:
    parts = image_content_parts(images)
    if not parts:
        return
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = [{"type": "text", "text": content}, *parts]
        elif isinstance(content, list):
            if not any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in content
            ):
                content.extend(parts)
        return
    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": "用户附加了图片。"}, *parts],
    })
