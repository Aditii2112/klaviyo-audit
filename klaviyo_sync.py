"""
Klaviyo REST API v3 sync pipeline.

Pulls flows (with definitions), flow actions/messages, and templates, then
parses them into a structured local cache (`klaviyo_dump.json`) that the
Streamlit copilot can reason over.

Usage:
    python klaviyo_sync.py
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from config import get_secret

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent

DUMP_PATH = ROOT / "klaviyo_dump.json"
KLAVIYO_BASE = "https://a.klaviyo.com"
# Pin a revision that supports GET /api/flows/{id}?additional-fields[flow]=definition.
API_REVISION = "2026-01-15"

# Extract href targets from email HTML.
HREF_RE = re.compile(r"""href\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)


ProgressCallback = Optional[Callable[[str], None]]


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


class _BodyParser(HTMLParser):
    """Extract visible text and <a href> URLs from email HTML."""

    SKIP_TAGS = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self.links: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in {"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "td"}:
            self.parts.append("\n")
        href = dict(attrs).get("href")
        if href:
            self.links.append(href.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text_and_links(html: str) -> tuple[str, List[str]]:
    """Return (plain_text, href_list) from an HTML email body."""
    if not html:
        return "", []
    parser = _BodyParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Fall back to a crude strip if the markup is malformed.
        text = re.sub(r"<[^>]+>", " ", html)
        links = HREF_RE.findall(html)
        return re.sub(r"\s+", " ", text).strip(), links
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # Preserve unique link order.
    seen = set()
    unique_links: List[str] = []
    for link in parser.links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
    return text, unique_links


def human_delay(data: Dict[str, Any]) -> Optional[str]:
    """Normalize a time-delay action payload into a readable string."""
    if not data:
        return None
    value = data.get("value")
    unit = data.get("unit")
    if value is None and "delay_seconds" in data:
        seconds = int(data["delay_seconds"] or 0)
        if seconds % 86400 == 0:
            value, unit = seconds // 86400, "days"
        elif seconds % 3600 == 0:
            value, unit = seconds // 3600, "hours"
        elif seconds % 60 == 0:
            value, unit = seconds // 60, "minutes"
        else:
            value, unit = seconds, "seconds"
    if value is None:
        return None
    unit = str(unit or "units")
    try:
        numeric = float(value)
        singular = numeric == 1
    except (TypeError, ValueError):
        singular = str(value) == "1"
    label = unit.rstrip("s") if singular else unit
    extra = []
    if data.get("delay_until_time"):
        extra.append(f"until {data['delay_until_time']}")
    weekdays = data.get("delay_until_weekdays") or []
    if weekdays:
        extra.append("on " + ", ".join(weekdays))
    suffix = f" ({'; '.join(extra)})" if extra else ""
    return f"{value} {label}{suffix}"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class KlaviyoClient:
    """Thin wrapper around Klaviyo REST API v3 with pagination + 429 retries."""

    def __init__(self, api_key: str) -> None:
        if not api_key or api_key.startswith("your_"):
            raise ValueError(
                "KLAVIYO_API_KEY is missing. Add your private key to .env."
            )
        self.session = requests.Session()
        # Streamlit / IDE shells often set HTTP(S)_PROXY; bypass for Klaviyo direct calls.
        self.session.trust_env = False
        self.session.proxies = {"http": None, "https": None}
        self.session.headers.update(
            {
                "Authorization": f"Klaviyo-API-Key {api_key}",
                "Accept": "application/vnd.api+json",
                "Revision": API_REVISION,
            }
        )

    def get(self, path_or_url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else f"{KLAVIYO_BASE}{path_or_url}"
        )
        last_error: Optional[Exception] = None
        for attempt in range(7):
            try:
                response = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(min(2**attempt, 16))
                continue
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else (2**attempt)
                time.sleep(min(wait, 30))
                continue
            if 500 <= response.status_code < 600:
                time.sleep(min(2**attempt, 16))
                continue
            if not response.ok:
                detail = response.text[:500]
                raise RuntimeError(
                    f"Klaviyo {response.status_code} on {url}: {detail}"
                )
            return response.json()
        raise RuntimeError(f"Klaviyo request failed after retries: {last_error}")

    def paginate(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Walk `links.next` until every page is collected."""
        records: List[Dict[str, Any]] = []
        url: Optional[str] = path
        query = params
        while url:
            payload = self.get(url, params=query)
            records.extend(payload.get("data") or [])
            url = (payload.get("links") or {}).get("next")
            query = None  # cursor URL already encodes params
        return records


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def fetch_flow_definition(client: KlaviyoClient, flow_id: str) -> Dict[str, Any]:
    """GET /api/flows/{id} with the definition additional field (read-only)."""
    payload = client.get(
        f"/api/flows/{flow_id}/",
        params={"additional-fields[flow]": "definition"},
    )
    return payload.get("data") or {}


def fetch_flows(client: KlaviyoClient, log: Callable[[str], None]) -> List[Dict[str, Any]]:
    """GET /api/flows, then hydrate each flow with its definition. Includes archived."""
    list_params = {
        "page[size]": 50,
        "fields[flow]": "name,status,archived,created,updated,trigger_type",
    }
    log("Fetching flow list…")
    live = client.paginate("/api/flows/", params=list_params)
    try:
        archived = client.paginate(
            "/api/flows/",
            params={**list_params, "filter": "equals(archived,true)"},
        )
    except Exception as exc:
        log(f"Archived flow fetch skipped: {exc}")
        archived = []

    by_id = {item["id"]: item for item in live}
    for item in archived:
        by_id.setdefault(item["id"], item)

    flows: List[Dict[str, Any]] = []
    total = len(by_id)
    log(f"  → {total} flow(s). Loading definitions…")
    for i, flow_id in enumerate(by_id, start=1):
        time.sleep(0.35)  # stay under the 3 req/s flows burst limit
        try:
            detailed = fetch_flow_definition(client, flow_id)
            flows.append(detailed or by_id[flow_id])
        except Exception as exc:
            log(f"  definition skipped for {flow_id}: {exc}")
            flows.append(by_id[flow_id])
        if i % 10 == 0 or i == total:
            log(f"  definitions {i}/{total}")
    return flows


def fetch_templates(client: KlaviyoClient, log: Callable[[str], None]) -> List[Dict[str, Any]]:
    """GET /api/templates including HTML bodies."""
    log("Fetching templates…")
    records = client.paginate(
        "/api/templates/",
        params={
            "page[size]": 10,
            "fields[template]": "name,editor_type,html,text,created,updated",
        },
    )
    log(f"  → {len(records)} template(s)")
    return records


def fetch_template_by_id(client: KlaviyoClient, template_id: str) -> Optional[Dict[str, Any]]:
    try:
        payload = client.get(
            f"/api/templates/{template_id}/",
            params={"fields[template]": "name,editor_type,html,text,created,updated"},
        )
        return payload.get("data")
    except Exception:
        return None


def fetch_flow_messages(
    client: KlaviyoClient, action_id: str
) -> List[Dict[str, Any]]:
    """GET /api/flow-actions/{id}/flow-messages — subject, preview, from-address."""
    try:
        payload = client.get(f"/api/flow-actions/{action_id}/flow-messages/")
        return payload.get("data") or []
    except Exception:
        return []


def fetch_message_template(
    client: KlaviyoClient, message_id: str
) -> Optional[Dict[str, Any]]:
    """GET /api/flow-messages/{id}/template — HTML for a specific send-email step."""
    try:
        payload = client.get(f"/api/flow-messages/{message_id}/template/")
        return payload.get("data")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _attrs(resource: Dict[str, Any]) -> Dict[str, Any]:
    return resource.get("attributes") or {}


def parse_email_content(
    html: str,
    text_fallback: str,
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    body_text, links = html_to_text_and_links(html)
    if not body_text:
        body_text = (text_fallback or "").strip()
    return {
        "name": meta.get("name"),
        "subject_line": meta.get("subject_line") or meta.get("subject"),
        "preview_text": meta.get("preview_text") or "",
        "from_email": meta.get("from_email"),
        "from_label": meta.get("from_label"),
        "template_id": meta.get("template_id"),
        "message_id": meta.get("id") or meta.get("message_id"),
        "body_text": body_text,
        "html": html or "",
        "links": links,
    }


def parse_definition_action(
    action: Dict[str, Any],
    template_lookup: Dict[str, Dict[str, Any]],
    client: KlaviyoClient,
) -> Dict[str, Any]:
    """Map one item from `definition.actions` into a normalized step."""
    action_type = action.get("type") or action.get("action_type") or "unknown"
    data = action.get("data") or {}
    step: Dict[str, Any] = {
        "id": str(action.get("id")),
        "type": action_type,
        "status": data.get("status"),
        "next": (action.get("links") or {}).get("next"),
        "delay": None,
        "email": None,
        "sms": None,
        "split": None,
        "raw_summary": None,
    }

    if action_type in {"time-delay", "TIME_DELAY", "countdown-delay", "back-in-stock-delay"}:
        step["delay"] = {
            "label": human_delay(data),
            "value": data.get("value"),
            "unit": data.get("unit"),
            "timezone": data.get("timezone"),
            "delay_until_time": data.get("delay_until_time"),
            "delay_until_weekdays": data.get("delay_until_weekdays") or [],
        }
        return step

    if action_type in {"send-email", "SEND_EMAIL"}:
        message = data.get("message") or {}
        template_id = message.get("template_id")
        html = ""
        text = ""
        if template_id and template_id in template_lookup:
            tmpl = template_lookup[template_id]
            html = tmpl.get("html") or ""
            text = tmpl.get("text") or ""
        elif template_id:
            time.sleep(0.35)
            fetched = fetch_template_by_id(client, template_id)
            if fetched:
                parsed = parse_template_resource(fetched)
                template_lookup[template_id] = parsed
                html = parsed.get("html") or ""
                text = parsed.get("text") or ""
        # Last resort: resolve HTML via the flow-message relationship.
        if not html and message.get("id"):
            time.sleep(0.35)
            tmpl = fetch_message_template(client, message["id"])
            if tmpl:
                parsed = parse_template_resource(tmpl)
                html = parsed.get("html") or ""
                text = parsed.get("text") or ""
                if parsed.get("id"):
                    template_lookup.setdefault(parsed["id"], parsed)
        step["email"] = parse_email_content(html, text, message)
        return step

    if action_type in {"send-sms", "SEND_SMS"}:
        message = data.get("message") or {}
        body = message.get("body") or ""
        step["sms"] = {
            "name": message.get("name"),
            "body": body,
            "message_id": message.get("id"),
        }
        return step

    if action_type in {"conditional-split", "trigger-split", "CONDITIONAL_SPLIT", "TRIGGER_SPLIT"}:
        step["split"] = {
            "true_next": (action.get("links") or {}).get("next_if_true"),
            "false_next": (action.get("links") or {}).get("next_if_false"),
            "profile_filter": data.get("profile_filter"),
            "trigger_filter": data.get("trigger_filter"),
        }
        return step

    # Keep a compact snapshot of anything else (webhooks, profile updates, …).
    step["raw_summary"] = {
        k: v for k, v in data.items() if k not in {"message"}
    }
    return step


def parse_template_resource(resource: Dict[str, Any]) -> Dict[str, Any]:
    attrs = _attrs(resource)
    html = attrs.get("html") or ""
    text = attrs.get("text") or ""
    body_text, links = html_to_text_and_links(html)
    if not body_text:
        body_text = (text or "").strip()
    return {
        "id": resource.get("id"),
        "name": attrs.get("name"),
        "editor_type": attrs.get("editor_type"),
        "created": attrs.get("created"),
        "updated": attrs.get("updated"),
        "html": html,
        "text": text,
        "body_text": body_text,
        "links": links,
    }


def parse_flow(
    flow: Dict[str, Any],
    template_lookup: Dict[str, Dict[str, Any]],
    client: KlaviyoClient,
    log: Callable[[str], None],
) -> Dict[str, Any]:
    attrs = _attrs(flow)
    definition = attrs.get("definition") or {}
    actions = definition.get("actions") or []
    steps: List[Dict[str, Any]] = []

    if actions:
        for action in actions:
            steps.append(parse_definition_action(action, template_lookup, client))
    else:
        # Older accounts / revisions may omit definition; walk related actions.
        log(f"  No definition on '{attrs.get('name')}'; fetching flow-actions…")
        steps = parse_flow_via_actions(client, flow["id"], template_lookup)

    trigger = None
    triggers = definition.get("triggers") or []
    if triggers:
        trigger = triggers[0]

    return {
        "id": flow.get("id"),
        "name": attrs.get("name"),
        "status": attrs.get("status"),
        "archived": bool(attrs.get("archived")),
        "trigger_type": attrs.get("trigger_type"),
        "trigger": trigger,
        "created": attrs.get("created"),
        "updated": attrs.get("updated"),
        "entry_action_id": definition.get("entry_action_id"),
        "profile_filter": definition.get("profile_filter"),
        "steps": steps,
    }


def parse_flow_via_actions(
    client: KlaviyoClient,
    flow_id: str,
    template_lookup: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Fallback: GET /api/flows/{id}/flow-actions then related messages."""
    actions = client.paginate(f"/api/flows/{flow_id}/flow-actions")
    steps: List[Dict[str, Any]] = []
    for action in actions:
        attrs = _attrs(action)
        action_type = attrs.get("action_type") or "unknown"
        step: Dict[str, Any] = {
            "id": action.get("id"),
            "type": action_type,
            "status": attrs.get("status"),
            "next": None,
            "delay": None,
            "email": None,
            "sms": None,
            "split": None,
            "raw_summary": attrs.get("settings") or {},
        }
        settings = attrs.get("settings") or {}
        if "DELAY" in str(action_type).upper() or "delay" in settings:
            step["delay"] = {
                "label": human_delay(settings),
                **settings,
            }
        if "EMAIL" in str(action_type).upper():
            time.sleep(0.35)  # stay under the 3 req/s flows burst limit
            messages = fetch_flow_messages(client, action["id"])
            if messages:
                msg = messages[0]
                mattrs = _attrs(msg)
                content = mattrs.get("content") or {}
                html = ""
                text = ""
                tmpl = fetch_message_template(client, msg["id"])
                if tmpl:
                    parsed = parse_template_resource(tmpl)
                    html = parsed.get("html") or ""
                    text = parsed.get("text") or ""
                meta = {
                    "name": mattrs.get("name"),
                    "subject_line": content.get("subject") or content.get("subject_line"),
                    "preview_text": content.get("preview_text"),
                    "from_email": content.get("from_email"),
                    "from_label": content.get("from_label"),
                    "template_id": ((msg.get("relationships") or {}).get("template") or {})
                    .get("data", {})
                    .get("id"),
                    "id": msg.get("id"),
                }
                step["email"] = parse_email_content(html, text, meta)
        steps.append(step)
    return steps


def summarize(flows: List[Dict[str, Any]], templates: List[Dict[str, Any]]) -> Dict[str, int]:
    emails = [
        step.get("email")
        for flow in flows
        for step in flow.get("steps") or []
        if step.get("email")
    ]
    sms = [
        step.get("sms")
        for flow in flows
        for step in flow.get("steps") or []
        if step.get("sms")
    ]
    statuses = [str(f.get("status") or "").lower() for f in flows]
    return {
        "total_flows": len(flows),
        "live_flows": sum(1 for s in statuses if s == "live"),
        "manual_flows": sum(1 for s in statuses if s == "manual"),
        "draft_flows": sum(1 for s in statuses if s == "draft"),
        "total_templates": len(templates),
        "total_emails": len(emails),
        "total_sms": len(sms),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_sync(progress_callback: ProgressCallback = None) -> Dict[str, Any]:
    """
    Fetch Klaviyo lifecycle data and write `klaviyo_dump.json`.

    Callable from the CLI (`python klaviyo_sync.py`) or from Streamlit.
    Returns the dump dict that was written.
    """

    def log(message: str) -> None:
        if progress_callback:
            progress_callback(message)
        else:
            print(message)

    api_key = get_secret("KLAVIYO_API_KEY")
    client = KlaviyoClient(api_key)

    log("Starting Klaviyo sync…")
    raw_flows = fetch_flows(client, log)
    raw_templates = fetch_templates(client, log)

    templates = [parse_template_resource(item) for item in raw_templates]
    template_lookup = {t["id"]: t for t in templates if t.get("id")}

    flows: List[Dict[str, Any]] = []
    for i, raw in enumerate(raw_flows, start=1):
        name = _attrs(raw).get("name") or raw.get("id")
        log(f"Parsing flow {i}/{len(raw_flows)}: {name}")
        flows.append(parse_flow(raw, template_lookup, client, log))

    # Templates fetched on demand while parsing emails should also be stored.
    templates = list(template_lookup.values())
    dump = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "api_revision": API_REVISION,
        "account_summary": summarize(flows, templates),
        "flows": flows,
        "templates": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "editor_type": t.get("editor_type"),
                "created": t.get("created"),
                "updated": t.get("updated"),
                "body_text": t.get("body_text"),
                "links": t.get("links"),
                "html": t.get("html"),
            }
            for t in templates
        ],
    }

    DUMP_PATH.write_text(json.dumps(dump, indent=2), encoding="utf-8")
    summary = dump["account_summary"]
    log(
        f"Wrote {DUMP_PATH.name}: {summary['total_flows']} flows, "
        f"{summary['total_templates']} templates, {summary['total_emails']} emails."
    )
    return dump


if __name__ == "__main__":
    run_sync()
