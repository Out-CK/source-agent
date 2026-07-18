"""
Nimble TikTok tools — wrappers around Nimble's pre-built TikTok agents.

  NimbleTikTokHashtagTool  — tiktok_hashtag_feed_community_2026_04_30
  NimbleTikTokVideoTool    — tiktok_video_page
  NimbleTikTokAccountTool  — tiktok_account
"""
from __future__ import annotations

import os
from typing import Any, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from utils.logger import get_logger

logger = get_logger(__name__)


def _run_agent(agent_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run a Nimble pre-built agent via the nimble-python SDK."""
    from nimble_python import Nimble

    api_key = os.environ["NIMBLE_API_KEY"]
    nimble = Nimble(api_key=api_key)
    response = nimble.agent.run(agent=agent_name, params=params, timeout=90)

    # Structured output lives in response.data.parsing
    parsing = response.data.parsing if response.data else None
    if parsing is None:
        return {}
    # The SDK may return a Pydantic model or a plain dict
    if hasattr(parsing, "model_dump"):
        return parsing.model_dump()
    if hasattr(parsing, "entities"):
        return parsing.entities or {}
    if isinstance(parsing, dict):
        return parsing
    return {}


# ---------------------------------------------------------------------------
# Hashtag Feed
# ---------------------------------------------------------------------------

class NimbleTikTokHashtagInput(BaseModel):
    tag: str = Field(description="Hashtag text WITHOUT the leading #")


class NimbleTikTokHashtagTool(BaseTool):
    name: str = "nimble_tiktok_hashtag"
    description: str = (
        "Collect TikTok videos for a given hashtag via the Nimble TikTok Hashtag Feed agent. "
        "Returns a list of video records with caption, creator_handle, post_id, and engagement."
    )
    args_schema: Type[BaseModel] = NimbleTikTokHashtagInput

    def _run(self, tag: str) -> list[dict[str, Any]]:
        return self._fetch_with_retry(tag)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=lambda rs: logger.warning(
            f"TikTok hashtag retry {rs.attempt_number} for tag={rs.args[1] if len(rs.args) > 1 else '?'}"
        ),
    )
    def _fetch_with_retry(self, tag: str) -> list[dict[str, Any]]:
        logger.info(f"Nimble TikTok hashtag | #{tag}")
        data = _run_agent("tiktok_hashtag_feed_community_2026_04_30", {"tag": tag})
        items = data.get("items") or []
        logger.info(f"TikTok hashtag #{tag} → {len(items)} videos")
        return items

    async def _arun(self, tag: str) -> list[dict[str, Any]]:
        return self._run(tag)


# ---------------------------------------------------------------------------
# Video Page
# ---------------------------------------------------------------------------

class NimbleTikTokVideoInput(BaseModel):
    video_id: str = Field(description="TikTok post ID (numeric string)")
    account_id: str = Field(description="Creator handle (e.g. 'ldanarad')")


class NimbleTikTokVideoTool(BaseTool):
    name: str = "nimble_tiktok_video"
    description: str = (
        "Fetch full details for a single TikTok video page via the Nimble TikTok Video Page agent. "
        "Returns description, comments, URLs, and creator metadata."
    )
    args_schema: Type[BaseModel] = NimbleTikTokVideoInput

    def _run(self, video_id: str, account_id: str) -> dict[str, Any]:
        return self._fetch_with_retry(video_id, account_id)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=lambda rs: logger.warning(
            f"TikTok video page retry {rs.attempt_number}"
        ),
    )
    def _fetch_with_retry(self, video_id: str, account_id: str) -> dict[str, Any]:
        logger.info(f"Nimble TikTok video | video_id={video_id} account_id={account_id}")
        data = _run_agent("tiktok_video_page", {"video_id": video_id, "account_id": account_id})
        return data

    async def _arun(self, video_id: str, account_id: str) -> dict[str, Any]:
        return self._run(video_id, account_id)


# ---------------------------------------------------------------------------
# Account / Profile (returns top_posts_data with descriptions)
# ---------------------------------------------------------------------------

class NimbleTikTokAccountInput(BaseModel):
    account_id: str = Field(description="TikTok handle without the @ (e.g. 'brooklynsteel')")


class NimbleTikTokAccountTool(BaseTool):
    name: str = "nimble_tiktok_account"
    description: str = (
        "Fetch a TikTok account's profile and recent posts via the Nimble TikTok Account agent. "
        "Returns top_posts_data with descriptions, hashtags, post_url, and create_date."
    )
    args_schema: Type[BaseModel] = NimbleTikTokAccountInput

    def _run(self, account_id: str) -> dict[str, Any]:
        return self._fetch_with_retry(account_id)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=lambda rs: logger.warning(
            f"TikTok account retry {rs.attempt_number} for account={rs.args[1] if len(rs.args) > 1 else '?'}"
        ),
    )
    def _fetch_with_retry(self, account_id: str) -> dict[str, Any]:
        logger.info(f"Nimble TikTok account | @{account_id}")
        data = _run_agent("tiktok_account", {"account_id": account_id})
        posts = data.get("top_posts_data") or []
        logger.info(f"TikTok account @{account_id} → {len(posts)} posts")
        return data

    async def _arun(self, account_id: str) -> dict[str, Any]:
        return self._run(account_id)
