"""
weatherbot.live_trading - real money trading scaffold for Polymarket CLOB.

THIS MODULE IS NOT WIRED INTO bot_v2.py BY DEFAULT.

To enable real trading you have to:
  1. Run paper trading for 2+ weeks, resolve 50+ markets, see positive PnL
  2. Set LIVE_TRADING=true in .env (plus the wallet keys)
  3. Manually edit bot_v2.py to call execute_buy() / execute_sell() instead
     of the simulated balance updates

Even with LIVE_TRADING=true, this module enforces:
  - max_bet hard cap (config.live_max_bet)
  - daily loss limit (config.live_daily_loss_limit)
  - max open positions (config.live_max_open_positions)
  - max total exposure (config.live_max_total_exposure)
  - min balance check (config.live_min_balance)
  - dry-run mode (config.live_dry_run_first) - logs orders without placing

Read everything in this file before flipping LIVE_TRADING to true.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# py_clob_client is an optional dep. Only required when LIVE_TRADING=true.
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

class LiveTradingError(Exception):
    """Raised when something is wrong with the live trading setup."""


def is_live_trading_enabled() -> bool:
    """True only if LIVE_TRADING=true in env. Default false for safety."""
    return os.environ.get("LIVE_TRADING", "false").lower() == "true"


def preflight_checks(config: dict) -> dict:
    """
    Run all safety checks before initializing a live trading client.
    Raises LiveTradingError if anything is wrong.
    Returns a dict of validated env vars on success.
    """
    if not is_live_trading_enabled():
        raise LiveTradingError(
            "LIVE_TRADING is not set to 'true' in .env. "
            "Set LIVE_TRADING=true to enable real orders. "
            "Make sure you actually want this before doing it."
        )

    if not CLOB_AVAILABLE:
        raise LiveTradingError(
            "py-clob-client is not installed. "
            "Run: pip install py-clob-client"
        )

    pk = os.environ.get("POLY_PRIVATE_KEY", "").strip()
    if not pk:
        raise LiveTradingError("POLY_PRIVATE_KEY not set in .env")
    if not pk.startswith("0x") or len(pk) != 66:
        raise LiveTradingError(
            f"POLY_PRIVATE_KEY looks malformed (got {len(pk)} chars, expected 66 starting with 0x)"
        )

    funder = os.environ.get("POLY_FUNDER_ADDRESS", "").strip()
    if not funder:
        raise LiveTradingError("POLY_FUNDER_ADDRESS not set in .env")
    if not funder.startswith("0x") or len(funder) != 42:
        raise LiveTradingError(
            f"POLY_FUNDER_ADDRESS looks malformed (got {len(funder)} chars, expected 42)"
        )

    sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))
    if sig_type not in (0, 1, 2):
        raise LiveTradingError(
            f"POLY_SIGNATURE_TYPE must be 0, 1, or 2 (got {sig_type})"
        )

    # Check that risk limits exist in config
    required_limits = [
        "live_max_bet", "live_max_total_exposure",
        "live_max_open_positions", "live_daily_loss_limit",
        "live_min_balance",
    ]
    missing = [k for k in required_limits if k not in config]
    if missing:
        raise LiveTradingError(
            f"config.json is missing live_* risk limits: {missing}. "
            "Copy them from .env.example or update config.json."
        )

    return {
        "private_key":  pk,
        "funder":       funder,
        "sig_type":     sig_type,
        "dry_run":      bool(config.get("live_dry_run_first", True)),
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_client: Optional["ClobClient"] = None


def get_client(config: dict) -> "ClobClient":
    """Initialize the CLOB client. Cached so we don't re-derive creds every call."""
    global _client
    if _client is not None:
        return _client

    creds = preflight_checks(config)

    _client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,                          # Polygon mainnet
        key=creds["private_key"],
        signature_type=creds["sig_type"],
        funder=creds["funder"],
    )
    _client.set_api_creds(_client.create_or_derive_api_creds())
    return _client


# ---------------------------------------------------------------------------
# Risk checks (called before every order)
# ---------------------------------------------------------------------------

def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_loss_so_far(state: dict, today: str) -> float:
    """Sum up today's realized losses from state."""
    daily = state.get("daily_pnl", {})
    return -min(0.0, daily.get(today, 0.0))


def check_can_open(config: dict, state: dict, open_positions: list, size: float) -> tuple[bool, str]:
    """
    Returns (allowed, reason). reason is empty if allowed.
    Call this before sending any buy order.
    """
    if size <= 0:
        return False, "size <= 0"

    if size > config["live_max_bet"]:
        return False, f"size ${size:.2f} exceeds live_max_bet ${config['live_max_bet']:.2f}"

    balance = state.get("balance", 0)
    if balance < config["live_min_balance"]:
        return False, f"balance ${balance:.2f} below live_min_balance ${config['live_min_balance']:.2f}"

    if balance - size < 0:
        return False, "would go negative"

    if len(open_positions) >= config["live_max_open_positions"]:
        return False, f"already at live_max_open_positions ({config['live_max_open_positions']})"

    total_exposure = sum(p.get("cost", 0) for p in open_positions) + size
    if total_exposure > config["live_max_total_exposure"]:
        return False, f"total exposure ${total_exposure:.2f} would exceed live_max_total_exposure ${config['live_max_total_exposure']:.2f}"

    today = _today_iso()
    daily_loss = _daily_loss_so_far(state, today)
    if daily_loss >= config["live_daily_loss_limit"]:
        return False, f"daily loss ${daily_loss:.2f} hit live_daily_loss_limit ${config['live_daily_loss_limit']:.2f}"

    return True, ""


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

ORDERS_LOG = Path("data/live_orders.log")


def _log_order(action: str, payload: dict) -> None:
    """Append every order attempt to a log file. Never fails silently."""
    ORDERS_LOG.parent.mkdir(exist_ok=True)
    entry = {
        "ts":     datetime.now(timezone.utc).isoformat(),
        "action": action,
        **payload,
    }
    with ORDERS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def execute_buy(config: dict, token_id: str, price: float, size_usd: float,
                state: dict, open_positions: list) -> Optional[dict]:
    """
    Place a real BUY limit order on Polymarket CLOB.

    Returns the response dict from CLOB on success, None on failure.
    Always logs the attempt regardless of outcome.

    THIS WILL SPEND REAL USDC if LIVE_TRADING=true and dry_run=false.
    """
    allowed, reason = check_can_open(config, state, open_positions, size_usd)
    if not allowed:
        _log_order("buy_rejected", {
            "token_id": token_id, "price": price, "size_usd": size_usd, "reason": reason,
        })
        print(f"  [LIVE-SKIP] BUY {token_id[:8]} rejected: {reason}")
        return None

    shares = round(size_usd / price, 2)

    if config.get("live_dry_run_first", True):
        _log_order("buy_dry_run", {
            "token_id": token_id, "price": price, "size_usd": size_usd, "shares": shares,
        })
        print(f"  [DRY-RUN] BUY {shares:.2f} shares @ ${price:.3f} = ${size_usd:.2f}")
        return {"dry_run": True, "shares": shares, "price": price, "size_usd": size_usd}

    # === REAL ORDER PLACEMENT ===
    # This block only runs when LIVE_TRADING=true AND live_dry_run_first=false.
    try:
        client = get_client(config)
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side=BUY,
        )
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)
        _log_order("buy_placed", {
            "token_id": token_id, "price": price, "size_usd": size_usd, "shares": shares,
            "response": resp,
        })
        success = bool(resp and resp.get("success"))
        if success:
            print(f"  [LIVE-BUY] {shares:.2f} shares @ ${price:.3f} placed successfully")
        else:
            print(f"  [LIVE-FAIL] BUY rejected by CLOB: {resp}")
        return resp if success else None
    except Exception as e:
        _log_order("buy_error", {
            "token_id": token_id, "price": price, "size_usd": size_usd, "error": str(e),
        })
        print(f"  [LIVE-ERROR] BUY failed: {e}")
        return None


def execute_sell(config: dict, token_id: str, price: float, shares: float) -> Optional[dict]:
    """
    Place a real SELL limit order to close a position.

    Returns the response dict from CLOB on success, None on failure.
    Closing positions has fewer pre-flight checks than opening - we always want
    to be able to exit even if other limits are hit.
    """
    if not is_live_trading_enabled():
        return None
    if shares <= 0 or price <= 0:
        return None

    if config.get("live_dry_run_first", True):
        _log_order("sell_dry_run", {
            "token_id": token_id, "price": price, "shares": shares,
        })
        print(f"  [DRY-RUN] SELL {shares:.2f} shares @ ${price:.3f}")
        return {"dry_run": True, "shares": shares, "price": price}

    try:
        client = get_client(config)
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side=SELL,
        )
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)
        _log_order("sell_placed", {
            "token_id": token_id, "price": price, "shares": shares, "response": resp,
        })
        success = bool(resp and resp.get("success"))
        if success:
            print(f"  [LIVE-SELL] {shares:.2f} shares @ ${price:.3f} placed")
        else:
            print(f"  [LIVE-FAIL] SELL rejected by CLOB: {resp}")
        return resp if success else None
    except Exception as e:
        _log_order("sell_error", {
            "token_id": token_id, "price": price, "shares": shares, "error": str(e),
        })
        print(f"  [LIVE-ERROR] SELL failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Diagnostic CLI
# ---------------------------------------------------------------------------

def diagnose() -> int:
    """
    Run preflight checks and print the result. Doesn't place any orders.
    Useful to verify your .env is configured correctly before going live.

    Run with: python -m weatherbot.live_trading
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from weatherbot.common import load_config, load_env_file

    print("=" * 55)
    print("  Weatherbot live trading diagnostic")
    print("=" * 55)

    load_env_file()
    config = load_config()

    print(f"\n  LIVE_TRADING env:   {os.environ.get('LIVE_TRADING', 'not set')}")
    print(f"  py_clob_client:     {'installed' if CLOB_AVAILABLE else 'NOT INSTALLED'}")
    print(f"  POLY_PRIVATE_KEY:   {'set (' + str(len(os.environ.get('POLY_PRIVATE_KEY', ''))) + ' chars)' if os.environ.get('POLY_PRIVATE_KEY') else 'not set'}")
    print(f"  POLY_FUNDER:        {'set' if os.environ.get('POLY_FUNDER_ADDRESS') else 'not set'}")
    print(f"  Signature type:     {os.environ.get('POLY_SIGNATURE_TYPE', '1 (default)')}")
    print(f"  Dry-run mode:       {config.get('live_dry_run_first', True)}")
    print(f"\n  Risk limits from config.json:")
    for k in ["live_max_bet", "live_max_total_exposure", "live_max_open_positions",
             "live_daily_loss_limit", "live_min_balance"]:
        print(f"    {k}: {config.get(k, 'NOT SET')}")

    print(f"\n  Pre-flight check:")
    try:
        creds = preflight_checks(config)
        print(f"    [OK] All checks passed.")
        print(f"    Funder: {creds['funder'][:10]}...{creds['funder'][-4:]}")
        if creds["dry_run"]:
            print(f"    Mode: DRY-RUN (no real orders will be placed)")
        else:
            print(f"    Mode: LIVE (real orders WILL be placed)")
        return 0
    except LiveTradingError as e:
        print(f"    [FAIL] {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(diagnose())
