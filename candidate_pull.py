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
    "passed_filter", "discovery_source",
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

    passed = liq >= F["min_liquidity_usd"] and mcap >= F["min_market_cap_usd"]
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
    }


def main():
    today = dt.date.today().isoformat()
    pairs = collect_pairs()

    rows = [build_row(src, p, today) for src, p in pairs.values()]
    passed = [r for r in rows if r["passed_filter"]]
    print(f"\n{len(rows)} candidates, {len(passed)} passed filter "
          f"(liq>=${F['min_liquidity_usd']:,}, mcap>=${F['min_market_cap_usd']:,})")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not (OUT_CSV.exists() and OUT_CSV.stat().st_size > 0)
    with open(OUT_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"Appended {len(rows)} rows to {OUT_CSV}")
    # Print the day's filtered shortlist, ranked by 24h volume, as a quick preview
    for r in sorted(passed, key=lambda x: x["vol_24h"], reverse=True)[:10]:
        print(f"  {r['pair']:<20} liq=${r['liquidity_usd']:>12,.0f}  "
              f"vol24h=${r['vol_24h']:>12,.0f}  age={r['pool_age_days']}d  {r['dex']}")


if __name__ == "__main__":
    main()
