import subprocess
import types
import unittest
from unittest.mock import AsyncMock, patch

from goals_iterm_mcp.backends import ItermBackend
from goals_iterm_mcp.direct import DirectItermBackend
from goals_iterm_mcp.errors import DomainError
from goals_iterm_mcp.sdk_auth import bounded_sdk_authentication


class SDKAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class Runner:
            def __init__(self, script):
                self._script = script.encode()

        self.original = Runner
        self.auth = types.SimpleNamespace(
            CommandLineApplescriptRunner=Runner, gAppKitAvailable=True
        )
        self.package = types.ModuleType("iterm2")
        self.package.auth = self.auth

    def test_bounded_same_cookie_flow_restores_globals(self):
        completed = subprocess.CompletedProcess([], 0, b"cookie key\n", b"")
        with patch.dict("sys.modules", {"iterm2": self.package}), patch(
            "goals_iterm_mcp.sdk_auth.subprocess.run", return_value=completed
        ) as run:
            with bounded_sdk_authentication(2):
                runner = self.auth.CommandLineApplescriptRunner("request cookie")
                runner.execute()
                self.assertFalse(self.auth.gAppKitAvailable)
                self.assertEqual(runner._output, "cookie key")
            self.assertIs(self.auth.CommandLineApplescriptRunner, self.original)
            self.assertTrue(self.auth.gAppKitAvailable)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 2)
            self.assertEqual(run.call_args.args[0], ["/usr/bin/osascript", "-"])

    def test_timeout_is_classified_and_globals_restored(self):
        with patch.dict("sys.modules", {"iterm2": self.package}), patch(
            "goals_iterm_mcp.sdk_auth.subprocess.run",
            side_effect=subprocess.TimeoutExpired("osascript", 1),
        ):
            with self.assertRaises(DomainError) as raised:
                with bounded_sdk_authentication(1):
                    self.auth.CommandLineApplescriptRunner("request cookie").execute()
            self.assertEqual(raised.exception.code, "ITERM_AUTH_TIMEOUT")
            self.assertIs(self.auth.CommandLineApplescriptRunner, self.original)
            self.assertTrue(self.auth.gAppKitAvailable)

    async def test_direct_backend_connect_uses_bounded_sdk_auth(self):
        backend = DirectItermBackend(broker_epoch="broker")
        observed_appkit = []

        async def fake_connect(instance):
            observed_appkit.append(self.auth.gAppKitAvailable)
            return instance

        with patch.dict("sys.modules", {"iterm2": self.package}), patch.object(
            ItermBackend, "connect", new=fake_connect
        ):
            await backend.connect()

        self.assertEqual(observed_appkit, [False])
        self.assertTrue(self.auth.gAppKitAvailable)


if __name__ == "__main__":
    unittest.main()
