# Josh Macro Playbook — Live Dashboard

A static site + a daily GitHub Actions job. No server to run, no monthly bill.
Everything that has a real free primary-source API updates automatically;
everything else (BOJ decisions, budget cycles, intervention confirmations)
is a hand-edited file so the dashboard never fakes "live" for something
that only changes a few times a year.

## Why this exists (read this before the setup steps)

Claude.ai Artifacts cannot call third-party APIs directly — the sandbox
blocks `fetch()` to anything except `api.anthropic.com`. That's a hard
platform limit, not a workaround problem. This repo moves the "genuinely
live" part *outside* that sandbox: GitHub's own servers run the fetch job,
with normal unrestricted internet access, on a schedule, for free.

## What's live vs. what's manual

| Indicator | Source | Cadence |
|---|---|---|
| Fed Funds Rate, 10Y/30Y/2Y UST, CPI, Unemployment, Debt/GDP, VIX, Buffett Indicator | FRED (official, free) | Daily |
| Bitcoin | CoinGecko | Daily |
| USD/JPY | Frankfurter (ECB reference rates) | Daily |
| 10/20/30/40Y JGB curve | Japan MOF `jgbcme.csv` (primary source) | Daily (JP business days) |
| Gold | gold-api.com | Daily — **verify this one on first run**, free gold APIs are the least stable link in the chain |
| Shiller CAPE | Yale/Shiller workbook | Daily attempt — **best-effort**, Excel parsing is fragile; if it breaks, the frontend just shows "stale" instead of crashing |
| BOJ policy rate, debt-service ratio, intervention log, fiscal calendar | `manual.json` — you edit this | Whenever the real thing happens |

## One-time setup (15 minutes)

1. **Create a new GitHub repo** (public, so Pages is free) and push these
   files to it.
2. **Get a free FRED API key**: https://fred.stlouisfed.org/docs/api/api_key.html
   — instant, no approval wait.
3. **Add it as a repo secret**: repo → Settings → Secrets and variables →
   Actions → New repository secret → name it `FRED_API_KEY`.
4. **Enable GitHub Pages**: repo → Settings → Pages → Source: "Deploy from
   a branch" → branch `main`, folder `/ (root)`.
5. **Run the workflow once manually** to populate real data before waiting
   for the first scheduled run: repo → Actions → "Update macro dashboard
   data" → Run workflow.
6. Your dashboard is live at `https://<your-username>.github.io/<repo-name>/`.

## Updating the manual (event-driven) data

Open `manual.json` directly in GitHub's web editor and commit changes —
no local setup needed. Update it when:

- BOJ makes a rate decision (8x/year)
- A new budget or supplementary budget is announced
- An intervention is reported (add a new entry to `intervention_log`)
- A political/fiscal event worth tracking happens (add to `fiscal_political_calendar`)

## Known fragility, stated plainly

- **Gold and CAPE are the two weakest links.** Both rely on less formal
  free sources than FRED/MOF. If either breaks, the dashboard marks it
  `stale` and keeps the last good value — it will not silently show a
  wrong number, but it also won't be truly live until you swap the source.
- **The Buffett Indicator here uses the Wilshire-5000-to-GDP proxy**, the
  common practitioner method — not the more rigorous (and slower, lagged)
  Fed Z.1 flow-of-funds market-cap figure. Close enough for a monthly
  cycle read; say so if you ever quote it externally.
- **This still isn't a Bloomberg terminal.** It's daily-cadence, not
  tick-by-tick. For the indicators here (Fed rate, CAPE, JGB curve, etc.)
  that's the right cadence anyway — none of them meaningfully move
  intraday in a way that matters for this framework.

## Companion: the in-chat quick-check dashboard

Alongside this repo, there's a Claude.ai Artifact version (in your
Macro Playbook project) that uses Claude + web search on demand — good
for pulling narrative context ("what actually happened this week") that
no API gives you. Treat *this* repo as source of truth for numbers, and
the in-chat one as the annotation layer on top of it.
