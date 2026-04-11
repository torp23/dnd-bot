"""
Persists server-level settings (log channel, voice channel) across restarts.
Saved as server_config.json in the project root.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

CONFIG_FILE = "server_config.json"


class ServerConfig:
    def __init__(self):
        self.log_channel_id: int = 0
        self.voice_channel_id: int = 0

    def save(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump({
                    "log_channel_id": self.log_channel_id,
                    "voice_channel_id": self.voice_channel_id,
                }, f, indent=2)
            logger.info(
                f"[Config] Saved — log_channel_id={self.log_channel_id}, "
                f"voice_channel_id={self.voice_channel_id}"
            )
        except Exception as e:
            logger.error(f"[Config] Failed to save {CONFIG_FILE}: {e}", exc_info=True)

    @classmethod
    def load(cls) -> "ServerConfig":
        cfg = cls()
        if not os.path.exists(CONFIG_FILE):
            logger.info("[Config] No server_config.json found — using defaults")
            return cfg
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            cfg.log_channel_id = data.get("log_channel_id", 0)
            cfg.voice_channel_id = data.get("voice_channel_id", 0)
            logger.info(
                f"[Config] Loaded — log_channel_id={cfg.log_channel_id}, "
                f"voice_channel_id={cfg.voice_channel_id}"
            )
        except Exception as e:
            logger.error(f"[Config] Failed to load {CONFIG_FILE}: {e}", exc_info=True)
        return cfg
