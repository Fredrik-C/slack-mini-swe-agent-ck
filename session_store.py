from __future__ import annotations

import html
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionStoreConfig:
    max_review_passes: int
    web_ui_max_sessions: int
    status_output_chars: int


class SessionStore:
    def __init__(self, config: SessionStoreConfig) -> None:
        self._config = config
        self._pending_plans: dict[str, dict[str, Any]] = {}
        self._pending_plans_lock = threading.Lock()
        self._runtime_state_lock = threading.Lock()
        self._runtime_state: dict[str, Any] = {
            "running": False,
            "repo_alias": "",
            "branch": "",
            "stage": "idle",
            "stage_since": 0.0,
            "worktree": "",
            "command": "",
            "last_update": 0.0,
            "last_status": "idle",
            "last_error": "",
            "last_stdout": "",
            "last_stderr": "",
            "ck_checked": False,
            "ck_used": False,
            "ck_stages": [],
            "ck_examples": [],
            "live_output": [],
        }
        self._sessions_lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def set_runtime_state(self, **kwargs: Any) -> None:
        with self._runtime_state_lock:
            self._runtime_state.update(kwargs)
            self._runtime_state["last_update"] = time.time()

    def snapshot_runtime_state(self) -> dict[str, Any]:
        with self._runtime_state_lock:
            return dict(self._runtime_state)

    def append_runtime_output_line(self, line: str, max_lines: int = 400) -> None:
        text = str(line).rstrip()
        if not text:
            return
        with self._runtime_state_lock:
            current = self._runtime_state.get("live_output", [])
            if not isinstance(current, list):
                current = []
            current.append(text)
            if len(current) > max_lines:
                current = current[-max_lines:]
            self._runtime_state["live_output"] = current
            self._runtime_state["last_update"] = time.time()

    def init_session(self, job: dict[str, Any]) -> None:
        session_id = str(job.get("session_id", "")).strip()
        if not session_id:
            return

        now = self._now_unix()
        with self._sessions_lock:
            self._sessions[session_id] = {
                "session_id": session_id,
                "conversation_id": str(job.get("conversation_id", "")),
                "thread_ts": str(job.get("thread_ts", "")),
                "channel": str(job.get("channel", "")),
                "repo_alias": str(job.get("repo_alias", "")),
                "branch": str(job.get("branch", "")),
                "task": str(job.get("task", "")),
                "state": "queued",
                "stage": "queued",
                "status": "queued",
                "review_pass": 0,
                "max_review_passes": self._config.max_review_passes,
                "created_at": now,
                "updated_at": now,
                "worktree": "",
                "command": "",
                "last_error": "",
                "last_stdout": "",
                "last_stderr": "",
                "ck_checked": False,
                "ck_used": False,
                "ck_stages": [],
                "ck_examples": [],
            }

            if len(self._sessions) > self._config.web_ui_max_sessions:
                completed = [
                    (sid, record.get("updated_at", 0.0))
                    for sid, record in self._sessions.items()
                    if str(record.get("state", "")) in {"completed", "failed", "timeout", "canceled"}
                ]
                completed.sort(key=lambda item: item[1])
                for sid, _ in completed:
                    if len(self._sessions) <= self._config.web_ui_max_sessions:
                        break
                    self._sessions.pop(sid, None)

    def update_session(self, session_id: str, **kwargs: Any) -> None:
        if not session_id:
            return
        with self._sessions_lock:
            if session_id not in self._sessions:
                return
            self._sessions[session_id].update(kwargs)
            self._sessions[session_id]["updated_at"] = self._now_unix()

    def snapshot_sessions(self) -> list[dict[str, Any]]:
        with self._sessions_lock:
            rows = [dict(record) for record in self._sessions.values()]
        rows.sort(key=lambda row: float(row.get("created_at", 0.0) or 0.0), reverse=True)
        return rows

    def queue_pending_plan(
        self,
        *,
        conversation_id: str,
        session_id: str,
        channel: str,
        thread_ts: str,
        task: str,
        repo_alias: str,
        repo_path: str,
        branch: str,
        answers: list[str],
        questions: list[str],
    ) -> None:
        with self._pending_plans_lock:
            self._pending_plans[conversation_id] = {
                "session_id": session_id,
                "channel": channel,
                "thread_ts": thread_ts,
                "task": task,
                "repo_alias": repo_alias,
                "repo_path": repo_path,
                "branch": branch,
                "answers": list(answers),
                "questions": list(questions),
            }

    def pop_pending_plan(self, conversation_id: str) -> dict[str, Any] | None:
        with self._pending_plans_lock:
            return self._pending_plans.pop(conversation_id, None)

    def peek_pending_plan(self, conversation_id: str) -> dict[str, Any] | None:
        with self._pending_plans_lock:
            return self._pending_plans.get(conversation_id)

    def status_message(self, *, queue_depth: int) -> str:
        state = self.snapshot_runtime_state()
        pending = self.pending_count
        running = bool(state.get("running"))
        stage = str(state.get("stage", "unknown"))
        last_status = str(state.get("last_status", "unknown"))
        repo_alias = str(state.get("repo_alias", ""))
        branch = str(state.get("branch", ""))
        worktree = str(state.get("worktree", ""))
        command = str(state.get("command", ""))
        ck_checked = bool(state.get("ck_checked", False))
        ck_used = bool(state.get("ck_used", False))
        ck_stages_raw = state.get("ck_stages", [])
        ck_examples_raw = state.get("ck_examples", [])
        live_output_raw = state.get("live_output", [])
        last_error = self._tail(str(state.get("last_error", "")))
        last_stdout = self._tail(str(state.get("last_stdout", "")))
        last_stderr = self._tail(str(state.get("last_stderr", "")))
        stage_since = float(state.get("stage_since", 0.0) or 0.0)
        elapsed = int(max(0, time.time() - stage_since)) if stage_since else 0
        ck_stages = [str(item).strip() for item in ck_stages_raw if str(item).strip()] if isinstance(ck_stages_raw, list) else []
        ck_examples = [str(item).strip() for item in ck_examples_raw if str(item).strip()] if isinstance(ck_examples_raw, list) else []
        live_output = [str(item) for item in live_output_raw if str(item).strip()] if isinstance(live_output_raw, list) else []

        lines = [
            f"running=`{running}` stage=`{stage}` elapsed=`{elapsed}s`",
            f"repo=`{repo_alias or '(none)'}` branch=`{branch or '(none)'}`",
            f"queue=`{queue_depth}` pending_clarifications=`{pending}` last_status=`{last_status}`",
        ]
        if worktree:
            lines.append(f"worktree=`{worktree}`")
        if command:
            lines.append(f"command={command}")
        if ck_checked:
            stage_text = ",".join(ck_stages) if ck_stages else "(none)"
            lines.append(f"ck_used=`{ck_used}` ck_stages=`{stage_text}`")
            if ck_examples:
                sample = "\n".join(ck_examples[:3])
                lines.append(f"ck_examples:\n```{sample}```")
        else:
            lines.append("ck_used=`unknown` (no completed stage telemetry yet)")
        if last_error:
            lines.append(f"last_error:\n```{last_error}```")
        if last_stdout:
            lines.append(f"last_stdout_tail:\n```{last_stdout}```")
        if last_stderr:
            lines.append(f"last_stderr_tail:\n```{last_stderr}```")
        if live_output:
            lines.append(f"live_output_tail:\n```{self._tail_lines(live_output, 25)}```")
        return "\n".join(lines)

    def sessions_payload(self, *, queue_depth: int) -> dict[str, Any]:
        return {
            "runtime": self.snapshot_runtime_state(),
            "queue_depth": queue_depth,
            "pending_clarifications": self.pending_count,
            "sessions": self.snapshot_sessions(),
            "generated_at": self._now_unix(),
        }

    def render_sessions_html(self, *, queue_depth: int) -> str:
        payload = self.sessions_payload(queue_depth=queue_depth)
        runtime = payload["runtime"]
        rows = payload["sessions"]
        generated = self._fmt_timestamp(float(payload["generated_at"]))
        runtime_stage = html.escape(str(runtime.get("stage", "")))
        runtime_status = html.escape(str(runtime.get("last_status", "")))
        pending = int(payload["pending_clarifications"])
        runtime_stage_since = float(runtime.get("stage_since", 0.0) or 0.0)
        runtime_elapsed = int(max(0, time.time() - runtime_stage_since)) if runtime_stage_since else 0
        runtime_repo = html.escape(str(runtime.get("repo_alias", "")) or "(none)")
        runtime_branch = html.escape(str(runtime.get("branch", "")) or "(none)")
        runtime_worktree = html.escape(str(runtime.get("worktree", "")) or "(none)")
        runtime_command = html.escape(str(runtime.get("command", "")) or "(none)")
        runtime_ck_checked = bool(runtime.get("ck_checked", False))
        runtime_ck_used = bool(runtime.get("ck_used", False))
        runtime_ck_stages_raw = runtime.get("ck_stages", [])
        runtime_ck_examples_raw = runtime.get("ck_examples", [])
        runtime_live_output_raw = runtime.get("live_output", [])
        runtime_ck_stages = (
            ", ".join(str(item).strip() for item in runtime_ck_stages_raw if str(item).strip())
            if isinstance(runtime_ck_stages_raw, list)
            else ""
        ) or "(none)"
        runtime_ck_examples = (
            "\n".join(str(item).strip() for item in runtime_ck_examples_raw if str(item).strip())
            if isinstance(runtime_ck_examples_raw, list)
            else ""
        )
        runtime_error = html.escape(self._tail(str(runtime.get("last_error", ""))))
        runtime_stdout = html.escape(self._tail(str(runtime.get("last_stdout", ""))))
        runtime_stderr = html.escape(self._tail(str(runtime.get("last_stderr", ""))))
        runtime_live_output = (
            "\n".join(str(item) for item in runtime_live_output_raw if str(item).strip())
            if isinstance(runtime_live_output_raw, list)
            else ""
        )

        table_rows: list[str] = []
        for row in rows:
            created = self._fmt_timestamp(float(row.get("created_at", 0.0) or 0.0))
            updated = self._fmt_timestamp(float(row.get("updated_at", 0.0) or 0.0))
            state_raw = str(row.get("state", ""))
            stage_raw = str(row.get("stage", ""))
            status_raw = str(row.get("status", ""))
            state = html.escape(state_raw)
            stage = html.escape(stage_raw)
            status = html.escape(status_raw)
            session_id = html.escape(str(row.get("session_id", "")))
            repo_alias_raw = str(row.get("repo_alias", ""))
            branch_raw = str(row.get("branch", ""))
            repo_alias = html.escape(repo_alias_raw)
            branch = html.escape(branch_raw)
            review_pass = int(row.get("review_pass", 0) or 0)
            max_review_passes = int(row.get("max_review_passes", 0) or 0)
            task_raw = str(row.get("task", ""))
            task_text = html.escape(task_raw)
            task_short = task_text if len(task_text) <= 220 else f"{task_text[:220]}..."
            error_raw = str(row.get("last_error", ""))
            error_text = html.escape(error_raw)
            command_raw = str(row.get("command", ""))
            command_text = html.escape(command_raw)
            worktree_raw = str(row.get("worktree", ""))
            worktree_text = html.escape(worktree_raw)
            stdout_text = html.escape(self._tail(str(row.get("last_stdout", ""))))
            stderr_text = html.escape(self._tail(str(row.get("last_stderr", ""))))
            ck_checked = bool(row.get("ck_checked", False))
            ck_used = bool(row.get("ck_used", False))
            ck_stages_raw = row.get("ck_stages", [])
            ck_examples_raw = row.get("ck_examples", [])
            ck_stages_text = (
                ", ".join(str(item).strip() for item in ck_stages_raw if str(item).strip())
                if isinstance(ck_stages_raw, list)
                else ""
            ) or "(none)"
            ck_examples_text = (
                "\n".join(str(item).strip() for item in ck_examples_raw if str(item).strip())
                if isinstance(ck_examples_raw, list)
                else ""
            )

            detail_lines: list[str] = [
                f"session_id={session_id}",
                f"state={state_raw} stage={stage_raw} status={status_raw}",
                f"review={review_pass}/{max_review_passes}",
                f"repo={repo_alias_raw} branch={branch_raw}",
                f"created={created} updated={updated}",
            ]
            if worktree_raw:
                detail_lines.append(f"worktree={worktree_raw}")
            if command_raw:
                detail_lines.append(f"command={command_raw}")
            if ck_checked:
                detail_lines.append(f"ck_used={ck_used} ck_stages={ck_stages_text}")
                if ck_examples_text:
                    detail_lines.append("ck_examples:")
                    detail_lines.append(ck_examples_text)
            else:
                detail_lines.append("ck_used=unknown")
            if error_raw:
                detail_lines.append("last_error:")
                detail_lines.append(error_raw)
            if stdout_text:
                detail_lines.append("last_stdout_tail:")
                detail_lines.append(html.unescape(stdout_text))
            if stderr_text:
                detail_lines.append("last_stderr_tail:")
                detail_lines.append(html.unescape(stderr_text))
            detail_lines.append("task:")
            detail_lines.append(task_raw)
            details_body = html.escape("\n".join(detail_lines))

            table_rows.append(
                (
                    "<tr>"
                    f"<td>{session_id}</td>"
                    f"<td>{state}</td>"
                    f"<td>{stage}</td>"
                    f"<td>{status}</td>"
                    f"<td>{review_pass}/{max_review_passes}</td>"
                    f"<td>{repo_alias}</td>"
                    f"<td>{branch}</td>"
                    f"<td title=\"{task_text}\">{task_short}</td>"
                    f"<td>{created}</td>"
                    f"<td>{updated}</td>"
                    f"<td>{error_text}</td>"
                    f"<td><details><summary>View</summary><pre>{details_body}</pre></details></td>"
                    "</tr>"
                )
            )

        rows_html = "\n".join(table_rows) if table_rows else "<tr><td colspan='12'>No sessions yet.</td></tr>"
        runtime_details_lines = [
            f"running={bool(runtime.get('running', False))}",
            f"stage={html.unescape(runtime_stage)} elapsed={runtime_elapsed}s status={html.unescape(runtime_status)}",
            f"repo={html.unescape(runtime_repo)} branch={html.unescape(runtime_branch)}",
            f"worktree={html.unescape(runtime_worktree)}",
            f"command={html.unescape(runtime_command)}",
        ]
        if runtime_ck_checked:
            runtime_details_lines.append(f"ck_used={runtime_ck_used} ck_stages={runtime_ck_stages}")
            if runtime_ck_examples:
                runtime_details_lines.append("ck_examples:")
                runtime_details_lines.append(runtime_ck_examples)
        else:
            runtime_details_lines.append("ck_used=unknown")
        if runtime_error:
            runtime_details_lines.append("last_error:")
            runtime_details_lines.append(html.unescape(runtime_error))
        if runtime_stdout:
            runtime_details_lines.append("last_stdout_tail:")
            runtime_details_lines.append(html.unescape(runtime_stdout))
        if runtime_stderr:
            runtime_details_lines.append("last_stderr_tail:")
            runtime_details_lines.append(html.unescape(runtime_stderr))
        if runtime_live_output:
            runtime_details_lines.append("live_output_tail:")
            runtime_details_lines.append(self._tail_lines(runtime_live_output.splitlines(), 80))
        runtime_details = html.escape("\n".join(runtime_details_lines))
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta http-equiv="refresh" content="5" />
  <title>mini-swe-agent sessions</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3f5f7;
      --card: #ffffff;
      --text: #16212b;
      --muted: #5f6b7a;
      --line: #d8dee6;
      --accent: #145ea8;
    }}
    body {{
      margin: 0;
      padding: 18px;
      font-family: "Segoe UI", Tahoma, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .summary {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      margin-bottom: 14px;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 6px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      padding: 8px;
      font-size: 13px;
    }}
    th {{
      background: #eef2f6;
      color: #27394f;
      font-weight: 600;
      position: sticky;
      top: 0;
    }}
    tr:last-child td {{
      border-bottom: 0;
    }}
    .title {{
      margin: 0 0 10px 0;
      color: var(--accent);
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: Consolas, Monaco, Menlo, monospace;
      font-size: 12px;
      line-height: 1.35;
    }}
    details summary {{
      cursor: pointer;
      color: var(--accent);
      font-weight: 600;
    }}
    .runtime-detail {{
      margin-top: 10px;
      background: #f7f9fc;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
    }}
  </style>
</head>
<body>
  <h2 class="title">mini-swe-agent Sessions</h2>
  <div class="summary">
    <div>runtime_stage=<strong>{runtime_stage}</strong> runtime_status=<strong>{runtime_status}</strong></div>
    <div>queue_depth=<strong>{queue_depth}</strong> pending_clarifications=<strong>{pending}</strong> sessions=<strong>{len(rows)}</strong></div>
    <div class="runtime-detail"><pre>{runtime_details}</pre></div>
    <div class="meta">updated {generated} | <a href="/sessions.json">sessions.json</a></div>
  </div>
  <table>
    <thead>
      <tr>
        <th>Session</th>
        <th>State</th>
        <th>Stage</th>
        <th>Status</th>
        <th>Review</th>
        <th>Repo</th>
        <th>Branch</th>
        <th>Task</th>
        <th>Created</th>
        <th>Updated</th>
        <th>Error</th>
        <th>Details</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</body>
</html>"""

    @property
    def pending_count(self) -> int:
        with self._pending_plans_lock:
            return len(self._pending_plans)

    @staticmethod
    def _now_unix() -> float:
        return time.time()

    @staticmethod
    def _fmt_timestamp(ts: float) -> str:
        if not ts:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def _tail(self, text: str) -> str:
        max_chars = self._config.status_output_chars
        if not text:
            return ""
        return text[-max_chars:] if len(text) > max_chars else text

    @staticmethod
    def _tail_lines(lines: list[str], max_lines: int) -> str:
        if not lines:
            return ""
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(lines[-max_lines:])
