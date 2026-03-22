"""
Manages persistent game state: players, HP, inventory, location, and conversation history.
Each campaign is saved as its own JSON file under the campaigns/ directory.
"""
import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

CAMPAIGNS_DIR = "campaigns"
LEGACY_STATE_FILE = "game_state.json"


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Player:
    name: str
    discord_id: int
    character_name: str = "Unknown Adventurer"
    race: str = "Human"
    char_class: str = "Fighter"
    level: int = 1
    max_hp: int = 10
    current_hp: int = 10
    inventory: list = field(default_factory=list)
    notes: str = ""
    dndbeyond_id: str = ""
    # Extended tracking for DM constraint enforcement
    spells_known: list = field(default_factory=list)   # [{"name": str, "level": int}]
    spell_slots: dict = field(default_factory=dict)    # {"1": {"max": 4, "remaining": 4}, ...}
    class_features: list = field(default_factory=list) # [{"name": str, "max_uses": int, "remaining": int, "recharge": str}]
    actions_used: dict = field(default_factory=lambda: {"action": False, "bonus_action": False, "reaction": False})


@dataclass
class GameState:
    campaign_id: str = field(default_factory=_short_id)
    campaign_name: str = "The Lost Campaign"
    current_location: str = "a mysterious tavern"
    session_number: int = 1
    players: dict = field(default_factory=dict)
    conversation_history: list = field(default_factory=list)
    world_notes: str = ""
    campaign_active: bool = False
    last_played: str = ""
    human_dm_id: int = 0
    human_dm_name: str = ""

    # ── Internal ─────────────────────────────────────────────────────────────

    def _path(self) -> str:
        os.makedirs(CAMPAIGNS_DIR, exist_ok=True)
        return os.path.join(CAMPAIGNS_DIR, f"{self.campaign_id}.json")

    # ── Mutation helpers ──────────────────────────────────────────────────────

    def reset(self, campaign_name: str = "The Lost Campaign", location: str = "a mysterious tavern"):
        """Assign a fresh campaign ID and wipe all state."""
        self.campaign_id = _short_id()
        self.campaign_name = campaign_name
        self.current_location = location
        self.session_number = 1
        self.players = {}
        self.conversation_history = []
        self.world_notes = ""
        self.campaign_active = False
        self.last_played = ""
        self.human_dm_id = 0
        self.human_dm_name = ""

    def add_player(self, discord_id: int, discord_name: str):
        if str(discord_id) not in self.players:
            self.players[str(discord_id)] = asdict(Player(
                name=discord_name,
                discord_id=discord_id
            ))

    def get_player(self, discord_id: int) -> Optional[dict]:
        return self.players.get(str(discord_id))

    def update_player(self, discord_id: int, **kwargs):
        p = self.get_player(discord_id)
        if p:
            p.update(kwargs)

    def player_summary(self) -> str:
        if not self.players:
            return "No players registered."
        lines = []
        for p in self.players.values():
            lines.append(
                f"- {p['character_name']} ({p['race']} {p['char_class']} Lv{p['level']}) "
                f"HP: {p['current_hp']}/{p['max_hp']} | {p['name']}"
            )
        return "\n".join(lines)

    def constraint_summary(self) -> str:
        """Format per-player capability constraints for injection into the DM prompt."""
        if not self.players:
            return "No players registered."
        lines = []
        for p in self.players.values():
            lines.append(f"  {p['character_name']} ({p['race']} {p['char_class']} Lv{p['level']}):")
            lines.append(f"    HP: {p['current_hp']}/{p['max_hp']}")

            inv = p.get("inventory", [])
            lines.append(f"    Inventory: {', '.join(inv) if inv else 'nothing'}")

            spells = p.get("spells_known", [])
            if spells:
                spell_strs = [f"{s['name']} (Lv{s['level']})" for s in spells]
                lines.append(f"    Spells known: {', '.join(spell_strs)}")
            else:
                lines.append(f"    Spells known: none")

            slots = p.get("spell_slots", {})
            if slots:
                slot_parts = []
                for lvl in sorted(slots.keys(), key=int):
                    s = slots[lvl]
                    depleted = " (depleted)" if s["remaining"] == 0 else ""
                    slot_parts.append(f"Lv{lvl}: {s['remaining']}/{s['max']}{depleted}")
                lines.append(f"    Spell slots: {', '.join(slot_parts)}")

            features = p.get("class_features", [])
            if features:
                feat_parts = [
                    f"{f['name']} ({f['remaining']}/{f['max_uses']} uses)"
                    for f in features
                ]
                lines.append(f"    Class features: {', '.join(feat_parts)}")

            actions = p.get("actions_used", {})
            used = [k.replace("_", " ") for k, v in actions.items() if v]
            lines.append(f"    Actions used this turn: {', '.join(used) if used else 'none'}")
        return "\n".join(lines)

    def add_to_history(self, role: str, content: str, max_history: int = 40):
        self.conversation_history.append({"role": role, "parts": [{"text": content}]})
        if len(self.conversation_history) > max_history:
            self.conversation_history = self.conversation_history[-max_history:]

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self):
        self.last_played = datetime.now(timezone.utc).isoformat()
        with open(self._path(), "w") as f:
            json.dump({
                "campaign_id": self.campaign_id,
                "campaign_name": self.campaign_name,
                "current_location": self.current_location,
                "session_number": self.session_number,
                "players": self.players,
                "conversation_history": self.conversation_history,
                "world_notes": self.world_notes,
                "campaign_active": self.campaign_active,
                "last_played": self.last_played,
                "human_dm_id": self.human_dm_id,
                "human_dm_name": self.human_dm_name,
            }, f, indent=2)

    @classmethod
    def load_campaign(cls, campaign_id: str) -> "GameState":
        path = os.path.join(CAMPAIGNS_DIR, f"{campaign_id}.json")
        with open(path, "r") as f:
            data = json.load(f)
        gs = cls.__new__(cls)
        gs.campaign_id = data.get("campaign_id", campaign_id)
        gs.campaign_name = data.get("campaign_name", "The Lost Campaign")
        gs.current_location = data.get("current_location", "a mysterious tavern")
        gs.session_number = data.get("session_number", 1)
        gs.players = data.get("players", {})
        gs.conversation_history = data.get("conversation_history", [])
        gs.world_notes = data.get("world_notes", "")
        gs.campaign_active = data.get("campaign_active", False)
        gs.last_played = data.get("last_played", "")
        gs.human_dm_id = data.get("human_dm_id", 0)
        gs.human_dm_name = data.get("human_dm_name", "")
        return gs

    @classmethod
    def list_campaigns(cls) -> list[dict]:
        """Return summary dicts for all saved campaigns, sorted newest first."""
        if not os.path.exists(CAMPAIGNS_DIR):
            return []
        summaries = []
        for fname in os.listdir(CAMPAIGNS_DIR):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(CAMPAIGNS_DIR, fname), "r") as f:
                    data = json.load(f)
                summaries.append({
                    "id": data.get("campaign_id", fname[:-5]),
                    "name": data.get("campaign_name", "Unknown"),
                    "session_number": data.get("session_number", 1),
                    "player_count": len(data.get("players", {})),
                    "location": data.get("current_location", "unknown"),
                    "last_played": data.get("last_played", ""),
                    "campaign_active": data.get("campaign_active", False),
                })
            except Exception:
                continue
        summaries.sort(key=lambda x: x["last_played"], reverse=True)
        return summaries

    @classmethod
    def migrate_legacy(cls):
        """Move an old single-file game_state.json into the campaigns directory."""
        if not os.path.exists(LEGACY_STATE_FILE):
            return
        try:
            with open(LEGACY_STATE_FILE, "r") as f:
                data = json.load(f)
            gs = cls()
            gs.campaign_name = data.get("campaign_name", gs.campaign_name)
            gs.current_location = data.get("current_location", gs.current_location)
            gs.session_number = data.get("session_number", gs.session_number)
            gs.players = data.get("players", {})
            gs.conversation_history = data.get("conversation_history", [])
            gs.world_notes = data.get("world_notes", "")
            gs.campaign_active = data.get("campaign_active", False)
            gs.save()
            os.rename(LEGACY_STATE_FILE, LEGACY_STATE_FILE + ".migrated")
            print(f"[State] Migrated {LEGACY_STATE_FILE} → campaigns/{gs.campaign_id}.json")
        except Exception as e:
            print(f"[State] Legacy migration failed: {e}")

    @classmethod
    def load(cls) -> "GameState":
        """Load the most recently played campaign, or a blank state if none exist."""
        cls.migrate_legacy()
        campaigns = cls.list_campaigns()
        if campaigns:
            try:
                return cls.load_campaign(campaigns[0]["id"])
            except Exception:
                pass
        return cls()
