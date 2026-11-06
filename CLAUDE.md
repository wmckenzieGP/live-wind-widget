# Live Wind Widget

Streamlit panel showing course wind, boat trim and board cycling. Read `README.md`
first: it explains every tile, alarm and query.

## Board cycling comes first

**Board cycling is always the priority, for speed and lowest delay. No feature may
ever slow it** (the user's rule, 2026-11-05). Follow "New features must not slow
it" in `README.md` before adding anything. In short:

- Do not touch the board query, its channels, its timer or its fragment.
- Add no new query, thread, timer or connection. A new channel joins `fetch_boat`.
- Keep that query the same size, and measure the change on the real query.

The two fragments share one thread, so a slower wind poll delays the board counter.
A background worker was tried and reverted (`631b979`): querying at the same time
slowed the board's own tick. Do not retry it without new evidence.

## Workflow

- Commit or push only when the user asks.
- **A push does not restart the widget.** Streamlit Cloud reloads `app.py` but keeps
  other modules in memory, so a change across modules crashes it until a reboot:
  `fa3b38c` took it down for about 5 minutes on 2026-11-05. After every push, use
  Manage app > Reboot app, then open https://blackfoilswindwidget.streamlit.app/ and
  check it draws. Do this only when nobody needs it on the water.
- Keep any local run to one database connection: set `TSDB_MAX_CONNECTIONS=1`.
- Test alarms by replaying a real fault. Live mode needs the boat to be streaming.
  Known faults are listed under "Stuck button" in `README.md`.
