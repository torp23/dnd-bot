"""
DM Cog — the main orchestrator.
Handles joining voice, processing transcripts, and all DM-related commands.
"""
import asyncio
import random
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands
from game_state import GameState
from dm_brain import get_dm_response, start_session, announce_level_up
from tts import generate_tts_source
from dndbeyond import fetch_ddb_character
from voice_listener import DnDVoiceSink
from server_config import ServerConfig
import os

# Try to import voice_recv; warn if missing
try:
    from discord.ext import voice_recv
    VOICE_RECV_AVAILABLE = True
except ImportError:
    VOICE_RECV_AVAILABLE = False
    print("⚠️  discord-ext-voice-recv not installed. Voice listening disabled.")


_AUTOROLL_RE = re.compile(
    r'\[AUTOROLL:\s*player=([^,\]]+),\s*dice=(\d+)d(\d+),\s*type=(adv|dis|normal)\]',
    re.IGNORECASE,
)
_AUTOROLL_STRIP_RE = re.compile(r'\[AUTOROLL:[^\]]*\]', re.IGNORECASE)
_VALID_DIE_SIZES = {4, 6, 8, 10, 12, 20, 100}


def _parse_autoroll(text: str):
    """Return (player, num_dice, die_size, roll_type) or None if no tag found."""
    m = _AUTOROLL_RE.search(text)
    if not m:
        return None
    player = m.group(1).strip()
    num_dice = max(1, min(100, int(m.group(2))))
    die_size = int(m.group(3))
    roll_type = m.group(4).lower()
    if die_size not in _VALID_DIE_SIZES:
        return None
    return player, num_dice, die_size, roll_type


def _build_autoroll_embed(player_name: str, num_dice: int, die_size: int, roll_type: str) -> discord.Embed:
    """Roll dice and return a result embed."""
    if roll_type in ("adv", "dis"):
        set_a = [random.randint(1, die_size) for _ in range(num_dice)]
        set_b = [random.randint(1, die_size) for _ in range(num_dice)]
        total_a, total_b = sum(set_a), sum(set_b)
        if roll_type == "adv":
            kept, dropped = (set_a, set_b) if total_a >= total_b else (set_b, set_a)
            color, label = 0x2ecc71, "Advantage"
        else:
            kept, dropped = (set_a, set_b) if total_a <= total_b else (set_b, set_a)
            color, label = 0xe67e22, "Disadvantage"
        total = sum(kept)
        embed = discord.Embed(color=color)
        embed.set_author(name=f"Auto Roll — {player_name} — {num_dice}d{die_size} ({label})")
        embed.add_field(name="Kept", value=f"`{kept}`", inline=True)
        embed.add_field(name="Dropped", value=f"~~`{dropped}`~~", inline=True)
        embed.add_field(name="Result", value=f"**{total}**", inline=False)
    else:
        rolls = [random.randint(1, die_size) for _ in range(num_dice)]
        total = sum(rolls)
        embed = discord.Embed(color=0xe74c3c)
        embed.set_author(name=f"Auto Roll — {player_name} — {num_dice}d{die_size}")
        embed.add_field(name="Rolls", value=f"`{rolls}`", inline=True)
        embed.add_field(name="Result", value=f"**{total}**", inline=True)
    return embed


def _time_ago(iso_str: str) -> str:
    """Return a human-readable 'X ago' string from an ISO timestamp."""
    if not iso_str:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_str)
        diff = datetime.now(timezone.utc) - dt
        days = diff.days
        hours = diff.seconds // 3600
        if days >= 7:
            return f"{days // 7}w ago"
        if days >= 1:
            return f"{days}d ago"
        if hours >= 1:
            return f"{hours}h ago"
        return "just now"
    except Exception:
        return "unknown"


class DMCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.state = GameState.load()
        self.config = ServerConfig.load()
        self.log_channel: discord.TextChannel | None = None
        self.voice_client = None
        self._speaking_lock = asyncio.Lock()

    @commands.Cog.listener()
    async def on_ready(self):
        """Restore persisted channel settings after bot connects."""
        if self.config.log_channel_id:
            ch = self.bot.get_channel(self.config.log_channel_id)
            if isinstance(ch, discord.TextChannel):
                self.log_channel = ch
                print(f"[Config] Log channel restored: #{ch.name}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Send a welcome message when the bot is added to a server."""
        # Prefer the server's system channel, then fall back to the first writable text channel
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
                None,
            )
        if channel is None:
            return

        embed = discord.Embed(
            title="D&D Bot is here!",
            description=(
                "I'm your AI-powered Dungeon Master. I'll listen to your players in voice, "
                "narrate the world, enforce character rules, and roll dice automatically — "
                "all powered by Google Gemini."
            ),
            color=0x8e44ad,
        )
        embed.add_field(
            name="Step 1 — Run setup",
            value=(
                "Type `!setup` to choose which text channel I post to and which voice channel "
                "I join for sessions. Do this before anything else."
            ),
            inline=False,
        )
        embed.add_field(
            name="Step 2 — Create a campaign",
            value="Type `!newcampaign` to set up your first campaign (name, location, backstory, and Human DM).",
            inline=False,
        )
        embed.add_field(
            name="Step 3 — Register characters",
            value=(
                "Each player runs `!register <dndbeyond_url>` to import their sheet, "
                "or `!register <name> <race> <class>` for manual entry."
            ),
            inline=False,
        )
        embed.add_field(
            name="Step 4 — Start playing",
            value="Type `!startcampaign`, pick your campaign, and the adventure begins.",
            inline=False,
        )
        embed.set_footer(text="Type !gamehelp at any time for a full command reference.")
        await channel.send(embed=embed)

    # ─── Helper ────────────────────────────────────────────────────────────────

    async def speak_dm(self, text: str):
        """Speak a DM response in the voice channel via TTS.
        Falls back to a text embed if the bot is not in voice."""
        if not self.voice_client or not self.voice_client.is_connected():
            # Fallback: post embed if no voice connection
            if self.log_channel:
                embed = discord.Embed(description=text, color=0x8e44ad)
                embed.set_author(name="Dungeon Master")
                await self.log_channel.send(embed=embed)
            else:
                print(f"[DM] {text}")
            return

        async with self._speaking_lock:
            try:
                source = await generate_tts_source(text)
            except Exception as e:
                print(f"[TTS Error] {e}")
                return

            loop = asyncio.get_running_loop()
            done = loop.create_future()

            def after(error):
                loop.call_soon_threadsafe(done.set_result, error)

            self.voice_client.play(source, after=after)
            await done

    async def _execute_autoroll(self, player: str, num_dice: int, die_size: int, roll_type: str):
        """Roll dice automatically and post result embeds to the log channel."""
        channel = self.log_channel
        if not channel:
            return

        # "all" rolls for every registered player (e.g. initiative)
        if player.lower() == "all":
            names = [p["character_name"] for p in self.state.players.values()] or ["Everyone"]
        else:
            names = [player]

        for name in names:
            embed = _build_autoroll_embed(name, num_dice, die_size, roll_type)
            await channel.send(embed=embed)

    async def on_transcript(self, user_id: int, username: str, transcript: str):
        """Called when a player's speech is transcribed."""
        if not transcript:
            return

        print(f"[STT] {username}: {transcript}")

        # Echo what was heard to the log channel
        if self.log_channel:
            await self.log_channel.send(f"🗣️ **{username}:** {transcript}")

        # Get DM response from Gemini
        response = await get_dm_response(self.state, transcript, username)
        autoroll = _parse_autoroll(response)
        clean = _AUTOROLL_STRIP_RE.sub('', response).strip()
        await self.speak_dm(clean)
        if autoroll:
            await self._execute_autoroll(*autoroll)

    # ─── Voice Commands ────────────────────────────────────────────────────────

    @commands.command(name="join")
    async def join_voice(self, ctx):
        """Join the voice channel and start listening."""
        if not ctx.author.voice:
            await ctx.send("❌ You need to be in a voice channel first!")
            return

        if not VOICE_RECV_AVAILABLE:
            await ctx.send(
                "⚠️ Voice listening requires `discord-ext-voice-recv`. "
                "The bot can join but won't transcribe audio.\n"
                "Install it with: `pip install discord-ext-voice-recv`"
            )

        await self._join_voice(ctx)
        await ctx.send(f"🎙️ Listening for adventurers!")

    @commands.command(name="leave")
    async def leave_voice(self, ctx):
        """Leave the voice channel."""
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
            self.voice_client = None
            await ctx.send("👋 Left the voice channel.")
        else:
            await ctx.send("❌ I'm not in a voice channel.")

    @commands.command(name="setchannel")
    async def set_log_channel(self, ctx):
        """Set this channel as the DM log / dice roll channel."""
        self.log_channel = ctx.channel
        self.config.log_channel_id = ctx.channel.id
        self.config.save()
        await ctx.send(f"This channel is now the DM log. Dice rolls and DM narration will appear here.")

    # ─── Server Setup Commands ─────────────────────────────────────────────────

    @commands.command(name="setup")
    async def server_setup(self, ctx):
        """First-time setup: pick the log text channel and voice channel from a list."""
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        guild = ctx.guild
        if not guild:
            await ctx.send("This command must be used in a server.")
            return

        # ── Step 1: Pick text channel ─────────────────────────────────────────
        text_channels = [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages]
        lines = [f"**{i}.** #{c.name}" for i, c in enumerate(text_channels, 1)]
        current_log = f"#{self.log_channel.name}" if self.log_channel else "not set"
        embed = discord.Embed(
            title="Server Setup (1/2) — Log Channel",
            description=(
                f"Current: {current_log}\n\n"
                + "\n".join(lines)
                + "\n\nType a number to select, or `skip` to keep the current setting."
            ),
            color=0x3498db,
        )
        await ctx.send(embed=embed)

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60.0)
            choice = msg.content.strip().lower()
        except asyncio.TimeoutError:
            await ctx.send("Setup cancelled — timed out.")
            return

        if choice != "skip":
            try:
                idx = int(choice) - 1
                if not (0 <= idx < len(text_channels)):
                    raise ValueError
                self.log_channel = text_channels[idx]
                self.config.log_channel_id = self.log_channel.id
                self.config.save()
            except ValueError:
                await ctx.send("Invalid choice — log channel unchanged.")

        # ── Step 2: Pick voice channel ────────────────────────────────────────
        voice_channels = [c for c in guild.voice_channels if c.permissions_for(guild.me).connect]
        lines = [f"**{i}.** {c.name}" for i, c in enumerate(voice_channels, 1)]
        configured_vc = self.bot.get_channel(self.config.voice_channel_id)
        current_voice = configured_vc.name if isinstance(configured_vc, discord.VoiceChannel) else "not set (joins user's channel)"
        embed = discord.Embed(
            title="Server Setup (2/2) — Voice Channel",
            description=(
                f"Current: {current_voice}\n\n"
                + "\n".join(lines)
                + "\n\nType a number to select, or `skip` to keep joining the user's channel."
            ),
            color=0x3498db,
        )
        await ctx.send(embed=embed)

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60.0)
            choice = msg.content.strip().lower()
        except asyncio.TimeoutError:
            await ctx.send("Setup cancelled — timed out.")
            return

        if choice != "skip":
            try:
                idx = int(choice) - 1
                if not (0 <= idx < len(voice_channels)):
                    raise ValueError
                vc = voice_channels[idx]
                self.config.voice_channel_id = vc.id
                self.config.save()
            except ValueError:
                await ctx.send("Invalid choice — voice channel unchanged.")

        # ── Summary ───────────────────────────────────────────────────────────
        log_name = f"#{self.log_channel.name}" if self.log_channel else "not set"
        configured_vc = self.bot.get_channel(self.config.voice_channel_id)
        voice_name = configured_vc.name if isinstance(configured_vc, discord.VoiceChannel) else "user's channel (default)"
        embed = discord.Embed(
            title="Setup complete!",
            description=(
                f"**Log channel:** {log_name}\n"
                f"**Voice channel:** {voice_name}\n\n"
                "Use `!newcampaign` to create your first campaign, or `!startcampaign` to resume one."
            ),
            color=0x2ecc71,
        )
        await ctx.send(embed=embed)

    @commands.command(name="setvoice")
    async def set_voice_channel(self, ctx):
        """Pick the voice channel the bot will join for sessions."""
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        guild = ctx.guild
        if not guild:
            await ctx.send("This command must be used in a server.")
            return

        voice_channels = [c for c in guild.voice_channels if c.permissions_for(guild.me).connect]
        lines = [f"**{i}.** {c.name}" for i, c in enumerate(voice_channels, 1)]
        configured_vc = self.bot.get_channel(self.config.voice_channel_id)
        current = configured_vc.name if isinstance(configured_vc, discord.VoiceChannel) else "not set (joins user's channel)"

        embed = discord.Embed(
            title="Select Voice Channel",
            description=(
                f"Current: {current}\n\n"
                + "\n".join(lines)
                + "\n\nType a number to select, `none` to clear (bot will join the user's channel), or `cancel`."
            ),
            color=0x3498db,
        )
        await ctx.send(embed=embed)

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30.0)
            choice = msg.content.strip().lower()
        except asyncio.TimeoutError:
            await ctx.send("Cancelled — timed out.")
            return

        if choice == "cancel":
            await ctx.send("Cancelled.")
            return

        if choice == "none":
            self.config.voice_channel_id = 0
            self.config.save()
            await ctx.send("Voice channel cleared — bot will join whatever channel you're in.")
            return

        try:
            idx = int(choice) - 1
            if not (0 <= idx < len(voice_channels)):
                raise ValueError
            vc = voice_channels[idx]
            self.config.voice_channel_id = vc.id
            self.config.save()
            await ctx.send(f"Voice channel set to **{vc.name}**.")
        except ValueError:
            await ctx.send("Invalid choice — voice channel unchanged.")

    # ─── Session Commands ──────────────────────────────────────────────────────

    @commands.command(name="startsession")
    async def start_game_session(self, ctx):
        """Start the DnD session with an opening narration."""
        if not self.log_channel:
            self.log_channel = ctx.channel

        await ctx.send("⏳ The Dungeon Master is preparing the session...")
        opening = await start_session(self.state)
        await self.speak_dm(opening)

    @commands.command(name="dm")
    async def manual_dm_input(self, ctx, *, text: str):
        """Manually send input to the DM (for players without mic or testing)."""
        response = await get_dm_response(self.state, text, ctx.author.display_name)
        if self.log_channel:
            await self.log_channel.send(f"🗣️ **{ctx.author.display_name}:** {text}")
        autoroll = _parse_autoroll(response)
        clean = _AUTOROLL_STRIP_RE.sub('', response).strip()
        await self.speak_dm(clean)
        if autoroll:
            await self._execute_autoroll(*autoroll)

    @commands.command(name="location")
    async def set_location(self, ctx, *, location: str):
        """Update the current location (DM use)."""
        self.state.current_location = location
        self.state.save()
        await ctx.send(f"📍 Location updated to: **{location}**")

    @commands.command(name="note")
    async def add_world_note(self, ctx, *, note: str):
        """Add a world/lore note the DM AI will remember."""
        self.state.world_notes += f"\n- {note}"
        self.state.save()
        await ctx.send(f"📝 Note added to world lore.")

    # ─── Campaign Setup Helper ─────────────────────────────────────────────────

    async def _setup_new_campaign(self, ctx, activate: bool = True) -> bool:
        """Interactive prompts to name a campaign, set the starting location, and capture backstory.
        activate=True marks the campaign active immediately (used by !startcampaign).
        activate=False just saves it for later (used by !newcampaign).
        Returns True on success, False if the user cancels or times out."""
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        # ── Campaign name ─────────────────────────────────────────────────────
        await ctx.send("**Campaign Setup (1/3)** — What is the name of your campaign?")
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60.0)
            campaign_name = msg.content.strip()
        except asyncio.TimeoutError:
            await ctx.send("Campaign setup cancelled — timed out.")
            return False

        if not campaign_name:
            await ctx.send("Campaign setup cancelled — no name provided.")
            return False

        # ── Starting location ─────────────────────────────────────────────────
        await ctx.send("**Campaign Setup (2/3)** — Where does the adventure begin? *(starting location — e.g. 'a foggy dockside tavern in the city of Waterdeep')*")
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60.0)
            location = msg.content.strip() or "a mysterious tavern"
        except asyncio.TimeoutError:
            location = "a mysterious tavern"

        # ── Backstory ─────────────────────────────────────────────────────────
        await ctx.send(
            "**Campaign Setup (3/3)** — Share any backstory or context for the DM AI.\n"
            "This is great for continuing a manual campaign — include setting details, "
            "major past events, villain motivations, party history, or anything else the DM should know.\n"
            "*(Type `skip` or leave blank to start fresh)*"
        )
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=120.0)
            backstory = msg.content.strip()
        except asyncio.TimeoutError:
            backstory = ""

        if backstory.lower() == "skip":
            backstory = ""

        # ── Human DM assignment ───────────────────────────────────────────────
        await ctx.send(
            "**Campaign Setup (4/4)** — @mention the Human DM for this campaign, or type `skip`.\n"
            "The Human DM can privately message the bot to add context and plot points "
            "that only the AI DM will see."
        )
        human_dm_id = 0
        human_dm_name = ""
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60.0)
            if msg.content.strip().lower() != "skip" and msg.mentions:
                human_dm = msg.mentions[0]
                human_dm_id = human_dm.id
                human_dm_name = human_dm.display_name
        except asyncio.TimeoutError:
            pass

        self.state.campaign_name = campaign_name
        self.state.current_location = location
        self.state.world_notes = f"Campaign Backstory:\n{backstory}" if backstory else ""
        self.state.human_dm_id = human_dm_id
        self.state.human_dm_name = human_dm_name
        self.state.campaign_active = activate
        self.state.save()
        return True

    async def _send_character_sheet_dm(self, player: dict, campaign_name: str, session_number: int) -> bool:
        """DM a player their end-of-session character sheet. Returns True on success."""
        discord_id = player["discord_id"]

        user = self.bot.get_user(discord_id)
        if not user:
            try:
                user = await self.bot.fetch_user(discord_id)
            except Exception:
                return False

        # ── D&D Beyond link ───────────────────────────────────────────────────
        ddb_id = player.get("dndbeyond_id", "")
        if ddb_id:
            ddb_url = f"https://www.dndbeyond.com/characters/{ddb_id}"
            header = (
                f"Please remember to update your character sheet on D&D Beyond!\n"
                f"**{ddb_url}**"
            )
        else:
            header = (
                "Please remember to update your character sheet on D&D Beyond!\n"
                "*(Tip: link your sheet with `!register <dndbeyond_url>` next session)*"
            )

        embed = discord.Embed(
            title=f"📋 {player['character_name']} — End of Session {session_number}",
            description=header,
            color=0x3498db,
        )

        # ── Core stats ────────────────────────────────────────────────────────
        embed.add_field(
            name="Character",
            value=(
                f"**Race:** {player['race']}\n"
                f"**Class:** {player['char_class']}\n"
                f"**Level:** {player['level']}\n"
                f"**HP:** {player['current_hp']}/{player['max_hp']}"
            ),
            inline=True,
        )

        # ── Inventory ─────────────────────────────────────────────────────────
        inv = player.get("inventory", [])
        if inv:
            inv_text = "\n".join(f"• {item}" for item in inv)
            if len(inv_text) > 1020:
                inv_text = inv_text[:1017] + "..."
        else:
            inv_text = "Nothing"
        embed.add_field(name="Inventory", value=inv_text, inline=True)

        # ── Spells known ──────────────────────────────────────────────────────
        spells = player.get("spells_known", [])
        if spells:
            cantrips = [s["name"] for s in spells if s["level"] == 0]
            levelled = sorted([s for s in spells if s["level"] > 0], key=lambda s: s["level"])
            lines = []
            if cantrips:
                lines.append(f"**Cantrips:** {', '.join(cantrips)}")
            by_level: dict[int, list] = {}
            for s in levelled:
                by_level.setdefault(s["level"], []).append(s["name"])
            for lvl, names in sorted(by_level.items()):
                lines.append(f"**Level {lvl}:** {', '.join(names)}")
            spell_text = "\n".join(lines)
            if len(spell_text) > 1020:
                spell_text = spell_text[:1017] + "..."
            embed.add_field(name="Spells Known", value=spell_text, inline=False)

        # ── Spell slots ───────────────────────────────────────────────────────
        slots = player.get("spell_slots", {})
        if slots:
            slot_lines = []
            for lvl in sorted(slots.keys(), key=int):
                s = slots[lvl]
                bar = "█" * s["remaining"] + "░" * (s["max"] - s["remaining"])
                slot_lines.append(f"Level {lvl}: `{bar}` {s['remaining']}/{s['max']}")
            embed.add_field(name="Spell Slots Remaining", value="\n".join(slot_lines), inline=True)

        # ── Class features ────────────────────────────────────────────────────
        features = player.get("class_features", [])
        if features:
            feat_lines = [
                f"• **{f['name']}:** {f['remaining']}/{f['max_uses']} uses"
                f" *(recharges: {f['recharge']} rest)*"
                for f in features
            ]
            feat_text = "\n".join(feat_lines)
            if len(feat_text) > 1020:
                feat_text = feat_text[:1017] + "..."
            embed.add_field(name="Class Features", value=feat_text, inline=False)

        # ── Notes ─────────────────────────────────────────────────────────────
        notes = player.get("notes", "").strip()
        if notes:
            notes_text = notes if len(notes) <= 1020 else notes[:1017] + "..."
            embed.add_field(name="Notes", value=notes_text, inline=False)

        embed.set_footer(text=f"{campaign_name} · Session {session_number} complete")

        try:
            await user.send(embed=embed)
            return True
        except discord.Forbidden:
            return False
        except Exception:
            return False

    async def _join_voice(self, ctx) -> bool:
        """Join the configured or author's voice channel. Returns True if connected."""
        if self.voice_client and self.voice_client.is_connected():
            return True

        # Prefer the configured voice channel; fall back to the author's current channel
        channel = None
        if self.config.voice_channel_id:
            channel = self.bot.get_channel(self.config.voice_channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                channel = None
        if channel is None and ctx.author.voice:
            channel = ctx.author.voice.channel
        if channel is None:
            return False

        if VOICE_RECV_AVAILABLE:
            self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
            sink = DnDVoiceSink(on_transcript_callback=self.on_transcript)
            self.voice_client.listen(sink)
        else:
            self.voice_client = await channel.connect()

        await ctx.send(f"Joined **{channel.name}**.")
        return True

    # ─── Campaign Commands ─────────────────────────────────────────────────────

    @commands.command(name="startcampaign")
    async def start_campaign(self, ctx):
        """Show saved campaigns and start or resume one."""
        if not self.log_channel:
            self.log_channel = ctx.channel

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        campaigns = GameState.list_campaigns()

        # ── No saved campaigns at all ─────────────────────────────────────────
        if not campaigns:
            await ctx.send("No saved campaigns found. Let's create one!")
            self.state = GameState()
            success = await self._setup_new_campaign(ctx, activate=True)
            if not success:
                return

        else:
            # ── Show the campaign list ────────────────────────────────────────
            lines = []
            for i, c in enumerate(campaigns, 1):
                tag = " *(active)*" if (
                    c["id"] == self.state.campaign_id and self.state.campaign_active
                ) else ""
                lines.append(
                    f"**{i}.** {c['name']}{tag}\n"
                    f"     Session {c['session_number']} · {c['player_count']} player(s) · {c['location']}\n"
                    f"     Last played: {_time_ago(c['last_played'])}"
                )

            embed = discord.Embed(
                title="Saved Campaigns",
                description=(
                    "\n\n".join(lines)
                    + "\n\n`new` — Create a new campaign\n`cancel` — Do nothing"
                ),
                color=0x3498db,
            )
            await ctx.send(embed=embed)

            try:
                msg = await self.bot.wait_for("message", check=check, timeout=30.0)
                choice = msg.content.strip().lower()
            except asyncio.TimeoutError:
                await ctx.send("Cancelled — no response.")
                return

            if choice == "cancel":
                await ctx.send("Cancelled.")
                return

            if choice == "new":
                # Save and pause the current campaign if one is running
                if self.state.campaign_active:
                    self.state.campaign_active = False
                    self.state.save()
                self.state = GameState()
                success = await self._setup_new_campaign(ctx, activate=True)
                if not success:
                    return

            else:
                try:
                    idx = int(choice) - 1
                    if not (0 <= idx < len(campaigns)):
                        raise ValueError
                except ValueError:
                    await ctx.send("Invalid choice — cancelled.")
                    return

                selected = campaigns[idx]

                # Save current campaign if switching to a different one
                if self.state.campaign_active and self.state.campaign_id != selected["id"]:
                    self.state.campaign_active = False
                    self.state.save()

                try:
                    self.state = GameState.load_campaign(selected["id"])
                except Exception:
                    await ctx.send("Failed to load that campaign — the file may be corrupted.")
                    return

                self.state.campaign_active = True
                self.state.save()

        # ── Campaign is loaded and active — join voice and narrate ────────────
        in_voice = await self._join_voice(ctx)
        if not in_voice and not (self.voice_client and self.voice_client.is_connected()):
            await ctx.send(
                "You're not in a voice channel — use `!join` once everyone is ready. "
                "Starting narration in text for now."
            )

        await ctx.send(
            f"Starting **{self.state.campaign_name}** — Session {self.state.session_number}..."
        )
        opening = await start_session(self.state)
        await self.speak_dm(opening)

    @commands.command(name="stopcampaign")
    async def stop_campaign(self, ctx):
        """Pause the active campaign, save state, and increment the session counter."""
        if not self.state.campaign_active:
            await ctx.send("No campaign is currently active.")
            return

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        await ctx.send(
            f"Stop **{self.state.campaign_name}**? "
            f"Session {self.state.session_number} will be saved. "
            f"Type `yes` to confirm."
        )
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30.0)
            if msg.content.strip().lower() != "yes":
                await ctx.send("The adventure continues!")
                return
        except asyncio.TimeoutError:
            await ctx.send("No response — campaign continues.")
            return

        completed_session = self.state.session_number
        self.state.campaign_active = False
        self.state.session_number += 1
        self.state.conversation_history = []  # clear session history; world notes kept
        self.state.save()

        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
            self.voice_client = None

        await ctx.send(
            f"**{self.state.campaign_name}** paused after Session {completed_session}. "
            f"All progress saved. Use `!startcampaign` to resume."
        )

        # ── Send end-of-session character sheet DMs ───────────────────────────
        if self.state.players:
            await ctx.send("Sending session summaries to all players via DM...")
            succeeded, failed = [], []
            for p in self.state.players.values():
                ok = await self._send_character_sheet_dm(p, self.state.campaign_name, completed_session)
                (succeeded if ok else failed).append(p["character_name"])

            lines = []
            if succeeded:
                lines.append(f"✅ Sent to: {', '.join(succeeded)}")
            if failed:
                lines.append(
                    f"❌ Could not DM: {', '.join(failed)} "
                    f"*(they may have DMs disabled)*"
                )
            await ctx.send("\n".join(lines))

    @commands.command(name="newcampaign")
    async def new_campaign(self, ctx):
        """Create a new campaign. The current campaign is saved, not deleted."""
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        # If a campaign is running, save and pause it first
        if self.state.campaign_active:
            await ctx.send(
                f"**{self.state.campaign_name}** is currently active. "
                f"It will be saved and paused. Type `yes` to continue."
            )
            try:
                msg = await self.bot.wait_for("message", check=check, timeout=30.0)
                if msg.content.strip().lower() != "yes":
                    await ctx.send("Cancelled.")
                    return
            except asyncio.TimeoutError:
                await ctx.send("Cancelled — timed out.")
                return

            self.state.campaign_active = False
            self.state.session_number += 1
            self.state.conversation_history = []
            self.state.save()

            if self.voice_client and self.voice_client.is_connected():
                await self.voice_client.disconnect()
                self.voice_client = None

        # Fresh campaign state with a new ID — does not touch existing campaign files
        self.state = GameState()
        await ctx.send("Let's build your new campaign!")
        success = await self._setup_new_campaign(ctx, activate=False)
        if success:
            embed = discord.Embed(
                title=f"{self.state.campaign_name} is ready!",
                color=0x2ecc71,
            )
            embed.add_field(name="Starting Location", value=self.state.current_location, inline=False)
            if self.state.world_notes:
                preview = self.state.world_notes
                if len(preview) > 200:
                    preview = preview[:197] + "..."
                embed.add_field(name="Backstory", value=preview, inline=False)
            if self.state.human_dm_id:
                embed.add_field(name="Human DM", value=self.state.human_dm_name, inline=False)
            embed.add_field(
                name="Next Steps",
                value="Players can register with `!register`.\nUse `!startcampaign` when everyone is ready to begin.",
                inline=False,
            )
            await ctx.send(embed=embed)

    @commands.command(name="humanDM")
    async def show_human_dm(self, ctx):
        """Show the current Human DM for the active campaign."""
        if self.state.human_dm_id:
            await ctx.send(f"The Human DM for **{self.state.campaign_name}** is **{self.state.human_dm_name}**.")
        else:
            await ctx.send(f"No Human DM is assigned to **{self.state.campaign_name}**. Use `!updateDM` to assign one.")

    @commands.command(name="updateDM")
    async def update_human_dm(self, ctx):
        """Assign or change the Human DM. @mention the new DM or type 'none' to remove."""
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        await ctx.send("@mention the new Human DM, type `none` to remove, or `cancel` to abort.")
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.send("Cancelled — timed out.")
            return

        choice = msg.content.strip().lower()

        if choice == "cancel":
            await ctx.send("Cancelled.")
            return

        if choice == "none":
            self.state.human_dm_id = 0
            self.state.human_dm_name = ""
            self.state.save()
            await ctx.send("Human DM removed.")
            return

        if not msg.mentions:
            await ctx.send("No user mentioned — cancelled.")
            return

        human_dm = msg.mentions[0]
        self.state.human_dm_id = human_dm.id
        self.state.human_dm_name = human_dm.display_name
        self.state.save()
        await ctx.send(f"**{human_dm.display_name}** is now the Human DM for **{self.state.campaign_name}**.")

    # ─── Human DM private message listener ────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for private DMs from the Human DM and add them to world notes."""
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        if not self.state.human_dm_id or message.author.id != self.state.human_dm_id:
            return

        note = message.content.strip()
        if not note:
            return

        self.state.world_notes += f"\n[Human DM] {note}"
        self.state.save()
        await message.channel.send(f"Got it — added to campaign context for **{self.state.campaign_name}**.")

    # ─── Player Commands ───────────────────────────────────────────────────────

    @commands.command(name="register")
    async def register_player(self, ctx, *, args: str = ""):
        """Register your character.
        Usage:
          !register <dndbeyond_url>               — import from D&D Beyond
          !register <name> <race> <class>          — manual entry
        """
        args = args.strip()

        if not args:
            await ctx.send(
                "Usage:\n"
                "`!register <dndbeyond_url>` — import from D&D Beyond\n"
                "`!register <name> <race> <class>` — manual entry"
            )
            return

        # ── D&D Beyond URL ────────────────────────────────────────────────────
        if "dndbeyond.com/characters/" in args:
            msg = await ctx.send("Fetching character from D&D Beyond...")
            char_data = await fetch_ddb_character(args)

            if char_data is None:
                await msg.edit(
                    content="Could not fetch that character. "
                            "Make sure the URL is correct and the character sheet is set to **public** on D&D Beyond."
                )
                return

            # Record the old level before overwriting so we can detect a level-up
            existing = self.state.get_player(ctx.author.id)
            old_level = existing["level"] if existing else 0

            self.state.add_player(ctx.author.id, ctx.author.display_name)
            self.state.update_player(ctx.author.id, **char_data)
            self.state.save()

            p = self.state.get_player(ctx.author.id)
            embed = discord.Embed(
                title=f"⚔️ {p['character_name']} linked from D&D Beyond!",
                color=0xe74c3c,
            )
            embed.add_field(name="Race", value=p["race"], inline=True)
            embed.add_field(name="Class", value=p["char_class"], inline=True)
            embed.add_field(name="Level", value=str(p["level"]), inline=True)
            embed.add_field(name="HP", value=f"{p['current_hp']}/{p['max_hp']}", inline=True)
            embed.set_footer(text=f"Linked to {ctx.author.display_name}")
            await msg.delete()
            await ctx.send(embed=embed)

            # Announce level-up if the character gained levels since last sync
            if p["level"] > old_level and old_level > 0:
                channel = self.log_channel or ctx.channel
                await announce_level_up(channel, p, old_level, p["level"])
            return

        # ── Manual entry: name race class ─────────────────────────────────────
        parts = args.split()
        if len(parts) < 3:
            await ctx.send(
                "Manual registration needs three arguments: `!register <name> <race> <class>`\n"
                "Or link a D&D Beyond character: `!register <dndbeyond_url>`"
            )
            return

        character_name, race, char_class = parts[0], parts[1], parts[2]
        self.state.add_player(ctx.author.id, ctx.author.display_name)
        self.state.update_player(
            ctx.author.id,
            character_name=character_name,
            race=race,
            char_class=char_class,
        )
        self.state.save()
        await ctx.send(
            f"⚔️ **{character_name}** the {race} {char_class} has joined the party! "
            f"Use `!hp set <max>` to set your HP."
        )

    @commands.command(name="hp")
    async def manage_hp(self, ctx, action: str, amount: int = 0):
        """Manage HP. Usage: !hp set 30 | !hp damage 5 | !hp heal 3 | !hp show"""
        p = self.state.get_player(ctx.author.id)
        if not p:
            await ctx.send("❌ Register first with `!register <name> <race> <class>`")
            return

        if action == "set":
            self.state.update_player(ctx.author.id, max_hp=amount, current_hp=amount)
            await ctx.send(f"❤️ **{p['character_name']}** HP set to {amount}/{amount}")
        elif action == "damage":
            new_hp = max(0, p["current_hp"] - amount)
            self.state.update_player(ctx.author.id, current_hp=new_hp)
            status = "💀 **UNCONSCIOUS!**" if new_hp == 0 else f"❤️ HP: {new_hp}/{p['max_hp']}"
            await ctx.send(f"🩸 **{p['character_name']}** takes {amount} damage! {status}")
        elif action == "heal":
            new_hp = min(p["max_hp"], p["current_hp"] + amount)
            self.state.update_player(ctx.author.id, current_hp=new_hp)
            await ctx.send(f"💚 **{p['character_name']}** heals {amount} HP! ❤️ HP: {new_hp}/{p['max_hp']}")
        elif action == "show":
            await ctx.send(f"❤️ **{p['character_name']}** HP: {p['current_hp']}/{p['max_hp']}")

        self.state.save()

    @commands.command(name="party")
    async def show_party(self, ctx):
        """Show all registered players and their status."""
        summary = self.state.player_summary()
        embed = discord.Embed(
            title=f"⚔️ The Party — {self.state.campaign_name}",
            description=summary,
            color=0x3498db
        )
        embed.set_footer(text=f"Session {self.state.session_number} | {self.state.current_location}")
        await ctx.send(embed=embed)

    @commands.command(name="inventory")
    async def manage_inventory(self, ctx, action: str = "show", *, item: str = ""):
        """Manage inventory. Usage: !inventory show | !inventory add Sword | !inventory remove Sword"""
        p = self.state.get_player(ctx.author.id)
        if not p:
            await ctx.send("❌ Register first with `!register <name> <race> <class>`")
            return

        if action == "show":
            inv = p.get("inventory", [])
            items = ", ".join(inv) if inv else "Nothing"
            await ctx.send(f"🎒 **{p['character_name']}'s** inventory: {items}")
        elif action == "add" and item:
            p["inventory"].append(item)
            self.state.save()
            await ctx.send(f"🎒 Added **{item}** to {p['character_name']}'s inventory.")
        elif action == "remove" and item:
            inv = p.get("inventory", [])
            if item in inv:
                inv.remove(item)
                self.state.save()
                await ctx.send(f"🎒 Removed **{item}** from {p['character_name']}'s inventory.")
            else:
                await ctx.send(f"❌ **{item}** not found in inventory.")

    @commands.command(name="gamehelp")
    async def game_help(self, ctx):
        """Show all DnD bot commands."""
        embed = discord.Embed(title="🎲 DnD Bot Commands", color=0x8e44ad)
        embed.add_field(name="📖 Campaign", value=(
            "`!newcampaign` — Create a fresh campaign (wipes all data)\n"
            "`!startcampaign` — Start or resume a campaign\n"
            "`!stopcampaign` — Pause campaign and save progress\n"
            "`!humanDM` — Show the current Human DM\n"
            "`!updateDM` — Assign or change the Human DM"
        ), inline=False)
        embed.add_field(name="🎙️ Session", value=(
            "`!setup` — First-time setup: pick log and voice channels\n"
            "`!setchannel` — Set this channel as the DM log\n"
            "`!setvoice` — Pick the voice channel the bot will join\n"
            "`!join` — Bot joins your (or the configured) voice channel\n"
            "`!leave` — Bot leaves voice\n"
            "`!startsession` — Regenerate opening narration\n"
            "`!dm <text>` — Send text to DM manually\n"
            "`!location <place>` — Update current location\n"
            "`!note <lore>` — Add world lore note"
        ), inline=False)
        embed.add_field(name="🎲 Dice", value=(
            "`!roll <die>` — Start a guided roll (d4/d6/d8/d10/d12/d20/d100)\n"
            "  Bot will ask: number of dice → adv/dis/normal → modifier\n"
            "`!stats` — Roll full ability score set (4d6 drop lowest × 6)"
        ), inline=False)
        embed.add_field(name="⚔️ Characters", value=(
            "`!register <dndbeyond_url>` — Import character from D&D Beyond\n"
            "`!register <name> <race> <class>` — Manual character entry\n"
            "`!hp set/damage/heal/show <amount>` — Manage HP\n"
            "`!inventory show/add/remove <item>`\n"
            "`!party` — Show all party members"
        ), inline=False)
        embed.add_field(name="🔮 Spells & Abilities", value=(
            "`!spell add <level> <name>` — Learn a spell\n"
            "`!spell cast <name> [slot_level]` — Cast and consume a slot\n"
            "`!spell show` — List known spells\n"
            "`!slot set/use/restore/show` — Manage spell slots\n"
            "`!feature add/use/restore/show` — Track class features\n"
            "`!action use/reset/show` — Track turn action economy\n"
            "`!rest short|long` — Take a rest (restores slots, features, HP)"
        ), inline=False)
        embed.add_field(name="⬆️ Levelling", value=(
            "`!level up` — Gain one level with full level-up breakdown\n"
            "`!level set <n>` — Set level directly (announces if levelling up)"
        ), inline=False)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(DMCog(bot))
