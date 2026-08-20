"""Reusable query layer for the SailGP TimescaleDB.

Copy into the app next to tsdb_config.py. The app then only needs to say which
channels it wants; this module builds the query that gets them out quickly.

    from tsdb_client import TSDBClient

    client = TSDBClient()
    df = client.fetch_pivot(
        boat="NZL",
        channels=list(RENAMING_DICT.keys()),
        start="2026-07-25T15:00:00Z",
        stop="2026-07-25T15:10:00Z",
        last_channels=WRAPPING_AND_DISCRETE,   # see pick_last_channels()
    )

Returns a pandas DataFrame: one row per bucket, one column per channel, named
exactly as the channel is. Columns that had no data at all are dropped, which
matches how InfluxDB behaved (missing measurement -> missing column) and keeps
downstream `if col in df.columns` guards working.
"""
import os

import pandas as pd
import psycopg2
import psycopg2.errors

import tsdb_config

_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "pivot_template.sql")


class TSDBTimeout(RuntimeError):
    """Server cancelled the query at its 5-minute statement_timeout.

    Raised rather than returning empty on purpose. SQLSTATE 57014 means
    "cancelled, no result" - reading it as an empty result set is how a
    timed-out query silently becomes a dashboard that says the boat never
    sailed.
    """


class TSDBClient:
    def __init__(self, level="strm", persistent=False, pool_size=0):
        """
        level:      default level for queries. 'strm' is the live race stream;
                    'mdss' is the race-hub passthrough (channels named
                    *_MDSS_* only, with the mark name carried in `boat`).
                    'log' and 'sim' are invisible to team logins - filtering
                    for them returns zero rows, not an error. Every method
                    takes a `level=` override, so one client can serve both.
        persistent: hold the connection open between queries. Each new
                    connection costs a full TLS handshake (~1s), which is
                    painful for a widget polling every second but irrelevant
                    for a dashboard fetching on refresh.

                    Use persistent=True for long-lived single-threaded apps,
                    and call close() on exit. Leave it False for Streamlit: a
                    rerun can abandon the object without closing it, leaking a
                    server slot per rerun.
        pool_size:  >0 keeps a thread-safe pool of warm connections. Use this,
                    NOT persistent=True, whenever queries run concurrently:
                    a psycopg2 connection is not thread-safe, and sharing one
                    across a thread pool interleaves protocol traffic and
                    corrupts results. Implies persistent behaviour.

                    Size it to what the server actually allows the role. Asking
                    for more does not get you more - the extras are refused at
                    connect time, and the first query to want one fails.
        """
        self.level = level
        self.pool_size = pool_size
        self.persistent = persistent or pool_size > 0
        self._conn = None
        self._pool = None
        self._slots = None
        if pool_size > 0:
            import threading
            from psycopg2.pool import ThreadedConnectionPool
            # Callers queue for a connection rather than being turned away.
            # psycopg2's pool raises PoolError the moment every connection is
            # checked out, so without this a second viewer, or a fetch that
            # overlaps the one before it, is an error rather than a short wait.
            self._slots = threading.BoundedSemaphore(pool_size)
            self._pool = ThreadedConnectionPool(
                1, pool_size, **tsdb_config.connection_kwargs())

    def _connect(self):
        return psycopg2.connect(**tsdb_config.connection_kwargs())

    def _acquire(self):
        """-> (connection, disposition) where disposition says how to release."""
        if self._pool is not None:
            return self._pool.getconn(), "pool"
        if not self.persistent:
            return self._connect(), "close"
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
        return self._conn, "keep"

    def _release(self, conn, disposition):
        if disposition == "pool":
            self._pool.putconn(conn)
        elif disposition == "close":
            conn.close()

    def close(self):
        """Close the persistent connection or drain the pool. No-op otherwise."""
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _run(self, sql, params):
        # Wait for a free slot before touching the pool, so a busy moment costs
        # a query its turn rather than failing it outright.
        if self._slots is not None:
            self._slots.acquire()
        try:
            return self._execute(sql, params)
        finally:
            if self._slots is not None:
                self._slots.release()

    def _execute(self, sql, params):
        conn, disposition = self._acquire()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
            if disposition != "close":
                # Release the transaction snapshot, or a reused connection
                # keeps reading the same frozen view of the data - which on a
                # live widget looks exactly like the feed having stalled.
                conn.commit()
            return rows, cols
        except psycopg2.errors.QueryCanceled as e:
            if disposition != "close":
                conn.rollback()
            raise TSDBTimeout(
                "Query cancelled by the server's 5-minute timeout. Narrow the "
                "time window, coarsen the bucket, or add channel filters."
            ) from e
        except psycopg2.OperationalError:
            # A reused connection can go stale between races. Drop it so the
            # next call reconnects rather than failing forever.
            if disposition == "keep":
                self.close()
            elif disposition == "pool":
                self._pool.putconn(conn, close=True)
                disposition = "done"
            raise
        finally:
            # psycopg2's `with conn` closes the transaction, NOT the socket.
            # An app that refetches on a timer leaks a server slot per tick
            # without this.
            if disposition in ("close", "pool"):
                self._release(conn, disposition)

    def fetch_pivot(self, boat, channels, start, stop,
                    bucket="0.5 seconds", last_channels=(), level=None,
                    aggs=None):
        """One row per time bucket, one column per channel.

        All channels come back in a SINGLE scan via aggregate FILTER - never
        one query per channel, never a self-join.

        last_channels: channels aggregated with last(value, time) instead of
        avg(value). Use it for anything that wraps at 0/360 (headings, TWD,
        TWA, COG) and anything discrete (race/leg numbers, state flags).
        Averaging a heading across the wrap yields 180 - a wrong number that
        looks entirely plausible on a dashboard.
        """
        channels = list(channels)
        if not channels:
            return pd.DataFrame()

        with open(_TEMPLATE_PATH) as f:
            template = f.read()

        sql = (template
               .replace("{bucket}", bucket)
               .replace("{selectList}", _select_list(channels, set(last_channels), aggs)))

        rows, cols = self._run(sql, {
            "boat": boat,
            "channels": channels,
            "level": level or self.level,
            "start": start,
            "stop": stop,
        })
        if not rows:
            return pd.DataFrame()
        return _numeric(pd.DataFrame(rows, columns=cols))

    def fetch_pivot_multi(self, boats, channels, start, stop,
                          bucket="1 second", last_channels=(), level=None,
                          aggs=None):
        """Same pivot, several boats/marks at once, with a `boat` column.

        For apps that compare the fleet, or read a set of course marks at
        `mdss` level (where the mark name - SL1, WG1, LG1 - is carried in the
        `boat` column). One scan for all of them; do NOT loop fetch_pivot per
        boat, which multiplies the work by the fleet size.
        """
        boats, channels = list(boats), list(channels)
        if not boats or not channels:
            return pd.DataFrame()

        select_list = _select_list(channels, set(last_channels), aggs)
        sql = f"""
SELECT
    time_bucket(%(bucket)s, time) AS "_time",
    boat,
{select_list}
FROM sgp_telemetry
WHERE boat = ANY(%(boats)s)
  AND level = %(level)s
  AND channel = ANY(%(channels)s)
  AND time >= %(start)s
  AND time <  %(stop)s
GROUP BY 1, 2
ORDER BY 1, 2;
"""
        rows, cols = self._run(sql, {
            "bucket": bucket, "boats": boats, "channels": channels,
            "level": level or self.level, "start": start, "stop": stop,
        })
        if not rows:
            return pd.DataFrame()
        return _numeric(pd.DataFrame(rows, columns=cols), skip=("_time", "boat"))

    def fetch_latest(self, pairs, level=None, start=None, stop=None):
        """Newest value for each (boat, channel) pair.

        Uses CROSS JOIN LATERAL with ORDER BY time DESC LIMIT 1 per pair -
        NOT max(time), which scans. This is the right call for "what is the
        wind doing right now" widgets.
        """
        pairs = list(pairs)
        if not pairs:
            return pd.DataFrame()
        # Bounding the search matters when "latest" must mean "latest in this
        # race": unbounded, a mark that stopped reporting yesterday still
        # returns yesterday's position, which looks current.
        window = ""
        if start is not None:
            window += " AND time >= %(start)s"
        if stop is not None:
            window += " AND time < %(stop)s"
        rows, cols = self._run(
            f"""
            SELECT bc.boat, bc.channel, t.time, t.value
            FROM unnest(%(boats)s::text[], %(channels)s::text[]) AS bc(boat, channel)
            CROSS JOIN LATERAL (
                SELECT time, value FROM sgp_telemetry
                WHERE boat = bc.boat AND channel = bc.channel
                  AND level = %(level)s{window}
                ORDER BY time DESC LIMIT 1
            ) t
            """,
            {"boats": [b for b, _ in pairs],
             "channels": [c for _, c in pairs],
             "level": level or self.level,
             "start": start, "stop": stop},
        )
        return pd.DataFrame(rows, columns=cols)

    def query(self, sql, params=None):
        """Escape hatch: run app-specific SQL, get a DataFrame back.

        For the cases the helpers above do not cover - a server-relative time
        range (`now() - %(lookback)s::interval`, which avoids trusting the
        client clock), a bespoke pivot, a window function. Everything the
        helpers do for you still applies: filter level/boat/channel with
        literals, keep the time range plain, and aggregate rather than dumping.
        """
        rows, cols = self._run(sql, params or {})
        return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame()

    def fetch_raw(self, boat, channel, start, stop, level=None):
        """Unaggregated trace of one channel. Keep the window short."""
        rows, cols = self._run(
            """SELECT time, value FROM sgp_telemetry
               WHERE boat = %(boat)s AND channel = %(channel)s
                 AND level = %(level)s
                 AND time >= %(start)s AND time < %(stop)s
               ORDER BY time""",
            {"boat": boat, "channel": channel, "level": level or self.level,
             "start": start, "stop": stop},
        )
        return pd.DataFrame(rows, columns=cols)

    def channel_counts(self, boat, day, channels, level=None):
        """Row count per channel over one UTC day. {channel: count}.

        GROUP BY, never SELECT DISTINCT - DISTINCT scans the whole table and
        times out; GROUP BY uses the segment index.
        """
        rows, _ = self._run(
            """SELECT channel, count(*) FROM sgp_telemetry
               WHERE boat = %(boat)s AND level = %(level)s
                 AND channel = ANY(%(channels)s)
                 AND time >= %(day)s::date AND time < %(day)s::date + 1
               GROUP BY channel""",
            {"boat": boat, "level": level or self.level,
             "channels": list(channels), "day": day},
        )
        return dict(rows)

    def boats_on(self, day, level=None):
        """Which boats have data on a UTC day. [(boat, rows), ...]"""
        rows, _ = self._run(
            """SELECT boat, count(*) FROM sgp_telemetry
               WHERE level = %(level)s
                 AND time >= %(day)s::date AND time < %(day)s::date + 1
               GROUP BY boat ORDER BY boat""",
            {"level": level or self.level, "day": day},
        )
        return rows

    def recent_days(self, limit=7):
        """Newest data via the chunk catalog - no table scan."""
        rows, _ = self._run(
            """SELECT range_start::date, range_end::date
               FROM timescaledb_information.chunks
               WHERE hypertable_name = 'sgp_telemetry'
               ORDER BY range_end DESC LIMIT %(limit)s""",
            {"limit": limit},
        )
        return rows


def _numeric(df, skip=("_time",)):
    """Coerce channel columns to numbers and drop the ones with no data.

    NULL-heavy columns arrive as object dtype, which silently skips later
    numeric work. All-null columns are dropped so the app sees a missing
    column, exactly as it did under InfluxDB.
    """
    for c in df.columns:
        if c not in skip:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(axis=1, how="all")


# Aggregates allowed in the pivot. Anything outside this set is rejected
# rather than interpolated into SQL.
_AGGS = {"avg", "last", "first", "max", "min", "sum", "count"}


def _agg_expr(agg):
    if agg not in _AGGS:
        raise ValueError(f"Unsupported aggregate '{agg}'. Use one of {sorted(_AGGS)}.")
    if agg in ("last", "first"):
        return f"{agg}(value, time)"
    return f"{agg}(value)"


def _select_list(channels, last_channels, aggs=None):
    """One aggregate per channel.

    aggs: optional {channel: 'avg'|'last'|'max'|...} for the cases a single
    rule cannot express - e.g. a momentary button that needs `max` so a press
    shorter than the bucket still registers, alongside analogue channels that
    want `last`.
    """
    aggs = aggs or {}
    lines = []
    for ch in channels:
        if "'" in ch or '"' in ch:
            raise ValueError(f"Illegal character in channel name: {ch}")
        agg = aggs.get(ch, "last" if ch in last_channels else "avg")
        lines.append(
            f"    {_agg_expr(agg)} FILTER (WHERE channel = '{ch}') AS \"{ch}\"")
    return ",\n".join(lines)


def pick_last_channels(channels):
    """Best-effort guess at which channels must not be averaged.

    Catches the common SailGP naming: headings/bearings that wrap at 0/360,
    and discrete race/state channels. ALWAYS eyeball the result - a channel
    wrongly averaged produces plausible wrong numbers, not obvious errors.
    """
    wrapping = ("HEADING", "TWD", "TWA", "AWA", "COG", "LEEWAY", "BEARING")
    discrete = ("RACE_NUM", "LEG_NUM", "STATE", "STATUS", "ACTIVE", "MODE", "FLAG")
    out = set()
    for ch in channels:
        up = ch.upper()
        if any(w in up for w in wrapping) or any(d in up for d in discrete):
            out.add(ch)
    return out
