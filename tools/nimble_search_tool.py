import os
from typing import Any, Literal, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from utils.logger import get_logger

logger = get_logger(__name__)


class NimbleSearchInput(BaseModel):
    query: str = Field(description="The search query string")
    query_type: Literal["broad", "niche"] = Field(
        description="'broad' for general searches (max_results=10), 'niche' for specific (max_results=5)"
    )


class NimbleSearchTool(BaseTool):
    name: str = "nimble_search"
    description: str = (
        "Search the web using the Nimble Search API. Returns a list of pages with url, title, and content."
    )
    args_schema: Type[BaseModel] = NimbleSearchInput

    def _run(self, query: str, query_type: Literal["broad", "niche"] = "broad") -> list[dict[str, Any]]:
        return self._search_with_retry(query, query_type)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=lambda retry_state: logger.warning(
            f"Nimble search retry attempt {retry_state.attempt_number} "
            f"for query: {retry_state.args[1] if len(retry_state.args) > 1 else '?'}"
        ),
    )
    def _search_with_retry(self, query: str, query_type: str) -> list[dict[str, Any]]:
        from nimble_python import Nimble

        api_key = os.environ["NIMBLE_API_KEY"]
        nimble = Nimble(api_key=api_key)

        max_results = 5
        logger.info(f"Nimble search | query_type={query_type} max_results={max_results} | query: {query!r}")

        result = nimble.search(
            query=query,
            max_results=max_results,
        )

        pages = []
        for item in result.results or []:
            # SDK uses .description for snippet; .content may also be present
            content = getattr(item, "content", None) or getattr(item, "description", "") or ""
            pages.append({
                "url": getattr(item, "url", ""),
                "title": getattr(item, "title", ""),
                "content": content,
            })

        logger.info(f"Nimble search returned {len(pages)} results for: {query!r}")
        return pages

    async def _arun(self, query: str, query_type: Literal["broad", "niche"] = "broad") -> list[dict[str, Any]]:
        return self._run(query, query_type)
