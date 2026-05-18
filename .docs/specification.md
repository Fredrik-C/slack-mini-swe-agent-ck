# Integrating Context King into SWE-agent

A practical guide for bundling [Context King](https://github.com/Fredrik-C/ContextKing) as a native SWE-agent tool, enabling intelligent file discovery on large codebases.

---

## Why this matters

SWE-agent's default file navigation (grep, find, windowed file viewer) degrades significantly on large monorepos. On a codebase like the Mews C# monolith (~20,000 files), the agent wastes a large portion of its context window on exploration before it can do any actual work.

Context King solves this directly: it uses folder-level bag-of-words embeddings built from paths, filenames, and exported method names to return the most relevant files for a given query — with no code leaving the machine. Bundling it as a SWE-agent tool makes it a first-class part of the agent's ACI (Agent-Computer Interface), so the model can invoke it naturally before editing.

---

## SWE-agent tool bundle structure

A tool bundle is a folder with this layout:

```
tools/context_king/
├── bin/
│   ├── context_king          # the main tool executable
│   └── state                 # optional: state command run after every action
├── config.yaml               # tool schema — what the model sees
├── install.sh                # runs once when the sandbox container starts
└── README.md
```

---

## Step 1: Build the bundle

### `install.sh`

Installs Context King into the sandbox container. Adjust the install method to match your CK distribution (binary release, dotnet tool, etc.).

```bash
#!/usr/bin/env bash
set -e

# Option A: install as a .NET global tool (if published to NuGet)
dotnet tool install --global ContextKing

# Option B: copy a self-contained binary (preferred for reproducibility)
# The binary is expected to already be in bin/ alongside this script
chmod +x "$(dirname "$0")/bin/context_king"

echo "Context King installed."
```

### `bin/context_king`

This is the executable the agent calls. It should be a thin wrapper if the real binary has a different name or needs environment setup:

```bash
#!/usr/bin/env bash
# Wrapper — adjust path if CK binary has a different name
exec context-king "$@"
```

If you're shipping a self-contained binary, name it `context_king` directly and skip the wrapper.

### `config.yaml`

This is the most important file — it defines what the model sees when deciding whether and how to call the tool.

```yaml
tools:
  context_king:
    signature: "context_king <query> [--top <n>] [--root <path>]"
    docstring: |
      Search for the most relevant files in the codebase for a given task or concept.
      Uses semantic folder-level indexing (paths, filenames, exported method names).
      Call this BEFORE reading or editing files when you don't already know which
      files are relevant. Returns a ranked list of file paths.

      Examples:
        context_king "Adyen terminal payment flow"
        context_king "mass transit consumer registration" --top 20
        context_king "hotel reservation cancellation" --root src/
    arguments:
      - name: query
        type: string
        description: Natural language description of the task or concept to find files for.
        required: true
      - name: --top
        type: integer
        description: Number of results to return. Defaults to 10.
        required: false
      - name: --root
        type: string
        description: Root directory to search within. Defaults to the repo root.
        required: false
```

> The `docstring` is injected verbatim into the model's system prompt via `{{command_docs}}`. Write it to guide the model's decision-making, not just to describe the syntax.

---

## Step 2: Register the bundle in your config YAML

Create or extend a SWE-agent config file to include the Context King bundle:

```yaml
# config/mews_monolith.yaml

agent:
  templates:
    system_template: |-
      You are a helpful assistant that can interact with a computer to solve software engineering tasks.
      For large codebases, always use context_king to identify relevant files before reading or editing.

    instance_template: |-
      <uploaded_files>
      {{working_dir}}
      </uploaded_files>

      Repository: Mews C# monolith (~20,000 files)

      Task:
      {{problem_statement}}

      Start by using context_king to locate relevant files, then read and edit as needed.

    next_step_template: |-
      OBSERVATION:
      {{observation}}

  tools:
    env_variables:
      PAGER: cat
      MANPAGER: cat
      GIT_PAGER: cat
      PIP_PROGRESS_BAR: 'off'
      TQDM_DISABLE: '1'
    bundles:
      - path: tools/registry          # always first
      - path: tools/edit_anthropic    # SWE-agent's default editor
      - path: tools/context_king      # your new bundle
      - path: tools/review_on_submit_m
    registry_variables:
      USE_FILEMAP: 'true'
    enable_bash_tool: true

  parse_function:
    type: function_calling

  history_processors:
    - type: cache_control
      last_n_messages: 2
```

---

## Step 3: Index the repository

Context King needs to build its index before the agent runs. Do this once per repo (and re-run when the codebase changes significantly):

```bash
# Run from the repo root
context-king index .

# Or target a specific subtree
context-king index ./src
```

The index is written to a local file (no network calls). Include the index file in your Docker image or mount it into the SWE-ReX container so it's available at agent startup.

If you're using SWE-ReX with a local Docker container, add the index step to `install.sh`:

```bash
# At the end of install.sh, after installing CK:
echo "Building Context King index..."
context-king index "${REPO_ROOT:-.}"
echo "Index built."
```

---

## Step 4: Run SWE-agent with the custom config

```bash
sweagent run \
  --config config/mews_monolith.yaml \
  --problem-statement "Fix the Adyen terminal flow timeout handling" \
  --repo-path /path/to/mews-repo
```

Or for batch runs:

```bash
sweagent run-batch \
  --config config/mews_monolith.yaml \
  --instances issues.jsonl
```

---

## How the agent uses it

With the bundle registered, the model's system prompt includes the `context_king` tool signature and docstring alongside all other tools. A typical trajectory looks like:

```
[agent thought]
I need to find the files related to Adyen terminal payment processing.

[tool call]
context_king "Adyen terminal payment processing timeout"

[observation]
src/Payments/Adyen/TerminalPaymentProcessor.cs  (score: 0.94)
src/Payments/Adyen/AdyenTerminalClient.cs        (score: 0.89)
src/Payments/Adyen/TerminalPaymentRequest.cs     (score: 0.81)
...

[agent thought]
I'll start by reading TerminalPaymentProcessor.cs.
```

This replaces the default pattern of blind `grep` and `find` calls that burn context before any real reasoning begins.

---

## Caveats and limitations

**Index freshness.** The index reflects the codebase at index time. In an active monolith, stale index entries are a real risk. For PR-scoped tasks, re-indexing the relevant subtree at container startup is a reasonable mitigation.

**C# and TypeScript only.** Context King currently supports C# and TypeScript. If your task touches other file types (configs, SQL, etc.), the agent will still need to fall back to grep/find for those.

**Not a replacement for bash.** Context King answers "which files are relevant?" — it doesn't replace the agent's need to read file contents, run tests, or navigate directory structure. Keep `enable_bash_tool: true`.

**Token budget.** CK's output is a ranked file list, which is compact. The real saving is upstream: fewer wasted bash exploration steps means more of the context budget is available for actual reasoning and editing.

---

## Potential extension: MCP bridge

Context King could also be exposed as an MCP server, which would make it available to Claude Code and other MCP-native agents without any SWE-agent-specific bundling. SWE-agent doesn't speak MCP natively, but a thin stdio shim could bridge the two:

```bash
# bin/context_king (MCP shim variant)
#!/usr/bin/env bash
# Calls a locally running CK MCP server via its HTTP interface
curl -s -X POST http://localhost:3333/query \
  --json "{\"query\": \"$1\", \"top\": ${2:-10}}" \
  | jq -r '.files[].path'
```

This is shimming the protocol manually and adds operational complexity (the MCP server must be running in the container). For SWE-agent, the direct binary bundle is simpler and more robust.