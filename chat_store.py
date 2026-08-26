"""
Persist chat history across Streamlit Cloud sleeps/restarts.

Streamlit Cloud can restart the server without you refreshing the page.
That wipes st.session_state. We keep chat in:
  1. browser localStorage (survives cloud sleep while the tab stays open)
  2. chat_history.json on disk (helps local runs / same container)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import streamlit as st

ROOT = Path(__file__).resolve().parent
CHAT_PATH = ROOT / "chat_history.json"
STORAGE_KEY = "klaviyo_copilot_chat_v1"


def _clean(messages: List[dict]) -> List[dict]:
    return [
        {"role": m.get("role"), "content": m.get("content")}
        for m in messages
        if isinstance(m, dict)
        and m.get("role") in {"user", "assistant"}
        and m.get("content") is not None
    ]


def load_chat_from_disk() -> List[dict]:
    if not CHAT_PATH.exists():
        return []
    try:
        data = json.loads(CHAT_PATH.read_text(encoding="utf-8"))
        return _clean(data) if isinstance(data, list) else []
    except Exception:
        return []


def save_chat_to_disk(messages: List[dict]) -> None:
    CHAT_PATH.write_text(json.dumps(_clean(messages), indent=2), encoding="utf-8")


def clear_chat_disk() -> None:
    if CHAT_PATH.exists():
        CHAT_PATH.unlink()


def _local_storage():
    try:
        from streamlit_local_storage import LocalStorage

        return LocalStorage()
    except Exception:
        return None


def _load_from_browser() -> Optional[List[dict]]:
    ls = _local_storage()
    if ls is None:
        return None
    try:
        raw = ls.getItem(STORAGE_KEY)
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return _clean(data) if isinstance(data, list) else None
    except Exception:
        return None


def _save_to_browser(messages: List[dict]) -> None:
    ls = _local_storage()
    if ls is None:
        return
    try:
        ls.setItem(STORAGE_KEY, json.dumps(_clean(messages)))
    except Exception:
        pass


def init_chat_state() -> None:
    """Hydrate session_state.messages from browser, then disk."""
    if "pending_prompt" not in st.session_state:
        st.session_state.pending_prompt = None

    if "messages" in st.session_state:
        return

    browser = _load_from_browser()
    if browser:
        st.session_state.messages = browser
        return

    st.session_state.messages = load_chat_from_disk()


def persist_chat(messages: List[dict]) -> None:
    cleaned = _clean(messages)
    st.session_state.messages = cleaned
    save_chat_to_disk(cleaned)
    _save_to_browser(cleaned)


def clear_chat() -> None:
    st.session_state.messages = []
    st.session_state.pending_prompt = None
    clear_chat_disk()
    _save_to_browser([])
