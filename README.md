# Live Wind Widget

Course wind, NZL boat trim, and daggerboard cycling at a glance for SailGP
racing. A compact panel, designed to be shrunk into the corner of a screen and
left running. No login.

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Wind metrics

| Tile | Definition |
|---|---|
| **Course axis** | Bearing from the leeward gate midpoint (LG1/LG2) to the windward gate midpoint (WG1/WG2) |
| **Course TWD / TWS** | Averaged across WG1, WG2, LG1, LG2, M1 — then a simple moving average (default 10 min, adjustable 1–20) |
| **Top TWD / TWS** | WG1 + WG2 averaged, then a 20-second EMA |
| **Bottom TWD / TWS** | LG1 + LG2 averaged, then a 20-second EMA |

The two gate TWD tiles are shaded by their offset from the course TWD: white
within ±2°, pale red when left of the course wind, pale green when right,
saturating gently and clamping at ±15°. Text is always black.

### Range

The Course TWD and TWS tiles carry a small `+` / `−` pair on the right: how far
the wind has swung either side of the average now on screen, over the last 30
minutes. `293° +8 −16` means it reached 301° and 277°.

**A shift only counts if it held for 3 seconds.** A boat sailing past a mark
yanks one mark's wind for a second or two, and that must not widen the range.
The filter is a min/max hold: a rolling 3-second *minimum* only rises once
every sample in the window is high, so the maximum of that rolling minimum is
the highest level actually sustained. Short spikes always share their window
with normal samples and are erased. The low side is the mirror image.

Direction is done circularly — the deviation from the average is taken with
`signed_diff` first, so a range spanning 0°/360° behaves. If nothing was
sustained on a side, that side reads zero rather than an inverted range.

The 30-minute query costs ~14 s against ~9 s for the leading edge, so it runs
on its own 30 s cadence (`RANGE_REFRESH_S`) rather than on every poll; the
deviations are re-derived against the current average in between.

## Boat metrics (NZL)

Stacked under the wind tiles. The widget never grows wider.

| Tile | Channel | Definition |
|---|---|---|
| **Rud AVG** | `ANGLE_RUD_AVG_deg` | Rudder average, live |
| **Rud DIFF** | `ANGLE_RUD_DIFF_deg` | Rudder differential, **unsigned** — how far off zero, not which side |
| **Pitch · 5s** | `PITCH_deg` | Mean of the last 5 seconds |
| **Camber** | `ANGLE_CA1_deg` | Live camber, signed |
| **Camber %** | `ANGLE_CA1_deg` / `CAMBER_INPUT_deg` | Percentage of the set camber reached, 0–100 |

`ANGLE_CA1_deg` is the root camber actuator, the one that tracks the crew's
demand one-for-one and flips sign across an invert. CA4–CA6 fall away down the
wing because of twist, and CA2/CA3 are not populated. `CAMBER_INPUT_deg` is the
demanded magnitude, always positive.

### Alarms

Two tiles flash red. Both need their condition to hold for 3 seconds, and both
are gated on the boat actually sailing.

**Rud DIFF** — flashes when the differential sits within ±0.1° of zero. It
rests on zero through manoeuvres and whenever the boat is not pressed up, so it
only alarms once the boat has been **above 30 km/h in a straight line for 5
seconds** (`RATE_YAW_deg_s_1` smoothed over 3 s, under 3°/s).

**Camber %** — flashes when the reading *settles* below 100%, which is an
invert that was not held long enough. Sweeping through on the way to target is
not an alarm, so the value has to sit within ±3% for 3 seconds first. At target
(≥95%, allowing for actuator hunt) or at zero is fine — zero meaning either the
camber itself is within 1° of zero, or `BTN_WT_P/S_CAMBER_ZERO` was pressed in
the last 15 seconds. Only alarms above 15 km/h.

Measured over the 2026-07-26 session (2 h), the gates matter a great deal:

| Alarm | Ungated | Gated |
|---|---|---|
| Camber % | 45.9% of session | 1.9%, 9 episodes, median 10 s |
| Rud DIFF | 74.8% of session | 4.3%, 33 episodes, median 5 s |

Parked, camber sits eased at a few degrees against a 20° setting and the
differential rests on zero, so ungated both flash almost continuously whenever
the boat is not racing. Thresholds are named constants at the top of `app.py`.

Tiles read `—` whenever their channel has no data, exactly like the wind tiles.

## Board cycling

More than six daggerboard movements inside a rolling minute is a penalty, so
these two tiles count down what is left rather than up what has been used:
**Available Port** (red) and **Available Stbd** (green), from 6 down through 0
and into negative numbers once the limit is passed. Movements age out
individually as they fall out of the 60-second window, so the count climbs back
one at a time.

### Counting a movement

From `LENGTH_DB_H_P_mm` / `LENGTH_DB_H_S_mm` — daggerboard vertical height off
the linear position sensor, 10 Hz. The rule counts the board going up and down,
so height is the quantity it is actually about; reading it directly beats
inferring it from cant or rake, neither of which tracks the movement in all
conditions.

`wind_math.board_movements` walks the trace holding the extreme reached since
the last pivot, and treats a reversal of **200 mm** away from it as a new
movement. Two consequences, both wanted:

- **An up after an up is not a second movement** — only a reversal is. So a
  lift interrupted by a small dip and then continued counts once, while a
  genuine part-way lift and its return count twice.
- **Travel under 200 mm never registers**, so sensor noise and the board
  settling on its stop are ignored. Full travel is about 1900 mm and a parked
  board measures 0.5 mm peak to peak, so the threshold has an enormous margin.

The movement is recorded the instant the travel is confirmed, not when the leg
finishes — the count has to lead the sailors, not trail them.

### Validation

Replaying Abu Dhabi 2025-11-29 (race start 10:47:00Z, light air, TWS ~11 km/h),
the starboard board:

```
10:46:50   1 available
10:47:00   1 available   <- start
10:47:10   0 available
10:48:20   0 available
10:48:30  -2 available   <- over the limit
10:48:50  -1 available
```

First breach 90 s after the start, matching the penalty taken in that race —
and the counter sat at zero beforehand, which is the warning that would have
prevented it. Across a breezy session (2026-07-26, TWS 33 km/h) the same
threshold never goes below 3 available, so it does not cry wolf when foiling.

### Keeping it live

This is the one metric where lag costs something, so it is built differently
from the rest of the widget:

- **Its own 1-second fragment.** It redraws on a separate timer and never waits
  on the wind queries.
- **A pool of warm TimescaleDB connections.** Opening a connection per query
  costs a TLS handshake — measured at 3.3 s against 1.0 s on a reused one. The
  pool (`TSDBClient(pool_size=6)`) is shared by every query in the app. It is a
  pool rather than a single shared connection because `parallel()` runs several
  fetches at once and a psycopg2 connection is not thread-safe.
- **Incremental fetch.** A rolling 150-second trace is held in session state and
  extended with only the samples that arrived since the last one, typically
  about a second's worth. A tick costs ~0.28 s of query and ~6 ms of counting.
  If the buffer falls more than 20 s behind it is refilled outright rather than
  spliced across a hole.

The rolling window is anchored to the wall clock, not to the newest sample: a
movement made 61 s ago has aged out whether or not the feed is keeping up. Feed
lag shows in the status line instead, as `boards 0.4s`, which turns amber past
3 s — a stale counter is worse than no counter, so it has to be visible. That
figure assumes the machine clock is synced.

## Replay clock

In replay the status line shows the virtual clock to the second, in bold, and
ticks once a second so it can be lined up against other apps. Live mode still
shows the timestamp of the newest data instead.

## Modes

**Live** — queries TimescaleDB every 3 s with no caching or buffering, so the
leading edge is always current. Each poll re-reads the whole trailing window,
which is stateless and cannot drift or gap.

**Replay** — pick any date/time (UTC) and press play; it runs forward at normal
speed through the identical calculation path. Replay prefetches in 10-minute
blocks and slices locally, so playback stays smooth without hammering the API.
Each block carries a 31-minute warm-up behind it so the range has its full
window from the first frame, which makes the first load of a block slower.

## Running it as a floating widget

**Desktop launcher (recommended).** Opens the deployed app in a small
always-on-top window with no browser chrome:

```bash
pip install -r widget-requirements.txt
python widget.pyw          # or just double-click widget.pyw
```

`--frameless` drops the title bar too, at the cost of the close button.
Point it elsewhere with the `WIND_WIDGET_URL` environment variable.

`pywebview` is deliberately kept out of `requirements.txt` so Streamlit Cloud
doesn't try to install a GUI toolkit it has no use for.

**Browser alternative.** Chrome → ⋮ → Cast, save and share → Create shortcut…
→ tick "Open as window", then pin it with PowerToys' Always On Top
(`Win+Ctrl+T`).

Either way the live refresh keeps running: an always-on-top window is never
occluded, so its timers are never throttled the way a background tab's are.
Minimising it *will* throttle it.

Streamlit Cloud sleeps idle apps, so the first load after a quiet spell takes
~30 s to wake. Leaving the window open avoids this.

## Data source

TimescaleDB at `tsdb.sailgp.tech:5432`, table `sgp_telemetry`, over mutual TLS.
Mark data is at `level == 'mdss'`, boat data at `level == 'strm'`, both with the
name in the `boat` column.

| Channel | Level | Notes |
|---|---|---|
| `TWD_MDSS_deg` | mdss | Wind direction |
| `TWS_MDSS_km_h_1` | mdss | Wind speed, km/h |
| `LATITUDE_MDSS_deg` / `LONGITUDE_MDSS_deg` | mdss | Integers — divide by 10,000,000 |
| `ANGLE_RUD_AVG_deg` / `ANGLE_RUD_DIFF_deg` | strm | Rudder, 1 Hz |
| `PITCH_deg` | strm | 5 Hz |
| `ANGLE_CA1_deg` / `CAMBER_INPUT_deg` | strm | Camber and its demand |
| `BTN_WT_P_CAMBER_ZERO` / `BTN_WT_S_CAMBER_ZERO` | strm | Camber-zero buttons |
| `GPS_SOG_km_h_1` / `RATE_YAW_deg_s_1` | strm | Not displayed — they gate the alarms |
| `LENGTH_DB_H_P_mm` / `LENGTH_DB_H_S_mm` | strm | Daggerboard height, mm, 10 Hz — the cycling counter |

Marks: `WG1`/`WG2` (windward gate), `LG1`/`LG2` (leeward gate), `M1`,
`SL1`/`SL2` (committee boat / pin).

Boat channels decimate to 1 s with `last`, except the camber-zero buttons,
which use `max` so a press shorter than a second still registers.

### Two things worth knowing

**Marks sample at 5 Hz.** Queries downsample to 1 s server-side. TWD uses
`last` rather than `mean` — decimating is circular-safe, whereas a server-side
mean would break whenever the wind sits near 0°/360°. TWS is scalar, so `mean`
is fine.

**Marks get stowed between sessions.** When recovered onto a support boat they
sit metres apart, and a bearing across that is meaningless. Below 300 m of
gate-to-gate separation the widget shows "MARKS NOT DEPLOYED" and leaves the
gate tiles uncoloured rather than displaying noise.

## Circular maths

Wind direction is an angle, so it cannot be averaged or smoothed arithmetically
— 359° and 1° average to 180°, pointing the opposite way. Every TWD operation
in `wind_math.py` decomposes to sin/cos, aggregates those, and recombines with
`atan2`.

## Configuration

Credentials come from Streamlit Cloud secrets in production, falling back to a
local `.env`:

```
TSDB_HOST=tsdb.sailgp.tech
TSDB_PORT=5432
TSDB_DB=sailgp
TSDB_USER=sailgp_team_nzl
TSDB_PASSWORD=…
TSDB_SSLCERT=client.crt
TSDB_SSLKEY=client.key
```

The connection also needs the client certificate pair `client.crt`/`client.key`,
split from the team `.p12` bundle. On Streamlit Cloud they travel as PEM text in
the secrets vault instead (`python generate_tsdb_secrets.py` produces the block).
Certificate expires 2027-08-12.

`.env`, `client.crt` and `client.key` are gitignored. So is `Start Timing App/` — that folder is reference code
only and contains live credentials.

## Files

| File | Role |
|---|---|
| `app.py` | UI, layout, metrics, alarm rules, mode control, refresh timing |
| `wind_data.py` | TimescaleDB queries, pooled client, mark and boat channel constants |
| `wind_math.py` | Circular statistics, dwell filtering, board movement detection, geometry, colour ramp |
| `tsdb_config.py` | Credentials and TLS material (shared migration kit) |
| `tsdb_client.py` / `pivot_template.sql` | Query layer: pooled connections, pivots, latest-value lookups (shared migration kit) |

Each poll runs its queries concurrently (`wind_data.parallel`), so the cycle
costs about as long as its slowest query rather than their sum. Two fragments
drive the page on separate timers: wind and trim on the slow one, board cycling
on its own 1-second tick.
