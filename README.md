# goals-iterm-mcp

[Website](https://lucky-goals.github.io/goals-iterm-mcp/)
English | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

**Use your coding agent to drive the CLIs on your Mac—even when you are working from your phone.**

goals-iterm-mcp connects MCP-capable agents to **iTerm2 through its Python API**. Describe a task in conversation, and your agent can open a terminal window, launch another coding CLI in a project, send it instructions, and read back its progress and results. This makes it easier to coordinate vibe coding tasks across multiple projects and CLI sessions without manually switching between terminals.

## What you can do

- **Continue work from your phone:** use an agent's mobile or remote interface to direct work in iTerm2 on your Mac, when that interface connects to an agent running with local MCP access.
- **Coordinate coding CLIs:** ask your agent to launch Claude Code, Codex CLI, or another CLI, select a project, and submit a development or diagnosis task.
- **Check progress in conversation:** list windows and tabs, read terminal output, and ask for a summary of what each session is doing.
- **Keep projects in separate sessions:** create dedicated windows or tabs and target them by explicit IDs.
- **Coordinate work toward a persistent goal:** give a supervising agent such as Codex or Claude a goal and clear completion criteria. It can use this MCP to inspect other CLI sessions, assess progress, provide follow-up instructions, and verify results across repeated iterations. The supervising agent manages the goal and continuation; this MCP provides terminal access.

The intended workflow is **phone or desktop conversation → agent with local MCP access → iTerm2 → coding CLI**. You can use this pattern with tools such as Codex, Claude, Cursor, WorkBuddy, Trae, or Hermes where the chosen client supports local stdio MCP and the remote/mobile connection you use. Our manual validation used Codex desktop to operate Claude Code and Codex CLI in iTerm2.

For example, give the supervising agent this goal:

> Guide the coding CLI in the project's dedicated iTerm session until it has diagnosed the login issue, implemented a fix, and passed the relevant tests. Inspect progress between steps, provide follow-up instructions when needed, and verify the final result against these criteria. Do not duplicate tasks that are still running. Ask me when a decision outside this scope is required, and report completion with the changes and test results.

The published 0.2.0 package provides 12 stdio MCP tools: the original session discovery, terminal read/write, creation, and naming tools plus four closing tools. The `main` branch also contains an unreleased incremental read-only `inspect_sessions` tool, bringing the current source to 13 tools; it is not part of PyPI 0.2.0. See the [closing-tools implementation specification](docs/closing-tools-spec.md) for the source contract and the main-only audit details.

## Prerequisites

### Enable the iTerm2 Python API

Use macOS with iTerm2 installed and running. This project has been verified with **iTerm2 3.7.6**.

1. Open **iTerm2 → Settings → General → Magic**.
2. Enable **Python API**.
3. When the MCP server first connects, approve iTerm2's API authorization request if prompted.
4. Keep iTerm2 running while using the MCP tools.

See the [official iTerm2 settings documentation](https://iterm2.com/documentation-preferences-general.html).

### Configure your coding CLIs

Before asking an agent to operate a coding CLI through iTerm2, install that CLI and configure its authentication in the iTerm environment: complete its OAuth login, or configure its API Gateway endpoint and credentials as supported by the CLI. Start it manually in iTerm2 and confirm that it can respond to a simple prompt.

Ensure new iTerm windows and tabs inherit the required configuration. goals-iterm-mcp controls the terminal; each coding CLI uses its own authentication and API configuration. Keep credentials in the CLI's supported credential store or local configuration, rather than including them in agent prompts.

## Run with uvx

Version 0.2.0 is available on [PyPI](https://pypi.org/project/goals-iterm-mcp/). This release includes all 12 tools. Run it without cloning this repository:

```sh
uvx --python 3.12 goals-iterm-mcp@0.2.0 serve
```

The Python import path is `goals_iterm_mcp`, and the command is `goals-iterm-mcp`.

## Install through an agent conversation

Copy the prompt below into an agent that can run local commands and configure your MCP client. Replace `[MCP client]` with your client, such as Codex desktop or Claude Code. Local macOS access is required; a cloud-only chat cannot connect to your local iTerm2.

```text
Please install goals-iterm-mcp 0.2.0 from PyPI and configure it in [MCP client].
Repository: https://github.com/lucky-goals/goals-iterm-mcp

1. Check that this computer runs macOS and has iTerm2 installed and running.
   Check for uv/uvx; if missing, install uv using its official instructions.
   Use Python 3.12. Do not clone the repository or require a project .venv.
2. Verify the package with:
   uvx --python 3.12 goals-iterm-mcp@0.2.0 --help
3. Inspect the client's existing MCP configuration and preserve unrelated settings.
   Add or update one user-level stdio server named "iterm", avoiding duplicates:
   command: the absolute path to uvx on this computer
   args: ["--python", "3.12", "goals-iterm-mcp@0.2.0", "serve"]
   Use the client's supported configuration format and location. Back up an
   existing configuration before editing; do not change other servers.
4. Guide me to enable iTerm2's Python API and approve its authorization dialog
   if needed. Reload the MCP connection using the client's supported mechanism;
   tell me if I need to restart the client myself.
5. Verify the MCP handshake, discovery of the installed version’s tools, and a read-only
   list_sessions call. Do not send terminal input, read session contents, or
   create/close windows as part of installation verification.
6. Report the configuration location, launch command, and verification results.
   Distinguish a successful standalone connection from tools actually loaded
   in the target client. If blocked, explain the specific remaining step.
```

Installing this public package does not require a PyPI account or API token. The configured server exposes the installed version’s tools; the installation check above only lists sessions.

## Update an existing installation

First inspect the existing `iterm` server configuration and back it up. Update that entry in place; preserve its environment variables, `--read-only`, `--session-id` restrictions, and unrelated servers. Use the same installation method as before.

### Installed with uvx

Check the [PyPI releases](https://pypi.org/project/goals-iterm-mcp/#history) and [GitHub Releases](https://github.com/lucky-goals/goals-iterm-mcp/releases), then verify the latest published package starts:

```sh
uvx --python 3.12 goals-iterm-mcp@latest --help
```

This command does **not** update the MCP client's saved configuration. In its existing `args`, replace `goals-iterm-mcp@0.1.2` (or another old pin) with `goals-iterm-mcp@<VERSION>`, replacing `<VERSION>` with the verified release number. Keep the absolute `uvx` command path and other arguments. Pinning a version makes upgrades deliberate and allows rollback to the previous pin.

Alternatively, configure `goals-iterm-mcp@latest` to follow published updates on future launches. This can change the version at each launch; omitting the version is not equivalent because uvx can reuse a cached tool. See [uv's tool version documentation](https://docs.astral.sh/uv/concepts/tools/#tool-versions). “Latest” means a published package, not unreleased repository changes.

### Installed from source or with pip

For the editable source installation documented below, save local changes and confirm the intended branch first. From the original clone, update and reinstall into the same Python 3.12 environment:

```sh
git pull --ff-only
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m goals_iterm_mcp --help
```

If Git refuses the update, resolve the local changes or branch divergence before continuing. Keep the client's absolute Python path pointing at this environment. If you instead installed a PyPI package with pip, use that server environment's interpreter: `/absolute/path/to/.venv/bin/python -m pip install --upgrade goals-iterm-mcp`; upgrading another environment does not update this MCP server.

### Reload and verify

Restart/reconnect the `iterm` MCP server using the client's supported control, or restart the client if necessary. Updating files does not replace an already running server. For Codex, use **Settings → MCP servers → Restart**, or restart Codex. See the [official MCP configuration guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

After reconnecting, query the tools actually available in that client and run only `list_sessions`. Match the tool list to the installed release; for version 0.2.0, verify all 12 tools, including `close_session`, `close_tab`, `close_window`, and `clean_all_sessions`. A checkout of `main` may additionally expose the unreleased read-only `inspect_sessions` tool (13 tools total); do not treat that source increment as part of PyPI 0.2.0. A standalone handshake does not prove an existing conversation has refreshed its tools. If it still lists the old interfaces, reconnect or start a new conversation and check again. Do not create/close terminals or send input just to verify an upgrade. Never start Python with `--reload`.

You can also ask a local agent to do the update:

```text
Update the existing goals-iterm-mcp in [MCP client] to the latest published release.
Inspect its current launch command and installation method first. Back up and
update the existing iterm entry without duplicates; preserve environment variables,
read-only/session restrictions, and other servers. Verify the release on PyPI and
GitHub, update its version pin or the exact environment it uses, then reload the
MCP connection. Verify actual client tool discovery and a read-only list_sessions
call. Report the version, configuration path, and results; clearly state any
manual restart still required. Do not switch a source install to PyPI silently.
```

## Installation and startup

Complete the [prerequisites](#prerequisites) above. Installing from source requires Python 3.12.

From the repository directory:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m goals_iterm_mcp serve
```

`serve` waits for MCP protocol messages on stdin/stdout; it is not an interactive shell. Diagnostics go to stderr. Run it without Python auto-reload.

Example MCP client configuration (replace the command with your absolute path):

```json
{
  "mcpServers": {
    "iterm": {
      "command": "/absolute/path/to/goals-iterm-mcp/.venv/bin/python",
      "args": ["-m", "goals_iterm_mcp", "serve"]
    }
  }
}
```

Optional arguments:

- `--read-only`: the published 0.2.0 package exposes three read-only tools: `list_sessions`, `read_screen`, and `read_history`. A checkout of `main` additionally exposes the unreleased read-only `inspect_sessions` tool.
- `--session-id <ID>`: restrict access to a specific session; repeat to allow multiple sessions. Window and tab creation is disabled when this restriction is active.

By default, the server can operate on all iTerm sessions belonging to the current user. Read and send operations require an exact `session_id` from `list_sessions`; closing tools also accept the explicit selectors documented below. Targets are never inferred from focus, wildcards, prefixes, or titles.

## MCP tools

| Tool | Parameters | Description |
| --- | --- | --- |
| `list_sessions` | None | List session, tab, and window IDs, readable `name`, and session metadata. |
| `read_screen` | `session_id` | Read rendered screen lines, dimensions, and cursor position. |
| `read_history` | `session_id`, `lines=100` | Read retained scrollback, up to 2,000 lines. |
| `inspect_sessions` | `session_id=None`, `nickname=None` | **Main-only unreleased increment.** Read-only cleanup-capability audit. Select exactly one exact session ID or unique case-folded nickname, or omit both to inspect all authorized visible sessions. Reports bounded prompt/process/CLI evidence, activity, identity and persistence observations, blockers, remediation, and advisory close-readiness; it does not authorize work or closing and never sends input, exits a CLI, writes a receipt, or closes a session. |
| `send_text` | `session_id`, `text` | Send exact text, up to 16 KiB, without appending a newline. |
| `send_keys` | `session_id`, `key` | Send one terminal key sequence. |
| `create_window` | Optional `command` | Create a window using the default profile, optionally with a startup command; return its IDs. |
| `create_tab` | `window_id`, optional `command` | Create a tab in the specified window, optionally with a startup command. |
| `rename_session` | `session_id`, optional `name` | Rename an existing session; omit `name` to choose a random unused nickname. |
| `close_session` | `session_id=None`, `nickname=None`, `force=False`, `dry_run=False` | Close one selected session after CLI exit and native-close verification. Exactly one of `session_id` or `nickname` is required. |
| `close_tab` | `tab_id=None`, `session_id=None`, `nickname=None`, `force=False`, `dry_run=False` | Close the tab containing the selected session or tab. Exactly one selector is required. |
| `close_window` | `window_id=None`, `session_id=None`, `nickname=None`, `force=False`, `dry_run=False` | Close the window containing the selected session or window. Exactly one selector is required. |
| `clean_all_sessions` | `force=False`, `dry_run=False` | Inspect the initial session snapshot and clean authorized sessions. By default, skip busy, unknown, or unrecoverable sessions; `force=true` attempts every authorized session in that snapshot. |

### Read-only session capability audit

`inspect_sessions` is an advisory, read-only diagnostic in the current `main` source and is not included in the published 0.2.0 package. `session_id` and `nickname` are mutually exclusive. An exact `session_id` or a unique nickname (trimmed and case-folded) selects one authorized session; omitting both inspects every currently authorized visible session. Invalid, ambiguous, unavailable, or out-of-scope selectors fail without terminal writes.

Each result includes the session, tab, and window identity plus bounded `prompt`, `process`, `cli`, `terminal_activity`, `business_acceptance`, and `close_readiness` sections. The diagnostic may report whether evidence is available, observed activity, stable process/CLI identity, chat persistence observations, a blocker code, and a remediation suggestion. `business_acceptance` is always `not_observed`; `close_readiness` is advisory only, with `status` set to `eligible_if_unchanged`, `blocked`, or `unknown` and `ready` true only for `eligible_if_unchanged`. Even a `ready: true` result is not authorization or proof that a close will succeed. Re-probe immediately before any lifecycle write. The tool reads evidence only: it does not send terminal input, exit a CLI, persist a recovery receipt, or invoke native close.

### Closing tools

The four closing tools are included in version 0.2.0. The complete contract is in the [closing-tools implementation specification](docs/closing-tools-spec.md).

`close_session`, `close_tab`, and `close_window` require exactly one selector. IDs are matched explicitly and are never treated as nickname fallbacks. A nickname is a session-scoped selector that matches the catalogue name after trimming and case-folding; ambiguity is an error. For a tab or window close, a session selector chooses its containing tab or window. There is no focus, wildcard, or prefix targeting. A tab or window containing any session outside the configured allowlist is refused.

Ordinary closes preflight every affected session before sending terminal input. Busy, waiting, or unknown activity refuses the whole targeted operation. `clean_all_sessions` skips busy, unknown, and unrecoverable sessions by default and closes only confirmed idle sessions; it retains the remaining sessions and windows. With `force=true`, cleanup attempts every authorized session in its initial snapshot, while sessions that arrive later are excluded. `force` authorizes interruption through the activity gate only: it does not bypass the allowlist, ambiguous identity, changed targets, or recovery-write failures, and it is not an implicit `SIGKILL`. Native iTerm force-confirmation behavior is separate from this interruption authorization.

Before the first interruption, CLI exit, or native close, the service writes a private recovery receipt containing metadata only. A recognized chat CLI must provide its actual chat ID before interruption or exit; the service does not send chat text or slash commands merely to discover that ID. Plain shells and ordinary jobs have an empty `cli_sessions` array, and unknown CLI identity is never silently downgraded to a shell or job. If current-process or chat identity evidence is unavailable or cannot be tied to the target, the operation fails closed (cleanup may report the session as skipped). Screen silence, age, low CPU, a completed-looking answer, or a session-list `in_progress` value is not completion evidence. CLI exit is bounded and verified; if exit cannot be verified, the service does not issue a native close for that target, although iTerm's configured auto-close-on-exit behavior may still remove a pane as a side effect of the CLI's own exit. The service never kills a shared daemon or a process selected only by name.

Current recovery support is intentionally narrow. Claude and Codex can identify a chat from a trusted process registration with a bound chat ID or a transcript/rollout file opened by the exact current process; a UUID found only in startup arguments is reported but remains blocked. Kiro's saved-session listing and startup arguments do not prove current chat identity, so it requires a process-bound known ID and refuses an unproven identity even with `force`. Cursor's passive path accepts a UUID only from a recognized log file opened by the supplied PID; startup arguments alone are insufficient. CLI task activity is generally `unknown` unless trusted lifecycle evidence supplies it; the adapter does not automatically capture every chat or every running CLI.

Results include `operation`, `status`, `force`, `dry_run`, `target`, recovery receipt IDs and paths, and closed, skipped, failed, and unknown session lists. `status` is one of `closed`, `partial`, `refused`, `noop`, `dry_run`, or `unknown`. Session results include iTerm IDs, nickname, `cli_sessions`, interruption, exit, and closure status. Chat records include `cli`, `chat_uuid`, `cwd`, identity source, persistence status, and a structured resume executable/argv. `chat_uuid` identifies the chat, but it does not by itself prove that history was saved or that the resume command was tested; those are separate result fields. The topology is refreshed after a close, because an API acknowledgement is not closure proof. Lost or timed-out responses become `unknown` and are never retried automatically. Refused, failed, and unknown operations are tool errors with structured data; cleanup skips alone are not errors. Partial results report completed side effects and do not reopen terminals. Dry runs send no input, write no receipts, change no names, and close no objects.

Sessions created with `create_window` or `create_tab` automatically receive a random nickname from a built-in pool of 200 distinct names, each 4–7 characters long. Existing sessions can be named with `rename_session`. Explicit names may contain up to 64 characters; name comparisons ignore case. Names already used by another live session are rejected; closed sessions release their names. If the pool is exhausted, automatic naming fails before creating a session. Closing-tool nickname selectors use the same live session catalogue and trim and case-fold the name before matching.

For read/send operations, first use `list_sessions` to find the exact `session_id`. Closing tools may use an exact nickname or an explicit target ID under the selector rules above; do not infer a target from focus or a title.

Supported keys: `enter`, `tab`, `escape`, `backspace`, `up`, `down`, `left`, `right`, `ctrl_c`, `ctrl_d`, `ctrl_u`, and `ctrl_l`.

Embedded newlines, Enter, and control keys are interpreted by the foreground program and may execute commands or exit it. Sends use `suppress_broadcast=True` to target only the specified session. An `api_acknowledged` response confirms the iTerm API call; command completion remains `unknown`. Failed or timed-out sends are never retried automatically.

Screen and history reads return rendered terminal output, not a CLI editor's internal text or raw PTY bytes. Tool operations are serialized within one server process; they are not coordinated with other MCP processes or manual input. Shutting down the server disconnects the API connection without closing user sessions.

## Verification

Run the targeted tests for the closing tools, recovery adapter, and direct backend:

```sh
.venv/bin/python -m unittest tests.test_closing tests.test_closing_backend tests.test_cli_recovery tests.test_direct -q
```

On October 22, 2026, these 69 targeted tests passed. Changed-file Ruff checks and `git diff --check` also passed; this verification does not claim a full-suite result.

The opt-in closing smoke test creates only new allowlisted iTerm sessions. Show its plan or execute it with:

```sh
.venv/bin/python scripts/smoke_closing.py          # Show the plan only
.venv/bin/python scripts/smoke_closing.py --run   # Run the opt-in native smoke test
```

The October 22, 2026 run passed: three targeted native closes succeeded; default cleanup closed one idle session and skipped one running `sleep` session; forced cleanup then closed one session; all six pre-existing sessions remained.

Earlier real stdio MCP → iTerm Python API verification covered window and tab creation, text and Enter input, Chinese/emoji screen output, and nonempty scrollback. Both temporary test sessions were closed afterward.

To reproduce that earlier live smoke test, which operates only on newly created `/bin/cat` sessions:

```sh
.venv/bin/python scripts/smoke_iterm.py         # Show the plan only
.venv/bin/python scripts/smoke_iterm.py --run   # Create, test, and close dedicated sessions
```

## Project structure

Runtime modules cover MCP tools, the iTerm backend, read-result types, cursor coordinate conversion, transactions, connection cleanup, and the closing-tool safety contract. The previous broker/gateway, management UI, and experimental implementations have been removed; closing-tool CLI identity remains dependent on current process and runtime evidence.

See the [dependency and cleanup record](docs/cleanup-plan.md) for the retention/removal rationale and the [dependency inventory](docs/cleanup-dependencies.json) for the module-level audit.

## License

[MIT](LICENSE) © 2026 lucky-goals.
