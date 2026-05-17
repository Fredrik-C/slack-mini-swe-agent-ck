import os
import queue
import re
import shlex
import subprocess
import threading
import time
import uuid
import json
import fnmatch
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")

if not SLACK_BOT_TOKEN.startswith("xoxb-"):
    raise RuntimeError("SLACK_BOT_TOKEN is missing or invalid. Expected xoxb- token.")
if not SLACK_APP_TOKEN.startswith("xapp-"):
    raise RuntimeError("SLACK_APP_TOKEN is missing or invalid. Expected xapp- token.")

MINI_CMD = os.getenv("MINI_CMD", "mini")
MINI_USE_YOLO = _bool_env("MINI_USE_YOLO", True)
MINI_EXIT_IMMEDIATELY = _bool_env("MINI_EXIT_IMMEDIATELY", True)
MINI_MODEL_CLASS = os.getenv("MINI_MODEL_CLASS", "").strip()
MINI_MODEL_NAME = os.getenv("MINI_MODEL_NAME", "").strip()
MINI_PLAN_MODEL_CLASS = os.getenv("MINI_PLAN_MODEL_CLASS", "").strip()
MINI_PLAN_MODEL_NAME = os.getenv("MINI_PLAN_MODEL_NAME", "").strip()
MINI_IMPLEMENT_MODEL_CLASS = os.getenv("MINI_IMPLEMENT_MODEL_CLASS", "").strip()
MINI_IMPLEMENT_MODEL_NAME = os.getenv("MINI_IMPLEMENT_MODEL_NAME", "").strip()
MINI_REVIEW_MODEL_CLASS = os.getenv("MINI_REVIEW_MODEL_CLASS", "").strip()
MINI_REVIEW_MODEL_NAME = os.getenv("MINI_REVIEW_MODEL_NAME", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "7200"))
MAX_STDOUT_CHARS = int(os.getenv("MAX_STDOUT_CHARS", "3500"))
MAX_STDERR_CHARS = int(os.getenv("MAX_STDERR_CHARS", "1500"))
WORKDIR = os.getenv("WORKDIR", ".")
TASK_PREFIX = os.getenv("TASK_PREFIX", "").strip()
REPO_CONFIG_PATH = os.getenv("REPO_CONFIG_PATH", "repos.json")
WORKTREE_ROOT = os.getenv("WORKTREE_ROOT", ".worktrees")
GIT_FETCH_BEFORE_WORKTREE = _bool_env("GIT_FETCH_BEFORE_WORKTREE", True)
KEEP_WORKTREE_ON_FAILURE = _bool_env("KEEP_WORKTREE_ON_FAILURE", False)
PROGRESS_HEARTBEAT_SECONDS = int(os.getenv("PROGRESS_HEARTBEAT_SECONDS", "0"))
STATUS_OUTPUT_CHARS = int(os.getenv("STATUS_OUTPUT_CHARS", "1200"))
MSWEA_CONFIGURED = os.getenv("MSWEA_CONFIGURED", "true").strip()
PLAN_GUIDE_PATH = os.getenv("PLAN_GUIDE_PATH", "prompts/planning.md")
REVIEW_GUIDE_PATH = os.getenv("REVIEW_GUIDE_PATH", "prompts/review.md")
WORKFLOW_GUIDE_PATH = os.getenv("WORKFLOW_GUIDE_PATH", "prompts/workflow.md")
PLAN_OUTPUT_FILENAME = os.getenv("PLAN_OUTPUT_FILENAME", ".mini_workflow_plan.json")
REVIEW_OUTPUT_FILENAME = os.getenv("REVIEW_OUTPUT_FILENAME", ".mini_workflow_review.json")
MAX_IMPLEMENT_REVIEW_LOOPS = int(os.getenv("MAX_IMPLEMENT_REVIEW_LOOPS", "3"))
WEB_UI_ENABLED = _bool_env("WEB_UI_ENABLED", True)
WEB_UI_BIND = os.getenv("WEB_UI_BIND", "0.0.0.0").strip() or "0.0.0.0"
WEB_UI_PORT = int(os.getenv("WEB_UI_PORT", "8787"))
WEB_UI_MAX_SESSIONS = int(os.getenv("WEB_UI_MAX_SESSIONS", "200"))
ALLOW_CHANNEL_IDS = {
    c.strip() for c in os.getenv("ALLOW_CHANNEL_IDS", "").split(",") if c.strip()
}

model_classes_to_check = [
    MINI_MODEL_CLASS,
    MINI_PLAN_MODEL_CLASS,
    MINI_IMPLEMENT_MODEL_CLASS,
    MINI_REVIEW_MODEL_CLASS,
]
if any(model_class.lower() == "openrouter" for model_class in model_classes_to_check if model_class):
    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is required when any model class is set to openrouter."
        )

app = App(token=SLACK_BOT_TOKEN)
task_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
pending_plans: dict[str, dict[str, Any]] = {}
pending_plans_lock = threading.Lock()
runtime_state_lock = threading.Lock()
runtime_state: dict[str, Any] = {
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
}
sessions_lock = threading.Lock()
sessions: dict[str, dict[str, Any]] = {}


def _run_quiet(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=180)


def _load_repo_config() -> dict[str, Any]:
    path = Path(REPO_CONFIG_PATH)
    if not path.exists():
        raise RuntimeError(
            f"Repo config not found at {path}. Create it from repos.example.json."
        )
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    repos = data.get("repos", {})
    if not isinstance(repos, dict) or not repos:
        raise RuntimeError("Repo config must define a non-empty 'repos' object.")
    return data


REPO_CONFIG = _load_repo_config()


def _read_required_text(path: str, label: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        raise RuntimeError(f"{label} file not found: {p}")
    content = p.read_text(encoding="utf-8").strip()
    if not content:
        raise RuntimeError(f"{label} file is empty: {p}")
    return content


def _load_workflow_docs() -> dict[str, str]:
    return {
        "planning": _read_required_text(PLAN_GUIDE_PATH, "Planning guide"),
        "review": _read_required_text(REVIEW_GUIDE_PATH, "Review guide"),
        "workflow": _read_required_text(WORKFLOW_GUIDE_PATH, "Workflow guide"),
    }


def _extract_mention_text(text: str) -> str:
    return re.sub(r"<@[^>]+>", "", text).strip()


def _thread_key(event: dict[str, Any]) -> str:
    return event.get("thread_ts") or event.get("ts", "")


def _extract_task_payload(text: str) -> str:
    cleaned = re.sub(r"<@[^>]+>", "", text).strip()
    if TASK_PREFIX:
        if not cleaned.lower().startswith(TASK_PREFIX.lower()):
            return ""
        cleaned = cleaned[len(TASK_PREFIX) :].strip()
    return cleaned


def _is_repo_listing_command(payload: str) -> bool:
    normalized = " ".join(payload.lower().split())
    return normalized in {"repos", "list repos", "repo list"}


def _is_help_command(payload: str) -> bool:
    normalized = " ".join(payload.lower().split())
    return normalized in {"help", "usage", "commands"}


def _is_status_command(payload: str) -> bool:
    normalized = " ".join(payload.lower().split())
    return normalized in {"status", "state", "progress", "last output", "output"}


def _repo_listing_message() -> str:
    default_repo = REPO_CONFIG.get("default_repo", "")
    lines = [f"Configured repos (default=`{default_repo}`):"]
    for alias in sorted(REPO_CONFIG["repos"].keys()):
        entry = REPO_CONFIG["repos"][alias]
        default_branch = entry.get("default_branch", "main")
        patterns = entry.get("allowed_branches", [])
        pattern_text = ", ".join(patterns) if patterns else "(any)"
        lines.append(
            f"- `{alias}`: default_branch=`{default_branch}` allowed_branches=`{pattern_text}`"
        )
    return "\n".join(lines)


def _help_message() -> str:
    lines = [
        "Commands:",
        "- `repos` or `list repos`: show allowed repo aliases and branch patterns",
        "- `status`: show current run stage, queue depth, and latest output/error",
        "- `help`: show this message",
        "- During planning clarifications: reply in-thread with `@bot <answers>` or `@bot cancel`",
        "",
        "Task format:",
        "- `repo=<alias> branch=<branch> <task text>`",
        "- `branch=` is optional (uses repo default branch)",
        "- `repo=` is optional (uses `default_repo`)",
    ]
    return "\n".join(lines)


def _set_runtime_state(**kwargs: Any) -> None:
    with runtime_state_lock:
        runtime_state.update(kwargs)
        runtime_state["last_update"] = time.time()


def _snapshot_runtime_state() -> dict[str, Any]:
    with runtime_state_lock:
        return dict(runtime_state)


def _now_unix() -> float:
    return time.time()


def _fmt_timestamp(ts: float) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _init_session(job: dict[str, Any]) -> None:
    session_id = str(job.get("session_id", "")).strip()
    if not session_id:
        return
    now = _now_unix()
    with sessions_lock:
        sessions[session_id] = {
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
            "max_review_passes": MAX_IMPLEMENT_REVIEW_LOOPS,
            "created_at": now,
            "updated_at": now,
            "worktree": "",
            "last_error": "",
            "last_stdout": "",
            "last_stderr": "",
        }
        if len(sessions) > WEB_UI_MAX_SESSIONS:
            # Best effort trim of oldest completed records.
            completed = [
                (sid, record.get("updated_at", 0.0))
                for sid, record in sessions.items()
                if str(record.get("state", "")) in {"completed", "failed", "timeout", "canceled"}
            ]
            completed.sort(key=lambda item: item[1])
            for sid, _ in completed:
                if len(sessions) <= WEB_UI_MAX_SESSIONS:
                    break
                sessions.pop(sid, None)


def _update_session(session_id: str, **kwargs: Any) -> None:
    if not session_id:
        return
    with sessions_lock:
        if session_id not in sessions:
            return
        sessions[session_id].update(kwargs)
        sessions[session_id]["updated_at"] = _now_unix()


def _snapshot_sessions() -> list[dict[str, Any]]:
    with sessions_lock:
        rows = [dict(record) for record in sessions.values()]
    rows.sort(key=lambda row: float(row.get("created_at", 0.0) or 0.0), reverse=True)
    return rows


def _status_message() -> str:
    state = _snapshot_runtime_state()
    queued = task_queue.qsize()
    pending = len(pending_plans)
    running = bool(state.get("running"))
    stage = str(state.get("stage", "unknown"))
    last_status = str(state.get("last_status", "unknown"))
    repo_alias = str(state.get("repo_alias", ""))
    branch = str(state.get("branch", ""))
    worktree = str(state.get("worktree", ""))
    command = str(state.get("command", ""))
    last_error = _tail(str(state.get("last_error", "")), STATUS_OUTPUT_CHARS)
    last_stdout = _tail(str(state.get("last_stdout", "")), STATUS_OUTPUT_CHARS)
    last_stderr = _tail(str(state.get("last_stderr", "")), STATUS_OUTPUT_CHARS)
    stage_since = float(state.get("stage_since", 0.0) or 0.0)
    elapsed = int(max(0, time.time() - stage_since)) if stage_since else 0

    lines = [
        f"running=`{running}` stage=`{stage}` elapsed=`{elapsed}s`",
        f"repo=`{repo_alias or '(none)'}` branch=`{branch or '(none)'}`",
        f"queue=`{queued}` pending_clarifications=`{pending}` last_status=`{last_status}`",
    ]
    if worktree:
        lines.append(f"worktree=`{worktree}`")
    if command:
        lines.append(f"command=`{command}`")
    if last_error:
        lines.append(f"last_error:\n```{last_error}```")
    if last_stdout:
        lines.append(f"last_stdout_tail:\n```{last_stdout}```")
    if last_stderr:
        lines.append(f"last_stderr_tail:\n```{last_stderr}```")
    return "\n".join(lines)


def _sessions_payload() -> dict[str, Any]:
    return {
        "runtime": _snapshot_runtime_state(),
        "queue_depth": task_queue.qsize(),
        "pending_clarifications": len(pending_plans),
        "sessions": _snapshot_sessions(),
        "generated_at": _now_unix(),
    }


def _render_sessions_html() -> str:
    payload = _sessions_payload()
    runtime = payload["runtime"]
    rows = payload["sessions"]
    generated = _fmt_timestamp(float(payload["generated_at"]))
    runtime_stage = html.escape(str(runtime.get("stage", "")))
    runtime_status = html.escape(str(runtime.get("last_status", "")))
    queue_depth = int(payload["queue_depth"])
    pending = int(payload["pending_clarifications"])

    table_rows: list[str] = []
    for row in rows:
        created = _fmt_timestamp(float(row.get("created_at", 0.0) or 0.0))
        updated = _fmt_timestamp(float(row.get("updated_at", 0.0) or 0.0))
        state = html.escape(str(row.get("state", "")))
        stage = html.escape(str(row.get("stage", "")))
        status = html.escape(str(row.get("status", "")))
        session_id = html.escape(str(row.get("session_id", "")))
        repo_alias = html.escape(str(row.get("repo_alias", "")))
        branch = html.escape(str(row.get("branch", "")))
        review_pass = int(row.get("review_pass", 0) or 0)
        max_review_passes = int(row.get("max_review_passes", 0) or 0)
        task_text = html.escape(str(row.get("task", "")))
        task_short = task_text if len(task_text) <= 220 else f"{task_text[:220]}..."
        error_text = html.escape(str(row.get("last_error", "")))
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
                "</tr>"
            )
        )

    rows_html = "\n".join(table_rows) if table_rows else "<tr><td colspan='11'>No sessions yet.</td></tr>"
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
  </style>
</head>
<body>
  <h2 class="title">mini-swe-agent Sessions</h2>
  <div class="summary">
    <div>runtime_stage=<strong>{runtime_stage}</strong> runtime_status=<strong>{runtime_status}</strong></div>
    <div>queue_depth=<strong>{queue_depth}</strong> pending_clarifications=<strong>{pending}</strong> sessions=<strong>{len(rows)}</strong></div>
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
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</body>
</html>"""


class _WebStatusHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/sessions.json":
            payload = _sessions_payload()
            body = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/" or path == "/index.html":
            body = _render_sessions_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Not found")

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def _start_web_ui() -> None:
    if not WEB_UI_ENABLED:
        return
    server = ThreadingHTTPServer((WEB_UI_BIND, WEB_UI_PORT), _WebStatusHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Web UI running on http://{WEB_UI_BIND}:{WEB_UI_PORT}")


def _parse_payload(payload: str) -> dict[str, str]:
    def _clean_selector(value: str) -> str:
        # tolerate Slack punctuation like `repo=atg,`
        return value.strip().strip(",.;:")

    tokens = shlex.split(payload)
    repo = ""
    branch = ""
    task_tokens: list[str] = []
    for token in tokens:
        lower = token.lower()
        if lower.startswith("repo=") and not repo:
            repo = _clean_selector(token.split("=", 1)[1])
            continue
        if lower.startswith("branch=") and not branch:
            branch = _clean_selector(token.split("=", 1)[1])
            continue
        task_tokens.append(token)
    return {"repo": repo, "branch": branch, "task": " ".join(task_tokens).strip()}


def _resolve_repo_and_branch(repo_alias: str, branch: str) -> dict[str, str]:
    repos = REPO_CONFIG["repos"]
    default_repo = REPO_CONFIG.get("default_repo", "")
    selected_alias = repo_alias or default_repo
    if selected_alias not in repos:
        allowed = ", ".join(sorted(repos.keys()))
        raise ValueError(f"Unknown repo alias '{selected_alias}'. Allowed: {allowed}")

    repo_entry = repos[selected_alias]
    repo_path = str(Path(repo_entry["path"]).expanduser().resolve())
    default_branch = repo_entry.get("default_branch", "main")
    selected_branch = branch or default_branch
    allowed_branches = repo_entry.get("allowed_branches", [])
    if allowed_branches:
        if not any(fnmatch.fnmatch(selected_branch, pattern) for pattern in allowed_branches):
            patterns = ", ".join(allowed_branches)
            raise ValueError(
                f"Branch '{selected_branch}' is not allowed for repo '{selected_alias}'. "
                f"Allowed patterns: {patterns}"
            )
    return {
        "repo_alias": selected_alias,
        "repo_path": repo_path,
        "branch": selected_branch,
    }


def _ref_exists(repo_path: str, ref: str) -> bool:
    proc = _run_quiet(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet", ref])
    return proc.returncode == 0


def _prepare_worktree(repo_path: str, branch: str) -> tuple[str, str]:
    if not Path(repo_path).exists():
        raise ValueError(f"Repo path does not exist: {repo_path}")
    if _run_quiet(["git", "-C", repo_path, "rev-parse", "--is-inside-work-tree"]).returncode != 0:
        raise ValueError(f"Path is not a git repository: {repo_path}")

    if GIT_FETCH_BEFORE_WORKTREE:
        fetch_proc = _run_quiet(["git", "-C", repo_path, "fetch", "--all", "--prune"])
        if fetch_proc.returncode != 0:
            details = (fetch_proc.stderr or fetch_proc.stdout).strip() or "no output"
            raise RuntimeError(
                "Failed to fetch repository before worktree creation. "
                "Verify git credentials for private remotes. "
                f"git output: {details}"
            )

    candidates = [branch, f"origin/{branch}"]
    base_ref = ""
    for ref in candidates:
        if _ref_exists(repo_path, ref):
            base_ref = ref
            break
    if not base_ref:
        raise ValueError(f"Cannot find branch/ref '{branch}' in repository: {repo_path}")

    worktree_root = Path(WORKTREE_ROOT).expanduser().resolve()
    worktree_root.mkdir(parents=True, exist_ok=True)
    wt_name = f"{Path(repo_path).name}-{branch.replace('/', '_')}-{uuid.uuid4().hex[:8]}"
    wt_path = str((worktree_root / wt_name).resolve())
    add_proc = _run_quiet(
        ["git", "-C", repo_path, "worktree", "add", "--detach", wt_path, base_ref]
    )
    if add_proc.returncode != 0:
        raise RuntimeError(
            f"Failed to create worktree: {add_proc.stderr.strip() or add_proc.stdout.strip()}"
        )
    return wt_path, base_ref


def _cleanup_worktree(repo_path: str, wt_path: str) -> None:
    _run_quiet(["git", "-C", repo_path, "worktree", "remove", "--force", wt_path])


def _resolve_phase_model(phase: str) -> tuple[str, str]:
    phase_key = phase.strip().lower()
    if phase_key == "plan":
        model_class = MINI_PLAN_MODEL_CLASS or MINI_MODEL_CLASS
        model_name = MINI_PLAN_MODEL_NAME or MINI_MODEL_NAME
        return model_class, model_name
    if phase_key == "implement":
        model_class = MINI_IMPLEMENT_MODEL_CLASS or MINI_MODEL_CLASS
        model_name = MINI_IMPLEMENT_MODEL_NAME or MINI_MODEL_NAME
        return model_class, model_name
    if phase_key == "review":
        model_class = MINI_REVIEW_MODEL_CLASS or MINI_MODEL_CLASS
        model_name = MINI_REVIEW_MODEL_NAME or MINI_MODEL_NAME
        return model_class, model_name
    return MINI_MODEL_CLASS, MINI_MODEL_NAME


def _build_command(task: str, phase: str) -> list[str]:
    cmd = shlex.split(MINI_CMD)
    model_class, model_name = _resolve_phase_model(phase)
    if model_class:
        cmd.extend(["--model-class", model_class])
    if model_name:
        cmd.extend(["-m", model_name])
    if MINI_USE_YOLO:
        cmd.append("-y")
    if MINI_EXIT_IMMEDIATELY:
        cmd.append("--exit-immediately")
    cmd.extend(["-t", task])
    return cmd


def _mini_env() -> dict[str, str]:
    env = os.environ.copy()
    env["MSWEA_CONFIGURED"] = MSWEA_CONFIGURED or "true"
    return env


def _run_with_heartbeat(
    cmd: list[str],
    cwd: str,
    env: dict[str, str],
    timeout_seconds: int,
    channel: str,
    thread_ts: str,
    stage_label: str,
) -> subprocess.CompletedProcess[str]:
    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["proc"] = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    start = time.time()
    thread.start()

    if PROGRESS_HEARTBEAT_SECONDS > 0:
        while thread.is_alive():
            thread.join(timeout=PROGRESS_HEARTBEAT_SECONDS)
            if thread.is_alive():
                elapsed = int(time.time() - start)
                _post_thread(
                    channel,
                    thread_ts,
                    f"Stage: {stage_label} (still running, {elapsed}s elapsed)",
                )
    else:
        thread.join()

    if "error" in result:
        raise result["error"]
    return result["proc"]


def _answers_block(answers: list[str]) -> str:
    if not answers:
        return "(none)"
    lines = []
    for idx, answer in enumerate(answers, start=1):
        lines.append(f"{idx}. {answer}")
    return "\n".join(lines)


def _build_planning_task(
    user_task: str,
    answers: list[str],
    workflow_guide: str,
    planning_guide: str,
) -> str:
    return f"""
You are executing the planning phase only.

Primary workflow requirements:
{workflow_guide}

Planning guidance:
{planning_guide}

Original user task:
{user_task}

Clarifications provided by user so far:
{_answers_block(answers)}

Hard requirements for this phase:
1. Do not change source code or tests.
2. Create exactly one JSON file named `{PLAN_OUTPUT_FILENAME}` in the current working directory.
3. The JSON must match this schema:
   {{
     "status": "needs_input" | "ready",
     "plan": "string",
     "questions": ["string", ...],
     "assumptions": ["string", ...]
   }}
4. If you need user input before implementation, set `"status": "needs_input"` and provide 1-3 concrete questions in `"questions"`.
5. If planning is complete, set `"status": "ready"` and keep `"questions"` as an empty array.
6. Validate JSON syntax (`python -m json.tool {PLAN_OUTPUT_FILENAME}`) before finishing.
7. End with: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
""".strip()


def _build_implementation_task(
    user_task: str,
    answers: list[str],
    workflow_guide: str,
    plan_text: str,
    review_feedback: list[str],
) -> str:
    feedback_block = "\n".join(f"- {item}" for item in review_feedback) if review_feedback else "- (none)"
    return f"""
Follow this required workflow exactly:
{workflow_guide}

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


def _build_review_task(
    user_task: str,
    answers: list[str],
    workflow_guide: str,
    review_guide: str,
    plan_text: str,
) -> str:
    return f"""
Follow this required workflow exactly:
{workflow_guide}

Use this review guidance:
{review_guide}

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
3. Create exactly one JSON file named `{REVIEW_OUTPUT_FILENAME}` in the current working directory.
4. The JSON must match this schema:
   {{
     "status": "needs_changes" | "approved",
     "issues": ["string", ...]
   }}
5. If changes are needed, set `"status": "needs_changes"` and provide concrete issues to fix.
6. If implementation is acceptable, set `"status": "approved"` and keep `"issues"` as an empty array.
7. Validate JSON syntax (`python -m json.tool {REVIEW_OUTPUT_FILENAME}`) before finishing.
8. End with: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
""".strip()


def _build_test_pr_task(
    user_task: str,
    answers: list[str],
    workflow_guide: str,
    review_guide: str,
    plan_text: str,
) -> str:
    return f"""
Follow this required workflow exactly:
{workflow_guide}

Use this review guidance:
{review_guide}

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
5. End with: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
""".strip()


def _load_plan_output(worktree_path: str) -> dict[str, Any]:
    path = Path(worktree_path) / PLAN_OUTPUT_FILENAME
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


def _load_review_output(worktree_path: str) -> dict[str, Any]:
    path = Path(worktree_path) / REVIEW_OUTPUT_FILENAME
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


def _tail(text: str, max_chars: int) -> str:
    if not text:
        return ""
    return text[-max_chars:] if len(text) > max_chars else text


def _post_thread(channel: str, thread_ts: str, text: str) -> None:
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def _queue_pending_plan(
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
    with pending_plans_lock:
        pending_plans[conversation_id] = {
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


def _pop_pending_plan(conversation_id: str) -> dict[str, Any] | None:
    with pending_plans_lock:
        return pending_plans.pop(conversation_id, None)


def _peek_pending_plan(conversation_id: str) -> dict[str, Any] | None:
    with pending_plans_lock:
        return pending_plans.get(conversation_id)


def _run_workflow(job: dict[str, Any]) -> tuple[bool, bool, str]:
    channel = job["channel"]
    thread_ts = job["thread_ts"]
    conversation_id = job["conversation_id"]
    session_id = str(job.get("session_id", "")).strip()
    task = job["task"]
    repo_alias = job["repo_alias"]
    repo_path = job["repo_path"]
    branch = job["branch"]
    answers: list[str] = list(job.get("answers", []))
    latest_reply = str(job.get("latest_reply", "")).strip()
    if latest_reply:
        answers.append(latest_reply)

    workflow_docs = _load_workflow_docs()
    wt_path = ""
    keep_worktree = False
    review_feedback: list[str] = []
    review_pass = 0
    try:
        _set_runtime_state(
            running=True,
            repo_alias=repo_alias,
            branch=branch,
            stage="preparing-worktree",
            stage_since=time.time(),
            worktree="",
            command="",
            last_status="running",
            last_error="",
            last_stdout="",
            last_stderr="",
        )
        _update_session(
            session_id,
            state="running",
            stage="preparing-worktree",
            status="running",
            review_pass=review_pass,
            last_error="",
            last_stdout="",
            last_stderr="",
        )
        wt_path, base_ref = _prepare_worktree(repo_path, branch)
        _set_runtime_state(worktree=wt_path)
        _update_session(session_id, worktree=wt_path)
        _post_thread(
            channel,
            thread_ts,
            (
                f"Workflow started.\n"
                f"session=`{session_id}`\n"
                f"repo=`{repo_alias}` branch=`{branch}` ref=`{base_ref}`\n"
                f"worktree=`{wt_path}`\n"
                "Stage: planning"
            ),
        )

        planning_task = _build_planning_task(
            user_task=task,
            answers=answers,
            workflow_guide=workflow_docs["workflow"],
            planning_guide=workflow_docs["planning"],
        )
        planning_cmd = _build_command(planning_task, phase="plan")
        _set_runtime_state(
            stage="planning",
            stage_since=time.time(),
            command=shlex.join(planning_cmd),
        )
        _update_session(
            session_id,
            stage="planning",
            status="running",
            command=shlex.join(planning_cmd),
        )
        _post_thread(
            channel,
            thread_ts,
            f"Planning command: `{shlex.join(planning_cmd)}`",
        )
        planning_proc = _run_with_heartbeat(
            cmd=planning_cmd,
            cwd=wt_path,
            env=_mini_env(),
            timeout_seconds=TASK_TIMEOUT_SECONDS,
            channel=channel,
            thread_ts=thread_ts,
            stage_label="planning",
        )
        if planning_proc.returncode != 0:
            stdout = _tail(planning_proc.stdout, MAX_STDOUT_CHARS)
            stderr = _tail(planning_proc.stderr, MAX_STDERR_CHARS)
            _set_runtime_state(
                running=False,
                stage="planning-failed",
                stage_since=time.time(),
                last_status=f"failed (planning exit {planning_proc.returncode})",
                last_error=f"Planning stage failed (exit {planning_proc.returncode}).",
                last_stdout=stdout,
                last_stderr=stderr,
            )
            _update_session(
                session_id,
                state="failed",
                stage="planning-failed",
                status=f"failed (planning exit {planning_proc.returncode})",
                last_error=f"Planning stage failed (exit {planning_proc.returncode}).",
                last_stdout=stdout,
                last_stderr=stderr,
            )
            message = f"Planning stage failed (exit {planning_proc.returncode})."
            if stdout:
                message += f"\n\nstdout:\n```{stdout}```"
            if stderr:
                message += f"\n\nstderr:\n```{stderr}```"
            _post_thread(channel, thread_ts, message)
            return False, False, wt_path

        plan_output = _load_plan_output(wt_path)
        if plan_output["status"] == "needs_input":
            questions = plan_output["questions"]
            if not questions:
                raise RuntimeError("Planning requested input but returned no questions.")
            question_lines = "\n".join(
                f"{idx}. {question}" for idx, question in enumerate(questions, start=1)
            )
            _queue_pending_plan(
                conversation_id=conversation_id,
                session_id=session_id,
                channel=channel,
                thread_ts=thread_ts,
                task=task,
                repo_alias=repo_alias,
                repo_path=repo_path,
                branch=branch,
                answers=answers,
                questions=questions,
            )
            _post_thread(
                channel,
                thread_ts,
                (
                    "Planning requires clarification before implementation.\n"
                    "Reply in this thread by mentioning the bot and answering these questions:\n"
                    f"{question_lines}"
                ),
            )
            _set_runtime_state(
                running=False,
                stage="waiting-user-clarification",
                stage_since=time.time(),
                command="",
                last_status="waiting_for_input",
                last_stdout=_tail(planning_proc.stdout, MAX_STDOUT_CHARS),
                last_stderr=_tail(planning_proc.stderr, MAX_STDERR_CHARS),
            )
            _update_session(
                session_id,
                state="waiting_input",
                stage="waiting-user-clarification",
                status="waiting_for_input",
                last_stdout=_tail(planning_proc.stdout, MAX_STDOUT_CHARS),
                last_stderr=_tail(planning_proc.stderr, MAX_STDERR_CHARS),
            )
            return False, True, wt_path

        assumptions = plan_output["assumptions"]
        assumptions_block = (
            "\n".join(f"- {item}" for item in assumptions) if assumptions else "- (none)"
        )
        plan_text = f"{plan_output['plan']}\n\nAssumptions:\n{assumptions_block}"

        _post_thread(channel, thread_ts, "Stage: implement + review loop")
        approved = False
        for review_pass in range(1, MAX_IMPLEMENT_REVIEW_LOOPS + 1):
            _post_thread(
                channel,
                thread_ts,
                f"Loop {review_pass}/{MAX_IMPLEMENT_REVIEW_LOOPS}: implementation pass",
            )
            implementation_task = _build_implementation_task(
                user_task=task,
                answers=answers,
                workflow_guide=workflow_docs["workflow"],
                plan_text=plan_text,
                review_feedback=review_feedback,
            )
            implement_cmd = _build_command(implementation_task, phase="implement")
            _set_runtime_state(
                stage=f"implementation-pass-{review_pass}",
                stage_since=time.time(),
                command=shlex.join(implement_cmd),
                last_status="running",
            )
            _update_session(
                session_id,
                stage=f"implementation-pass-{review_pass}",
                status="running",
                review_pass=review_pass,
                command=shlex.join(implement_cmd),
            )
            _post_thread(
                channel,
                thread_ts,
                f"Implementation command: `{shlex.join(implement_cmd)}`",
            )
            implement_proc = _run_with_heartbeat(
                cmd=implement_cmd,
                cwd=wt_path,
                env=_mini_env(),
                timeout_seconds=TASK_TIMEOUT_SECONDS,
                channel=channel,
                thread_ts=thread_ts,
                stage_label=f"implementation pass {review_pass}",
            )
            implement_stdout = _tail(implement_proc.stdout, MAX_STDOUT_CHARS)
            implement_stderr = _tail(implement_proc.stderr, MAX_STDERR_CHARS)
            if implement_proc.returncode != 0:
                _set_runtime_state(
                    running=False,
                    stage="implementation-failed",
                    stage_since=time.time(),
                    command="",
                    last_status=f"failed (implement exit {implement_proc.returncode})",
                    last_error=f"Implementation stage failed (exit {implement_proc.returncode}).",
                    last_stdout=implement_stdout,
                    last_stderr=implement_stderr,
                )
                _update_session(
                    session_id,
                    state="failed",
                    stage="implementation-failed",
                    status=f"failed (implement exit {implement_proc.returncode})",
                    last_error=f"Implementation stage failed (exit {implement_proc.returncode}).",
                    last_stdout=implement_stdout,
                    last_stderr=implement_stderr,
                    review_pass=review_pass,
                )
                message = f"Implementation stage failed (exit {implement_proc.returncode})."
                if implement_stdout:
                    message += f"\n\nstdout:\n```{implement_stdout}```"
                if implement_stderr:
                    message += f"\n\nstderr:\n```{implement_stderr}```"
                _post_thread(channel, thread_ts, message)
                return False, False, wt_path

            _post_thread(channel, thread_ts, f"Loop {review_pass}/{MAX_IMPLEMENT_REVIEW_LOOPS}: review pass")
            review_output_path = Path(wt_path) / REVIEW_OUTPUT_FILENAME
            if review_output_path.exists():
                review_output_path.unlink()
            review_task = _build_review_task(
                user_task=task,
                answers=answers,
                workflow_guide=workflow_docs["workflow"],
                review_guide=workflow_docs["review"],
                plan_text=plan_text,
            )
            review_cmd = _build_command(review_task, phase="review")
            _set_runtime_state(
                stage=f"review-pass-{review_pass}",
                stage_since=time.time(),
                command=shlex.join(review_cmd),
                last_status="running",
            )
            _update_session(
                session_id,
                stage=f"review-pass-{review_pass}",
                status="running",
                review_pass=review_pass,
                command=shlex.join(review_cmd),
            )
            _post_thread(
                channel,
                thread_ts,
                f"Review command: `{shlex.join(review_cmd)}`",
            )
            review_proc = _run_with_heartbeat(
                cmd=review_cmd,
                cwd=wt_path,
                env=_mini_env(),
                timeout_seconds=TASK_TIMEOUT_SECONDS,
                channel=channel,
                thread_ts=thread_ts,
                stage_label=f"review pass {review_pass}",
            )
            review_stdout = _tail(review_proc.stdout, MAX_STDOUT_CHARS)
            review_stderr = _tail(review_proc.stderr, MAX_STDERR_CHARS)
            if review_proc.returncode != 0:
                _set_runtime_state(
                    running=False,
                    stage="review-failed",
                    stage_since=time.time(),
                    command="",
                    last_status=f"failed (review exit {review_proc.returncode})",
                    last_error=f"Review stage failed (exit {review_proc.returncode}).",
                    last_stdout=review_stdout,
                    last_stderr=review_stderr,
                )
                _update_session(
                    session_id,
                    state="failed",
                    stage="review-failed",
                    status=f"failed (review exit {review_proc.returncode})",
                    last_error=f"Review stage failed (exit {review_proc.returncode}).",
                    last_stdout=review_stdout,
                    last_stderr=review_stderr,
                    review_pass=review_pass,
                )
                message = f"Review stage failed (exit {review_proc.returncode})."
                if review_stdout:
                    message += f"\n\nstdout:\n```{review_stdout}```"
                if review_stderr:
                    message += f"\n\nstderr:\n```{review_stderr}```"
                _post_thread(channel, thread_ts, message)
                return False, False, wt_path

            review_output = _load_review_output(wt_path)
            if review_output["status"] == "approved":
                approved = True
                review_feedback = []
                _post_thread(channel, thread_ts, f"Review pass {review_pass}: approved.")
                break

            review_feedback = review_output["issues"]
            feedback_block = "\n".join(f"- {item}" for item in review_feedback)
            _post_thread(
                channel,
                thread_ts,
                (
                    f"Review pass {review_pass}: changes requested. "
                    "Will run another implementation pass.\n"
                    f"Issues:\n{feedback_block}"
                ),
            )

        if not approved:
            _post_thread(
                channel,
                thread_ts,
                (
                    "Reached max implement/review loops without explicit review approval. "
                    "Proceeding to test + PR with latest implementation."
                ),
            )

        _post_thread(channel, thread_ts, "Stage: test + PR")
        test_pr_task = _build_test_pr_task(
            user_task=task,
            answers=answers,
            workflow_guide=workflow_docs["workflow"],
            review_guide=workflow_docs["review"],
            plan_text=plan_text,
        )
        test_pr_cmd = _build_command(test_pr_task, phase="implement")
        _set_runtime_state(
            stage="test-pr",
            stage_since=time.time(),
            command=shlex.join(test_pr_cmd),
            last_status="running",
        )
        _update_session(
            session_id,
            stage="test-pr",
            status="running",
            review_pass=review_pass,
            command=shlex.join(test_pr_cmd),
        )
        _post_thread(
            channel,
            thread_ts,
            f"Test/PR command: `{shlex.join(test_pr_cmd)}`",
        )
        proc = _run_with_heartbeat(
            cmd=test_pr_cmd,
            cwd=wt_path,
            env=_mini_env(),
            timeout_seconds=TASK_TIMEOUT_SECONDS,
            channel=channel,
            thread_ts=thread_ts,
            stage_label="test+pr",
        )
        stdout = _tail(proc.stdout, MAX_STDOUT_CHARS)
        stderr = _tail(proc.stderr, MAX_STDERR_CHARS)
        status = "completed" if proc.returncode == 0 else f"failed (exit {proc.returncode})"
        _set_runtime_state(
            running=False,
            stage="completed" if proc.returncode == 0 else "execution-failed",
            stage_since=time.time(),
            command="",
            last_status=status,
            last_error="" if proc.returncode == 0 else f"Execution failed (exit {proc.returncode}).",
            last_stdout=stdout,
            last_stderr=stderr,
        )
        _update_session(
            session_id,
            state="completed" if proc.returncode == 0 else "failed",
            stage="completed" if proc.returncode == 0 else "execution-failed",
            status=status,
            last_error="" if proc.returncode == 0 else f"Execution failed (exit {proc.returncode}).",
            last_stdout=stdout,
            last_stderr=stderr,
            review_pass=review_pass,
        )
        message = f"Workflow run {status}."
        if stdout:
            message += f"\n\nstdout:\n```{stdout}```"
        if stderr:
            message += f"\n\nstderr:\n```{stderr}```"
        if not stdout and not stderr:
            message += "\n\n(no output)"
        _post_thread(channel, thread_ts, message)
        if proc.returncode != 0 and KEEP_WORKTREE_ON_FAILURE:
            keep_worktree = True
            _post_thread(
                channel,
                thread_ts,
                f"Keeping failed worktree for inspection: `{wt_path}`",
            )
        return keep_worktree, False, wt_path
    except subprocess.TimeoutExpired:
        _set_runtime_state(
            running=False,
            stage="timeout",
            stage_since=time.time(),
            command="",
            last_status="timed_out",
            last_error=f"Timed out after {TASK_TIMEOUT_SECONDS} seconds.",
        )
        _update_session(
            session_id,
            state="timeout",
            stage="timeout",
            status="timed_out",
            last_error=f"Timed out after {TASK_TIMEOUT_SECONDS} seconds.",
        )
        _post_thread(channel, thread_ts, f"Timed out after {TASK_TIMEOUT_SECONDS} seconds.")
        return False, False, wt_path
    except Exception as exc:
        _set_runtime_state(
            running=False,
            stage="runner-error",
            stage_since=time.time(),
            command="",
            last_status="runner_error",
            last_error=str(exc),
        )
        _update_session(
            session_id,
            state="failed",
            stage="runner-error",
            status="runner_error",
            last_error=str(exc),
        )
        _post_thread(channel, thread_ts, f"Runner error: `{exc}`")
        return False, False, wt_path


@app.event("app_mention")
def on_app_mention(event: dict[str, Any], say) -> None:
    channel = event.get("channel", "")
    if ALLOW_CHANNEL_IDS and channel not in ALLOW_CHANNEL_IDS:
        say(thread_ts=event.get("ts"), text="This channel is not in the allow-list.")
        return

    conversation_id = _thread_key(event)
    mention_text = _extract_mention_text(event.get("text", ""))
    pending = _peek_pending_plan(conversation_id) if conversation_id else None
    if pending is not None:
        normalized = " ".join(mention_text.lower().split())
        if normalized in {"status", "state", "progress", "last output", "output"}:
            say(thread_ts=pending["thread_ts"], text=_status_message())
            return
        if normalized in {"cancel", "abort"}:
            canceled = _pop_pending_plan(conversation_id)
            if canceled is not None:
                _update_session(
                    str(canceled.get("session_id", "")),
                    state="canceled",
                    stage="canceled",
                    status="canceled",
                )
            say(thread_ts=pending["thread_ts"], text="Pending planning conversation canceled.")
            return
        if not mention_text:
            say(
                thread_ts=pending["thread_ts"],
                text="No clarification text found. Reply with your answers in this thread.",
            )
            return
        resumed = _pop_pending_plan(conversation_id)
        if resumed is None:
            say(
                thread_ts=event.get("thread_ts") or event.get("ts"),
                text="No pending planning conversation found for this thread.",
            )
            return
        resumed_job = {
            **resumed,
            "conversation_id": conversation_id,
            "latest_reply": mention_text,
        }
        task_queue.put(resumed_job)
        _update_session(
            str(resumed.get("session_id", "")),
            state="queued",
            stage="queued",
            status="queued",
        )
        say(
            thread_ts=resumed["thread_ts"],
            text="Clarifications received. Resuming planning and execution.",
        )
        return

    payload = _extract_task_payload(event.get("text", ""))
    if not payload:
        if TASK_PREFIX:
            say(
                thread_ts=event.get("ts"),
                text=f"No task found. Start with `{TASK_PREFIX}`.",
            )
        else:
            say(thread_ts=event.get("ts"), text="No task found after mention.")
        return
    if _is_repo_listing_command(payload):
        say(thread_ts=event.get("ts"), text=_repo_listing_message())
        return
    if _is_help_command(payload):
        say(thread_ts=event.get("ts"), text=_help_message())
        return
    if _is_status_command(payload):
        say(thread_ts=event.get("ts"), text=_status_message())
        return

    parsed = _parse_payload(payload)
    if not parsed["task"]:
        say(
            thread_ts=event.get("ts"),
            text="No task text found. Use: `repo=<alias> branch=<name> <task>`.",
        )
        return

    try:
        target = _resolve_repo_and_branch(parsed["repo"], parsed["branch"])
    except Exception as exc:
        say(thread_ts=event.get("ts"), text=f"Invalid repo/branch selection: `{exc}`")
        return

    session_id = uuid.uuid4().hex[:10]
    job = {
        "session_id": session_id,
        "channel": channel,
        "thread_ts": conversation_id,
        "conversation_id": conversation_id,
        "task": parsed["task"],
        "repo_alias": target["repo_alias"],
        "repo_path": target["repo_path"],
        "branch": target["branch"],
        "answers": [],
        "latest_reply": "",
    }
    _init_session(job)
    task_queue.put(job)
    _set_runtime_state(last_status="queued")
    _update_session(session_id, state="queued", stage="queued", status="queued")
    say(
        thread_ts=event.get("ts"),
        text=(
            f"Queued session=`{session_id}`. "
            f"repo=`{target['repo_alias']}` branch=`{target['branch']}`."
        ),
    )


def worker() -> None:
    while True:
        job = task_queue.get()
        keep_worktree = False
        wt_path = ""
        try:
            keep_worktree, _, wt_path = _run_workflow(job)
        finally:
            repo_path = job["repo_path"]
            if wt_path and not keep_worktree:
                _cleanup_worktree(repo_path, wt_path)
            task_queue.task_done()


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    _start_web_ui()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
