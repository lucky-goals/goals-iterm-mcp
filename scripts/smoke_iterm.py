"""Explicit real stdio MCP -> iTerm smoke; --run creates two temporary cat sessions."""

import argparse
import asyncio
from contextlib import redirect_stdout
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cc_iterm_mcp.direct import DirectItermBackend


async def smoke() -> dict:
    created = []
    results = {}
    try:
        async with asyncio.timeout(45):
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "cc_iterm_mcp", "serve"]
            )
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    results["tool_count"] = len((await client.list_tools()).tools)

                    async def call(name, args):
                        result = await client.call_tool(name, args)
                        if result.isError:
                            raise RuntimeError(f"{name} failed; no write retry")
                        return result.structuredContent

                    before = await call("list_sessions", {})
                    existing = {r["session_id"] for r in before["sessions"]}
                    window = await call("create_window", {"command": "/bin/cat"})
                    assert window["session_id"] not in existing
                    created.append(window["session_id"])
                    tab = await call(
                        "create_tab",
                        {"window_id": window["window_id"], "command": "/bin/cat"},
                    )
                    assert tab["session_id"] not in existing | set(created)
                    created.append(tab["session_id"])
                    results.update(create_window=True, create_tab=True)
                    target = tab["session_id"]
                    await asyncio.sleep(0.3)
                    marker = "cc-iterm-direct-smoke-界😀"
                    sent = await call(
                        "send_text", {"session_id": target, "text": marker}
                    )
                    results["send_text_ack"] = sent["api_acknowledged"]
                    sent = await call(
                        "send_keys", {"session_id": target, "key": "enter"}
                    )
                    results["send_key_ack"] = sent["api_acknowledged"]
                    for _ in range(10):
                        screen = await call("read_screen", {"session_id": target})
                        if any(marker in r["text"] for r in screen["screen"]["lines"]):
                            results["unicode_screen_match"] = True
                            break
                        await asyncio.sleep(0.1)
                    assert results.get("unicode_screen_match")
                    # Produce enough harmless cat output for real, nonempty
                    # scrollback; cap the input independently of terminal size.
                    rows = min(max(screen["screen"]["rows"] + 10, 40), 200)
                    await call(
                        "send_text",
                        {
                            "session_id": target,
                            "text": "".join(
                                f"cc-iterm-history-{i}\n" for i in range(rows)
                            ),
                        },
                    )
                    for _ in range(10):
                        history = await call(
                            "read_history", {"session_id": target, "lines": 10}
                        )
                        if any(
                            "cc-iterm-history-" in r["text"] for r in history["lines"]
                        ):
                            results["nonempty_history_match"] = True
                            break
                        await asyncio.sleep(0.1)
                    assert results.get("nonempty_history_match")
    finally:
        if created:
            backend = DirectItermBackend(broker_epoch="direct-smoke-cleanup")
            with redirect_stdout(sys.stderr):
                try:
                    async with asyncio.timeout(10):
                        await backend.connect()
                        for sid in created:
                            session = backend._app.get_session_by_id(sid)
                            if session is not None:
                                await session.async_close(force=True)
                        for _ in range(10):
                            if not any(
                                backend._app.get_session_by_id(sid) for sid in created
                            ):
                                results["owned_sessions_closed"] = len(created)
                                break
                            await asyncio.sleep(0.1)
                        assert results.get("owned_sessions_closed") == len(created)
                finally:
                    await backend.close(timeout=2)
        print(json.dumps(results))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="create/send/read/close dedicated test sessions",
    )
    args = parser.parse_args()
    if not args.run:
        print(
            "Plan only: create a new window and tab running /bin/cat, verify seven MCP tools, close both test sessions. Add --run to execute."
        )
        return
    asyncio.run(smoke())


if __name__ == "__main__":
    main()
