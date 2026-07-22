"""InfluxDB credentials: Streamlit Cloud secrets in production, .env locally.

No app login — this widget is intentionally open.
"""
import os


def _load():
    try:
        import streamlit as st
        return st.secrets["ORG_ID"], st.secrets["TOKEN"], st.secrets["URL"]
    except Exception:
        import dotenv
        dotenv.load_dotenv()
        return os.getenv("ORG_ID"), os.getenv("TOKEN"), os.getenv("URL")


ORG_ID, TOKEN, URL = _load()
BUCKET = "sailgp"
