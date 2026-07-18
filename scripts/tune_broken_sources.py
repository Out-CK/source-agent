"""
One-off: find working scrape_options for sources whose calendars render empty.
Tries escalating configs per URL; prints the first config that yields real
event content (JSON result map at the end).
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from tools.nimble_extract_tool import NimbleExtractTool  # noqa: E402

# URL -> candidate replacement URLs (first entry = current registry URL)
TARGETS = {
    "SRC-000001": ["https://www.msg.com/madison-square-garden/calendar",
                   "https://www.msg.com/madison-square-garden",
                   "https://www.msg.com/calendar"],
    "SRC-000003": ["https://www.msg.com/radio-city-music-hall/calendar",
                   "https://www.msg.com/radio-city-music-hall"],
    "SRC-000005": ["https://www.msg.com/beacon-theatre/calendar",
                   "https://www.msg.com/beacon-theatre"],
    "SRC-000004": ["https://www.carnegiehall.org/Calendar"],
    "SRC-000006": ["https://www.irvingplaza.com/shows"],
    "SRC-000007": ["https://www.websterhall.com/shows"],
    "SRC-000008": ["https://www.bowerypresents.com/venues/brooklyn-steel"],
    "SRC-000009": ["https://www.terminal5nyc.com/shows"],
    "SRC-000012": ["https://www.bowerypresents.com/venues/music-hall-of-williamsburg"],
    "SRC-000016": ["https://www.unitedpalace.org/events"],
    "SRC-000018": ["https://www.bklynlibrary.org/calendar"],
}

CONFIGS = [
    ("wait5", {"browser_actions": [{"wait": "5s"}]}),
    ("wait+autoscroll+fullpage", {
        "browser_actions": [{"wait": "4s"}, {"auto_scroll": True}, {"wait": "2s"}],
        "markdown_backend": "full_page"}),
    ("stealth-vx10", {
        "driver": "vx10",
        "browser_actions": [{"wait": "5s"}, {"auto_scroll": True}, {"wait": "2s"}],
        "markdown_backend": "full_page"}),
]

_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b", re.IGNORECASE
)
_JUNK = ("# 404", "page could not be found", "showing 0 events", "access denied",
         "verify you are a human")


def looks_good(content: str | None) -> bool:
    if not content or len(content) < 1500:
        return False
    if any(m in content[:2000].lower() for m in _JUNK):
        return False
    return len(_DATE_RE.findall(content)) >= 3


async def tune(sid: str, urls: list[str], sem):
    tool = NimbleExtractTool()
    async with sem:
        for url in urls:
            for cname, cfg in CONFIGS:
                content = (await asyncio.to_thread(tool._run, url, cfg)).get("content")
                dates = len(_DATE_RE.findall(content or ""))
                print(f"[{sid}] {url} | {cname} -> {len(content or '')} chars, {dates} dates",
                      flush=True)
                if looks_good(content):
                    return sid, {"url": url, "config_name": cname, "options": cfg}
        return sid, None


async def main():
    sem = asyncio.Semaphore(4)
    results = await asyncio.gather(*(tune(s, u, sem) for s, u in TARGETS.items()))
    out = {sid: r for sid, r in results}
    print("\nRESULTS_JSON:", json.dumps(out))


asyncio.run(main())
