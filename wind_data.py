"""InfluxDB access for SailGP course mark wind and positions.

All mark data lives in the `sailgp` bucket at level "mdss", with the mark name
carried in the `boat` tag. Marks sample at 5 Hz, so queries downsample to 1 s
server-side -- far finer than the 20 s EMA or 10 min SMA need, and ~10x less
data over the wire.

TWD is decimated with `last` rather than averaged. Taking the final sample of
each second is circular-safe, whereas a server-side mean would break whenever
the wind sits near 0/360. TWS is scalar, so `mean` is fine.
"""
from __future__ import annotations

import atexit
import warnings
from concurrent.futures import ThreadPoolExecutor

import arrow
import pandas as pd
from influxdb_client import InfluxDBClient
from influxdb_client.client.warnings import MissingPivotFunction

from config import BUCKET, ORG_ID, TOKEN, URL

warnings.simplefilter("ignore", MissingPivotFunction)

TOP_MARKS = ["WG1", "WG2"]        # windward gate
BOTTOM_MARKS = ["LG1", "LG2"]     # leeward gate
COURSE_WIND_MARKS = ["WG1", "WG2", "LG1", "LG2", "M1"]
ALL_MARKS = ["WG1", "WG2", "LG1", "LG2", "M1", "SL1", "SL2"]

TWD = "TWD_MDSS_deg"
TWS = "TWS_MDSS_km_h_1"
LAT = "LATITUDE_MDSS_deg"
LON = "LONGITUDE_MDSS_deg"
GPS_SCALE = 10_000_000

# Boat channels live at level "strm", not "mdss".
BOAT = "NZL"
RUD_AVG = "ANGLE_RUD_AVG_deg"
RUD_DIFF = "ANGLE_RUD_DIFF_deg"
PITCH = "PITCH_deg"
# CA1 is the root camber actuator, the one that tracks the crew's demand
# one-for-one. CA4-CA6 fall away down the wing because of twist, and CA2/CA3
# are not populated.
CAMBER = "ANGLE_CA1_deg"
CAMBER_TARGET = "CAMBER_INPUT_deg"          # magnitude, always positive
CAMBER_ZERO = ["BTN_WT_P_CAMBER_ZERO", "BTN_WT_S_CAMBER_ZERO"]
# Not displayed -- these gate the alarms, which only mean anything while the
# boat is actually sailing.
SOG = "GPS_SOG_km_h_1"
YAW_RATE = "RATE_YAW_deg_s_1"

BOAT_ANALOG = [RUD_AVG, RUD_DIFF, PITCH, CAMBER, CAMBER_TARGET, SOG, YAW_RATE]

# Daggerboard, for the cycling counter. Two axes, because which one the crew
# moves depends entirely on the conditions:
#
#   cant  -- raising and lowering the board. How the boards are cycled when
#            foiling, and swapped at manoeuvres.
#   rake  -- rotating the board fore and aft. In light air the boards stay
#            fully deployed (cant pinned around 95%) and the cycling happens
#            here instead: measured at Abu Dhabi 2025-11-29 the cant moved
#            0.75% across the whole race while the rake swung -4 to +5 deg
#            every few seconds.
#
# Watching only cant made the counter blind in exactly the conditions it
# matters most, so both are counted.
CANT_P = "CANT_POS_PCT_P_pct"
CANT_S = "CANT_POS_PCT_S_pct"
RAKE_P = "ANGLE_DB_RAKE_P_deg"
RAKE_S = "ANGLE_DB_RAKE_S_deg"
BOARD_CHANNELS = [CANT_P, CANT_S, RAKE_P, RAKE_S]

# Below this gate-to-gate separation the marks are stowed, not deployed,
# and the course axis is meaningless.
DEPLOYED_MIN_SEPARATION_M = 300.0


# One client, held open and reused. Building a fresh one per query costs a TLS
# handshake every time -- measured at 3.2 s against 1.3 s reusing a connection,
# which the board counter cannot afford.
_CLIENT: InfluxDBClient | None = None


def _client() -> InfluxDBClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = InfluxDBClient(url=URL, token=TOKEN, org=ORG_ID, timeout=60_000)
    return _CLIENT


def _reset_client() -> None:
    global _CLIENT
    try:
        if _CLIENT is not None:
            _CLIENT.close()
    except Exception:
        pass
    _CLIENT = None


# Close on the way out, while the modules the client needs are still alive --
# left to __del__ during interpreter teardown it raises on a half-torn-down
# module and prints an ignored-exception traceback.
atexit.register(_reset_client)


def _fmt(dt) -> str:
    return arrow.get(dt).to("UTC").format("YYYY-MM-DDTHH:mm:ss") + "Z"


def _fmt_ns(dt) -> str:
    """Sub-second precision, for resuming a stream exactly where it left off."""
    return arrow.get(dt).to("UTC").format("YYYY-MM-DDTHH:mm:ss.SSS") + "Z"


def _mark_filter(marks: list[str]) -> str:
    return " or ".join(f'r["boat"] == "{m}"' for m in marks)


def _query(flux: str) -> pd.DataFrame:
    try:
        df = _client().query_api().query_data_frame(org=ORG_ID, query=flux)
    except Exception:
        # A pooled connection can go stale between races. Rebuild once and
        # retry rather than surfacing a transport error as missing data.
        _reset_client()
        df = _client().query_api().query_data_frame(org=ORG_ID, query=flux)
    if isinstance(df, list):
        parts = [d for d in df if d is not None and not d.empty]
        df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return df if df is not None else pd.DataFrame()


def _naive_utc(s: pd.Series) -> pd.Series:
    s = pd.to_datetime(s, utc=True)
    return s.dt.tz_localize(None)


# ---------------------------------------------------------------------------
# Wind
# ---------------------------------------------------------------------------

def fetch_wind(start, end, marks: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (twd, tws) frames at 1 s resolution, indexed by UTC time with one
    column per mark. Missing marks simply don't appear as columns."""
    marks = marks or ALL_MARKS
    mf = _mark_filter(marks)
    flux = f"""
twd = from(bucket: "{BUCKET}")
  |> range(start: {_fmt(start)}, stop: {_fmt(end)})
  |> filter(fn: (r) => r["_measurement"] == "{TWD}")
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["level"] == "mdss")
  |> filter(fn: (r) => {mf})
  |> aggregateWindow(every: 1s, fn: last, createEmpty: false)

tws = from(bucket: "{BUCKET}")
  |> range(start: {_fmt(start)}, stop: {_fmt(end)})
  |> filter(fn: (r) => r["_measurement"] == "{TWS}")
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["level"] == "mdss")
  |> filter(fn: (r) => {mf})
  |> aggregateWindow(every: 1s, fn: mean, createEmpty: false)

union(tables: [twd, tws])
  |> keep(columns: ["_time", "_value", "boat", "_measurement"])
"""
    df = _query(flux)
    if df.empty or "_measurement" not in df.columns:
        return pd.DataFrame(), pd.DataFrame()

    df["_time"] = _naive_utc(df["_time"])

    def _pivot(measurement: str) -> pd.DataFrame:
        sub = df[df["_measurement"] == measurement]
        if sub.empty:
            return pd.DataFrame()
        out = sub.pivot_table(index="_time", columns="boat", values="_value")
        out.index.name = "time"
        out.columns.name = None
        return out.sort_index()

    return _pivot(TWD), _pivot(TWS)


# ---------------------------------------------------------------------------
# Boat
# ---------------------------------------------------------------------------

def fetch_boat(start, end, boat: str = BOAT) -> pd.DataFrame:
    """Return a 1 s frame of boat channels, one column per measurement.

    Analogue channels decimate with `last`; the camber-zero buttons use `max`
    so a press shorter than a second still shows up.
    """
    def _stream(measurements: list[str], fn: str) -> str:
        mf = " or ".join(f'r["_measurement"] == "{m}"' for m in measurements)
        return f"""from(bucket: "{BUCKET}")
  |> range(start: {_fmt(start)}, stop: {_fmt(end)})
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["level"] == "strm")
  |> filter(fn: (r) => r["boat"] == "{boat}")
  |> filter(fn: (r) => {mf})
  |> aggregateWindow(every: 1s, fn: {fn}, createEmpty: false)"""

    flux = f"""
analog = {_stream(BOAT_ANALOG, "last")}

buttons = {_stream(CAMBER_ZERO, "max")}

union(tables: [analog, buttons])
  |> keep(columns: ["_time", "_value", "_measurement"])
"""
    df = _query(flux)
    if df.empty or "_measurement" not in df.columns:
        return pd.DataFrame()

    df["_time"] = _naive_utc(df["_time"])
    out = df.pivot_table(index="_time", columns="_measurement", values="_value")
    out.index.name = "time"
    out.columns.name = None
    return out.sort_index()


# ---------------------------------------------------------------------------
# Daggerboard
#
# The cycling counter is the one metric that must not lag, so this is kept as
# narrow as it can be: four channels, no joins, decimated server-side, and
# normally fetched as a one-second delta onto a buffer the caller already holds.
# ---------------------------------------------------------------------------

BOARD_EVERY = "200ms"       # far finer than a board movement needs


def _board_flux(range_expr: str) -> str:
    mf = " or ".join(f'r["_measurement"] == "{m}"' for m in BOARD_CHANNELS)
    return f"""
from(bucket: "{BUCKET}")
  |> range({range_expr})
  |> filter(fn: (r) => {mf})
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["level"] == "strm")
  |> filter(fn: (r) => r["boat"] == "{BOAT}")
  |> aggregateWindow(every: {BOARD_EVERY}, fn: last, createEmpty: false)
  |> keep(columns: ["_time", "_value", "_measurement"])
"""


def _board_frame(flux: str) -> pd.DataFrame:
    df = _query(flux)
    if df.empty or "_measurement" not in df.columns:
        return pd.DataFrame()
    df["_time"] = _naive_utc(df["_time"])
    out = df.pivot_table(index="_time", columns="_measurement", values="_value")
    out.index.name = "time"
    out.columns.name = None
    return out.sort_index()


def fetch_board_recent(window: pd.Timedelta) -> pd.DataFrame:
    """Backfill: the trailing `window`, relative to server time so no client
    clock skew creeps in."""
    return _board_frame(_board_flux(f"start: -{int(window.total_seconds())}s"))


def fetch_board_since(after: pd.Timestamp) -> pd.DataFrame:
    """The steady-state call -- only what has arrived since the last sample."""
    return _board_frame(_board_flux(f"start: {_fmt_ns(after)}"))


def fetch_board_range(start, end) -> pd.DataFrame:
    """Explicit window, for replay."""
    return _board_frame(_board_flux(f"start: {_fmt(start)}, stop: {_fmt(end)}"))


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def fetch_positions(start, end, marks: list[str] | None = None) -> dict[str, tuple[float, float]]:
    """Latest known (lat, lon) per mark within the window."""
    marks = marks or ALL_MARKS
    flux = f"""
from(bucket: "{BUCKET}")
  |> range(start: {_fmt(start)}, stop: {_fmt(end)})
  |> filter(fn: (r) => r["_measurement"] == "{LAT}" or r["_measurement"] == "{LON}")
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["level"] == "mdss")
  |> filter(fn: (r) => {_mark_filter(marks)})
  |> last()
  |> keep(columns: ["boat", "_measurement", "_value"])
"""
    df = _query(flux)
    if df.empty or "_measurement" not in df.columns:
        return {}

    wide = df.pivot_table(index="boat", columns="_measurement", values="_value")
    if LAT not in wide.columns or LON not in wide.columns:
        return {}

    out = {}
    for mark, row in wide.iterrows():
        lat, lon = row.get(LAT), row.get(LON)
        if pd.isna(lat) or pd.isna(lon):
            continue
        out[str(mark)] = (float(lat) / GPS_SCALE, float(lon) / GPS_SCALE)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parallel(*calls):
    """Run zero-argument fetches concurrently and return their results in order.

    Each poll needs several independent queries; run serially they add up to
    more than the refresh interval. Nothing here touches Streamlit, so worker
    threads are safe.
    """
    if not calls:
        return []
    with ThreadPoolExecutor(max_workers=len(calls)) as ex:
        return [f.result() for f in [ex.submit(c) for c in calls]]


def latest_data_time():
    """Most recent mark timestamp in the bucket, or None. Used to tell whether
    anything is streaming live right now."""
    df = _query(f"""
from(bucket: "{BUCKET}")
  |> range(start: -30d)
  |> filter(fn: (r) => r["_measurement"] == "{TWD}")
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["level"] == "mdss")
  |> last()
  |> keep(columns: ["_time"])
""")
    if df.empty or "_time" not in df.columns:
        return None
    return _naive_utc(df["_time"]).max()
