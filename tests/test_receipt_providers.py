"""Receipt providers in gmail_scraper: parsing, currency, accounts, cancellations, scrape/dry-run.

Stdlib only. Synthetic .eml fixtures (fake ids) live in tests/fixtures/receipts. Gmail is a fake
in-memory service; billing.load_config is patched so the real config.json is never read, and
billing.merge_scraped is patched so billing.json is never written.
"""
import base64
import tempfile
import unittest
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as email_policy
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (sys.path insert of the repo root)
import billing
import gmail_scraper as gs

FIX = Path(__file__).resolve().parent / "fixtures" / "receipts"
CLAUDEX_CFG = {"accounts": {"claudex": {"paid_by": "openai_chatgpt"}}}


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def cfg(d):
    return mock.patch.object(billing, "load_config", return_value=d)


def eml(frm, subject, text, date="Mon, 20 Jul 2026 16:45:00 +0000", mid="inline-0001", html=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = frm, "user@example.com", subject, date
    m["Message-ID"] = f"<{mid}@mail.example.com>"
    if html is None:
        m.set_content(text, charset="utf-8")
    else:
        m.set_content(html, subtype="html", charset="utf-8")
    return bytes(m)


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class FakeGmail:
    """Stub of service.users().messages().list/get(...).execute(); records calls."""

    def __init__(self, by_query: dict, raws: dict):
        self.by_query, self.raws, self.calls = by_query, raws, []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults=100, pageToken=None):
        self.calls.append(("list", q))
        ids = self.by_query.get(q, [])
        return _Exec({"messages": [{"id": i} for i in ids], "resultSizeEstimate": len(ids)} if ids else {})

    def get(self, userId, id, format="full", metadataHeaders=None):
        self.calls.append(("get", id, format))
        data = self.raws[id]
        if format == "raw":
            return _Exec({"id": id, "raw": base64.urlsafe_b64encode(data).decode("ascii")})
        msg = message_from_bytes(data, policy=email_policy)
        return _Exec({"id": id, "payload": {"headers": [
            {"name": h, "value": str(msg[h] or "")} for h in (metadataHeaders or [])]}})


GP_FILES = {
    "gp1": "google_play_plus_purchase.eml", "gp2": "google_play_plus_renewal.eml",
    "gp3": "google_play_upgrade_proration.eml", "gp4": "google_play_pro_renewal.eml",
    "gp5": "google_play_payment_declined.eml", "gp6": "google_play_cancellation.eml",
}


class GooglePlayTests(unittest.TestCase):
    def parse(self, name, config=CLAUDEX_CFG):
        with cfg(config):
            return gs.PROVIDERS["google_play"].parse(raw(name))

    def test_real_history_layouts(self):
        want = {
            "google_play_plus_purchase.eml": ("GPA.3399-0000-1111-22222", "2026-05-14", "2026-06-14",
                                              "ChatGPT Plus", 76.99, 3.67, 19.96),
            "google_play_plus_renewal.eml": ("GPA.3399-0000-1111-22222..0", "2026-06-14", "2026-07-14",
                                             "ChatGPT Plus", 76.99, 3.67, 19.96),
            "google_play_upgrade_proration.eml": ("GPA.3399-0000-1111-33333", "2026-07-13", "2026-07-14",
                                                  "ChatGPT Pro", 9.97, 0.47, 2.59),
            "google_play_pro_renewal.eml": ("GPA.3399-0000-1111-22222..1", "2026-07-14", "2026-08-14",
                                            "ChatGPT Pro", 384.99, 18.33, 99.84),
        }
        for name, (rid, start, end, plan, orig, vat, usd) in want.items():
            with self.subTest(name):
                c = self.parse(name)
                self.assertEqual(set(c), set(gs.CHARGE_KEYS))
                self.assertEqual(c["kind"], "charge")
                self.assertEqual(c["receipt_id"], rid)
                self.assertEqual((c["start"], c["end"]), (start, end))
                self.assertEqual(c["plan"], plan)
                self.assertEqual(c["amount_original"], orig)
                self.assertEqual(c["vat_original"], vat)
                self.assertEqual(c["amount_usd"], usd)
                self.assertEqual(c["currency"], "AED")
                self.assertEqual(c["fx_rate"], 3.6725)
                self.assertFalse(c["fx_approximate"])
                self.assertEqual(c["provider"], "openai_chatgpt")
                self.assertEqual(c["store"], "google_play")
                self.assertEqual(c["merchant"], "OpenAI OpCo, LLC")
                self.assertEqual(c["source"], "gmail")
                self.assertEqual(c["account"], "claudex")

    def test_upgrade_raw_plan_is_new_plan(self):
        self.assertEqual(self.parse("google_play_upgrade_proration.eml")["raw_plan"], "ChatGPT Pro (ChatGPT)")

    def test_default_account_without_config(self):
        self.assertEqual(self.parse("google_play_plus_purchase.eml", {})["account"], "claudex")

    def test_declined_is_not_a_charge(self):
        self.assertIsNone(self.parse("google_play_payment_declined.eml"))

    def test_cancellation_record(self):
        c = self.parse("google_play_cancellation.eml")
        self.assertEqual(c["kind"], "cancellation")
        self.assertEqual(c["effective"], "2026-08-14")
        self.assertEqual(c["provider"], "openai_chatgpt")
        self.assertEqual(c["merchant"], "OpenAI OpCo, LLC")
        self.assertEqual(c["plan"], "ChatGPT Pro")
        self.assertEqual(c["account"], "claudex")
        self.assertNotIn("amount_usd", c)

    def test_missing_order_number_gets_stable_id(self):
        body = ("Order date: 14 May 2026 02:44:35 GST\nItem Price\n"
                "ChatGPT Plus (ChatGPT) 76.99 د.إ./month\nTotal: 76.99 د.إ./month\n(Includes VAT of 3.67 د.إ.)\n")
        b = eml("Google Play <googleplay-noreply@google.com>", "Your Google Play Order Receipt", body)
        with cfg({}):
            a, b2 = gs.parse_google_play(b), gs.parse_google_play(b)
        self.assertTrue(a["receipt_id"].startswith("msg-"))
        self.assertEqual(a["receipt_id"], b2["receipt_id"])
        self.assertEqual(a["amount_usd"], 19.96)

    def test_html_only_body(self):
        html = ("<html><body><table><tr><td>Order number: GPA.3399-0000-1111-44444</td></tr>"
                "<tr><td>Order date: 14 May 2026 02:44:35 GST</td></tr><tr><td>Item</td><td>Price</td></tr>"
                "<tr><td>ChatGPT Plus (ChatGPT)</td><td>76.99&nbsp;د.إ./month</td></tr>"
                "<tr><td>Total: 76.99 د.إ./month</td></tr><tr><td>(Includes VAT of 3.67 د.إ.)</td></tr>"
                "</table></body></html>")
        b = eml("Google Play <googleplay-noreply@google.com>", "Your Google Play Order Receipt", "", html=html)
        with cfg({}):
            c = gs.parse_google_play(b)
        self.assertEqual((c["receipt_id"], c["start"], c["end"], c["amount_usd"]),
                         ("GPA.3399-0000-1111-44444", "2026-05-14", "2026-06-14", 19.96))

    def test_unrelated_google_play_purchase_ignored(self):
        b = eml("Google Play <googleplay-noreply@google.com>", "Your Google Play Order Receipt",
                "Order number: GPA.1-2\nOrder date: 1 May 2026\nItem Price\nSome Game Coins 9.99 د.إ.\nTotal: 9.99 د.إ.\n")
        self.assertIsNone(gs.parse_google_play(b))


class OtherProviderTests(unittest.TestCase):
    def test_anthropic_legacy_keys_unchanged(self):
        with cfg({}):
            c = gs.parse_receipt(raw("anthropic_max.eml"))
        legacy = {"receipt_id": "2443-1340-7804", "start": "2026-04-06", "end": "2026-05-06",
                  "plan": "Max 20x", "raw_plan": "Max plan - 20x", "amount_usd": 200.0, "source": "gmail"}
        self.assertEqual({k: c[k] for k in legacy}, legacy)
        self.assertEqual((c["provider"], c["merchant"], c["account"], c["kind"], c["currency"]),
                         ("anthropic", "Anthropic", "personal", "charge", "USD"))
        self.assertEqual(set(c), set(gs.CHARGE_KEYS))
        self.assertIsNone(gs.parse_receipt(raw("google_play_plus_purchase.eml")))

    def test_openai_chatgpt_stripe(self):
        with cfg({}):
            c = gs.parse_openai_chatgpt(raw("openai_chatgpt_stripe.eml"))
            self.assertIsNone(gs.parse_openai_api(raw("openai_chatgpt_stripe.eml")))
        self.assertEqual((c["provider"], c["store"], c["plan"]), ("openai_chatgpt", "stripe", "ChatGPT Plus"))
        self.assertEqual((c["start"], c["end"]), ("2026-07-14", "2026-08-14"))
        self.assertEqual((c["amount_original"], c["vat_original"], c["amount_usd"]), (21.0, 1.0, 20.0))
        self.assertEqual(c["receipt_id"], "openai-1234-5678")
        self.assertEqual(c["account"], "claudex")

    def test_openai_api_credits(self):
        with cfg({}):
            c = gs.parse_openai_api(raw("openai_api_credits.eml"))
            self.assertIsNone(gs.parse_openai_chatgpt(raw("openai_api_credits.eml")))
        self.assertEqual((c["provider"], c["plan"]), ("openai_api", "OpenAI API credits"))
        self.assertEqual((c["start"], c["end"]), ("2026-07-20", "2026-07-21"))
        self.assertEqual((c["amount_original"], c["vat_original"], c["amount_usd"]), (52.5, 2.5, 50.0))
        self.assertEqual(c["account"], "openai-api")

    def test_openai_api_never_claudex_or_codex_cli(self):
        for bad in ("claudex", "codex-cli"):
            with self.subTest(bad), cfg({"accounts": {bad: {"paid_by": "openai_api"}}}):
                self.assertEqual(gs.suggest_account("openai_api"), "openai-api")
                self.assertEqual(gs.parse_openai_api(raw("openai_api_credits.eml"))["account"], "openai-api")
        with cfg({"accounts": {"claudex": {"paid_by": "openai_api"}, "api-org": {"paid_by": "openai_api"}}}):
            self.assertEqual(gs.suggest_account("openai_api"), "api-org")

    def test_suggest_account_from_config_and_defaults(self):
        with cfg({"accounts": {"work": {"paid_by": "anthropic"}, "gpt": {"paid_by": "openai_chatgpt"}}}):
            self.assertEqual(gs.suggest_account("anthropic"), "work")
            self.assertEqual(gs.suggest_account("openai_chatgpt"), "gpt")
        with cfg({}):
            self.assertEqual([gs.suggest_account(p) for p in ("anthropic", "openai_chatgpt", "openai_api")],
                             ["personal", "claudex", "openai-api"])

    def test_apple_claude_eur_approximate_then_override(self):
        with cfg({}):
            c = gs.parse_apple(raw("apple_claude_pro_eur.eml"))
        self.assertEqual((c["provider"], c["store"], c["plan"], c["account"]),
                         ("anthropic", "apple_app_store", "Pro", "personal"))
        self.assertEqual((c["start"], c["end"]), ("2026-06-03", "2026-07-03"))
        self.assertEqual((c["currency"], c["amount_original"], c["vat_original"]), ("EUR", 22.99, 3.67))
        self.assertTrue(c["fx_approximate"])
        self.assertEqual(c["amount_usd"], round(19.32 / 0.92, 2))
        with cfg({"fx_per_usd": {"EUR": 0.9}}):
            c2 = gs.parse_apple(raw("apple_claude_pro_eur.eml"))
        self.assertFalse(c2["fx_approximate"])
        self.assertEqual((c2["fx_rate"], c2["amount_usd"]), (0.9, 21.47))

    def test_apple_chatgpt_maps_to_openai_chatgpt(self):
        body = ("DATE\nJul 14, 2026\nORDER ID\nMQ7TEST123\nChatGPT\nChatGPT Plus (Monthly)\n"
                "Renews Aug 14, 2026\n$19.99\nSubtotal $19.99\nTax $0.00\nTOTAL $19.99\n")
        b = eml("Apple <no_reply@email.apple.com>", "Your receipt from Apple.", body)
        with cfg(CLAUDEX_CFG):
            c = gs.parse_apple(b)
        self.assertEqual((c["provider"], c["store"], c["plan"], c["account"], c["amount_usd"]),
                         ("openai_chatgpt", "apple_app_store", "ChatGPT Plus", "claudex", 19.99))
        self.assertEqual((c["start"], c["end"], c["receipt_id"]), ("2026-07-14", "2026-08-14", "apple-MQ7TEST123"))

    def test_money_and_fx_helpers(self):
        self.assertEqual(gs.parse_money("Total: 76.99 د.إ./month"), (76.99, "AED"))
        self.assertEqual(gs.parse_money("Qty 1 $20.00"), (20.0, "USD"))
        self.assertEqual(gs.parse_money("22,99 €"), (22.99, "EUR"))
        self.assertEqual(gs.parse_money("AED 1,234.50"), (1234.5, "AED"))
        self.assertEqual(gs.parse_money("£9.99"), (9.99, "GBP"))
        with cfg({}):
            self.assertEqual(gs.fx_rate("AED"), (3.6725, False))
            self.assertEqual(gs.fx_rate("USD"), (1.0, False))
            self.assertTrue(gs.fx_rate("EUR")[1])
            self.assertEqual(gs.fx_rate("XYZ"), (None, True))


class CancellationTests(unittest.TestCase):
    def test_clamps_overrunning_charge_only(self):
        charges = [
            {"kind": "charge", "provider": "openai_chatgpt", "account": "claudex", "receipt_id": "a",
             "start": "2026-07-14", "end": "2026-08-14"},
            {"kind": "charge", "provider": "openai_chatgpt", "account": "other", "receipt_id": "b",
             "start": "2026-07-14", "end": "2026-08-14"},
            {"kind": "charge", "provider": "anthropic", "account": "claudex", "receipt_id": "c",
             "start": "2026-07-14", "end": "2026-08-14"},
            {"kind": "charge", "provider": "openai_chatgpt", "account": "claudex", "receipt_id": "d",
             "start": "2026-08-20", "end": "2026-09-20"},
        ]
        canc = [{"kind": "cancellation", "provider": "openai_chatgpt", "account": "claudex",
                 "effective": "2026-08-01"}]
        out = gs.apply_cancellations(charges, canc)
        self.assertEqual([c["end"] for c in out], ["2026-08-01", "2026-08-14", "2026-08-14", "2026-09-20"])
        self.assertEqual(charges[0]["end"], "2026-08-14")  # input untouched

    def test_real_history_cancellation_is_noop_at_period_end(self):
        with cfg(CLAUDEX_CFG):
            pro = gs.parse_google_play(raw("google_play_pro_renewal.eml"))
            canc = gs.parse_google_play(raw("google_play_cancellation.eml"))
        self.assertEqual(gs.apply_cancellations([pro], [canc])[0]["end"], "2026-08-14")


def _gmail():
    ids = {k: raw(v) for k, v in GP_FILES.items()}
    ids.update(st=raw("openai_chatgpt_stripe.eml"), api=raw("openai_api_credits.eml"),
               apl=raw("apple_claude_pro_eur.eml"), an=raw("anthropic_max.eml"),
               funded=eml("OpenAI <noreply@tm.openai.com>", "Your OpenAI API account has been funded",
                          "We've successfully processed your payment of $52.50 for OpenAI API credits.\n"
                          "You added $50.00 in credits to your organization.\n"))
    q = {p: gs.PROVIDERS[p].query for p in gs.PROVIDERS}
    by_query = {q["google_play"]: list(GP_FILES), q["anthropic"]: ["an"], q["apple_app_store"]: ["apl"],
                q["openai_chatgpt"]: ["st", "api"],   # overlapping hits: each mail claimed once
                q["openai_api"]: ["api", "st", "funded"]}
    return FakeGmail(by_query, ids)


class ScrapeTests(unittest.TestCase):
    def test_scrape_all_dedupes_and_returns_cancellations(self):
        svc = _gmail()
        with cfg(CLAUDEX_CFG):
            rows = gs.scrape_all(svc)
        charges = [r for r in rows if r.get("kind") == "charge"]
        self.assertEqual(sorted(r["receipt_id"] for r in charges), sorted([
            "GPA.3399-0000-1111-22222", "GPA.3399-0000-1111-22222..0", "GPA.3399-0000-1111-33333",
            "GPA.3399-0000-1111-22222..1", "openai-1234-5678", "openai-2214-3392", "apple-MZ4K2LQ9XB",
            "2443-1340-7804"]))  # funded notice deduped against its Stripe receipt
        self.assertEqual([r["effective"] for r in rows if r.get("kind") == "cancellation"], ["2026-08-14"])
        self.assertFalse([r for r in rows if "_error" in r])
        # each raw message fetched once even when several queries return it
        raws = [c[1] for c in svc.calls if c[0] == "get" and c[2] == "raw"]
        self.assertEqual(len(raws), len(set(raws)))

    def test_scrape_all_provider_subset(self):
        svc = _gmail()
        with cfg({}):
            rows = gs.scrape_all(svc, providers=["google_play"])
        self.assertEqual({r["store"] for r in rows}, {"google_play"})
        self.assertEqual([c[1] for c in svc.calls if c[0] == "list"], [gs.PROVIDERS["google_play"].query])
        with self.assertRaises(ValueError):
            gs.scrape_all(svc, providers=["nope"])

    def test_dry_run_headers_counts_no_bodies_no_writes(self):
        svc = _gmail()
        with cfg(CLAUDEX_CFG), mock.patch.object(billing, "merge_scraped", side_effect=AssertionError), \
                mock.patch.object(billing, "save_charges", side_effect=AssertionError):
            rep = gs.dry_run(svc)
        self.assertEqual(set(rep), set(gs.PROVIDERS))
        gp = rep["google_play"]
        self.assertEqual(gp["count"], 6)
        self.assertEqual(gp["query"], gs.PROVIDERS["google_play"].query)
        self.assertEqual(gp["messages"][0]["from"], "Google Play <googleplay-noreply@google.com>")
        self.assertIn("Payment declined for ChatGPT subscription", [m["subject"] for m in gp["messages"]])
        self.assertEqual(len(gp["parsed"]), 5)  # declined -> nothing
        self.assertEqual([p["kind"] for p in gp["parsed"]].count("cancellation"), 1)
        for m in gp["messages"]:
            self.assertEqual(set(m), {"from", "subject", "date"})
        self.assertNotIn("Mastercard", repr(rep))  # no body text leaks
        self.assertEqual([c for c in svc.calls if c[0] == "list"].__len__(), len(gs.PROVIDERS))
        self.assertTrue(any(c[0] == "get" and c[2] == "metadata" for c in svc.calls))

    def test_scrape_and_save_merges_charges_only(self):
        svc = _gmail()
        seen = {}

        def merge(rows):
            seen["rows"] = list(rows)
            return {"added": len(seen["rows"]), "updated": 0, "total": len(seen["rows"])}

        with cfg(CLAUDEX_CFG), mock.patch.object(gs, "build_service", return_value=svc), \
                mock.patch.object(billing, "merge_scraped", side_effect=merge):
            res = gs.scrape_and_save()
        self.assertEqual({r["kind"] for r in seen["rows"]}, {"charge"})
        self.assertEqual(res["found"], 8)
        self.assertEqual(res["errors"], 0)
        self.assertEqual((res["added"], res["updated"], res["total"]), (8, 0, 8))
        self.assertEqual(len(res["cancellations"]), 1)

    def test_build_service_non_interactive_raises_without_token(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("google_auth_oauthlib.flow.InstalledAppFlow.run_local_server",
                           side_effect=AssertionError("browser flow launched")):
            with self.assertRaises(gs.GmailAuthError):
                gs.build_service(Path(d) / "credentials.json", Path(d) / "token.json", interactive=False)


if __name__ == "__main__":
    unittest.main()
