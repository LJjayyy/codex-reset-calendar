import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

script = Path(__file__).resolve().parents[1] / "plugins/codex-reset-calendar/scripts/manage.py"
spec = importlib.util.spec_from_file_location("manage", script)
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class ManagerTests(unittest.TestCase):
    def test_calendar_passes_email_in_private_file_only(self):
        address = "alice@example.com"

        def verify_command(command, *args, **kwargs):
            self.assertNotIn(address, " ".join(map(str, command)))
            self.assertEqual(
                command[command.index("--operation-lock-file") + 1], manage.ROOT / "calendar-operation.lock",
            )
            address_file = command[command.index("--account-email-file") + 1]
            self.assertEqual(address_file.read_text(), address)
            self.assertEqual(address_file.stat().st_mode & 0o777, 0o600)
            result_file = command[command.index("--result-file") + 1]
            result_file.write_text('{"ok":true}')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "Test Calendar.app"
            bundle.mkdir()
            with patch.object(manage, "ROOT", root), patch.object(manage, "run", side_effect=verify_command):
                result = manage.calendar("verify", cards=[], scope="b" * 64, account_email=address, bundle=bundle)
            self.assertTrue(result["ok"])

    def test_verify_reads_expected_events_without_syncing(self):
        snapshot = manage.CardSnapshot(
            [{"id": "a" * 64, "expiresAt": 2_000_000_000}], "b" * 64, True, "alice@example.com",
        )
        result = {
            "ok": True, "verified": 1, "expected": 1, "duplicates": 0, "alarms": 4,
            "linked": 1, "labeled": 1,
        }
        with patch.object(manage, "calendar", return_value=result) as helper, patch("builtins.print"):
            self.assertEqual(manage.verify(snapshot), result)
        helper.assert_called_once_with(
            "verify", cards=snapshot.cards, scope=snapshot.scope, account_email=snapshot.account_email,
        )

    def test_verify_rejects_duplicate_events(self):
        result = {"ok": True, "verified": 1, "expected": 1, "duplicates": 1}
        with patch.object(manage, "calendar", return_value=result), patch("builtins.print"), \
             self.assertRaises(manage.SyncError):
            manage.verify(manage.CardSnapshot([], None, False, None))

    def test_verify_rejects_old_calendar_link(self):
        result = {"ok": True, "verified": 1, "expected": 1, "duplicates": 0, "linked": 0}
        with patch.object(manage, "calendar", return_value=result), patch("builtins.print"), \
             self.assertRaisesRegex(manage.SyncError, "用量链接尚未更新"):
            manage.verify(manage.CardSnapshot([], None, False, None))

    def test_verify_rejects_missing_account_notes(self):
        result = {"ok": True, "verified": 1, "expected": 1, "duplicates": 0, "linked": 1, "labeled": 0}
        snapshot = manage.CardSnapshot([], "b" * 64, True, "alice@example.com")
        with patch.object(manage, "calendar", return_value=result), patch("builtins.print"), \
             self.assertRaisesRegex(manage.SyncError, "账号备注尚未更新"):
            manage.verify(snapshot)

    def test_account_email_requires_scope(self):
        payload = {"cards": [], "scope": None, "prune": False, "accountEmail": "alice@example.com"}
        with patch.object(manage, "run", return_value=SimpleNamespace(stdout=json.dumps(payload))), \
             self.assertRaises(manage.SyncError):
            manage.read_snapshot("/fake/codex")

    def test_sync_writes_nested_helper_result_to_status(self):
        ids = [{"id": "a" * 64, "expiresAt": 2_000_000_000}]
        scope = "b" * 64
        account_email = "alice@example.com"
        snapshot = manage.CardSnapshot(ids, scope, True, account_email)
        result = {"ok": True, "counts": {"created": 1, "updated": 0, "removed": 2}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(manage, "ROOT", root), \
                 patch.object(manage, "plugin_enabled", return_value=True), \
                 patch.object(manage, "read_snapshot", return_value=snapshot), \
                 patch.object(manage, "calendar", return_value=result) as helper:
                summary = manage.sync("/fake/codex")
            helper.assert_called_once_with(
                "sync", cards=ids, scope=scope, account_email=account_email, prune=True,
            )
            self.assertEqual(summary, json.loads((root / "status.json").read_bytes()))
            self.assertEqual((summary["created"], summary["removed"]), (1, 2))

    def test_failed_read_leaves_calendar_alone_and_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(manage, "ROOT", root), \
                 patch.object(manage, "plugin_enabled", return_value=True), \
                 patch.object(manage, "read_snapshot", side_effect=manage.SyncError("offline")), \
                 patch.object(manage, "calendar") as helper, \
                 self.assertRaises(manage.SyncError):
                manage.sync("/fake/codex")
            helper.assert_not_called()
            self.assertFalse(json.loads((root / "status.json").read_bytes())["ok"])

    def test_disabled_plugin_stops_before_account_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(manage, "ROOT", root), \
                 patch.object(manage, "plugin_enabled", return_value=False), \
                 patch.object(manage, "read_snapshot") as read, \
                 patch.object(manage, "calendar") as helper, \
                 self.assertRaises(manage.SyncError):
                manage.sync("/fake/codex")
            read.assert_not_called()
            helper.assert_not_called()

    def test_scope_less_snapshot_never_updates_calendar(self):
        snapshot = manage.CardSnapshot([{"id": "a" * 64, "expiresAt": 2_000_000_000}], None, False, None)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(manage, "ROOT", Path(directory)), \
                 patch.object(manage, "plugin_enabled", return_value=True), \
                 patch.object(manage, "calendar") as helper, \
                 self.assertRaisesRegex(manage.SyncError, "已跳过本次日历同步"):
                manage.sync("/fake/codex", snapshot=snapshot)
            helper.assert_not_called()

    def test_install_verifies_same_fresh_snapshot_before_starting_job(self):
        before = manage.CardSnapshot([], "a" * 64, True, None)
        after = manage.CardSnapshot([], "b" * 64, True, None)
        completed = []
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(manage, "ROOT", Path(directory)), \
                 patch.object(manage.platform, "system", return_value="Darwin"), \
                 patch.object(manage.platform, "mac_ver", return_value=("14.0", (), "")), \
                 patch.object(manage, "codex_path", return_value="/fake/codex"), \
                 patch.object(manage, "ensure_plugin_enabled"), \
                 patch.object(manage, "read_snapshot", side_effect=(before, after)), \
                 patch.object(manage, "build_helper") as build, \
                 patch.object(manage, "sync", return_value={"cards": 0}) as sync, \
                 patch.object(manage, "verify", side_effect=lambda snapshot: completed.append("verify")) as verify, \
                 patch.object(manage, "install_job", side_effect=lambda: completed.append("start")), \
                 patch("builtins.print"):
                manage.install(None)
                build.assert_called_once_with(Path(directory), authorize=True)
                sync.assert_called_once_with("/fake/codex", snapshot=after)
                verify.assert_called_once_with(after)
        self.assertEqual(completed, ["verify", "start"])

    def test_canceled_calendar_access_restores_the_previous_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / manage.BUNDLE_NAME
            bundle.mkdir()
            (bundle / "old-executable").write_bytes(b"old")
            stamp = root / "calendar-build.txt"
            stamp.write_text("old-build\n")

            def compile_helper(command, *args, **kwargs):
                if command[0] == "/usr/bin/swiftc":
                    Path(command[-1]).write_bytes(b"new")

            with patch.object(manage, "run", side_effect=compile_helper), \
                 patch.object(manage, "authorize_helper", side_effect=manage.SyncError("denied")), \
                 self.assertRaisesRegex(manage.SyncError, "denied"):
                manage.build_helper(root, authorize=True)
            self.assertEqual((bundle / "old-executable").read_bytes(), b"old")
            self.assertEqual(stamp.read_text(), "old-build\n")

    def test_launch_job_is_local_and_periodic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root / "LaunchAgents" / f"{manage.LABEL}.plist"
            with patch.object(manage, "ROOT", root), patch.object(manage, "JOB", job), \
                 patch.object(manage, "loaded", return_value=False), \
                 patch.object(manage, "run") as launchctl:
                manage.install_job()
            config = plistlib.loads(job.read_bytes())
            self.assertTrue(config["RunAtLoad"])
            self.assertEqual(config["StartInterval"], 600)
            self.assertEqual(config["ProgramArguments"][-2:], [str(root / "worker/manage.py"), "sync"])
            self.assertEqual(launchctl.call_args.args[0][1], "bootstrap")


if __name__ == "__main__":
    unittest.main()
