"""Bound the pinned SDK's synchronous AppleScript authentication subprocess.

This retains iTerm's own cookie/consent flow. It never approves a dialog or
changes preferences. asyncio deadlines alone cannot interrupt communicate().
"""
from __future__ import annotations

from contextlib import contextmanager
import subprocess
import time

from .errors import DomainError


@contextmanager
def bounded_sdk_authentication(timeout: float = 8.0):
    from iterm2 import auth

    original_runner = auth.CommandLineApplescriptRunner
    original_appkit = auth.gAppKitAvailable
    deadline = time.monotonic() + timeout

    class BoundedRunner(original_runner):
        def execute(self):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DomainError(
                    "ITERM_AUTH_TIMEOUT",
                    "iTerm authorization did not finish in time",
                )
            try:
                completed = subprocess.run(
                    ["/usr/bin/osascript", "-"],
                    input=self._script,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=remaining,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                # subprocess.run terminates and reaps only its own osascript.
                raise DomainError(
                    "ITERM_AUTH_TIMEOUT",
                    "iTerm authorization did not finish in time",
                ) from exc
            self._returncode = completed.returncode
            self._output = completed.stdout.decode("utf-8").rstrip()
            self._error = completed.stderr.decode("utf-8").rstrip()

    # Explicitly choosing the SDK's command-line runner bounds auth even in a
    # Python environment that happens to have AppKit installed. Restore SDK
    # globals on every path.
    auth.CommandLineApplescriptRunner = BoundedRunner
    auth.gAppKitAvailable = False
    try:
        yield
    finally:
        auth.CommandLineApplescriptRunner = original_runner
        auth.gAppKitAvailable = original_appkit
