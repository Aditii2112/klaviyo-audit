"""
Streamlit dashboard: Klaviyo lifecycle dump + Gemini strategy copilot.

Run:
    streamlit run app.py
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from config import get_secret
from gemini_copilot import (
    ask_copilot,
    dump_is_populated,
    load_dump,
    resolved_model_name,
)
from klaviyo_sync import DUMP_PATH, run_sync

ROOT = Path(__file__).resolve().parent

STARTER_PROMPTS = [
    "Give me an overview of our live lifecycle flows.",
    "Where should I insert a new VIP/Loyalty program email?",
    "Draft a follow-up email for our Abandoned Cart flow.",
]


def _key_configured(name: str) -> bool:
    value = get_secret(name)
    return bool(value) and not value.startswith("your_")


def _format_synced_at(raw: object) -> str:
    if not raw:
        return "Never"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(raw)


def _inject_styles() -> None:
    st.markdown(
        """
        <style>
          .stApp { background: #0f1419; }
          [data-testid="stSidebar"] {
            background: #161d27;
            border-right: 1px solid #243042;
          }
          [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2,
          [data-testid="stSidebar"] h3, [data-testid="stSidebar"] p,
          [data-testid="stSidebar"] span, [data-testid="stSidebar"] label {
            color: #e8eef5;
          }
          .hero-kicker {
            letter-spacing: 0.12em;
            text-transform: uppercase;
            font-size: 0.72rem;
            color: #7ddea0;
            margin-bottom: 0.15rem;
          }
          .status-pill {
            display: inline-block;
            padding: 0.15rem 0.55rem;
            border-radius: 999px;
            font-size: 0.78rem;
            font-weight: 600;
          }
          .ok { background: #143d2a; color: #7ddea0; }
          .bad { background: #3d1a1a; color: #f0a0a0; }
          .starter-wrap { margin: 0.4rem 0 1.1rem 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _run_sync() -> None:
    log_lines: list[str] = []
    status = st.status("Syncing Klaviyo data…", expanded=True)

    def on_progress(message: str) -> None:
        log_lines.append(message)
        status.write(message)

    try:
        dump = run_sync(progress_callback=on_progress)
        summary = dump.get("account_summary") or {}
        status.update(
            label=(
                f"Sync complete — {summary.get('total_flows', 0)} flows, "
                f"{summary.get('total_templates', 0)} templates"
            ),
            state="complete",
        )
        st.session_state["last_sync_ok"] = True
        st.session_state["last_sync_error"] = None
    except Exception as exc:  # noqa: BLE001 — show sync failures in the UI
        status.update(label="Sync failed", state="error")
        status.write(str(exc))
        st.session_state["last_sync_ok"] = False
        text = str(exc)
        if "Failed to resolve" in text or "nodename nor servname" in text:
            st.session_state["last_sync_error"] = (
                "Could not reach Klaviyo (DNS/network). Your existing local dump is still usable — "
                "ask questions below, or retry Sync when you’re on a normal network."
            )
        elif "ProxyError" in text or "Unable to connect to proxy" in text:
            st.session_state["last_sync_error"] = (
                "A proxy blocked the Klaviyo request. Your existing local dump is still usable."
            )
        else:
            st.session_state["last_sync_error"] = text


def main() -> None:
    st.set_page_config(
        page_title="Klaviyo AI Copilot",
        page_icon="✉️",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_styles()

    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "pending_prompt" not in st.session_state:
        st.session_state.pending_prompt = None

    dump = load_dump()
    summary = dump.get("account_summary") or {}
    klaviyo_ok = _key_configured("KLAVIYO_API_KEY")
    gemini_ok = _key_configured("GEMINI_API_KEY")
    cache_exists = DUMP_PATH.exists() and dump_is_populated(dump)

    with st.sidebar:
        st.markdown("### ✉️ Klaviyo Copilot")
        st.caption("Lifecycle audit & copy assistant")
        st.divider()

        st.markdown("**API status**")
        st.markdown(
            f'<span class="status-pill {"ok" if klaviyo_ok else "bad"}">'
            f'{"● Klaviyo key set" if klaviyo_ok else "○ KLAVIYO_API_KEY missing"}'
            f"</span>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<span class="status-pill {"ok" if gemini_ok else "bad"}">'
            f'{"● Gemini key set" if gemini_ok else "○ GEMINI_API_KEY missing"}'
            f"</span>",
            unsafe_allow_html=True,
        )
        model = resolved_model_name()
        st.caption(f"Model: `{model or 'gemini-2.5-flash'}`")

        st.caption(f"Last synced: **{_format_synced_at(dump.get('synced_at'))}**")
        st.caption(f"Cache: `{DUMP_PATH.name}` {'ready' if cache_exists else 'empty — sync first'}")

        st.divider()
        sync_disabled = not klaviyo_ok
        if st.button(
            "🔄 Sync Klaviyo Data",
            use_container_width=True,
            disabled=sync_disabled,
            type="primary",
        ):
            _run_sync()
            st.rerun()
        if sync_disabled:
            st.caption("Add `KLAVIYO_API_KEY` to `.env` to enable sync.")
        if st.session_state.get("last_sync_error"):
            st.error(st.session_state["last_sync_error"])

        st.divider()
        st.markdown("**Library**")
        c1, c2 = st.columns(2)
        c1.metric("Flows synced", int(summary.get("total_flows") or 0))
        c2.metric("Templates indexed", int(summary.get("total_templates") or 0))
        c3, c4 = st.columns(2)
        c3.metric("Emails parsed", int(summary.get("total_emails") or 0))
        c4.metric("Live flows", int(summary.get("live_flows") or 0))
        c5, c6 = st.columns(2)
        c5.metric("Draft flows", int(summary.get("draft_flows") or 0))
        c6.metric("SMS steps", int(summary.get("total_sms") or 0))

        st.divider()
        if st.button("Clear chat", use_container_width=True):
            st.session_state.messages = []
            st.session_state.pending_prompt = None
            st.rerun()

    st.markdown('<p class="hero-kicker">Lifecycle strategy</p>', unsafe_allow_html=True)
    st.title("Klaviyo AI Assistant")
    st.caption(
        "Ask audit questions, place new lifecycle emails, or draft copy using "
        "the full local dump of your flows and templates."
    )

    if not cache_exists:
        st.info(
            "No Klaviyo data yet. Set `KLAVIYO_API_KEY` in `.env`, then click "
            "**🔄 Sync Klaviyo Data** in the sidebar."
        )
    else:
        st.caption(
            f"Using local dump from {_format_synced_at(dump.get('synced_at'))} — "
            "chat works from this cache; Sync only refreshes it from Klaviyo."
        )
    if not gemini_ok:
        st.warning("Set `GEMINI_API_KEY` in `.env` to enable the copilot.")

    st.markdown("**Starter prompts**")
    cols = st.columns(3)
    for col, prompt in zip(cols, STARTER_PROMPTS):
        with col:
            if st.button(prompt, use_container_width=True, key=f"starter-{prompt}"):
                st.session_state.pending_prompt = prompt

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    pending = st.session_state.pending_prompt
    typed = st.chat_input("Ask about flows, links, placements, or draft copy…")
    prompt = pending or typed
    if prompt:
        st.session_state.pending_prompt = None
        if not gemini_ok:
            st.session_state.messages.append({"role": "user", "content": prompt})
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": "Add `GEMINI_API_KEY` to `.env` and restart the app.",
                }
            )
            st.rerun()
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Thinking with your Klaviyo dump…"):
                history = st.session_state.messages
                st.session_state.messages.append({"role": "user", "content": prompt})
                try:
                    reply = ask_copilot(prompt, history)
                except Exception as exc:  # noqa: BLE001
                    reply = f"**Copilot error:** {exc}"
                st.markdown(reply)
        st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()


if __name__ == "__main__":
    main()
