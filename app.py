import os
import queue
import re
import shlex
import subprocess
import threading
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
ALLOW_CHANNEL_IDS = {
    c.strip() for c in os.getenv("ALLOW_CHANNEL_IDS", "").split(",") if c.strip()
}

app = App(token=SLACK_BOT_TOKEN)
task_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()


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
        "- `help`: show this message",
        "",
        "Task format:",
        "- `repo=<alias> branch=<branch> <task text>`",
        "- `branch=` is optional (uses repo default branch)",
        "- `repo=` is optional (uses `default_repo`)",
    ]
    return "\n".join(lines)


def _parse_payload(payload: str) -> dict[str, str]:
    tokens = shlex.split(payload)
    repo = ""
    branch = ""
    task_tokens: list[str] = []
    for token in tokens:
        lower = token.lower()
        if lower.startswith("repo=") and not repo:
            repo = token.split("=", 1)[1].strip()
            continue
        if lower.startswith("branch=") and not branch:
            branch = token.split("=", 1)[1].strip()
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
    cmd.extend(["-t", task])
    return cmd


def _tail(text: str, max_chars: int) -> str:
    if not text:
        return ""
    return text[-max_chars:] if len(text) > max_chars else text


def _post_thread(channel: str, thread_ts: str, text: str) -> None:
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


@app.event("app_mention")
def on_app_mention(event: dict[str, Any], say) -> None:
    channel = event.get("channel", "")
    if ALLOW_CHANNEL_IDS and channel not in ALLOW_CHANNEL_IDS:
        say(thread_ts=event.get("ts"), text="This channel is not in the allow-list.")
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
        "thread_ts": event.get("ts", ""),
        "task": parsed["task"],
        "repo_alias": target["repo_alias"],
        "repo_path": target["repo_path"],
        "branch": target["branch"],
    }
    task_queue.put(job)
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
        channel = job["channel"]
        thread_ts = job["thread_ts"]
        task = job["task"]
        repo_alias = job["repo_alias"]
        repo_path = job["repo_path"]
        branch = job["branch"]
        cmd = _build_command(task)
        wt_path = ""
        keep_worktree = False
        try:
            wt_path, base_ref = _prepare_worktree(repo_path, branch)
            _post_thread(
                channel,
                thread_ts,
                (
                    f"Running: `{shlex.join(cmd)}`\n"
                    f"repo=`{repo_alias}` branch=`{branch}` ref=`{base_ref}`\n"
                    f"worktree=`{wt_path}`"
                ),
            )
            proc = subprocess.run(
                cmd,
                cwd=wt_path,
                capture_output=True,
                text=True,
                timeout=TASK_TIMEOUT_SECONDS,
            )
            stdout = _tail(proc.stdout, MAX_STDOUT_CHARS)
            stderr = _tail(proc.stderr, MAX_STDERR_CHARS)

            status = "completed" if proc.returncode == 0 else f"failed (exit {proc.returncode})"
            message = f"Run {status}."
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
        except subprocess.TimeoutExpired:
            _post_thread(channel, thread_ts, f"Timed out after {TASK_TIMEOUT_SECONDS} seconds.")
        except Exception as exc:
            _post_thread(channel, thread_ts, f"Runner error: `{exc}`")
        finally:
            if wt_path and not keep_worktree:
                _cleanup_worktree(repo_path, wt_path)
            task_queue.task_done()


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
