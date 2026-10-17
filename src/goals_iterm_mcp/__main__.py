"""Run the iTerm Python API MCP service over stdio."""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="iTerm Python API MCP service")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start the stdio MCP service")
    serve.add_argument(
        "--session-id",
        action="append",
        help="allowed session ID; repeat to allow several",
    )
    serve.add_argument(
        "--read-only", action="store_true", help="expose only discovery and read tools"
    )
    args = parser.parse_args()
    from .direct import run

    try:
        run(session_ids=args.session_id, read_only=args.read_only)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"goals-iterm-mcp stopped: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
