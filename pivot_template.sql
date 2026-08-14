-- Wide pivot of sgp_telemetry: one row per time bucket, one column per channel.
--
-- Two placeholders are string-substituted by tsdb_client.py (bucket width and
-- the per-channel aggregate list). Do not write those placeholder names
-- anywhere else in this file, comments included: substitution is a plain
-- string replace, and a multi-line list expanded inside a `--` comment would
-- leak SQL out of the comment and break the query.
--
-- Everything else is a psycopg parameter. psycopg2 interpolates client-side,
-- so boat/channels/times reach the server as literals - which is exactly what
-- lets TimescaleDB skip chunks. Do not "improve" this into a subquery or a
-- JOIN against a lookup table; those are evaluated too late to skip anything.
--
-- Satisfies all five speed rules from tsdb-quickstart-teams.md:
--   1. plain time range, no function wrapping `time` in WHERE
--   2. level pinned to exactly one value
--   3. boat and channel filtered with literals
--   4. channel set inlined as an array
--   5. aggregated into buckets, never a raw dump
SELECT
    time_bucket('{bucket}', time) AS "_time",
{selectList}
FROM sgp_telemetry
WHERE boat = %(boat)s
  AND level = %(level)s
  AND channel = ANY(%(channels)s)
  AND time >= %(start)s
  AND time <  %(stop)s
GROUP BY 1
ORDER BY 1;
