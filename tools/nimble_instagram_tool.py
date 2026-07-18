"""
Nimble Instagram tools — wrappers around Nimble's pre-built Instagram agents.

  NimbleInstagramProfileTool  — instagram_profile_by_account
  NimbleInstagramPostTool     — instagram_post
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

    parsing = response.data.parsing if response.data else None
    if parsing is None:
        return {}
    if hasattr(parsing, "model_dump"):
        return parsing.model_dump()
    if hasattr(parsing, "entities"):
        return parsing.entities or {}
    if isinstance(parsing, dict):
        return parsing
    return {}


# ---------------------------------------------------------------------------
# Instagram Profile
# ---------------------------------------------------------------------------

class NimbleInstagramProfileInput(BaseModel):
    profile: str = Field(description="Instagram username/handle without the @ (e.g. 'brooklynsteel')")


class NimbleInstagramProfileTool(BaseTool):
    name: str = "nimble_instagram_profile"
    description: str = (
        "Fetch an Instagram profile and recent posts via the Nimble Instagram Profile agent. "
        "Returns biography, followers, and a posts array with captions and metadata."
    )
    args_schema: Type[BaseModel] = NimbleInstagramProfileInput

    def _run(self, profile: str) -> dict[str, Any]:
        return self._fetch_with_retry(profile)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=lambda rs: logger.warning(
            f"Instagram profile retry {rs.attempt_number} for @{rs.args[1] if len(rs.args) > 1 else '?'}"
        ),
    )
    def _fetch_with_retry(self, profile: str) -> dict[str, Any]:
        logger.info(f"Nimble Instagram profile | @{profile}")
        data = _run_agent("instagram_profile_by_account", {"profile": profile})
        posts = data.get("posts") or []
        logger.info(f"Instagram profile @{profile} → {len(posts)} posts")
        return data

    async def _arun(self, profile: str) -> dict[str, Any]:
        return self._run(profile)


# ---------------------------------------------------------------------------
# Instagram Post
# ---------------------------------------------------------------------------

class NimbleInstagramPostInput(BaseModel):
    post_id: str = Field(description="Instagram post shortcode (e.g. 'CjnopuUIF1E')")


class NimbleInstagramPostTool(BaseTool):
    name: str = "nimble_instagram_post"
    description: str = (
        "Fetch full details for a single Instagram post via the Nimble Instagram Post agent. "
        "Returns description, hashtags, date_posted, location, and comments."
    )
    args_schema: Type[BaseModel] = NimbleInstagramPostInput

    def _run(self, post_id: str) -> dict[str, Any]:
        return self._fetch_with_retry(post_id)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=lambda rs: logger.warning(
            f"Instagram post retry {rs.attempt_number} for post={rs.args[1] if len(rs.args) > 1 else '?'}"
        ),
    )
    def _fetch_with_retry(self, post_id: str) -> dict[str, Any]:
        logger.info(f"Nimble Instagram post | {post_id}")
        data = _run_agent("instagram_post", {"post_id": post_id})
        return data

    async def _arun(self, post_id: str) -> dict[str, Any]:
        return self._run(post_id)
