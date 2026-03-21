"""
DM Cog — the main orchestrator.
Handles joining voice, processing transcripts, and all DM-related commands.
"""
import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands
from game_state import GameState
from dm_brain import get_dm_response, start_session, announce_level_up
from tts import generate_tts_source
from dndbeyond import fetch_ddb_character
from voice_listener import DnDVoiceSink
import os

# Try to import voice_recv; warn if missing
try:
    from discord.ext import voice_recv
    VOICE_RECV_AVAILABLE = True
except ImportError:
    VOICE_RECV_AVAILABLE = False
    print("⚠️  discord-ext-voice-recv not installed. Voice listening disabled.")


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
        self.log_channel: discord.TextChannel | None = None
        self.voice_client = None
        self._speaking_lock = asyncio.Lock()

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
        await self.speak_dm(response)

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
        await ctx.send(f"📜 This channel is now the DM log. Dice rolls and DM narration will appear here.")

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
        await self.speak_dm(response)

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
        """Interactive prompts to name a campaign and set the starting location.
        activate=True marks the campaign active immediately (used by !startcampaign).
        activate=False just saves it for later (used by !newcampaign).
        Returns True on success, False if the user cancels or times out."""
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        await ctx.send("What is the name of your campaign?")
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60.0)
            campaign_name = msg.content.strip()
        except asyncio.TimeoutError:
            await ctx.send("Campaign setup cancelled — timed out.")
            return False

        if not campaign_name:
            await ctx.send("Campaign setup cancelled — no name provided.")
            return False

        await ctx.send("Where does the adventure begin? *(starting location)*")
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60.0)
            location = msg.content.strip() or "a mysterious tavern"
        except asyncio.TimeoutError:
            location = "a mysterious tavern"

        self.state.campaign_name = campaign_name
        self.state.current_location = location
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
        """Join the author's voice channel. Returns True if connected."""
        if self.voice_client and self.voice_client.is_connected():
            return True
        if not ctx.author.voice:
            return False

        channel = ctx.author.voice.channel
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
                description=(
                    f"Location: {self.state.current_location}\n\n"
                    "Players can register with `!register`.\n"
                    "Use `!startcampaign` when everyone is ready to begin."
                ),
                color=0x2ecc71,
            )
            await ctx.send(embed=embed)

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
            "`!stopcampaign` — Pause campaign and save progress"
        ), inline=False)
        embed.add_field(name="🎙️ Session", value=(
            "`!join` — Bot joins your voice channel\n"
            "`!leave` — Bot leaves voice\n"
            "`!setchannel` — Set dice/status log channel\n"
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
