"""Research delegation, caching and accounting for the coding-agent coordinator."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from scidev.runtime.errors import PermanentError
from scidev.agent.multi_agent import run_specialist_agent, validate_delegation, validate_parallel_delegations
from scidev.workspace.patching import PatchReviewRejected
from scidev.runtime.helpers import now_iso

if TYPE_CHECKING:
    from scidev.runtime.ledger import EventLedger


def delegate_research(
    provider: Any,
    session: dict[str, Any],
    prompt: str,
    arguments: Any,
    *,
    turn: int,
    workspace_revision: int,
    task_id: str,
    project_root: Path,
    toolbox: Any,
    default_model: str,
    call_limit: int,
    ledger: EventLedger,
    snapshot: Callable[[dict[str, Any]], dict[str, Any]],
    request_scope_id: Callable[[str, str], str],
    save: Callable[[dict[str, Any]], None],
    emit: Callable[[str, dict[str, Any]], None],
    record_model_request: Callable[[dict[str, Any], str, str, dict[str, Any]], None],
) -> str:
    try:
        request = validate_delegation(arguments)
        normalized_paths: list[str] = []
        for raw_path in request["focus_paths"]:
            resolved = toolbox.project_context.resolve_scope(raw_path)
            relative = resolved.relative_to(project_root).as_posix()
            if relative not in normalized_paths:
                normalized_paths.append(relative)
        request["focus_paths"] = normalized_paths
    except (ValueError, OSError, RuntimeError) as exc:
        raise PermanentError(f"协作 Agent 委派请求无效：{exc}") from exc

    records = session.setdefault("specialist_calls", [])
    if not isinstance(records, list):
        records = []
        session["specialist_calls"] = records
    collaboration = session.get("multi_agent_config")
    configured_model = (
        collaboration.get("model", "")
        if isinstance(collaboration, dict)
        else default_model
    )
    selected_model = str(
        configured_model
        or getattr(provider, "model", "")
        or ""
    ).strip()[:160]
    workspace_state = snapshot(session)
    fingerprint_payload = json.dumps(
        {
            "role": request["role"],
            "question": request["question"],
            "focus_paths": request["focus_paths"],
            "workspace_revision": int(workspace_revision),
            "workspace_fingerprint": workspace_state.get("fingerprint", ""),
            "workspace_complete": bool(workspace_state.get("complete")),
            "model": selected_model,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
    existing = next(
        (item for item in reversed(records)
         if isinstance(item, dict)
         and item.get("fingerprint") == fingerprint
         and item.get("status") == "completed"
         and item.get("workspace_complete") is True),
        None,
    )
    if existing is not None:
        summary = str(existing.get("summary") or "该协作调查先前未成功完成。")[:6000]
        event = {
            "session_id": session["session_id"],
            "task_id": task_id,
            "role": request["role"],
            "call_index": existing.get("call_index"),
            "status": existing.get("status"),
            "workspace_revision": workspace_revision,
            "workspace_complete": bool(workspace_state.get("complete")),
        }
        ledger.append("specialist_result_reused", event)
        emit("specialist_result_reused", event)
        return f"协作 Agent（{request['role']}，复用同一代码版本的调查）结果：\n{summary}"

    if existing is None:
        if len(records) >= call_limit:
            event = {
                "session_id": session["session_id"],
                "task_id": task_id,
                "turn": turn,
                "limit": call_limit,
            }
            ledger.append("specialist_limit_reached", event)
            emit("specialist_limit_reached", event)
            return f"本任务最多委派 {call_limit} 次；本次未启动子 Agent。请基于已有证据继续。"
        existing = {
            "call_index": len(records) + 1,
            "task_id": task_id,
            "fingerprint": fingerprint,
            "question_sha256": hashlib.sha256(request["question"].encode("utf-8")).hexdigest(),
            "role": request["role"],
            "focus_paths": request["focus_paths"],
            "workspace_revision": workspace_revision,
            "workspace_complete": bool(workspace_state.get("complete")),
            "model": selected_model,
            "status": "running",
            "summary": "",
            "started_at": now_iso(),
        }
        records.append(existing)
    call_index = int(existing.get("call_index", 0) or 0)
    session_id = str(session["session_id"])
    event_base = {
        "session_id": session_id,
        "task_id": task_id,
        "turn": turn,
        "role": request["role"],
        "call_index": call_index,
        "model": selected_model,
        "focus_paths": request["focus_paths"],
        "workspace_revision": workspace_revision,
    }
    existing["status"] = "running"
    existing["started_at"] = now_iso()
    save(session)
    ledger.append("specialist_started", event_base)
    emit("specialist_started", event_base)

    request_scope = request_scope_id(
        session_id, task_id or str(session.get("last_task_id") or session_id),
    )
    request_id = f"specialist-{request_scope}-{call_index}-{fingerprint[:12]}"

    def specialist_event(name: str, data: dict[str, Any]) -> None:
        event = {"session_id": session_id, "task_id": task_id, "call_index": call_index, **data}
        ledger.append(name, event)
        emit(name, event)

    def specialist_metrics(child_turn: int, metrics: dict[str, Any]) -> None:
        logical_task = task_id or str(session.get("last_task_id") or session_id)
        record_model_request(
            session,
            f"{logical_task}|specialist:{call_index}:turn:{child_turn}",
            "specialist",
            metrics,
        )

    try:
        outcome = run_specialist_agent(
            provider,
            toolbox,
            role=request["role"],
            task=prompt,
            question=request["question"],
            focus_paths=request["focus_paths"],
            model=selected_model,
            request_id=request_id,
            on_event=specialist_event,
            on_metrics=specialist_metrics,
        )
    except Exception as exc:
        outcome = {
            "status": "unavailable",
            "summary": f"子 Agent 调用失败（{type(exc).__name__}）；主 Agent 可继续自行调查。",
            "model": selected_model,
            "turns": 0,
            "tool_calls": 0,
            "modified_paths": [],
        }
    existing.update(
        {
            "status": str(outcome.get("status") or "unavailable"),
            "summary": str(outcome.get("summary") or "")[:6000],
            "model": str(outcome.get("model") or selected_model)[:160],
            "turns": int(outcome.get("turns", 0) or 0),
            "tool_calls": int(outcome.get("tool_calls", 0) or 0),
            "modified_paths": list(outcome.get("modified_paths") or []),
            "patch_partial": bool(outcome.get("patch_partial")),
            "patch_rejected": bool(outcome.get("patch_rejected")),
            "finished_at": now_iso(),
        }
    )
    save(session)
    event = {
        **event_base,
        "status": existing["status"],
        "turns": existing["turns"],
        "tool_calls": existing["tool_calls"],
        "modified_paths": list(existing.get("modified_paths") or []),
        "patch_partial": bool(existing.get("patch_partial")),
        "summary": existing["summary"],
    }
    event_name = "specialist_completed" if existing["status"] == "completed" else "specialist_failed"
    ledger.append(event_name, event)
    emit(event_name, event)
    if existing.get("patch_rejected") is True:
        raise PatchReviewRejected(existing["summary"] or "编码专员补丁未获批准；任务已停止。")
    return f"协作 Agent（{request['role']}）调查结果（需由主 Agent 核验）：\n{existing['summary']}"


def delegate_parallel_research(
    provider: Any,
    session: dict[str, Any],
    prompt: str,
    arguments: Any,
    *,
    turn: int,
    workspace_revision: int,
    task_id: str,
    project_root: Path,
    toolbox: Any,
    default_model: str,
    call_limit: int,
    ledger: EventLedger,
    snapshot: Callable[[dict[str, Any]], dict[str, Any]],
    request_scope_id: Callable[[str, str], str],
    save: Callable[[dict[str, Any]], None],
    emit: Callable[[str, dict[str, Any]], None],
    record_model_request: Callable[[dict[str, Any], str, str, dict[str, Any]], None],
) -> str:
    try:
        requests = validate_parallel_delegations(arguments)
        for request in requests:
            normalized_paths: list[str] = []
            for raw_path in request["focus_paths"]:
                resolved = toolbox.project_context.resolve_scope(raw_path)
                relative = resolved.relative_to(project_root).as_posix()
                if relative not in normalized_paths:
                    normalized_paths.append(relative)
            request["focus_paths"] = normalized_paths
    except (ValueError, OSError, RuntimeError) as exc:
        raise PermanentError(f"并行协作 Agent 委派请求无效：{exc}") from exc

    records = session.setdefault("specialist_calls", [])
    if not isinstance(records, list):
        records = []
        session["specialist_calls"] = records
    collaboration = session.get("multi_agent_config")
    configured_model = (
        collaboration.get("model", "")
        if isinstance(collaboration, dict)
        else default_model
    )
    selected_model = str(configured_model or getattr(provider, "model", "") or "").strip()[:160]
    workspace_state = snapshot(session)
    session_id = str(session["session_id"])
    request_scope = request_scope_id(
        session_id, task_id or str(session.get("last_task_id") or session_id),
    )
    results: list[str | None] = [None] * len(requests)
    jobs: list[dict[str, Any]] = []
    normalized_identities: set[tuple[str, str, tuple[str, ...]]] = set()

    for position, request in enumerate(requests):
        identity = (request["role"], request["question"], tuple(request["focus_paths"]))
        if identity in normalized_identities:
            raise PermanentError("并行委派在路径规范化后包含重复子任务")
        normalized_identities.add(identity)
        fingerprint_payload = json.dumps(
            {
                "role": request["role"],
                "question": request["question"],
                "focus_paths": request["focus_paths"],
                "workspace_revision": int(workspace_revision),
                "workspace_fingerprint": workspace_state.get("fingerprint", ""),
                "workspace_complete": bool(workspace_state.get("complete")),
                "model": selected_model,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
        cached = next(
            (item for item in reversed(records)
             if isinstance(item, dict)
             and item.get("fingerprint") == fingerprint
             and item.get("status") == "completed"
             and item.get("workspace_complete") is True),
            None,
        )
        if cached is not None:
            summary = str(cached.get("summary") or "该协作调查先前未成功完成。")[:6000]
            results[position] = f"协作 Agent（{request['role']}，复用同一代码版本的调查）结果：\n{summary}"
            event = {
                "session_id": session_id,
                "task_id": task_id,
                "role": request["role"],
                "call_index": cached.get("call_index"),
                "status": "completed",
                "workspace_revision": workspace_revision,
                "workspace_complete": bool(workspace_state.get("complete")),
            }
            ledger.append("specialist_result_reused", event)
            emit("specialist_result_reused", event)
            continue

        jobs.append({"position": position, "request": request, "fingerprint": fingerprint})

    if len(records) + len(jobs) > call_limit:
        event = {
            "session_id": session_id,
            "task_id": task_id,
            "turn": turn,
            "requested": len(jobs),
            "remaining": max(0, call_limit - len(records)),
            "limit": call_limit,
        }
        ledger.append("specialist_limit_reached", event)
        emit("specialist_limit_reached", event)
        results = [item for item in results if item]
        results.append("并行调查未启动：本任务的子 Agent 委派次数不足；没有部分启动新调查。")
        return "\n\n".join(results)

    for job in jobs:
        request = job["request"]
        call_index = len(records) + 1
        record = {
            "call_index": call_index,
            "task_id": task_id,
            "fingerprint": job["fingerprint"],
            "question_sha256": hashlib.sha256(request["question"].encode("utf-8")).hexdigest(),
            "role": request["role"],
            "focus_paths": request["focus_paths"],
            "workspace_revision": workspace_revision,
            "workspace_complete": bool(workspace_state.get("complete")),
            "model": selected_model,
            "status": "running",
            "summary": "",
            "started_at": now_iso(),
        }
        records.append(record)
        job.update({
            "record": record,
            "call_index": call_index,
            "event_base": {
                "session_id": session_id,
                "task_id": task_id,
                "turn": turn,
                "role": request["role"],
                "call_index": call_index,
                "model": selected_model,
                "focus_paths": request["focus_paths"],
                "workspace_revision": workspace_revision,
            },
            "events": [],
            "metrics": [],
        })

    if not jobs:
        return "\n\n".join(item for item in results if item)

    save(session)
    for job in jobs:
        ledger.append("specialist_started", job["event_base"])
        emit("specialist_started", job["event_base"])

    def execute(job: dict[str, Any]) -> dict[str, Any]:
        request = job["request"]

        def capture_event(name: str, data: dict[str, Any]) -> None:
            job["events"].append((name, data))

        def capture_metrics(child_turn: int, metrics: dict[str, Any]) -> None:
            job["metrics"].append((child_turn, metrics))

        try:
            outcome = run_specialist_agent(
                provider,
                toolbox,
                role=request["role"],
                task=prompt,
                question=request["question"],
                focus_paths=request["focus_paths"],
                model=selected_model,
                request_id=(f"specialist-{request_scope}-{job['call_index']}-"
                            f"{job['fingerprint'][:12]}"),
                on_event=capture_event,
                on_metrics=capture_metrics,
            )
        except Exception as exc:
            outcome = {
                "status": "unavailable",
                "summary": f"子 Agent 调用失败（{type(exc).__name__}）；主 Agent 可继续自行调查。",
                "model": selected_model,
                "turns": 0,
                "tool_calls": 0,
                "modified_paths": [],
            }
        return outcome

    # Workers return data only; session persistence and usage accounting remain single-threaded.
    outcomes: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="scidev-research") as executor:
        future_to_job = {executor.submit(execute, job): job for job in jobs}
        for future in as_completed(future_to_job):
            job = future_to_job[future]
            outcomes[job["call_index"]] = future.result()

    try:
        workspace_after = snapshot(session)
    except Exception:
        workspace_after = {"complete": False, "fingerprint": ""}
    workspace_stable = bool(
        workspace_state.get("complete") is True
        and workspace_after.get("complete") is True
        and workspace_state.get("fingerprint") == workspace_after.get("fingerprint")
    )
    workspace_changed = bool(
        workspace_state.get("complete") is True and not workspace_stable
    )

    for job in jobs:
        request = job["request"]
        record = job["record"]
        outcome = outcomes[job["call_index"]]
        for name, data in job["events"]:
            event = {
                "session_id": session_id,
                "task_id": task_id,
                "call_index": job["call_index"],
                **data,
            }
            ledger.append(name, event)
            emit(name, event)
        for child_turn, metrics in job["metrics"]:
            logical_task = task_id or str(session.get("last_task_id") or session_id)
            record_model_request(
                session,
                f"{logical_task}|specialist:{job['call_index']}:turn:{child_turn}",
                "specialist",
                metrics,
            )
        status = str(outcome.get("status") or "unavailable")
        summary = str(outcome.get("summary") or "")[:6000]
        notice = ""
        if status == "completed" and workspace_changed:
            status = "stale"
            notice = "并行调查期间工作区发生变化；报告可能混合不同代码版本，不会缓存为可复用成功结果。"
        elif status == "completed" and not workspace_stable:
            notice = "Harness 无法完整核实调查期间的工作区版本稳定性；此报告不会缓存复用。"
        if notice:
            suffix = "\n\n" + notice
            summary = summary[:max(0, 6000 - len(suffix))].rstrip() + suffix
        record.update({
            "status": status,
            "summary": summary,
            "workspace_stable": workspace_stable,
            "workspace_changed": workspace_changed,
            "model": str(outcome.get("model") or selected_model)[:160],
            "turns": int(outcome.get("turns", 0) or 0),
            "tool_calls": int(outcome.get("tool_calls", 0) or 0),
            "modified_paths": list(outcome.get("modified_paths") or []),
            "patch_partial": bool(outcome.get("patch_partial")),
            "patch_rejected": bool(outcome.get("patch_rejected")),
            "finished_at": now_iso(),
        })
        save(session)
        event = {
            **job["event_base"],
            "status": record["status"],
            "turns": record["turns"],
            "tool_calls": record["tool_calls"],
            "modified_paths": [],
            "summary": record["summary"],
            "workspace_stable": workspace_stable,
            "workspace_changed": workspace_changed,
        }
        event_name = "specialist_completed" if record["status"] == "completed" else "specialist_failed"
        ledger.append(event_name, event)
        emit(event_name, event)
        results[job["position"]] = (
            f"协作 Agent（{request['role']}）调查结果（需由主 Agent 核验）：\n{record['summary']}"
        )

    return "\n\n".join(item for item in results if item)
