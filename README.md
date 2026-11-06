# Live Wind Widget

Course wind, boat trim, and daggerboard cycling at a glance for SailGP racing,
for NZL or the shared hull B15. A compact panel, designed to be shrunk into the corner of a screen and
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

### Choosing the marks

A mark can stream bad TWD or TWS while the rest of the course is fine, and an
average has no way to notice — one bad mark just drags the number every tile is
read against. **Settings → Wind marks** has a pill per mark, grouped Top / Mid /
Bottom, all on by default. Deselect one and it drops out of the course average
and out of the gate above it; select it again and it is back on the next tick.
The status line carries e.g. `WG2 off` in amber for as long as anything is
deselected, so a held-back mark is never invisible on the frame it changed.

**Wind only.** A mark's position stays good when its wind does not, so the
course axis and both gate biases are unaffected — they are pure geometry and
keep using both marks of a pair whatever is selected. Deselecting a mark
therefore never costs a bias; it only stops that mark's wind feeding the
averages. Deselect *both* marks of a gate and that gate's TWD/TWS read dashes,
and its bias goes with them (a bias needs a wind direction to be read against).

The query still fetches all five course marks whatever is selected — the filter
is applied when the metrics are computed. Narrowing the query instead would
invalidate the replay block cache and the 30-minute range buffer every time a
pill is touched, so a mark switched back on would read dashes until the next
refetch. One mark's worth of rows is the cheaper side of that trade.

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

## Choosing the boat

The **NZL / B15** selector beside Live / Replay picks the boat behind the boat
tiles, board cycling and height traces, in both modes. The wind tiles come from
the course marks, so they do not change with it.

A new session opens on B15 through 2026-11-14 (`CAMP_LAST_DAY` in `app.py`) and
on NZL after that. To offer another boat, add it to `BOATS` in `wind_data.py`.

**B15 is a shared hull.** The login only sees it for the periods SailGP assigns
it to us. Outside them it returns zero rows with no error, so `no B15 data` in
the status line means "not ours right now", not "did not sail".

**B15's stream can arrive in bursts.** Its first dock test came as about 1.4 s
of samples, then a hole of about 3 s (up to 7 s), though it ran continuously once
our block started on 2026-11-05. If bursts return, the height traces break at
each hole and `boards X.Xs` can go amber. Both are the feed's real state, so
neither is smoothed over.

## Boat metrics

Stacked under the wind tiles. The widget never grows wider.

| Tile | Channel | Definition |
|---|---|---|
| **Rud AVG** | `ANGLE_RUD_AVG_deg` | Rudder average, live |
| **Rud DIFF** | `ANGLE_RUD_DIFF_deg` | Rudder differential, **unsigned** — how far off zero, not which side |
| **Pitch · 5s** | `PITCH_deg` | Mean of the last 5 seconds |
| **Clew** | `LENGTH_WING_CLEW_mm`, or `LENGTH_WING_CLEW_m` on B15 | Clew offset across the boat, mm. Starboard as read, port inverted, so **positive = to leeward**, matching `wing_clew_length_n` in the dashboard |
| **Camber** | `ANGLE_CA1_deg` | Live camber, signed |
| **Camber %** | `ANGLE_CA1_deg` / `CAMBER_INPUT_deg` | Percentage of the set camber reached, 0–100 |
| **Stuck Button** | `STUCK_BUTTON_GLOBAL_unk`, then the panel flags | `NONE`, or the stuck button's name, flashing. See [Stuck button](#stuck-button) |

`ANGLE_CA1_deg` is the root camber actuator, the one that tracks the crew's
demand one-for-one and flips sign across an invert. CA4–CA6 fall away down the
wing because of twist, and CA2/CA3 are not populated. `CAMBER_INPUT_deg` is the
demanded magnitude, always positive.

### Alarms

Two tiles flash red. Both need their condition to hold for 3 seconds, and both
are gated on the boat actually sailing.

**Rud DIFF** — flashes when the differential sits within ±0.1° of zero. It
rests on zero whenever the boat is not pressed up, so it only alarms **above 30
km/h**. Speed is the only gate.

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

### Stuck button

The third alarm comes from the boat, not from this widget. The boat flags a button
held for about 20 s, so that is the earliest the box can react, and the tile adds
up to one 3 s poll on top. It is not gated on speed, because a button stuck at the
dock is just as stuck. The flag stays up until it clears on the boat: on
2026-11-05 it cleared as `CMD_P_RESET_STUCK_unk` ran.

Each panel has a 1 Hz flag whose value is a **bitfield of the stuck buttons**, so
`CADI_P_STUCK_unk = 4` means bit 4 of the port foot unit, which is the wing
trimmer's board up. `stuck_buttons.py` holds the bit names and how they were found.
A bit with no name still flashes, showing its panel and bit number.

| Panel | Flag | Named bits |
|---|---|---|
| Foot buttons | `CADI_P/S_STUCK_unk` | Proved on a real fault, 2026-11-05 |
| Wheel | `WHEEL_P/S_STUCK_unk` | Proved on a real fault, 2026-09-20. Board up and down only, from what the boards did |
| Flight controller, grinder, wing trimmer | `G3K_FC/GD/WT_P/S_STUCK_unk` | Bits matched to `BTN_*` channels. These panels have not faulted in the data checked |
| Pilot | `PILOT_P/S_STUCK_unk` | As above |
| Strategist | `G3K_T_P/S_STUCK_unk` | None. No `BTN_*` channels exist, so it shows panel and bit |

Checked in replay against both faults: **B15 2026-11-05 21:39:38–21:40:22Z** (port
board up, wing trimmer foot button) and **NZL 2026-09-20 from 14:00:30Z** (stbd
board down, wheel, on and off for 18 minutes during racing).

Only the global flag is on every poll. The fourteen panel flags join that same
boat query when a button needs naming: once when the flag goes up, then every 30 s
in case a second button follows. Between those polls the tile holds the name, so
for the first 3 s of a fault it reads `BUTTON STUCK`.

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

- **Its own 500 ms fragment.** It redraws on a separate timer. Waiting for the next
  redraw is dead time between data landing and a sailor seeing it, so the tick is
  shorter than the wind's. A tick shorter than its own query only queues requests
  faster than they come back. It has its own timer but **not its own thread**, see
  below.
- **One network round trip per query.** The database answers these queries almost
  at once, so a query's time is the network. By default psycopg2 wraps each one in
  `BEGIN` and `COMMIT`, which are two more round trips, so the pool's connections
  run in autocommit. Measured on the same board query: 376 ms before, 116 ms after.
- **A pool of warm TimescaleDB connections.** Opening a connection per query
  costs a TLS handshake — measured at 3.3 s against 1.0 s on a reused one. The
  pool (`TSDB_MAX_CONNECTIONS`, 3 by default) is shared by every query in the app. It is a
  pool rather than a single shared connection because each viewer queries from its
  own thread and a psycopg2 connection is not thread-safe.
- **Incremental fetch.** A rolling 150-second trace is held in session state and
  extended with only the samples that arrived since the last one, typically
  half a second's worth. If the buffer falls more than 20 s behind it is refilled outright rather than
  spliced across a hole.

The rolling window is anchored to the wall clock, not to the newest sample: a
movement made 61 s ago has aged out whether or not the feed is keeping up. Feed
lag shows in the status line instead, as `boards 0.4s`, which turns amber past
3 s — a stale counter is worse than no counter, so it has to be visible. That
figure assumes the machine clock is synced.

### New features must not slow it

**Board cycling is always the priority, for speed and lowest delay. No feature may
slow it.** Two measured facts decide how a feature can be added:

- **Streamlit runs both fragments one at a time, on one thread per session.** A
  board tick due during a wind poll waits for that poll to finish, so anything that
  makes the wind poll longer delays the board counter. That thread has a fixed
  budget: with three round trips per query, 6 board ticks and 3 wind queries came to
  about 3.3 s of work in every 3 s, so it could never catch up. Run on one thread
  with the real queries and the same timers for 120 s, 167 of 240 board ticks ran
  with three round trips and 238 with one, 90 % of gaps were within 1.72 s and 0.74 s,
  and the wind poll held the thread for 1.27 s and 0.51 s.
- **A background thread is not a way out.** The server serves concurrent queries
  worse than sequential ones: the board's own tick went from ~0.33 s to ~1.12 s
  while a second thread queried. That line of work took board delays to 5 s and
  more, and was reverted on 2026-09-06 (`631b979`). The wind poll's own three queries
  run one after another for the same reason: 0.75 s, against 1.07 s side by side.

So for any new feature:

1. Do not touch the board query, its channels, its timer or its fragment.
2. Add no new query, thread, timer or connection. A new channel joins `fetch_boat`.
3. Keep that query the same size. Drop a channel nothing reads to pay for it, or
   fetch it only when it is needed, as the stuck-button panel flags are.
4. Measure it: alternate before and after on the real query for at least 60 rounds.
   One run proves nothing here, because query times are noisy.

The stuck button followed these rules, and dropping the unused `RATE_YAW_deg_s_1`
paid for its flag. Over 60 rounds the boat query was 384 ms before and 380 ms after
on one run, and 400 ms and 394 ms on another. A poll that also carries the panel
flags takes 20–40 ms longer, which is 2 polls in 24 through the 2026-11-05 fault.

### Height trace

Under the two counters sit two plots — **Port Height** and **Stbd Height** — each
showing the last **60 seconds** of raw board height — the same rolling minute the
movement counter above them is drawn from, so the trace covers the whole window
the count is made of. They exist to answer one question: how far behind the boat
is the data?

They read off the *same* session-state buffer as the counter, in the same 200 ms
buckets, on the same 500 ms tick. No extra query, no second connection, and
nothing the plots can say that the tiles above them would disagree with.

**The x-axis is pinned to the wall clock, not to the newest sample**, and that is
the whole point. The trace ends where the data ends; everything between there and
the right-hand edge is shaded, and that shaded width *is* the feed lag. Watch the
board move on the water, watch the trace move, and the difference between them is
the number you are after. Each plot also states it: `0.4s behind`, per channel, so
one sensor dropping out while the other reports is visible. Amber past 3 s.

Three more things the plots are deliberate about:

- **The line breaks across a hole in the feed** rather than drawing through it.
  A clean line across a dropout would read as data and would hide exactly what is
  being looked for. Anything over a 1-second gap starts a new segment.
- **The y-domain is fixed at 0–1900 mm** (nominal full travel) and **shared by both
  boards**, so a movement is the same size on either plot and the two can be read
  against each other. Autoscaling would be actively misleading here: a parked board
  measures 0.5 mm peak to peak, and rescaling to that turns sensor noise into a
  mountain range. The domain grows only if the data runs outside it.
- **The y-axis is inverted.** The sensor reads larger the further down the board
  goes, so plotted the usual way up a *lowering* board draws a *rising* line. The
  axis is flipped so the stroke slopes the way the board travels: a raise runs up
  the plot, a lower runs down it. Only the mapping to screen y is flipped — the
  samples, the per-channel readouts and the movement counter are all still real
  millimetres.

Drawn as inline SVG rather than through a charting library — a Vega or Matplotlib
figure inside a fragment that reruns every second would cost more render time than
the query it is meant to be measuring.

Both plots read the buffer, so they work unchanged in replay, showing the minute
behind the virtual clock.

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
| `LENGTH_WING_CLEW_mm` | strm | Clew offset, mm, 2 Hz |
| `LENGTH_WING_CLEW_m` | strm | B15's clew offset, metres, 2 Hz. Used only when the mm channel is absent |
| `TWA_SGP_deg` | strm | Not displayed — sets the clew's tack |
| `ANGLE_CA1_deg` / `CAMBER_INPUT_deg` | strm | Camber and its demand |
| `BTN_WT_P_CAMBER_ZERO` / `BTN_WT_S_CAMBER_ZERO` | strm | Camber-zero buttons |
| `GPS_SOG_km_h_1` | strm | Not displayed — gates the alarms |
| `STUCK_BUTTON_GLOBAL_unk` | strm | Any button stuck, 1 Hz. On every poll |
| `CADI_`, `WHEEL_`, `PILOT_`, `G3K_FC_`, `G3K_GD_`, `G3K_T_`, `G3K_WT_` + `P/S_STUCK_unk` | strm | Bitfield of the panel's stuck buttons, 1 Hz. Fetched only to name one |
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
| `stuck_buttons.py` | Stuck-button flags, the names of their bits, and when to ask for them |
| `tsdb_config.py` | Credentials and TLS material (shared migration kit) |
| `tsdb_client.py` / `pivot_template.sql` | Query layer: pooled connections, pivots, latest-value lookups (shared migration kit) |

Each poll runs its queries one after another. Two fragments drive the page on
separate timers: wind and trim on the slow one, board cycling on its own 500 ms
tick.
