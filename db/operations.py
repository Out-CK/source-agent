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
    def get_venue_coords_cache(self) -> dict: ...
    def get_entries_missing_coords(self) -> list[dict]: ...
    def get_entries_missing_media(self, limit: int = 50) -> list[dict]: ...
    def update_event_entry(self, event_entry_id: str, fields: dict) -> None: ...
    def get_past_entries(self) -> list[dict]: ...
    def insert_past_event_entry(self, entry: dict) -> None: ...
    def past_entry_exists(self, event_entry_id: str) -> bool: ...
    def delete_event_entry(self, event_entry_id: str) -> None: ...


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

    def _paged(self, query_builder):
        rows: list[dict] = []
        page, offset = 1000, 0
        while True:
            batch = query_builder.range(offset, offset + page - 1).execute().data or []
            rows.extend(batch)
            if len(batch) < page:
                return rows
            offset += page

    def get_venue_coords_cache(self) -> dict:
        rows = self._paged(
            self._sb.table("event_entry_database_v2")
            .select("venue, address, lat, lng")
            .not_.is_("lat", "null")
        )
        return {
            r["venue"]: (r["lat"], r["lng"], r.get("address") or "")
            for r in rows
            if r.get("venue")
        }

    def get_entries_missing_coords(self) -> list[dict]:
        return self._paged(
            self._sb.table("event_entry_database_v2")
            .select("event_entry_id, venue, address")
            .is_("lat", "null")
        )

    def get_entries_missing_media(self, limit: int = 50) -> list[dict]:
        res = (
            self._sb.table("event_entry_database_v2")
            .select("event_entry_id, artist, venue")
            .is_("media_url", "null")
            .limit(limit)
            .execute()
        )
        return res.data or []

    def update_event_entry(self, event_entry_id: str, fields: dict) -> None:
        self._sb.table("event_entry_database_v2").update(fields).eq(
            "event_entry_id", event_entry_id
        ).execute()

    def get_past_entries(self) -> list[dict]:
        from datetime import date as _date, datetime as _dt

        rows = self._paged(self._sb.table("event_entry_database_v2").select("*"))
        past = []
        for r in rows:
            try:
                d = _dt.strptime((r.get("date") or "").strip(), "%m-%d-%Y").date()
            except ValueError:
                continue
            if d < _date.today():
                past.append(r)
        return past

    def insert_past_event_entry(self, entry: dict) -> None:
        # Drop the primary key so the past table auto-assigns its own
        self._sb.table("past_event_entry_database").insert(
            {k: v for k, v in entry.items() if k != "id"}
        ).execute()

    def past_entry_exists(self, event_entry_id: str) -> bool:
        res = (
            self._sb.table("past_event_entry_database")
            .select("event_entry_id")
            .eq("event_entry_id", event_entry_id)
            .limit(1)
            .execute()
        )
        return bool(res.data)

    def delete_event_entry(self, event_entry_id: str) -> None:
        self._sb.table("event_entry_database_v2").delete().eq(
            "event_entry_id", event_entry_id
        ).execute()


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

    def get_venue_coords_cache(self) -> dict:
        return {
            e["venue"]: (e["lat"], e["lng"], e.get("address") or "")
            for e in self._db["event_entry_database_v2"]
            if e.get("venue") and e.get("lat") is not None
        }

    def get_entries_missing_coords(self) -> list[dict]:
        return [
            {k: e.get(k) for k in ("event_entry_id", "venue", "address")}
            for e in self._db["event_entry_database_v2"]
            if e.get("lat") is None
        ]

    def get_entries_missing_media(self, limit: int = 50) -> list[dict]:
        rows = [
            {k: e.get(k) for k in ("event_entry_id", "artist", "venue")}
            for e in self._db["event_entry_database_v2"]
            if not e.get("media_url")
        ]
        return rows[:limit]

    def update_event_entry(self, event_entry_id: str, fields: dict) -> None:
        for e in self._db["event_entry_database_v2"]:
            if e.get("event_entry_id") == event_entry_id:
                e.update(fields)
        self._save()

    def get_past_entries(self) -> list[dict]:
        from datetime import date as _date, datetime as _dt

        past = []
        for e in self._db["event_entry_database_v2"]:
            try:
                d = _dt.strptime((e.get("date") or "").strip(), "%m-%d-%Y").date()
            except ValueError:
                continue
            if d < _date.today():
                past.append(e)
        return past

    def insert_past_event_entry(self, entry: dict) -> None:
        self._db.setdefault("past_event_entry_database", []).append(
            {k: v for k, v in entry.items() if k != "id"}
        )
        self._save()

    def past_entry_exists(self, event_entry_id: str) -> bool:
        return any(
            e.get("event_entry_id") == event_entry_id
            for e in self._db.get("past_event_entry_database", [])
        )

    def delete_event_entry(self, event_entry_id: str) -> None:
        self._db["event_entry_database_v2"] = [
            e for e in self._db["event_entry_database_v2"]
            if e.get("event_entry_id") != event_entry_id
        ]
        self._save()


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
