# Live Wind Widget

Course wind at a glance for SailGP racing. Seven metrics in a compact panel,
designed to be shrunk into the corner of a screen and left running. No login.

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Metrics

| Tile | Definition |
|---|---|
| **Course axis** | Bearing from the leeward gate midpoint (LG1/LG2) to the windward gate midpoint (WG1/WG2) |
| **Course TWD / TWS** | Averaged across WG1, WG2, LG1, LG2, M1 — then a simple moving average (default 10 min, adjustable 1–20) |
| **Top TWD / TWS** | WG1 + WG2 averaged, then a 20-second EMA |
| **Bottom TWD / TWS** | LG1 + LG2 averaged, then a 20-second EMA |

The two gate TWD tiles are shaded by their offset from the course TWD: white
within ±2°, pale red when left of the course wind, pale green when right,
saturating gently and clamping at ±15°. Text is always black.

## Modes

**Live** — queries InfluxDB every 3 s with no caching or buffering, so the
leading edge is always current. Each poll re-reads the whole trailing window,
which is stateless and cannot drift or gap.

**Replay** — pick any date/time (UTC) and press play; it runs forward at normal
speed through the identical calculation path. Replay prefetches in 10-minute
blocks and slices locally, so playback stays smooth without hammering the API.

## Data source

InfluxDB at `data.sailgp.tech`, bucket `sailgp`, all mark data at `level ==
"mdss"` with the mark name in the `boat` tag.

| Measurement | Notes |
|---|---|
| `TWD_MDSS_deg` | Wind direction |
| `TWS_MDSS_km_h_1` | Wind speed, km/h |
| `LATITUDE_MDSS_deg` / `LONGITUDE_MDSS_deg` | Integers — divide by 10,000,000 |

Marks: `WG1`/`WG2` (windward gate), `LG1`/`LG2` (leeward gate), `M1`,
`SL1`/`SL2` (committee boat / pin).

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
ORG_ID=…
TOKEN=…
URL=https://data.sailgp.tech
```

`.env` is gitignored. So is `Start Timing App/` — that folder is reference code
only and contains live credentials.

## Files

| File | Role |
|---|---|
| `app.py` | UI, layout, mode control, refresh timing |
| `wind_data.py` | InfluxDB queries, mark constants |
| `wind_math.py` | Circular statistics, course geometry, colour ramp |
| `config.py` | Credentials |
