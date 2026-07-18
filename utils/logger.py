import logging
import os
import sys


def get_logger(name: str) -> logging.Logger:
    """Get a configured logger for a module."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
        )
        logger.addHandler(handler)
        logger.propagate = False
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        logger.setLevel(getattr(logging, level, logging.INFO))
    return logger


def setup_root_logger() -> None:
    """Configure the root logger once at startup."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        stream=sys.stdout,
    )
