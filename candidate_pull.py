"""
candidate_pull.py — daily candidate scouting via DexScreener.

Replaces the manual "eyeball the base_tokens_fees_tvl sheet" step. Discovers
Base pools, applies the agreed filters (>= $50k liquidity AND >= $1M market cap),
derives selection features, and appends to data/candidates_daily.csv.

IMPORTANT (verified Jun 2026): DexScreener's API does NOT expose swap fees or
fee tier. The pair schema only has price / volume / liquidity / fdv / marketCap /
txns / pairCreatedAt / labels. So this script captures everything DexScreener
*does* give (including pairCreatedAt -> pool age, a feature we wanted), and the
fee tier + fee estimate are added afterwards by fee_enrich.py via on-chain reads.

Run daily (GitHub Action). Network calls only work where outbound is allowed.
"""

import csv
import datetime as dt
import json
import os
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG = json.loads((SCRIPT_DIR / "selection_config.json").read_text())
OUT_CSV = SCRIPT_DIR / "data" / "candidates_daily.csv"

API = "https://api.dexscreener.com"
CHAIN = CONFIG["chain"]
F = CONFIG["candidate_filters"]

FIELDS = [
    "date", "chain", "dex", "pair", "pair_address",
    "base_symbol", "base_address", "quote_symbol",
    "price_usd", "liquidity_usd", "market_cap", "fdv",
    "vol_24h", "vol_6h", "vol_1h",
    "txns_24h_buys", "txns_24h_sells",
    "price_change_24h", "pair_created_at", "pool_age_days",
    "vol_tvl_24h", "buy_sell_ratio_24h", "labels",
    "passed_filter", "discovery_source", "inverse_connector",
]


def _get(path: str) -> object:
    r = requests.get(f"{API}{path}", timeout=20)
    r.raise_for_status()
    return r.json()


def _pairs_for_token(token_address: str) -> list:
    """All pools for a token on our chain. /token-pairs/v1/{chain}/{address}."""
    try:
        data = _get(f"/token-pairs/v1/{CHAIN}/{token_address}")
        return data if isinstance(data, list) else data.get("pairs", []) or []
    except Exception as e:
        print(f"    token-pairs {token_address[:10]}.. failed: {e}")
        return []


def _search(query: str) -> list:
    try:
        return _get(f"/latest/dex/search?q={requests.utils.quote(query)}").get("pairs", []) or []
    except Exception as e:
        print(f"    search '{query}' failed: {e}")
        return []


def _subgraph_top_pool_ids(subgraph_id: str, api_key: str, n: int, min_vol: float) -> list:
    """Top-N pool addresses by all-time volume from a DEX subgraph.

    Schema-agnostic: tries the Uniswap-v3-style `pools` schema (Aerodrome CL /
    Uniswap v3 forks), then falls back to the Messari `liquidityPools` schema.
    We only need the addresses — pools are hydrated via DexScreener afterwards.
    Retries on the flaky Graph gateway and surfaces "errors" (e.g. "bad indexers")
    rather than silently returning 0.
    """
    url = f"https://gateway.thegraph.com/api/{api_key}/subgraphs/id/{subgraph_id}"
    # (entity, volume_field) variants, tried in order.
    variants = [("pools", "volumeUSD"), ("liquidityPools", "cumulativeVolumeUSD")]
    for entity, vol_field in variants:
        query = ("query($n: Int!) { %s(first: $n, orderBy: %s, orderDirection: desc) "
                 "{ id %s } }" % (entity, vol_field, vol_field))
        for attempt in (1, 2):
            try:
                r = requests.post(url, json={"query": query, "variables": {"n": n}}, timeout=45)
                r.raise_for_status()
                body = r.json()
                if body.get("errors"):
                    msg = str(body["errors"])
                    if "has no field" in msg:
                        break  # wrong schema for this subgraph — try next variant
                    raise RuntimeError(msg[:140])  # indexer/transient error — retry
                items = (body.get("data") or {}).get(entity, []) or []
                return [it["id"].lower() for it in items if float(it.get(vol_field) or 0) >= min_vol]
            except Exception as e:
                if attempt == 2:
                    print(f"    subgraph {subgraph_id[:8]}.. ({entity}) failed: {e}")
                else:
                    time.sleep(2)
    return []


def _hydrate_pairs(addrs: list) -> list:
    """Fetch full DexScreener pair objects for pool addresses (batched, 30/call) so
    subgraph-discovered pools carry the same fields (mcap, txns, age) the filters need."""
    out = []
    for i in range(0, len(addrs), 30):
        batch = ",".join(addrs[i:i + 30])
        try:
            data = _get(f"/latest/dex/pairs/{CHAIN}/{batch}")
            out.extend((data.get("pairs") if isinstance(data, dict) else data) or [])
        except Exception as e:
            print(f"    hydrate batch failed: {e}")
        time.sleep(0.2)
    return out


def discover_token_addresses() -> set:
    """Seed a set of Base token addresses from trending / new / boosted lists."""
    tokens = set()
    d = CONFIG["discovery"]

    if d.get("use_latest_token_profiles"):
        try:
            for t in _get("/token-profiles/latest/v1") or []:
                if t.get("chainId") == CHAIN and t.get("tokenAddress"):
                    tokens.add(t["tokenAddress"].lower())
        except Exception as e:
            print(f"  token-profiles failed: {e}")

    if d.get("use_top_boosts"):
        try:
            for t in _get("/token-boosts/top/v1") or []:
                if t.get("chainId") == CHAIN and t.get("tokenAddress"):
                    tokens.add(t["tokenAddress"].lower())
        except Exception as e:
            print(f"  token-boosts failed: {e}")

    for addr in d.get("seed_token_addresses", []):
        tokens.add(addr.lower())

    print(f"  discovered {len(tokens)} seed token addresses")
    return tokens


def collect_pairs() -> dict:
    """Gather candidate pairs from all configured sources, keyed by pairAddress."""
    pairs = {}
    d = CONFIG["discovery"]

    # 1) Trending metas (each meta carries a list of pairs)
    if d.get("use_trending_metas"):
        try:
            for meta in _get("/metas/trending/v1") or []:
                slug = meta.get("slug")
                if not slug:
                    continue
                detail = _get(f"/metas/meta/v1/{slug}")
                for p in detail.get("pairs", []) or []:
                    if p.get("chainId") == CHAIN:
                        pairs.setdefault(p["pairAddress"].lower(), ("trending_meta", p))
                time.sleep(0.2)
        except Exception as e:
            print(f"  trending metas failed: {e}")

    # 2) Seed search queries
    for q in d.get("seed_search_queries", []):
        for p in _search(q):
            if p.get("chainId") == CHAIN:
                pairs.setdefault(p["pairAddress"].lower(), ("search", p))
        time.sleep(0.2)

    # 3) Pools for each discovered token
    for addr in discover_token_addresses():
        for p in _pairs_for_token(addr):
            if p.get("chainId") == CHAIN:
                pairs.setdefault(p["pairAddress"].lower(), ("token_pairs", p))
        time.sleep(0.15)

    # 4) Subgraph top pools by volume -> hydrate via DexScreener. Systematic
    #    coverage of the dominant Base DEXes, beyond DexScreener's sample.
    if d.get("use_subgraph_pools"):
        key = os.environ.get("THEGRAPH_API_KEY", "")
        if not key:
            print("  use_subgraph_pools set but THEGRAPH_API_KEY missing — skipping subgraph discovery")
        else:
            sg = CONFIG.get("subgraphs", {})
            n = d.get("subgraph_pools_per_dex", 200)
            min_vol = d.get("subgraph_min_volume_usd", 5000)
            ids = d.get("discovery_subgraph_ids") or [sg.get("aerodrome_base"), sg.get("uniswap_v3_base")]
            pool_ids = set()
            for sid in filter(None, ids):
                got = _subgraph_top_pool_ids(sid, key, n, min_vol)
                print(f"  subgraph {sid[:8]}..: {len(got)} pools (vol >= ${min_vol:,.0f})")
                pool_ids.update(got)
            fresh = [a for a in pool_ids if a not in pairs]
            before = len(pairs)
            for p in _hydrate_pairs(fresh):
                if p.get("chainId") == CHAIN and p.get("pairAddress"):
                    pairs.setdefault(p["pairAddress"].lower(), ("subgraph", p))
            print(f"  subgraph discovery: {len(pool_ids)} top pools -> +{len(pairs) - before} new pairs")

    # 5) Inverse-connector: for each token with a deep pool passing the standard
    #    filter, pull its pools and admit the opposite-base (USDC/WETH/cbBTC) variant
    #    even when thin — often more capital-efficient (high fee/TVL) but dropped by
    #    the $50k floor. Austin's heuristic; catches DEGEN/USDC-style pairs.
    if F.get("inverse_connector_enabled"):
        bases = {b.upper() for b in F.get("inverse_connector_bases", ["USDC", "WETH", "cbBTC"])}
        cmin = F.get("inverse_connector_min_liquidity_usd", 2000)
        std = F["min_liquidity_usd"]
        # Anchor = each qualifying NON-base token's deepest pool + its quote base.
        # (Base tokens like WETH/USDC/cbBTC are connectors, not assets we bootstrap.)
        anchor = {}
        for _src, p in list(pairs.values()):
            liq = _f((p.get("liquidity") or {}).get("usd"))
            if liq < std or _f(p.get("marketCap")) < F["min_market_cap_usd"]:
                continue
            bt = p.get("baseToken") or {}
            a = (bt.get("address") or "").lower()
            if not a or (bt.get("symbol") or "").upper() in bases:
                continue
            q = ((p.get("quoteToken") or {}).get("symbol") or "").upper()
            if a not in anchor or liq > anchor[a][0]:
                anchor[a] = (liq, q)
        top_tokens = [a for a, _ in sorted(anchor.items(), key=lambda x: -x[1][0])][
            : F.get("inverse_connector_max_tokens", 40)]
        before = len(pairs)
        for addr in top_tokens:
            anchor_q = anchor[addr][1]
            for p in _pairs_for_token(addr):
                pa = (p.get("pairAddress") or "").lower()
                q = ((p.get("quoteToken") or {}).get("symbol") or "").upper()
                liq = _f((p.get("liquidity") or {}).get("usd"))
                # the OPPOSITE base, and only the THIN ones the $50k floor would drop
                if (p.get("chainId") == CHAIN and pa and pa not in pairs
                        and q in bases and q != anchor_q and cmin <= liq < std):
                    pairs[pa] = ("inverse_connector", p)
            time.sleep(0.15)
        print(f"  inverse-connector: scanned {len(top_tokens)} tokens -> +{len(pairs) - before} thin connector pairs")

    print(f"  collected {len(pairs)} unique Base pairs")
    return pairs


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def build_row(source: str, p: dict, today: str) -> dict:
    liq = _f((p.get("liquidity") or {}).get("usd"))
    mcap = _f(p.get("marketCap"))
    vol = p.get("volume") or {}
    vol_24h = _f(vol.get("h24"))
    txns_24h = (p.get("txns") or {}).get("h24") or {}
    buys = int(txns_24h.get("buys") or 0)
    sells = int(txns_24h.get("sells") or 0)

    created_ms = p.get("pairCreatedAt")
    age_days = ""
    if created_ms:
        created = dt.datetime.fromtimestamp(created_ms / 1000, tz=dt.timezone.utc)
        age_days = round((dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 86400, 2)

    # Inverse-connector pairs (the thin opposite-base variant of a qualifying token)
    # use a relaxed liquidity floor — that's the whole point, they're efficient but
    # below $50k. The market-cap requirement still applies.
    is_connector = source == "inverse_connector"
    liq_floor = (F.get("inverse_connector_min_liquidity_usd", 2000)
                 if is_connector else F["min_liquidity_usd"])
    passed = liq >= liq_floor and mcap >= F["min_market_cap_usd"]
    quote_sym = (p.get("quoteToken") or {}).get("symbol", "")
    if quote_sym in F.get("exclude_quote_symbols", []):
        passed = False

    return {
        "date": today,
        "chain": p.get("chainId", CHAIN),
        "dex": p.get("dexId", ""),
        "pair": f"{(p.get('baseToken') or {}).get('symbol','?')}/{quote_sym}",
        "pair_address": p.get("pairAddress", ""),
        "base_symbol": (p.get("baseToken") or {}).get("symbol", ""),
        "base_address": (p.get("baseToken") or {}).get("address", ""),
        "quote_symbol": quote_sym,
        "price_usd": _f(p.get("priceUsd")),
        "liquidity_usd": round(liq, 2),
        "market_cap": round(mcap, 2),
        "fdv": round(_f(p.get("fdv")), 2),
        "vol_24h": round(vol_24h, 2),
        "vol_6h": round(_f(vol.get("h6")), 2),
        "vol_1h": round(_f(vol.get("h1")), 2),
        "txns_24h_buys": buys,
        "txns_24h_sells": sells,
        "price_change_24h": _f((p.get("priceChange") or {}).get("h24")),
        "pair_created_at": created_ms or "",
        "pool_age_days": age_days,
        "vol_tvl_24h": round(vol_24h / liq, 4) if liq > 0 else 0,
        "buy_sell_ratio_24h": round(buys / sells, 3) if sells > 0 else "",
        "labels": "|".join(p.get("labels") or []),
        "passed_filter": passed,
        "discovery_source": source,
        "inverse_connector": is_connector,
    }


def main():
    today = dt.date.today().isoformat()
    pairs = collect_pairs()

    rows = [build_row(src, p, today) for src, p in pairs.values()]
    passed = [r for r in rows if r["passed_filter"]]
    print(f"\n{len(rows)} candidates, {len(passed)} passed filter "
          f"(liq>=${F['min_liquidity_usd']:,}, mcap>=${F['min_market_cap_usd']:,})")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    # Rewrite the whole daily log with the current FIELDS header so a schema change
    # (e.g. a new column) auto-migrates older rows instead of misaligning an append.
    prior = []
    if OUT_CSV.exists() and OUT_CSV.stat().st_size > 0:
        with open(OUT_CSV, newline="") as f:
            prior = [{k: r.get(k, "") for k in FIELDS} for r in csv.DictReader(f)]
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(prior + rows)

    print(f"Wrote {len(prior)} prior + {len(rows)} new rows to {OUT_CSV}")
    # Print the day's filtered shortlist, ranked by 24h volume, as a quick preview
    for r in sorted(passed, key=lambda x: x["vol_24h"], reverse=True)[:10]:
        print(f"  {r['pair']:<20} liq=${r['liquidity_usd']:>12,.0f}  "
              f"vol24h=${r['vol_24h']:>12,.0f}  age={r['pool_age_days']}d  {r['dex']}")


if __name__ == "__main__":
    main()
