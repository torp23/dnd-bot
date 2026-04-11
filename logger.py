"""
Centralised logging setup for the DnD bot.
Call setup_logging() once at startup (bot.py).
Every other module gets its own logger via:
    import logging
    logger = logging.getLogger(__name__)
"""
import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger to write structured lines to stdout (captured by Docker)."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)-8s] %(name)-20s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )

    # Silence noisy discord.py internals — keep WARNING+ only
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.voice_client").setLevel(logging.WARNING)

    # gTTS and urllib3 are chatty too
    logging.getLogger("gtts").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
