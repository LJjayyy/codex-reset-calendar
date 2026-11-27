from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


script = Path(__file__).resolve().parents[1] / "plugins/codex-reset-calendar/scripts/manage.py"
spec = importlib.util.spec_from_file_location("manage", script)
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


SNAPSHOT = manage.CardSnapshot(
    [{"id": "a" * 64, "expiresAt": 2_000_000_000}], "b" * 64, True, "alice@example.com",
)
CALENDAR_RESULT = {"ok": True, "counts": {"created": 1, "updated": 0, "removed": 0}}


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def fake_codex(path):
    path.write_text(f"""#!{sys.executable}
import json, os, sys
if sys.argv[1:3] == ["plugin", "list"]:
    state = json.load(open(os.environ["FAKE_CODEX_STATE"]))
    log = os.environ.get("FAKE_CODEX_CWD_LOG")
    if log:
        open(log, "w").write(os.getcwd())
    print(json.dumps({{"installed": [{{"pluginId": "codex-reset-calendar@reset-calendar-local", "installed": True, "enabled": state}}]}}))
    raise SystemExit(0)
raise SystemExit("unexpected codex args: " + " ".join(sys.argv[1:]))
""", encoding="utf-8")
    path.chmod(0o755)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.state = self.root / "state.json"
        self.state.write_text("false")
        self.codex = self.root / "codex"
        fake_codex(self.codex)
        self.cwd_log = self.root / "cwd.log"
        self.config = {"codex": str(self.codex), "syncedAt": 1000.0}
        self.write_config()
        (self.root / "config.toml").write_text("")
        self.status = {"ok": True, "checkedAt": "2026-01-01T00:00:00+00:00", "cards": 3}
        self.write_status()
        self.env = {
            "CODEX_HOME": str(self.root),
            "FAKE_CODEX_STATE": str(self.state),
            "FAKE_CODEX_CWD_LOG": str(self.cwd_log),
        }

    def write_config(self):
        (self.root / "config.json").write_text(json.dumps(self.config))

    def write_status(self):
        (self.root / "status.json").write_text(json.dumps(self.status))

    def run_worker(self, clock_value=1001.0, ticks=3):
        manage.worker(clock=Clock(clock_value), sleep=lambda _: None, max_ticks=ticks)

    def test_worker_skips_while_plugin_off_and_retains_last_status(self):
        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage, "read_snapshot") as read, \
             patch.object(manage, "calendar") as helper:
            self.run_worker()
        self.assertEqual(json.loads((self.root / "status.json").read_bytes()), self.status)
        read.assert_not_called()
        helper.assert_not_called()

    def test_worker_syncs_immediately_when_plugin_reenabled(self):
        clock = Clock(1001.0)
        sleeps = []

        def sleep(_):
            sleeps.append(1)
            if len(sleeps) == 1:
                self.state.write_text("true")
                os.utime(self.root / "config.toml", (1002, 1002))
                clock.value = 1003.0

        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage, "read_snapshot", return_value=SNAPSHOT) as read, \
             patch.object(manage, "calendar", return_value=CALENDAR_RESULT) as helper:
            manage.worker(clock=clock, sleep=sleep, max_ticks=4)
        read.assert_called_once_with(str(self.codex))
        helper.assert_called_once()
        latest = json.loads((self.root / "status.json").read_bytes())
        self.assertTrue(latest["ok"])
        self.assertEqual(latest["cards"], 1)
        self.assertEqual(Path(self.cwd_log.read_text()).resolve(), (self.root / "codex-cwd").resolve())

    def test_worker_ignores_unrelated_config_changes_until_original_due(self):
        self.state.write_text("true")
        clock = Clock(1001.0)
        toml = self.root / "config.toml"
        sleeps = []
        seen = []

        def sleep(_):
            index = len(sleeps)
            sleeps.append(1)
            if index == 0:
                os.utime(toml, (1400, 1400))
                clock.value = 1400.0
                seen.append((clock.value, helper.call_count, read.call_count))
            elif index == 1:
                os.utime(toml, (1500, 1500))
                clock.value = 1599.0
                seen.append((clock.value, helper.call_count, read.call_count))
            elif index == 2:
                clock.value = 1601.0

        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage, "read_snapshot", return_value=SNAPSHOT) as read, \
             patch.object(manage, "calendar", return_value=CALENDAR_RESULT) as helper:
            manage.worker(clock=clock, sleep=sleep, max_ticks=5)
        self.assertEqual(seen, [(1400.0, 0, 0), (1599.0, 0, 0)])
        self.assertEqual(helper.call_count, 1)
        self.assertEqual(read.call_count, 1)

    def test_sync_cancels_before_calendar_when_plugin_disabled_during_read(self):
        self.state.write_text("true")

        def read(codex):
            self.state.write_text("false")
            return SNAPSHOT

        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage, "read_snapshot", side_effect=read), \
             patch.object(manage, "calendar") as helper, \
             self.assertRaises(manage.SyncError):
            manage.sync(str(self.codex))
        helper.assert_not_called()
        self.assertEqual(json.loads((self.root / "status.json").read_bytes()), self.status)

    def test_install_job_replaces_old_interval_job_with_keepalive_worker(self):
        import plistlib

        job = self.root / "LaunchAgents" / f"{manage.LABEL}.plist"
        job.parent.mkdir(parents=True)
        job.write_bytes(plistlib.dumps({
            "Label": manage.LABEL,
            "ProgramArguments": [sys.executable, str(self.root / "worker/manage.py"), "sync"],
            "RunAtLoad": True,
            "StartInterval": 600,
            "LimitLoadToSessionType": "Aqua",
        }))
        calls = []
        with patch.object(manage, "ROOT", self.root), \
             patch.object(manage, "JOB", job), \
             patch.object(manage, "loaded", return_value=True), \
             patch.object(manage, "run", side_effect=lambda command, *a, **k: calls.append(command[1])):
            manage.install_job()
        config = plistlib.loads(job.read_bytes())
        self.assertTrue(config["KeepAlive"])
        self.assertNotIn("StartInterval", config)
        self.assertEqual(config["ProgramArguments"][-2:], [str(self.root / "worker/manage.py"), "worker"])
        self.assertEqual(calls, ["bootout", "enable", "bootstrap"])

    def test_worker_retries_failed_sync_at_finite_interval(self):
        self.state.write_text("true")
        self.config.pop("syncedAt")
        self.write_config()
        clock = Clock(1001.0)
        seen = []

        def sync_attempt(*args, **kwargs):
            seen.append(clock.value)
            if len(seen) == 1:
                raise manage.SyncError("日历助手暂时不可用")
            return CALENDAR_RESULT

        def sleep(_):
            clock.value += manage.RETRY_SECONDS

        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage, "read_snapshot", return_value=SNAPSHOT), \
             patch.object(manage, "calendar", side_effect=sync_attempt):
            manage.worker(clock=clock, sleep=sleep, max_ticks=3)
        self.assertEqual(seen, [1001.0, 1001.0 + manage.RETRY_SECONDS])
        latest = json.loads((self.root / "status.json").read_bytes())
        self.assertTrue(latest["ok"])

    def test_installed_first_sync_is_not_repeated_by_worker(self):
        self.state.write_text("true")
        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage.platform, "system", return_value="Darwin"), \
             patch.object(manage.platform, "mac_ver", return_value=("14.0", (), "")), \
             patch.object(manage, "codex_path", return_value=str(self.codex)), \
             patch.object(manage, "read_snapshot", return_value=SNAPSHOT), \
             patch.object(manage, "build_helper"), \
             patch.object(manage, "sync", return_value={"cards": 1}), \
             patch.object(manage, "verify"), \
             patch.object(manage, "install_job"), \
             patch.object(manage.time, "time", return_value=1001.0), \
             patch("builtins.print"):
            manage.install(None)
        self.assertEqual(json.loads((self.root / "config.json").read_bytes())["syncedAt"], 1001.0)

        clock = Clock(1002.0)
        toml = self.root / "config.toml"
        sleeps = []

        def sleep(_):
            index = len(sleeps)
            sleeps.append(1)
            if index == 0:
                temporary = self.root / "config.toml.new"
                temporary.write_text("changed = true\n")
                os.replace(temporary, toml)
                clock.value = 1100.0
            elif index == 1:
                toml.unlink()
                clock.value = 1200.0
            elif index == 2:
                toml.write_text("restored = true\n")
                clock.value = 1300.0

        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage, "read_snapshot") as read, \
             patch.object(manage, "calendar") as helper:
            manage.worker(clock=clock, sleep=sleep, max_ticks=4)
        helper.assert_not_called()
        read.assert_not_called()
        self.assertEqual(json.loads((self.root / "config.json").read_bytes())["syncedAt"], 1001.0)
    def test_worker_failure_at_due_time_retries_after_30s_not_every_tick(self):
        self.state.write_text("true")
        self.config["syncedAt"] = 1001.0 - manage.INTERVAL_SECONDS + 4.0
        self.write_config()
        clock = Clock(1001.0)
        attempts = []

        def calendar_attempt(*args, **kwargs):
            attempts.append(clock.value)
            if len(attempts) == 1:
                raise manage.SyncError("日历助手暂时不可用")
            return CALENDAR_RESULT

        def sleep(_):
            clock.value += manage.POLL_SECONDS

        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage, "read_snapshot", return_value=SNAPSHOT), \
             patch.object(manage, "calendar", side_effect=calendar_attempt), \
             patch.object(manage, "plugin_enabled", return_value=True):
            manage.worker(clock=clock, sleep=sleep, max_ticks=18)
        self.assertEqual(attempts, [1005.0, 1035.0])

    def test_worker_retry_delay_starts_after_failed_sync_finishes(self):
        self.state.write_text("true")
        self.config.pop("syncedAt")
        self.write_config()
        clock = Clock(1001.0)
        attempts = []

        def calendar_attempt(*args, **kwargs):
            attempts.append(clock.value)
            clock.value += 12.0
            if len(attempts) == 1:
                raise manage.SyncError("日历助手暂时不可用")
            return CALENDAR_RESULT

        def sleep(_):
            clock.value += manage.POLL_SECONDS

        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage, "read_snapshot", return_value=SNAPSHOT), \
             patch.object(manage, "calendar", side_effect=calendar_attempt), \
             patch.object(manage, "plugin_enabled", return_value=True):
            manage.worker(clock=clock, sleep=sleep, max_ticks=17)
        self.assertEqual(attempts, [1001.0, 1043.0])

    def test_worker_off_or_unknown_state_does_not_check_codex_every_tick(self):
        for state, expected in (("false", False), ("unknown", None)):
            with self.subTest(state=state):
                self.state.write_text(state)
                self.config.pop("syncedAt", None)
                self.write_config()
                clock = Clock(1001.0)

                def sleep(_):
                    clock.value += manage.POLL_SECONDS

                with patch.object(manage, "ROOT", self.root), \
                     patch.dict(os.environ, self.env), \
                     patch.object(manage, "read_snapshot") as read, \
                     patch.object(manage, "calendar") as helper, \
                     patch.object(manage, "plugin_enabled", return_value=expected) as enabled:
                    manage.worker(clock=clock, sleep=sleep, max_ticks=5)
                self.assertEqual(enabled.call_count, 1)
                read.assert_not_called()
                helper.assert_not_called()
                self.assertEqual(json.loads((self.root / "status.json").read_bytes()), self.status)

    def test_worker_syncs_immediately_when_state_goes_unknown_to_true(self):
        self.state.write_text("unknown")
        self.config.pop("syncedAt")
        self.write_config()
        clock = Clock(1001.0)
        lookups = []

        def enabled(*args, **kwargs):
            lookups.append(clock.value)
            return True if clock.value >= 1031.0 else None

        def sleep(_):
            clock.value += manage.POLL_SECONDS

        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage, "plugin_enabled", side_effect=enabled), \
             patch.object(manage, "read_snapshot", return_value=SNAPSHOT), \
             patch.object(manage, "calendar", return_value=CALENDAR_RESULT) as helper:
            manage.worker(clock=clock, sleep=sleep, max_ticks=16)
        helper.assert_called_once()
        self.assertEqual(lookups, [1001.0, 1031.0, 1031.0, 1031.0])

    def test_install_job_restarts_loaded_worker_when_source_changes(self):
        job = self.root / "LaunchAgents" / f"{manage.LABEL}.plist"
        with patch.object(manage, "ROOT", self.root), \
             patch.object(manage, "JOB", job), \
             patch.object(manage, "loaded", return_value=False), \
             patch.object(manage, "run"):
            manage.install_job()

        worker = self.root / "worker"
        worker.mkdir()
        (worker / "manage.py").write_text("old")
        (worker / "read_cards.py").write_text("old")
        (self.root / "worker-build.txt").write_text("stale\n")
        (worker / "manage.py").write_text("new")

        calls = []
        with patch.object(manage, "ROOT", self.root), \
             patch.object(manage, "JOB", job), \
             patch.object(manage, "loaded", return_value=True), \
             patch.object(manage, "run", side_effect=lambda command, *a, **k: calls.append(command[1])):
            manage.install_job()
        self.assertEqual(calls, ["bootout", "enable", "bootstrap"])

    def test_install_job_keeps_loaded_worker_when_nothing_changed(self):
        job = self.root / "LaunchAgents" / f"{manage.LABEL}.plist"
        worker = self.root / "worker"
        worker.mkdir()
        (worker / "manage.py").write_text("same")
        (worker / "read_cards.py").write_text("same")
        calls = []
        with patch.object(manage, "ROOT", self.root), \
             patch.object(manage, "JOB", job), \
             patch.object(manage, "loaded", return_value=False), \
             patch.object(manage, "run", side_effect=lambda command, *a, **k: calls.append(command[1])):
            manage.install_job()
        self.assertEqual(calls, ["enable", "bootstrap"])
        calls.clear()
        with patch.object(manage, "ROOT", self.root), \
             patch.object(manage, "JOB", job), \
             patch.object(manage, "loaded", return_value=True), \
             patch.object(manage, "run", side_effect=lambda command, *a, **k: calls.append(command[1])):
            manage.install_job()
        self.assertEqual(calls, [])

    def test_worker_synced_at_write_shares_lock_with_status_write(self):
        self.state.write_text("true")
        self.config.pop("syncedAt", None)
        self.write_config()
        events = []

        @contextmanager
        def tracked_lock():
            events.append("lock")
            try:
                yield
            finally:
                events.append("unlock")

        def tracked_write(path, value):
            events.append(path.name)

        with patch.object(manage, "ROOT", self.root), \
             patch.dict(os.environ, self.env), \
             patch.object(manage, "sync_lock", tracked_lock), \
             patch.object(manage, "write_json", side_effect=tracked_write), \
             patch.object(manage, "read_snapshot", return_value=SNAPSHOT), \
             patch.object(manage, "calendar", return_value=CALENDAR_RESULT), \
             patch.object(manage, "plugin_enabled", return_value=True):
            manage.worker(clock=Clock(1001.0), sleep=lambda _: None, max_ticks=1)
        self.assertEqual(events, ["lock", "status.json", "config.json", "unlock"])


if __name__ == "__main__":
    unittest.main()
