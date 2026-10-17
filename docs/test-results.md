# Implementation verification report

Date: October 17, 2026. Scope: the seven stdio MCP tools backed by the iTerm2 Python API. CLI internal-state detection and plugin modifications are outside this scope.

## Defect and fix

Previously, if resolving the iTerm app failed after connecting, the backend discarded its connection reference without releasing the dispatcher and websocket. All initialization failure paths now perform bounded cleanup before returning an error, including DomainError, cancellation, and a missing app result. Repeated cancellation does not interrupt cleanup. Cleanup failure, timeout, or a self-cancelled websocket close blocks reconnection until the service restarts; successful cleanup allows a fresh connection. Cleanup waits at most two seconds. It cannot guarantee release of third-party SDK resources that ignore cancellation.

## Automated verification

Command: `.venv/bin/python -m unittest discover -s tests -v`. All 69 tests passed.

Coverage includes MCP tool registration, a real stdio handshake, stdout isolation, read-only mode, session restrictions, all 12 key mappings, text/history/command parameter limits, creation arguments, no write retries, serialization, screen and cursor reads, history transactions, session lifecycle, and connection cleanup. New regression tests cover initialization errors, cancellation, repeated cancellation, reconnection, missing app results, and cleanup errors, timeouts, and self-cancellation.

`ruff check src tests scripts` and `.venv/bin/python -m pip check` also passed.

The wheel built for this verification passed. SHA-256: `92be388ccf0cea21707319fdd1ad8317f934a99ca19d9a38824bea809d3ceccd`. This identifies the pre-release artifact tested at that time, not the subsequently published 0.1.0 distribution.

## Live iTerm verification

Command: `.venv/bin/python scripts/smoke_iterm.py --run`, exit code 0. A real stdio MCP subprocess exercised all seven tools, sending test content only to newly created `/bin/cat` sessions.

| Check | Result |
| --- | --- |
| Tool discovery | Seven tools |
| Session listing, window creation, tab creation in a specified window | Passed |
| Exact text and Enter input | API acknowledged |
| Chinese/emoji screen content matching | Passed |
| Nonempty scrollback content matching | Passed |
| Closing sessions created by the test | Two closures confirmed |

Automated tests verify the byte mappings for all supported keys. Live terminal testing exercises Enter only; foreground programs' handling of control keys is not an MCP completion guarantee. An API acknowledgement still does not prove that a foreground command completed.
