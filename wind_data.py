"""TimescaleDB access for SailGP course mark wind and positions.

All mark data lives in `sgp_telemetry` at level "mdss", with the mark name
carried in the `boat` column. Marks sample at 5 Hz, so queries downsample to 1 s
server-side -- far finer than the 20 s EMA or 10 min SMA need, and ~10x less
data over the wire.

TWD is decimated with `last` rather than averaged. Taking the final sample of
each second is circular-safe, whereas a server-side mean would break whenever
the wind sits near 0/360. TWS is scalar, so `mean` is fine.
"""
from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor

import arrow
import pandas as pd
import psycopg2

from tsdb_client import TSDBClient, TSDBTimeout

# "The database is not answering right now", as opposed to "that query was
# wrong". A caller on a timer can hold its last frame through one of these and
# try again; anything else is a bug and should surface.
FEED_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError, TSDBTimeout)

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

# Daggerboard vertical height, from the linear position sensor. The rule counts
# the board going up and down, so height is the quantity it is actually about:
# it reads the movement directly instead of inferring it from cant or rake,
# neither of which tracks it in all conditions. Full travel is about 1900 mm.
HEIGHT_P = "LENGTH_DB_H_P_mm"
HEIGHT_S = "LENGTH_DB_H_S_mm"
BOARD_CHANNELS = [HEIGHT_P, HEIGHT_S]

# Below this gate-to-gate separation the marks are stowed, not deployed,
# and the course axis is meaningless.
DEPLOYED_MIN_SEPARATION_M = 300.0


# A pool of warm connections, not one shared connection. Each new connection
# costs a TLS handshake -- measured at 3.3 s against 1.0 s reusing one, which
# the board counter cannot afford -- but `parallel()` runs several fetches at
# once and a psycopg2 connection is not thread-safe. The pool gives both.
_CLIENT: TSDBClient | None = None
_POOL_SIZE = 6          # comfortably above the widest parallel() fan-out


def _client() -> TSDBClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = TSDBClient(level="mdss", pool_size=_POOL_SIZE)
    return _CLIENT


def reset_client() -> None:
    """Drain the pool and forget it, so the next call builds a fresh one.

    Worth doing after a connection failure as well as on the way out: the
    connections the pool is holding go back to the server now, rather than
    sitting there until an idle timeout reaps them. If the server refused us
    because we were already holding too many, hanging on to them is the one
    thing that guarantees the next attempt fails too.
    """
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
atexit.register(reset_client)


def _fmt(dt) -> str:
    return arrow.get(dt).to("UTC").format("YYYY-MM-DDTHH:mm:ss") + "Z"


def _fmt_ns(dt) -> str:
    """Sub-second precision, for resuming a stream exactly where it left off."""
    return arrow.get(dt).to("UTC").format("YYYY-MM-DDTHH:mm:ss.SSS") + "Z"


def _naive_utc(s: pd.Series) -> pd.Series:
    s = pd.to_datetime(s, utc=True)
    return s.dt.tz_localize(None)


def _indexed(df: pd.DataFrame) -> pd.DataFrame:
    """Bucket column -> naive UTC index named `time`, sorted."""
    if df.empty or "_time" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out["_time"] = _naive_utc(out["_time"])
    out = out.set_index("_time").sort_index()
    out.index.name = "time"
    return out


# ---------------------------------------------------------------------------
# Wind
# ---------------------------------------------------------------------------

def fetch_wind(start, end, marks: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (twd, tws) frames at 1 s resolution, indexed by UTC time with one
    column per mark. Missing marks simply don't appear as columns."""
    marks = marks or ALL_MARKS
    # Both channels for every mark in ONE scan. TWD takes the last sample of
    # each second (circular-safe); TWS averages.
    df = _client().fetch_pivot_multi(
        boats=marks,
        channels=[TWD, TWS],
        start=_fmt(start),
        stop=_fmt(end),
        bucket="1 second",
        level="mdss",
        aggs={TWD: "last", TWS: "avg"},
    )
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df["_time"] = _naive_utc(df["_time"])

    def _pivot(channel: str) -> pd.DataFrame:
        if channel not in df.columns:
            return pd.DataFrame()
        out = df.pivot_table(index="_time", columns="boat", values=channel)
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
    aggs = {c: "last" for c in BOAT_ANALOG}
    aggs.update({c: "max" for c in CAMBER_ZERO})

    df = _client().fetch_pivot(
        boat=boat,
        channels=BOAT_ANALOG + CAMBER_ZERO,
        start=_fmt(start),
        stop=_fmt(end),
        bucket="1 second",
        level="strm",
        aggs=aggs,
    )
    return _indexed(df)


# ---------------------------------------------------------------------------
# Daggerboard
#
# The cycling counter is the one metric that must not lag, so this is kept as
# narrow as it can be: four channels, no joins, decimated server-side, and
# normally fetched as a one-second delta onto a buffer the caller already holds.
# ---------------------------------------------------------------------------

BOARD_EVERY = "200 milliseconds"    # far finer than a board movement needs

# Written out rather than routed through fetch_pivot so the trailing-window
# variant can bound time with the SERVER's clock (now() - interval). Bounding
# it by the client clock would silently drop or duplicate samples whenever the
# laptop's time drifts from the database's.
_BOARD_SELECT = ",\n".join(
    f"    last(value, time) FILTER (WHERE channel = '{c}') AS \"{c}\""
    for c in BOARD_CHANNELS
)


def _board_frame(where: str, params: dict) -> pd.DataFrame:
    sql = f"""
SELECT
    time_bucket('{BOARD_EVERY}', time) AS "_time",
{_BOARD_SELECT}
FROM sgp_telemetry
WHERE boat = %(boat)s
  AND level = 'strm'
  AND channel = ANY(%(channels)s)
  AND {where}
GROUP BY 1
ORDER BY 1;
"""
    df = _client().query(sql, {"boat": BOAT, "channels": BOARD_CHANNELS, **params})
    return _indexed(df)


def fetch_board_recent(window: pd.Timedelta) -> pd.DataFrame:
    """Backfill: the trailing `window`, relative to server time so no client
    clock skew creeps in."""
    return _board_frame(
        "time >= now() - %(lookback)s::interval",
        {"lookback": f"{int(window.total_seconds())} seconds"},
    )


def fetch_board_since(after: pd.Timestamp) -> pd.DataFrame:
    """The steady-state call -- only what has arrived since the last sample."""
    return _board_frame("time >= %(after)s", {"after": _fmt_ns(after)})


def fetch_board_range(start, end) -> pd.DataFrame:
    """Explicit window, for replay."""
    return _board_frame(
        "time >= %(start)s AND time < %(stop)s",
        {"start": _fmt(start), "stop": _fmt(end)},
    )


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def fetch_positions(start, end, marks: list[str] | None = None) -> dict[str, tuple[float, float]]:
    """Latest known (lat, lon) per mark within the window."""
    marks = marks or ALL_MARKS
    # Latest sample per (mark, coordinate) WITHIN the window. The bound is the
    # point: unbounded, a mark that stopped reporting last event still returns
    # its old position, which reads as current.
    df = _client().fetch_latest(
        [(m, ch) for m in marks for ch in (LAT, LON)],
        level="mdss",
        start=_fmt(start),
        stop=_fmt(end),
    )
    if df.empty or "channel" not in df.columns:
        return {}

    wide = df.pivot_table(index="boat", columns="channel", values="value")
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
    """Most recent mark timestamp, or None. Used to tell whether anything is
    streaming live right now.

    One indexed lookup per mark (ORDER BY time DESC LIMIT 1), bounded to the
    last 30 days. `max(time)` would scan instead of using the index.
    """
    df = _client().query(
        """
        SELECT max(t.time) AS latest
        FROM unnest(%(marks)s::text[]) AS m(boat)
        CROSS JOIN LATERAL (
            SELECT time FROM sgp_telemetry
            WHERE boat = m.boat AND channel = %(channel)s AND level = 'mdss'
              AND time >= now() - INTERVAL '30 days'
            ORDER BY time DESC LIMIT 1
        ) t
        """,
        {"marks": ALL_MARKS, "channel": TWD},
    )
    if df.empty or df["latest"].isna().all():
        return None
    return _naive_utc(df["latest"]).max()
