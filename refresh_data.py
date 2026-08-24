#!/usr/bin/env python3
"""
refresh_data.py — Crypto Radar data + signal engine.

Fetches from keyless public APIs, appends a snapshot to history.json, then scores
every asset against ITS OWN trailing history (not against a fixed threshold) so the
dashboard surfaces what is statistically unusual right now rather than what is
already large.

Outputs:
  data.json     — everything the dashboard renders (committed each run)
  history.json  — rolling compact time series used for the z-scores (committed each run)

All sources are free and require no API key. Any single source failing degrades that
panel only; the run still completes.
"""

import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "data.json")
HISTORY_PATH = os.path.join(HERE, "history.json")

# How many snapshots of history to retain per asset. At the default 2-hourly cron
# this is ~10 days of context, which is the right window for "is this move new?".
MAX_SNAPSHOTS = 120
# Minimum snapshots before a per-asset z-score is trusted. Below this we fall back
# to cross-sectional ranking and label the signal as "cold".
MIN_SNAPSHOTS_FOR_Z = 8
# CoinGecko pages of 250. 2 pages = top 500 coins.
CG_PAGES = 2
# Trailing price points inlined per flagged mover for its sparkline.
SPARK_POINTS = 36

UA = "crypto-radar/1.0 (+https://github.com/) python-urllib"


def _ssl_context():
    """
    Framework Python on macOS ships without a populated CA store, so urllib fails
    cert verification locally while working fine on CI. Prefer certifi's bundle
    when it is importable; otherwise fall back to the system default.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


# --------------------------------------------------------------------------- http


def fetch(url, tries=3, timeout=45, method="GET", body=None, headers=None):
    """GET/POST JSON with backoff. Returns parsed JSON or None (never raises)."""
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # 429 = rate limited; back off harder and retry.
            wait = 6 * (attempt + 1) if e.code == 429 else 2 * (attempt + 1)
            log(f"  ! HTTP {e.code} {url.split('?')[0]} (attempt {attempt+1})")
            if attempt == tries - 1:
                return None
            time.sleep(wait)
        except Exception as e:
            log(f"  ! {type(e).__name__} {url.split('?')[0]} (attempt {attempt+1})")
            if attempt == tries - 1:
                return None
            time.sleep(2 * (attempt + 1))
    return None


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------------------- stats


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def stdev(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def zscore(current, series):
    """How many standard deviations above its own norm is `current`?"""
    hist = [x for x in series if x is not None and x > 0]
    if current is None or len(hist) < MIN_SNAPSHOTS_FOR_Z:
        return None
    m = mean(hist)
    s = stdev(hist)
    if not m or not s or s == 0:
        return None
    return (current - m) / s


def pct_rank(value, population):
    """Cross-sectional percentile (0-100) of value within population."""
    pop = sorted(x for x in population if x is not None)
    if not pop or value is None:
        return None
    below = sum(1 for x in pop if x < value)
    return 100.0 * below / len(pop)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def safe(x, default=None):
    """Coerce to float, tolerating None/str/NaN."""
    if x is None:
        return default
    try:
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------- fetchers


def get_markets():
    """Top N coins with multi-timeframe returns. This is the scanner universe."""
    out = []
    for page in range(1, CG_PAGES + 1):
        url = (
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&order=market_cap_desc&per_page=250"
            f"&page={page}&price_change_percentage=1h,24h,7d,30d"
        )
        chunk = fetch(url)
        if not chunk:
            log(f"  markets page {page}: FAILED")
            continue
        out.extend(chunk)
        log(f"  markets page {page}: {len(chunk)} coins")
        time.sleep(3)  # stay under the free-tier rate limit
    return out


def get_categories():
    return fetch("https://api.coingecko.com/api/v3/coins/categories") or []


def get_trending():
    d = fetch("https://api.coingecko.com/api/v3/search/trending") or {}
    return d.get("coins", [])


def get_global():
    d = fetch("https://api.coingecko.com/api/v3/global") or {}
    return d.get("data", {})


def get_fng():
    d = fetch("https://api.alternative.me/fng/?limit=30") or {}
    return d.get("data", [])


def get_protocols():
    return fetch("https://api.llama.fi/protocols") or []


def get_dexs():
    d = fetch(
        "https://api.llama.fi/overview/dexs"
        "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
    ) or {}
    return d.get("protocols", [])


def get_fees():
    d = fetch(
        "https://api.llama.fi/overview/fees"
        "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
    ) or {}
    return d.get("protocols", [])


def get_stablecoins():
    d = fetch("https://stablecoins.llama.fi/stablecoins?includePrices=false") or {}
    return d.get("peggedAssets", [])


def get_hyperliquid():
    """Perp funding + open interest — the crowding/positioning read."""
    d = fetch(
        "https://api.hyperliquid.xyz/info",
        method="POST",
        body={"type": "metaAndAssetCtxs"},
    )
    if not d or len(d) < 2:
        return []
    universe, ctxs = d[0].get("universe", []), d[1]
    rows = []
    for coin, ctx in zip(universe, ctxs):
        if not ctx:
            continue
        rows.append(
            {
                "symbol": coin.get("name"),
                "funding": safe(ctx.get("funding"), 0.0),
                "oi": safe(ctx.get("openInterest"), 0.0),
                "mark": safe(ctx.get("markPx"), 0.0),
                "prev": safe(ctx.get("prevDayPx"), 0.0),
                "vol24h": safe(ctx.get("dayNtlVlm"), 0.0),
            }
        )
    return rows


def get_new_pools(networks=("solana", "eth", "base")):
    """Freshly created onchain pools — the earliest possible detection surface."""
    pools = []
    for net in networks:
        d = fetch(
            f"https://api.geckoterminal.com/api/v2/networks/{net}/new_pools?page=1"
        )
        if not d:
            continue
        for p in d.get("data", []):
            a = p.get("attributes", {})
            pools.append(parse_pool(a, net))
        time.sleep(2)
    return pools


def get_trending_pools(networks=("solana", "eth", "base")):
    pools = []
    for net in networks:
        d = fetch(
            f"https://api.geckoterminal.com/api/v2/networks/{net}/trending_pools?page=1"
        )
        if not d:
            continue
        for p in d.get("data", []):
            a = p.get("attributes", {})
            pools.append(parse_pool(a, net))
        time.sleep(2)
    return pools


def parse_pool(a, net):
    tx = a.get("transactions", {}) or {}
    h1 = tx.get("h1", {}) or {}
    buys, sells = safe(h1.get("buys"), 0), safe(h1.get("sells"), 0)
    vol = a.get("volume_usd", {}) or {}
    chg = a.get("price_change_percentage", {}) or {}
    return {
        "net": net,
        "name": a.get("name"),
        "address": a.get("address"),
        "created": a.get("pool_created_at"),
        "liq": safe(a.get("reserve_in_usd"), 0.0),
        "fdv": safe(a.get("fdv_usd"), 0.0),
        "mcap": safe(a.get("market_cap_usd")),
        "vol24h": safe(vol.get("h24"), 0.0),
        "vol1h": safe(vol.get("h1"), 0.0),
        "chg1h": safe(chg.get("h1")),
        "chg24h": safe(chg.get("h24")),
        "buys1h": buys,
        "sells1h": sells,
        "price": safe(a.get("base_token_price_usd")),
    }


# ------------------------------------------------------------------------ history


def _round_big(x):
    """Volumes and market caps: whole dollars are plenty."""
    return None if x is None else int(round(x))


def _round_sig(x, sig=6):
    """Prices: 6 significant figures, so sub-cent tokens keep their precision."""
    if x is None or x == 0:
        return x
    return round(x, -int(math.floor(math.log10(abs(x)))) + (sig - 1))


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"snapshots": [], "coins": {}, "cats": {}}
    try:
        with open(HISTORY_PATH) as f:
            h = json.load(f)
        h.setdefault("snapshots", [])
        h.setdefault("coins", {})
        h.setdefault("cats", {})
        return h
    except Exception as e:
        log(f"  ! history unreadable ({e}) — starting fresh")
        return {"snapshots": [], "coins": {}, "cats": {}}


def append_history(hist, stamp, markets, categories):
    """Append this run's snapshot, padding absent assets with null and trimming."""
    hist["snapshots"].append(stamp)
    n = len(hist["snapshots"])

    seen = set()
    for c in markets:
        cid = c.get("id")
        if not cid:
            continue
        seen.add(cid)
        rec = hist["coins"].setdefault(cid, {"v": [], "m": [], "p": []})
        # Pad so every series lines up with the snapshots index.
        for key in ("v", "m", "p"):
            while len(rec[key]) < n - 1:
                rec[key].append(None)
        # Round on write — full float precision would roughly double the file
        # and every digit past this is noise for a z-score.
        rec["v"].append(_round_big(safe(c.get("total_volume"))))
        rec["m"].append(_round_big(safe(c.get("market_cap"))))
        rec["p"].append(_round_sig(safe(c.get("current_price"))))

    for cid, rec in hist["coins"].items():
        if cid not in seen:
            for key in ("v", "m", "p"):
                while len(rec[key]) < n:
                    rec[key].append(None)

    seen_cat = set()
    for c in categories:
        cid = c.get("id")
        if not cid:
            continue
        seen_cat.add(cid)
        rec = hist["cats"].setdefault(cid, {"m": []})
        while len(rec["m"]) < n - 1:
            rec["m"].append(None)
        rec["m"].append(_round_big(safe(c.get("market_cap"))))
    for cid, rec in hist["cats"].items():
        if cid not in seen_cat:
            while len(rec["m"]) < n:
                rec["m"].append(None)

    # Trim to the rolling window.
    if n > MAX_SNAPSHOTS:
        cut = n - MAX_SNAPSHOTS
        hist["snapshots"] = hist["snapshots"][cut:]
        for rec in hist["coins"].values():
            for key in ("v", "m", "p"):
                rec[key] = rec[key][cut:]
        for rec in hist["cats"].values():
            rec["m"] = rec["m"][cut:]

    # Drop assets that have fallen entirely out of the window (keeps the file small).
    for store, keys in ((hist["coins"], ("v", "m", "p")), (hist["cats"], ("m",))):
        dead = [
            k
            for k, rec in store.items()
            if all(x is None for x in rec[keys[0]])
        ]
        for k in dead:
            del store[k]

    return hist


# ------------------------------------------------------------------------ scoring


def ignition(r1, r7, r30):
    """
    Reward a *young* thrust; penalise one that is already extended.

    This is the core "early" filter. A coin up 15% today having been flat for a
    month scores high. A coin up 15% today after already tripling scores low —
    the move is not new and you would be buying someone else's exit.
    """
    if r1 is None:
        return 0.0
    thrust = clamp(r1, -20.0, 60.0)
    t = max(0.0, thrust) / 25.0  # 25% in 24h saturates the thrust term
    ext = 0.0
    if r30 is not None and r30 > 0:
        ext = min(1.0, r30 / 200.0)  # +200% over 30d = fully extended
    if r7 is not None and r7 > 0:
        ext = max(ext, min(1.0, r7 / 80.0))  # or +80% over 7d
    return 100.0 * min(1.0, t) * (1.0 - 0.7 * ext)


def score_coins(markets, hist, llama_by_gecko, hl_by_symbol):
    """Score the universe. Returns rows sorted by composite score, descending."""
    n_snap = len(hist["snapshots"])
    have_history = n_snap >= MIN_SNAPSHOTS_FOR_Z

    turnovers = []
    for c in markets:
        mc, vol = safe(c.get("market_cap")), safe(c.get("total_volume"))
        turnovers.append(vol / mc if mc and vol and mc > 0 else None)

    rows = []
    for c, turn in zip(markets, turnovers):
        cid = c.get("id")
        mc = safe(c.get("market_cap"))
        vol = safe(c.get("total_volume"))
        if not mc or mc <= 0:
            continue

        r1 = safe(c.get("price_change_percentage_24h_in_currency"))
        r7 = safe(c.get("price_change_percentage_7d_in_currency"))
        r30 = safe(c.get("price_change_percentage_30d_in_currency"))
        rh = safe(c.get("price_change_percentage_1h_in_currency"))

        reasons = []

        # --- volume anomaly: vs the coin's own trailing volume ------------------
        vz = None
        if have_history:
            rec = hist["coins"].get(cid)
            if rec:
                vz = zscore(vol, rec["v"][:-1])  # exclude the point we just appended
        if vz is not None:
            vol_score = 100.0 * clamp(vz / 4.0, 0.0, 1.0)
            if vz >= 2:
                reasons.append(f"volume {vz:.1f}σ above its own norm")
            mode = "history"
        else:
            # Cold start: rank turnover across the universe instead.
            p = pct_rank(turn, turnovers)
            vol_score = p if p is not None else 0.0
            if p and p >= 95:
                reasons.append(f"turnover in top {100-p:.0f}% of market")
            mode = "cold"

        # --- turnover: volume relative to size --------------------------------
        turn_pct = pct_rank(turn, turnovers) or 0.0
        if turn_pct >= 90 and "turnover" not in " ".join(reasons):
            reasons.append(f"unusually high volume/mcap ({turn_pct:.0f}th pct)")

        # --- ignition: is the move young? -------------------------------------
        ign = ignition(r1, r7, r30)
        if ign >= 45:
            reasons.append(f"fresh thrust +{r1:.1f}% on a flat base")
        elif r1 and r1 > 8 and r30 and r30 > 150:
            reasons.append(f"moving, but already +{r30:.0f}% in 30d (late)")

        # --- flow confirmation: is real capital following? ---------------------
        flow_score, tvl_7d = 0.0, None
        proto = llama_by_gecko.get(cid)
        if proto:
            tvl_7d = safe(proto.get("change_7d"))
            if tvl_7d is not None:
                flow_score = 100.0 * clamp(tvl_7d / 40.0, 0.0, 1.0)
                if tvl_7d >= 15:
                    reasons.append(f"protocol TVL +{tvl_7d:.0f}% in 7d")

        # --- crowding penalty: is the trade already consensus? -----------------
        funding, crowd_pen = None, 0.0
        hl = hl_by_symbol.get((c.get("symbol") or "").upper())
        if hl:
            funding = hl.get("funding")
            if funding is not None:
                # Measured against Hyperliquid's neutral floor, not against zero:
                # ~4x the floor is a genuinely hot, crowded long.
                hot_at = HL_BASELINE_FUNDING * 4
                if funding > hot_at:
                    crowd_pen = 100.0 * clamp((funding - hot_at) / 0.0003, 0.0, 1.0)
                    if crowd_pen > 40:
                        reasons.append("perp funding hot — longs already crowded")
                elif funding < 0:
                    reasons.append("negative funding — shorts paying, squeeze fuel")

        composite = (
            0.35 * vol_score
            + 0.30 * ign
            + 0.20 * turn_pct
            + 0.15 * flow_score
            - 0.10 * crowd_pen
        )

        rows.append(
            {
                "id": cid,
                "sym": (c.get("symbol") or "").upper(),
                "name": c.get("name"),
                "img": c.get("image"),
                "price": safe(c.get("current_price")),
                "mcap": mc,
                "rank": c.get("market_cap_rank"),
                "vol": vol,
                "turnover": turn,
                "r1h": rh,
                "r24h": r1,
                "r7d": r7,
                "r30d": r30,
                "vol_z": round(vz, 2) if vz is not None else None,
                "score": round(clamp(composite, 0.0, 100.0), 1),
                "ignition": round(ign, 1),
                "flow": round(flow_score, 1),
                "tvl_7d": tvl_7d,
                "funding": funding,
                "mode": mode,
                "reasons": reasons[:3],
                "ath_off": safe(c.get("ath_change_percentage")),
            }
        )

    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


# A narrative needs real size to be tradeable. Below the established floor a
# category's 24h change is dominated by listing artifacts (a coin being added to
# the category reads as a +800% "move"), so we split into two tiers instead.
CAT_ESTABLISHED_FLOOR = 100_000_000
CAT_EMERGING_FLOOR = 5_000_000
CAT_MAX_PLAUSIBLE_24H = 300.0
# Structural buckets, not narratives: their market cap moves when tokens are
# re-classified or double-counted, which is noise dressed up as a sector rotation.
CAT_EXCLUDE_TERMS = ("bridged", "wrapped")


def score_categories(categories, hist):
    """Narrative rotation: which sectors are gaining, and are they accelerating?"""
    n = len(hist["snapshots"])
    rows = []
    for c in categories:
        mc = safe(c.get("market_cap"))
        if not mc or mc < CAT_EMERGING_FLOOR:
            continue
        name_l = (c.get("name") or "").lower()
        if any(t in name_l for t in CAT_EXCLUDE_TERMS):
            continue
        cid = c.get("id")
        chg24 = safe(c.get("market_cap_change_24h"))
        # Reject implausible jumps — these are constituents being added, not flows.
        if chg24 is not None and abs(chg24) > CAT_MAX_PLAUSIBLE_24H:
            continue

        # Acceleration: current mcap vs ~24h and ~72h ago in our own history.
        accel, chg_3d = None, None
        rec = hist["cats"].get(cid)
        if rec and n >= 2:
            series = rec["m"]
            def ago(k):
                idx = len(series) - 1 - k
                return series[idx] if 0 <= idx < len(series) else None
            # At a 2-hourly cron, 12 snapshots ≈ 24h, 36 ≈ 72h.
            d1, d3 = ago(12), ago(36)
            if d1 and d1 > 0 and d3 and d3 > 0:
                r1 = 100.0 * (mc - d1) / d1
                r3 = 100.0 * (mc - d3) / d3
                chg_3d = r3
                accel = r1 - (r3 / 3.0)  # today's pace vs the 3-day average pace

        vol = safe(c.get("volume_24h"))
        rows.append(
            {
                "id": cid,
                "name": c.get("name"),
                "mcap": mc,
                "vol": vol,
                "turnover": (vol / mc) if (vol and mc) else None,
                "chg24": chg24,
                "chg3d": chg_3d,
                "accel": accel,
                "top": (c.get("top_3_coins_id") or [])[:3],
                "top_img": (c.get("top_3_coins") or [])[:3],
            }
        )

    def by_chg(rs):
        return sorted(
            rs, key=lambda r: (r["chg24"] if r["chg24"] is not None else -999), reverse=True
        )

    established = by_chg([r for r in rows if r["mcap"] >= CAT_ESTABLISHED_FLOOR])
    # Emerging: small but genuinely trading, not a dormant tag with a stale mcap.
    emerging = by_chg(
        [
            r
            for r in rows
            if r["mcap"] < CAT_ESTABLISHED_FLOOR
            and (r["turnover"] or 0) >= 0.02
            and (r["vol"] or 0) >= 500_000
        ]
    )
    return established, emerging


def score_new_pools(pools):
    """
    Filter fresh launches down to ones with a real shape:
    meaningful liquidity, volume actually turning over that liquidity, buy pressure.
    """
    now = datetime.now(timezone.utc)
    out = []
    for p in pools:
        liq = p.get("liq") or 0
        if liq < 25000:  # below this it is noise or a rug in progress
            continue
        vol = p.get("vol24h") or 0
        if vol < 20000:
            continue
        age_h = None
        if p.get("created"):
            try:
                created = datetime.fromisoformat(p["created"].replace("Z", "+00:00"))
                age_h = (now - created).total_seconds() / 3600.0
            except Exception:
                pass
        buys, sells = p.get("buys1h") or 0, p.get("sells1h") or 0
        ratio = buys / sells if sells else (buys if buys else 0)
        churn = vol / liq if liq else 0
        # Score: turnover intensity, buy skew, and youth.
        s = 0.0
        s += 40 * clamp(churn / 5.0, 0, 1)
        s += 30 * clamp((ratio - 1.0) / 2.0, 0, 1)
        if age_h is not None:
            s += 30 * clamp((48 - age_h) / 48.0, 0, 1)
        p = dict(p)
        p["age_h"] = round(age_h, 1) if age_h is not None else None
        p["churn"] = round(churn, 2)
        p["buy_ratio"] = round(ratio, 2)
        p["score"] = round(s, 1)
        out.append(p)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def score_flows(dexs, fees, protocols):
    """Where real economic activity is accelerating — volume, fees, TVL."""
    def movers(rows, value_key, floor):
        out = []
        for r in rows:
            v = safe(r.get(value_key))
            c1, c7 = safe(r.get("change_1d")), safe(r.get("change_7d"))
            if v is None or v < floor or c7 is None:
                continue
            out.append(
                {
                    "name": r.get("name"),
                    "logo": r.get("logo"),
                    "value": v,
                    "chg1d": c1,
                    "chg7d": c7,
                    "category": r.get("category"),
                    "chains": (r.get("chains") or [])[:3],
                }
            )
        out.sort(key=lambda x: x["chg7d"], reverse=True)
        return out[:15]

    tvl = []
    for p in protocols:
        v, c7, c1 = safe(p.get("tvl")), safe(p.get("change_7d")), safe(p.get("change_1d"))
        if v is None or v < 5_000_000 or c7 is None:
            continue
        if (p.get("category") or "") == "CEX":
            continue  # exchange balances are not a narrative signal
        tvl.append(
            {
                "name": p.get("name"),
                "logo": p.get("logo"),
                "value": v,
                "chg1d": c1,
                "chg7d": c7,
                "category": p.get("category"),
                "chains": (p.get("chains") or [])[:3],
            }
        )
    tvl.sort(key=lambda x: x["chg7d"], reverse=True)

    return {
        "tvl": tvl[:15],
        "dex_volume": movers(dexs, "total24h", 1_000_000),
        "fees": movers(fees, "total24h", 50_000),
    }


# Hyperliquid's funding floor is 0.0000125/hr (~11% APR) — that is the NEUTRAL
# resting rate, not bullish positioning. Anything at the floor carries no signal,
# so crowding is measured as the premium over this baseline, never versus zero.
HL_BASELINE_FUNDING = 0.0000125


def score_positioning(hl):
    """Funding extremes = crowded trades. Both tails are informative."""
    rows = []
    for r in hl:
        if not r.get("vol24h") or r["vol24h"] < 1_000_000:
            continue
        mark, prev = r.get("mark") or 0, r.get("prev") or 0
        chg = 100.0 * (mark - prev) / prev if prev else None
        f = r["funding"] or 0.0
        rows.append(
            {
                "sym": r["symbol"],
                "funding": f,
                "funding_apr": f * 24 * 365 * 100,
                # Premium over the neutral floor — this is the actual crowding read.
                "excess_apr": (f - HL_BASELINE_FUNDING) * 24 * 365 * 100,
                "oi_usd": (r.get("oi") or 0) * mark,
                "vol24h": r["vol24h"],
                "chg24": chg,
            }
        )
    # Longs pay meaningfully over the floor.
    hot = sorted(
        [r for r in rows if r["funding"] > HL_BASELINE_FUNDING * 1.5],
        key=lambda x: x["funding"],
        reverse=True,
    )[:12]
    # Shorts genuinely pay longs — funding below zero, not merely below the floor.
    cold = sorted(
        [r for r in rows if r["funding"] < 0], key=lambda x: x["funding"]
    )[:12]
    return {
        "crowded_long": hot,
        "crowded_short": cold,
        "baseline_apr": HL_BASELINE_FUNDING * 24 * 365 * 100,
    }


def build_regime(g, fng, stables, markets):
    """Top-of-page context: is this an environment where alt risk gets paid?"""
    total_mc = safe((g.get("total_market_cap") or {}).get("usd"))
    total_vol = safe((g.get("total_volume") or {}).get("usd"))
    btc_dom = safe((g.get("market_cap_percentage") or {}).get("btc"))
    eth_dom = safe((g.get("market_cap_percentage") or {}).get("eth"))
    mc_chg = safe(g.get("market_cap_change_percentage_24h_usd"))

    stable_supply = 0.0
    for s in stables:
        c = (s.get("circulating") or {}).get("peggedUSD")
        v = safe(c)
        if v:
            stable_supply += v

    fng_now = safe(fng[0].get("value")) if fng else None
    fng_prev = safe(fng[7].get("value")) if len(fng) > 7 else None

    # Breadth: share of the top 100 outperforming BTC over 7d. A genuine alt
    # rotation shows up here before it shows up in dominance.
    btc7 = None
    for c in markets:
        if c.get("id") == "bitcoin":
            btc7 = safe(c.get("price_change_percentage_7d_in_currency"))
            break
    breadth = None
    if btc7 is not None:
        top100 = [c for c in markets if (c.get("market_cap_rank") or 999) <= 100]
        vals = [
            safe(c.get("price_change_percentage_7d_in_currency")) for c in top100
        ]
        vals = [v for v in vals if v is not None]
        if vals:
            breadth = 100.0 * sum(1 for v in vals if v > btc7) / len(vals)

    return {
        "total_mcap": total_mc,
        "total_volume": total_vol,
        "mcap_chg24": mc_chg,
        "btc_dom": btc_dom,
        "eth_dom": eth_dom,
        "stable_supply": stable_supply,
        "fng": fng_now,
        "fng_prev_week": fng_prev,
        "fng_label": fng[0].get("value_classification") if fng else None,
        "btc_7d": btc7,
        "breadth_7d": breadth,
    }


# --------------------------------------------------------------------------- main


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"Crypto Radar refresh @ {stamp}")

    log("Fetching market universe...")
    markets = get_markets()
    if not markets:
        log("FATAL: no market data — aborting without touching data.json")
        return 1

    log("Fetching narrative + context sources...")
    categories = get_categories()
    time.sleep(2)
    trending = get_trending()
    time.sleep(2)
    g = get_global()
    fng = get_fng()
    log(f"  categories={len(categories)} trending={len(trending)} fng={len(fng)}")

    log("Fetching capital-flow sources...")
    protocols = get_protocols()
    dexs = get_dexs()
    fees = get_fees()
    stables = get_stablecoins()
    log(f"  protocols={len(protocols)} dexs={len(dexs)} fees={len(fees)} stables={len(stables)}")

    log("Fetching positioning + onchain...")
    hl = get_hyperliquid()
    new_pools = get_new_pools()
    trend_pools = get_trending_pools()
    log(f"  perps={len(hl)} new_pools={len(new_pools)} trending_pools={len(trend_pools)}")

    # Join tables.
    llama_by_gecko = {}
    for p in protocols:
        gid = p.get("gecko_id")
        if not gid:
            continue
        # Keep the largest protocol per token when several share a gecko_id.
        prev = llama_by_gecko.get(gid)
        if not prev or (safe(p.get("tvl")) or 0) > (safe(prev.get("tvl")) or 0):
            llama_by_gecko[gid] = p
    hl_by_symbol = {r["symbol"].upper(): r for r in hl if r.get("symbol")}

    log("Updating history...")
    hist = load_history()
    prior = len(hist["snapshots"])
    hist = append_history(hist, stamp, markets, categories)
    log(f"  snapshots: {prior} -> {len(hist['snapshots'])} (max {MAX_SNAPSHOTS})")

    log("Scoring...")
    coins = score_coins(markets, hist, llama_by_gecko, hl_by_symbol)
    cats, emerging_cats = score_categories(categories, hist)
    flows = score_flows(dexs, fees, protocols)
    positioning = score_positioning(hl)
    fresh = score_new_pools(new_pools)
    hot_pools = score_new_pools(trend_pools)
    regime = build_regime(g, fng, stables, markets)

    # Inline sparkline points for just the rows we actually render. history.json
    # grows to ~2.7 MB at full depth and the browser has no reason to download it.
    movers = coins[:60]
    for m in movers:
        rec = hist["coins"].get(m["id"])
        pts = [p for p in (rec["p"][-SPARK_POINTS:] if rec else []) if p is not None]
        m["spark"] = pts if len(pts) >= 4 else None

    n_snap = len(hist["snapshots"])
    data = {
        "generated": stamp,
        "snapshots": n_snap,
        "signal_mode": "history" if n_snap >= MIN_SNAPSHOTS_FOR_Z else "cold",
        "snapshots_until_warm": max(0, MIN_SNAPSHOTS_FOR_Z - n_snap),
        "regime": regime,
        "categories": cats,
        "emerging_categories": emerging_cats[:20],
        "trending": [
            {
                "id": t["item"].get("id"),
                "sym": (t["item"].get("symbol") or "").upper(),
                "name": t["item"].get("name"),
                "thumb": t["item"].get("thumb"),
                "rank": t["item"].get("market_cap_rank"),
            }
            for t in trending
        ],
        "movers": movers,
        "universe_size": len(coins),
        "flows": flows,
        "positioning": positioning,
        "new_pools": fresh[:25],
        "trending_pools": hot_pools[:25],
        "sources_ok": {
            "markets": bool(markets),
            "categories": bool(categories),
            "protocols": bool(protocols),
            "dexs": bool(dexs),
            "fees": bool(fees),
            "hyperliquid": bool(hl),
            "pools": bool(new_pools or trend_pools),
            "stables": bool(stables),
            "fng": bool(fng),
        },
    }

    with open(DATA_PATH, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    with open(HISTORY_PATH, "w") as f:
        json.dump(hist, f, separators=(",", ":"))

    log(
        f"Wrote data.json ({os.path.getsize(DATA_PATH)/1024:.0f} KB) "
        f"history.json ({os.path.getsize(HISTORY_PATH)/1024:.0f} KB)"
    )
    log(f"Signal mode: {data['signal_mode']} ({n_snap} snapshots)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
