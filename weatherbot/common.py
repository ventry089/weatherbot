"""
weatherbot.common - shared building blocks for v1 and v2.

Keeps the math, the location table, the Polymarket helpers, and the parsers
in one place so the two bots stay in sync.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
# These coordinates match the airport stations Polymarket resolves on, NOT the
# city centers. See README for why this matters.

LOCATIONS = {
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us",   "tz": "America/New_York"},
    "chicago":      {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "region": "us",   "tz": "America/Chicago"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "region": "us",   "tz": "America/New_York"},
    "dallas":       {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "region": "us",   "tz": "America/Chicago"},
    "seattle":      {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "region": "us",   "tz": "America/Los_Angeles"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us",   "tz": "America/New_York"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "region": "eu",   "tz": "Europe/London"},
    "paris":        {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "region": "eu",   "tz": "Europe/Paris"},
    "munich":       {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "region": "eu",   "tz": "Europe/Berlin"},
    "ankara":       {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "region": "eu",   "tz": "Europe/Istanbul"},
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "region": "asia", "tz": "Asia/Seoul"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "region": "asia", "tz": "Asia/Tokyo"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia", "tz": "Asia/Shanghai"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "region": "asia", "tz": "Asia/Singapore"},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia", "tz": "Asia/Kolkata"},
    "tel-aviv":     {"lat": 32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG", "unit": "C", "region": "asia", "tz": "Asia/Jerusalem"},
    "toronto":      {"lat": 43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ", "unit": "C", "region": "ca",   "tz": "America/Toronto"},
    "sao-paulo":    {"lat": -23.4356, "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR", "unit": "C", "region": "sa",   "tz": "America/Sao_Paulo"},
    "buenos-aires": {"lat": -34.8222, "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ", "unit": "C", "region": "sa",   "tz": "America/Argentina/Buenos_Aires"},
    "wellington":   {"lat": -41.3272, "lon":  174.8052, "name": "Wellington",    "station": "NZWN", "unit": "C", "region": "oc",   "tz": "Pacific/Auckland"},
}

# v1 only knows these six US cities - it uses the NWS API which is US-only.
US_NWS_GRIDS = {
    "nyc":     "https://api.weather.gov/gridpoints/OKX/37,39/forecast/hourly",
    "chicago": "https://api.weather.gov/gridpoints/LOT/66,77/forecast/hourly",
    "miami":   "https://api.weather.gov/gridpoints/MFL/106,51/forecast/hourly",
    "dallas":  "https://api.weather.gov/gridpoints/FWD/87,107/forecast/hourly",
    "seattle": "https://api.weather.gov/gridpoints/SEW/124,61/forecast/hourly",
    "atlanta": "https://api.weather.gov/gridpoints/FFC/50,82/forecast/hourly",
}

MONTHS = ["january", "february", "march", "april", "may", "june",
         "july", "august", "september", "october", "november", "december"]


# ---------------------------------------------------------------------------
# Colors with auto-detect for non-TTY
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if sys.platform == "win32" and not os.environ.get("ANSICON"):
        return os.environ.get("TERM", "") != ""
    return True


class C:
    if _supports_color():
        GREEN  = "\033[92m"
        YELLOW = "\033[93m"
        RED    = "\033[91m"
        CYAN   = "\033[96m"
        GRAY   = "\033[90m"
        RESET  = "\033[0m"
        BOLD   = "\033[1m"
    else:
        GREEN = YELLOW = RED = CYAN = GRAY = RESET = BOLD = ""


def ok(msg):   print(f"{C.GREEN}  [OK] {msg}{C.RESET}")
def warn(msg): print(f"{C.YELLOW}  [!]  {msg}{C.RESET}")
def info(msg): print(f"{C.CYAN}  {msg}{C.RESET}")
def skip(msg): print(f"{C.GRAY}  [-]  {msg}{C.RESET}")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str = "config.json") -> dict:
    """Load config.json, return empty dict if missing (caller should use defaults)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        warn(f"{path} not found - using defaults")
        return {}
    except json.JSONDecodeError as e:
        warn(f"Bad JSON in {path}: {e} - using defaults")
        return {}


def load_env_file(path: str = ".env") -> None:
    """Minimal .env loader - sets os.environ for KEY=value lines. No deps."""
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        warn(f"Could not read {path}: {e}")


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def in_bucket(forecast: float, t_low: float, t_high: float) -> bool:
    """Check if a forecast falls into a temperature bucket."""
    if t_low == t_high:
        return round(float(forecast)) == round(t_low)
    return t_low <= float(forecast) <= t_high


def bucket_prob(forecast: float, t_low: float, t_high: float, sigma: float = 2.0) -> float:
    """
    Probability the forecast lands in [t_low, t_high].

    Edge buckets ('or below', 'or higher') get a normal-distribution treatment
    so the bot can value the tails. Regular buckets are 0/1 because at this
    scale (1-2°F wide) the forecast resolution doesn't justify a continuous
    estimate without per-source calibration.
    """
    s = max(0.1, float(sigma))
    if t_low == -999:
        return norm_cdf((t_high - float(forecast)) / s)
    if t_high == 999:
        return 1.0 - norm_cdf((t_low - float(forecast)) / s)
    return 1.0 if in_bucket(forecast, t_low, t_high) else 0.0


def calc_ev(p: float, price: float) -> float:
    """Expected value of a 1-unit bet at `price` with true prob `p`. Pays 1 on win."""
    if price <= 0 or price >= 1:
        return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)


def calc_kelly(p: float, price: float, fraction: float = 1.0) -> float:
    """
    Fractional Kelly stake for a binary bet.

    Returns the fraction of bankroll to bet, clamped to [0, 1].
    `fraction` shrinks the stake (0.25 = quarter Kelly, the standard safe choice).
    """
    if price <= 0 or price >= 1:
        return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * fraction, 1.0), 4)


# ---------------------------------------------------------------------------
# Polymarket parsing
# ---------------------------------------------------------------------------

def parse_temp_range(question: str) -> Optional[Tuple[float, float]]:
    """
    Extract a temperature range from a Polymarket question string.

    Returns (low, high) in degrees, or None if not parseable.
    Edge buckets use sentinels: low=-999 means 'or below', high=999 means 'or higher'.
    """
    if not question:
        return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'°?[FC] or below', question, re.IGNORECASE)
        if m:
            return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'°?[FC] or higher', question, re.IGNORECASE)
        if m:
            return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'°?[FC]', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'°?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None


def hours_until(end_date_iso: Optional[str]) -> float:
    """Hours from now (UTC) until an ISO timestamp. Returns 999 if unparseable, 0 if past."""
    if not end_date_iso:
        return 999.0
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        delta = (end - datetime.now(timezone.utc)).total_seconds() / 3600
        return max(0.0, delta)
    except (ValueError, AttributeError):
        return 999.0


# ---------------------------------------------------------------------------
# Polymarket API
# ---------------------------------------------------------------------------

POLY_GAMMA = "https://gamma-api.polymarket.com"


def get_polymarket_event(city_slug: str, month: str, day: int, year: int) -> Optional[dict]:
    """Find a daily-high-temp market on Polymarket by URL slug."""
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r = requests.get(f"{POLY_GAMMA}/events", params={"slug": slug}, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
    except (requests.RequestException, json.JSONDecodeError):
        pass
    return None


def get_polymarket_market(market_id: str) -> Optional[dict]:
    """Fetch a single market by id. Returns parsed JSON or None on failure."""
    try:
        r = requests.get(f"{POLY_GAMMA}/markets/{market_id}", timeout=10)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, json.JSONDecodeError):
        return None


def parse_outcome_prices(market: dict) -> Tuple[float, float]:
    """
    Read (yes, no) from a market dict. Polymarket returns this as a JSON string
    inside the 'outcomePrices' field. Returns (0.5, 0.5) on failure.
    """
    raw = market.get("outcomePrices", "[0.5,0.5]")
    try:
        if isinstance(raw, str):
            prices = json.loads(raw)
        else:
            prices = raw
        yes = float(prices[0])
        no = float(prices[1]) if len(prices) > 1 else 1.0 - yes
        return yes, no
    except (json.JSONDecodeError, ValueError, TypeError, IndexError):
        return 0.5, 0.5
