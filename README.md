# Claude Usage Dashboard

A **fully local** dashboard for your personal Claude Code usage. Reads conversation logs from `~/.claude/projects/`, indexes them into SQLite, and shows tokens, costs, top projects, longest conversations, and how much you saved by subscribing vs paying API rates.

> **No data leaves your computer.** Logs, the SQLite database, billing receipts, and Gmail OAuth tokens all stay in this folder. There is no remote server, no telemetry, nothing phones home.

Idle CPU is near zero — only re-parses `.jsonl` files whose mtime changed.

## Run it

**Windows**

```cmd
start.bat
```

**macOS / Linux**

```bash
chmod +x start.sh
./start.sh
```

First launch creates a virtualenv, installs deps, and opens `http://127.0.0.1:8765` in your browser.

Requires Python 3.10+.

## Connect Gmail (auto-fill billing data)

Click **✉ Connect Gmail** in the top bar. It pulls your Anthropic receipts and writes them to `billing.json` so the "Saved by subscribing" math is accurate. All data and tokens stay on your machine.

One-time setup (~5 minutes):

1. [console.cloud.google.com](https://console.cloud.google.com/projectcreate) → create a new project.
2. [Enable the Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com).
3. [OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent) → **External**, fill the basics, add yourself as a **Test user**.
4. [Credentials](https://console.cloud.google.com/apis/credentials) → **Create credentials → OAuth client ID → Desktop app** → download the JSON.
5. Save it as `credentials.json` next to `dashboard.py`, then click the button again.

Scope is read-only Gmail. Revoke any time at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## Manually editing billing

If you don't want to connect Gmail, copy `billing.json.example` to `billing.json` and add one entry per receipt: `{receipt_id, start, end, plan, amount_usd, source: "manual"}`. Amounts are excluding tax. Manual entries are never overwritten by the scraper.

## Files of note

| File | Purpose |
|---|---|
| `indexer.py` | Walks `~/.claude/projects/**/*.jsonl`, writes deduped messages to `usage.db`. |
| `dashboard.py` | FastAPI server on `127.0.0.1:8765`. |
| `pricing.py` | Anthropic per-token pricing table (edit when prices change). |
| `billing.py` | Reads `billing.json`, prorates subscription cost across windows. |
| `gmail_scraper.py` | Optional Gmail OAuth + receipt parser. |
| `usage.db` | Generated; safe to delete (`python indexer.py --force` rebuilds). |

## What about people who don't use a subscription?

If `billing.json` is missing or empty, the dashboard hides the "Subscription paid" and "Saved by subscribing" cards and just shows the API cost — your would-have-paid number. No nagging, no required setup.

## Export

Click **⬇ Export** in the top bar to download a PNG of the highlights (KPI cards, daily activity, activity heatmap) for the currently selected time window. The image is generated entirely in your browser via `html2canvas` — nothing is uploaded.

## Privacy

`usage.db`, `billing.json`, `credentials.json`, and `token.json` are all gitignored. Nothing leaves your machine. The Gmail scope is read-only and used only to fetch emails from `invoice+statements@mail.anthropic.com`. Revoke the Gmail permission any time at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## License

[MIT](LICENSE).
