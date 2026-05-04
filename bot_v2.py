#!/usr/bin/env python3
"""
Weather Trading Bot v2 - Polymarket
====================================
Multi-source forecasts (ECMWF + HRRR + METAR), Kelly-sized positions, EV
filter, stop-loss + trailing stop + take-profit, sigma calibration from
resolved markets. 20 cities across US, EU, Asia, Americas, and Oceania.

Usage:
    python bot_v2.py             # main loop (full scan hourly, monitor every 10 min)
    python bot_v2.py once        # single full scan then exit (cron / smoke test)
    python bot_v2.py status      # balance and open positions
    python bot_v2.py report      # full PnL report including resolved markets
    python bot_v2.py reset       # reset state (balance + history)

Data layout:
    data/state.json              # bankroll + W/L counters
    data/calibration.json        # per-city, per-source sigma estimates
    data/markets/{city}_{date}.json  # one file per market we've watched
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from weatherbot.common import (
    C, MONTHS, LOCATIONS,
    info, ok, skip, warn,
    load_config, load_env_file,
    parse_temp_range, hours_until, in_bucket, bucket_prob, calc_ev, calc_kelly,
    get_polymarket_event, get_polymarket_market, parse_outcome_prices,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_env_file()  # populate os.environ from .env
_cfg = load_config()

BALANCE          = float(_cfg.get("balance", 10000.0))
MAX_BET          = float(_cfg.get("v2_max_bet", 20.0))
MIN_EV           = float(_cfg.get("v2_min_ev", 0.10))
MAX_PRICE        = float(_cfg.get("v2_max_price", 0.45))
MIN_VOLUME       = float(_cfg.get("v2_min_volume", 500))
MIN_HOURS        = float(_cfg.get("v2_min_hours", 2.0))
MAX_HOURS        = float(_cfg.get("v2_max_hours", 72.0))
KELLY_FRACTION   = float(_cfg.get("v2_kelly_fraction", 0.25))
MAX_SLIPPAGE     = float(_cfg.get("v2_max_slippage", 0.03))
SCAN_INTERVAL    = int(_cfg.get("v2_scan_interval", 3600))
MONITOR_INTERVAL = int(_cfg.get("v2_monitor_interval", 600))
CALIBRATION_MIN  = int(_cfg.get("v2_calibration_min", 30))
LOCATIONS_ALL    = bool(_cfg.get("v2_locations_all", True))

# Pull VC_KEY from env first, then config (env wins).
VC_KEY = os.environ.get("VC_KEY") or _cfg.get("vc_key", "")
if VC_KEY in ("YOUR_KEY_HERE", "your_key_here", ""):
    VC_KEY = ""

# Default sigma per unit - used until calibration kicks in.
SIGMA_F = 2.0  # Fahrenheit cities
SIGMA_C = 1.2  # Celsius cities (smaller in absolute terms)

# v2 either uses all 20 LOCATIONS or only the subset listed in config["locations"].
if LOCATIONS_ALL:
    ACTIVE_CITIES = list(LOCATIONS.keys())
else:
    raw = _cfg.get("locations", "")
    ACTIVE_CITIES = [s.strip().lower() for s in raw.split(",") if s.strip() in LOCATIONS]
    if not ACTIVE_CITIES:
        ACTIVE_CITIES = list(LOCATIONS.keys())

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
CALIBRATION_FILE = DATA_DIR / "calibration.json"
MARKETS_DIR = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Calibration (per-city, per-source sigma estimates)
# ---------------------------------------------------------------------------

_cal: dict = {}


def load_cal() -> dict:
    if CALIBRATION_FILE.exists():
        try:
            return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            warn(f"Bad JSON in {CALIBRATION_FILE} - resetting calibration")
    return {}


def get_sigma(city_slug: str, source: str = "ecmwf") -> float:
    key = f"{city_slug}_{source}"
    if key in _cal:
        return _cal[key]["sigma"]
    return SIGMA_F if LOCATIONS[city_slug]["unit"] == "F" else SIGMA_C


def run_calibration(markets: list) -> dict:
    """Recompute sigma from MAE of forecast vs actual on resolved markets."""
    resolved = [m for m in markets if m.get("status") == "resolved" and m.get("actual_temp") is not None]
    if not resolved:
        return load_cal()

    cal = load_cal()
    updated = []

    for source in ["ecmwf", "hrrr", "metar"]:
        for city in {m["city"] for m in resolved}:
            errors = []
            for m in (mk for mk in resolved if mk["city"] == city):
                snap = next(
                    (s for s in reversed(m.get("forecast_snapshots", []))
                     if s.get(source) is not None),
                    None,
                )
                if snap is not None:
                    errors.append(abs(snap[source] - m["actual_temp"]))
            if len(errors) < CALIBRATION_MIN:
                continue
            mae = sum(errors) / len(errors)
            key = f"{city}_{source}"
            old = cal.get(key, {}).get(
                "sigma", SIGMA_F if LOCATIONS[city]["unit"] == "F" else SIGMA_C
            )
            new = round(mae, 3)
            cal[key] = {
                "sigma": new,
                "n": len(errors),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if abs(new - old) > 0.05:
                updated.append(f"{LOCATIONS[city]['name']} {source}: {old:.2f}→{new:.2f}")

    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    if updated:
        info(f"[calibration] {', '.join(updated)}")
    return cal


# ---------------------------------------------------------------------------
# Forecast sources
# ---------------------------------------------------------------------------

def _retry_get(url: str, params: Optional[dict] = None, attempts: int = 3) -> Optional[dict]:
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=(5, 10))
            if r.status_code == 200:
                return r.json()
        except (requests.RequestException, json.JSONDecodeError):
            pass
        if i < attempts - 1:
            time.sleep(2)
    return None


def get_ecmwf(city_slug: str, dates: list) -> dict:
    """ECMWF via Open-Meteo with bias correction. Works globally."""
    loc = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    params = {
        "latitude": loc["lat"], "longitude": loc["lon"],
        "daily": "temperature_2m_max", "temperature_unit": temp_unit,
        "forecast_days": 7, "timezone": loc.get("tz", "UTC"),
        "models": "ecmwf_ifs025",
        "bias_correction": "true",
    }
    data = _retry_get("https://api.open-meteo.com/v1/forecast", params)
    if not data or "error" in data:
        return {}
    result = {}
    daily = data.get("daily", {})
    for date, temp in zip(daily.get("time", []), daily.get("temperature_2m_max", [])):
        if date in dates and temp is not None:
            result[date] = round(temp, 1) if unit == "C" else round(temp)
    return result


def get_hrrr(city_slug: str, dates: list) -> dict:
    """HRRR/GFS-seamless via Open-Meteo. US cities only, near-term horizon."""
    loc = LOCATIONS[city_slug]
    if loc["region"] != "us":
        return {}
    params = {
        "latitude": loc["lat"], "longitude": loc["lon"],
        "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
        "forecast_days": 3, "timezone": loc.get("tz", "UTC"),
        "models": "gfs_seamless",
    }
    data = _retry_get("https://api.open-meteo.com/v1/forecast", params)
    if not data or "error" in data:
        return {}
    result = {}
    daily = data.get("daily", {})
    for date, temp in zip(daily.get("time", []), daily.get("temperature_2m_max", [])):
        if date in dates and temp is not None:
            result[date] = round(temp)
    return result


def get_metar(city_slug: str) -> Optional[float]:
    """Latest observed temperature from a METAR station. D+0 only."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    try:
        r = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": station, "format": "json"},
            timeout=(5, 8),
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, list) and data:
            temp_c = data[0].get("temp")
            if temp_c is not None:
                return round(float(temp_c) * 9 / 5 + 32) if unit == "F" else round(float(temp_c), 1)
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        pass
    return None


def get_actual_temp(city_slug: str, date_str: str) -> Optional[float]:
    """Resolved actual daily-max via Visual Crossing. Needs VC_KEY."""
    if not VC_KEY:
        return None
    loc = LOCATIONS[city_slug]
    unit_group = "us" if loc["unit"] == "F" else "metric"
    url = (f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
           f"/{loc['station']}/{date_str}/{date_str}")
    params = {"unitGroup": unit_group, "key": VC_KEY,
              "include": "days", "elements": "tempmax"}
    try:
        r = requests.get(url, params=params, timeout=(5, 8))
        if r.status_code != 200:
            return None
        days = r.json().get("days", [])
        if days and days[0].get("tempmax") is not None:
            return round(float(days[0]["tempmax"]), 1)
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        pass
    return None


def check_market_resolved(market_id: str) -> Optional[bool]:
    """
    Has Polymarket closed this market?
    Returns: True (YES won), False (NO won), None (still open or ambiguous).
    """
    market = get_polymarket_market(market_id)
    if market is None:
        return None
    if not market.get("closed", False):
        return None
    yes, _ = parse_outcome_prices(market)
    if yes >= 0.95:
        return True
    if yes <= 0.05:
        return False
    return None


# ---------------------------------------------------------------------------
# Market storage (one JSON file per market)
# ---------------------------------------------------------------------------

def market_path(city_slug: str, date_str: str) -> Path:
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"


def load_market(city_slug: str, date_str: str) -> Optional[dict]:
    p = market_path(city_slug, date_str)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_market(market: dict) -> None:
    p = market_path(market["city"], market["date"])
    p.write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")


def load_all_markets() -> list:
    out = []
    for f in MARKETS_DIR.glob("*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return out


def new_market(city_slug: str, date_str: str, event: dict, hours: float) -> dict:
    loc = LOCATIONS[city_slug]
    return {
        "city":               city_slug,
        "city_name":          loc["name"],
        "date":               date_str,
        "unit":               loc["unit"],
        "station":            loc["station"],
        "event_end_date":     event.get("endDate", ""),
        "hours_at_discovery": round(hours, 1),
        "status":             "open",
        "position":           None,
        "actual_temp":        None,
        "resolved_outcome":   None,
        "pnl":                None,
        "forecast_snapshots": [],
        "market_snapshots":   [],
        "all_outcomes":       [],
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Bankroll state
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            warn(f"Bad JSON in {STATE_FILE} - resetting state")
    return {
        "balance": BALANCE,
        "starting_balance": BALANCE,
        "total_trades": 0,
        "wins": 0, "losses": 0,
        "peak_balance": BALANCE,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def reset_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    for f in MARKETS_DIR.glob("*.json"):
        f.unlink()
    if CALIBRATION_FILE.exists():
        CALIBRATION_FILE.unlink()
    print(f"  [OK] State reset - balance back to ${BALANCE:.2f}, market history cleared")


# ---------------------------------------------------------------------------
# Bet sizing
# ---------------------------------------------------------------------------

def bet_size(kelly: float, balance: float) -> float:
    """Cap Kelly stake at MAX_BET dollars."""
    return round(min(kelly * balance, MAX_BET), 2)


# ---------------------------------------------------------------------------
# Forecast snapshots
# ---------------------------------------------------------------------------

def take_forecast_snapshot(city_slug: str, dates: list) -> dict:
    """Pull forecasts from all sources and pick the best one per date."""
    now_str = datetime.now(timezone.utc).isoformat()
    ecmwf = get_ecmwf(city_slug, dates)
    hrrr = get_hrrr(city_slug, dates)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    metar_temp = get_metar(city_slug)

    out = {}
    for date in dates:
        snap = {
            "ts":    now_str,
            "ecmwf": ecmwf.get(date),
            "hrrr":  hrrr.get(date) if date <= (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d") else None,
            "metar": metar_temp if date == today else None,
        }
        # Best source: HRRR for US near-term, otherwise ECMWF.
        loc = LOCATIONS[city_slug]
        if loc["region"] == "us" and snap["hrrr"] is not None:
            snap["best"], snap["best_source"] = snap["hrrr"], "hrrr"
        elif snap["ecmwf"] is not None:
            snap["best"], snap["best_source"] = snap["ecmwf"], "ecmwf"
        else:
            snap["best"], snap["best_source"] = None, None
        out[date] = snap
    return out


# ---------------------------------------------------------------------------
# Order execution helper - single source of truth for ask/bid/spread
# ---------------------------------------------------------------------------

def fetch_real_book(market_id: str) -> Optional[dict]:
    """
    Pull bestAsk/bestBid for a single market. Polymarket exposes these on
    /markets/{id}; outcomePrices is laggier and sometimes missing the spread.
    """
    market = get_polymarket_market(market_id)
    if market is None:
        return None
    try:
        ask = float(market.get("bestAsk", 0)) or None
        bid = float(market.get("bestBid", 0)) or None
        if ask is None or bid is None:
            yes, no = parse_outcome_prices(market)
            ask = ask or yes
            bid = bid or yes
        return {"ask": ask, "bid": bid, "spread": round((ask or 0) - (bid or 0), 4)}
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main scan: update markets, open/close positions
# ---------------------------------------------------------------------------

def scan_and_update() -> tuple[int, int, int]:
    """One full pass: snapshot forecasts, manage positions, resolve closed."""
    global _cal
    now = datetime.now(timezone.utc)
    state = load_state()
    balance = state["balance"]
    new_pos = closed = resolved = 0

    for city_slug in ACTIVE_CITIES:
        loc = LOCATIONS[city_slug]
        unit_sym = loc["unit"]
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
            snapshots = take_forecast_snapshot(city_slug, dates)
            time.sleep(0.3)
        except Exception as e:
            print(f"skipped ({e})")
            continue

        for i, date in enumerate(dates):
            dt = datetime.strptime(date, "%Y-%m-%d")
            event = get_polymarket_event(city_slug, MONTHS[dt.month - 1], dt.day, dt.year)
            if not event:
                continue

            hours = hours_until(event.get("endDate", ""))
            horizon = f"D+{i}"

            mkt = load_market(city_slug, date)
            if mkt is None:
                if hours < MIN_HOURS or hours > MAX_HOURS:
                    continue
                mkt = new_market(city_slug, date, event, hours)

            if mkt["status"] == "resolved":
                continue

            # --- snapshot all market outcomes ---
            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid = str(market.get("id", ""))
                volume = float(market.get("volume", 0) or 0)
                rng = parse_temp_range(question)
                if not rng:
                    continue
                yes, no = parse_outcome_prices(market)
                # outcomePrices[0] is YES (bid-ish), [1] is NO (1-yes-ish).
                # Use the live API's bestAsk/bestBid as the trading price.
                try:
                    ask = float(market.get("bestAsk", yes) or yes)
                    bid = float(market.get("bestBid", yes) or yes)
                except (ValueError, TypeError):
                    ask = bid = yes
                outcomes.append({
                    "question": question,
                    "market_id": mid,
                    "range": rng,
                    "bid": round(bid, 4),
                    "ask": round(ask, 4),
                    "price": round(yes, 4),
                    "spread": round(max(0.0, ask - bid), 4),
                    "volume": round(volume, 0),
                })

            outcomes.sort(key=lambda x: x["range"][0])
            mkt["all_outcomes"] = outcomes

            snap = snapshots.get(date, {})
            mkt["forecast_snapshots"].append({
                "ts":          snap.get("ts"),
                "horizon":     horizon,
                "hours_left":  round(hours, 1),
                "ecmwf":       snap.get("ecmwf"),
                "hrrr":        snap.get("hrrr"),
                "metar":       snap.get("metar"),
                "best":        snap.get("best"),
                "best_source": snap.get("best_source"),
            })

            top = max(outcomes, key=lambda x: x["price"]) if outcomes else None
            mkt["market_snapshots"].append({
                "ts":         snap.get("ts"),
                "top_bucket": f"{top['range'][0]}-{top['range'][1]}{unit_sym}" if top else None,
                "top_price":  top["price"] if top else None,
            })

            forecast_temp = snap.get("best")
            best_source = snap.get("best_source")

            # --- manage open position: stop / trailing / take-profit / forecast-shift exit ---
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                # find current bid for our specific outcome
                current_price = None
                for o in outcomes:
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["bid"]
                        break

                if current_price is not None:
                    entry = pos["entry_price"]
                    stop = pos.get("stop_price", entry * 0.80)

                    # trailing: once 20% in profit, move stop to break-even
                    if current_price >= entry * 1.20 and stop < entry:
                        pos["stop_price"] = entry
                        pos["trailing_activated"] = True

                    # decide take-profit threshold based on time-to-resolution
                    if hours < 24:
                        take_profit = None  # ride to resolution
                    elif hours < 48:
                        take_profit = 0.85
                    else:
                        take_profit = 0.75

                    take_triggered = take_profit is not None and current_price >= take_profit
                    stop_triggered = current_price <= pos.get("stop_price", entry * 0.80)
                    forecast_shifted = False

                    # forecast-shift exit: forecast moved out of our bucket by margin
                    if forecast_temp is not None:
                        bl, bh = pos["bucket_low"], pos["bucket_high"]
                        buf = 2.0 if loc["unit"] == "F" else 1.0
                        if not in_bucket(forecast_temp, bl, bh):
                            if bl == -999:
                                forecast_shifted = forecast_temp > bh + buf
                            elif bh == 999:
                                forecast_shifted = forecast_temp < bl - buf
                            else:
                                forecast_shifted = (forecast_temp < bl - buf) or (forecast_temp > bh + buf)

                    if take_triggered or stop_triggered or forecast_shifted:
                        pnl = round((current_price - entry) * pos["shares"], 2)
                        balance += pos["cost"] + pnl
                        pos["closed_at"] = snap.get("ts") or datetime.now(timezone.utc).isoformat()
                        pos["exit_price"] = current_price
                        pos["pnl"] = pnl
                        pos["status"] = "closed"
                        if take_triggered:
                            reason, label = "take_profit", "TAKE"
                        elif forecast_shifted:
                            reason, label = "forecast_changed", "CLOSE-FX"
                        elif current_price < entry:
                            reason, label = "stop_loss", "STOP"
                        else:
                            reason, label = "trailing_stop", "TRAILING"
                        pos["close_reason"] = reason
                        closed += 1
                        sign = "+" if pnl >= 0 else ""
                        print(f"\n  [{label}] {loc['name']} {date} | "
                              f"entry ${entry:.3f} → ${current_price:.3f} | PnL: {sign}{pnl:.2f}")

            # --- consider opening a position ---
            if not mkt.get("position") and forecast_temp is not None and hours >= MIN_HOURS:
                sigma = get_sigma(city_slug, best_source or "ecmwf")
                matched = next((o for o in outcomes if in_bucket(forecast_temp, *o["range"])), None)

                if matched and matched["volume"] >= MIN_VOLUME:
                    t_low, t_high = matched["range"]
                    bid, ask, spread = matched["bid"], matched["ask"], matched["spread"]

                    p = bucket_prob(forecast_temp, t_low, t_high, sigma)
                    ev = calc_ev(p, ask)

                    if ev >= MIN_EV and ask < MAX_PRICE and spread <= MAX_SLIPPAGE:
                        kelly = calc_kelly(p, ask, KELLY_FRACTION)
                        size = bet_size(kelly, balance)

                        if size >= 0.50:
                            # re-fetch live book to be sure we don't trade stale prices
                            live = fetch_real_book(matched["market_id"])
                            if live:
                                ask = live.get("ask") or ask
                                bid = live.get("bid") or bid
                                spread = live.get("spread") or spread

                            if spread <= MAX_SLIPPAGE and ask < MAX_PRICE:
                                shares = round(size / ask, 2)
                                signal = {
                                    "market_id":     matched["market_id"],
                                    "question":      matched["question"],
                                    "bucket_low":    t_low,
                                    "bucket_high":   t_high,
                                    "entry_price":   ask,
                                    "bid_at_entry":  bid,
                                    "spread":        spread,
                                    "shares":        shares,
                                    "cost":          size,
                                    "stop_price":    round(ask * 0.80, 4),
                                    "p":             round(p, 4),
                                    "ev":            round(calc_ev(p, ask), 4),
                                    "kelly":         round(kelly, 4),
                                    "forecast_temp": forecast_temp,
                                    "forecast_src":  best_source,
                                    "sigma":         sigma,
                                    "opened_at":     snap.get("ts") or datetime.now(timezone.utc).isoformat(),
                                    "status":        "open",
                                    "pnl":           None,
                                    "exit_price":    None,
                                    "close_reason":  None,
                                    "closed_at":     None,
                                }
                                balance -= size
                                mkt["position"] = signal
                                state["total_trades"] += 1
                                new_pos += 1
                                src = (best_source or "?").upper()
                                bucket = f"{t_low}-{t_high}{unit_sym}"
                                print(f"\n  [BUY]  {loc['name']} {horizon} {date} | {bucket} | "
                                      f"${ask:.3f} | EV {signal['ev']:+.2f} | "
                                      f"${size:.2f} ({src})")

            if hours < 0.5 and mkt["status"] == "open":
                mkt["status"] = "closed"

            save_market(mkt)
            time.sleep(0.1)

        print("ok")

    # --- auto-resolve closed markets ---
    for mkt in load_all_markets():
        if mkt["status"] == "resolved":
            continue
        pos = mkt.get("position")
        if not pos or pos.get("status") != "closed":
            # only resolve markets where we actually had a position that was closed,
            # OR where there was no position at all (just record the outcome)
            if pos and pos.get("status") == "open":
                # check if Polymarket has resolved
                won = check_market_resolved(pos.get("market_id", ""))
                if won is None:
                    continue
                # close the still-open position at 1.0 or 0.0
                pnl = (round(pos["shares"] * (1 - pos["entry_price"]), 2) if won
                       else round(-pos["cost"], 2))
                balance += pos["cost"] + pnl
                pos["exit_price"] = 1.0 if won else 0.0
                pos["pnl"] = pnl
                pos["close_reason"] = "resolved"
                pos["closed_at"] = now.isoformat()
                pos["status"] = "closed"
                mkt["pnl"] = pnl
                mkt["status"] = "resolved"
                mkt["resolved_outcome"] = "win" if won else "loss"
                if won:
                    state["wins"] += 1
                else:
                    state["losses"] += 1
                resolved += 1
                sign = "+" if pnl >= 0 else ""
                print(f"  [{('WIN' if won else 'LOSS')}] {mkt['city_name']} {mkt['date']} | PnL: {sign}{pnl:.2f}")
            elif not pos:
                # no position taken - just check if we can record actual_temp for calibration
                actual = get_actual_temp(mkt["city"], mkt["date"]) if hours_until(mkt.get("event_end_date")) <= 0 else None
                if actual is not None:
                    mkt["actual_temp"] = actual
                    mkt["status"] = "resolved"
                    mkt["resolved_outcome"] = "no_position"
                    mkt["pnl"] = 0.0
                    resolved += 1
            save_market(mkt)
            continue

        # position was closed by us earlier (stop/take/trailing/forecast)
        # - still record actual temp for calibration if available
        if mkt["status"] != "resolved":
            actual = get_actual_temp(mkt["city"], mkt["date"])
            if actual is not None:
                mkt["actual_temp"] = actual
            mkt["status"] = "resolved"
            mkt["resolved_outcome"] = "win" if (pos.get("pnl") or 0) > 0 else "loss"
            mkt["pnl"] = pos.get("pnl", 0)
            if (pos.get("pnl") or 0) > 0:
                state["wins"] += 1
            else:
                state["losses"] += 1
            resolved += 1
            save_market(mkt)
        time.sleep(0.2)

    state["balance"] = round(balance, 2)
    state["peak_balance"] = max(state.get("peak_balance", balance), balance)
    save_state(state)

    # run calibration if we have enough data
    all_mkts = load_all_markets()
    resolved_count = len([m for m in all_mkts if m.get("status") == "resolved"])
    if resolved_count >= CALIBRATION_MIN:
        _cal = run_calibration(all_mkts)

    return new_pos, closed, resolved


# ---------------------------------------------------------------------------
# Quick monitor pass - only checks open positions for stop/take, no full forecast pull
# ---------------------------------------------------------------------------

def monitor_positions() -> int:
    open_mkts = [m for m in load_all_markets()
                 if m.get("position") and m["position"].get("status") == "open"]
    if not open_mkts:
        return 0

    state = load_state()
    balance = state["balance"]
    closed = 0

    for mkt in open_mkts:
        pos = mkt["position"]
        live = fetch_real_book(pos["market_id"])
        if live is None or live.get("bid") is None:
            continue
        current = live["bid"]

        entry = pos["entry_price"]
        stop = pos.get("stop_price", entry * 0.80)
        loc = LOCATIONS.get(mkt["city"], {})
        city_name = loc.get("name", mkt["city"])

        hours_left = hours_until(mkt.get("event_end_date"))
        if hours_left < 24:
            take_profit = None
        elif hours_left < 48:
            take_profit = 0.85
        else:
            take_profit = 0.75

        if current >= entry * 1.20 and stop < entry:
            pos["stop_price"] = entry
            pos["trailing_activated"] = True
            print(f"  [TRAILING] {city_name} {mkt['date']} - stop → BE ${entry:.3f}")

        take_t = take_profit is not None and current >= take_profit
        stop_t = current <= pos.get("stop_price", entry * 0.80)

        if not (take_t or stop_t):
            continue

        pnl = round((current - entry) * pos["shares"], 2)
        balance += pos["cost"] + pnl
        pos["closed_at"] = datetime.now(timezone.utc).isoformat()
        pos["exit_price"] = current
        pos["pnl"] = pnl
        pos["status"] = "closed"
        if take_t:
            pos["close_reason"], label = "take_profit", "TAKE"
        elif current < entry:
            pos["close_reason"], label = "stop_loss", "STOP"
        else:
            pos["close_reason"], label = "trailing_stop", "TRAILING"
        closed += 1
        sign = "+" if pnl >= 0 else ""
        print(f"  [{label}] {city_name} {mkt['date']} | "
              f"${entry:.3f} → ${current:.3f} | {hours_left:.0f}h left | PnL: {sign}{pnl:.2f}")
        save_market(mkt)

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)
    return closed


# ---------------------------------------------------------------------------
# CLI: status / report
# ---------------------------------------------------------------------------

def print_status() -> None:
    state = load_state()
    markets = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m.get("status") == "resolved"]

    bal = state["balance"]
    start = state["starting_balance"]
    ret_pct = (bal - start) / start * 100 if start else 0
    wins, losses = state["wins"], state["losses"]
    total = wins + losses

    print(f"\n{'=' * 55}")
    print(f"  WEATHERBOT v2 - STATUS")
    print(f"{'=' * 55}")
    sign = "+" if ret_pct >= 0 else ""
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {sign}{ret_pct:.1f}%)")
    if total:
        print(f"  Trades:      {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}")
    else:
        print("  Trades:      0")
    print(f"  Open:        {len(open_pos)}")
    print(f"  Resolved:    {len(resolved)}")

    if open_pos:
        print(f"\n  Open positions:")
        unrealized_total = 0.0
        for m in open_pos:
            pos = m["position"]
            unit_sym = m["unit"]
            label = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"
            current = pos["entry_price"]
            for o in m.get("all_outcomes", []):
                if o["market_id"] == pos["market_id"]:
                    current = o["price"]
                    break
            ur = round((current - pos["entry_price"]) * pos["shares"], 2)
            unrealized_total += ur
            sign = "+" if ur >= 0 else ""
            src = (pos.get("forecast_src") or "?").upper()
            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} → ${current:.3f} | "
                  f"PnL: {sign}{ur:.2f} | {src}")
        sign = "+" if unrealized_total >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{unrealized_total:.2f}")

    print(f"{'=' * 55}\n")


def print_report() -> None:
    markets = load_all_markets()
    resolved = [m for m in markets
                if m.get("status") == "resolved" and m.get("pnl") is not None]

    print(f"\n{'=' * 55}")
    print(f"  WEATHERBOT v2 - FULL REPORT")
    print(f"{'=' * 55}")

    if not resolved:
        print("  No resolved markets yet.")
        return

    total_pnl = sum(m["pnl"] for m in resolved)
    wins = [m for m in resolved if m.get("resolved_outcome") == "win"]
    losses = [m for m in resolved if m.get("resolved_outcome") == "loss"]

    print(f"\n  Total resolved: {len(resolved)}")
    print(f"  Wins:           {len(wins)} | Losses: {len(losses)}")
    if (wins or losses):
        print(f"  Win rate:       {len(wins)/(len(wins)+len(losses)):.0%}")
    sign = "+" if total_pnl >= 0 else ""
    print(f"  Total PnL:      {sign}{total_pnl:.2f}")

    print(f"\n  By city:")
    for city in sorted({m["city"] for m in resolved}):
        group = [m for m in resolved if m["city"] == city]
        w = len([m for m in group if m.get("resolved_outcome") == "win"])
        pnl = sum(m["pnl"] for m in group)
        name = LOCATIONS[city]["name"]
        sign = "+" if pnl >= 0 else ""
        print(f"    {name:<16} {w}/{len(group)}  PnL: {sign}{pnl:.2f}")

    print(f"{'=' * 55}\n")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_loop() -> None:
    global _cal
    _cal = load_cal()

    print(f"\n{'=' * 55}")
    print(f"  WEATHERBOT v2 - RUNNING")
    print(f"{'=' * 55}")
    print(f"  Cities:    {len(ACTIVE_CITIES)}")
    print(f"  Balance:   ${BALANCE:,.0f} | Max bet: ${MAX_BET}")
    print(f"  Scan:      {SCAN_INTERVAL // 60} min | Monitor: {MONITOR_INTERVAL // 60} min")
    print(f"  Sources:   ECMWF + HRRR(US) + METAR(D+0)")
    print(f"  Data:      {DATA_DIR.resolve()}")
    print(f"  Ctrl+C to stop\n")

    last_full = 0.0

    try:
        while True:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            now_ts = time.time()

            if now_ts - last_full >= SCAN_INTERVAL:
                print(f"[{now_str}] full scan...")
                try:
                    new_pos, closed, resolved = scan_and_update()
                    state = load_state()
                    print(f"  balance: ${state['balance']:,.2f} | "
                          f"new: {new_pos} | closed: {closed} | resolved: {resolved}")
                    last_full = time.time()
                except requests.ConnectionError:
                    print("  Connection lost - waiting 60s")
                    time.sleep(60)
                    continue
                except Exception as e:
                    print(f"  Error: {e} - waiting 60s")
                    time.sleep(60)
                    continue
            else:
                print(f"[{now_str}] monitoring positions...")
                try:
                    stopped = monitor_positions()
                    if stopped:
                        state = load_state()
                        print(f"  balance: ${state['balance']:,.2f}")
                except Exception as e:
                    print(f"  Monitor error: {e}")

            time.sleep(MONITOR_INTERVAL)
    except KeyboardInterrupt:
        print("\n  Stopping - state already saved each cycle. Bye.")


def main() -> int:
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "run"
    global _cal

    if cmd == "run":
        run_loop()
    elif cmd == "once":
        _cal = load_cal()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] single full scan...")
        new_pos, closed, resolved = scan_and_update()
        state = load_state()
        print(f"  balance: ${state['balance']:,.2f} | "
              f"new: {new_pos} | closed: {closed} | resolved: {resolved}")
    elif cmd == "status":
        _cal = load_cal()
        print_status()
    elif cmd == "report":
        _cal = load_cal()
        print_report()
    elif cmd == "reset":
        reset_state()
    else:
        print("Usage: python bot_v2.py [run|once|status|report|reset]")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
