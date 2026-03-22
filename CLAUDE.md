# CLAUDE.md — DnD Bot Project Context

## What This Is

A Discord bot that acts as an AI Dungeon Master for D&D 5e campaigns. It listens to players in a voice channel via speech-to-text, sends their input to Google Gemini, and speaks the DM's response back into voice using TTS. It enforces character constraints (spell lists, spell slots, class features, action economy) by injecting a full player capabilities block into every Gemini prompt.

---

## File Structure

```
DnD Bot/
├── bot.py            — Entry point. Loads all three cogs on ready.
├── dm_cog.py         — Main cog. Campaign management, session control, voice,
│                       Human DM private messaging, character registration, HP/inventory.
├── character_cog.py  — Spell, slot, class feature, action economy, rest, and level-up commands.
├── dice_cog.py       — Guided interactive dice rolling (!roll, !stats).
├── dm_brain.py       — Gemini API interface. DM responses, session opener, level-up info.
├── game_state.py     — All persistent state. Each campaign saved as campaigns/<id>.json.
├── voice_listener.py — Per-player audio capture + Google Cloud STT transcription.
├── tts.py            — gTTS text-to-speech → FFmpegOpusAudio for voice playback.
├── dndbeyond.py      — D&D Beyond character import (stats, spells, features, slots).
├── requirements.txt
├── .env.example
├── README.md
└── COMMANDS.txt
```

---

## Architecture Notes

- **State**: `GameState` is a dataclass saved as JSON per campaign under `campaigns/`. `DMCog` holds the active `GameState` instance (`self.state`). `CharacterCog` accesses it via `bot.cogs.get("DMCog").state`.
- **Gemini**: Every call to `get_dm_response()` injects a full game state + player capabilities block. `world_notes` (including Human DM notes and campaign backstory) are included in this block.
- **Voice pipeline**: `discord-ext-voice-recv` captures PCM per player → RMS silence detection cuts utterances → Google Cloud STT → Gemini → gTTS → FFmpegOpusAudio. An asyncio lock queues TTS playback so responses don't overlap.
- **Fallback**: If the bot isn't in voice, DM responses post as a Discord embed in the log channel.
- **Campaigns**: Multiple campaigns can be saved. Only one is active at a time. `!startcampaign` lets you switch between them.

---

## Features Built

### Campaign Management
- `!newcampaign` — 4-step guided setup: campaign name → starting location → backstory → Human DM assignment. Pauses and saves any active campaign first.
- `!startcampaign` — Lists saved campaigns with session number, player count, location, last played. Pick by number, type `new`, or `cancel`.
- `!stopcampaign` — Saves state, increments session counter, clears conversation history (world notes kept), DMs each player their full character sheet.

### Human DM (most recently added)
- A designated Human DM can privately message the bot to inject context and plot points that only the AI DM sees (stored in `world_notes` as `[Human DM] <note>`).
- Assigned during `!newcampaign` setup (step 4) via @mention or `skip`.
- `!humanDM` — shows who the current Human DM is.
- `!updateDM` — reassign or remove the Human DM.
- Human DM is stored on `GameState` as `human_dm_id` (int) and `human_dm_name` (str).

### Campaign Backstory
- Step 3 of `!newcampaign` prompts for free-form backstory (2-minute timeout). Saved to `world_notes` as `"Campaign Backstory:\n..."`. Useful for groups continuing a manual campaign with the bot.

### Character Registration
- `!register <dndbeyond_url>` — imports name, race, class, level, HP, spells, slots, and class features from D&D Beyond (sheet must be public).
- `!register <name> <race> <class>` — manual entry.
- Re-running `!register` with a D&D Beyond URL auto-detects level changes and fires the level-up announcement.

### Character Tracking (CharacterCog)
- `!hp set/damage/heal/show`
- `!inventory show/add/remove`
- `!spell add/remove/show/cast` — cast consumes the appropriate slot; cantrips are free.
- `!slot set/use/restore/show` — visual bar display.
- `!feature add/use/restore/show` — tracks recharge type (short/long/none).
- `!action use/reset/show` — per-turn action economy.
- `!rest short|long` — restores appropriate features, slots, HP.
- `!level up` / `!level set <n>` — Gemini generates full mechanical level-up breakdown.

### Session Commands
- `!join` / `!leave` — voice channel management.
- `!setchannel` — sets the text channel for dice rolls and DM log.
- `!startsession` — generates a fresh opening narration.
- `!dm <text>` — manual text input to the DM (for players without mic).
- `!location <place>` — updates location in DM context.
- `!note <lore>` — adds a persistent world note.
- `!party` — shows all registered players and their status.
- `!gamehelp` — in-Discord command reference embed.

### Dice (DiceCog + DMCog auto-roll)
- **Auto-roll** — after every DM response, `_parse_autoroll()` checks for an `[AUTOROLL: player=X, dice=NdX, type=adv|dis|normal]` tag injected by Gemini. If found, `_execute_autoroll()` rolls and posts a result embed to the log channel. For `player=all` (initiative), it rolls separately for every registered player. The tag is stripped from the text before TTS and before storing in conversation history.
- `!roll <die>` — guided 3-prompt manual roll: dice count → advantage/disadvantage/normal → modifier. Posts embed to text channel. Each prompt shows `!resetroll to cancel`.
- `!resetroll` — cancels an in-progress roll at any step. Detected inside `wait_for` by checking if the message starts with `!resetroll`. Standalone command responds gracefully if no roll is active.
- `!stats` — 4d6 drop lowest × 6.

### End-of-Session DMs
- On `!stopcampaign`, each registered player receives a Discord DM with their character sheet: HP, race/class/level, inventory, known spells, remaining spell slots (visual bar), class feature uses, notes, and D&D Beyond link.

---

## GameState Fields

```python
campaign_id: str          # unique hex ID, filename of save file
campaign_name: str
current_location: str
session_number: int
players: dict             # keyed by str(discord_id)
conversation_history: list  # Gemini format: [{role, parts: [{text}]}], capped at 40
world_notes: str          # persistent lore; includes backstory + [Human DM] notes
campaign_active: bool
last_played: str          # ISO timestamp
human_dm_id: int          # Discord user ID of the Human DM (0 if none)
human_dm_name: str
```

---

## Environment Variables (.env)

```
DISCORD_TOKEN
GEMINI_API_KEY
GOOGLE_APPLICATION_CREDENTIALS   # path to Google Cloud service account JSON (for STT)
```

---

## GitHub

Repository: https://github.com/torp23/dnd-bot
Branch: master (protected — no force push or deletion)
Owner: torp23

---

## Last Worked On

Added auto-roll system:
- Gemini system prompt updated to append `[AUTOROLL: player=X, dice=NdX, type=adv|dis|normal]` tags when calling for a roll.
- `_parse_autoroll()` and `_build_autoroll_embed()` added as module-level helpers in `dm_cog.py`.
- `_execute_autoroll()` method on `DMCog` rolls and posts embeds; handles `player=all` for initiative by rolling for every registered player.
- Tag is stripped before TTS (`speak_dm`) and before storing in Gemini conversation history.
- Wired into both `on_transcript()` and `manual_dm_input()`.
