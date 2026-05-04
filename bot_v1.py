#!/usr/bin/env python3
"""
Weather Trading Bot v1 - Polymarket
====================================
Simple base bot. Finds mispriced US daily-high-temperature markets using NWS
forecasts. Six US cities only, flat position sizing, fixed entry/exit thresholds.

For the multi-source, Kelly-sized, 20-city version, see bot_v2.py.

Usage:
    python bot_v1.py             # paper mode - show signals only
    python bot_v1.py --live      # simulate trades against virtual $1,000
    python bot_v1.py --reset     # reset simulation balance
    python bot_v1.py --positions # show open positions and PnL
    python bot_v1.py --once      # single scan then exit (for cron / smoke test)
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import requests

from weatherbot.common import (
    C, MONTHS, US_NWS_GRIDS, LOCATIONS,
    info, ok, skip, warn,
    load_config,
    parse_temp_range, hours_until,
    get_polymarket_event, get_polymarket_market, parse_outcome_prices,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_cfg = load_config()

ENTRY_THRESHOLD = float(_cfg.get("v1_entry_threshold", 0.15))
EXIT_THRESHOLD  = float(_cfg.get("v1_exit_threshold", 0.45))
MAX_TRADES      = int(_cfg.get("v1_max_trades_per_run", 5))
MIN_HOURS_LEFT  = float(_cfg.get("v1_min_hours_to_resolution", 2))
POSITION_PCT    = float(_cfg.get("v1_position_pct", 0.05))
SIM_BALANCE     = float(_cfg.get("balance", 1000.0))

ACTIVE_LOCATIONS = [
    s.strip().lower()
    for s in _cfg.get("locations", "nyc,chicago,miami,dallas,seattle,atlanta").split(",")
    if s.strip()
]
# v1 only supports US cities (NWS API)
ACTIVE_LOCATIONS = [c for c in ACTIVE_LOCATIONS if c in US_NWS_GRIDS]
if not ACTIVE_LOCATIONS:
    ACTIVE_LOCATIONS = list(US_NWS_GRIDS.keys())

SIM_FILE = "simulation.json"

# ---------------------------------------------------------------------------
# Simulation state
# ---------------------------------------------------------------------------

def _empty_sim() -> dict:
    return {
        "balance": SIM_BALANCE,
        "starting_balance": SIM_BALANCE,
        "positions": {},
        "trades": [],
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "peak_balance": SIM_BALANCE,
    }


def load_sim() -> dict:
    try:
        with open(SIM_FILE, encoding="utf-8") as f:
            sim = json.load(f)
        # backfill any missing keys to keep older sim files working
        defaults = _empty_sim()
        for k, v in defaults.items():
            sim.setdefault(k, v)
        return sim
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty_sim()


def save_sim(sim: dict) -> None:
    with open(SIM_FILE, "w", encoding="utf-8") as f:
        json.dump(sim, f, indent=2)


def reset_sim() -> None:
    if os.path.exists(SIM_FILE):
        os.remove(SIM_FILE)
    print(f"{C.GREEN}  [OK] Simulation reset - balance back to ${SIM_BALANCE:.2f}{C.RESET}")


# ---------------------------------------------------------------------------
# NWS forecast
# ---------------------------------------------------------------------------

NWS_HEADERS = {"User-Agent": "weatherbot/2.0 (https://github.com/alteregoeth-ai/weatherbot)"}


def get_forecast(city_slug: str) -> dict:
    """
    Fetch daily-max-temp forecast from NWS.

    Combines real station observations (already-recorded today) with the hourly
    forecast (upcoming) so the daily max for today reflects what's already
    happened - not what was forecast 12 hours ago.

    Returns: {"YYYY-MM-DD": int_temp_F}
    """
    forecast_url = US_NWS_GRIDS.get(city_slug)
    station_id = LOCATIONS.get(city_slug, {}).get("station")
    if not forecast_url or not station_id:
        return {}

    daily_max: dict = {}

    # Past observations (today and earlier)
    try:
        obs_url = f"https://api.weather.gov/stations/{station_id}/observations"
        r = requests.get(obs_url, params={"limit": 48}, headers=NWS_HEADERS, timeout=10)
        if r.status_code == 200:
            for obs in r.json().get("features", []):
                props = obs.get("properties", {})
                time_str = (props.get("timestamp") or "")[:10]
                temp_c = (props.get("temperature") or {}).get("value")
                if time_str and temp_c is not None:
                    temp_f = round(temp_c * 9 / 5 + 32)
                    if time_str not in daily_max or temp_f > daily_max[time_str]:
                        daily_max[time_str] = temp_f
    except requests.RequestException as e:
        warn(f"Observations error for {city_slug}: {e}")

    # Upcoming hourly forecast
    try:
        r = requests.get(forecast_url, headers=NWS_HEADERS, timeout=10)
        if r.status_code == 200:
            periods = r.json().get("properties", {}).get("periods", [])
            for p in periods:
                date = (p.get("startTime") or "")[:10]
                temp = p.get("temperature")
                if date and temp is not None:
                    if p.get("temperatureUnit") == "C":
                        temp = round(temp * 9 / 5 + 32)
                    if date not in daily_max or temp > daily_max[date]:
                        daily_max[date] = temp
    except requests.RequestException as e:
        warn(f"Forecast error for {city_slug}: {e}")

    return daily_max


# ---------------------------------------------------------------------------
# Show open positions
# ---------------------------------------------------------------------------

def show_positions() -> None:
    sim = load_sim()
    positions = sim["positions"]
    print(f"\n{C.BOLD}Open positions:{C.RESET}")
    if not positions:
        print("  (none)")
        return

    total_pnl = 0.0
    for mid, pos in positions.items():
        market = get_polymarket_market(mid)
        if market is None:
            current_price = pos["entry_price"]
            note = " (price unavailable)"
        else:
            yes, _ = parse_outcome_prices(market)
            current_price = yes
            note = ""

        pnl = (current_price - pos["entry_price"]) * pos["shares"]
        total_pnl += pnl
        pnl_color = C.GREEN if pnl >= 0 else C.RED
        sign = "+" if pnl >= 0 else ""
        print(f"\n  - {pos['question'][:65]}")
        print(f"    Entry: ${pos['entry_price']:.3f} | Now: ${current_price:.3f}{note} | "
              f"Shares: {pos['shares']:.1f} | PnL: {pnl_color}{sign}${pnl:.2f}{C.RESET}")
        print(f"    Cost: ${pos['cost']:.2f}")

    pnl_color = C.GREEN if total_pnl >= 0 else C.RED
    sign = "+" if total_pnl >= 0 else ""
    print(f"\n  Balance:      ${sim['balance']:.2f}")
    print(f"  Open PnL:     {pnl_color}{sign}${total_pnl:.2f}{C.RESET}")
    print(f"  Total trades: {sim['total_trades']} | W/L: {sim['wins']}/{sim['losses']}")


# ---------------------------------------------------------------------------
# Main strategy
# ---------------------------------------------------------------------------

def _check_exits(positions: dict, sim: dict, balance: float, dry_run: bool) -> tuple[float, int]:
    """Walk open positions, sell anything above EXIT_THRESHOLD."""
    print(f"\n{C.BOLD}Checking exits...{C.RESET}")
    exits_found = 0

    for mid, pos in list(positions.items()):
        market = get_polymarket_market(mid)
        if market is None:
            continue
        current_price, _ = parse_outcome_prices(market)

        if current_price < EXIT_THRESHOLD:
            continue

        exits_found += 1
        pnl = (current_price - pos["entry_price"]) * pos["shares"]
        ok(f"EXIT: {pos['question'][:50]}...")
        info(f"Price ${current_price:.3f} >= exit ${EXIT_THRESHOLD:.2f} | PnL: +${pnl:.2f}")

        if dry_run:
            skip("Paper mode - not selling")
            continue

        balance += pos["cost"] + pnl
        if pnl > 0:
            sim["wins"] += 1
        else:
            sim["losses"] += 1
        sim["trades"].append({
            "type": "exit",
            "question": pos["question"],
            "entry_price": pos["entry_price"],
            "exit_price": current_price,
            "pnl": round(pnl, 2),
            "cost": pos["cost"],
            "closed_at": datetime.now(timezone.utc).isoformat(),
        })
        del positions[mid]
        sign = "+" if pnl >= 0 else ""
        ok(f"Closed - PnL: {sign}{pnl:.2f}")

    if exits_found == 0:
        skip("No exit opportunities")
    return balance, exits_found


def _scan_entries(positions: dict, sim: dict, balance: float, dry_run: bool) -> tuple[float, int]:
    """Walk active cities, scan 4 days ahead, open new positions where edge exists."""
    print(f"\n{C.BOLD}Scanning for entry signals...{C.RESET}")
    trades_executed = 0
    now = datetime.now(timezone.utc)

    for city_slug in ACTIVE_LOCATIONS:
        loc = LOCATIONS.get(city_slug)
        if not loc:
            warn(f"Unknown location: {city_slug}")
            continue

        forecast = get_forecast(city_slug)
        if not forecast:
            continue

        for i in range(4):
            date = now + timedelta(days=i)
            date_str = date.strftime("%Y-%m-%d")
            month = MONTHS[date.month - 1]

            forecast_temp = forecast.get(date_str)
            if forecast_temp is None:
                continue

            event = get_polymarket_event(city_slug, month, date.day, date.year)
            if not event:
                continue

            hours_left = hours_until(event.get("endDate"))

            print(f"\n{C.BOLD}{loc['name']} - {date_str}{C.RESET}")
            info(f"Forecast: {forecast_temp}°F | Resolves in: {hours_left:.0f}h")

            if hours_left < MIN_HOURS_LEFT:
                skip(f"Resolves in {hours_left:.0f}h - too soon")
                continue

            matched = None
            for market in event.get("markets", []):
                question = market.get("question", "")
                rng = parse_temp_range(question)
                if rng and rng[0] <= forecast_temp <= rng[1]:
                    yes_price, _ = parse_outcome_prices(market)
                    matched = {"market": market, "question": question,
                              "price": yes_price, "range": rng}
                    break

            if not matched:
                skip(f"No bucket found for {forecast_temp}°F")
                continue

            price = matched["price"]
            market_id = str(matched["market"].get("id", ""))
            question = matched["question"]

            info(f"Bucket: {question[:60]}")
            info(f"Market price: ${price:.3f}")

            if price >= ENTRY_THRESHOLD:
                skip(f"Price ${price:.3f} above threshold ${ENTRY_THRESHOLD:.2f}")
                continue
            if market_id in positions:
                skip("Already in this market")
                continue
            if trades_executed >= MAX_TRADES:
                skip(f"Max trades ({MAX_TRADES}) reached")
                continue

            position_size = round(balance * POSITION_PCT, 2)
            if position_size < 0.50:
                skip(f"Position size ${position_size:.2f} too small")
                continue

            shares = position_size / price
            ok(f"SIGNAL - buying {shares:.1f} shares @ ${price:.3f} = ${position_size:.2f}")

            if dry_run:
                skip("Paper mode - not buying")
                trades_executed += 1
                continue

            balance -= position_size
            positions[market_id] = {
                "question": question,
                "entry_price": price,
                "shares": shares,
                "cost": position_size,
                "date": date_str,
                "location": city_slug,
                "forecast_temp": forecast_temp,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }
            sim["total_trades"] += 1
            sim["trades"].append({
                "type": "entry",
                "question": question,
                "entry_price": price,
                "shares": shares,
                "cost": position_size,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            })
            trades_executed += 1
            ok(f"Position opened - ${position_size:.2f} deducted")

    return balance, trades_executed


def run(dry_run: bool = True) -> None:
    print(f"\n{C.BOLD}{C.CYAN}Weather Trading Bot v1{C.RESET}")
    print("=" * 50)

    sim = load_sim()
    balance = sim["balance"]
    starting = sim["starting_balance"]
    total_return = (balance - starting) / starting * 100 if starting else 0

    mode = f"{C.YELLOW}PAPER MODE{C.RESET}" if dry_run else f"{C.GREEN}LIVE (SIM){C.RESET}"
    return_str = (f"{C.GREEN}+{total_return:.1f}%{C.RESET}" if total_return >= 0
                  else f"{C.RED}{total_return:.1f}%{C.RESET}")

    print(f"\n  Mode:            {mode}")
    print(f"  Virtual balance: {C.BOLD}${balance:.2f}{C.RESET} (started ${starting:.2f}, {return_str})")
    print(f"  Position size:   {POSITION_PCT:.0%} of balance per trade")
    print(f"  Entry threshold: below ${ENTRY_THRESHOLD:.2f}")
    print(f"  Exit threshold:  above ${EXIT_THRESHOLD:.2f}")
    print(f"  Cities:          {', '.join(ACTIVE_LOCATIONS)}")
    print(f"  Trades W/L:      {sim['wins']}/{sim['losses']}")

    balance, exits_found = _check_exits(sim["positions"], sim, balance, dry_run)
    balance, trades_executed = _scan_entries(sim["positions"], sim, balance, dry_run)

    if not dry_run:
        sim["balance"] = round(balance, 2)
        sim["peak_balance"] = max(sim.get("peak_balance", balance), balance)
        save_sim(sim)

    print(f"\n{'=' * 50}")
    print(f"{C.BOLD}Summary:{C.RESET}")
    info(f"Balance:         ${balance:.2f}")
    info(f"Trades this run: {trades_executed}")
    info(f"Exits found:     {exits_found}")
    if dry_run:
        print(f"\n  {C.YELLOW}[PAPER MODE - use --live to record simulated trades]{C.RESET}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Weather Trading Bot v1")
    parser.add_argument("--live", action="store_true",
                        help="Execute trades (updates simulation balance)")
    parser.add_argument("--positions", action="store_true",
                        help="Show open positions")
    parser.add_argument("--reset", action="store_true",
                        help="Reset simulation to starting balance")
    parser.add_argument("--once", action="store_true",
                        help="Single scan then exit (default; here for clarity)")
    args = parser.parse_args()

    if args.reset:
        reset_sim()
    elif args.positions:
        show_positions()
    else:
        run(dry_run=not args.live)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
