"""Circular statistics, course geometry, and the colour ramp.

Wind direction is an angle, so it cannot be averaged or smoothed arithmetically
-- 359 deg and 1 deg average to 180 deg, which points the opposite way. Every
TWD operation here decomposes to sin/cos components, aggregates those, and
recombines with atan2. TWS is a plain scalar and needs none of this.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EARTH_R = 6_371_000.0

# Colour ramp: white at the deadband, pale tint at full offset. Never dark --
# black text sits on top at every step.
DEADBAND_DEG = 2.0
FULL_SCALE_DEG = 15.0
_L_MAX, _L_MIN = 98.0, 85.0   # lightness %, near-white -> still-pale
_HUE_LEFT, _SAT_LEFT = 5.0, 60.0      # red side
_HUE_RIGHT, _SAT_RIGHT = 145.0, 42.0  # green side


# ---------------------------------------------------------------------------
# Circular statistics
# ---------------------------------------------------------------------------

def _to_xy(deg):
    r = np.radians(np.asarray(deg, dtype=float))
    return np.cos(r), np.sin(r)


def _to_deg(x, y):
    deg = np.degrees(np.arctan2(y, x)) % 360.0
    # A hair below 0 wraps to a hair below 360, which then rounds up to a
    # literal 360.0 and would render as "360 deg". Snap it back to 0.
    return np.where(np.abs(deg - 360.0) < 1e-9, 0.0, deg)


def circular_mean(degrees) -> float:
    """Vector mean of a set of bearings. NaN if nothing valid."""
    a = np.asarray(degrees, dtype=float)
    a = a[~np.isnan(a)]
    if a.size == 0:
        return float("nan")
    x, y = _to_xy(a)
    if np.hypot(x.mean(), y.mean()) < 1e-12:
        return float("nan")  # perfectly opposed, mean is undefined
    return float(_to_deg(x.mean(), y.mean()))


def circular_mean_columns(df: pd.DataFrame) -> pd.Series:
    """Row-wise circular mean across columns, skipping NaN."""
    if df.empty:
        return pd.Series(dtype=float)
    x, y = _to_xy(df.to_numpy())
    mask = ~np.isnan(df.to_numpy())
    n = mask.sum(axis=1)
    with np.errstate(invalid="ignore"):
        xm = np.where(mask, x, 0.0).sum(axis=1) / n
        ym = np.where(mask, y, 0.0).sum(axis=1) / n
    out = _to_deg(xm, ym)
    return pd.Series(np.where(n > 0, out, np.nan), index=df.index)


def circular_sma(s: pd.Series, window: pd.Timedelta) -> pd.Series:
    """Simple moving average over a time window, circular-safe."""
    if s.empty:
        return s
    x, y = _to_xy(s)
    xs = pd.Series(x, index=s.index).rolling(window).mean()
    ys = pd.Series(y, index=s.index).rolling(window).mean()
    return pd.Series(_to_deg(xs.to_numpy(), ys.to_numpy()), index=s.index)


def circular_ema(s: pd.Series, halflife: pd.Timedelta) -> pd.Series:
    """Time-weighted exponential moving average, circular-safe.

    Uses `times=` so irregular sampling is handled correctly.
    """
    if s.empty:
        return s
    x, y = _to_xy(s)
    idx = s.index
    xs = pd.Series(x, index=idx).ewm(halflife=halflife, times=idx).mean()
    ys = pd.Series(y, index=idx).ewm(halflife=halflife, times=idx).mean()
    return pd.Series(_to_deg(xs.to_numpy(), ys.to_numpy()), index=idx)


def signed_diff(a: float, b: float) -> float:
    """Shortest signed angle from b to a, in -180..180.

    Negative = a is anticlockwise (left) of b. Positive = clockwise (right).
    """
    if a is None or b is None or np.isnan(a) or np.isnan(b):
        return float("nan")
    return float((a - b + 180.0) % 360.0 - 180.0)


# ---------------------------------------------------------------------------
# Course geometry
# ---------------------------------------------------------------------------

def midpoint(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Mean of lat/lon pairs. Fine at course scale (marks are ~km apart)."""
    pts = [p for p in points if p and not any(np.isnan(v) for v in p)]
    if not pts:
        return None
    return (float(np.mean([p[0] for p in pts])), float(np.mean([p[1] for p in pts])))


def bearing(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Initial great-circle bearing from p1 to p2, in 0..360."""
    lat1, lon1 = np.radians(p1)
    lat2, lon2 = np.radians(p2)
    dlon = lon2 - lon1
    y = np.sin(dlon) * np.cos(lat2)
    x = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(dlon)
    return float(np.degrees(np.arctan2(y, x)) % 360.0)


def distance_m(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Haversine distance in metres."""
    lat1, lon1 = np.radians(p1)
    lat2, lon2 = np.radians(p2)
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return float(2 * EARTH_R * np.arcsin(np.sqrt(a)))


# ---------------------------------------------------------------------------
# Colour ramp
# ---------------------------------------------------------------------------

def _hsl_to_hex(h: float, s: float, l: float) -> str:
    s, l = s / 100.0, l / 100.0
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = l - c / 2
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][int(h // 60) % 6]
    return "#{:02x}{:02x}{:02x}".format(*(round((v + m) * 255) for v in (r, g, b)))


def shade(diff_deg: float) -> str:
    """Background colour for a gate TWD tile, given its signed offset from the
    course axis wind. White inside the deadband; pale red to the left, pale
    green to the right, saturating gently and stopping while still light."""
    if diff_deg is None or np.isnan(diff_deg):
        return "#ffffff"
    mag = abs(diff_deg)
    if mag <= DEADBAND_DEG:
        return "#ffffff"
    t = min((mag - DEADBAND_DEG) / (FULL_SCALE_DEG - DEADBAND_DEG), 1.0)
    lightness = _L_MAX - (_L_MAX - _L_MIN) * t
    hue, sat = (_HUE_RIGHT, _SAT_RIGHT) if diff_deg > 0 else (_HUE_LEFT, _SAT_LEFT)
    return _hsl_to_hex(hue, sat, lightness)
