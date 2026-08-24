# Crypto Radar

A daily-driver dashboard for spotting market rotation and altcoin momentum **early** —
built around one idea: rank by how *statistically unusual* something is right now,
not by how big the move already is.

## What it actually does

Raw "top gainers" lists are useless for entry timing — by the time a coin is on one,
the move is priced. This scores every asset in the top 500 against **its own trailing
history** and against the market cross-section, then explains why each name is flagged.

| Panel | Question it answers |
|---|---|
| **Radar** | Which assets are moving unusually *relative to their own norm*, and is the move young or already extended? |
| **Narratives** | Which sectors are rotating? Split into established (>$100M) and emerging ($5–100M, actively trading). |
| **Onchain** | What launched in the last 48h with real liquidity and buy pressure? |
| **Capital Flow** | Where are TVL, fees, and DEX volume actually accelerating — real capital, not just price. |
| **Positioning** | Which trades are already crowded (funding extremes), so you can tell early from late. |

### Charts

Clicking any row in the Radar expands a TradingView Advanced Chart inline — candlesticks
with the full drawing toolbar, defaulted to **log scale** (percentage moves are what
matter across assets spanning three orders of magnitude, and a linear axis flattens the
early base that is exactly the part worth seeing).

Symbols are guessed as `BINANCE:{SYM}USDT`, with one-click switching to Bybit, OKX,
Coinbase, MEXC, Gate, or TradingView's own index feed, plus in-widget symbol search when
the guess is wrong. Wrapped and staked tokens (WBTC, stETH, JitoSOL…) chart their
underlying asset, since they have no exchange pair of their own.

Drawings persist while the panel is open but are **not** saved across reloads — the free
widget cannot do that; TradingView only persists layouts for signed-in users on its own
site.

### The score

```
score = 0.35·volume_anomaly + 0.30·ignition + 0.20·turnover
      + 0.15·flow_confirmation − 0.10·crowding
```

- **volume_anomaly** — z-score of current volume vs the asset's own trailing volume.
  "6σ above its own norm" is a real event; "high volume" is not.
- **ignition** — rewards a *young* thrust and discounts an extended one. Up 15% today
  after a flat month scores high; up 15% today after already tripling scores low.
  This is the whole "early, not late" mechanism.
- **turnover** — volume/mcap percentile. Rotation shows up here first.
- **flow_confirmation** — protocol TVL growth (joined via DefiLlama's `gecko_id`),
  separating real capital from a pure price pump.
- **crowding** — *penalty* when perp funding shows the trade is already consensus.

## Honest limitations

- **It cannot see a narrative before it exists.** Nothing can. The goal is earliest
  credible detection — hours, not foresight.
- **Cold start.** Volume z-scores need ≥8 snapshots (~16h at the default cron). Before
  that the dashboard falls back to cross-sectional turnover ranking and says so in a
  banner. Signals get meaningfully better after ~3 days of history.
- **Token unlocks are missing** — DefiLlama paywalls that endpoint (HTTP 402). It is
  the one genuinely useful "upcoming shift" source not covered here.
- **Binance is unusable** from US-hosted runners (HTTP 451), so CEX volume comes from
  OKX and Hyperliquid instead.
- Nothing here is financial advice. These are anomaly statistics, not predictions.

## Running it

```bash
python3 refresh_data.py
```

Writes `data.json` (what the page renders) and `history.json` (the rolling series the
z-scores need, capped at 120 snapshots). Open `index.html` over any static server.

All sources are **free and keyless**: CoinGecko, DefiLlama, GeckoTerminal, Hyperliquid,
alternative.me. No API keys, no secrets to configure.

## Deploying

GitHub Actions runs the refresh every 2 hours and commits both JSON files
(`.github/workflows/refresh.yml`). Point GitHub Pages at the default branch, root
folder, and the dashboard serves itself. Cadence is one line in the cron if you want
it tighter — hourly roughly doubles the resolution of the z-scores.
