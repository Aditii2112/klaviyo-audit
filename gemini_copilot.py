"""
Gemini copilot for Klaviyo lifecycle strategy.

Loads `klaviyo_dump.json` into the model context and answers audit, placement,
and copywriting questions with `google-genai`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types

from config import get_secret

ROOT = Path(__file__).resolve().parent
DUMP_PATH = ROOT / "klaviyo_dump.json"

DEFAULT_MODEL = "gemini-2.5-flash"
FALLBACK_MODEL = "gemini-2.0-flash"

SYSTEM_PROMPT = """You are an expert Klaviyo Email Strategy Copilot. You have full context of all current email flows, message bodies, triggers, delays, and links. Your job is to:
   1. Answer audit and strategy questions about the account's lifecycle program.
   2. Suggest strategic placements for new lifecycle communications.
   3. Draft brand-aligned email copy based on adjacent emails in a flow.

Guidelines:
- Ground every answer in the provided Klaviyo dump. Cite flow names, statuses, trigger types, delays, subject lines, step names, links, and body copy when relevant.
- Search the dump dynamically for whatever the user asks (links, copy themes, promos, CTAs, etc.) — do not assume a fixed checklist of audit topics.
- For placement recommendations, consider trigger type, existing delays, tone of neighboring emails, and gaps in the journey.
- When drafting copy, match the brand voice of adjacent emails (subject length, preview text style, CTA phrasing). Return subject line, preview text, and body.
- If the dump is empty or a named flow is missing, say so and tell the user to click "Sync Klaviyo Data".
- Be concise, structured, and actionable. Use markdown headings and bullets.
"""

# Cache dump by mtime so Streamlit reruns stay cheap but still pick up a new sync.
_DUMP_CACHE: Dict[str, Any] = {"mtime": None, "data": None}
_CLIENT: Optional[genai.Client] = None
_RESOLVED_MODEL: Optional[str] = None


def _get_model_candidates() -> tuple[str, ...]:
    """Return (primary, fallback) model names, with primary configurable via secrets."""
    configured = get_secret("GEMINI_MODEL", DEFAULT_MODEL)
    if configured and configured != DEFAULT_MODEL:
        return (configured, DEFAULT_MODEL, FALLBACK_MODEL)
    return (DEFAULT_MODEL, FALLBACK_MODEL)


def get_client() -> genai.Client:
    """Initialize the Gemini client from GEMINI_API_KEY."""
    global _CLIENT
    if _CLIENT is None:
        api_key = get_secret("GEMINI_API_KEY")
        if not api_key or api_key.startswith("your_"):
            raise ValueError(
                "GEMINI_API_KEY is missing. Add it to Streamlit secrets or .env."
            )
        _CLIENT = genai.Client(api_key=api_key)
    return _CLIENT


def load_dump() -> Dict[str, Any]:
    """Read the local JSON cache, refreshing when the file changes on disk."""
    if not DUMP_PATH.exists():
        return {
            "synced_at": None,
            "account_summary": {},
            "flows": [],
            "templates": [],
        }
    mtime = DUMP_PATH.stat().st_mtime
    if _DUMP_CACHE["mtime"] != mtime:
        with DUMP_PATH.open(encoding="utf-8") as handle:
            _DUMP_CACHE["data"] = json.load(handle)
        _DUMP_CACHE["mtime"] = mtime
    return _DUMP_CACHE["data"] or {}


def dump_is_populated(dump: Optional[Dict[str, Any]] = None) -> bool:
    data = dump if dump is not None else load_dump()
    return bool(data.get("flows") or data.get("templates"))


def _truncate(text: Optional[str], limit: int = 2500) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


def compact_dump_for_model(dump: Dict[str, Any]) -> str:
    """
    Project the dump into a token-efficient JSON payload.

    Raw HTML is dropped; plain-text bodies and links are kept so Gemini can
    inspect every email and answer open-ended audit questions.
    """
    compact_flows = []
    for flow in dump.get("flows") or []:
        compact_steps = []
        for step in flow.get("steps") or []:
            email = step.get("email")
            sms = step.get("sms")
            compact_steps.append(
                {
                    "id": step.get("id"),
                    "type": step.get("type"),
                    "status": step.get("status"),
                    "delay": step.get("delay"),
                    "email": (
                        {
                            "name": email.get("name"),
                            "subject_line": email.get("subject_line"),
                            "preview_text": email.get("preview_text"),
                            "from_email": email.get("from_email"),
                            "from_label": email.get("from_label"),
                            "template_id": email.get("template_id"),
                            "body_text": _truncate(email.get("body_text")),
                            "links": (email.get("links") or [])[:25],
                        }
                        if email
                        else None
                    ),
                    "sms": (
                        {
                            "name": sms.get("name"),
                            "body": _truncate(sms.get("body"), 800),
                        }
                        if sms
                        else None
                    ),
                    "split": step.get("split"),
                }
            )
        compact_flows.append(
            {
                "id": flow.get("id"),
                "name": flow.get("name"),
                "status": flow.get("status"),
                "archived": flow.get("archived"),
                "trigger_type": flow.get("trigger_type"),
                "trigger": flow.get("trigger"),
                "created": flow.get("created"),
                "updated": flow.get("updated"),
                "steps": compact_steps,
            }
        )

    payload = {
        "synced_at": dump.get("synced_at"),
        "account_summary": dump.get("account_summary"),
        "flows": compact_flows,
    }
    encoded = json.dumps(payload, indent=2, default=str)
    # Hard cap so a huge account still fits a single generate_content call.
    max_chars = 750_000
    if len(encoded) > max_chars:
        encoded = encoded[:max_chars] + "\n…[context truncated]"
    return encoded


def _history_to_contents(
    chat_history: List[Dict[str, str]], query: str
) -> List[types.Content]:
    """Map Streamlit {role, content} messages onto Gemini user/model turns."""
    contents: List[types.Content] = []
    for message in chat_history:
        role = message.get("role", "user")
        text = (message.get("content") or "").strip()
        if not text:
            continue
        gemini_role = "model" if role in {"assistant", "model"} else "user"
        contents.append(
            types.Content(role=gemini_role, parts=[types.Part.from_text(text=text)])
        )
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=query)]))
    return contents


def _stream(client: genai.Client, model: str, contents: List[types.Content], system: str):
    """Return a streaming response iterator."""
    return client.models.generate_content_stream(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.4,
            max_output_tokens=16000,
        ),
    )


def ask_copilot_stream(query: str, chat_history: Optional[list] = None):
    """
    Stream the Gemini reply token-by-token.

    Yields text chunks as they arrive. `chat_history` must NOT already contain
    the current `query` — pass only prior messages.
    """
    dump = load_dump()
    context_json = compact_dump_for_model(dump)
    system = (
        f"{SYSTEM_PROMPT}\n\n"
        "--- Klaviyo dump (JSON) ---\n"
        f"{context_json}\n"
        "--- end dump ---"
    )

    client = get_client()
    contents = _history_to_contents(chat_history or [], query)

    global _RESOLVED_MODEL
    candidates = _get_model_candidates()
    models = [_RESOLVED_MODEL] if _RESOLVED_MODEL else list(candidates)
    for candidate in candidates:
        if candidate not in models:
            models.append(candidate)

    last_exc: Optional[Exception] = None
    for model in models:
        try:
            for chunk in _stream(client, model, contents, system):
                if chunk.text:
                    yield chunk.text
            _RESOLVED_MODEL = model
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _RESOLVED_MODEL = None
            continue

    raise RuntimeError(
        "Gemini request failed for all candidate models."
    ) from last_exc


def ask_copilot(query: str, chat_history: Optional[list] = None) -> str:
    """Non-streaming wrapper — collects the full streamed reply into a string."""
    return "".join(ask_copilot_stream(query, chat_history))


def resolved_model_name() -> Optional[str]:
    return _RESOLVED_MODEL


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) or "Summarize our current lifecycle flows."
    print(ask_copilot(question, []))
