"""Opt-in closing smoke using only newly created, allowlisted iTerm sessions."""

import argparse
import asyncio
from contextlib import redirect_stdout
import json
from pathlib import Path
import shlex
import sys
import tempfile
from uuid import uuid4

from goals_iterm_mcp.closing import RecoveryStore
from goals_iterm_mcp.direct import DirectItermBackend, ItermTools


async def smoke():
    backend = DirectItermBackend(broker_epoch=f"closing-smoke-{uuid4()}")
    created = set()
    results = {}
    with tempfile.TemporaryDirectory(prefix="closing-smoke-") as directory:
        try:
            await backend.connect()
            original = {r["target"]["session_id"] for r in await backend.list_sessions()}

            async def create(kind="window", window_id=None, command="/bin/zsh -f"):
                value = await backend.create(kind, window_id, command,
                                             name=f"smoke-{uuid4().hex[:12]}")
                sid = value["session_id"]
                assert sid not in original
                created.add(sid)
                await asyncio.sleep(1)
                return value

            def service(ids):
                return ItermTools(backend, session_ids=list(ids),
                                  recovery_store=RecoveryStore(directory))

            for kind in ("session", "tab", "window"):
                value = await create()
                scoped = service([value["session_id"]])
                # Nickname selects exactly one pane or its containing tab.
                selector = ({"nickname": value["name"]} if kind != "window"
                            else {"window_id": value["window_id"]})
                planned = await scoped.invoke_close(kind, **selector, force=True, dry_run=True)
                plan = planned.structuredContent
                assert plan["status"] == "dry_run", plan
                assert plan["recovery_record_path"] is None
                assert backend._app.get_session_by_id(value["session_id"]) is not None
                closed = await scoped.invoke_close(kind, **selector, force=True)
                payload = closed.structuredContent
                assert not closed.isError, payload
                assert payload["status"] == "closed", payload
                assert [r["session_id"] for r in payload["closed_sessions"]] == [value["session_id"]]
                results[f"close_{kind}"] = "closed"

            idle = await create()
            integration = Path("/Applications/iTerm.app/Contents/Resources/iterm2_shell_integration.zsh")
            if integration.is_file():
                session = backend._app.get_session_by_id(idle["session_id"])
                await session.async_send_text(f"source {shlex.quote(str(integration))}\r",
                                              suppress_broadcast=True)
                await asyncio.sleep(0.5)
            busy = await create("tab", idle["window_id"], "/bin/sleep 60")
            scoped = service([idle["session_id"], busy["session_id"]])
            cleanup = await scoped.invoke_close("all")
            payload = cleanup.structuredContent
            assert not cleanup.isError, payload
            assert busy["session_id"] in {r["session_id"] for r in payload["skipped_sessions"]}, payload
            results["default_cleanup_closed"] = len(payload["closed_sessions"])
            results["default_cleanup_skipped"] = len(payload["skipped_sessions"])
            if integration.is_file():
                assert idle["session_id"] in {r["session_id"] for r in payload["closed_sessions"]}, payload
            forced = await scoped.invoke_close("all", force=True)
            payload = forced.structuredContent
            assert not forced.isError, payload
            assert payload["status"] == "closed", payload
            results["force_cleanup_closed"] = len(payload["closed_sessions"])
            remaining = {r["target"]["session_id"] for r in await backend.list_sessions()}
            assert not (created & remaining)
            assert original <= remaining
            results["original_sessions_preserved"] = len(original)
        finally:
            # Test failure cleanup is limited to exact IDs created above.
            # It is deliberately independent of the code under test.
            if backend._app is not None:
                await backend._app.async_refresh()
                for sid in created:
                    session = backend._app.get_session_by_id(sid)
                    if session is not None:
                        await session.async_close(force=True)
            await backend.close(timeout=2)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="create and close five dedicated temporary sessions")
    args = parser.parse_args()
    if not args.run:
        print("Plan only: test four close tools on five new allowlisted sessions. Add --run to execute.")
        return
    with redirect_stdout(sys.stderr):
        results = asyncio.run(smoke())
    print(json.dumps(results))


if __name__ == "__main__":
    main()
