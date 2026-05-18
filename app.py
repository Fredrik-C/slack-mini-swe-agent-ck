from __future__ import annotations

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from mini_executor import MiniExecutor, MiniExecutorConfig
from session_store import SessionStore, SessionStoreConfig
from slack_handlers import SlackHandlerConfig, SlackHandlers
from workflow_content import WorkflowPromptBuilder, WorkflowPromptConfig
from workflow_runner import WorkflowRunner, WorkflowRunnerConfig

load_dotenv()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_repo_config(path_value: str) -> dict[str, Any]:
    path = Path(path_value)
    if not path.exists():
        raise RuntimeError(f"Repo config not found at {path}. Create it from repos.example.json.")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    repos = data.get("repos", {})
    if not isinstance(repos, dict) or not repos:
        raise RuntimeError("Repo config must define a non-empty 'repos' object.")
    return data


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
MSWEA_COST_TRACKING = os.getenv("MSWEA_COST_TRACKING", "ignore_errors").strip()
MINI_INFRA_RETRY_MAX = int(os.getenv("MINI_INFRA_RETRY_MAX", "1"))
PLAN_GUIDE_PATH = os.getenv("PLAN_GUIDE_PATH", "prompts/planning.md")
REVIEW_GUIDE_PATH = os.getenv("REVIEW_GUIDE_PATH", "prompts/review.md")
WORKFLOW_GUIDE_PATH = os.getenv("WORKFLOW_GUIDE_PATH", "prompts/workflow.md")
TOOLING_GUIDE_PATH = os.getenv("TOOLING_GUIDE_PATH", "prompts/tooling.md")
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
        raise RuntimeError("OPENROUTER_API_KEY is required when any model class is set to openrouter.")

REPO_CONFIG = _load_repo_config(REPO_CONFIG_PATH)

app = App(token=SLACK_BOT_TOKEN)
task_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()

store = SessionStore(
    SessionStoreConfig(
        max_review_passes=MAX_IMPLEMENT_REVIEW_LOOPS,
        web_ui_max_sessions=WEB_UI_MAX_SESSIONS,
        status_output_chars=STATUS_OUTPUT_CHARS,
    )
)

mini_executor = MiniExecutor(
    MiniExecutorConfig(
        mini_cmd=MINI_CMD,
        mini_model_class=MINI_MODEL_CLASS,
        mini_model_name=MINI_MODEL_NAME,
        mini_plan_model_class=MINI_PLAN_MODEL_CLASS,
        mini_plan_model_name=MINI_PLAN_MODEL_NAME,
        mini_implement_model_class=MINI_IMPLEMENT_MODEL_CLASS,
        mini_implement_model_name=MINI_IMPLEMENT_MODEL_NAME,
        mini_review_model_class=MINI_REVIEW_MODEL_CLASS,
        mini_review_model_name=MINI_REVIEW_MODEL_NAME,
        mini_use_yolo=MINI_USE_YOLO,
        mini_exit_immediately=MINI_EXIT_IMMEDIATELY,
        mswea_configured=MSWEA_CONFIGURED,
        mswea_cost_tracking=MSWEA_COST_TRACKING,
        mini_infra_retry_max=MINI_INFRA_RETRY_MAX,
        progress_heartbeat_seconds=PROGRESS_HEARTBEAT_SECONDS,
    )
)

prompt_builder = WorkflowPromptBuilder(
    WorkflowPromptConfig(
        plan_output_filename=PLAN_OUTPUT_FILENAME,
        review_output_filename=REVIEW_OUTPUT_FILENAME,
    )
)


def _post_thread(channel: str, thread_ts: str, text: str) -> None:
    app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


workflow_runner = WorkflowRunner(
    config=WorkflowRunnerConfig(
        plan_guide_path=PLAN_GUIDE_PATH,
        review_guide_path=REVIEW_GUIDE_PATH,
        workflow_guide_path=WORKFLOW_GUIDE_PATH,
        tooling_guide_path=TOOLING_GUIDE_PATH,
        plan_output_filename=PLAN_OUTPUT_FILENAME,
        review_output_filename=REVIEW_OUTPUT_FILENAME,
        task_timeout_seconds=TASK_TIMEOUT_SECONDS,
        max_stdout_chars=MAX_STDOUT_CHARS,
        max_stderr_chars=MAX_STDERR_CHARS,
        max_implement_review_loops=MAX_IMPLEMENT_REVIEW_LOOPS,
        git_fetch_before_worktree=GIT_FETCH_BEFORE_WORKTREE,
        keep_worktree_on_failure=KEEP_WORKTREE_ON_FAILURE,
        worktree_root=WORKTREE_ROOT,
    ),
    repo_config=REPO_CONFIG,
    store=store,
    mini_executor=mini_executor,
    prompt_builder=prompt_builder,
    post_thread=_post_thread,
)

slack_handlers = SlackHandlers(
    config=SlackHandlerConfig(
        task_prefix=TASK_PREFIX,
        allow_channel_ids=ALLOW_CHANNEL_IDS,
    ),
    repo_config=REPO_CONFIG,
    store=store,
    workflow_runner=workflow_runner,
    task_queue=task_queue,
)
slack_handlers.register(app)


def _make_web_status_handler() -> type[BaseHTTPRequestHandler]:
    class WebStatusHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/sessions.json":
                payload = store.sessions_payload(queue_depth=task_queue.qsize())
                body = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/" or path == "/index.html":
                body = store.render_sessions_html(queue_depth=task_queue.qsize()).encode("utf-8")
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

    return WebStatusHandler


def _start_web_ui() -> None:
    if not WEB_UI_ENABLED:
        return
    server = ThreadingHTTPServer((WEB_UI_BIND, WEB_UI_PORT), _make_web_status_handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Web UI running on http://{WEB_UI_BIND}:{WEB_UI_PORT}")


if __name__ == "__main__":
    threading.Thread(target=slack_handlers.worker_loop, daemon=True).start()
    _start_web_ui()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
