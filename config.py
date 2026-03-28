# ~/mara/config.py
# Worker-internal constants. Runtime regime config lives in config/*.yaml.
# This file is imported directly by workers and the test suite.

INITIAL_CAPITAL_USD     = 200.0
MIN_TRADE_SIZE_USD      = 10.0
MAX_POSITION_PCT        = 0.80

VAR_CONFIDENCE          = 0.99
VAR_SIMULATIONS         = 10_000
VAR_HORIZON_HOURS       = 8
MAX_VAR_PCT             = 0.05
CVAR_MULTIPLIER         = 1.5
LOOKBACK_DAYS           = 30
MIN_SHARPE_TO_TRADE     = 0.5
SHARPE_RISK_FREE_RATE   = 0.045

REBALANCE_INTERVAL_SEC  = 3_600
FUNDING_RATE_INTERVAL   = 28_800
MIN_FUNDING_RATE        = 0.0003

PAPER_TRADING           = True
SLIPPAGE_MODEL_PCT      = 0.0005
FEE_MODEL_PCT           = 0.0004

EXCHANGES               = ["okx"]
RECESSION_PAIRS         = ["SH", "PSQ"]
RECESSION_REGIMES       = ["BEAR_RECESSION", "CRISIS_ACUTE"]
QUOTE_CURRENCY          = "USDT"

USE_LIVE_RATES          = False
USE_LIVE_OHLCV          = False

SWING_MACD_FAST         = 8
SWING_MACD_SLOW         = 21
SWING_MACD_SIGNAL       = 5
SWING_TIMEFRAME         = "4h"
SWING_CACHE_TTL_SEC     = 14_400
SWING_PAIRS             = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "AVAX/USDT"]
SWING_STOP_LOSS_PCT     = 0.02
SWING_TAKE_PROFIT_RATIO = 2.0
SWING_RSI_PERIOD        = 14
SWING_RSI_BULL_MIN      = 40
SWING_RSI_BEAR_MAX      = 60

LOG_LEVEL               = "INFO"
LOG_FILE                = "logs/hypervisor.log"
STATE_SNAPSHOT_FILE     = "logs/portfolio_state.json"