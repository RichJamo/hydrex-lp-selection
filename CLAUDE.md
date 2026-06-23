# CLAUDE.md — hydrex-lp-selection

Context for Claude Code working in this repo. Keep this file up to date as the
project evolves.

## What this project is

A data-driven pipeline for choosing which LP pools Hydrex (a ve(3,3) CLAMM DEX on
Base) should bootstrap each weekly epoch. Goal: pick pools that earn **more in
trading fees than we spend incentivizing them**.

This repo is a **fork/extension of `beawesomelee/hydrex-lp-dashboard`** (Austin's).
The original tracks pools we've already picked (outcomes); this adds the step
*before* that — scanning the market, scoring candidates, recommending 2–4 picks
per epoch.

## The core metric

- Success = **`$Fees / $Incentive ≥ 1`** for a pool over an epoch.
- Training label (Aerodrome proxy) = **`profitable = fees_usd > emissions_usd`**.
- Incentive cost: paid in oHYDX, valued as **`HYDX_price × 0.7`** (30% option discount).
- Capital-efficiency benchmark: Aerodrome runs ≈ **$30 TVL per $1 incentive**.

## Architecture / file map

| File | Role |
|------|------|
| `candidate_pull.py` | Daily DexScreener scout → `data/candidates_daily.csv`. Applies filters, derives pool age / vol-TVL / buy-sell ratio. |
| `fee_enrich.py` | Adds fee tier + fee estimate via **on-chain reads** → `data/candidates_enriched.csv`. |
| `proxy_dataset.py` | Aerodrome labeled training set via Dune API → `data/aerodrome_proxy.csv`. |
| `score.py` | Phase-1 transparent weighted ranking → `data/weekly_picks.csv`. `--feature-importance` = phase-2 ML on proxy labels. |
| `selection_config.json` | All thresholds + scoring weights. Tune here, not in code. |
| `hydrex_daily_pull.py` | (carried over) own-pool daily metrics from Hydrex subgraph. |
| `weekly_bootstrap_update.py` | (carried over) per-epoch outcomes → `data/bootstrap_tracker.csv`. Our validation labels. |
| `bootstrap_picks.json` | (carried over) which pools we're incentivizing each epoch. |
| `retention_scorecard.py` | Aggregates `bootstrap_tracker.csv` per pool → KEEP/WATCH/CUT re-incentivize call. Outputs `data/retention_scorecard.csv` + `retention.html` + console table. Answers "which of our pools should we keep funding?" `--refresh-market` pulls total Hydrex fees/epoch into `data/market_fees.csv` for the fee-share (beta-vs-alpha) signal that filters market-wide drops out of the decay call. `--image [--highlight "PAIR,PAIR"]` renders `retention_scorecard.png` (gitignored) via headless Chrome for sharing. |

Pipeline: `candidate_pull.py` → `fee_enrich.py` → `score.py`. Run the Aero proxy
separately to build training data, then `score.py --feature-importance`.

## Key technical facts (verified Jun 2026 — don't relitigate)

- **DexScreener has NO fees or fee tier.** Pair schema only exposes price,
  volume, liquidity, fdv, marketCap, txns, `pairCreatedAt`, labels. Fees must be
  derived as `volume × fee_tier`; the tier comes from on-chain.
- **Hydrex pools are Algebra Integral CLAMM.** Read the fee via
  `globalState()` → `lastFee` (millionths; `500` = 0.05% = 5 bps). Confirmed on
  VVV/USDC `0x02107b…`. Uniswap/Aerodrome-style pools fall back to `fee()`.
- **Algebra fees are dynamic** — `lastFee` is a snapshot, and pools can quote a
  lower fee on the first swap of a block to game aggregators. On-chain fee is a
  ranking signal, NOT ground truth. For *realized* fees use the Hydrex subgraph
  (`feesUSD`, see `hydrex_daily_pull.py`). Enriched rows carry a `dynamic_fee` flag.
- **Candidate filters (agreed Jun 9 meeting):** liquidity ≥ $50k AND market cap
  ≥ $1M (strips meme coins / illiquid junk).
- **Epoch model:** Thursday epoch flip; `hydrex_epoch + 107 = aero_epoch`.

## The two-phase plan

1. **Phase 1 (now):** transparent weighted score in `selection_config.json`
   (est fees, fees/TVL, primary-LP share, newness, balanced flow). Explainable;
   tune weights freely.
2. **Phase 2 (later):** once `aerodrome_proxy.csv` has enough labeled pool-epochs,
   `score.py --feature-importance` learns which features predict profitability.
   Use that to re-weight phase 1. This sidesteps our small in-house sample (~20 pools).

## Setup / commands

```bash
pip install -r requirements.txt
python candidate_pull.py && python fee_enrich.py && python score.py   # full run
python score.py --feature-importance                                  # phase 2
```

Environment / secrets:
- `BASE_RPC_URL` — any Base RPC (needed by `fee_enrich.py`).
- `DUNE_API_KEY` — only for `proxy_dataset.py`.
- `selection_config.json → proxy_dataset.aerodrome_query_id` — point at the
  fees/emissions-per-pool query on https://dune.com/0xkhmer/aerodrome.

## Open follow-ups

- Wire fee tier into `weekly_bootstrap_update.py` (the dashboard shows a Fee Tier
  column the CSV doesn't store yet).
- Set `aerodrome_query_id` and build the first proxy dataset.
- Build the **bootstrap-optimization** module: fee-tier choice vs Aero, router
  TVL threshold (~$5k?), opposite-token pairing (WETH/USDC/cbBTC), incentive
  sizing + duration / cut rules.

## Conventions

- Keep all tunables in `selection_config.json`; avoid hardcoding thresholds.
- New CSVs go in `data/`. The daily GitHub Action commits them.
- Don't overwrite the carried-over files from Austin's repo without reason —
  their outputs are our validation labels.
