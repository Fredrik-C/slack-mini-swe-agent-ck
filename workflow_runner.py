from __future__ import annotations

import fnmatch
import json
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mini_executor import MiniExecutor
from session_store import SessionStore
from workflow_content import (
    WorkflowPromptBuilder,
    load_plan_output,
    load_review_output,
    load_workflow_docs,
)


@dataclass(frozen=True)
class WorkflowRunnerConfig:
    plan_guide_path: str
    review_guide_path: str
    workflow_guide_path: str
    tooling_guide_path: str
    plan_output_filename: str
    review_output_filename: str
    task_timeout_seconds: int
    max_stdout_chars: int
    max_stderr_chars: int
    max_implement_review_loops: int
    git_fetch_before_worktree: bool
    keep_worktree_on_failure: bool
    worktree_root: str
    mini_trajectory_path: str


class WorkflowRunner:
    def __init__(
        self,
        *,
        config: WorkflowRunnerConfig,
        repo_config: dict[str, Any],
        store: SessionStore,
        mini_executor: MiniExecutor,
        prompt_builder: WorkflowPromptBuilder,
        post_thread: Callable[[str, str, str], None],
    ) -> None:
        self._config = config
        self._repo_config = repo_config
        self._store = store
        self._mini_executor = mini_executor
        self._prompt_builder = prompt_builder
        self._post_thread = post_thread

    def parse_payload(self, payload: str) -> dict[str, str]:
        def clean_selector(value: str) -> str:
            return value.strip().strip(",.;:")

        tokens = shlex.split(payload)
        repo = ""
        branch = ""
        task_tokens: list[str] = []
        for token in tokens:
            lower = token.lower()
            if lower.startswith("repo=") and not repo:
                repo = clean_selector(token.split("=", 1)[1])
                continue
            if lower.startswith("branch=") and not branch:
                branch = clean_selector(token.split("=", 1)[1])
                continue
            task_tokens.append(token)
        return {"repo": repo, "branch": branch, "task": " ".join(task_tokens).strip()}

    def resolve_repo_and_branch(self, repo_alias: str, branch: str) -> dict[str, str]:
        repos = self._repo_config["repos"]
        default_repo = self._repo_config.get("default_repo", "")
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

    def prepare_worktree(self, repo_path: str, branch: str) -> tuple[str, str]:
        if not Path(repo_path).exists():
            raise ValueError(f"Repo path does not exist: {repo_path}")
        if self._run_quiet(["git", "-C", repo_path, "rev-parse", "--is-inside-work-tree"]).returncode != 0:
            raise ValueError(f"Path is not a git repository: {repo_path}")

        if not self._config.git_fetch_before_worktree:
            raise RuntimeError(
                "Fresh-state policy requires remote fetch before every worktree. "
                "Set GIT_FETCH_BEFORE_WORKTREE=true."
            )

        fetch_proc = self._run_quiet(["git", "-C", repo_path, "fetch", "--all", "--prune"])
        if fetch_proc.returncode != 0:
            details = (fetch_proc.stderr or fetch_proc.stdout).strip() or "no output"
            raise RuntimeError(
                "Failed to fetch repository before worktree creation. "
                "Verify git credentials for private remotes. "
                f"git output: {details}"
            )

        remote_ref = f"origin/{branch}"
        if not self._ref_exists(repo_path, remote_ref):
            raise ValueError(
                f"Cannot find remote branch '{remote_ref}' in repository: {repo_path}. "
                "Fresh-state runs require a remote-tracked branch."
            )
        base_ref = remote_ref

        worktree_root = Path(self._config.worktree_root).expanduser().resolve()
        worktree_root.mkdir(parents=True, exist_ok=True)
        wt_name = f"{Path(repo_path).name}-{branch.replace('/', '_')}-{uuid.uuid4().hex[:8]}"
        wt_path = str((worktree_root / wt_name).resolve())
        add_proc = self._run_quiet(
            ["git", "-C", repo_path, "worktree", "add", "--detach", wt_path, base_ref]
        )
        if add_proc.returncode != 0:
            raise RuntimeError(
                f"Failed to create worktree: {add_proc.stderr.strip() or add_proc.stdout.strip()}"
            )

        checkout_proc = self._run_quiet(
            ["git", "-C", wt_path, "checkout", "-B", branch, remote_ref]
        )
        if checkout_proc.returncode != 0:
            self._run_quiet(["git", "-C", repo_path, "worktree", "remove", "--force", wt_path])
            raise RuntimeError(
                "Failed to align worktree branch with remote tip: "
                f"{checkout_proc.stderr.strip() or checkout_proc.stdout.strip()}"
            )
        return wt_path, base_ref

    def cleanup_worktree(self, repo_path: str, wt_path: str) -> None:
        self._run_quiet(["git", "-C", repo_path, "worktree", "remove", "--force", wt_path])

    def run_workflow(self, job: dict[str, Any]) -> tuple[bool, bool, str]:
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

        workflow_docs = load_workflow_docs(
            plan_guide_path=self._config.plan_guide_path,
            review_guide_path=self._config.review_guide_path,
            workflow_guide_path=self._config.workflow_guide_path,
            tooling_guide_path=self._config.tooling_guide_path,
        )
        wt_path = ""
        keep_worktree = False
        review_feedback: list[str] = []
        review_pass = 0
        ck_stages: list[str] = []
        ck_examples: list[str] = []

        def update_ck_telemetry(stage_name: str) -> bool:
            hits = self._extract_ck_commands_from_trajectory()
            stage_has_ck = bool(hits)
            if hits:
                if stage_name not in ck_stages:
                    ck_stages.append(stage_name)
                for item in hits:
                    if item not in ck_examples:
                        ck_examples.append(item)
            payload = {
                "ck_checked": True,
                "ck_used": bool(ck_stages),
                "ck_stages": list(ck_stages),
                "ck_examples": list(ck_examples[:5]),
            }
            self._store.set_runtime_state(**payload)
            self._store.update_session(session_id, **payload)
            return stage_has_ck

        try:
            self._store.set_runtime_state(
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
                ck_checked=False,
                ck_used=False,
                ck_stages=[],
                ck_examples=[],
            )
            self._store.update_session(
                session_id,
                state="running",
                stage="preparing-worktree",
                status="running",
                review_pass=review_pass,
                last_error="",
                last_stdout="",
                last_stderr="",
                ck_checked=False,
                ck_used=False,
                ck_stages=[],
                ck_examples=[],
            )
            wt_path, base_ref = self.prepare_worktree(repo_path, branch)
            self._store.set_runtime_state(worktree=wt_path)
            self._store.update_session(session_id, worktree=wt_path)
            self._post_thread(
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

            planning_task = self._prompt_builder.build_planning_task(
                user_task=task,
                answers=answers,
                workflow_guide=workflow_docs["workflow"],
                planning_guide=workflow_docs["planning"],
                tooling_guide=workflow_docs["tooling"],
            )
            planning_cmd = self._mini_executor.build_command(planning_task, phase="plan")
            planning_summary = self._stage_command_summary(phase="plan", cmd=planning_cmd)
            self._store.set_runtime_state(
                stage="planning",
                stage_since=time.time(),
                command=planning_summary,
            )
            self._store.update_session(
                session_id,
                stage="planning",
                status="running",
                command=planning_summary,
            )
            self._post_thread(channel, thread_ts, f"Planning stage invocation: {planning_summary}")
            planning_proc = self._mini_executor.run_stage(
                cmd=planning_cmd,
                cwd=wt_path,
                env=self._mini_executor.mini_env(),
                timeout_seconds=self._config.task_timeout_seconds,
                stage_label="planning",
                post_progress=lambda text: self._post_thread(channel, thread_ts, text),
            )
            planning_used_ck = update_ck_telemetry("planning")
            if planning_proc.returncode != 0:
                stdout = self._tail(planning_proc.stdout, self._config.max_stdout_chars)
                stderr = self._tail(planning_proc.stderr, self._config.max_stderr_chars)
                self._store.set_runtime_state(
                    running=False,
                    stage="planning-failed",
                    stage_since=time.time(),
                    last_status=f"failed (planning exit {planning_proc.returncode})",
                    last_error=f"Planning stage failed (exit {planning_proc.returncode}).",
                    last_stdout=stdout,
                    last_stderr=stderr,
                )
                self._store.update_session(
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
                self._post_thread(channel, thread_ts, message)
                return False, False, wt_path
            if not planning_used_ck:
                stdout = self._tail(planning_proc.stdout, self._config.max_stdout_chars)
                stderr = self._tail(planning_proc.stderr, self._config.max_stderr_chars)
                error_text = (
                    "Planning stage completed without Context King usage. "
                    "Planning must invoke CK commands before proceeding."
                )
                self._store.set_runtime_state(
                    running=False,
                    stage="planning-missing-ck",
                    stage_since=time.time(),
                    command="",
                    last_status="failed (planning missing CK)",
                    last_error=error_text,
                    last_stdout=stdout,
                    last_stderr=stderr,
                )
                self._store.update_session(
                    session_id,
                    state="failed",
                    stage="planning-missing-ck",
                    status="failed (planning missing CK)",
                    last_error=error_text,
                    last_stdout=stdout,
                    last_stderr=stderr,
                )
                self._post_thread(
                    channel,
                    thread_ts,
                    (
                        "Planning stage failed CK policy.\n"
                        "No CK invocation was detected in planning execution output. "
                        "Run aborted before implementation."
                    ),
                )
                return False, False, wt_path

            plan_output = load_plan_output(
                worktree_path=wt_path,
                plan_output_filename=self._config.plan_output_filename,
            )
            if plan_output["status"] == "needs_input":
                questions = plan_output["questions"]
                if not questions:
                    raise RuntimeError("Planning requested input but returned no questions.")
                question_lines = "\n".join(
                    f"{idx}. {question}" for idx, question in enumerate(questions, start=1)
                )
                self._store.queue_pending_plan(
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
                self._post_thread(
                    channel,
                    thread_ts,
                    (
                        "Planning requires clarification before implementation.\n"
                        "Reply in this thread by mentioning the bot and answering these questions:\n"
                        f"{question_lines}"
                    ),
                )
                self._store.set_runtime_state(
                    running=False,
                    stage="waiting-user-clarification",
                    stage_since=time.time(),
                    command="",
                    last_status="waiting_for_input",
                    last_stdout=self._tail(planning_proc.stdout, self._config.max_stdout_chars),
                    last_stderr=self._tail(planning_proc.stderr, self._config.max_stderr_chars),
                )
                self._store.update_session(
                    session_id,
                    state="waiting_input",
                    stage="waiting-user-clarification",
                    status="waiting_for_input",
                    last_stdout=self._tail(planning_proc.stdout, self._config.max_stdout_chars),
                    last_stderr=self._tail(planning_proc.stderr, self._config.max_stderr_chars),
                )
                return False, True, wt_path

            assumptions = plan_output["assumptions"]
            assumptions_block = "\n".join(f"- {item}" for item in assumptions) if assumptions else "- (none)"
            plan_text = f"{plan_output['plan']}\n\nAssumptions:\n{assumptions_block}"

            self._post_thread(channel, thread_ts, "Stage: implement + review loop")
            approved = False
            for review_pass in range(1, self._config.max_implement_review_loops + 1):
                self._post_thread(
                    channel,
                    thread_ts,
                    f"Loop {review_pass}/{self._config.max_implement_review_loops}: implementation pass",
                )
                implementation_task = self._prompt_builder.build_implementation_task(
                    user_task=task,
                    answers=answers,
                    workflow_guide=workflow_docs["workflow"],
                    tooling_guide=workflow_docs["tooling"],
                    plan_text=plan_text,
                    review_feedback=review_feedback,
                )
                implement_cmd = self._mini_executor.build_command(implementation_task, phase="implement")
                implement_summary = self._stage_command_summary(phase="implement", cmd=implement_cmd)
                self._store.set_runtime_state(
                    stage=f"implementation-pass-{review_pass}",
                    stage_since=time.time(),
                    command=implement_summary,
                    last_status="running",
                )
                self._store.update_session(
                    session_id,
                    stage=f"implementation-pass-{review_pass}",
                    status="running",
                    review_pass=review_pass,
                    command=implement_summary,
                )
                self._post_thread(
                    channel,
                    thread_ts,
                    f"Implementation stage invocation: {implement_summary}",
                )
                implement_proc = self._mini_executor.run_stage(
                    cmd=implement_cmd,
                    cwd=wt_path,
                    env=self._mini_executor.mini_env(),
                    timeout_seconds=self._config.task_timeout_seconds,
                    stage_label=f"implementation pass {review_pass}",
                    post_progress=lambda text: self._post_thread(channel, thread_ts, text),
                )
                update_ck_telemetry(f"implementation-pass-{review_pass}")
                implement_stdout = self._tail(implement_proc.stdout, self._config.max_stdout_chars)
                implement_stderr = self._tail(implement_proc.stderr, self._config.max_stderr_chars)
                if implement_proc.returncode != 0:
                    self._store.set_runtime_state(
                        running=False,
                        stage="implementation-failed",
                        stage_since=time.time(),
                        command="",
                        last_status=f"failed (implement exit {implement_proc.returncode})",
                        last_error=f"Implementation stage failed (exit {implement_proc.returncode}).",
                        last_stdout=implement_stdout,
                        last_stderr=implement_stderr,
                    )
                    self._store.update_session(
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
                    self._post_thread(channel, thread_ts, message)
                    return False, False, wt_path

                if not self._has_meaningful_changes(wt_path):
                    no_change_issue = (
                        "No repository changes were detected after this implementation pass. "
                        "Apply the planned code/test edits before ending the phase."
                    )
                    review_feedback = [no_change_issue]
                    if review_pass >= self._config.max_implement_review_loops:
                        self._store.set_runtime_state(
                            running=False,
                            stage="implementation-no-changes",
                            stage_since=time.time(),
                            command="",
                            last_status="failed (implementation made no changes)",
                            last_error=no_change_issue,
                            last_stdout=implement_stdout,
                            last_stderr=implement_stderr,
                        )
                        self._store.update_session(
                            session_id,
                            state="failed",
                            stage="implementation-no-changes",
                            status="failed (implementation made no changes)",
                            last_error=no_change_issue,
                            last_stdout=implement_stdout,
                            last_stderr=implement_stderr,
                            review_pass=review_pass,
                        )
                        self._post_thread(
                            channel,
                            thread_ts,
                            (
                                "Implementation did not produce repository changes after the final retry. "
                                "Stopping before review/test to avoid wasted cycles.\n"
                                f"Issue:\n- {no_change_issue}"
                            ),
                        )
                        return False, False, wt_path

                    self._post_thread(
                        channel,
                        thread_ts,
                        (
                            f"Implementation pass {review_pass}: no code changes detected. "
                            "Skipping review and retrying implementation with explicit feedback."
                        ),
                    )
                    continue

                self._post_thread(
                    channel,
                    thread_ts,
                    f"Loop {review_pass}/{self._config.max_implement_review_loops}: review pass",
                )
                review_output_path = Path(wt_path) / self._config.review_output_filename
                if review_output_path.exists():
                    review_output_path.unlink()
                review_task = self._prompt_builder.build_review_task(
                    user_task=task,
                    answers=answers,
                    workflow_guide=workflow_docs["workflow"],
                    review_guide=workflow_docs["review"],
                    tooling_guide=workflow_docs["tooling"],
                    plan_text=plan_text,
                )
                review_cmd = self._mini_executor.build_command(review_task, phase="review")
                review_summary = self._stage_command_summary(phase="review", cmd=review_cmd)
                self._store.set_runtime_state(
                    stage=f"review-pass-{review_pass}",
                    stage_since=time.time(),
                    command=review_summary,
                    last_status="running",
                )
                self._store.update_session(
                    session_id,
                    stage=f"review-pass-{review_pass}",
                    status="running",
                    review_pass=review_pass,
                    command=review_summary,
                )
                self._post_thread(channel, thread_ts, f"Review stage invocation: {review_summary}")
                review_proc = self._mini_executor.run_stage(
                    cmd=review_cmd,
                    cwd=wt_path,
                    env=self._mini_executor.mini_env(),
                    timeout_seconds=self._config.task_timeout_seconds,
                    stage_label=f"review pass {review_pass}",
                    post_progress=lambda text: self._post_thread(channel, thread_ts, text),
                )
                update_ck_telemetry(f"review-pass-{review_pass}")
                review_stdout = self._tail(review_proc.stdout, self._config.max_stdout_chars)
                review_stderr = self._tail(review_proc.stderr, self._config.max_stderr_chars)
                if review_proc.returncode != 0:
                    self._store.set_runtime_state(
                        running=False,
                        stage="review-failed",
                        stage_since=time.time(),
                        command="",
                        last_status=f"failed (review exit {review_proc.returncode})",
                        last_error=f"Review stage failed (exit {review_proc.returncode}).",
                        last_stdout=review_stdout,
                        last_stderr=review_stderr,
                    )
                    self._store.update_session(
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
                    self._post_thread(channel, thread_ts, message)
                    return False, False, wt_path

                review_output = load_review_output(
                    worktree_path=wt_path,
                    review_output_filename=self._config.review_output_filename,
                )
                if review_output["status"] == "approved":
                    approved = True
                    review_feedback = []
                    self._post_thread(channel, thread_ts, f"Review pass {review_pass}: approved.")
                    break

                review_feedback = review_output["issues"]
                feedback_block = "\n".join(f"- {item}" for item in review_feedback)
                self._post_thread(
                    channel,
                    thread_ts,
                    (
                        f"Review pass {review_pass}: changes requested. "
                        "Will run another implementation pass.\n"
                        f"Issues:\n{feedback_block}"
                    ),
                )

            if not approved:
                self._post_thread(
                    channel,
                    thread_ts,
                    (
                        "Reached max implement/review loops without explicit review approval. "
                        "Proceeding to test + PR with latest implementation."
                    ),
                )

            self._post_thread(channel, thread_ts, "Stage: test + PR")
            test_pr_task = self._prompt_builder.build_test_pr_task(
                user_task=task,
                answers=answers,
                workflow_guide=workflow_docs["workflow"],
                review_guide=workflow_docs["review"],
                tooling_guide=workflow_docs["tooling"],
                plan_text=plan_text,
            )
            test_pr_cmd = self._mini_executor.build_command(test_pr_task, phase="implement")
            test_pr_summary = self._stage_command_summary(phase="test-pr", cmd=test_pr_cmd)
            self._store.set_runtime_state(
                stage="test-pr",
                stage_since=time.time(),
                command=test_pr_summary,
                last_status="running",
            )
            self._store.update_session(
                session_id,
                stage="test-pr",
                status="running",
                review_pass=review_pass,
                command=test_pr_summary,
            )
            self._post_thread(channel, thread_ts, f"Test/PR stage invocation: {test_pr_summary}")
            proc = self._mini_executor.run_stage(
                cmd=test_pr_cmd,
                cwd=wt_path,
                env=self._mini_executor.mini_env(),
                timeout_seconds=self._config.task_timeout_seconds,
                stage_label="test+pr",
                post_progress=lambda text: self._post_thread(channel, thread_ts, text),
            )
            update_ck_telemetry("test-pr")
            delivery_issue = ""
            if proc.returncode == 0:
                delivery_issue = self._validate_delivery(
                    worktree_path=wt_path,
                    base_branch=branch,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
                if delivery_issue:
                    proc = subprocess.CompletedProcess(
                        args=proc.args,
                        returncode=90,
                        stdout=proc.stdout,
                        stderr=(proc.stderr + ("\n" if proc.stderr else "") + delivery_issue),
                    )
            stdout = self._tail(proc.stdout, self._config.max_stdout_chars)
            stderr = self._tail(proc.stderr, self._config.max_stderr_chars)
            status = "completed" if proc.returncode == 0 else f"failed (exit {proc.returncode})"
            self._store.set_runtime_state(
                running=False,
                stage="completed" if proc.returncode == 0 else "execution-failed",
                stage_since=time.time(),
                command="",
                last_status=status,
                last_error="" if proc.returncode == 0 else f"Execution failed (exit {proc.returncode}).",
                last_stdout=stdout,
                last_stderr=stderr,
            )
            self._store.update_session(
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
            self._post_thread(channel, thread_ts, message)
            if proc.returncode != 0 and self._config.keep_worktree_on_failure:
                keep_worktree = True
                self._post_thread(
                    channel,
                    thread_ts,
                    f"Keeping failed worktree for inspection: `{wt_path}`",
                )
            return keep_worktree, False, wt_path
        except subprocess.TimeoutExpired:
            self._store.set_runtime_state(
                running=False,
                stage="timeout",
                stage_since=time.time(),
                command="",
                last_status="timed_out",
                last_error=f"Timed out after {self._config.task_timeout_seconds} seconds.",
            )
            self._store.update_session(
                session_id,
                state="timeout",
                stage="timeout",
                status="timed_out",
                last_error=f"Timed out after {self._config.task_timeout_seconds} seconds.",
            )
            self._post_thread(
                channel,
                thread_ts,
                f"Timed out after {self._config.task_timeout_seconds} seconds.",
            )
            return False, False, wt_path
        except Exception as exc:
            self._store.set_runtime_state(
                running=False,
                stage="runner-error",
                stage_since=time.time(),
                command="",
                last_status="runner_error",
                last_error=str(exc),
            )
            self._store.update_session(
                session_id,
                state="failed",
                stage="runner-error",
                status="runner_error",
                last_error=str(exc),
            )
            self._post_thread(channel, thread_ts, f"Runner error: `{exc}`")
            return False, False, wt_path

    @staticmethod
    def _tail(text: str, max_chars: int) -> str:
        if not text:
            return ""
        return text[-max_chars:] if len(text) > max_chars else text

    @staticmethod
    def _run_quiet(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=180)

    def _ref_exists(self, repo_path: str, ref: str) -> bool:
        proc = self._run_quiet(["git", "-C", repo_path, "rev-parse", "--verify", "--quiet", ref])
        return proc.returncode == 0

    def _has_meaningful_changes(self, repo_path: str) -> bool:
        proc = self._run_quiet(["git", "-C", repo_path, "status", "--porcelain"])
        if proc.returncode != 0:
            details = (proc.stderr or proc.stdout).strip() or "no output"
            raise RuntimeError(f"Failed to inspect worktree changes: {details}")

        ignored_paths = {
            self._config.plan_output_filename,
            self._config.review_output_filename,
        }
        for raw in proc.stdout.splitlines():
            line = raw.rstrip()
            if len(line) < 4:
                continue
            path_text = line[3:].strip()
            if " -> " in path_text:
                path_text = path_text.split(" -> ", 1)[1].strip()
            if path_text in ignored_paths:
                continue
            return True
        return False

    @staticmethod
    def _stage_command_summary(*, phase: str, cmd: list[str]) -> str:
        model_class = WorkflowRunner._extract_arg_value(cmd, "--model-class") or "(default)"
        model_name = WorkflowRunner._extract_arg_value(cmd, "-m") or "(default)"
        task_text = WorkflowRunner._extract_arg_value(cmd, "-t")
        task_chars = len(task_text) if task_text else 0
        flags: list[str] = []
        if "-y" in cmd:
            flags.append("-y")
        if "--exit-immediately" in cmd:
            flags.append("--exit-immediately")
        flags_text = ",".join(flags) if flags else "(none)"
        return (
            f"phase={phase} model_class={model_class} model={model_name} "
            f"flags={flags_text} task_chars={task_chars}"
        )

    @staticmethod
    def _extract_arg_value(cmd: list[str], flag: str) -> str:
        for idx in range(len(cmd) - 1):
            if cmd[idx] == flag:
                return cmd[idx + 1]
        return ""

    def _extract_ck_commands_from_trajectory(self) -> list[str]:
        traj_path = Path(self._config.mini_trajectory_path).expanduser()
        if not traj_path.exists():
            return []
        try:
            raw = traj_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:
            return []

        hits: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                cmd_value = node.get("cmd")
                if isinstance(cmd_value, str):
                    trimmed = cmd_value.strip()
                    if trimmed and self._is_ck_invocation(trimmed) and trimmed not in hits:
                        hits.append(trimmed)
                for value in node.values():
                    walk(value)
                return
            if isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        return hits

    @staticmethod
    def _is_ck_invocation(command_text: str) -> bool:
        pattern = re.compile(
            r"(^|\s)(ck|~/.ck/bin/ck|[^ \t\n\r]*ck\.exe)(\s|$)",
            flags=re.IGNORECASE,
        )
        return bool(pattern.search(command_text))

    def _validate_delivery(
        self,
        *,
        worktree_path: str,
        base_branch: str,
        stdout: str,
        stderr: str,
    ) -> str:
        if self._has_meaningful_changes(worktree_path):
            return (
                "Delivery check failed: working tree still has uncommitted changes after test/PR stage. "
                "Expected committed changes pushed to a remote branch."
            )

        head_proc = self._run_quiet(["git", "-C", worktree_path, "rev-parse", "HEAD"])
        base_proc = self._run_quiet(["git", "-C", worktree_path, "rev-parse", f"origin/{base_branch}"])
        if head_proc.returncode != 0 or base_proc.returncode != 0:
            return "Delivery check failed: unable to resolve HEAD/origin branch commit for verification."
        head_sha = head_proc.stdout.strip()
        base_sha = base_proc.stdout.strip()
        if head_sha == base_sha:
            return (
                "Delivery check failed: HEAD equals origin base branch, so no new committed deliverable was produced."
            )

        combined = f"{stdout}\n{stderr}"
        pr_pattern = re.compile(
            r"https?://github\.com/[^\s/]+/[^\s/]+/pull/(?:\d+|new/[^\s`]+)",
            flags=re.IGNORECASE,
        )
        if pr_pattern.search(combined):
            return ""

        blocker_pattern = re.compile(
            r"(blocker|cannot create pr|failed to push|push failed|gh cli not available|no github token|not authenticated|authentication failed)",
            flags=re.IGNORECASE,
        )
        if blocker_pattern.search(combined):
            return (
                "Delivery check failed: PR was not created. A blocker was reported instead; "
                "run is marked failed so delivery is explicit."
            )

        return (
            "Delivery check failed: no PR URL or delivery blocker was reported by test/PR stage output."
        )
