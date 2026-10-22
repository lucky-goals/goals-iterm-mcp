# Terminal closing tools — implementation contract

Status: implementation specification, 2026-10-22. This supersedes the earlier
proposal that `clean_all_sessions` refused the entire request when one session
was busy. No package release is implied by this change.

## Public tools

- `close_session(session_id=None, nickname=None, force=False, dry_run=False)`
- `close_tab(tab_id=None, session_id=None, nickname=None, force=False, dry_run=False)`
- `close_window(window_id=None, session_id=None, nickname=None, force=False, dry_run=False)`
- `clean_all_sessions(force=False, dry_run=False)`

Exactly one selector is required for the first three tools. Nicknames match the
catalogue name exactly after trimming/case-folding; ambiguity is an error. A
session selector for a tab/window selects its containing object. Explicit IDs
are never treated as nickname fallbacks. No focus, wildcard or prefix targeting.

## Safety and scope

1. Ordinary closes preflight every affected session before any terminal input.
   Busy, waiting, or unknown activity refuses the whole targeted operation.
2. Cleanup without force skips busy/unknown/unrecoverable sessions and closes
   only confirmed idle sessions. It does not infer usefulness from age, low CPU,
   a silent screen, or a completed-looking answer. Remaining sessions/windows
   are retained. Force cleanup attempts every authorized session in the initial
   snapshot; new arrivals never join that request.
3. `force` bypasses the activity gate, authorizing interruption. It does not
   bypass access scope, ambiguous identity, recovery-write failures or changed
   targets. Native iTerm `force` skips UI confirmation and is a distinct concern.
4. A tab/window containing any session outside the allowlist is refused.
   Cleanup operates only on allowed sessions, never closes a mixed-scope parent.
5. A recognized chat CLI requires its actual chat ID before interruption/exit.
   Plain shells and ordinary jobs have an empty `cli_sessions` array. CLI unknown
   identity must not be silently downgraded to an ordinary shell/job.
6. Do not send chat text or identifier slash commands into a running/unknown
   CLI merely to discover its ID. Passive current-process evidence is preferred;
   any interactive identity probe must prove the input context before writing.
7. Persist a private recovery receipt before the first interruption/exit/close.
   Only metadata is retained, not terminal bodies, credentials or chat content.
8. CLI exit is bounded and verified, including the previously observed scoped
   child processes; failure does not fall through to a native close call.
   iTerm may itself remove a terminal when its command exits. Force is not an implicit SIGKILL. Never kill a
   shared daemon or process selected only by name. Protect the MCP host process
   and its ancestors, since self-termination cannot deliver a trustworthy result.
9. Revalidate identity and membership before writes. Close native tab/window
   only when the original affected scope is still valid and every CLI exited.
   iTerm transactions must not contain UI-producing close/exit operations.
10. Refresh topology after close. A verified exit may itself remove an original
    session; report its confirmed disappearance without issuing another close.
    Remaining original parent members can be closed only after checking that
    no new members arrived. API acknowledgement is not closure proof.
    Timeout/lost responses yield unknown outcomes and never automatic retries.
11. Dry runs do not send input, save receipts, change names or close objects.
    Read-only mode exposes none of these four destructive tools.

## Result contract

Return `operation`, `status` (`closed`, `partial`, `refused`, `noop`, `dry_run`,
or `unknown`), `force`, `dry_run`, `target`, `recovery_record_id`,
`recovery_record_path`, `closed_sessions`, `skipped_sessions`, `failed_sessions`,
`unknown_sessions`, and (where applicable) `code` and `blocking_sessions`.
Each session result includes iTerm IDs, nickname, `cli_sessions`, interruption,
exit and closure status. Each chat record includes `cli`, `chat_uuid`, `cwd`,
identity source, persistence status and a structured resume executable/argv.
An ID alone is not proof of saved history or successful resume.

Refused/failed/unknown operations must be MCP tool errors with structured data,
not success-shaped strings. Cleanup skips alone are not errors. Partial results
report all successful side effects plus remaining failures; nothing is rolled
back by reopening a terminal. Receipts remain usable after a lost response.

## Internal integration contract

`CloseService(backend, allowed=None, store=None)` owns planning and execution.
The backend implements async `snapshot() -> list[dict]`, `probe(row) -> dict`,
`exit_session(row, probe, force) -> dict`, and
`close_target(kind, target_id, expected_rows, force) -> None`.
Rows contain `session_id`, `window_id`, `tab_id`, `name`, and `target` epochs.
Probes contain `state` (idle/busy/unknown/exited), `reason`, `identity` (stable
process identity), `cli_sessions` (list), and optional `blocked_code`.
An exit result contains `cli_exit_status` (not_needed/exited),
`task_interrupted`, and may update chat persistence evidence.
`CloseService.run(kind, *, session_id=None, nickname=None, tab_id=None,
window_id=None, force=False, dry_run=False)` returns the result dictionary;
kind is session/tab/window/all. The public layer holds the existing service
lock, connects once, and maps error statuses to MCP CallToolResult.isError.

## Acceptance tests (TDD)

- IDs and unique nicknames select only intended targets; invalid/duplicate
  selectors and case-folded collisions never write.
- Busy sibling refuses tab/window; closing an idle sibling alone succeeds.
- Default cleanup skips busy and unknown, force cleanup includes busy.
- All four tools honor allowlists/read-only mode and default force=false.
- Recovery receipt precedes every terminal write; persistence failure writes
  nothing; successful/partial/unknown responses preserve chat UUIDs.
- Chat identity missing, background jobs, stale process IDs, changed tab
  membership, denied close, disconnect and cancellation never become success.
- Native close is issued once and disappearance is independently verified.
- CLI shutdown failures retain terminals and report actual side effects.
- Live smoke uses only newly created disposable sessions and exact IDs.

## Compatibility boundary

Task state and identity evidence depend on installed CLI/runtime capabilities.
Adapters fail closed when evidence cannot be tied to the current process/chat;
screen silence and session-list `in_progress` are not task completion signals.
Resuming a chat does not resurrect an interrupted OS process or undo effects.
