# Bybit 0DTE BTC Long Gamma Algo

Automated execution engine for a **synthetic straddle** strategy on Bybit's BTC 0DTE (zero days to expiration) options market.

The algo constructs a delta-neutral position by pairing a **long BTCUSDT perpetual** with a **long in-the-money put** expiring the same day. This creates a pure gamma exposure — the position profits from large intraday BTC moves in either direction and bleeds theta if price stays flat.

## Strategy

### Payoff Structure

```
P&L
 ^
 |  \                        /
 |    \                    /
 |      \                /
 |        \            /
 |          \        /
 |            \    /
 |              \/  ← max loss (put premium)
 |
 +----------+--+--+----------→ BTC Price
           strike spot
```

**Entry:** Long 0.01 BTC perp (10x leverage) + Long 0.01 BTC ITM put

**Exit:** Either take-profit at +30% or hard close at session end

### Why It Works

A deep ITM put has a delta near -1 and positive gamma. Combined with a long perp (delta = +1), the net portfolio is approximately delta-neutral at entry. As BTC moves, gamma causes the put delta to shift — the position naturally becomes long when BTC rises and short when BTC falls. This convexity is the edge: the put loses less on adverse moves than it gains on favorable ones.

The cost of this exposure is **theta** — the time value of the put decays to zero by 08:00 UTC settlement. The strategy is profitable when intraday realized volatility exceeds the implied volatility priced into the put.

## Architecture

```
main.py
 └── Algo (orchestrator)
      ├── BybitExchange       ← REST + WebSocket API wrapper
      │    ├── Linear perp orders (market)
      │    └── Option orders (aggressive limit / chase)
      ├── MarketData          ← Real-time price feeds
      ├── OptionChain         ← 0DTE chain management
      ├── Portfolio           ← Position tracking + P&L + state persistence
      ├── RiskManager         ← Pre-trade checks + circuit breaker
      ├── SessionManager      ← Session lifecycle (entry → monitor → close)
      ├── ExitManager         ← Continuous TP monitoring (5s polling)
      ├── Scheduler           ← APScheduler cron jobs for 3 daily sessions
      └── Notifier            ← Telegram alerts
```

### Module Breakdown

| Module | Purpose |
|--------|---------|
| `config.py` | All tunable parameters — capital, sessions, execution, risk |
| `core/exchange.py` | Bybit V5 API: perp orders, option orders (with chase logic), WebSockets |
| `core/portfolio.py` | Straddle tracking, IVC computation, capital allocation, JSON persistence |
| `core/scheduler.py` | APScheduler wrapper for session entry/close/daily reset cron jobs |
| `core/notifier.py` | Async Telegram notifications |
| `data/option_chain.py` | Fetches and parses 0DTE option tickers, filters ITM puts |
| `data/market_data.py` | Unified price feed (perp WS with REST fallback) |
| `strategy/session_manager.py` | Full session lifecycle: pre-checks → sizing → entry → monitoring |
| `strategy/straddle_builder.py` | Atomic straddle entry/exit: open perp then buy put, close perp then sell put |
| `strategy/exit_manager.py` | Async TP monitor loop + time-based hard close |
| `strategy/option_selector.py` | Selects nearest ITM put passing spread/liquidity filters |
| `strategy/position_sizer.py` | Computes straddle count from capital, margin, and reserve rules |
| `risk/risk_manager.py` | Entry guards: max straddles, daily loss limit, API circuit breaker |
| `utils/time_utils.py` | UTC/SGT helpers, 0DTE expiry date calculation |

## Sessions

Three sessions per trading day (Mon–Fri), aligned with Bybit's 08:00 UTC daily settlement:

| Session | Entry (UTC) | Close (UTC) | Allocation | Notes |
|---------|-------------|-------------|------------|-------|
| 1 | 08:30 | 18:00 | Half | Post-settlement, captures Asian + European vol |
| 2 | 12:00 | 14:00 | Half | Short session targeting US pre-market |
| 3 | 14:00 | 07:30+1d | Full (SOD IVC) | Overnight session, closes 30 min before settlement |

Session 2 is automatically closed before Session 3 opens (both scheduled at 14:00 UTC).

## Capital Management

```
IVC = max(IVC_FLOOR, IVC_PCT × total_capital)

Reserve = max(MIN_RESERVE_CONTRACTS × straddle_cost, RESERVE_IVC_PCT × IVC)

Available = (Capital − Reserve) [÷ 2 for Sessions 1 & 2]

Straddles = floor(Available / straddle_cost)
```

**Straddle cost** = `(qty × perp_price / leverage) + (qty × put_premium)`

At 10x leverage with BTC at $70k and put premium at $1,000:
- Perp margin: 0.01 × $70,000 / 10 = **$70**
- Put cost: 0.01 × $1,000 = **$10**
- Total per straddle: **$80**

## Execution

### Perp Leg
Market orders on BTCUSDT linear perpetual. Fills instantly. Leverage set to 10x at startup.

### Option Leg
Bybit does not support market orders for options. The algo uses an **aggressive limit chase**:

1. Post IOC limit at `ask + 0.2%` (rounded to $5 tick)
2. If not filled, cancel and re-post at a higher price
3. Repeat up to 10 attempts, walking up to `ask + 5%` max
4. If all attempts fail, emergency-close the perp leg

The same chase logic runs in reverse when selling puts (walks the bid down).

### Entry Order
Perp first, then put. If the put fails to fill, the perp is immediately unwound.

### Exit Order
Perp first (market, instant), then put (chase limit). Minimizes single-leg exposure time.

## Risk Management

| Control | Parameter | Default |
|---------|-----------|---------|
| Max daily loss | `MAX_DAILY_LOSS_PCT` | 10% of capital |
| Max open straddles | `MAX_OPEN_STRADDLES` | 12 |
| Max per session | `MAX_SINGLE_SESSION_STRADDLES` | 6 |
| API circuit breaker | `CIRCUIT_BREAKER_API_ERRORS` | 5 consecutive errors |
| Bid-ask spread filter | `MAX_BID_ASK_SPREAD_PCT` | 10% of mid |
| Option slippage cap | `OPTION_MAX_SLIPPAGE_PCT` | 5% above initial ask |
| Daily reset | 08:00 UTC | Force-close stragglers, reset P&L |

## Setup

### Prerequisites
- Python 3.12+
- Bybit Unified Trading account with API key (Spot + Options + Derivatives permissions)

### Install

```bash
git clone https://github.com/orlandokohjy/bybit-0dte-algo.git
cd bybit-0dte-algo
pip install -r requirements.txt
cp .env.example .env
```

### Configure `.env`

```
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
BYBIT_TESTNET=false
BYBIT_DEMO=true          # Start with demo trading
DRY_RUN=false             # Set true to simulate without orders

TELEGRAM_BOT_TOKEN=       # Optional
TELEGRAM_CHAT_ID=         # Optional
```

### Run

```bash
# Full scheduler (waits for next session time)
python main.py

# Manual immediate session (for testing)
HOLD_SECONDS=120 python manual_session.py

# Test suite (4 levels: offline → public API → auth → dry-run)
python test_algo.py --level 4
```

## Modes

| Mode | Orders | API Calls | Use Case |
|------|--------|-----------|----------|
| `DRY_RUN=true` | Simulated | Market data only | Logic validation |
| `BYBIT_DEMO=true` | Real (demo money) | Demo endpoint | Integration testing |
| `BYBIT_DEMO=false` | Real (real money) | Mainnet | Production |

## Key Design Decisions

**Perps over spot:** 10x capital efficiency. The basis between perp and spot is negligible for 0DTE holds (typically <0.1%). Funding rate cost is minimal for intraday positions.

**IOC chase over GTC:** Options are illiquid. GTC orders sit in the book and may never fill. IOC with progressive re-pricing guarantees a fill within the slippage budget or fails fast.

**Fixed qty per straddle (0.01 BTC):** Bybit's minimum option order is 0.01 BTC. Instead of varying size, the algo varies the *number* of straddle units.

**Perp first, put second:** If the put chase fails, unwinding a perp is a single market order. If we bought the put first and the perp failed, we'd be stuck with an illiquid option position.

**State persistence:** Open positions are written to `state/positions.json` on every change. If the process restarts, it loads existing positions and continues monitoring.
