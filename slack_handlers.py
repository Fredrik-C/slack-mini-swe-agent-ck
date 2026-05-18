from __future__ import annotations

import queue
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from slack_bolt import App

from session_store import SessionStore
from workflow_runner import WorkflowRunner


@dataclass(frozen=True)
class SlackHandlerConfig:
    task_prefix: str
    allow_channel_ids: set[str]


class SlackHandlers:
    def __init__(
        self,
        *,
        config: SlackHandlerConfig,
        repo_config: dict[str, Any],
        store: SessionStore,
        workflow_runner: WorkflowRunner,
        task_queue: "queue.Queue[dict[str, Any]]",
    ) -> None:
        self._config = config
        self._repo_config = repo_config
        self._store = store
        self._workflow_runner = workflow_runner
        self._task_queue = task_queue

    def register(self, app: App) -> None:
        @app.event("app_mention")
        def on_app_mention(event: dict[str, Any], say) -> None:
            self.handle_app_mention(event, say)

    def handle_app_mention(self, event: dict[str, Any], say: Callable[..., Any]) -> None:
        channel = event.get("channel", "")
        if self._config.allow_channel_ids and channel not in self._config.allow_channel_ids:
            say(thread_ts=event.get("ts"), text="This channel is not in the allow-list.")
            return

        conversation_id = self._thread_key(event)
        mention_text = self._extract_mention_text(event.get("text", ""))
        pending = self._store.peek_pending_plan(conversation_id) if conversation_id else None
        if pending is not None:
            normalized = " ".join(mention_text.lower().split())
            if normalized in {"status", "state", "progress", "last output", "output"}:
                say(
                    thread_ts=pending["thread_ts"],
                    text=self._store.status_message(queue_depth=self._task_queue.qsize()),
                )
                return
            if normalized in {"cancel", "abort"}:
                canceled = self._store.pop_pending_plan(conversation_id)
                if canceled is not None:
                    self._store.update_session(
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
            resumed = self._store.pop_pending_plan(conversation_id)
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
            self._task_queue.put(resumed_job)
            self._store.update_session(
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

        payload = self._extract_task_payload(event.get("text", ""))
        if not payload:
            if self._config.task_prefix:
                say(
                    thread_ts=event.get("ts"),
                    text=f"No task found. Start with `{self._config.task_prefix}`.",
                )
            else:
                say(thread_ts=event.get("ts"), text="No task found after mention.")
            return

        if self._is_repo_listing_command(payload):
            say(thread_ts=event.get("ts"), text=self._repo_listing_message())
            return
        if self._is_help_command(payload):
            say(thread_ts=event.get("ts"), text=self._help_message())
            return
        if self._is_status_command(payload):
            say(thread_ts=event.get("ts"), text=self._store.status_message(queue_depth=self._task_queue.qsize()))
            return

        parsed = self._workflow_runner.parse_payload(payload)
        if not parsed["task"]:
            say(
                thread_ts=event.get("ts"),
                text="No task text found. Use: `repo=<alias> branch=<name> <task>`.",
            )
            return

        try:
            target = self._workflow_runner.resolve_repo_and_branch(parsed["repo"], parsed["branch"])
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
        self._store.init_session(job)
        self._task_queue.put(job)
        self._store.set_runtime_state(last_status="queued")
        self._store.update_session(session_id, state="queued", stage="queued", status="queued")
        say(
            thread_ts=event.get("ts"),
            text=(
                f"Queued session=`{session_id}`. "
                f"repo=`{target['repo_alias']}` branch=`{target['branch']}`."
            ),
        )

    def worker_loop(self) -> None:
        while True:
            job = self._task_queue.get()
            keep_worktree = False
            wt_path = ""
            try:
                keep_worktree, _, wt_path = self._workflow_runner.run_workflow(job)
            finally:
                repo_path = job["repo_path"]
                if wt_path and not keep_worktree:
                    self._workflow_runner.cleanup_worktree(repo_path, wt_path)
                self._task_queue.task_done()

    @staticmethod
    def _extract_mention_text(text: str) -> str:
        return re.sub(r"<@[^>]+>", "", text).strip()

    @staticmethod
    def _thread_key(event: dict[str, Any]) -> str:
        return event.get("thread_ts") or event.get("ts", "")

    def _extract_task_payload(self, text: str) -> str:
        cleaned = re.sub(r"<@[^>]+>", "", text).strip()
        if self._config.task_prefix:
            if not cleaned.lower().startswith(self._config.task_prefix.lower()):
                return ""
            cleaned = cleaned[len(self._config.task_prefix) :].strip()
        return cleaned

    @staticmethod
    def _is_repo_listing_command(payload: str) -> bool:
        normalized = " ".join(payload.lower().split())
        return normalized in {"repos", "list repos", "repo list"}

    @staticmethod
    def _is_help_command(payload: str) -> bool:
        normalized = " ".join(payload.lower().split())
        return normalized in {"help", "usage", "commands"}

    @staticmethod
    def _is_status_command(payload: str) -> bool:
        normalized = " ".join(payload.lower().split())
        return normalized in {"status", "state", "progress", "last output", "output"}

    def _repo_listing_message(self) -> str:
        default_repo = self._repo_config.get("default_repo", "")
        lines = [f"Configured repos (default=`{default_repo}`):"]
        for alias in sorted(self._repo_config["repos"].keys()):
            entry = self._repo_config["repos"][alias]
            default_branch = entry.get("default_branch", "main")
            patterns = entry.get("allowed_branches", [])
            pattern_text = ", ".join(patterns) if patterns else "(any)"
            lines.append(
                f"- `{alias}`: default_branch=`{default_branch}` allowed_branches=`{pattern_text}`"
            )
        return "\n".join(lines)

    @staticmethod
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
