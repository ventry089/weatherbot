#!/usr/bin/env python3
"""
Local dashboard for the weatherbot.

Serves a single-page HTML view of:
  - Bankroll (state.json)
  - Open positions
  - Resolved markets / PnL
  - Recent forecast and market snapshots

Auto-detects which bot is running:
  - If data/state.json exists → reads v2 layout
  - Else, falls back to simulation.json (v1 layout)

Run:
    python dashboard/app.py
    open http://127.0.0.1:5000

Set DASHBOARD_PORT or DASHBOARD_HOST in env to override.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

# Allow running this script from any CWD
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "state.json"
MARKETS_DIR = DATA_DIR / "markets"
SIM_FILE = ROOT / "simulation.json"  # v1's data file

app = Flask(__name__, static_folder=str(Path(__file__).parent / "static"), static_url_path="")


def _read_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_v2_state() -> dict:
    state = _read_json(STATE_FILE) or {}
    markets = []
    if MARKETS_DIR.exists():
        for f in sorted(MARKETS_DIR.glob("*.json")):
            data = _read_json(f)
            if data is not None:
                markets.append(data)

    open_positions = []
    resolved = []
    closed_by_bot = []  # stop-loss / take-profit / trailing / forecast-shift, NOT market resolution
    for m in markets:
        pos = m.get("position")
        if pos and pos.get("status") == "open":
            current = pos["entry_price"]
            for o in m.get("all_outcomes", []):
                if o["market_id"] == pos["market_id"]:
                    current = o.get("price", current)
                    break
            unrealized = round((current - pos["entry_price"]) * pos["shares"], 2)
            open_positions.append({
                "city":           m.get("city_name"),
                "date":           m.get("date"),
                "bucket":         f"{pos['bucket_low']}-{pos['bucket_high']}{m.get('unit', 'F')}",
                "entry_price":    pos["entry_price"],
                "current_price":  current,
                "shares":         pos["shares"],
                "cost":           pos["cost"],
                "unrealized_pnl": unrealized,
                "ev":             pos.get("ev"),
                "kelly":          pos.get("kelly"),
                "forecast_src":   pos.get("forecast_src"),
                "forecast_temp":  pos.get("forecast_temp"),
            })
        # Position closed by bot (stop / take / trailing / forecast shift) but market not yet resolved
        if pos and pos.get("status") == "closed" and m.get("status") != "resolved":
            closed_by_bot.append({
                "city":          m.get("city_name"),
                "date":          m.get("date"),
                "reason":        pos.get("close_reason", "?"),
                "pnl":           pos.get("pnl"),
                "entry_price":   pos.get("entry_price"),
                "exit_price":    pos.get("exit_price"),
            })
        if m.get("status") == "resolved" and m.get("pnl") is not None:
            resolved.append({
                "city":     m.get("city_name"),
                "date":     m.get("date"),
                "outcome":  m.get("resolved_outcome"),
                "pnl":      m.get("pnl"),
                "actual":   m.get("actual_temp"),
            })

    resolved.sort(key=lambda r: r["date"], reverse=True)
    closed_by_bot.sort(key=lambda r: r["date"], reverse=True)

    realized_pnl = round(
        sum(r["pnl"] or 0 for r in resolved) +
        sum(r["pnl"] or 0 for r in closed_by_bot), 2,
    )
    unrealized_pnl = round(sum(p["unrealized_pnl"] for p in open_positions), 2)
    total_pnl = round(realized_pnl + unrealized_pnl, 2)

    cash = state.get("balance", 0)
    open_value = round(sum(p["cost"] + p["unrealized_pnl"] for p in open_positions), 2)
    equity = round(cash + open_value, 2)

    return {
        "bot_version":  "v2",
        "balance":      cash,                     # free cash, not invested
        "equity":       equity,                   # cash + value of open positions
        "starting":     state.get("starting_balance", 0),
        "peak_balance": state.get("peak_balance", state.get("balance", 0)),
        "wins":         state.get("wins", 0),
        "losses":       state.get("losses", 0),
        "total_trades": state.get("total_trades", 0),
        "total_pnl":    total_pnl,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "open_positions": open_positions,
        "closed_by_bot": closed_by_bot[:30],
        "resolved":     resolved[:50],
        "markets_total": len(markets),
    }


def _load_v1_state() -> dict:
    sim = _read_json(SIM_FILE) or {}
    positions = sim.get("positions", {}) or {}

    open_positions = []
    for mid, pos in positions.items():
        open_positions.append({
            "city":           pos.get("location"),
            "date":           pos.get("date"),
            "bucket":         pos.get("question", "")[:50],
            "entry_price":    pos.get("entry_price", 0),
            "current_price":  pos.get("entry_price", 0),
            "shares":         pos.get("shares", 0),
            "cost":           pos.get("cost", 0),
            "unrealized_pnl": 0.0,
            "forecast_src":   "nws",
            "forecast_temp":  pos.get("forecast_temp"),
        })

    resolved = []
    for t in sim.get("trades", []):
        if t.get("type") == "exit":
            resolved.append({
                "city":   None,
                "date":   t.get("closed_at", "")[:10],
                "outcome": "win" if (t.get("pnl") or 0) > 0 else "loss",
                "pnl":    t.get("pnl"),
                "actual": None,
            })
    resolved.sort(key=lambda r: r["date"], reverse=True)

    return {
        "bot_version":  "v1",
        "balance":      sim.get("balance", 0),
        "starting":     sim.get("starting_balance", 0),
        "peak_balance": sim.get("peak_balance", sim.get("balance", 0)),
        "wins":         sim.get("wins", 0),
        "losses":       sim.get("losses", 0),
        "total_trades": sim.get("total_trades", 0),
        "total_pnl":    round(sum(r["pnl"] or 0 for r in resolved), 2),
        "open_positions": open_positions,
        "resolved":     resolved[:50],
        "markets_total": len(sim.get("trades", [])),
    }


@app.route("/api/state")
def api_state():
    """Return whichever bot has data; v2 takes priority if both exist."""
    if STATE_FILE.exists():
        return jsonify(_load_v2_state())
    if SIM_FILE.exists():
        return jsonify(_load_v1_state())
    return jsonify({
        "bot_version": "none",
        "balance": 0, "starting": 0, "peak_balance": 0,
        "wins": 0, "losses": 0, "total_trades": 0, "total_pnl": 0,
        "open_positions": [], "resolved": [], "markets_total": 0,
        "message": "No data yet. Run bot_v1.py --live or bot_v2.py once first.",
    })


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def main() -> int:
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "5000"))
    debug = bool(os.environ.get("DASHBOARD_DEBUG"))
    print(f"\n  Dashboard running at http://{host}:{port}")
    print(f"  Reading data from: {DATA_DIR.resolve()} (or {SIM_FILE.resolve()} for v1)")
    print(f"  Press Ctrl+C to stop\n")
    app.run(host=host, port=port, debug=debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
