from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class MiniExecutorConfig:
    mini_cmd: str
    mini_model_class: str
    mini_model_name: str
    mini_plan_model_class: str
    mini_plan_model_name: str
    mini_implement_model_class: str
    mini_implement_model_name: str
    mini_review_model_class: str
    mini_review_model_name: str
    mini_use_yolo: bool
    mini_exit_immediately: bool
    mswea_configured: str
    mswea_cost_tracking: str
    mini_infra_retry_max: int
    progress_heartbeat_seconds: int


class MiniExecutor:
    def __init__(self, config: MiniExecutorConfig) -> None:
        self._config = config

    def resolve_phase_model(self, phase: str) -> tuple[str, str]:
        phase_key = phase.strip().lower()
        if phase_key == "plan":
            model_class = self._config.mini_plan_model_class or self._config.mini_model_class
            model_name = self._config.mini_plan_model_name or self._config.mini_model_name
            return model_class, model_name
        if phase_key == "implement":
            model_class = self._config.mini_implement_model_class or self._config.mini_model_class
            model_name = self._config.mini_implement_model_name or self._config.mini_model_name
            return model_class, model_name
        if phase_key == "review":
            model_class = self._config.mini_review_model_class or self._config.mini_model_class
            model_name = self._config.mini_review_model_name or self._config.mini_model_name
            return model_class, model_name
        return self._config.mini_model_class, self._config.mini_model_name

    def build_command(self, task: str, phase: str) -> list[str]:
        cmd = shlex.split(self._config.mini_cmd)
        model_class, model_name = self.resolve_phase_model(phase)
        if model_class:
            cmd.extend(["--model-class", model_class])
        if model_name:
            cmd.extend(["-m", model_name])
        if self._config.mini_use_yolo:
            cmd.append("-y")
        if self._config.mini_exit_immediately:
            cmd.append("--exit-immediately")
        cmd.extend(["-t", task])
        return cmd

    def mini_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["MSWEA_CONFIGURED"] = self._config.mswea_configured or "true"
        if self._config.mswea_cost_tracking and not env.get("MSWEA_COST_TRACKING"):
            env["MSWEA_COST_TRACKING"] = self._config.mswea_cost_tracking
        return env

    def run_stage(
        self,
        *,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_seconds: int,
        stage_label: str,
        post_progress: Callable[[str], None],
    ) -> subprocess.CompletedProcess[str]:
        attempts = 0
        stage_env = dict(env)
        while True:
            attempts += 1
            proc = self._run_with_heartbeat(
                cmd=cmd,
                cwd=cwd,
                env=stage_env,
                timeout_seconds=timeout_seconds,
                stage_label=stage_label if attempts == 1 else f"{stage_label} retry {attempts - 1}",
                post_progress=post_progress,
            )
            if proc.returncode == 0:
                return proc
            if attempts > (self._config.mini_infra_retry_max + 1):
                return proc
            retry = self._retry_env_for_mini_failure(proc, stage_env)
            if retry is None:
                return proc
            reason, env_overrides = retry
            stage_env.update(env_overrides)
            override_text = ", ".join(f"{k}={v}" for k, v in env_overrides.items())
            post_progress(
                f"Stage: {stage_label} hit an infrastructure error ({reason}). Retrying with `{override_text}`."
            )

    def _retry_env_for_mini_failure(
        self,
        proc: subprocess.CompletedProcess[str],
        env: dict[str, str],
    ) -> tuple[str, dict[str, str]] | None:
        text = f"{proc.stdout}\n{proc.stderr}".lower()
        if "no valid cost information available from openrouter api" in text:
            current = env.get("MSWEA_COST_TRACKING", "").strip().lower()
            if current != "ignore_errors":
                return (
                    "OpenRouter cost metadata was missing",
                    {"MSWEA_COST_TRACKING": "ignore_errors"},
                )
        return None

    def _run_with_heartbeat(
        self,
        *,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_seconds: int,
        stage_label: str,
        post_progress: Callable[[str], None],
    ) -> subprocess.CompletedProcess[str]:
        result: dict[str, object] = {}

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
            except Exception as exc:  # pragma: no cover - passthrough
                result["error"] = exc

        thread = threading.Thread(target=_runner, daemon=True)
        start = time.time()
        thread.start()

        if self._config.progress_heartbeat_seconds > 0:
            while thread.is_alive():
                thread.join(timeout=self._config.progress_heartbeat_seconds)
                if thread.is_alive():
                    elapsed = int(time.time() - start)
                    post_progress(f"Stage: {stage_label} (still running, {elapsed}s elapsed)")
        else:
            thread.join()

        if "error" in result:
            raise result["error"]  # type: ignore[misc]
        return result["proc"]  # type: ignore[return-value]
