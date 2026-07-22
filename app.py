"""Live Wind Widget -- SailGP course wind at a glance.

Seven metrics, designed to be shrunk into the corner of a screen and left
running. No login.

Live mode queries InfluxDB every 3 s with no buffering, so the leading edge is
always current. Replay mode prefetches in blocks and advances a virtual clock
at normal speed, driving the identical calculation path.
"""
from __future__ import annotations

import datetime as dt
import time

import pandas as pd
import streamlit as st

import wind_data as wd
from wind_math import (bearing, circular_ema, circular_mean_columns,
                       circular_sma, distance_m, midpoint, shade, signed_diff)

EMA_HALFLIFE = pd.Timedelta("20s")
LIVE_REFRESH_S = 3
REPLAY_TICK_S = 1
REPLAY_BLOCK = pd.Timedelta("10min")
# Most recent session with marks deployed (see README).
DEFAULT_REPLAY = dt.datetime(2026, 6, 21, 17, 0, 0)

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
  .lbl {font-size: 9.5px; font-weight: 700; letter-spacing: .09em;
        text-transform: uppercase; color: #6b7178; line-height: 1.3;}
  .val {font-size: 30px; font-weight: 700; color: #000000; line-height: 1.15;
        font-variant-numeric: tabular-nums;}
  .val.sm {font-size: 15px; letter-spacing: .04em;}
  .unit {font-size: 13px; font-weight: 600; color: #000000; margin-left: 1px;}
  .status {font-size: 10px; color: #7b8189; text-align: center;
           padding: 5px 0 0; line-height: 1.5;}
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


def fetch_live(now: pd.Timestamp, sma: pd.Timedelta):
    """Direct query, no cache and no buffering -- minimum possible delay."""
    twd, tws = wd.fetch_wind(now - _lookback(sma), now)
    pos = wd.fetch_positions(now - pd.Timedelta("10min"), now)
    return twd, tws, pos


@st.cache_data(ttl=3600, show_spinner=False, max_entries=32)
def _replay_block(block_start: pd.Timestamp, sma_s: int):
    """One block of replay data plus the SMA warm-up behind it. Cached so
    playback slices locally instead of re-querying every tick."""
    sma = pd.Timedelta(seconds=sma_s)
    start, end = block_start - _lookback(sma), block_start + REPLAY_BLOCK
    twd, tws = wd.fetch_wind(start, end)
    pos = wd.fetch_positions(start, end)
    return twd, tws, pos


def fetch_replay(virtual_now: pd.Timestamp, sma: pd.Timedelta):
    block_start = virtual_now.floor(REPLAY_BLOCK)
    twd, tws, pos = _replay_block(block_start, int(sma.total_seconds()))
    if not twd.empty:
        twd = twd[twd.index <= virtual_now]
    if not tws.empty:
        tws = tws[tws.index <= virtual_now]
    return twd, tws, pos


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute(twd: pd.DataFrame, tws: pd.DataFrame, pos: dict, sma: pd.Timedelta) -> dict:
    r = {"axis": float("nan"), "deployed": False, "separation": float("nan"),
         "course_twd": float("nan"), "course_tws": float("nan"),
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
    return r


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _deg(v) -> str:
    return "&mdash;" if v is None or pd.isna(v) else f"{round(v) % 360:03.0f}&deg;"


def _kmh(v) -> str:
    return "&mdash;" if v is None or pd.isna(v) else f"{v:.1f}<span class='unit'>km/h</span>"


def _tile(label: str, value: str, bg: str = "#ffffff", wide=False, small=False) -> str:
    return (f"<div class='tile{' wide' if wide else ''}' style='background:{bg}'>"
            f"<div class='lbl'>{label}</div>"
            f"<div class='val{' sm' if small else ''}'>{value}</div></div>")


def render(r: dict, sma_min: int) -> str:
    if r["deployed"]:
        axis = _tile("Course Axis", _deg(r["axis"]), wide=True)
    else:
        axis = _tile("Course Axis", "MARKS NOT DEPLOYED", wide=True, small=True)

    # Without a valid axis there is no meaningful reference, so gates stay white.
    def gbg(key):
        return shade(r.get(f"{key}_offset")) if r["deployed"] else "#ffffff"

    return (
        "<div class='grid'>"
        + axis
        + _tile(f"Course TWD &middot; {sma_min}m", _deg(r["course_twd"]))
        + _tile(f"Course TWS &middot; {sma_min}m", _kmh(r["course_tws"]))
        + _tile("Top TWD", _deg(r.get("top_twd")), gbg("top"))
        + _tile("Top TWS", _kmh(r.get("top_tws")))
        + _tile("Bottom TWD", _deg(r.get("bottom_twd")), gbg("bottom"))
        + _tile("Bottom TWS", _kmh(r.get("bottom_tws")))
        + "</div>"
    )


def status(r: dict, mode: str, virtual_now: pd.Timestamp | None) -> str:
    bits = []
    if r["latest"] is not None:
        bits.append(f"{r['latest']:%H:%M:%S}Z")
    elif virtual_now is not None:
        bits.append(f"{virtual_now:%H:%M:%S}Z &mdash; <span class='warn'>no data</span>")
    if not pd.isna(r["separation"]):
        bits.append(f"gate {r['separation']:.0f} m")
    if r["missing"]:
        bits.append(f"<span class='warn'>missing {', '.join(r['missing'])}</span>")
    bits.append(mode)
    return f"<div class='status'>{' &nbsp;&middot;&nbsp; '.join(bits)}</div>"


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

ss = st.session_state
ss.setdefault("mode", "Live")
ss.setdefault("playing", False)
ss.setdefault("elapsed", 0.0)     # seconds of playback consumed
ss.setdefault("wall", None)       # wall-clock time when play was pressed

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

def draw():
    if mode == "Live":
        now = pd.Timestamp.utcnow().tz_localize(None)
        twd, tws, pos = fetch_live(now, sma)
        vnow, tag = now, "live"
    else:
        elapsed = ss["elapsed"] + (time.time() - ss["wall"] if ss["playing"] else 0.0)
        vnow = replay_start + pd.Timedelta(seconds=elapsed)
        twd, tws, pos = fetch_replay(vnow, sma)
        tag = "replay &#9654;" if ss["playing"] else "replay &#9208;"

    r = compute(twd, tws, pos, sma)
    st.markdown(render(r, sma_min) + status(r, tag, vnow), unsafe_allow_html=True)


if mode == "Live":
    every = LIVE_REFRESH_S
elif ss["playing"]:
    every = REPLAY_TICK_S
else:
    every = None      # paused: hold the frame, no polling
st.fragment(run_every=every)(draw)()
