# Claude Usage Dashboard

> A local, one-command dashboard that tells you how much you've actually saved (or not) by using Claude's subscription tiers vs. paying API list prices for the same usage. Runs entirely on your machine — no data leaves your device.

![Highlights view](docs/highlights.png)

## What it does

Reads your local Claude Code conversation history, tallies tokens and messages by model and project, prices the same usage at Claude's API list rates, and compares the result to what you've actually paid in subscriptions.

The output is two views:

- **Highlights** — total tokens, messages, sessions, projects, API-equivalent cost, subscription paid, the savings delta and multiplier, plus a daily activity bar chart.
- **Full report** — everything above, plus an activity heatmap, a model breakdown (donut + table), top projects, and a longest-conversations table.

![Full report](docs/full-report.png)

## Quick start

```bash
git clone https://github.com/ajmalaksar25/claude-usage-dashboard.git
cd claude-usage-dashboard

# Windows
start.bat

# macOS / Linux
chmod +x start.sh && ./start.sh
```

The script creates a virtualenv, installs deps, parses your conversation history, and opens the dashboard at `http://127.0.0.1:8765`. Requires Python 3.10+.

First load takes about ten seconds while it parses your conversation history. Subsequent loads are near-instant — only files whose mtime changed are re-parsed.

The dashboard is intentionally middle-ground: not a CLI you have to grep your way through, not a click-and-go consumer app. If you can run a command in a terminal, you can run this.

## Terminal CLI

Everything the dashboard shows is also available in the terminal — no server, no browser:

```bash
python cli.py                      # headline numbers, all time
python cli.py summary -w 1w        # this week
python cli.py projects -w 1m -n 10 # top projects, last month
python cli.py models               # tokens + cost per model
python cli.py accounts             # per-account rollup
python cli.py tools -w 1w          # tool usage        (index with: refresh --all)
python cli.py calls -n 20 --errors # latest errored tool calls
python cli.py refresh --all        # re-index logs incl. tool calls
python cli.py dash --all           # launch the web dashboard
```

Filters compose everywhere: `-w <window> -a <account> -p <project>`.

Or install the whole thing as real commands (works on Windows, macOS, Linux):

```bash
pipx install git+https://github.com/ajmalaksar25/claude-usage-dashboard.git
claude-usage refresh --all && claude-usage        # usage stats anywhere
ccm list                                          # profile manager anywhere
```

The installed copy keeps its database inside its own environment; point it elsewhere with `CLAUDE_USAGE_DB=/path/to/usage.db` or `--db`.

## How it reads your data

- **Conversations** — read from Claude Code's local store (default location varies by OS; the dashboard auto-detects).
- **API-equivalent cost** — calculated against Claude's published per-model API rates (input, output, cache read, cache write).
- **Subscription paid** — two options:
  - *Manual:* copy `billing.json.example` to `billing.json` and add one entry per receipt (`{receipt_id, start, end, plan, amount_usd, source: "manual"}`).
  - *Gmail invoice extraction:* click **✉ Connect Gmail** in the top bar. It pulls Anthropic's invoice emails and sums them. Read-only, runs locally.

No data is sent anywhere. The Gmail integration runs against your local OAuth token, parses invoices in-process, and discards the rest. Revoke any time at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

If `billing.json` is missing or empty, the dashboard hides the "Subscription paid" and "Saved by subscribing" cards and just shows the API cost — your would-have-paid number.

## Multiple accounts

Claude Code reads its config from `CLAUDE_CONFIG_DIR`, so running work and personal accounts side by side is just a matter of pointing that variable at different directories: `~/.claude` for the default account, `~/.claude-work`, `~/.claude-personal`, and so on. Each profile keeps its own credentials; the things you actually want everywhere — `skills/`, `agents/`, `commands/`, `rules/` — live once in `~/.claude-shared` and get linked into every profile.

Setting that up by hand is tedious, so the repo ships `ccm.py` (Claude Config Manager) — pure stdlib, no deps, works on macOS/Linux/Windows (falls back to NTFS junctions when symlinks need privileges):

```bash
# Move your existing skills/agents/commands/rules into ~/.claude-shared,
# leaving links behind in ~/.claude:
python ccm.py init-shared --from-profile default

# Create a second account, pre-linked to the shared resources:
python ccm.py create work

# Paste the printed alias into your shell rc, then log in once:
alias claude-work='CLAUDE_CONFIG_DIR="$HOME/.claude-work" claude'
claude-work

# See everything / print aliases for zsh, bash, and PowerShell / audit:
python ccm.py list
python ccm.py aliases --shell all
python ccm.py doctor
```

`ccm.py` never touches `.credentials.json`, `settings.json`, `projects/`, `todos/`, or `memory/` — those stay private to each profile. It also refuses to delete a real directory with content in it; `doctor` will tell you when something needs `init-shared --from-profile`.

The dashboard plays along automatically: it discovers every `~/.claude*/projects` directory as a separate account. If you keep profiles somewhere unusual, copy the convention into an `accounts.json` at the repo root (gitignored, like `billing.json`):

```json
[{"name": "work", "root": "~/.claude-work/projects"}]
```

---

## Want to rebuild this with Claude Code?

This README is also a build spec. If you'd rather not install — or you want to fork the idea, change the stack, or extend the calculations — drop this whole file into a Claude Code session and ask it to recreate the project. The spec below is intentionally framed by **behavior, not implementation**, so Claude Code can pick its own stack.

### Build spec

#### Goal

Build a local web dashboard that answers a single question: *"Is my Claude subscription actually saving me money compared to what I'd pay at API list prices for the same usage?"*

#### Inputs

- **Conversation history** — Claude Code's local conversation store. Auto-detect the path per OS (`~/.claude/...` on Unix-likes, `%APPDATA%\Claude\...` on Windows). Each conversation includes a model identifier, message-level token counts (input, output, cache read, cache write), timestamps, and a project identifier.
- **Subscription spend** — either:
  - User-entered tier(s) and effective date ranges, or
  - Gmail invoice extraction via OAuth (read-only). Filter for Anthropic invoice emails, parse amount and date, sum.
- **API rate table** — Claude's published per-model rates for input, output, cache read, cache write tokens. Hardcode current rates; allow override via config.

#### Required outputs

**Highlights view (compact, one-screen).**
1. Total tokens (with input/output/cache breakdown, e.g. `in 1.3M · out 18.8M · cache 6.4B`)
2. Messages count, with sessions and projects sub-counts
3. API cost at standard rates
4. Subscription paid (with tier history listed, e.g. `Pro · Pro extra · Max 5x · Max 20x`)
5. **Saved by subscribing** — primary visual, big number, with `Nx cheaper than API` badge
6. Daily activity chart — bars for tokens/day overlaid with a cumulative cost line
7. Header: date range, "runs locally · no data leaves your machine", generated-at timestamp

**Full report view (detailed, scrollable).**
1. Everything from Highlights
2. **Activity heatmap** — Mon/Wed/Fri rows × dates, intensity by token count. Show active-days fraction, peak day, daily average.
3. **By model** — donut chart of token share + table with model name, tokens, cost
4. **Top projects** — horizontal bar chart, ordered by tokens, with cost and token count per row
5. **Longest conversations** — table: project, model, message count, tokens, cost, duration
6. Footer: repo link, "Generated locally · no data left this device" indicator

#### Calculations

```
api_cost_per_message =
    input_tokens × model.input_rate
  + output_tokens × model.output_rate
  + cache_read_tokens × model.cache_read_rate
  + cache_write_tokens × model.cache_write_rate

api_cost_total = sum(api_cost_per_message for all messages)
saved          = api_cost_total − subscription_paid
multiplier     = api_cost_total / subscription_paid
```

#### Constraints

- Fully local. No outbound network calls except optional Gmail OAuth.
- One-command setup. Cloning, installing dependencies, parsing data, and serving the UI should all happen behind a single entry point.
- First load ≤ 10 seconds on a typical machine with a few months of history. Subsequent loads near-instant — cache parsed data and only re-parse changed conversations.
- Two clearly separated views (Highlights and Full report). The user should land on Highlights by default.

#### Stack hints (not prescriptive)

- **Backend:** any language with good local file access (Python and Node.js are both fine).
- **Frontend:** server-rendered HTML is fine; SPA is fine; whichever is faster to ship.
- **Charts:** any library that handles bar, donut, and heatmap. Chart.js, D3, or Recharts all work.
- **Persistence:** in-memory + a parsed-data cache file. No database needed.
- **Gmail:** Google's OAuth flow + Gmail API for invoice extraction. Skip if not requested.

#### Done criteria

- Run the one-command setup on a fresh checkout.
- Open the dashboard, see Highlights with real numbers from your local Claude Code history within 10 seconds.
- Switch to Full report, see the heatmap, model breakdown, top projects, and longest conversations populated correctly.
- Numbers reconcile: API cost matches a hand-calculation for one conversation; saved = API cost − subscription paid.

---

## Roadmap

- Multi-account support (currently parses one Claude account at a time)
- Per-project deep-dive view
- Export to JSON / CSV for further analysis
- Comparison mode for users on Cursor, Codex, or other AI coding tools
- Public/sharable summary card with PII stripped (for the inevitable Reddit thread)

## Feedback

If you run this and your numbers come out wildly different from mine, please [open an issue](https://github.com/ajmalaksar25/claude-usage-dashboard/issues) — that's the whole point. The dashboard is more useful as a debate-settler the more usage patterns it covers.

## License

[MIT](LICENSE).

## Author

[Ajmal Aksar](https://www.ajmalaksar.com) — backend dev who shipped this in two hours after a Reddit comment hit too close to home.
