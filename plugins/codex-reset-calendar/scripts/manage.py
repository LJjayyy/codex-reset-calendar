#!/usr/bin/env python3
"""Install and run the local Codex reset-card calendar worker."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple, Optional


PLUGIN = "codex-reset-calendar"
MARKET = "reset-calendar-local"
LABEL = "local.codex.reset-calendar"
BUNDLE_NAME = "Codex 重置卡日历.app"
INTERVAL_SECONDS = 600
DESKTOP_CODEX = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli")
    / "CodexCLI.app/Contents/MacOS/codex"
)
SCRIPTS = Path(__file__).resolve().parent
ROOT = Path.home() / "Library/Application Support/Codex Reset Calendar"
JOB = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
DOMAIN = f"gui/{os.getuid()}"
SERVICE = f"{DOMAIN}/{LABEL}"
HASH = re.compile(r"[0-9a-f]{64}")


class SyncError(Exception):
    pass


class CardSnapshot(NamedTuple):
    cards: list
    scope: Optional[str]
    prune: bool
    account_email: Optional[str]


def run(command, message, *, timeout=45, show=False):
    try:
        return subprocess.run(
            [str(part) for part in command],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=not show,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SyncError(message) from error


def read_json(path, message):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise SyncError(message) from error


def write_private(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_private(path, (json.dumps(value, ensure_ascii=False) + "\n").encode())


@contextmanager
def sync_lock():
    with (ROOT / "sync.lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def codex_path():
    for candidate in (DESKTOP_CODEX, shutil.which("codex")):
        if candidate and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise SyncError("找不到 Codex。请先安装 ChatGPT 桌面 App 或 Codex CLI。")


def read_snapshot(codex):
    result = run(
        [sys.executable, SCRIPTS / "read_cards.py", "--codex", codex],
        "读取 Codex 重置卡失败。请确认 Mac 已联网，并在 Codex 登录 ChatGPT。",
        timeout=55,
    )
    try:
        payload = json.loads(result.stdout)
        cards = payload["cards"]
        prune = payload["prune"]
        scope = payload.get("scope")
        account_email = payload.get("accountEmail")
        if not isinstance(cards, list) or type(prune) is not bool:
            raise ValueError
        if scope is not None and not HASH.fullmatch(scope):
            raise ValueError
        if prune and scope is None:
            raise ValueError
        if account_email is not None and (
            scope is None or not isinstance(account_email, str)
            or not 1 <= len(account_email) <= 254 or not account_email.isprintable()
            or any(character.isspace() for character in account_email)
        ):
            raise ValueError
        for card in cards:
            if not HASH.fullmatch(card["id"]) or type(card["expiresAt"]) is not int:
                raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise SyncError("Codex 返回的重置卡资料格式异常。") from error
    return CardSnapshot(cards, scope, prune, account_email)


def plugin_enabled(codex):
    result = run(
        [codex, "plugin", "list", "--marketplace", MARKET, "--json"],
        "无法确认 Codex 插件是否已启用。",
        timeout=15,
    )
    try:
        installed = json.loads(result.stdout)["installed"]
        matches = [entry for entry in installed if entry["pluginId"] == f"{PLUGIN}@{MARKET}"]
        return len(matches) == 1 and matches[0]["installed"] is True and matches[0]["enabled"] is True
    except (KeyError, TypeError, ValueError) as error:
        raise SyncError("无法确认 Codex 插件是否已启用。") from error


def ensure_plugin_enabled(codex):
    if not plugin_enabled(codex):
        raise SyncError("Codex 重置卡日历插件已停用或卸载，同步已暂停。")


def calendar(action, *, cards=None, scope=None, account_email=None, prune=False, bundle=None):
    bundle = bundle or ROOT / BUNDLE_NAME
    if not bundle.is_dir():
        raise SyncError("找不到日历助手。请重新运行安装程序。")
    with tempfile.TemporaryDirectory(prefix="calendar-", dir=ROOT) as directory:
        paths = Path(directory)
        result = paths / "result.json"
        permission = paths / "permission-pending"
        command = ["/usr/bin/open", "-n"]
        if action != "authorize":
            command.append("-g")
        command.extend(["-a", bundle, "--args", action, "--result-file", result])
        if action == "authorize":
            command.extend(["--permission-file", permission])
        else:
            command.extend(["--operation-lock-file", ROOT / "calendar-operation.lock"])
        if cards is not None:
            source = paths / "cards.json"
            write_json(source, cards)
            command.extend(["--input-file", source])
        if scope:
            command.extend(["--scope", scope])
        if account_email:
            address_file = paths / "account-email"
            write_private(address_file, account_email.encode("utf-8"))
            command.extend(["--account-email-file", address_file])
        if prune:
            command.append("--prune")
        run(command, "无法启动 macOS 日历助手。", timeout=15)
        deadline = time.monotonic() + (300 if action == "authorize" else 55)
        announced = False
        while not result.is_file() and time.monotonic() < deadline:
            if action == "authorize" and not announced and permission.is_file():
                print("macOS 正在请求日历权限。请在弹窗里选择「允许完整访问」。", flush=True)
                announced = True
            time.sleep(0.1)
        response = read_json(result, "日历助手没有返回结果。")
        if not isinstance(response, dict) or response.get("ok") is not True:
            message = response.get("error") if isinstance(response, dict) else None
            raise SyncError(message or "日历助手未能完成操作。")
        return response


def sync(codex, *, snapshot=None):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        ensure_plugin_enabled(codex)
        if snapshot is None:
            snapshot = read_snapshot(codex)
        if snapshot.scope is None:
            raise SyncError("Codex 没有返回可确认的账号标识，已跳过本次日历同步。")
        result = calendar(
            "sync", cards=snapshot.cards, scope=snapshot.scope,
            account_email=snapshot.account_email, prune=snapshot.prune,
        )
        counts = result["counts"]
        summary = {
            "ok": True,
            "checkedAt": timestamp,
            "cards": len(snapshot.cards),
            "complete": snapshot.prune,
            "created": counts["created"],
            "updated": counts["updated"],
            "removed": counts["removed"],
        }
    except SyncError as error:
        write_json(ROOT / "status.json", {"ok": False, "checkedAt": timestamp, "error": str(error)})
        raise
    write_json(ROOT / "status.json", summary)
    return summary


def verify(snapshot):
    result = calendar(
        "verify", cards=snapshot.cards, scope=snapshot.scope, account_email=snapshot.account_email,
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result["verified"] != result["expected"] or result["duplicates"]:
        raise SyncError("有重置卡尚未在 iCloud 日历中找到唯一的到期事件。")
    if result["linked"] != result["expected"]:
        raise SyncError("有重置卡日历事件的用量链接尚未更新。")
    if snapshot.account_email is not None and result["labeled"] != result["expected"]:
        raise SyncError("有重置卡日历事件的账号备注尚未更新。")
    return result


def authorize_helper(bundle):
    print("正在检查日历权限…", flush=True)
    calendar("authorize", bundle=bundle)


def build_helper(destination, *, authorize=False):
    source = SCRIPTS / "CalendarSync.swift"
    info = SCRIPTS / "CalendarInfo.plist"
    fingerprint = hashlib.sha256(
        source.read_bytes() + info.read_bytes() + platform.machine().encode()
    ).hexdigest()
    bundle = destination / BUNDLE_NAME
    stamp = destination / "calendar-build.txt"
    if bundle.is_dir() and stamp.exists() and stamp.read_text().strip() == fingerprint:
        try:
            run(["/usr/bin/codesign", "--verify", bundle], "日历助手签名无法验证。")
        except SyncError:
            pass
        else:
            if authorize:
                authorize_helper(bundle)
            return bundle

    with tempfile.TemporaryDirectory(prefix="build-", dir=destination) as directory:
        build = Path(directory)
        app = build / BUNDLE_NAME
        executable = app / "Contents/MacOS/CalendarSync"
        executable.parent.mkdir(parents=True)
        shutil.copyfile(info, app / "Contents/Info.plist")
        run(
            [
                "/usr/bin/swiftc", "-parse-as-library", "-O",
                "-target", f"{platform.machine()}-apple-macosx14.0",
                "-framework", "EventKit", "-module-cache-path", build / "swift-cache",
                source, "-o", executable,
            ],
            "编译日历助手失败。请确认本机已安装 Xcode Command Line Tools。",
            timeout=180,
            show=True,
        )
        run(["/usr/bin/codesign", "--force", "--sign", "-", app], "无法为日历助手签名。")
        run(["/usr/bin/codesign", "--verify", "--strict", app], "日历助手签名无法验证。")
        previous = build / "previous.app"
        if bundle.exists():
            bundle.rename(previous)
        try:
            app.rename(bundle)
            if authorize:
                authorize_helper(bundle)
            write_private(stamp, (fingerprint + "\n").encode())
        except (SyncError, OSError, KeyboardInterrupt):
            if bundle.exists():
                bundle.rename(app)
            if previous.exists():
                previous.rename(bundle)
            raise
    return bundle


def loaded():
    return subprocess.run(
        ["/bin/launchctl", "print", SERVICE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def install_job():
    JOB.parent.mkdir(exist_ok=True)
    config = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(ROOT / "worker/manage.py"), "sync"],
        "RunAtLoad": True,
        "StartInterval": INTERVAL_SECONDS,
        "LimitLoadToSessionType": "Aqua",
    }
    contents = plistlib.dumps(config)
    was_loaded = loaded()
    previous = JOB.read_bytes() if JOB.exists() else None
    if was_loaded and previous == contents:
        return
    if was_loaded:
        run(["/bin/launchctl", "bootout", SERVICE], "无法重新加载原有同步任务。")
    try:
        write_private(JOB, contents)
        run(["/bin/launchctl", "enable", SERVICE], "无法启用后台同步。")
        run(
            ["/bin/launchctl", "bootstrap", DOMAIN, JOB],
            "无法启动后台同步。请在已登录的 Mac 桌面上运行安装程序。",
        )
    except (SyncError, OSError):
        if not loaded():
            if previous is None:
                JOB.unlink(missing_ok=True)
            else:
                write_private(JOB, previous)
                if was_loaded:
                    run(["/bin/launchctl", "bootstrap", DOMAIN, JOB], "无法恢复原有同步任务。")
        raise


def register_plugin(codex, marketplace_file):
    marketplace = ROOT / "marketplace"
    metadata = marketplace / ".agents/plugins/marketplace.json"
    target = marketplace / "plugins" / PLUGIN
    metadata.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="plugin-", dir=ROOT) as directory:
        prepared = Path(directory) / PLUGIN
        shutil.copytree(
            SCRIPTS.parent, prepared,
            ignore=shutil.ignore_patterns("__pycache__", "tests", ".DS_Store"),
        )
        for path in prepared.rglob("*"):
            if path.is_file():
                output = target / path.relative_to(prepared)
                output.parent.mkdir(parents=True, exist_ok=True)
                write_private(output, path.read_bytes())
        write_private(metadata, marketplace_file.read_bytes())
    run(
        [codex, "plugin", "marketplace", "add", marketplace],
        "无法把本地插件目录登记到 Codex。",
    )
    run([codex, "plugin", "add", f"{PLUGIN}@{MARKET}"], "无法把插件安装到 Codex。")


def install(marketplace_file):
    if platform.system() != "Darwin" or int(platform.mac_ver()[0].split(".")[0]) < 14:
        raise SyncError("需要运行 macOS 14 或更新版本的 Mac。")
    codex = codex_path()
    if marketplace_file is None:
        ensure_plugin_enabled(codex)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sync_lock():
        print("正在检查 Codex 账号并准备日历助手…", flush=True)
        if read_snapshot(codex).scope is None:
            raise SyncError("Codex 没有返回可确认的账号标识，请先确认已登录。")
        build_helper(ROOT, authorize=True)
        if marketplace_file:
            register_plugin(codex, marketplace_file)
        worker = ROOT / "worker"
        worker.mkdir(mode=0o700, exist_ok=True)
        for filename in ("manage.py", "read_cards.py"):
            write_private(worker / filename, (SCRIPTS / filename).read_bytes())
        write_json(ROOT / "config.json", {"codex": codex})
        snapshot = read_snapshot(codex)
        first = sync(codex, snapshot=snapshot)
        verify(snapshot)
        install_job()
    print(f"已启动后台同步，每 {INTERVAL_SECONDS // 60} 分钟检查一次。")
    print(f"本次读取到 {first['cards']} 张带到期时间的有效重置卡。")


def stop():
    if ROOT.exists():
        with sync_lock():
            if loaded():
                run(["/bin/launchctl", "bootout", SERVICE], "无法停止后台同步。")
            JOB.unlink(missing_ok=True)
    print("已停止后台同步。")


def status():
    path = ROOT / "status.json"
    latest = read_json(path, "无法读取上次同步结果。") if path.exists() else None
    configured = ROOT / "config.json"
    enabled = None
    if configured.exists():
        try:
            config = read_json(configured, "无法读取同步设置。")
            enabled = plugin_enabled(config["codex"])
        except SyncError:
            pass
    print(json.dumps(
        {"running": loaded() and enabled is True, "pluginEnabled": enabled, "lastCheck": latest},
        ensure_ascii=False,
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    setup = commands.add_parser("install")
    setup.add_argument("--marketplace", type=Path, help="Path to the packaged marketplace.json")
    build = commands.add_parser("build-helper")
    build.add_argument("destination", type=Path)
    for action in ("check", "status", "sync", "stop", "verify"):
        commands.add_parser(action)
    args = parser.parse_args()
    try:
        if args.action == "install":
            install(args.marketplace)
        elif args.action == "build-helper":
            args.destination.mkdir(parents=True, exist_ok=True)
            print(build_helper(args.destination))
        elif args.action == "check":
            snapshot = read_snapshot(codex_path())
            print(json.dumps({
                "cards": len(snapshot.cards),
                "complete": snapshot.prune,
                "hasAccountEmail": snapshot.account_email is not None,
            }))
        elif args.action == "status":
            status()
        elif args.action == "stop":
            stop()
        else:
            if not (ROOT / "config.json").is_file():
                raise SyncError("请先运行安装程序。")
            with sync_lock():
                config = read_json(ROOT / "config.json", "无法读取同步设置。")
                codex = config["codex"]
                if args.action == "verify":
                    ensure_plugin_enabled(codex)
                    verify(read_snapshot(codex))
                else:
                    print(json.dumps(sync(codex), ensure_ascii=False))
        return 0
    except (SyncError, OSError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
