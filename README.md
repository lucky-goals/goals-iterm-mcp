# goals-iterm-mcp

English | [简体中文](README.zh-CN.md)

Terminal tools for MCP clients, powered by the **iTerm2 Python API**. The `serve` command runs a stdio MCP server for listing sessions, reading terminal output, sending text and keys, and creating windows and tabs.

## Run with uvx

Once version 0.1.0 is available on PyPI, run it without cloning this repository:

```sh
uvx --python 3.12 goals-iterm-mcp@0.1.0 serve
```

The Python import path remains `cc_iterm_mcp`. The previous `cc-iterm-mcp` command is retained as a compatibility alias.

## Installation and startup

Requires macOS, a running iTerm2 instance, and Python 3.12. Enable the Python API in **iTerm2 Settings → General → Magic**, and allow the API authorization request on first connection. See the [official iTerm2 settings documentation](https://iterm2.com/documentation-preferences-general.html).

From the repository directory:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m cc_iterm_mcp serve
```

`serve` waits for MCP protocol messages on stdin/stdout; it is not an interactive shell. Diagnostics go to stderr. Run it without Python auto-reload.

Example MCP client configuration (replace the command with your absolute path):

```json
{
  "mcpServers": {
    "iterm": {
      "command": "/absolute/path/to/goals-iterm-mcp/.venv/bin/python",
      "args": ["-m", "cc_iterm_mcp", "serve"]
    }
  }
}
```

Optional arguments:

- `--read-only`: expose only session discovery and read tools.
- `--session-id <ID>`: restrict access to a specific session; repeat to allow multiple sessions. Window and tab creation is disabled when this restriction is active.

By default, the server can operate on all iTerm sessions belonging to the current user. Every read or send operation requires an exact `session_id` from `list_sessions`; targets are never inferred from focus or titles.

## MCP tools

| Tool | Parameters | Description |
| --- | --- | --- |
| `list_sessions` | None | List session, tab, and window IDs with session metadata. |
| `read_screen` | `session_id` | Read rendered screen lines, dimensions, and cursor position. |
| `read_history` | `session_id`, `lines=100` | Read retained scrollback, up to 2,000 lines. |
| `send_text` | `session_id`, `text` | Send exact text, up to 16 KiB, without appending a newline. |
| `send_keys` | `session_id`, `key` | Send one terminal key sequence. |
| `create_window` | Optional `command` | Create a window using the default profile, optionally with a startup command; return its IDs. |
| `create_tab` | `window_id`, optional `command` | Create a tab in the specified window, optionally with a startup command. |

Supported keys: `enter`, `tab`, `escape`, `backspace`, `up`, `down`, `left`, `right`, `ctrl_c`, `ctrl_d`, `ctrl_u`, and `ctrl_l`.

Embedded newlines, Enter, and control keys are interpreted by the foreground program and may execute commands or exit it. Sends use `suppress_broadcast=True` to target only the specified session. An `api_acknowledged` response confirms the iTerm API call; command completion remains `unknown`. Failed or timed-out sends are never retried automatically.

Screen and history reads return rendered terminal output, not a CLI editor's internal text or raw PTY bytes. Tool operations are serialized within one server process; they are not coordinated with other MCP processes or manual input. Shutting down the server disconnects the API connection without closing user sessions.

## Verification

Run the current automated test suite:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

As of October 17, 2026, all 69 automated tests, Ruff checks, dependency checks, and the wheel build passed after the connection initialization cleanup fix. See the [test report (Chinese)](docs/test-results.md).

Real stdio MCP → iTerm Python API verification covered window and tab creation, text and Enter input, Chinese/emoji screen output, and nonempty scrollback. Both temporary test sessions were closed afterward.

To reproduce the live smoke test, which operates only on newly created `/bin/cat` sessions:

```sh
.venv/bin/python scripts/smoke_iterm.py         # Show the plan only
.venv/bin/python scripts/smoke_iterm.py --run   # Create, test, and close dedicated sessions
```

## Project structure

Runtime modules cover MCP tools, the iTerm backend, read-result types, cursor coordinate conversion, transactions, and connection cleanup. The previous broker/gateway, management UI, CLI certification, and experimental implementations have been removed.

See the [dependency and cleanup record (Chinese)](docs/cleanup-plan.md) for the retention/removal rationale and the [dependency inventory](docs/cleanup-dependencies.json) for the module-level audit.

## License

[MIT](LICENSE) © 2026 lucky-goals.
