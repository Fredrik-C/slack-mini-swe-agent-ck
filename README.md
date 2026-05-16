# mini-swe-agent Slack Relay

This repository contains a small Python worker that listens to Slack mentions via Socket Mode and runs `mini-swe-agent` tasks on a headless Ubuntu server.

It does not require inbound webhooks or a public server URL.

## What It Does

- Receives `app_mention` events in Slack.
- Parses `repo=<alias>` and optional `branch=<name>` from the mention text.
- Runs a workflow per task: plan => implement => self-review => test => create PR.
- Uses `mini -t "<task>"` to execute planning and implementation stages.
- Creates a dedicated git worktree per task, runs in that worktree, then removes it.
- If planning needs clarification, asks questions in the same Slack thread and resumes after user reply.
- Posts completion status and output back to the same Slack thread.

## Container-First Deployment (Recommended)

If you do not want Python/.NET/Node SDKs installed on the Ubuntu host, run this as a container.
The included image contains:

- Python runtime + pip
- .NET SDK 10
- Node.js + npm
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
sudo mkdir -p /srv/agent/chatgpt-auth
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


## 3) Configure This Relay Project

Clone this relay project and configure env:

```bash
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
- `MINI_MODEL_CLASS=litellm_response`
- `MINI_MODEL_NAME=chatgpt/gpt-5.3-codex`

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
- self-review checklist before finalizing (`review.md`)

## 4) Run with Docker Compose

```bash
cd /opt/mini-swe-slack
docker compose up -d --build
docker compose logs -f
```

Verify non-root runtime:

```bash
docker compose exec mini-swe-slack id
```

Expected: uid/gid should match `PUID`/`PGID` and must not be `0`.

First-time ChatGPT OAuth login (one-time):

1. Keep `docker compose logs -f` open.
2. Trigger any real task from Slack (not `repos`/`help`).
3. In logs, LiteLLM will print a device login prompt with URL/code.
4. Complete that login in browser.
5. OAuth tokens are persisted in `/srv/agent/chatgpt-auth` and reused across restarts.

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

`branch=` is optional; if omitted, `default_branch` from `repos.json` is used.

If `repo=` is omitted, `default_repo` from `repos.json` is used.

If you set `TASK_PREFIX=swe:`, the relay only accepts mentions that start with that prefix.

Utility commands:

- `@your-bot swe: repos`
- `@your-bot swe: list repos`
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
- `MINI_MODEL_CLASS` (optional): model class flag passed to `mini` as `--model-class` (set `litellm_response` for ChatGPT responses route).
- `MINI_MODEL_NAME` (optional): model name passed to `mini` as `-m` (for example `chatgpt/gpt-5.3-codex`).
- `MINI_USE_YOLO` (default: `true`): append `-y` for automatic execution.
- `MSWEA_CONFIGURED` (recommended: `true`): disables mini's interactive first-time setup prompt for non-TTY Slack runs.
- `TASK_TIMEOUT_SECONDS` (default: `7200`): timeout per task.
- `MAX_STDOUT_CHARS` (default: `3500`): stdout tail sent back to Slack.
- `MAX_STDERR_CHARS` (default: `1500`): stderr tail sent back to Slack.
- `TASK_PREFIX` (optional): only accept tasks starting with this prefix.
- `ALLOW_CHANNEL_IDS` (optional): comma-separated Slack channel allow-list.
- `REPO_CONFIG_PATH` (default: `repos.json`): repo/branch allow-list config file.
- `WORKTREE_ROOT` (default: `.worktrees`): parent folder for temporary task worktrees.
- `GIT_FETCH_BEFORE_WORKTREE` (default: `true`): run `git fetch --all --prune` before creating worktree (task fails if fetch fails).
- `KEEP_WORKTREE_ON_FAILURE` (default: `false`): keep failed worktree for debugging.
- `WORKFLOW_GUIDE_PATH` (default: `prompts/workflow.md`): markdown that defines required phase order and workflow expectations.
- `PLAN_GUIDE_PATH` (default: `prompts/planning.md`): markdown that controls planning behavior and question quality.
- `REVIEW_GUIDE_PATH` (default: `prompts/review.md`): markdown used for self-review quality bar.
- `PLAN_OUTPUT_FILENAME` (default: `.mini_workflow_plan.json`): planning-stage JSON handshake file written inside each task worktree.
- `PUID` / `PGID` (Docker build args): container runtime user identity (non-root).

## ChatGPT Responses Setup

For the ChatGPT subscription auth route discussed earlier, use:

- `MINI_MODEL_CLASS=litellm_response`
- `MINI_MODEL_NAME=chatgpt/gpt-5.3-codex` (or another `chatgpt/...` model supported by your LiteLLM version)

This avoids `/chat/completions` bridge issues by using Responses-mode model handling.

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
- Each task gets an isolated worktree path and does not reuse previous task filesystem state.
- During planning, the bot may pause and ask clarifying questions; reply in-thread with `@your-bot <answers>`.
- Use `repos`/`list repos` to see current aliases and allowed branch patterns from config.

## Notes

- Socket Mode is outbound-only from your Ubuntu host, so it works behind NAT/firewalls without public inbound ports.
- This worker is intentionally minimal: one queue, one worker thread, sequential task execution.
- For safer operation, use `TASK_PREFIX`, `ALLOW_CHANNEL_IDS`, and strict `allowed_branches` patterns.
- In Docker mode, repo paths inside `repos.json` must match container mount paths (default `/repos/...`).
- In Docker mode, mounted paths must be writable by the selected `PUID`/`PGID`.
