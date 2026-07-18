import os
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from utils.logger import get_logger

logger = get_logger(__name__)



class NimbleExtractInput(BaseModel):
    url: str = Field(description="The URL to extract content from")


class NimbleExtractTool(BaseTool):
    name: str = "nimble_extract"
    description: str = (
        "Extract full page content from a URL using the Nimble Extract API. "
        "Returns a dict with 'url' and 'content'."
    )
    args_schema: Type[BaseModel] = NimbleExtractInput

    def _run(self, url: str, options: Optional[dict] = None) -> dict[str, Any]:
        try:
            return self._extract_with_retry(url, options or {})
        except Exception as e:
            logger.error(f"Nimble extract failed after all retries for {url}: {e}")
            return {"url": url, "content": None}

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=lambda retry_state: logger.warning(
            f"Nimble extract retry attempt {retry_state.attempt_number} "
            f"for URL: {retry_state.args[1] if len(retry_state.args) > 1 else '?'}"
        ),
    )
    def _extract_with_retry(self, url: str, options: dict) -> dict[str, Any]:
        from nimble_python import Nimble

        api_key = os.environ["NIMBLE_API_KEY"]
        nimble = Nimble(api_key=api_key)

        kwargs: dict[str, Any] = {
            "url": url,
            "render": True,
            "formats": ["markdown"],
        }
        # Stealth tier (vx8 | vx10 | vx10-pro | vx12 | vx12-pro) for bot-protected sites
        if options.get("driver"):
            kwargs["driver"] = options["driver"]
        # Sequential browser actions (waits, scrolls, clicks) for JS-driven calendars,
        # e.g. [{"wait": "5s"}, {"auto_scroll": true}]. A short post-render wait is the
        # default — it's what makes most JS calendars (MSG, Live Nation, Bowery) populate.
        kwargs["browser_actions"] = options.get("browser_actions", [{"wait": "4s"}])
        # full_page keeps listings that main-content extraction sometimes drops
        if options.get("markdown_backend"):
            kwargs["markdown_backend"] = options["markdown_backend"]
        if options.get("request_timeout"):
            kwargs["request_timeout"] = options["request_timeout"]

        logger.info(f"Nimble extract | url: {url}" + (f" | options: {options}" if options else ""))
        result = nimble.extract(**kwargs)
        content = result.data.markdown if result.data else None
        logger.info(f"Nimble extract {'succeeded' if content else 'returned empty'} for: {url}")
        return {"url": url, "content": content}

    async def _arun(self, url: str) -> dict[str, Any]:
        return self._run(url)
