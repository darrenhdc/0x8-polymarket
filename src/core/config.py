"""
Polymarket AI Trading System Configuration
"""
import os
from dotenv import load_dotenv

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_TRADING_SYSTEM_DIR = os.path.join(_PROJECT_ROOT, "trading_system")

load_dotenv(os.path.join(_TRADING_SYSTEM_DIR, ".env"))

# ── Trading Mode ──────────────────────────────────────────────
# Set PAPER_TRADING=false in .env to enable real trading
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"

# ── Polymarket API Credentials (required for real trading) ────
# Private key: read from env only, NEVER written to disk.
# Set it before running:  export POLYMARKET_PRIVATE_KEY=0x...
# Or leave empty — will prompt interactively at startup when needed.
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")

# Keystore file — default: data/keystore.json (created by create_keystore.py)
_DEFAULT_KEYSTORE = os.path.join(_TRADING_SYSTEM_DIR, "data", "keystore.json")
KEYSTORE_FILE = os.getenv("KEYSTORE_FILE") or _DEFAULT_KEYSTORE


def _resolve_private_key() -> str:
    """Resolve private key: env var → keystore (password only) → interactive prompt."""
    global POLYMARKET_PRIVATE_KEY
    # 1) Already in env
    env_pk = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    if env_pk:
        POLYMARKET_PRIVATE_KEY = env_pk
        return env_pk
    # 2) Keystore file (only needs password)
    ks_path = KEYSTORE_FILE
    if ks_path and os.path.exists(ks_path):
        import json as _json, getpass
        from eth_account import Account
        with open(ks_path, "r") as f:
            ks = _json.load(f)
        pwd = os.getenv("KEYSTORE_PASSWORD", "") or getpass.getpass("🔑 Enter keystore password: ")
        try:
            pk = "0x" + Account.decrypt(ks, pwd).hex()
            acct = Account.from_key(pk)
            print(f"✅ Unlocked: {acct.address}")
            os.environ["POLYMARKET_PRIVATE_KEY"] = pk
            POLYMARKET_PRIVATE_KEY = pk
            return pk
        except Exception as e:
            print(f"❌ Keystore unlock failed: {e}")
            return ""
    # 3) Interactive prompt (works in terminal, skipped in non-TTY)
    if not PAPER_TRADING:
        import sys
        if sys.stdin.isatty():
            import getpass
            pk = getpass.getpass("🔑 Enter private key (not saved to disk): ")
            if pk:
                os.environ["POLYMARKET_PRIVATE_KEY"] = pk
                POLYMARKET_PRIVATE_KEY = pk
                return pk
    return ""


POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "")  # optional proxy wallet

# Chain ID: 137 = Polygon mainnet
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))

# ── LLM / AI Configuration (for event scanner + LLM pricing) ─
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-pro")

# API Endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ── Safety: daily loss limit (real trading only) ──────────────
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "5.0"))

# Trading Parameters (adjusted for $5 USDC balance)
# These are defaults; override in .env
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "5.0"))
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "5.0"))  # Max $5 per position
MAX_TOTAL_EXPOSURE = float(os.getenv("MAX_TOTAL_EXPOSURE", "10.0"))
MIN_TRADE_SIZE = float(os.getenv("MIN_TRADE_SIZE", "1.0"))

# Category whitelist for event-scanner style trading.
# Set ALLOW_WEATHER_MARKETS=true in .env if you have a weather prediction source.
ALLOW_WEATHER_MARKETS = os.getenv("ALLOW_WEATHER_MARKETS", "true").lower() == "true"

# Risk Management
MAX_POSITIONS = 3           # Maximum number of open positions
STOP_LOSS_PERCENT = 0.15  # 15% stop loss
TAKE_PROFIT_PERCENT = 0.30  # 30% take profit

# Dynamic stop loss for very low probability positions (<15%)
LOW_PROB_STOP_LOSS = 0.10  # 10% stop loss for positions with entry < 0.15
LOW_PROB_THRESHOLD = 0.15  # Threshold for "low probability" positions

# Cooldown for stopped-out positions (prevent immediate re-entry)
STOPPED_OUT_COOLDOWN_HOURS = 24  # Don't re-enter a position for 24h after stop loss

# Market Selection Criteria
MIN_LIQUIDITY = 2000.0  # Minimum $2k liquidity (relaxed for more opportunities)
MIN_VOLUME_24H = 500.0  # Minimum $500 24h volume (relaxed for more opportunities)
MAX_END_DATE_DAYS = 365  # Trade markets ending within 1 year (expanded for more opportunities)

# AI Decision Parameters
CONFIDENCE_THRESHOLD = 0.50  # Minimum confidence to trade (lowered for more activity)
MARKET_ANALYSIS_COUNT = 50  # Number of markets to analyze per cycle (increased)

# Strategy Optimization - Learned from losses
MIN_CONTRARIAN_PRICE = 0.10  # Only play contrarian if price >= 10%
# Reason: OpenAI (3.9%) and GTA VI (2.4%) both triggered stop losses

# Position sizing based on risk (entry price)
RISK_TIERS = {
    'safe': {      # entry >= 0.35
        'min_price': 0.35,
        'max_position': 2,
        'stop_loss': 0.15
    },
    'medium': {    # 0.15 <= entry < 0.35
        'min_price': 0.15,
        'max_position': 1.5,
        'stop_loss': 0.15
    },
    'risky': {     # 0.10 <= entry < 0.15
        'min_price': 0.10,
        'max_position': 1,
        'stop_loss': 0.10
    }
}

# Logging
LOG_DIR = os.path.join(_TRADING_SYSTEM_DIR, "logs")
DATA_DIR = os.path.join(_TRADING_SYSTEM_DIR, "data")

# State files
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
DECISIONS_FILE = os.path.join(DATA_DIR, "decisions.json")
STOPPED_OUT_FILE = os.path.join(DATA_DIR, "stopped_out.json")  # Track stopped-out markets
TRADE_JOURNAL_FILE = os.path.join(DATA_DIR, "trade_journal.json")  # Detailed trade reasoning log
