"""
Past Event Archiver — pure data logic, no LLM.

Moves event_entry_database_v2 rows whose date has passed into
past_event_entry_database. Insert-then-delete per row so a failed insert
never loses data.
"""
from __future__ import annotations

from db.operations import RegistryStore
from utils.logger import get_logger

logger = get_logger(__name__)


class PastEventArchiver:
    def __init__(self, store: RegistryStore):
        self._store = store

    def run(self) -> int:
        past = self._store.get_past_entries()
        if not past:
            logger.info("Archiver: no past events to archive")
            return 0

        logger.info(f"Archiver: moving {len(past)} past entries")
        archived = 0
        for entry in past:
            eid = entry.get("event_entry_id", "?")
            try:
                self._store.insert_past_event_entry(entry)
            except Exception as e:
                # Already archived by an earlier run: safe to delete the live row —
                # but only after confirming it really exists in the past table.
                if "duplicate key" in str(e) and self._store.past_entry_exists(eid):
                    logger.info(f"Archiver: {eid} already in past DB — deleting live row")
                else:
                    logger.error(f"Archiver: insert failed for {eid} — skipping delete: {e}")
                    continue
            try:
                self._store.delete_event_entry(eid)
                archived += 1
            except Exception as e:
                logger.error(f"Archiver: delete failed for {eid} (now duplicated in past DB): {e}")

        logger.info(f"Archiver: archived {archived} entries")
        return archived
