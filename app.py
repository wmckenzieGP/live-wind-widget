"""Live Wind Widget -- SailGP course wind at a glance.

Course wind, boat trim and board cycling, designed to be shrunk into the corner
of a screen and left running. No login.

Live mode queries TimescaleDB every 3 s with no buffering, so the leading edge
is always current. Replay mode prefetches in blocks and advances a virtual
clock at normal speed, driving the identical calculation path.
"""
from __future__ import annotations

import datetime as dt
import time

import pandas as pd
import streamlit as st

import wind_data as wd
from wind_math import (bearing, board_movements, circular_ema,
                       circular_mean_columns, circular_sma, distance_m, held,
                       midpoint, project, shade, signed_diff,
                       signed_diff_series, sustained_extremes)

EMA_HALFLIFE = pd.Timedelta("20s")
LIVE_REFRESH_S = 3
REPLAY_TICK_S = 1
REPLAY_BLOCK = pd.Timedelta("10min")
# Most recent session with marks deployed (see README).
DEFAULT_REPLAY = dt.datetime(2026, 6, 21, 17, 0, 0)

# Wind range: how far back to look, and how long a shift must hold to count.
RANGE_WINDOW = pd.Timedelta("30min")
RANGE_DWELL = pd.Timedelta("3s")
# The 30 min query costs ~14 s against ~9 s for the leading edge, so it runs on
# its own slow cadence rather than on every poll. A half-hour statistic does
# not move meaningfully in 30 s.
RANGE_REFRESH_S = 30

# Boat.
BOAT_WINDOW = pd.Timedelta("60s")
PITCH_AVG = pd.Timedelta("5s")
RUD_DIFF_ZERO = 0.1               # |differential| at or below this reads zero
RUD_DIFF_DWELL = pd.Timedelta("3s")
# Alarms only mean something under way. Parked, camber sits eased at a few
# degrees against a 20 deg setting and the differential rests on zero, so
# ungated both would flash most of the time the boat is not racing.
SAILING_KMH = 15.0                # camber
RUD_ALARM_KMH = 30.0              # differential: straight-line sailing only
RUD_ALARM_YAW = 3.0               # deg/s; above this the boat is turning
YAW_SMOOTH = pd.Timedelta("3s")   # the raw IMU rate is far too noisy to gate on
RUD_ALARM_QUALIFY = pd.Timedelta("5s")
CAMBER_SETTLE = pd.Timedelta("3s")
CAMBER_SETTLE_BAND = 3.0          # percent it may wander and still count settled
# The actuators hunt a percent or two around the demand, so anything in the
# mid-90s has reached target; a tighter line just blinks the tile for a second
# at a time.
CAMBER_AT_TARGET = 95.0           # percent
CAMBER_ZERO_DEG = 1.0             # |camber| at or below this reads zero
CAMBER_ZERO_BTN_WINDOW = pd.Timedelta("15s")
CAMBER_MIN_TARGET = 1.0           # below this no camber is set, so no percentage

# Board cycling. More than six movements per board inside a rolling minute is a
# penalty, so this counter is fed back to the sailors live and is the one
# metric where lag actually costs something. It redraws on its own 1 s timer,
# independent of the wind tiles, and fetches only the delta each tick.
BOARD_LIMIT = 6
BOARD_WINDOW = pd.Timedelta("60s")
BOARD_BUFFER = pd.Timedelta("150s")   # trace held locally; > window, for warm-up
BOARD_TICK_S = 1
# Height change that counts as a movement. Full board travel is about 1900 mm
# and real raises and lowers run the whole way, so this only has to clear the
# sensor's own jitter.
BOARD_THRESH_MM = 200.0
# Past this the buffer is too stale to extend, so refill it outright.
BOARD_REFILL_GAP = pd.Timedelta("20s")

# A dropped connection must not take the page down -- an unhandled error in a
# fragment that reruns every second or three is a crash loop, and each restart
# strands the pool's connections server-side until they idle out, which is how
# one refused connection becomes a widget that can never reconnect. Hold the
# last frame, say so, and leave the server alone for a bit.
FEED_RETRY_S = 10

st.set_page_config(page_title="Course Wind", page_icon="🌬️",
                   layout="centered", initial_sidebar_state="collapsed")

st.markdown("""
<style>
  #MainMenu, header, footer {visibility: hidden;}
  .block-container {padding: 0.6rem 0.7rem 0.4rem !important; max-width: 460px;}
  .grid {display: grid; grid-template-columns: 1fr 1fr; gap: 6px;}
  .tile {background: #ffffff; border: 1px solid #e3e5e8; border-radius: 8px;
         padding: 8px 10px 7px; text-align: center;}
  .tile.wide {grid-column: 1 / -1;}
  /* A gate reads as one row -- TWD, bias, TWS -- so the three share the full
     width the pair used to have. */
  .trio {grid-column: 1 / -1; display: grid;
         grid-template-columns: 1fr 1fr 1fr; gap: 6px;}
  .lbl {font-size: 9.5px; font-weight: 700; letter-spacing: .09em;
        text-transform: uppercase; color: #6b7178; line-height: 1.3;}
  .row {position: relative;}
  .val {font-size: 30px; font-weight: 700; color: #000000; line-height: 1.15;
        font-variant-numeric: tabular-nums;}
  .val.sm {font-size: 15px; letter-spacing: .04em;}
  .unit {font-size: 13px; font-weight: 600; color: #000000; margin-left: 1px;}
  /* Range rides at the right edge, absolutely placed so the big number stays
     centred in the tile and the widget never needs to grow. */
  .rng {position: absolute; right: 0; top: 50%; transform: translateY(-50%);
        display: flex; flex-direction: column; text-align: right;
        font-size: 10px; font-weight: 700; color: #6b7178; line-height: 1.45;
        letter-spacing: .02em; font-variant-numeric: tabular-nums;}
  /* A footnote under the big number. The tile grows by its height and the rest
     of the row stretches with it, so the big numbers stay on one baseline. */
  .sub {font-size: 9.5px; font-weight: 700; letter-spacing: .05em;
        color: #6b7178; line-height: 1.25; padding-top: 1px;
        font-variant-numeric: tabular-nums;}
  .sect {grid-column: 1 / -1; font-size: 10px; font-weight: 700;
         letter-spacing: .16em; text-transform: uppercase; color: #6b7178;
         text-align: center; padding: 7px 0 0;}
  .tile.alarm {animation: alarm 1.1s ease-in-out infinite;}
  @keyframes alarm {
    0%, 100% {background: #ffffff; border-color: #e3e5e8;}
    50%      {background: #ffb4b4; border-color: #e88a8a;}
  }
  .status {font-size: 10px; color: #7b8189; text-align: center;
           padding: 5px 0 0; line-height: 1.5;}
  .clock {font-size: 12px; font-weight: 700; color: #3d4349;
          font-variant-numeric: tabular-nums; letter-spacing: .02em;}
  .warn {color: #a8500f; font-weight: 600;}
  div[data-testid="stExpander"] details {border: none;}
  div[data-testid="stExpander"] summary {font-size: 11px; color: #6b7178;}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def _lookback(sma: pd.Timedelta) -> pd.Timedelta:
    """History the averages need behind the leading edge."""
    return sma + pd.Timedelta("1min")


def _range_lookback() -> pd.Timedelta:
    """History the range needs -- the window itself plus the dwell that gets
    trimmed off its leading edge."""
    return RANGE_WINDOW + RANGE_DWELL + pd.Timedelta("1min")


def fetch_live(now: pd.Timestamp, sma: pd.Timedelta):
    """Direct query, no cache and no buffering -- minimum possible delay."""
    return wd.parallel(
        # Only the five course marks carry wind we use; SL1/SL2 would be a
        # third more data over the wire for nothing.
        lambda: wd.fetch_wind(now - _lookback(sma), now, wd.COURSE_WIND_MARKS),
        lambda: wd.fetch_boat(now - BOAT_WINDOW, now),
        lambda: wd.fetch_positions(now - pd.Timedelta("10min"), now),
    )


def fetch_live_range(now: pd.Timestamp):
    """The 30 min window behind the leading edge, course marks only."""
    return wd.fetch_wind(now - _range_lookback(), now, wd.COURSE_WIND_MARKS)


@st.cache_data(ttl=3600, show_spinner=False, max_entries=32)
def _replay_block(block_start: pd.Timestamp, sma_s: int):
    """One block of replay data plus the warm-up behind it. Cached so playback
    slices locally instead of re-querying every tick."""
    warm = max(_lookback(pd.Timedelta(seconds=sma_s)), _range_lookback())
    start, end = block_start - warm, block_start + REPLAY_BLOCK
    (twd, tws), boat, pos = wd.parallel(
        lambda: wd.fetch_wind(start, end, wd.COURSE_WIND_MARKS),
        lambda: wd.fetch_boat(start, end),
        lambda: wd.fetch_positions(start, end),
    )
    return twd, tws, boat, pos


@st.cache_data(ttl=3600, show_spinner=False, max_entries=32)
def _replay_board(block_start: pd.Timestamp) -> pd.DataFrame:
    """Board trace for one replay block, with enough warm-up behind it for the
    detector to have established a direction before the block starts."""
    return wd.fetch_board_range(block_start - BOARD_BUFFER,
                               block_start + REPLAY_BLOCK)


def fetch_replay(virtual_now: pd.Timestamp, sma: pd.Timedelta):
    block_start = virtual_now.floor(REPLAY_BLOCK)
    twd, tws, boat, pos = _replay_block(block_start, int(sma.total_seconds()))
    return (_upto(twd, virtual_now), _upto(tws, virtual_now),
            _upto(boat, virtual_now), pos)


def _upto(df: pd.DataFrame, when: pd.Timestamp) -> pd.DataFrame:
    return df if df.empty else df[df.index <= when]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute(twd: pd.DataFrame, tws: pd.DataFrame, pos: dict, sma: pd.Timedelta) -> dict:
    r = {"axis": float("nan"), "deployed": False, "separation": float("nan"),
         "course_twd": float("nan"), "course_tws": float("nan"),
         "top_bias": None, "bottom_bias": None,
         "missing": [], "latest": None}

    # Course axis: bottom gate midpoint -> top gate midpoint.
    lg = midpoint([pos[m] for m in wd.BOTTOM_MARKS if m in pos])
    wgp = midpoint([pos[m] for m in wd.TOP_MARKS if m in pos])
    if lg and wgp:
        r["separation"] = distance_m(lg, wgp)
        # Stowed marks sit metres apart; a bearing across that is meaningless.
        r["deployed"] = r["separation"] >= wd.DEPLOYED_MIN_SEPARATION_M
        if r["deployed"]:
            r["axis"] = bearing(lg, wgp)

    if twd.empty:
        return r
    r["latest"] = twd.index[-1]
    r["missing"] = [m for m in wd.COURSE_WIND_MARKS if m not in twd.columns]

    # Course wind: average the five marks, then a simple moving average.
    cols = [m for m in wd.COURSE_WIND_MARKS if m in twd.columns]
    if cols:
        r["course_twd"] = circular_sma(circular_mean_columns(twd[cols]), sma).iloc[-1]
    cols_s = [m for m in wd.COURSE_WIND_MARKS if m in tws.columns]
    if cols_s and not tws.empty:
        r["course_tws"] = tws[cols_s].mean(axis=1).rolling(sma).mean().iloc[-1]

    # Gates: average the pair, then a 20 s EMA.
    for key, marks in (("top", wd.TOP_MARKS), ("bottom", wd.BOTTOM_MARKS)):
        c = [m for m in marks if m in twd.columns]
        r[f"{key}_twd"] = (circular_ema(circular_mean_columns(twd[c]), EMA_HALFLIFE).iloc[-1]
                           if c else float("nan"))
        cs = [m for m in marks if m in tws.columns]
        r[f"{key}_tws"] = (tws[cs].mean(axis=1).ewm(halflife=EMA_HALFLIFE, times=tws.index)
                           .mean().iloc[-1] if cs and not tws.empty else float("nan"))
        r[f"{key}_offset"] = signed_diff(r[f"{key}_twd"], r["course_twd"])
        r[f"{key}_bias"] = gate_bias(pos, marks, r[f"{key}_twd"],
                                     upwind=(key == "top"),
                                     deployed=r["deployed"])
    return r


def gate_bias(pos: dict, marks: list[str], twd: float,
              upwind: bool, deployed: bool) -> dict | None:
    """Which end of a gate pays, and by how much.

    One mark of a gate sits further along the wind axis than the other. Round
    the near one and that offset is saved twice -- once not sailing down to the
    far mark, and again not sailing back up -- so the gain is twice the
    along-wind separation of the pair.

    Everything is read from the approaching boat: the top gate upwind, the
    bottom gate downwind. That puts `along` ahead of the boat at either end, so
    the favoured mark is simply the nearer one, and left and right mean what
    they mean on board rather than on a chart.

    `square` is the wind the gate was laid for -- the perpendicular to the line
    between the marks, taken upwind. Read against the gate TWD beside it, it
    says where the bias came from: the gain is whatever the wind has shifted
    off square.
    """
    if not deployed or twd is None or pd.isna(twd):
        return None
    pts = [pos[m] for m in marks if m in pos]
    if len(pts) != 2:
        return None
    mid = midpoint(pts)
    if mid is None:
        return None

    heading = twd if upwind else (twd + 180.0) % 360.0
    legs = [project(mid, p, heading) for p in pts]
    near = min(legs, key=lambda leg: leg[0])
    return {"side": "R" if near[1] >= 0 else "L",
            "gain": 2.0 * abs(legs[0][0] - legs[1][0]),
            "square": square_to(pts, twd)}


def square_to(pts: list[tuple[float, float]], twd: float) -> float:
    """The upwind normal to a gate line -- the TWD the gate is square to.

    A line has two perpendiculars, 180 deg apart. The one that matters is the
    one pointing up the course, so take whichever sits nearer the wind.
    """
    line = bearing(pts[0], pts[1])
    perps = ((line + 90.0) % 360.0, (line - 90.0) % 360.0)
    return min(perps, key=lambda p: abs(signed_diff(p, twd)))


def wind_range(twd: pd.DataFrame, tws: pd.DataFrame,
               ref_twd: float, ref_tws: float) -> dict:
    """How far the wind has swung either side of the displayed averages.

    Reported as signed deviations from the averages, so they read directly
    against the big numbers. Only excursions held for RANGE_DWELL count.
    """
    r = {k: float("nan") for k in ("twd_lo", "twd_hi", "tws_lo", "tws_hi")}

    def _window(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        cols = [m for m in wd.COURSE_WIND_MARKS if m in df.columns]
        if not cols:
            return pd.DataFrame()
        df = df[cols]
        return df[df.index >= df.index[-1] - (RANGE_WINDOW + RANGE_DWELL)]

    def _bracket(lo, hi):
        # The + and - bracket the average that is on screen. Nothing sustained
        # on a side means zero there, never a deviation with the wrong sign --
        # which is what the raw extremes give when the wind has been entirely
        # one side of the average, or never held anywhere for the dwell.
        if pd.isna(lo) or pd.isna(hi):
            return float("nan"), float("nan")
        return min(lo, 0.0), max(hi, 0.0)

    d = _window(twd)
    if not d.empty and not pd.isna(ref_twd):
        dev = signed_diff_series(circular_mean_columns(d), ref_twd)
        r["twd_lo"], r["twd_hi"] = _bracket(*sustained_extremes(dev, RANGE_DWELL))

    s = _window(tws)
    if not s.empty and not pd.isna(ref_tws):
        r["tws_lo"], r["tws_hi"] = _bracket(
            *sustained_extremes(s.mean(axis=1) - ref_tws, RANGE_DWELL))
    return r


def compute_boat(b: pd.DataFrame) -> dict:
    """Rudder, pitch and camber for the boat section.

    The differential is reported unsigned -- which side it favours is not the
    point, how far off zero it sits is.
    """
    r = {k: float("nan") for k in ("rud_avg", "rud_diff", "pitch",
                                   "camber", "camber_pct")}
    r["rud_alarm"] = False
    r["camber_alarm"] = False
    if b.empty:
        return r

    def _last(col):
        if col not in b.columns:
            return pd.Series(dtype=float)
        return b[col].dropna()

    avg = _last(wd.RUD_AVG)
    if not avg.empty:
        r["rud_avg"] = float(avg.iloc[-1])

    diff = _last(wd.RUD_DIFF).abs()
    if not diff.empty:
        r["rud_diff"] = float(diff.iloc[-1])
        # Zero differential only reads as a fault when the boat is settled in a
        # straight line at speed -- it rests on zero through manoeuvres and
        # whenever the boat is not pressed up.
        r["rud_alarm"] = (_straight_and_fast(b)
                          and held(diff <= RUD_DIFF_ZERO, RUD_DIFF_DWELL))

    pitch = _last(wd.PITCH)
    if not pitch.empty:
        window = pitch[pitch.index >= pitch.index[-1] - PITCH_AVG]
        r["pitch"] = float(window.mean())

    camber = _last(wd.CAMBER)
    if not camber.empty:
        r["camber"] = float(camber.iloc[-1])
    r.update(_camber_pct(b, camber))
    r["camber_alarm"] = r["camber_alarm"] and _under_way(b)
    return r


def _under_way(b: pd.DataFrame) -> bool:
    """Is the boat sailing rather than sitting about?"""
    if wd.SOG not in b.columns:
        return False        # cannot confirm, so do not cry wolf
    sog = b[wd.SOG].dropna()
    return not sog.empty and float(sog.iloc[-1]) >= SAILING_KMH


def _straight_and_fast(b: pd.DataFrame) -> bool:
    """True once the boat has held a straight line above the speed threshold
    for RUD_ALARM_QUALIFY."""
    if wd.SOG not in b.columns or wd.YAW_RATE not in b.columns:
        return False
    yaw = b[wd.YAW_RATE].abs().rolling(YAW_SMOOTH).mean()
    ok = (b[wd.SOG] > RUD_ALARM_KMH) & (yaw <= RUD_ALARM_YAW)
    return held(ok.fillna(False), RUD_ALARM_QUALIFY)


def _camber_pct(b: pd.DataFrame, camber: pd.Series) -> dict:
    """Percentage of the set camber actually reached, and whether to alarm.

    An invert that is not held long enough leaves the wing parked part-way --
    say 12 deg against a 24 deg setting. That is what this catches. Sweeping
    through on the way to target is not an alarm, so the reading has to settle
    first; deliberate camber-zero is not an alarm either.
    """
    out = {"camber_pct": float("nan"), "camber_alarm": False}
    if camber.empty or wd.CAMBER_TARGET not in b.columns:
        return out

    target = b[wd.CAMBER_TARGET].reindex(camber.index).ffill()
    valid = target >= CAMBER_MIN_TARGET
    if not bool(valid.iloc[-1]):
        return out          # no camber set, so no percentage to hit

    pct = (camber.abs() / target * 100.0).clip(upper=100.0)[valid]
    if pct.empty:
        return out
    out["camber_pct"] = float(pct.iloc[-1])

    settled = held((pct - pct.iloc[-1]).abs() <= CAMBER_SETTLE_BAND,
                   CAMBER_SETTLE)
    at_target = out["camber_pct"] >= CAMBER_AT_TARGET
    at_zero = abs(camber.iloc[-1]) <= CAMBER_ZERO_DEG or _zeroed(b, camber.index[-1])
    out["camber_alarm"] = settled and not at_target and not at_zero
    return out


def _zeroed(b: pd.DataFrame, now: pd.Timestamp) -> bool:
    """Did the crew press camber-zero in the last few seconds?"""
    recent = b[b.index >= now - CAMBER_ZERO_BTN_WINDOW]
    cols = [c for c in wd.CAMBER_ZERO if c in recent.columns]
    return bool(cols) and bool((recent[cols] > 0).to_numpy().any())


# ---------------------------------------------------------------------------
# Board cycling
# ---------------------------------------------------------------------------

def board_buffer(now: pd.Timestamp) -> pd.DataFrame:
    """Keep a rolling cant trace in session state, extended a second at a time.

    Re-reading the whole window every tick would cost a second of latency for
    data already in hand. Fetching only what has arrived since the last sample
    keeps a tick down to roughly a quarter of a second.
    """
    buf = ss.get("board_buf")
    if buf is None or buf.empty:
        buf = wd.fetch_board_recent(BOARD_BUFFER)
    else:
        gap = now - buf.index[-1]
        if gap > BOARD_REFILL_GAP or gap < pd.Timedelta(0):
            # Lost the thread (feed dropped, or the clock moved): start clean
            # rather than splice across a hole.
            buf = wd.fetch_board_recent(BOARD_BUFFER)
        else:
            new = wd.fetch_board_since(buf.index[-1])
            if not new.empty:
                buf = pd.concat([buf, new])
                buf = buf[~buf.index.duplicated(keep="last")].sort_index()
    if not buf.empty:
        buf = buf[buf.index >= buf.index[-1] - BOARD_BUFFER]
    ss["board_buf"] = buf
    return buf


def board_counts(buf: pd.DataFrame, now: pd.Timestamp) -> dict:
    """Movements left on each board inside the rolling minute.

    The window is anchored to the wall clock, not to the newest sample -- a
    movement made 61 s ago has aged out whether or not the feed is keeping up.
    Feed lag shows in `age` instead, where it can be seen.
    """
    r = {"port": None, "stbd": None, "age": None, "moves": {}}
    if buf is None or buf.empty:
        return r
    r["age"] = max((now - buf.index[-1]).total_seconds(), 0.0)
    cutoff = now - BOARD_WINDOW
    for side, col in (("port", wd.HEIGHT_P), ("stbd", wd.HEIGHT_S)):
        if col not in buf.columns:
            continue
        v = buf[col].dropna()
        if v.empty:
            continue
        used = [t for t in board_movements(v, BOARD_THRESH_MM) if t > cutoff]
        r["moves"][side] = len(used)
        r[side] = BOARD_LIMIT - len(used)
    return r


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

DASH = "&mdash;"


def _deg(v) -> str:
    return DASH if v is None or pd.isna(v) else f"{round(v) % 360:03.0f}&deg;"


def _kmh(v) -> str:
    return DASH if v is None or pd.isna(v) else f"{v:.1f}<span class='unit'>km/h</span>"


def _num(v, dp: int = 1, unit: str = "&deg;") -> str:
    return DASH if v is None or pd.isna(v) else f"{v:.{dp}f}{unit}"


def _bias(v: dict | None) -> str:
    """Favoured end and the metres it is worth, e.g. "R 90"."""
    return DASH if not v else f"{v['side']}&nbsp;{v['gain']:.0f}"


def _square(v: dict | None) -> str:
    """The wind the gate is square to, as a footnote under the bias."""
    if not v or pd.isna(v.get("square")):
        return ""
    return f"<div class='sub'>SQ {_deg(v['square'])}</div>"


def _signed(v, dp: int = 0, unit: str = "&deg;") -> str:
    """A deviation, always carrying its sign."""
    if v is None or pd.isna(v):
        return DASH
    return f"{'+' if v >= 0 else '&minus;'}{abs(v):.{dp}f}{unit}"


def _range(hi, lo, dp: int = 0, unit: str = "&deg;") -> str:
    return (f"<div class='rng'><span>{_signed(hi, dp, unit)}</span>"
            f"<span>{_signed(lo, dp, unit)}</span></div>")


def _tile(label: str, value: str, bg: str = "#ffffff", wide=False, small=False,
          extra: str = "", alarm=False, sub: str = "") -> str:
    # An alarm tile animates its own background, so leave the inline colour off
    # and let the keyframes own it.
    style = "" if alarm else f" style='background:{bg}'"
    classes = "tile" + (" wide" if wide else "") + (" alarm" if alarm else "")
    return (f"<div class='{classes}'{style}>"
            f"<div class='lbl'>{label}</div>"
            f"<div class='row'><div class='val{' sm' if small else ''}'>{value}</div>"
            f"{extra}</div>{sub}</div>")


def render(r: dict, rng: dict, b: dict, sma_min: int) -> str:
    if r["deployed"]:
        axis = _tile("Course Axis", _deg(r["axis"]), wide=True)
    else:
        axis = _tile("Course Axis", "MARKS NOT DEPLOYED", wide=True, small=True)

    # Without a valid axis there is no meaningful reference, so gates stay white.
    def gbg(key):
        return shade(r.get(f"{key}_offset")) if r["deployed"] else "#ffffff"

    def gate(key: str, label: str) -> str:
        return ("<div class='trio'>"
                + _tile(f"{label} TWD", _deg(r.get(f"{key}_twd")), gbg(key))
                + _tile(f"{label} Bias", _bias(r.get(f"{key}_bias")),
                        sub=_square(r.get(f"{key}_bias")))
                + _tile(f"{label} TWS", _kmh(r.get(f"{key}_tws")))
                + "</div>")

    return (
        "<div class='grid'>"
        + axis
        + _tile(f"Course TWD &middot; {sma_min}m", _deg(r["course_twd"]),
                extra=_range(rng.get("twd_hi"), rng.get("twd_lo"), 0, "&deg;"))
        + _tile(f"Course TWS &middot; {sma_min}m", _kmh(r["course_tws"]),
                extra=_range(rng.get("tws_hi"), rng.get("tws_lo"), 1, ""))
        + gate("top", "Top")
        + gate("bottom", "Bottom")
        + f"<div class='sect'>{wd.BOAT}</div>"
        # ---- boat tiles ----
        + _tile("Rud AVG", _num(b.get("rud_avg")))
        + _tile("Rud DIFF", _num(b.get("rud_diff"), 2), alarm=b.get("rud_alarm"))
        + _tile("Pitch &middot; 5s", _num(b.get("pitch")), wide=True)
        + _tile("Camber", _num(b.get("camber")))
        + _tile("Camber %", _num(b.get("camber_pct"), 0, "%"),
                alarm=b.get("camber_alarm"))
        + "</div>"
    )


PORT_BG, STBD_BG = "#fbe4e4", "#e2f2e6"


def render_boards(c: dict) -> str:
    """Board cycling -- its own grid so it can redraw on a faster timer."""
    def cell(label, side, bg):
        n = c.get(side)
        val = DASH if n is None else f"{n:d}"
        return _tile(label, val, bg)

    return ("<div class='grid'>"
            + "<div class='sect'>Board Cycling</div>"
            + cell("Available Port", "port", PORT_BG)
            + cell("Available Stbd", "stbd", STBD_BG)
            + "</div>")


def board_age_bit(c: dict) -> str:
    """How far behind the counter is. Shown because a stale counter is worse
    than no counter -- if the feed stalls this has to be visible."""
    age = c.get("age")
    if age is None:
        return "<span class='warn'>no boards</span>"
    cls = "warn" if age > 3 else ""
    return f"<span class='{cls}'>boards {age:.1f}s</span>"


def status_bits(r: dict, tag: str, vnow: pd.Timestamp | None,
                replay: bool) -> list[str]:
    bits = []
    if replay:
        # The replay clock itself, to the second, so it can be lined up
        # against other apps.
        bits.append(f"<span class='clock'>{vnow:%H:%M:%S}Z</span>")
        if r["latest"] is None:
            bits.append("<span class='warn'>no data</span>")
    elif r["latest"] is not None:
        bits.append(f"{r['latest']:%H:%M:%S}Z")
    elif vnow is not None:
        bits.append(f"{vnow:%H:%M:%S}Z &mdash; <span class='warn'>no data</span>")
    if not pd.isna(r["separation"]):
        bits.append(f"gate {r['separation']:.0f} m")
    if r["missing"]:
        bits.append(f"<span class='warn'>missing {', '.join(r['missing'])}</span>")
    bits.append(tag)
    return bits


def status_line(bits: list[str]) -> str:
    return f"<div class='status'>{' &nbsp;&middot;&nbsp; '.join(bits)}</div>"


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

ss = st.session_state
ss.setdefault("mode", "Live")
ss.setdefault("playing", False)
ss.setdefault("elapsed", 0.0)     # seconds of playback consumed
ss.setdefault("wall", None)       # wall-clock time when play was pressed
ss.setdefault("range_at", None)   # when the 30 min range window last refreshed
ss.setdefault("range_twd", None)
ss.setdefault("range_tws", None)
ss.setdefault("board_buf", None)  # rolling daggerboard trace
ss.setdefault("wind_bits", [])    # status from the slow fragment, drawn by the fast one
ss.setdefault("wind_html", None)  # last good frame, put back up if the feed drops
ss.setdefault("board_html", None)
ss.setdefault("board_bit", None)
ss.setdefault("feed_at", None)    # when the feed last failed, for the back-off
ss.setdefault("feed_msg", None)   # and what it said, shown under the status line

mode = st.segmented_control("Mode", ["Live", "Replay"], default=ss["mode"],
                            key="mode_ctl", label_visibility="collapsed")
if mode and mode != ss["mode"]:
    ss.update(mode=mode, playing=False, elapsed=0.0, wall=None)
mode = ss["mode"]

replay_start = None
if mode == "Replay":
    c1, c2, c3 = st.columns([1.5, 1.2, 0.9], vertical_alignment="bottom")
    d = c1.date_input("Date", DEFAULT_REPLAY.date(), key="r_date",
                      format="YYYY-MM-DD", label_visibility="collapsed")
    t = c2.time_input("Time (UTC)", DEFAULT_REPLAY.time(), key="r_time",
                      step=60, label_visibility="collapsed")
    replay_start = pd.Timestamp(dt.datetime.combine(d, t))

    if replay_start != ss.get("anchor"):        # new start point -> rewind
        ss.update(anchor=replay_start, elapsed=0.0, wall=None, playing=False)

    if c3.button("Pause" if ss["playing"] else "Play", use_container_width=True):
        if ss["playing"]:
            ss["elapsed"] += time.time() - ss["wall"]
            ss.update(playing=False, wall=None)
        else:
            ss.update(playing=True, wall=time.time())
        st.rerun()

with st.expander("Settings"):
    sma_min = st.slider("Course average (minutes)", 1, 20, 10, key="sma")
sma = pd.Timedelta(minutes=sma_min)


# ---------------------------------------------------------------------------
# Tiles -- redrawn on a timer without re-running the whole page
# ---------------------------------------------------------------------------

def virtual_now() -> pd.Timestamp:
    elapsed = ss["elapsed"] + (time.time() - ss["wall"] if ss["playing"] else 0.0)
    return replay_start + pd.Timedelta(seconds=elapsed)


def feed_ready() -> bool:
    """False while the back-off after a connection failure is still running."""
    at = ss["feed_at"]
    return at is None or time.time() - at >= FEED_RETRY_S


def feed_failed(exc: Exception) -> None:
    """Start the back-off, let go of the pool, and keep what the server said.

    The message is shown in the widget as well as logged. It names the host and
    the role, which is not something to put on a page indefinitely -- but a
    deployment whose logs you cannot reach is worse, and this is the line that
    says whether the server refused us, timed out, or never heard of us.
    """
    wd.reset_client()
    ss["feed_at"] = time.time()
    ss["feed_msg"] = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
    print(f"[feed] {ss['feed_msg']}", flush=True)


def feed_bits() -> list[str]:
    return [] if ss["feed_at"] is None else ["<span class='warn'>connection lost</span>"]


def feed_diag() -> None:
    """What the database actually said, at the top of the widget.

    A red box rather than a line of small print, and st.code so it can be
    copied out in one click: a message nobody can find is the same as no
    message, which is how this started.
    """
    if ss["feed_msg"]:
        st.error("Database connection failed")
        st.code(ss["feed_msg"], language=None)


def blank_wind() -> str:
    """An all-dashes frame, for a feed that has never once answered."""
    return render(compute(pd.DataFrame(), pd.DataFrame(), {}, sma), {},
                  compute_boat(pd.DataFrame()), sma_min)


def draw():
    """Wind and boat trim. Slower cadence -- none of it changes in a second."""
    if feed_ready():
        try:
            if mode == "Live":
                now = pd.Timestamp.utcnow().tz_localize(None)
                (twd, tws), boat, pos = fetch_live(now, sma)
                r = compute(twd, tws, pos, sma)
                rng = live_range(now, r)
                vnow, tag, replay = now, "live", False
            else:
                vnow = virtual_now()
                twd, tws, boat, pos = fetch_replay(vnow, sma)
                r = compute(twd, tws, pos, sma)
                # Replay already holds the whole window in the cached block, so
                # there is nothing to throttle.
                rng = wind_range(twd, tws, r["course_twd"], r["course_tws"])
                tag = "replay &#9654;" if ss["playing"] else "replay &#9208;"
                replay = True

            # The status line is drawn by the board fragment so it sits at the
            # bottom and, in replay, ticks once a second with the clock.
            ss["wind_bits"] = status_bits(r, tag, vnow, replay)
            ss["wind_html"] = render(r, rng, compute_boat(boat), sma_min)
            ss["feed_at"] = ss["feed_msg"] = None
        except wd.FEED_ERRORS as e:
            feed_failed(e)

    # Called here rather than at the top so it reflects this tick, and still
    # lands above the tiles -- Streamlit draws in call order.
    feed_diag()

    # Whatever happened, put a frame up -- the last good one if there is one.
    # The wind bits keep the timestamp it was current at, so a held frame is
    # never mistaken for a live one.
    st.markdown(ss["wind_html"] or blank_wind(), unsafe_allow_html=True)


def draw_boards():
    """Board cycling. Its own timer, so a slow wind query cannot hold it up,
    and only the newest second of cant is fetched each tick."""
    if feed_ready():
        try:
            if mode == "Live":
                now = pd.Timestamp.utcnow().tz_localize(None)
                buf = board_buffer(now)
            else:
                now = virtual_now()
                buf = _upto(_replay_board(now.floor(REPLAY_BLOCK)), now)

            counts = board_counts(buf, now)
            ss["board_html"] = render_boards(counts)
            ss["board_bit"] = board_age_bit(counts)
            ss["feed_at"] = ss["feed_msg"] = None
        except wd.FEED_ERRORS as e:
            feed_failed(e)

    bits = list(ss["wind_bits"])
    bits.append(ss["board_bit"] or board_age_bit({}))
    st.markdown((ss["board_html"] or render_boards({}))
                + status_line(bits + feed_bits()), unsafe_allow_html=True)


def live_range(now: pd.Timestamp, r: dict) -> dict:
    """Refetch the 30 min window on its own slow cadence; between refreshes
    re-derive the deviations from the frame already in hand, so the + and -
    stay honest as the average underneath them drifts."""
    due = (ss["range_at"] is None
           or now - ss["range_at"] >= pd.Timedelta(seconds=RANGE_REFRESH_S))
    if due:
        ss["range_twd"], ss["range_tws"] = fetch_live_range(now)
        ss["range_at"] = now
    if ss["range_twd"] is None:
        return {}
    return wind_range(ss["range_twd"], ss["range_tws"],
                      r["course_twd"], r["course_tws"])


if mode == "Live":
    every, board_every = LIVE_REFRESH_S, BOARD_TICK_S
elif ss["playing"]:
    every = board_every = REPLAY_TICK_S
else:
    every = board_every = None   # paused: hold the frame, no polling

# Two timers. The board counter is the one metric where a second matters, so
# it redraws on its own and never waits on the wind queries.
st.fragment(run_every=every)(draw)()
st.fragment(run_every=board_every)(draw_boards)()
