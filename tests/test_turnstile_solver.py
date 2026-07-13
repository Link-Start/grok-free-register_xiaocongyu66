"""Unit tests for vendored Turnstile solver management."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from grok_register import turnstile_solver as ts


class TurnstileSolverConfigTests(unittest.TestCase):
    def test_resolve_solver_mode_aliases(self):
        self.assertEqual(ts.resolve_solver_mode("local"), "local")
        self.assertEqual(ts.resolve_solver_mode("d3vin"), "d3vin")
        self.assertEqual(ts.resolve_solver_mode("d3-vin"), "d3vin")
        self.assertEqual(ts.resolve_solver_mode("theyka"), "theyka")
        self.assertEqual(ts.resolve_solver_mode("api"), "api")
        self.assertEqual(ts.resolve_solver_mode("external"), "api")
        self.assertEqual(ts.resolve_solver_mode("nope"), "d3vin")

    def test_is_api_backend(self):
        self.assertFalse(ts.is_api_backend("local"))
        self.assertTrue(ts.is_api_backend("d3vin"))
        self.assertTrue(ts.is_api_backend("theyka"))
        self.assertTrue(ts.is_api_backend("api"))

    def test_resolve_engine(self):
        self.assertEqual(ts.resolve_engine("d3vin"), "d3vin")
        self.assertEqual(ts.resolve_engine("theyka"), "theyka")
        with mock.patch.dict(os.environ, {"TURNSTILE_SOLVER_ENGINE": "theyka"}, clear=False):
            self.assertEqual(ts.resolve_engine("api"), "theyka")
        with mock.patch.dict(os.environ, {"TURNSTILE_SOLVER_ENGINE": "external"}, clear=False):
            self.assertEqual(ts.resolve_engine("api"), "external")

    def test_resolve_api_url_defaults(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_API_URL": ""}, clear=False):
            os.environ.pop("TURNSTILE_API_URL", None)
            with mock.patch.dict(os.environ, {"TURNSTILE_SOLVER_PORT": ""}, clear=False):
                os.environ.pop("TURNSTILE_SOLVER_PORT", None)
                self.assertEqual(ts.resolve_api_url("d3vin"), "http://127.0.0.1:5072")
                self.assertEqual(ts.resolve_api_url("theyka"), "http://127.0.0.1:5000")

    def test_build_command_theyka_headless_injects_ua(self):
        cmd = ts.build_command(
            "theyka",
            host="127.0.0.1",
            port=5000,
            browser_type="chromium",
            thread=1,
            headless=True,
            debug=False,
            proxy=False,
            useragent=None,
        )
        self.assertIn("--headless", cmd)
        self.assertIn("True", cmd)
        self.assertIn("--useragent", cmd)

    def test_build_command_d3vin_headless_default(self):
        cmd = ts.build_command(
            "d3vin",
            host="127.0.0.1",
            port=5072,
            browser_type="chromium",
            thread=2,
            headless=True,
            debug=False,
            proxy=False,
            useragent=None,
        )
        self.assertNotIn("--no-headless", cmd)
        self.assertEqual(cmd[0:2], [ts.sys.executable, "api_solver.py"])
        self.assertIn("--thread", cmd)

    def test_build_command_d3vin_headed(self):
        cmd = ts.build_command(
            "d3vin",
            host="127.0.0.1",
            port=5072,
            browser_type="chromium",
            thread=1,
            headless=False,
            debug=True,
            proxy=True,
            useragent="UA",
        )
        self.assertIn("--no-headless", cmd)
        self.assertIn("--debug", cmd)
        self.assertIn("--proxy", cmd)
        self.assertIn("UA", cmd)

    def test_vendor_dirs_exist(self):
        self.assertTrue((ts.vendor_dir("d3vin") / "api_solver.py").is_file())
        self.assertTrue((ts.vendor_dir("theyka") / "api_solver.py").is_file())
        self.assertTrue((ts.vendor_dir("d3vin") / "db_results.py").is_file())
        self.assertTrue((ts.vendor_dir("d3vin") / "browser_configs.py").is_file())

    def test_should_manage_process(self):
        with mock.patch.dict(
            os.environ,
            {
                "TURNSTILE_API_MANAGED": "1",
                "TURNSTILE_API_URL": "http://127.0.0.1:5072",
            },
            clear=False,
        ):
            self.assertTrue(ts.should_manage_process("d3vin"))
            self.assertTrue(ts.should_manage_process("theyka"))
        with mock.patch.dict(
            os.environ,
            {
                "TURNSTILE_API_MANAGED": "1",
                "TURNSTILE_SOLVER_ENGINE": "external",
                "TURNSTILE_API_URL": "http://127.0.0.1:5072",
            },
            clear=False,
        ):
            self.assertFalse(ts.should_manage_process("api"))
        with mock.patch.dict(
            os.environ,
            {
                "TURNSTILE_API_MANAGED": "0",
            },
            clear=False,
        ):
            self.assertFalse(ts.should_manage_process("d3vin"))

    def test_sync_work_tree_copies_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"TURNSTILE_SOLVER_WORK_DIR": tmp}, clear=False):
                # reset DEFAULT path usage via env
                path = ts._sync_work_tree("d3vin")
                self.assertTrue((path / "api_solver.py").is_file())
                self.assertTrue((path / "db_results.py").is_file())
                self.assertTrue(path.is_relative_to(Path(tmp)) or str(path).startswith(tmp))

    def test_ensure_solver_local_noop(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_SOLVER": "local"}, clear=False):
            meta = ts.ensure_solver_for_register(log=lambda *_: None)
            self.assertEqual(meta["mode"], "local")
            self.assertFalse(meta.get("managed"))


class TurnstileSolverStartTests(unittest.TestCase):
    def test_start_reuses_healthy_listener(self):
        logs = []
        with mock.patch.dict(
            os.environ,
            {
                "TURNSTILE_SOLVER": "d3vin",
                "TURNSTILE_API_MANAGED": "1",
                "TURNSTILE_API_URL": "http://127.0.0.1:5072",
            },
            clear=False,
        ):
            with mock.patch.object(ts, "health_check", return_value=True):
                with mock.patch.object(ts, "ensure_runtime_ready") as ready:
                    meta = ts.start_managed_solver(mode="d3vin", log=logs.append, install=False)
        self.assertTrue(meta["already_running"])
        self.assertFalse(meta["managed"])
        ready.assert_not_called()
        self.assertTrue(any("复用" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
