#!/usr/bin/env python3
"""Read Codex reset-card expiry dates without making any account changes."""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path


BUNDLED_CODEX = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/"
    "CodexCLI.app/Contents/MacOS/codex"
)


class ReadError(Exception):
    """An error with a fixed message safe to log."""


def fingerprint(kind, value):
    return hashlib.sha256(f"codex-reset-calendar/{kind}/v1\0{value}".encode()).hexdigest()


def extract_cards(snapshot, now):
    account_id = snapshot.get("accountId")
    scope = fingerprint("account", account_id) if isinstance(account_id, str) and account_id else None
    summary = snapshot.get("rateLimitResetCredits")
    if summary is None:
        return {"cards": [], "scope": scope, "prune": False}
    if not isinstance(summary, dict):
        raise ReadError("Codex returned an invalid reset-card summary.")

    credits = summary.get("credits")
    count = summary.get("availableCount")
    if credits is None:
        empty = scope is not None and type(count) is int and count == 0
        return {"cards": [], "scope": scope, "prune": empty}
    if not isinstance(credits, list):
        raise ReadError("Codex returned an invalid reset-card list.")

    complete = scope is not None and type(count) is int and count == len(credits)
    seen = set()
    cards = []
    for credit in credits:
        if (
            not isinstance(credit, dict)
            or credit.get("status") != "available"
            or credit.get("resetType") != "codexRateLimits"
        ):
            complete = False
            continue
        raw_id = credit.get("id")
        expiry = credit.get("expiresAt")
        if not isinstance(raw_id, str) or not raw_id or "expiresAt" not in credit:
            complete = False
            continue
        if raw_id in seen:
            complete = False
            continue
        seen.add(raw_id)
        if expiry is None:
            continue
        if type(expiry) is not int:
            complete = False
            continue
        if expiry > now:
            # The card identifier stays stable even if the backend omits accountId.
            cards.append({"id": fingerprint("card", raw_id), "expiresAt": expiry})
    cards.sort(key=lambda card: (card["expiresAt"], card["id"]))
    return {"cards": cards, "scope": scope, "prune": complete}


def profile_identity(profile):
    if not isinstance(profile, dict) or "account" not in profile:
        return None
    account = profile["account"]
    if account is None:
        return ("signedOut", None, None, None)
    if not isinstance(account, dict) or not isinstance(account.get("type"), str):
        return None

    def value(text):
        return text if isinstance(text, str) and text else None

    return (
        account["type"],
        value(profile.get("userId")),
        value(account.get("email")),
        value(account.get("workspaceName")),
    )


def validated_email(email):
    if not isinstance(email, str) or not 1 <= len(email) <= 254 or not email.isprintable():
        return None
    local, _, domain = email.rpartition("@")
    if not re.fullmatch(r"[\w!#$%&'*+\-/=?^_`{|}~.]+", local):
        return None
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    if not re.fullmatch(rf"(?:{label}\.)+{label}", ascii_domain):
        return None
    return email


def add_account_email(result, before, after):
    result["accountEmail"] = None
    earlier = profile_identity(before)
    later = profile_identity(after)
    if earlier is None or later is None:
        return result
    if any(old is not None and new is not None and old != new for old, new in zip(earlier, later)):
        result["prune"] = False
        return result

    kind, earlier_user, earlier_email, _ = earlier
    _, later_user, later_email, _ = later
    shared_identity = (earlier_user is not None and earlier_user == later_user) or (
        earlier_email is not None and earlier_email == later_email
    )
    if result["scope"] is not None and kind == "chatgpt" and shared_identity:
        result["accountEmail"] = validated_email(later_email or earlier_email)
    return result


async def send(process, message):
    process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
    await process.stdin.drain()


async def receive(process, request_id, label):
    while True:
        line = await process.stdout.readline()
        if not line:
            raise ReadError("Codex App Server ended before replying.")
        if not line.strip():
            continue
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ReadError("Codex App Server returned an invalid message.")
        if message.get("id") != request_id or "method" in message:
            continue
        result = message.get("result")
        if "error" in message or not isinstance(result, dict):
            raise ReadError(f"Codex App Server could not {label}. Check that Codex is signed in and online.")
        return result


async def read_profile(process, request_id):
    try:
        await send(process, {
            "id": request_id,
            "method": "account/read",
            "params": {"refreshToken": False},
        })
        return await asyncio.wait_for(receive(process, request_id, "read account profile"), timeout=8)
    except (ReadError, asyncio.TimeoutError, OSError, ValueError):
        return None


async def request_cards(process):
    await send(process, {
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "codex-reset-calendar",
                "title": "Codex Reset Calendar",
                "version": "1.0.0",
            },
        },
    })
    await receive(process, 1, "initialize")
    await send(process, {"method": "initialized"})
    before = await read_profile(process, 2)
    await send(process, {"id": 3, "method": "account/rateLimits/read"})
    snapshot = await receive(process, 3, "read reset cards")
    after = await read_profile(process, 4)
    return add_account_email(extract_cards(snapshot, time.time()), before, after)


async def read_cards(codex):
    process = await asyncio.create_subprocess_exec(
        codex, "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=2 * 1024 * 1024,
    )
    try:
        return await asyncio.wait_for(request_cards(process), timeout=40)
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.communicate(), timeout=3)
        except asyncio.TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Path to the Codex executable")
    args = parser.parse_args()
    codex = args.codex or shutil.which("codex")
    if not codex and Path(BUNDLED_CODEX).is_file():
        codex = BUNDLED_CODEX
    if not codex:
        print("Codex executable was not found.", file=sys.stderr)
        return 1

    try:
        result = asyncio.run(read_cards(codex))
    except ReadError as error:
        print(error, file=sys.stderr)
        return 1
    except asyncio.TimeoutError:
        print("Codex App Server timed out while reading reset cards.", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Could not start Codex App Server or read its reply.", file=sys.stderr)
        return 1

    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
