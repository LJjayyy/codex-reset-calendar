import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/codex-reset-calendar/scripts"))
from read_cards import add_account_email, extract_cards, read_cards, validated_email


def snapshot(credits, count, account="account-for-testing"):
    return {
        "accountId": account,
        "rateLimitResetCredits": {"availableCount": count, "credits": credits},
    }


def profile(email="example-user@example.com", user="user-for-testing", workspace="Personal"):
    return {
        "account": {"type": "chatgpt", "email": email, "workspaceName": workspace},
        "userId": user,
    }


class ReaderTests(unittest.TestCase):
    def test_authoritative_details_keep_only_future_expiring_cards(self):
        credits = [
            {"id": "visible", "status": "available", "resetType": "codexRateLimits", "expiresAt": 150},
            {"id": "expired", "status": "available", "resetType": "codexRateLimits", "expiresAt": 90},
            {"id": "no-expiry", "status": "available", "resetType": "codexRateLimits", "expiresAt": None},
        ]
        result = extract_cards(snapshot(credits, 3), now=100)
        self.assertTrue(result["prune"])
        self.assertEqual([card["expiresAt"] for card in result["cards"]], [150])
        self.assertNotIn("visible", json.dumps(result))
        self.assertTrue(extract_cards(snapshot([], 0), now=100)["prune"])
        self.assertTrue(extract_cards(snapshot(None, 0), now=100)["prune"])

    def test_unscoped_or_partial_reads_never_prune(self):
        credit = {"id": "visible", "status": "available", "resetType": "codexRateLimits", "expiresAt": 150}
        cases = [
            (snapshot([credit], 1, account=None), 1),
            (snapshot([credit], 2), 1),
            (snapshot([credit, credit], 2), 1),
            (snapshot([credit, {"id": "unclear", "status": "unknown"}], 2), 1),
            (snapshot([credit, {**credit, "id": "unknown-type", "resetType": "unknown"}], 2), 1),
            (snapshot([credit, {"id": "missing-expiry", "status": "available", "resetType": "codexRateLimits"}], 2), 1),
            (snapshot(None, 2), 0),
            (snapshot(None, 0, account=None), 0),
            ({}, 0),
        ]
        known_id = extract_cards(snapshot([credit], 1), now=100)["cards"][0]["id"]
        for payload, expected_count in cases:
            with self.subTest(payload=payload):
                result = extract_cards(payload, now=100)
                self.assertFalse(result["prune"])
                self.assertEqual(len(result["cards"]), expected_count)
                if result["cards"]:
                    self.assertEqual(result["cards"][0]["id"], known_id)

    def test_stable_scoped_accounts_return_the_original_address(self):
        address = "Last.First+code@Example.com"
        result = add_account_email(
            extract_cards(snapshot([], 0), now=100),
            profile(email=address),
            profile(email=None),
        )
        self.assertEqual(result["accountEmail"], address)
        self.assertTrue(result["prune"])
        self.assertNotIn("user-for-testing", json.dumps(result))
        by_email = add_account_email(
            extract_cards(snapshot([], 0), now=100), profile(email=address, user=None), profile(email=address),
        )
        self.assertEqual(by_email["accountEmail"], address)

    def test_account_switches_disable_prune_and_email(self):
        for after in [
            profile(user="other-user"),
            profile(email="another@example.com"),
            profile(workspace="Other workspace"),
            {"account": None},
            {"account": {"type": "apiKey"}},
        ]:
            with self.subTest(after=after):
                result = add_account_email(extract_cards(snapshot([], 0), now=100), profile(), after)
                self.assertIsNone(result["accountEmail"])
                self.assertFalse(result["prune"])

    def test_missing_profile_or_scope_skips_email(self):
        result = add_account_email(extract_cards(snapshot([], 0), now=100), None, profile())
        self.assertIsNone(result["accountEmail"])
        self.assertTrue(result["prune"])
        result = add_account_email(
            extract_cards(snapshot([], 0, account=None), now=100), profile(), profile(),
        )
        self.assertIsNone(result["accountEmail"])
        self.assertFalse(result["prune"])

    def test_validation_preserves_valid_addresses_and_rejects_unsafe_values(self):
        maximum = f"{'a' * 64}@{'b' * 63}.{'c' * 63}.{'d' * 61}"
        self.assertEqual(len(maximum), 254)
        for address in ["x@EXAMPLE.com", "名.字@例子.测试", maximum]:
            self.assertEqual(validated_email(address), address)
        for invalid in [
            None,
            "foo\nbar@example.com",
            "foo@exa\u202emple.com",
            "foo@example..com",
            "foo@example.com/path",
            "foo@@example.com",
            "foo <user@example.com>",
            " foo@example.com",
            "foo..bar@example.com",
            maximum + "d",
        ]:
            with self.subTest(invalid=invalid):
                self.assertIsNone(validated_email(invalid))

        malformed = profile(email="ex\nample@example.com")
        result = add_account_email(extract_cards(snapshot([], 0), now=100), malformed, malformed)
        self.assertIsNone(result["accountEmail"])
        self.assertTrue(result["prune"])

    def run_protocol_fixture(self, first_reply, second_reply):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "codex"
            fake.write_text(f"""#!{sys.executable}
import json
import sys
assert sys.argv[1:] == ["app-server"]
initialize = json.loads(input())
assert initialize["method"] == "initialize"
print(json.dumps({{"id": initialize["id"], "result": {{}}}}), flush=True)
assert json.loads(input()) == {{"method": "initialized"}}
assert json.loads(input()) == {{"id": 2, "method": "account/read", "params": {{"refreshToken": False}}}}
print(json.dumps({{"id": 2, **{first_reply!r}}}), flush=True)
assert json.loads(input()) == {{"id": 3, "method": "account/rateLimits/read"}}
print(json.dumps({{"method": "account/rateLimits/updated", "params": {{}}}}), flush=True)
print(json.dumps({{"id": 3, "result": {snapshot([], 0)!r}}}), flush=True)
assert json.loads(input()) == {{"id": 4, "method": "account/read", "params": {{"refreshToken": False}}}}
print(json.dumps({{"id": 4, **{second_reply!r}}}), flush=True)
""", encoding="utf-8")
            fake.chmod(0o755)
            return asyncio.run(read_cards(str(fake)))

    def test_protocol_ignores_notifications_and_reads_without_consuming(self):
        result = self.run_protocol_fixture({"result": profile()}, {"result": profile()})
        self.assertEqual(result, {
            **extract_cards(snapshot([], 0), now=100),
            "accountEmail": "example-user@example.com",
        })

    def test_protocol_keeps_quota_read_if_profile_endpoint_unavailable(self):
        error = {"error": {"code": -32601, "message": "method not found"}}
        result = self.run_protocol_fixture(error, error)
        self.assertEqual(result, {**extract_cards(snapshot([], 0), now=100), "accountEmail": None})


if __name__ == "__main__":
    unittest.main()
