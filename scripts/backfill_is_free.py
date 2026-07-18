"""
One-time backfill: classify existing upcoming events as free / paid / unknown.

Batches events through the LLM using title, type, venue, description, and
ticket-link presence. Only writes is_free when the model is confident
(true/false); unknown stays NULL.

  python scripts/backfill_is_free.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from db.supabase_client import get_supabase_client
from utils.logger import get_logger, setup_root_logger

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"
BATCH = 25


class FreeCall(BaseModel):
    event_entry_id: str
    is_free: Optional[bool] = Field(
        description="true only if clearly free (free admission/no cover/free with "
        "RSVP). false if clearly ticketed/priced. null if you cannot tell."
    )


class FreeCalls(BaseModel):
    calls: List[FreeCall]


PROMPT = """For each NYC event below, decide whether admission is FREE.

Signals: greenmarkets, gallery/exhibition viewings, street fairs, and open markets
are usually free; concerts/comedy/theater with a ticket link are usually paid.
Only answer true/false when the text supports it; otherwise null.

EVENTS:
{events}
"""


def main() -> None:
    setup_root_logger()
    sb = get_supabase_client()
    rows: list[dict] = []
    page, offset = 1000, 0
    while True:
        batch = (
            sb.table("event_entry_database_v2")
            .select("event_entry_id, event_title, event_type, venue, description, tickets_source_1")
            .is_("is_free", "null")
            .range(offset, offset + page - 1)
            .execute()
            .data
            or []
        )
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page

    logger.info(f"Backfill is_free: {len(rows)} events to classify")
    llm = ChatAnthropic(model=MODEL, max_tokens=4096).with_structured_output(FreeCalls)

    updated = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        blob = "\n\n".join(
            f"id: {r['event_entry_id']}\ntitle: {r['event_title']}\ntype: {r['event_type']}\n"
            f"venue: {r.get('venue')}\nhas_ticket_link: {bool(r.get('tickets_source_1'))}\n"
            f"description: {(r.get('description') or '')[:300]}"
            for r in chunk
        )
        try:
            result: FreeCalls = llm.invoke(
                [{"role": "user", "content": PROMPT.format(events=blob)}]
            )
        except Exception as e:
            logger.error(f"Batch {i // BATCH} failed: {e}")
            continue
        for call in result.calls:
            if call.is_free is None:
                continue
            sb.table("event_entry_database_v2").update({"is_free": call.is_free}).eq(
                "event_entry_id", call.event_entry_id
            ).execute()
            updated += 1
        logger.info(f"Batch {i // BATCH + 1}/{(len(rows) + BATCH - 1) // BATCH}: {updated} set so far")

    logger.info(f"Backfill done: {updated}/{len(rows)} classified (rest stay NULL/unknown)")


if __name__ == "__main__":
    main()
