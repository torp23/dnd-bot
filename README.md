# DnD Discord Bot — Gemini-Powered Dungeon Master

A Discord bot that joins your voice channel, listens to your players via speech-to-text, uses Google Gemini to DM your campaign in real time, and **speaks the DM's response back into the voice channel**. The DM enforces character constraints — if a player tries to cast a spell they don't know or uses an action they've already spent, Gemini calls it out in-character. Dice rolls are posted to a text channel. Supports D&D Beyond character sheet import with automatic spell, feature, and slot syncing.

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A Discord bot token
- A Google Gemini API key (free at https://aistudio.google.com)
- A Google Cloud project with Speech-to-Text API enabled (for voice input)
- **FFmpeg** installed and on your system PATH (required for bot voice output)

**Install FFmpeg:**
```bash
# Windows (via winget)
winget install ffmpeg

# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

> **Note:** `discord-ext-voice-recv` requires `discord.py[voice]`. If you get errors, also run:
> ```bash
> pip install "discord.py[voice]"
> ```

### 3. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `DISCORD_TOKEN` — from https://discord.com/developers/applications
- `GEMINI_API_KEY` — from https://aistudio.google.com/app/apikey
- `GOOGLE_APPLICATION_CREDENTIALS` — path to your Google Cloud service account JSON

### 4. Discord Bot Permissions

When creating your bot in the Discord Developer Portal:
- **Bot Intents:** Enable `Message Content Intent` and `Server Members Intent`
- **OAuth2 Scopes:** `bot`
- **Bot Permissions:** `Send Messages`, `Embed Links`, `Connect`, `Speak`, `Use Voice Activity`

> The bot also sends direct messages to players at session end. Make sure it is not blocked by the server's DM settings.

### 5. Google Cloud Speech-to-Text

1. Go to https://console.cloud.google.com
2. Create a project and enable the **Speech-to-Text API**
3. Create a service account, download the JSON key
4. Set the path in `.env` as `GOOGLE_APPLICATION_CREDENTIALS`

> **Free tier:** Google STT gives 60 minutes/month free. For a prototype that's plenty.

### 6. Run the Bot

```bash
python bot.py
```

---

## Usage

### Campaign Management

Campaigns are saved as individual files and can be resumed across sessions.

```
!newcampaign      — Create a new campaign (existing campaigns are preserved)
!startcampaign    — Show saved campaigns and start or resume one
!stopcampaign     — Pause the campaign, save progress, and send session summaries to players
```

**`!newcampaign` / `!startcampaign` setup flow:**

Both commands walk through a 3-step campaign setup when creating a new campaign:

1. **Campaign name** — what the campaign is called
2. **Starting location** — where the adventure begins (e.g. *"a foggy dockside tavern in Waterdeep"*)
3. **Backstory** *(optional, 2-minute window)* — free-form context for the DM AI: setting details, past events, villain motivations, party history, etc. Ideal for groups continuing a manual campaign with the bot. Type `skip` or leave blank to start fresh.

The backstory is stored as a world note and included in every DM prompt, so the AI has full context from the very first scene.

**`!startcampaign` flow:**
- If no campaigns exist, the bot walks you through the 3-step setup above
- If campaigns exist, it shows a numbered list — pick one to resume, or type `new` to create another
- The bot joins your voice channel and speaks the opening narration

**`!stopcampaign` flow:**
- Saves the campaign, increments the session counter, clears conversation history (world notes kept)
- Bot leaves voice
- **Sends each registered player a direct message** with their current character sheet, including HP, inventory, spell slots, class feature uses, and a link to their D&D Beyond character if one is linked — so they can update their sheet between sessions

### Registering a Character

**Option A — Import from D&D Beyond (recommended):**
```
!register https://www.dndbeyond.com/characters/12345678
```
The bot fetches the character sheet and imports:
- Name, race, class, level, HP
- All known spells and remaining spell slots
- Class features with limited uses

The character is permanently tied to your Discord account. Re-run at any time to re-sync (e.g. after levelling up — the bot will automatically detect and announce the level change).

> The character sheet must be set to **public** on D&D Beyond:
> Character Sheet → Share → Anyone with the link

**Option B — Manual entry:**
```
!register Thorin Dwarf Fighter
```
Then set your HP: `!hp set 45`

### Starting a Session

1. Everyone joins the voice channel
2. Type `!startcampaign` — bot joins, narrates the opening, and begins listening
3. Players speak naturally — the bot transcribes and the DM responds in voice
4. Use `!dm <text>` for players without a mic or to test

### During Play

**DM interaction**
- Players speak in voice → bot transcribes → Gemini responds → bot speaks in voice
- `!dm <text>` — send text input to the DM (spoken back in voice)
- `!note The king is secretly a vampire` — add lore the DM will always remember
- `!location The Underdark` — update the current location in DM context

**Dice (posted to text channel)**
```
!roll d20
```
The bot walks you through three prompts:
1. How many dice?
2. Advantage / Disadvantage / Normal
3. Modifier (e.g. `+3`, `-2`, `0`)

```
!stats   — Roll full ability score set (4d6 drop lowest x6)
```

**HP**
```
!hp set 30       — Set max and current HP
!hp damage 8     — Take damage
!hp heal 4       — Heal
!hp show         — Show current HP
```

**Inventory**
```
!inventory show
!inventory add Longsword
!inventory remove Longsword
```

**Party overview**
```
!party   — Show all party members, HP, class, and current location
```

**Help**
```
!gamehelp   — Show all commands
```

---

## Character Constraint Enforcement

The Gemini DM receives a full **Player Capabilities** block in every prompt listing each character's exact state. If a player attempts something impossible, the DM responds in-character:

- Casting a spell not in their spell list → redirected
- Casting when all spell slots at that level are depleted → narrated failure
- Using a class feature with no uses remaining → reminded of recharge condition
- Spending an action type (action / bonus action / reaction) already used this turn → blocked

### Spell Management
```
!spell add <level> <name>          — Learn a spell (level 0 = cantrip)
!spell remove <name>               — Forget a spell
!spell show                        — List all known spells by level
!spell cast <name> [slot_level]    — Cast a spell and consume the appropriate slot
```

### Spell Slot Tracking
```
!slot set <level> <max>    — Set maximum slots for a spell level
!slot use <level>          — Manually consume a slot
!slot restore <level|all>  — Restore slots
!slot show                 — Visual slot bar for all levels
```

### Class Feature Tracking
```
!feature add <max_uses> <recharge> <name>   — Add a feature (recharge: short/long/none)
!feature use <name>                          — Expend one use
!feature restore <name|short|long|all>       — Restore uses
!feature show                                — List all features with use bars
```
Example: `!feature add 1 short Action Surge`

### Action Economy
```
!action use <action|bonus|reaction>   — Mark an action type as spent this turn
!action reset                          — New turn — restore all action types
!action show                           — View current action economy
```

### Resting
```
!rest short   — Restore short-rest features, reset action economy
!rest long    — Restore all spell slots, all features, full HP, action economy
```

---

## Level-Up Notifications

When a character gains a level, the bot posts an announcement embed to the log channel showing exactly what they gain at that level (features, spell slot table changes, proficiency bonus increases, hit die) — pulled live from Gemini.

```
!level up        — Gain one level with full breakdown
!level set <n>   — Set level directly (announces if levelling up)
```

Level-ups are also detected automatically when a player re-syncs their D&D Beyond sheet with `!register <url>`.

**Example announcement:**
```
⬆ Level Up!
Thorin has reached Level 6! — Hill Dwarf Fighter

What's new at Level 6
• Extra Attack: may attack twice per Attack action
• Ability Score Improvement or feat
• Hit die to roll: 1d10 + CON modifier

Next steps
Roll your hit die and use !hp set <new_max> to update HP.
Use !slot set <level> <max> to update spell slots if your table changed.
Use !feature add for any new limited-use features.
```

---

## End-of-Session Character Sheet DMs

When `!stopcampaign` is confirmed, the bot sends each registered player a direct message with their full current character sheet so they can update D&D Beyond between sessions.

The DM includes:
- Link to their D&D Beyond character (if linked)
- Current HP, race, class, level
- Full inventory
- Known spells organised by level
- Remaining spell slots (visual bar)
- Class feature uses remaining
- Any notes

Players who have DMs disabled are reported back in the channel.

---

## Architecture

```
Players speak in voice channel
       |
discord-ext-voice-recv captures PCM audio per-player
       |
RMS-based silence detection cuts audio into utterances
       |
Google Cloud Speech-to-Text transcribes speech to text
       |
Transcript + game state + player capabilities sent to Google Gemini
       |
Gemini returns DM narration (enforcing character constraints)
       |
gTTS converts text to MP3 → FFmpeg decodes → bot speaks in voice channel
       |
Dice roll commands (!roll, !stats) → embed posted to text channel only
       |
!stopcampaign → character sheet DM sent to each player
```

**Voice output:** DM responses are queued with an asyncio lock — simultaneous player speech plays back-to-back rather than overlapping. If the bot is not in a voice channel, responses fall back to a text embed.

**Campaigns:** Each campaign is saved as its own JSON file under `campaigns/`. An existing `game_state.json` from older versions is automatically migrated on first run.

---

## File Structure

```
dnd_bot/
├── bot.py              # Entry point — loads all cogs
├── dm_brain.py         # Gemini API: DM responses, level-up info, session opening
├── game_state.py       # All persistent state per campaign (campaigns/<id>.json)
├── voice_listener.py   # Per-player audio capture + Google Cloud STT
├── tts.py              # Text-to-speech (gTTS → FFmpegOpusAudio)
├── dndbeyond.py        # D&D Beyond character import (stats, spells, features)
├── dm_cog.py           # Campaign, session, character, and DM commands
├── dice_cog.py         # Guided interactive dice rolling
├── character_cog.py    # Spells, slots, features, actions, rest, level-up
├── requirements.txt
└── .env.example
```

---

## Swapping to Claude API (later)

When you're ready to upgrade from Gemini to Claude, edit `dm_brain.py`:

```python
# Replace google-generativeai with:
import anthropic
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Replace generate_content() calls with:
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    system=SYSTEM_PROMPT,
    messages=state.conversation_history  # adjust format: role/content vs role/parts
)
reply = response.content[0].text
```

The conversation history format needs a small tweak — Claude uses `{"role": "user/assistant", "content": "..."}` vs Gemini's `parts` format. Everything else stays the same.
