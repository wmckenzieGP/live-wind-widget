"""Produce the Streamlit Cloud secrets block for an app. Run from the app folder.

    python generate_tsdb_secrets.py

Reads .env plus client.crt / client.key, and writes a ready-to-paste TOML block
to a file in the OS temp directory - deliberately NOT stdout, because the block
contains the private key and the database password, and terminal output ends up
in scrollback, logs and screen shares.

The same block works for every app: they all share one team certificate.
"""
import os
import stat
import sys
import tempfile

try:
    import dotenv
    dotenv.load_dotenv()
except ImportError:
    pass


def read_pem(path):
    if not os.path.exists(path):
        sys.exit(f"ERROR: {path} not found. Split the .p12 first (split_cert.bat).")
    with open(path) as f:
        return f.read().strip()


cert = read_pem(os.getenv("TSDB_SSLCERT", "client.crt"))
key = read_pem(os.getenv("TSDB_SSLKEY", "client.key"))

user = os.getenv("TSDB_USER")
password = os.getenv("TSDB_PASSWORD")
if not user or not password:
    sys.exit("ERROR: TSDB_USER / TSDB_PASSWORD missing from .env.")

block = "\n".join([
    f'TSDB_HOST = "{os.getenv("TSDB_HOST", "tsdb.sailgp.tech")}"',
    f'TSDB_PORT = "{os.getenv("TSDB_PORT", "5432")}"',
    f'TSDB_DB = "{os.getenv("TSDB_DB", "sailgp")}"',
    f'TSDB_USER = "{user}"',
    f'TSDB_PASSWORD = "{password}"',
    f'TSDB_SSLCERT_PEM = """\n{cert}\n"""',
    f'TSDB_SSLKEY_PEM = """\n{key}\n"""',
])

out = os.path.join(tempfile.gettempdir(), "tsdb_streamlit_secrets.toml")
with open(out, "w") as f:
    f.write(block + "\n")
os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)

print(f"Wrote {len(block.splitlines())} lines to:\n  {out}\n")
print("Next:")
print("  1. Open that file and copy ALL of it.")
print("  2. Streamlit Cloud -> the app -> Settings -> Secrets.")
print("  3. Paste it at the TOP, ABOVE any [section] header.")
print("     TOML puts every key after a [table] header inside that table, so")
print("     pasting below one hides these keys from st.secrets['TSDB_*'].")
print("  4. Keep what is already in the vault - service accounts, app logins -")
print("     or those features break.")
print("  5. Save, then delete the temp file.")
