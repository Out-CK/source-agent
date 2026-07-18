"""Detect schema.org Event structured data in raw page content."""
from __future__ import annotations

import json
import re

_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

_EVENT_TYPES = {
    "event", "musicevent", "theaterevent", "comedyevent", "danceevent",
    "festival", "foodevent", "exhibitionevent", "socialevent", "educationevent",
    "visualartsevent", "screeningevent", "literaryevent",
}


def _walk(node) -> bool:
    if isinstance(node, dict):
        t = node.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if any(str(x).lower() in _EVENT_TYPES for x in types):
            return True
        return any(_walk(v) for v in node.values())
    if isinstance(node, list):
        return any(_walk(x) for x in node)
    return False


def has_event_jsonld(content: str) -> bool:
    """True if the page content embeds schema.org Event JSON-LD (any subtype)."""
    if not content:
        return False
    for block in _JSONLD_RE.findall(content):
        try:
            if _walk(json.loads(block.strip())):
                return True
        except (json.JSONDecodeError, ValueError):
            continue
    # Markdown-rendered pages lose <script> tags; fall back to a cheap signal
    return '"@type"' in content and any(
        f'"{t}"' in content.lower() for t in ("musicevent", "theaterevent", "comedyevent", "event")
    )
