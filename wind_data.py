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

import warnings

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

# Below this gate-to-gate separation the marks are stowed, not deployed,
# and the course axis is meaningless.
DEPLOYED_MIN_SEPARATION_M = 300.0


def _client() -> InfluxDBClient:
    return InfluxDBClient(url=URL, token=TOKEN, org=ORG_ID, timeout=60_000)


def _fmt(dt) -> str:
    return arrow.get(dt).to("UTC").format("YYYY-MM-DDTHH:mm:ss") + "Z"


def _mark_filter(marks: list[str]) -> str:
    return " or ".join(f'r["boat"] == "{m}"' for m in marks)


def _query(flux: str) -> pd.DataFrame:
    with _client() as c:
        df = c.query_api().query_data_frame(org=ORG_ID, query=flux)
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
