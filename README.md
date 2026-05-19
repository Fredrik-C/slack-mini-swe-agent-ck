# mini-swe-agent Slack Relay

This repository contains a small Python worker that listens to Slack mentions via Socket Mode and runs `mini-swe-agent` tasks on a headless Ubuntu server.

It does not require inbound webhooks or a public server URL.

> This project is a proof-of-concept (POC) meant for inspiration and experimentation. Treat it as a starting point, not a production-ready system.

## What It Does

- Receives `app_mention` events in Slack.
- Parses `repo=<alias>` and optional `branch=<name>` from the mention text.
- Runs a workflow per task: plan => implement => review (iterate implement/review up to 3 total review passes) => test => create PR.
- Treats requested `branch=` as PR base branch, then creates an ephemeral delivery branch for test/PR.
- Uses separate `mini -t "<task>"` runs for plan, implement, review, and test/PR stages.
- Injects a tooling guide into each phase prompt (language build/test matrix + Context King search protocol).
- Creates a dedicated git worktree per task, runs in that worktree, then removes it.
- If planning needs clarification, asks questions in the same Slack thread and resumes after user reply.
- Posts completion status and output back to the same Slack thread.
- Exposes a simple streaming web UI for session/task status and live stage output tails.

## Container-First Deployment (Recommended)

If you do not want Python/.NET/Node SDKs installed on the Ubuntu host, run this as a container.
The included image contains:

- Python runtime + pip
- .NET SDK 8/9/10 (side-by-side)
- Node.js + npm + TypeScript CLI (`tsc`)
- Context King CLI (`ck`)
- GitHub CLI (`gh`)
- git/bash/curl/jq

Host requirement: Docker + Docker Compose only.

## 1) Create and Configure Slack App

Create a Slack app and enable Socket Mode:

1. Go to https://api.slack.com/apps and create an app.
2. Enable **Socket Mode**.
3. Generate an app-level token with scope `connections:write` (this is your `xapp-...` token).
4. In **OAuth & Permissions**, add bot scopes:
   - `app_mentions:read`
   - `chat:write`
5. In **Event Subscriptions**, subscribe to bot event:
   - `app_mention`
6. Install the app to your workspace.
7. Copy:
   - Bot token (`xoxb-...`)
   - App token (`xapp-...`)

## 2) Prepare Host Folders (Container Mode)

Pick the host UID/GID that should own repo/worktree files:

```bash
export PUID="$(id -u)"
export PGID="$(id -g)"
```

```bash
sudo mkdir -p /srv/agent/repos
sudo mkdir -p /srv/agent/worktrees
sudo chown -R "$PUID":"$PGID" /srv/agent
```

Clone your target repositories under `/srv/agent/repos`:

```bash
git clone <repo-url> /srv/agent/repos/platform
git clone <repo-url> /srv/agent/repos/api
```

If you use private GitHub repositories, configure git authentication for the runtime user before starting the relay:

- Host mode: configure SSH keys or credential helper/PAT for the user running `python app.py`.
- Docker mode: configure credentials for the same UID/GID used by `PUID`/`PGID` (the container runs as `appuser` with that mapped identity).
- Validate from the runtime context with:

```bash
git -C /srv/agent/repos/platform fetch --all --prune
```

If this command prompts for credentials or fails, task runs may fail when resolving branches/worktrees.

Important with fresh-state policy:

- The runner always executes `git fetch --all --prune` before creating a worktree.
- `--all` fetches every configured remote (not just `origin`).
- If any remote is HTTPS without non-interactive credentials, runs fail with errors like:
  - `fatal: could not read Username for 'https://github.com': No such device or address`

Recommended fix (SSH remotes):

```bash
git -C /srv/agent/repos/platform remote -v
git -C /srv/agent/repos/platform remote set-url origin git@github.com:<owner>/<repo>.git
git -C /srv/agent/repos/platform fetch --all --prune
```

Alternative fix (HTTPS):

- Configure a non-interactive credential helper or token for the runtime user.
- Ensure every configured remote can be fetched without prompt.


## 3) Configure This Relay Project

Clone this relay project and configure env:

```bash
sudo mkdir -p /opt/mini-swe-slack
sudo chown <user>:<group> /opt/mini-swe-slack
git clone <your-repo-url> /opt/mini-swe-slack
cd /opt/mini-swe-slack
cp .env.example .env
nano .env
```

Set at least:

- `SLACK_BOT_TOKEN=xoxb-...`
- `SLACK_APP_TOKEN=xapp-...`
- `PUID=<your host uid>`
- `PGID=<your host gid>`
- `MINI_MODEL_CLASS=openrouter`
- `MINI_MODEL_NAME=openai/gpt-4.1-mini`
- `OPENROUTER_API_KEY=sk-or-v1-...`

Optional per-phase overrides:

- `MINI_PLAN_MODEL_NAME=<model-for-planning>`
- `MINI_IMPLEMENT_MODEL_NAME=<model-for-implementation>`
- `MINI_REVIEW_MODEL_NAME=<model-for-review>`

Create repo allow-list:

```bash
cp repos.example.json repos.json
nano repos.json
```

Use container-visible repo paths (`/repos/...`):

```json
{
  "default_repo": "platform",
  "repos": {
    "platform": {
      "path": "/repos/platform",
      "default_branch": "main",
      "allowed_branches": ["main", "develop", "release/*"]
    },
    "api": {
      "path": "/repos/api",
      "default_branch": "main",
      "allowed_branches": ["main", "hotfix/*"]
    }
  }
}
```

Edit workflow guides (optional, recommended):

```bash
nano prompts/workflow.md
nano prompts/planning.md
nano prompts/review.md
```

These files control:

- overall phase order and delivery expectations (`workflow.md`)
- planning quality and clarification behavior (`planning.md`)
- review checklist before finalizing (`review.md`)

## 4) Run with Docker Compose

```bash
cd /opt/mini-swe-slack
docker compose up -d --build
docker compose logs -f
```

Web UI:

- `http://<host>:8787/`
- JSON API: `http://<host>:8787/sessions.json`

The web UI includes:

- Current runtime state (stage/status/repo/branch/worktree/command)
- Live output tail while a stage is running (stdout/stderr lines as they arrive)
- Per-session details (task, command, errors, output tails, CK telemetry)

Verify non-root runtime:

```bash
docker compose exec mini-swe-slack id
```

Expected: uid/gid should match `PUID`/`PGID` and must not be `0`.

OpenRouter connectivity check:

1. Keep `docker compose logs -f` open.
2. Trigger any real task from Slack (not `repos`/`help`).
3. If API auth is invalid, task output will include the OpenRouter error (invalid key/model/access).

Stop:

```bash
docker compose down
```

## 5) Slack Usage

Mention the bot in Slack:

`@your-bot swe: repo=platform branch=develop run pytest and fix failing tests`

If planning needs clarification, the bot asks follow-up questions in the same thread.
Reply in that thread and mention the bot with your answers to resume execution.
Use `@your-bot cancel` in that thread to abort a pending clarification flow.

`branch=` is optional; if omitted, `default_branch` from `repos.json` is used. This value is the PR base branch.

If `repo=` is omitted, `default_repo` from `repos.json` is used.

If you set `TASK_PREFIX=swe:`, the relay only accepts mentions that start with that prefix.

Utility commands:

- `@your-bot swe: repos`
- `@your-bot swe: list repos`
- `@your-bot swe: status` (alias: `state`, `progress`, `output`, `last output`)
- `@your-bot swe: help`

## Optional: Run Without Docker

If you prefer host-native execution, you can still run:

```bash
sudo apt update
sudo apt install -y python3-venv git
cd /opt/mini-swe-slack
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python app.py
```

## Optional: systemd Service (Host-Native)

Edit `systemd/mini-swe-slack.service` if needed (`User`, paths), then install:

```bash
sudo cp systemd/mini-swe-slack.service /etc/systemd/system/mini-swe-slack.service
sudo systemctl daemon-reload
sudo systemctl enable --now mini-swe-slack
```

Check logs:

```bash
sudo systemctl status mini-swe-slack
sudo journalctl -u mini-swe-slack -f
```

## Configuration

Environment variables in `.env`:

- `SLACK_BOT_TOKEN` (required): Slack bot token (`xoxb-...`).
- `SLACK_APP_TOKEN` (required): Slack app-level token (`xapp-...`).
- `MINI_CMD` (default: `mini`): command used to run mini-swe-agent.
- `MINI_MODEL_CLASS` (recommended: `openrouter`): model class flag passed to `mini` as `--model-class`.
- `MINI_MODEL_NAME` (recommended): model name passed to `mini` as `-m` (for example `openai/gpt-4.1-mini`).
- `OPENROUTER_API_KEY` (required for `MINI_MODEL_CLASS=openrouter`): API key used by OpenRouter model execution.
- `GH_TOKEN` (optional, recommended for auto PR): GitHub token used by `gh` CLI when creating pull requests from the test/PR stage.
- `MINI_PLAN_MODEL_CLASS` / `MINI_PLAN_MODEL_NAME` (optional): overrides used only for planning stage.
- `MINI_IMPLEMENT_MODEL_CLASS` / `MINI_IMPLEMENT_MODEL_NAME` (optional): overrides used for implementation and test/PR stage.
- `MINI_REVIEW_MODEL_CLASS` / `MINI_REVIEW_MODEL_NAME` (optional): overrides used only for review stage.
- `MINI_USE_YOLO` (default: `true`): append `-y` for automatic execution.
- `MINI_EXIT_IMMEDIATELY` (default: `true`): append `--exit-immediately` so non-TTY runs do not block at REPL finish prompt.
- `MSWEA_CONFIGURED` (recommended: `true`): disables mini's interactive first-time setup prompt for non-TTY Slack runs.
- `MSWEA_COST_TRACKING` (default: `ignore_errors`): avoids OpenRouter runs failing when provider responses omit cost metadata.
- `MINI_INFRA_RETRY_MAX` (default: `1`): retries known infrastructure-only mini failures (for example OpenRouter cost-tracking metadata failures).
- `TASK_TIMEOUT_SECONDS` (default: `7200`): timeout per task.
- `PROGRESS_HEARTBEAT_SECONDS` (default: `0`): post in-thread "still running" updates while planning/execution is active (`0` disables).
- `STATUS_OUTPUT_CHARS` (default: `1200`): max output tail included in `status` responses.
- `MAX_STDOUT_CHARS` (default: `3500`): stdout tail sent back to Slack.
- `MAX_STDERR_CHARS` (default: `1500`): stderr tail sent back to Slack.
- `TASK_PREFIX` (optional): only accept tasks starting with this prefix.
- `ALLOW_CHANNEL_IDS` (optional): comma-separated Slack channel allow-list.
- `REPO_CONFIG_PATH` (default: `repos.json`): repo/branch allow-list config file.
- `WORKTREE_ROOT` (default: `.worktrees`): parent folder for temporary task worktrees.
- `GIT_FETCH_BEFORE_WORKTREE` (must be `true`): fresh-state policy requires `git fetch --all --prune` before every worktree; runs fail fast if disabled or fetch fails.
- `KEEP_WORKTREE_ON_FAILURE` (default: `false`): keep failed worktree for debugging.
- `WORKFLOW_GUIDE_PATH` (default: `prompts/workflow.md`): markdown that defines required phase order and workflow expectations.
- `PLAN_GUIDE_PATH` (default: `prompts/planning.md`): markdown that controls planning behavior and question quality.
- `REVIEW_GUIDE_PATH` (default: `prompts/review.md`): markdown used for review quality bar.
- `TOOLING_GUIDE_PATH` (default: `prompts/tooling.md`): markdown injected into all phases for language/runtime build-test guidance and Context King protocol usage.
- `PLAN_OUTPUT_FILENAME` (default: `.mini_workflow_plan.json`): planning-stage JSON handshake file written inside each task worktree.
- `REVIEW_OUTPUT_FILENAME` (default: `.mini_workflow_review.json`): review-stage JSON handshake file written inside each task worktree.
- `MINI_TRAJECTORY_PATH` (default: `~/.config/mini-swe-agent/last_mini_run.traj.json`): trajectory file parsed after each stage to detect CK command invocations for `status` telemetry.
- `MAX_IMPLEMENT_REVIEW_LOOPS` (default: `3`): max implement/review loop count before moving to test/PR.
- `WEB_UI_ENABLED` (default: `true`): enable local web status UI.
- `WEB_UI_BIND` (default: `0.0.0.0`): bind address for UI HTTP server.
- `WEB_UI_PORT` (default: `8787`): UI HTTP server port.
- `WEB_UI_MAX_SESSIONS` (default: `200`): max in-memory session records retained.
- `PUID` / `PGID` (Docker build args): container runtime user identity (non-root).

## OpenRouter Setup

Use OpenRouter directly with:

- `MINI_MODEL_CLASS=openrouter`
- `MINI_MODEL_NAME=<provider/model>` (example: `openai/gpt-4.1-mini`)
- `OPENROUTER_API_KEY=sk-or-v1-...`
- `MSWEA_COST_TRACKING=ignore_errors` (recommended for non-interactive relay runs)

This relay does not require ChatGPT OAuth/device-login when using this route.

## Slack Task Format

Use this format in a mention:

`@your-bot <optional-prefix> repo=<alias> branch=<branch> <task text>`

Examples:

- `@your-bot swe: repo=platform branch=main implement endpoint health checks`
- `@your-bot swe: repo=api fix flaky pytest in payments module`
- `@your-bot swe: branch=develop run lint and fix issues`

Notes:

- Only repos declared in `repos.json` are allowed.
- Branch is checked against `allowed_branches` patterns in the selected repo config.
- During test/PR, the runner creates a dedicated delivery branch from the selected base branch and blocks successful completion if the base branch is pushed directly.
- Each task gets an isolated worktree path and does not reuse previous task filesystem state.
- During planning, the bot may pause and ask clarifying questions; reply in-thread with `@your-bot <answers>`.
- Use `repos`/`list repos` to see current aliases and allowed branch patterns from config.

## Notes

- Socket Mode is outbound-only from your Ubuntu host, so it works behind NAT/firewalls without public inbound ports.
- This worker is intentionally minimal: one queue, one worker thread, sequential task execution.
- For safer operation, use `TASK_PREFIX`, `ALLOW_CHANNEL_IDS`, and strict `allowed_branches` patterns.
- In Docker mode, repo paths inside `repos.json` must match container mount paths (default `/repos/...`).
- In Docker mode, mounted paths must be writable by the selected `PUID`/`PGID`.
- For live runtime visibility, use `docker compose logs -f` on the host while a Slack task is running.
