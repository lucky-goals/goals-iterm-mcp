# Implementation verification report

## 0.2.0 release preparation — October 22, 2026

- Re-ran the 69 targeted closing/recovery/MCP tests successfully.
- Built wheel and source distribution; `twine check --strict` passed for both.
- Verified package versions, the four new implementation modules, and CLI entry point.
- Installed the wheel into a separate Python 3.12 environment; dependency checks,
  CLI help, and a real stdio handshake exposing all 12 tools passed.
- Changed-file Ruff correctness checks (`E4,E7,E9,F`) and whitespace checks passed.
- Updated all four README installation guides and both website languages to 0.2.0.

The existing release workflow also runs its required test suite before publishing.
Publication status and downloadable artifacts are recorded in the GitHub Release.

## Closing tools — October 22, 2026

Scope: the four new guarded closing tools and their integration into the
12-tool MCP server, prepared for release as version 0.2.0.

Targeted verification (the full suite was not run):

```sh
.venv/bin/python -m unittest tests.test_closing tests.test_closing_backend tests.test_cli_recovery tests.test_direct -q
```

All 69 targeted tests passed: orchestration (20), backend/process inspection
(21), CLI recovery (15), and MCP contracts (13). Coverage includes scope and
nickname selection, busy/unknown cleanup skips, strict force parameters,
receipt failures, current CLI identity, PID reuse, surviving child processes,
membership changes, cancellation, verified automatic closure, and preservation
of chat UUIDs through a real stdio MCP subprocess. Changed-file Ruff checks and
`git diff --check` passed. Independent review found and verified fixes for the
receipt, process identity, child-process, and automatic-closure paths.

Real iTerm verification:

```sh
.venv/bin/python scripts/smoke_closing.py --run
```

| Check | Result |
| --- | --- |
| Close one session by nickname | Passed |
| Close containing tab by nickname | Passed |
| Close window by exact ID | Passed |
| Dry run leaves the target open and writes no receipt | Passed |
| Default scoped cleanup | One confirmed idle shell closed; one running `sleep` skipped |
| Forced scoped cleanup | Running `sleep` interrupted and its disappearance verified |
| Fresh Cursor CLI in a temporary workspace | UUID returned, private receipt verified, CLI exit and terminal disappearance confirmed |
| Existing user terminals | All six preserved |

The smoke uses five newly created, explicitly allowlisted sessions. It loads
iTerm's bundled shell integration into one disposable shell only, without
changing the user's shell configuration. Its recovery receipts are temporary.
No Python process used `--reload`.

A separate bounded CLI smoke started `cursor-agent --trust --mode ask` in a
new empty temporary directory, submitted no chat prompt, and used only that
new session's exact allowlisted ID. It recovered the current process's chat
UUID, closed it with `force=True`, and verified the returned UUID and receipt.
An initial run without `--trust` produced `CLI_IDENTITY_MISSING`; the smoke
did not attempt the closing tool without that identity. Existing user sessions
were preserved in both runs.

CLI history/resume success is separate from UUID recovery and is not certified
by these checks. Activity without trusted lifecycle evidence stays unknown;
startup arguments and saved-session lists alone cannot prove the current chat.
The adapters refuse missing or ambiguous current identity even with force.

## Historical verification — October 17, 2026

The remaining sections record the earlier seven-tool verification and its
pre-release wheel. They are not a full-suite or package-build claim for the
current closing changes. CLI internal-state detection and plugin modifications
were outside that earlier scope.

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
