import os
from typing import Any, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from utils.logger import get_logger

logger = get_logger(__name__)


class NimbleMediaTool:
    """Nimble /media API: fetch a URL's rendered content (bypasses bot walls).

    Used to pull an event page's HTML so og:image / twitter:image / JSON-LD
    image tags can be read directly — the page's own artwork beats any
    image-search guess.
    """

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10),
        reraise=True,
        before_sleep=lambda rs: logger.warning(
            f"Nimble media retry {rs.attempt_number} for "
            f"{rs.args[1] if len(rs.args) > 1 else '?'}"
        ),
    )
    def fetch(self, url: str) -> Optional[str]:
        from nimble_python import Nimble

        nimble = Nimble(api_key=os.environ["NIMBLE_API_KEY"])
        result: Any = nimble.media.run(url=url)
        if isinstance(result, str) and result.strip():
            return result
        content = getattr(result, "content", None)
        return content if isinstance(content, str) and content.strip() else None
