"""
Persists server-level settings (log channel, voice channel) across restarts.
Saved as server_config.json in the project root.
"""
import json
import os

CONFIG_FILE = "server_config.json"


class ServerConfig:
    def __init__(self):
        self.log_channel_id: int = 0
        self.voice_channel_id: int = 0

    def save(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump({
                "log_channel_id": self.log_channel_id,
                "voice_channel_id": self.voice_channel_id,
            }, f, indent=2)

    @classmethod
    def load(cls) -> "ServerConfig":
        cfg = cls()
        if not os.path.exists(CONFIG_FILE):
            return cfg
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            cfg.log_channel_id = data.get("log_channel_id", 0)
            cfg.voice_channel_id = data.get("voice_channel_id", 0)
        except Exception as e:
            print(f"[Config] Failed to load server_config.json: {e}")
        return cfg
