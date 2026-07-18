"""
Registry store — all reads/writes for the source registry, search history,
and scraped content.

Two backends behind one interface:
  SupabaseStore — production (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)
  DryRunStore   — local JSON file, for testing the full pipeline with no DB
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.logger import get_logger
from utils.url_normalizer import normalize_url

logger = get_logger(__name__)

_TABLES = ("source_registry", "discovery_search_history", "source_web_content",
           "event_entry_database_v2")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RegistryStore:
    """Interface — see SupabaseStore / DryRunStore."""

    def all_registry_urls(self) -> set[str]: ...
    def insert_source(self, row: dict) -> Optional[dict]: ...
    def get_recent_queries(self, limit: int = 300) -> list[dict]: ...
    def insert_search_history(self, rows: list[dict]) -> None: ...
    def get_sources_due_for_scrape(self) -> list[dict]: ...
    def update_source(self, source_id: str, fields: dict) -> None: ...
    def insert_scraped_content(self, row: dict) -> None: ...
    def next_source_id(self) -> str: ...
    def get_unparsed_content(self, limit: Optional[int] = None) -> list[dict]: ...
    def mark_content_parsed(self, row_ids: list) -> None: ...
    def insert_event_entries(self, entries: list[dict]) -> None: ...
    def get_existing_future_entries(self) -> list[dict]: ...
    def next_event_entry_id(self) -> str: ...


class SupabaseStore(RegistryStore):
    def __init__(self):
        from db.supabase_client import get_supabase_client

        self._sb = get_supabase_client()

    def all_registry_urls(self) -> set[str]:
        res = self._sb.table("source_registry").select("normalized_url").execute()
        return {r["normalized_url"] for r in res.data}

    def insert_source(self, row: dict) -> Optional[dict]:
        try:
            res = self._sb.table("source_registry").insert(row).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            # UNIQUE violation on normalized_url means a concurrent/duplicate add — fine
            logger.warning(f"insert_source skipped for {row.get('url')}: {e}")
            return None

    def get_recent_queries(self, limit: int = 300) -> list[dict]:
        res = (
            self._sb.table("discovery_search_history")
            .select("query, angle, intent, sources_added, candidates_found")
            .order("executed_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    def insert_search_history(self, rows: list[dict]) -> None:
        if rows:
            self._sb.table("discovery_search_history").insert(rows).execute()

    def get_sources_due_for_scrape(self) -> list[dict]:
        res = (
            self._sb.table("source_registry")
            .select("*")
            .eq("status", "active")
            .execute()
        )
        return [s for s in res.data if _is_due(s)]

    def update_source(self, source_id: str, fields: dict) -> None:
        fields = {**fields, "updated_at": _now()}
        self._sb.table("source_registry").update(fields).eq("source_id", source_id).execute()

    def insert_scraped_content(self, row: dict) -> None:
        self._sb.table("source_web_content").insert(row).execute()

    def next_source_id(self) -> str:
        res = (
            self._sb.table("source_registry")
            .select("source_id")
            .order("source_id", desc=True)
            .limit(1)
            .execute()
        )
        last = int(res.data[0]["source_id"].split("-")[1]) if res.data else 0
        return f"SRC-{last + 1:06d}"

    def get_unparsed_content(self, limit: Optional[int] = None) -> list[dict]:
        q = (
            self._sb.table("source_web_content")
            .select("id, source_id, url, categories, content")
            .eq("parsed", False)
            .order("scraped_at")
        )
        if limit:
            q = q.limit(limit)
        return q.execute().data

    def mark_content_parsed(self, row_ids: list) -> None:
        for rid in row_ids:
            self._sb.table("source_web_content").update({"parsed": True}).eq("id", rid).execute()

    def insert_event_entries(self, entries: list[dict]) -> None:
        if entries:
            self._sb.table("event_entry_database_v2").insert(entries).execute()

    def get_existing_future_entries(self) -> list[dict]:
        # date is a text MM-DD-YYYY column, so >= comparisons in SQL are unreliable;
        # fetch the dedup columns and filter by real dates in the parser.
        # Paginate past PostgREST's 1000-row cap or the dedup index silently truncates.
        rows: list[dict] = []
        page = 1000
        offset = 0
        while True:
            res = (
                self._sb.table("event_entry_database_v2")
                .select("event_entry_id, artist, venue, date, start_time")
                .range(offset, offset + page - 1)
                .execute()
            )
            batch = res.data or []
            rows.extend(batch)
            if len(batch) < page:
                return rows
            offset += page

    def next_event_entry_id(self) -> str:
        return self._sb.rpc("next_event_entry_id").execute().data


class DryRunStore(RegistryStore):
    """File-backed store mirroring the Supabase tables, for --dry-run."""

    def __init__(self, path: str = "dry_run_db.json"):
        self._path = Path(path)
        if self._path.exists():
            self._db = json.loads(self._path.read_text())
        else:
            self._db = {}
        for t in _TABLES:
            self._db.setdefault(t, [])
        logger.info(f"DryRunStore using {self._path.resolve()}")

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._db, indent=2, default=str))

    def all_registry_urls(self) -> set[str]:
        return {r["normalized_url"] for r in self._db["source_registry"]}

    def insert_source(self, row: dict) -> Optional[dict]:
        if row["normalized_url"] in self.all_registry_urls():
            return None
        row = {**row, "created_at": _now(), "updated_at": _now()}
        self._db["source_registry"].append(row)
        self._save()
        return row

    def get_recent_queries(self, limit: int = 300) -> list[dict]:
        return list(reversed(self._db["discovery_search_history"][-limit:]))

    def insert_search_history(self, rows: list[dict]) -> None:
        for r in rows:
            self._db["discovery_search_history"].append({**r, "executed_at": _now()})
        self._save()

    def get_sources_due_for_scrape(self) -> list[dict]:
        return [
            s for s in self._db["source_registry"] if s.get("status") == "active" and _is_due(s)
        ]

    def update_source(self, source_id: str, fields: dict) -> None:
        for s in self._db["source_registry"]:
            if s["source_id"] == source_id:
                s.update(fields, updated_at=_now())
        self._save()

    def insert_scraped_content(self, row: dict) -> None:
        next_id = len(self._db["source_web_content"]) + 1
        self._db["source_web_content"].append({**row, "id": next_id, "scraped_at": _now()})
        self._save()

    def next_source_id(self) -> str:
        ids = [
            int(s["source_id"].split("-")[1])
            for s in self._db["source_registry"]
            if s.get("source_id", "").startswith("SRC-")
        ]
        return f"SRC-{(max(ids) if ids else 0) + 1:06d}"

    def get_unparsed_content(self, limit: Optional[int] = None) -> list[dict]:
        rows = [r for r in self._db["source_web_content"] if not r.get("parsed")]
        return rows[:limit] if limit else rows

    def mark_content_parsed(self, row_ids: list) -> None:
        for r in self._db["source_web_content"]:
            if r.get("id") in row_ids:
                r["parsed"] = True
        self._save()

    def insert_event_entries(self, entries: list[dict]) -> None:
        for e in entries:
            self._db["event_entry_database_v2"].append({**e, "created_at": _now()})
        self._save()

    def get_existing_future_entries(self) -> list[dict]:
        return [
            {k: e.get(k) for k in ("event_entry_id", "artist", "venue", "date", "start_time")}
            for e in self._db["event_entry_database_v2"]
        ]

    def next_event_entry_id(self) -> str:
        self._entry_id_counter = getattr(
            self, "_entry_id_counter", len(self._db["event_entry_database_v2"])
        ) + 1
        return f"{self._entry_id_counter:012d}"


def _is_due(source: dict) -> bool:
    last = source.get("last_scraped_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    freq_hours = source.get("scrape_frequency_hours") or 24
    return (datetime.now(timezone.utc) - last_dt).total_seconds() >= freq_hours * 3600


def get_store(dry_run: bool = False) -> RegistryStore:
    if dry_run or os.getenv("DRY_RUN", "").lower() in ("1", "true"):
        return DryRunStore()
    return SupabaseStore()


def build_source_row(
    *,
    source_id: str,
    name: str,
    url: str,
    source_type: str,
    categories: list[str],
    status: str,
    discovery_method: str,
    discovered_by_query: str = "",
    discovery_run_id: str = "",
    validation_notes: str = "",
    has_jsonld: Optional[bool] = None,
    scrape_method: Optional[str] = None,
    scrape_frequency_hours: int = 24,
) -> dict:
    return {
        "source_id": source_id,
        "name": name,
        "url": url,
        "normalized_url": normalize_url(url),
        "source_type": source_type,
        "categories": categories,
        "status": status,
        "discovery_method": discovery_method,
        "discovered_by_query": discovered_by_query,
        "discovery_run_id": discovery_run_id,
        "validation_notes": validation_notes,
        "has_jsonld": has_jsonld,
        "scrape_method": scrape_method,
        "scrape_frequency_hours": scrape_frequency_hours,
        "consecutive_failures": 0,
    }
