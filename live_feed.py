"""Live queries on a worker thread, so the script thread never waits on them.

Streamlit runs one script thread per session and takes fragment reruns one at a
time. Anything the wind poll does on that thread is time the board counter
cannot tick, and the counter is the one number in this widget that has to track
real time. Measured before this module existed, the poll held the thread for
~1.3 s out of every 2 s and the counter's nominal 500 ms tick came out at a
median of 1.69 s -- 2.80 s before the queries were unpicked.

So the queries live here instead. The worker keeps the newest frames in a single
slot; a fragment takes whatever is in the slot and renders it, which costs
microseconds. What it stores is deliberately *raw*: every per-session derivation
-- which marks are selected, the averaging window, the deviations -- still
happens on the script thread, so two viewers with different settings still see
their own numbers off one shared fetch.

Nothing in here touches Streamlit. A worker thread has no ScriptRunContext, so
it must never read or write session state, and it does not.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pandas as pd

import wind_data as wd


@dataclass(frozen=True)
class Snapshot:
    """One consistent set of frames, and the instant they were read for."""
    at: pd.Timestamp                  # the "now" the queries were bounded by
    twd: pd.DataFrame
    tws: pd.DataFrame
    boat: pd.DataFrame
    pos: dict
    range_twd: pd.DataFrame | None    # 30 min window, or None before it lands
    range_tws: pd.DataFrame | None
    landed: float                     # time.time() when it was published


def splice(old: pd.DataFrame, new: pd.DataFrame,
           keep: pd.Timedelta) -> pd.DataFrame:
    """Extend a rolling window with a newly fetched tail.

    The tail is refetched from the buffer's last sample, so the two overlap by a
    bucket: that bucket was partial when it was first read and is complete now,
    and `keep="last"` is what takes the finished version. A mark that appeared
    or dropped out mid-window aligns to NaN in the rows it was absent for,
    exactly as a full refetch would have left it.
    """
    if new is not None and not new.empty:
        old = pd.concat([old, new])
        old = old[~old.index.duplicated(keep="last")].sort_index()
    if old.empty:
        return old
    return old[old.index >= old.index[-1] - keep]


class LiveFeed:
    """A single worker polling the leading edge, shared by every viewer.

    One worker rather than one per session: the frames it fetches are the same
    for everyone, and this role's connection budget is small enough that a
    thread per viewer would be the thing that broke it.
    """

    def __init__(self, interval_s: float, lookback: pd.Timedelta,
                 boat_window: pd.Timedelta, pos_window: pd.Timedelta,
                 pos_refresh_s: float, refill_gap: pd.Timedelta,
                 range_lookback: pd.Timedelta, range_refresh_s: float,
                 range_refill_gap: pd.Timedelta, retry_s: float):
        self._interval_s = interval_s
        self._lookback = lookback
        self._boat_window = boat_window
        self._pos_window = pos_window
        self._pos_refresh_s = pos_refresh_s
        self._refill_gap = refill_gap
        self._range_lookback = range_lookback
        self._range_refresh_s = range_refresh_s
        self._range_refill_gap = range_refill_gap
        self._retry_s = retry_s

        self._lock = threading.Lock()
        self._snap: Snapshot | None = None
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Touched only by the worker, so they need no lock.
        self._twd: pd.DataFrame | None = None
        self._tws: pd.DataFrame | None = None
        self._pos: dict = {}
        self._pos_at: pd.Timestamp | None = None
        self._range_twd: pd.DataFrame | None = None
        self._range_tws: pd.DataFrame | None = None
        self._range_at: pd.Timestamp | None = None

    # -- what the fragments call -------------------------------------------

    def start(self) -> "LiveFeed":
        """Idempotent: the cached resource is built once, but say so anyway."""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="live-feed",
                                        daemon=True)
        self._thread.start()
        return self

    def snapshot(self) -> Snapshot | None:
        """The newest frames, or None until the first fetch lands."""
        with self._lock:
            return self._snap

    def error(self) -> str | None:
        """What the database last said, cleared by the next good fetch."""
        with self._lock:
            return self._error

    def age(self) -> float | None:
        """Seconds since the last good fetch landed."""
        with self._lock:
            return None if self._snap is None else time.time() - self._snap.landed

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()

    # -- the worker ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            began = time.time()
            try:
                snap = self._fetch()
            except wd.FEED_ERRORS as e:
                self._failed(e)
                self._stop.wait(self._retry_s)
                continue
            except Exception as e:                       # noqa: BLE001
                # A worker that dies leaves the widget frozen on its last frame
                # with nothing to say why, which is the one outcome worse than a
                # visible error. Nothing gets out of this loop alive.
                self._failed(e)
                self._stop.wait(self._retry_s)
                continue
            with self._lock:
                self._snap = snap
                self._error = None
            wait = self._interval_s - (time.time() - began)
            if wait > 0:
                self._stop.wait(wait)

    def _failed(self, exc: Exception) -> None:
        msg = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
        with self._lock:
            self._error = msg
        # Let go of the pool: a refused or half-open connection is not worth
        # keeping, and the next pass builds a fresh one.
        try:
            wd.reset_client()
        except Exception:                                # noqa: BLE001
            pass
        print(f"[feed] {msg}", flush=True)

    def _fetch(self) -> Snapshot:
        """One pass. Every query here is one the board counter pays for.

        The worker is off the script thread, but it is not free: while it is
        talking to the database the board's own query contends with it and slows
        down. Measured, a worker doing three full-window queries every 2 s took
        the board's fetch from 0.30 s to 0.77 s. So each part is kept to the
        least it can be -- the leading edge extended by a tail, positions on a
        slow cadence, and only the boat window read whole.
        """
        now = pd.Timestamp.utcnow().tz_localize(None)
        twd, tws = self._edge(now)
        # The boat window is 60 s and reads in ~0.36 s, near enough all of which
        # is the round trip. There is nothing to save by tailing it.
        boat = wd.fetch_boat(now - self._boat_window, now)
        pos = self._positions(now)
        rtwd, rtws = self._range(now)
        return Snapshot(at=now, twd=twd, tws=tws, boat=boat, pos=pos,
                        range_twd=rtwd, range_tws=rtws, landed=time.time())

    def _edge(self, now: pd.Timestamp):
        """The leading-edge wind, extended a tail at a time.

        The window is the widest any viewer's average can ask for, so the slider
        can move without a refetch: a rolling mean only looks back its own
        length, so a session set to 1 minute reads the same number off this
        frame as it would off a 2 minute one. Re-reading all of it every pass
        cost ~0.74 s against ~0.33 s for the tail.
        """
        twd, tws = self._twd, self._tws
        if twd is None or twd.empty:
            twd, tws = self._full_edge(now)
        else:
            last = twd.index[-1]
            gap = now - last
            if gap > self._refill_gap or gap < pd.Timedelta(0):
                twd, tws = self._full_edge(now)
            else:
                new_twd, new_tws = wd.fetch_wind(last, now,
                                                 wd.COURSE_WIND_MARKS)
                twd = splice(twd, new_twd, self._lookback)
                tws = splice(tws, new_tws, self._lookback)
        self._twd, self._tws = twd, tws
        return twd, tws

    def _full_edge(self, now: pd.Timestamp):
        return wd.fetch_wind(now - self._lookback, now, wd.COURSE_WIND_MARKS)

    def _positions(self, now: pd.Timestamp) -> dict:
        """Where the marks are, on a slow cadence.

        They are anchored buoys. Between the times they are physically moved
        they do not go anywhere, and the query costs the same round trip as a
        useful one. The bias itself still moves at the full refresh rate -- it
        is geometry against the wind, and the wind is what changes.
        """
        due = (self._pos_at is None
               or (now - self._pos_at)
               >= pd.Timedelta(seconds=self._pos_refresh_s))
        if due:
            found = wd.fetch_positions(now - self._pos_window, now)
            # Keep the last known fix for a mark that briefly stops reporting;
            # a gate that vanishes for one pass should not blank the axis.
            if found:
                self._pos = {**self._pos, **found}
            self._pos_at = now
        return self._pos

    def _range(self, now: pd.Timestamp):
        """The 30 min window, on its own slow cadence and extended by a tail.

        Held here rather than in session state because the worker cannot touch
        session state -- and because the raw window is the same for every
        viewer. The per-session part, the deviations against each viewer's own
        average, is derived on the script thread from what this returns.
        """
        due = (self._range_at is None
               or now - self._range_at
               >= pd.Timedelta(seconds=self._range_refresh_s))
        if not due:
            return self._range_twd, self._range_tws

        twd, tws = self._range_twd, self._range_tws
        if twd is None or twd.empty:
            twd, tws = self._full_range(now)
        else:
            last = twd.index[-1]
            gap = now - last
            if gap > self._range_refill_gap or gap < pd.Timedelta(0):
                # Feed dropped, or the clock moved under us: start clean rather
                # than weld the two sides of a hole together.
                twd, tws = self._full_range(now)
            else:
                new_twd, new_tws = wd.fetch_wind(last, now,
                                                 wd.COURSE_WIND_MARKS)
                twd = splice(twd, new_twd, self._range_lookback)
                tws = splice(tws, new_tws, self._range_lookback)

        self._range_twd, self._range_tws = twd, tws
        self._range_at = now
        return twd, tws

    def _full_range(self, now: pd.Timestamp):
        return wd.fetch_wind(now - self._range_lookback, now,
                             wd.COURSE_WIND_MARKS)
