"""Drop-in credential loading for the SailGP TimescaleDB.

Copy this file into the app (alongside its existing config.py, or merged into
it). Reads Streamlit Cloud secrets in production, .env locally.

The client certificate is the awkward part: locally it is a file on disk, but
Streamlit Cloud has no filesystem you can upload one to, so there it travels as
PEM text in the secrets vault and gets written to a temp file at startup.
Both paths end up handing libpq a filename, which is all it accepts.
"""
import os
import stat
import tempfile

try:
    import streamlit as st
    _secrets = st.secrets
except Exception:
    _secrets = {}

try:
    import dotenv
    # load_dotenv() searches from the directory of THIS file, which is wrong
    # whenever the kit is run from outside the app folder: it finds nothing,
    # falls back to the OS username, and the server rejects you with a
    # pg_hba.conf error that never mentions the missing .env. Search the
    # working directory first, then next to this file.
    _env = dotenv.find_dotenv(usecwd=True) or dotenv.find_dotenv()
    if _env:
        dotenv.load_dotenv(_env)
except ImportError:
    pass


def _get(key, default=None):
    """Streamlit Cloud vault first, local .env second."""
    try:
        if key in _secrets:
            return _secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)


def _materialize(pem_text, filename):
    path = os.path.join(tempfile.gettempdir(), filename)
    with open(path, "w") as f:
        f.write(pem_text.strip() + "\n")
    # libpq refuses a group/world-readable private key on Linux (Streamlit
    # Cloud). No-op on Windows, harmless to always apply.
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


TSDB_HOST     = _get("TSDB_HOST", "tsdb.sailgp.tech")
TSDB_PORT     = int(_get("TSDB_PORT", 5432))
TSDB_DB       = _get("TSDB_DB", "sailgp")
TSDB_USER     = _get("TSDB_USER")
TSDB_PASSWORD = _get("TSDB_PASSWORD")
# libpq waits forever by default. A caller polling on a timer needs a refused
# or unreachable server to come back as an error it can handle, not as a thread
# parked on a handshake that is never going to finish. The TLS handshake itself
# measures ~3.3 s, so this leaves room for a slow one.
TSDB_CONNECT_TIMEOUT = int(_get("TSDB_CONNECT_TIMEOUT", 10))
# Connections the role is allowed to hold at once. The server decides this, not
# us: past the cap the extras are refused at connect time, so asking for more
# buys nothing and costs the first query that wants one. One is the safe floor
# for a shared team login; raise it here or in secrets once the real cap is
# known, without touching code.
TSDB_MAX_CONNECTIONS = max(1, int(_get("TSDB_MAX_CONNECTIONS", 1)))

_cert_pem = _get("TSDB_SSLCERT_PEM")
_key_pem = _get("TSDB_SSLKEY_PEM")

if _cert_pem and _key_pem:
    TSDB_SSLCERT = _materialize(_cert_pem, "sgp_client.crt")
    TSDB_SSLKEY = _materialize(_key_pem, "sgp_client.key")
else:
    TSDB_SSLCERT = _get("TSDB_SSLCERT", "client.crt")
    TSDB_SSLKEY = _get("TSDB_SSLKEY", "client.key")


def connection_kwargs():
    """Everything psycopg2.connect() needs, including TLS."""
    import certifi
    if not TSDB_USER or not TSDB_PASSWORD:
        raise RuntimeError(
            "TSDB_USER / TSDB_PASSWORD not found in Streamlit secrets or .env. "
            "Without them psycopg2 falls back to your OS username and the "
            "server answers with a confusing 'no pg_hba.conf entry' error. "
            "Run from the app folder, or check the .env is saved."
        )
    for label, path in (("TSDB_SSLCERT", TSDB_SSLCERT), ("TSDB_SSLKEY", TSDB_SSLKEY)):
        if not os.path.exists(path):
            raise RuntimeError(
                f"{label} points at '{path}', which does not exist. Split the "
                f".p12 with split_cert.bat, or set {label} in .env."
            )
    return dict(
        host=TSDB_HOST, port=TSDB_PORT, dbname=TSDB_DB,
        user=TSDB_USER, password=TSDB_PASSWORD,
        connect_timeout=TSDB_CONNECT_TIMEOUT,
        sslmode="verify-full",           # server refuses anything weaker
        sslcert=TSDB_SSLCERT, sslkey=TSDB_SSLKEY,
        # certifi validates the public server cert on every OS. Do NOT point
        # this at the CA inside the .p12 - that is the *client* CA and does not
        # verify the server.
        sslrootcert=certifi.where(),
    )
