"""
Unified config loader: reads from Streamlit secrets (cloud deploy) first,
falls back to environment variables / .env (local dev).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def get_secret(key: str, default: str = "") -> str:
    """Return a secret from st.secrets → os.environ → default."""
    try:
        import streamlit as st
        value = st.secrets.get(key)
        if value:
            return str(value).strip()
    except Exception:
        pass
    return os.getenv(key, default).strip()
