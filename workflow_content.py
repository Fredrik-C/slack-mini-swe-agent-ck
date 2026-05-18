from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkflowPromptConfig:
    plan_output_filename: str
    review_output_filename: str


def read_required_text(path: str, label: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        raise RuntimeError(f"{label} file not found: {p}")
    content = p.read_text(encoding="utf-8").strip()
    if not content:
        raise RuntimeError(f"{label} file is empty: {p}")
    return content


def read_optional_text(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()


def load_workflow_docs(
    *,
    plan_guide_path: str,
    review_guide_path: str,
    workflow_guide_path: str,
    tooling_guide_path: str,
) -> dict[str, str]:
    return {
        "planning": read_required_text(plan_guide_path, "Planning guide"),
        "review": read_required_text(review_guide_path, "Review guide"),
        "workflow": read_required_text(workflow_guide_path, "Workflow guide"),
        "tooling": read_optional_text(tooling_guide_path),
    }


def _answers_block(answers: list[str]) -> str:
    if not answers:
        return "(none)"
    lines = []
    for idx, answer in enumerate(answers, start=1):
        lines.append(f"{idx}. {answer}")
    return "\n".join(lines)


def _guidance_block(title: str, body: str) -> str:
    content = body.strip()
    if not content:
        return ""
    return f"\n{title}:\n{content}\n"


class WorkflowPromptBuilder:
    def __init__(self, config: WorkflowPromptConfig) -> None:
        self._config = config

    def build_planning_task(
        self,
        *,
        user_task: str,
        answers: list[str],
        workflow_guide: str,
        planning_guide: str,
        tooling_guide: str,
    ) -> str:
        return f"""
You are executing the planning phase only.

Primary workflow requirements:
{workflow_guide}

Planning guidance:
{planning_guide}
{_guidance_block("Runtime and tool usage guidance", tooling_guide)}

Original user task:
{user_task}

Clarifications provided by user so far:
{_answers_block(answers)}

Hard requirements for this phase:
1. Do not change source code or tests.
2. Create exactly one JSON file named `{self._config.plan_output_filename}` in the current working directory.
3. The JSON must match this schema:
   {{
     "status": "needs_input" | "ready",
     "plan": "string",
     "questions": ["string", ...],
     "assumptions": ["string", ...]
   }}
4. If you need user input before implementation, set `"status": "needs_input"` and provide 1-3 concrete questions in `"questions"`.
5. If planning is complete, set `"status": "ready"` and keep `"questions"` as an empty array.
6. You must invoke Context King commands during planning (for example: `ck get-keyword-map`, `ck find-files`, `ck recall`).
7. Validate JSON syntax (`python -m json.tool {self._config.plan_output_filename}`) before finishing.
8. End with: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
""".strip()

    def build_implementation_task(
        self,
        *,
        user_task: str,
        answers: list[str],
        workflow_guide: str,
        tooling_guide: str,
        plan_text: str,
        review_feedback: list[str],
    ) -> str:
        feedback_block = "\n".join(f"- {item}" for item in review_feedback) if review_feedback else "- (none)"
        return f"""
Follow this required workflow exactly:
{workflow_guide}
{_guidance_block("Runtime and tool usage guidance", tooling_guide)}

You are in the implementation phase only.

Plan approved for execution:
{plan_text}

Original user task:
{user_task}

User clarifications:
{_answers_block(answers)}

Findings from the latest review pass to address:
{feedback_block}

Implementation requirements:
1. Modify code to satisfy the plan and review feedback.
2. Do not run full test suite yet.
3. Do not create a pull request yet.
4. End with: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
""".strip()

    def build_review_task(
        self,
        *,
        user_task: str,
        answers: list[str],
        workflow_guide: str,
        review_guide: str,
        tooling_guide: str,
        plan_text: str,
    ) -> str:
        return f"""
Follow this required workflow exactly:
{workflow_guide}

Use this review guidance:
{review_guide}
{_guidance_block("Runtime and tool usage guidance", tooling_guide)}

You are in the review phase only.

Plan approved for execution:
{plan_text}

Original user task:
{user_task}

User clarifications:
{_answers_block(answers)}

Hard requirements for this phase:
1. Do not change source code or tests.
2. Review the git diff in the current worktree and determine if implementation changes are needed.
3. Create exactly one JSON file named `{self._config.review_output_filename}` in the current working directory.
4. The JSON must match this schema:
   {{
     "status": "needs_changes" | "approved",
     "issues": ["string", ...]
   }}
5. If changes are needed, set `"status": "needs_changes"` and provide concrete issues to fix.
6. If implementation is acceptable, set `"status": "approved"` and keep `"issues"` as an empty array.
7. Validate JSON syntax (`python -m json.tool {self._config.review_output_filename}`) before finishing.
8. End with: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
""".strip()

    def build_test_pr_task(
        self,
        *,
        user_task: str,
        answers: list[str],
        workflow_guide: str,
        review_guide: str,
        tooling_guide: str,
        plan_text: str,
    ) -> str:
        return f"""
Follow this required workflow exactly:
{workflow_guide}

Use this review guidance:
{review_guide}
{_guidance_block("Runtime and tool usage guidance", tooling_guide)}

Plan approved for execution:
{plan_text}

Original user task:
{user_task}

User clarifications:
{_answers_block(answers)}

Execution requirements for this phase:
1. Do not make broad feature changes unless required to fix failing tests or review-identified defects.
2. Run relevant tests/verification commands.
3. Create a PR if tooling/auth allows it; if blocked, clearly report the blocker and exact command/output.
4. End with: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
""".strip()


def load_plan_output(*, worktree_path: str, plan_output_filename: str) -> dict[str, Any]:
    path = Path(worktree_path) / plan_output_filename
    if not path.exists():
        raise RuntimeError(f"Planning output file was not created: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Planning output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Planning output must be a JSON object.")

    status = str(data.get("status", "")).strip().lower()
    if status not in {"needs_input", "ready"}:
        raise RuntimeError("Planning output status must be 'needs_input' or 'ready'.")

    questions = data.get("questions", [])
    if not isinstance(questions, list) or any(not isinstance(q, str) for q in questions):
        raise RuntimeError("Planning output 'questions' must be an array of strings.")

    plan = str(data.get("plan", "")).strip()
    if not plan:
        raise RuntimeError("Planning output must include non-empty 'plan'.")

    assumptions = data.get("assumptions", [])
    if not isinstance(assumptions, list) or any(not isinstance(a, str) for a in assumptions):
        raise RuntimeError("Planning output 'assumptions' must be an array of strings.")

    return {
        "status": status,
        "plan": plan,
        "questions": [q.strip() for q in questions if q.strip()],
        "assumptions": [a.strip() for a in assumptions if a.strip()],
    }


def load_review_output(*, worktree_path: str, review_output_filename: str) -> dict[str, Any]:
    path = Path(worktree_path) / review_output_filename
    if not path.exists():
        raise RuntimeError(f"Review output file was not created: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Review output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Review output must be a JSON object.")

    status = str(data.get("status", "")).strip().lower()
    if status not in {"needs_changes", "approved"}:
        raise RuntimeError("Review output status must be 'needs_changes' or 'approved'.")

    issues = data.get("issues", [])
    if not isinstance(issues, list) or any(not isinstance(item, str) for item in issues):
        raise RuntimeError("Review output 'issues' must be an array of strings.")

    normalized_issues = [item.strip() for item in issues if item.strip()]
    if status == "needs_changes" and not normalized_issues:
        raise RuntimeError("Review marked needs_changes but did not provide issues.")

    return {"status": status, "issues": normalized_issues}
