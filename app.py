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
ALLOW_CHANNEL_IDS = {
    c.strip() for c in os.getenv("ALLOW_CHANNEL_IDS", "").split(",") if c.strip()
}

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


def _build_command(task: str) -> list[str]:
    cmd = shlex.split(MINI_CMD)
    if MINI_MODEL_CLASS:
        cmd.extend(["--model-class", MINI_MODEL_CLASS])
    if MINI_MODEL_NAME:
        cmd.extend(["-m", MINI_MODEL_NAME])
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


def _build_execution_task(
    user_task: str,
    answers: list[str],
    workflow_guide: str,
    review_guide: str,
    plan_text: str,
) -> str:
    return f"""
Follow this required workflow exactly:
{workflow_guide}

Use this self-review guidance:
{review_guide}

Plan approved for execution:
{plan_text}

Original user task:
{user_task}

User clarifications:
{_answers_block(answers)}

Execution requirements:
1. Implement the plan.
2. Perform self-review against your own diff before finishing.
3. Run relevant tests/verification commands.
4. Create a PR if tooling/auth allows it; if blocked, clearly report the blocker and exact command/output.
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


def _tail(text: str, max_chars: int) -> str:
    if not text:
        return ""
    return text[-max_chars:] if len(text) > max_chars else text


def _post_thread(channel: str, thread_ts: str, text: str) -> None:
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def _queue_pending_plan(
    conversation_id: str,
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
        wt_path, base_ref = _prepare_worktree(repo_path, branch)
        _set_runtime_state(worktree=wt_path)
        _post_thread(
            channel,
            thread_ts,
            (
                f"Workflow started.\n"
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
        planning_cmd = _build_command(planning_task)
        _set_runtime_state(
            stage="planning",
            stage_since=time.time(),
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
            return False, True, wt_path

        assumptions = plan_output["assumptions"]
        assumptions_block = (
            "\n".join(f"- {item}" for item in assumptions) if assumptions else "- (none)"
        )
        plan_text = f"{plan_output['plan']}\n\nAssumptions:\n{assumptions_block}"

        _post_thread(channel, thread_ts, "Stage: implement + self-review + test + PR")
        execute_task = _build_execution_task(
            user_task=task,
            answers=answers,
            workflow_guide=workflow_docs["workflow"],
            review_guide=workflow_docs["review"],
            plan_text=plan_text,
        )
        execute_cmd = _build_command(execute_task)
        _set_runtime_state(
            stage="implementation",
            stage_since=time.time(),
            command=shlex.join(execute_cmd),
            last_status="running",
        )
        _post_thread(
            channel,
            thread_ts,
            f"Execution command: `{shlex.join(execute_cmd)}`",
        )
        proc = _run_with_heartbeat(
            cmd=execute_cmd,
            cwd=wt_path,
            env=_mini_env(),
            timeout_seconds=TASK_TIMEOUT_SECONDS,
            channel=channel,
            thread_ts=thread_ts,
            stage_label="implementation",
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
            _pop_pending_plan(conversation_id)
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

    job = {
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
    task_queue.put(job)
    _set_runtime_state(last_status="queued")
    say(
        thread_ts=event.get("ts"),
        text=(
            "Queued. "
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
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
