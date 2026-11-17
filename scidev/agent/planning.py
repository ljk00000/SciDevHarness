"""Validated, serializable task plans shared by the agent and desktop UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


PLANNING_MODES = ("auto", "required", "off")
STEP_STATUSES = ("pending", "in_progress", "completed", "blocked", "skipped")

PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "Create or update the visible plan for this coding task. Provide the full step list; "
            "keep unfinished work visible, use blocked for obstacles, and use skipped only with "
            "an explanation. Mark a step completed only after the work and relevant checks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "explanation": {"type": "string", "maxLength": 2000},
                "plan": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": {"type": "string", "minLength": 1, "maxLength": 240},
                            "status": {"type": "string", "enum": list(STEP_STATUSES)},
                        },
                        "required": ["step", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["plan"],
            "additionalProperties": False,
        },
    },
}


def planning_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in PLANNING_MODES:
        raise ValueError("规划模式必须是 auto、required 或 off")
    return value


@dataclass(frozen=True)
class PlanStep:
    step: str
    status: str


@dataclass(frozen=True)
class TaskPlan:
    steps: tuple[PlanStep, ...]
    explanation: str = ""
    revision: int = 1
    updated_at: str = ""

    @staticmethod
    def _validate_steps(raw: Any) -> tuple[PlanStep, ...]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
            raise ValueError("计划必须包含 1 到 12 个步骤")
        steps: list[PlanStep] = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"step", "status"}:
                raise ValueError("每个步骤必须且只能包含 step 和 status")
            text, status = item["step"], item["status"]
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 240:
                raise ValueError("步骤名称必须为 1 到 240 个字符")
            if not isinstance(status, str) or status not in STEP_STATUSES:
                raise ValueError(f"未知步骤状态：{status}")
            steps.append(PlanStep(text.strip(), status))
        if len({step.step for step in steps}) != len(steps):
            raise ValueError("步骤名称不能重复")
        if sum(step.status == "in_progress" for step in steps) > 1:
            raise ValueError("同一时刻最多只能有一个进行中的步骤")
        return tuple(steps)

    @classmethod
    def from_record(cls, record: Any) -> TaskPlan | None:
        if record is None:
            return None
        if not isinstance(record, dict):
            raise ValueError("计划记录必须是 JSON 对象")
        steps = cls._validate_steps(record.get("steps"))
        explanation = record.get("explanation", "")
        revision = record.get("revision", 1)
        stamp = record.get("updated_at", "")
        if not isinstance(explanation, str) or len(explanation) > 2000:
            raise ValueError("计划说明最多 2000 个字符")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("无效计划版本")
        if not isinstance(stamp, str) or len(stamp) > 100:
            raise ValueError("无效计划时间")
        return cls(steps, explanation, revision, stamp)

    @classmethod
    def update(cls, previous: TaskPlan | None, plan: Any, explanation: str = "") -> TaskPlan:
        steps = cls._validate_steps(plan)
        if not isinstance(explanation, str) or len(explanation) > 2000:
            raise ValueError("计划说明最多 2000 个字符")
        reason = explanation.strip()
        if previous is not None:
            next_by_name = {step.step: step for step in steps}
            previous_by_name = {step.step: step for step in previous.steps}
            removed = [
                step for step in previous.steps
                if step.step not in next_by_name
            ]
            reopened = [
                step for step in previous.steps
                if step.status in {"completed", "skipped"}
                and step.step in next_by_name
                and next_by_name[step.step].status not in {"completed", "skipped"}
            ]
            newly_skipped = [
                step for step in steps
                if step.status == "skipped"
                and (step.step not in previous_by_name or previous_by_name[step.step].status != "skipped")
            ]
            if (removed or reopened or newly_skipped) and not reason:
                raise ValueError("删除步骤、重开已完成或已跳过步骤、或新跳过步骤时，必须填写 explanation 说明原因")
        elif any(step.status == "skipped" for step in steps) and not reason:
            raise ValueError("初始计划包含跳过步骤时，必须填写 explanation 说明原因")
        return cls(
            steps,
            reason,
            previous.revision + 1 if previous else 1,
            datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    @property
    def finished(self) -> bool:
        return all(step.status in {"completed", "skipped"} for step in self.steps)

    @property
    def progress(self) -> tuple[int, int]:
        return sum(step.status in {"completed", "skipped"} for step in self.steps), len(self.steps)

    def to_record(self) -> dict[str, Any]:
        return {
            "steps": [{"step": step.step, "status": step.status} for step in self.steps],
            "explanation": self.explanation,
            "revision": self.revision,
            "updated_at": self.updated_at,
        }

    def context(self) -> str:
        completed, total = self.progress
        lines = [f"当前任务计划（版本 {self.revision}，已完成 {completed}/{total}）："]
        lines.extend(f"{index}. [{step.status}] {step.step}" for index, step in enumerate(self.steps, 1))
        if self.explanation:
            lines.append(f"调整说明：{self.explanation}")
        return "\n".join(lines)
