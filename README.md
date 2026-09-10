# Hydrex LP Selection

Weekly pool-selection and retention analysis for [Hydrex](https://hydrex.fi), a
concentrated-liquidity DEX on Base. It decides which liquidity pools to back in the coming
epoch, then scores the last epoch's picks to work out what to keep and what to cut.

Forked from [beawesomelee/hydrex-lp-dashboard](https://github.com/beawesomelee/hydrex-lp-dashboard),
which built the original Aerodrome-versus-Hydrex dashboards. The discovery layer, the
selection model and the retention scorecard described below are my additions.

## What it does

**Discovery.** `candidate_pull.py` pulls pool candidates from the Aerodrome and Uniswap v3
subgraphs, with schema-agnostic queries so a subgraph change does not silently return
nothing. An inverse-connector pass recovers thin opposite-base pairs that a direct query
misses. `fee_enrich.py` then attaches fee data from chain.

**Selection.** `score.py` and `bootstrap_optimize.py` rank the candidates on an LP-yield
break-even model. Features are winsorized before scoring, and three filters run over the
result: a pick-liquidity floor, a quality floor, and exclusion of pools that failed in a
previous epoch.

**Retention.** `retention_scorecard.py` scores the picks that were actually made, epoch by
epoch, into a keep-or-cut decision. It carries a market-share signal that separates a pool
gaining share from one merely riding the market.

**Dashboards.** `build_site.py` assembles the pages and publishes them to GitHub Pages
through Actions. `daily_update.sh` runs the candidate scan every day, so the live tab does
not go stale between epochs.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env     # THEGRAPH_API_KEY, BASE_RPC_URL
python candidate_pull.py
python bootstrap_optimize.py
```

Python for the pipeline, plain HTML for the dashboards, no framework.
