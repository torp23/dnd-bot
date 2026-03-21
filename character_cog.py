"""
Character Cog — spell, slot, class feature, and action economy tracking.
These constraints are injected into every Gemini prompt so the DM can
counter players attempting actions their character cannot perform.
"""
import discord
from discord.ext import commands
from dm_brain import announce_level_up


def _get_dm_cog(bot):
    return bot.cogs.get("DMCog")


class CharacterCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _state(self):
        dm = _get_dm_cog(self.bot)
        return dm.state if dm else None

    async def _player(self, ctx):
        """Return (state, player_dict) or send an error and return (None, None)."""
        state = self._state()
        if not state:
            await ctx.send("Bot is not fully initialised yet.")
            return None, None
        p = state.get_player(ctx.author.id)
        if not p:
            await ctx.send("Register your character first with `!register`.")
            return None, None
        return state, p

    # ═══════════════════════════════════════════════════════════════════════════
    # !spell
    # ═══════════════════════════════════════════════════════════════════════════

    @commands.group(name="spell", invoke_without_command=True)
    async def spell_group(self, ctx):
        """Manage known spells. Subcommands: add, remove, show, cast"""
        await ctx.send(
            "Usage:\n"
            "`!spell add <level> <name>` — learn a spell (level 0 = cantrip)\n"
            "`!spell remove <name>` — forget a spell\n"
            "`!spell show` — list known spells\n"
            "`!spell cast <name> [slot_level]` — cast and consume a spell slot"
        )

    @spell_group.command(name="add")
    async def spell_add(self, ctx, level: int, *, name: str):
        """Learn a spell. Usage: !spell add 3 Fireball"""
        state, p = await self._player(ctx)
        if not p:
            return
        spells = p.setdefault("spells_known", [])
        if any(s["name"].lower() == name.lower() for s in spells):
            await ctx.send(f"**{name}** is already known.")
            return
        spells.append({"name": name, "level": level})
        state.save()
        slot_label = "cantrip" if level == 0 else f"level {level}"
        await ctx.send(f"**{p['character_name']}** learned **{name}** ({slot_label}).")

    @spell_group.command(name="remove")
    async def spell_remove(self, ctx, *, name: str):
        """Forget a spell. Usage: !spell remove Fireball"""
        state, p = await self._player(ctx)
        if not p:
            return
        spells = p.get("spells_known", [])
        match = next((s for s in spells if s["name"].lower() == name.lower()), None)
        if not match:
            await ctx.send(f"**{name}** not found in known spells.")
            return
        spells.remove(match)
        state.save()
        await ctx.send(f"**{p['character_name']}** forgot **{name}**.")

    @spell_group.command(name="show")
    async def spell_show(self, ctx):
        """List all known spells."""
        state, p = await self._player(ctx)
        if not p:
            return
        spells = p.get("spells_known", [])
        if not spells:
            await ctx.send(f"**{p['character_name']}** knows no spells.")
            return
        cantrips = [s["name"] for s in spells if s["level"] == 0]
        levelled = sorted([s for s in spells if s["level"] > 0], key=lambda s: s["level"])

        embed = discord.Embed(title=f"{p['character_name']}'s Spells", color=0x9b59b6)
        if cantrips:
            embed.add_field(name="Cantrips", value="\n".join(cantrips), inline=False)
        by_level: dict[int, list] = {}
        for s in levelled:
            by_level.setdefault(s["level"], []).append(s["name"])
        for lvl, names in sorted(by_level.items()):
            embed.add_field(name=f"Level {lvl}", value="\n".join(names), inline=True)
        await ctx.send(embed=embed)

    @spell_group.command(name="cast")
    async def spell_cast(self, ctx, *, args: str):
        """Cast a spell and consume a slot. Usage: !spell cast Fireball [3]"""
        state, p = await self._player(ctx)
        if not p:
            return

        parts = args.rsplit(None, 1)
        slot_level = None
        if len(parts) == 2 and parts[1].isdigit():
            spell_name, slot_level = parts[0], int(parts[1])
        else:
            spell_name = args

        spells = p.get("spells_known", [])
        match = next((s for s in spells if s["name"].lower() == spell_name.lower()), None)
        if not match:
            await ctx.send(f"**{p['character_name']}** doesn't know **{spell_name}**.")
            return

        spell_lvl = match["level"]

        # Cantrips don't use slots
        if spell_lvl == 0:
            await ctx.send(f"**{p['character_name']}** casts **{spell_name}** (cantrip — no slot used).")
            return

        use_level = slot_level or spell_lvl
        slots = p.get("spell_slots", {})
        slot = slots.get(str(use_level))

        if not slot or slot["remaining"] <= 0:
            available = [
                f"Lv{k}: {v['remaining']}" for k, v in slots.items() if v["remaining"] > 0
            ]
            hint = f"Available slots: {', '.join(available)}" if available else "No slots remaining."
            await ctx.send(
                f"No level {use_level} spell slots remaining. {hint}\n"
                f"Use `!spell cast {spell_name} <level>` to upcast."
            )
            return

        slot["remaining"] -= 1
        state.save()
        await ctx.send(
            f"**{p['character_name']}** casts **{spell_name}** using a level {use_level} slot. "
            f"({slot['remaining']}/{slot['max']} remaining)"
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # !slot
    # ═══════════════════════════════════════════════════════════════════════════

    @commands.group(name="slot", invoke_without_command=True)
    async def slot_group(self, ctx):
        """Manage spell slots. Subcommands: set, use, restore, show"""
        await ctx.send(
            "Usage:\n"
            "`!slot set <level> <max>` — set maximum slots for a spell level\n"
            "`!slot use <level>` — manually consume a slot\n"
            "`!slot restore <level|all>` — restore slots\n"
            "`!slot show` — view all slot counts"
        )

    @slot_group.command(name="set")
    async def slot_set(self, ctx, level: int, max_slots: int):
        """Set max spell slots for a level. Usage: !slot set 3 2"""
        state, p = await self._player(ctx)
        if not p:
            return
        slots = p.setdefault("spell_slots", {})
        existing = slots.get(str(level), {})
        slots[str(level)] = {
            "max": max_slots,
            "remaining": min(existing.get("remaining", max_slots), max_slots),
        }
        state.save()
        await ctx.send(f"Level {level} spell slots set to {max_slots} for **{p['character_name']}**.")

    @slot_group.command(name="use")
    async def slot_use(self, ctx, level: int):
        """Consume one spell slot manually. Usage: !slot use 3"""
        state, p = await self._player(ctx)
        if not p:
            return
        slots = p.get("spell_slots", {})
        slot = slots.get(str(level))
        if not slot or slot["remaining"] <= 0:
            await ctx.send(f"No level {level} slots remaining.")
            return
        slot["remaining"] -= 1
        state.save()
        await ctx.send(f"Level {level} slot used. ({slot['remaining']}/{slot['max']} remaining)")

    @slot_group.command(name="restore")
    async def slot_restore(self, ctx, level: str = "all"):
        """Restore spell slots. Usage: !slot restore all | !slot restore 3"""
        state, p = await self._player(ctx)
        if not p:
            return
        slots = p.get("spell_slots", {})
        if level == "all":
            for s in slots.values():
                s["remaining"] = s["max"]
            state.save()
            await ctx.send(f"All spell slots restored for **{p['character_name']}**.")
        elif level.isdigit():
            slot = slots.get(level)
            if not slot:
                await ctx.send(f"No level {level} slots configured. Use `!slot set {level} <max>` first.")
                return
            slot["remaining"] = slot["max"]
            state.save()
            await ctx.send(f"Level {level} slots restored ({slot['max']}/{slot['max']}).")
        else:
            await ctx.send("Usage: `!slot restore all` or `!slot restore <level>`")

    @slot_group.command(name="show")
    async def slot_show(self, ctx):
        """Show all spell slot counts."""
        state, p = await self._player(ctx)
        if not p:
            return
        slots = p.get("spell_slots", {})
        if not slots:
            await ctx.send(f"**{p['character_name']}** has no spell slots configured.")
            return
        lines = []
        for lvl in sorted(slots.keys(), key=int):
            s = slots[lvl]
            bar = "█" * s["remaining"] + "░" * (s["max"] - s["remaining"])
            lines.append(f"Level {lvl}: `{bar}` {s['remaining']}/{s['max']}")
        embed = discord.Embed(
            title=f"{p['character_name']}'s Spell Slots",
            description="\n".join(lines),
            color=0x3498db,
        )
        await ctx.send(embed=embed)

    # ═══════════════════════════════════════════════════════════════════════════
    # !feature
    # ═══════════════════════════════════════════════════════════════════════════

    @commands.group(name="feature", invoke_without_command=True)
    async def feature_group(self, ctx):
        """Manage class features. Subcommands: add, use, restore, show"""
        await ctx.send(
            "Usage:\n"
            "`!feature add <name> <max_uses> <recharge>` — add a feature (recharge: short/long/none)\n"
            "`!feature use <name>` — expend one use\n"
            "`!feature restore <name|short|long|all>` — restore uses\n"
            "`!feature show` — list all features"
        )

    @feature_group.command(name="add")
    async def feature_add(self, ctx, max_uses: int, recharge: str, *, name: str):
        """Add a class feature. Usage: !feature add 1 short Action Surge"""
        state, p = await self._player(ctx)
        if not p:
            return
        recharge = recharge.lower()
        if recharge not in ("short", "long", "none"):
            await ctx.send("Recharge must be `short`, `long`, or `none`.")
            return
        features = p.setdefault("class_features", [])
        if any(f["name"].lower() == name.lower() for f in features):
            await ctx.send(f"**{name}** already exists. Use `!feature restore` to refresh uses.")
            return
        features.append({"name": name, "max_uses": max_uses, "remaining": max_uses, "recharge": recharge})
        state.save()
        await ctx.send(
            f"Added **{name}** to **{p['character_name']}** "
            f"({max_uses} use(s), recharges on {recharge} rest)."
        )

    @feature_group.command(name="use")
    async def feature_use(self, ctx, *, name: str):
        """Expend one use of a feature. Usage: !feature use Action Surge"""
        state, p = await self._player(ctx)
        if not p:
            return
        features = p.get("class_features", [])
        feat = next((f for f in features if f["name"].lower() == name.lower()), None)
        if not feat:
            await ctx.send(f"Feature **{name}** not found. Add it with `!feature add`.")
            return
        if feat["remaining"] <= 0:
            await ctx.send(f"**{name}** has no uses remaining. Recharges on {feat['recharge']} rest.")
            return
        feat["remaining"] -= 1
        state.save()
        await ctx.send(f"**{name}** used. ({feat['remaining']}/{feat['max_uses']} remaining)")

    @feature_group.command(name="restore")
    async def feature_restore(self, ctx, *, target: str = "all"):
        """Restore feature uses. Usage: !feature restore all | short | long | <name>"""
        state, p = await self._player(ctx)
        if not p:
            return
        features = p.get("class_features", [])
        target_lower = target.lower()

        if target_lower in ("all", "short", "long"):
            restored = []
            for feat in features:
                if target_lower == "all" or feat["recharge"] == target_lower:
                    feat["remaining"] = feat["max_uses"]
                    restored.append(feat["name"])
            state.save()
            if restored:
                await ctx.send(f"Restored: {', '.join(restored)}.")
            else:
                await ctx.send("Nothing to restore.")
        else:
            feat = next((f for f in features if f["name"].lower() == target_lower), None)
            if not feat:
                await ctx.send(f"Feature **{target}** not found.")
                return
            feat["remaining"] = feat["max_uses"]
            state.save()
            await ctx.send(f"**{feat['name']}** restored to {feat['max_uses']}/{feat['max_uses']}.")

    @feature_group.command(name="show")
    async def feature_show(self, ctx):
        """List all class features."""
        state, p = await self._player(ctx)
        if not p:
            return
        features = p.get("class_features", [])
        if not features:
            await ctx.send(f"**{p['character_name']}** has no features tracked.")
            return
        lines = []
        for feat in features:
            bar = "█" * feat["remaining"] + "░" * (feat["max_uses"] - feat["remaining"])
            lines.append(
                f"**{feat['name']}** `{bar}` {feat['remaining']}/{feat['max_uses']} "
                f"— recharges on {feat['recharge']} rest"
            )
        embed = discord.Embed(
            title=f"{p['character_name']}'s Class Features",
            description="\n".join(lines),
            color=0xe67e22,
        )
        await ctx.send(embed=embed)

    # ═══════════════════════════════════════════════════════════════════════════
    # !action
    # ═══════════════════════════════════════════════════════════════════════════

    @commands.group(name="action", invoke_without_command=True)
    async def action_group(self, ctx):
        """Track turn action economy. Subcommands: use, reset, show"""
        await ctx.send(
            "Usage:\n"
            "`!action use <action|bonus|reaction>` — mark an action type as spent\n"
            "`!action reset` — start a new turn (restore all action types)\n"
            "`!action show` — view current action economy"
        )

    @action_group.command(name="use")
    async def action_use(self, ctx, action_type: str):
        """Mark an action type as used. Usage: !action use bonus"""
        state, p = await self._player(ctx)
        if not p:
            return
        key_map = {
            "action": "action",
            "bonus": "bonus_action",
            "bonus_action": "bonus_action",
            "reaction": "reaction",
        }
        key = key_map.get(action_type.lower())
        if not key:
            await ctx.send("Valid types: `action`, `bonus`, `reaction`.")
            return
        actions = p.setdefault("actions_used", {"action": False, "bonus_action": False, "reaction": False})
        if actions[key]:
            await ctx.send(f"**{action_type}** already used this turn. Use `!action reset` for a new turn.")
            return
        actions[key] = True
        state.save()
        label = key.replace("_", " ")
        await ctx.send(f"**{p['character_name']}**'s {label} marked as used.")

    @action_group.command(name="reset")
    async def action_reset(self, ctx):
        """Reset action economy for a new turn."""
        state, p = await self._player(ctx)
        if not p:
            return
        p["actions_used"] = {"action": False, "bonus_action": False, "reaction": False}
        state.save()
        await ctx.send(f"New turn — **{p['character_name']}**'s actions reset.")

    @action_group.command(name="show")
    async def action_show(self, ctx):
        """Show current action economy."""
        state, p = await self._player(ctx)
        if not p:
            return
        actions = p.get("actions_used", {"action": False, "bonus_action": False, "reaction": False})
        lines = []
        for key, used in actions.items():
            label = key.replace("_", " ").title()
            icon = "✗ spent" if used else "✓ available"
            lines.append(f"{label}: {icon}")
        embed = discord.Embed(
            title=f"{p['character_name']}'s Action Economy",
            description="\n".join(lines),
            color=0x2ecc71,
        )
        await ctx.send(embed=embed)

    # ═══════════════════════════════════════════════════════════════════════════
    # !rest
    # ═══════════════════════════════════════════════════════════════════════════

    @commands.command(name="rest")
    async def rest(self, ctx, rest_type: str = "long"):
        """Take a short or long rest. Usage: !rest short | !rest long"""
        state, p = await self._player(ctx)
        if not p:
            return
        rest_type = rest_type.lower()
        if rest_type not in ("short", "long"):
            await ctx.send("Usage: `!rest short` or `!rest long`.")
            return

        effects = []

        # Restore features that recharge on this type of rest
        features = p.get("class_features", [])
        for feat in features:
            if feat["recharge"] == rest_type or (rest_type == "long" and feat["recharge"] == "short"):
                if feat["remaining"] < feat["max_uses"]:
                    feat["remaining"] = feat["max_uses"]
                    effects.append(f"{feat['name']} restored")

        # Long rest: restore all spell slots and reset HP to max
        if rest_type == "long":
            for s in p.get("spell_slots", {}).values():
                s["remaining"] = s["max"]
            if effects or any(s["remaining"] < s["max"] for s in p.get("spell_slots", {}).values()):
                effects.append("all spell slots restored")
            p["current_hp"] = p["max_hp"]
            effects.append(f"HP restored to {p['max_hp']}")

        # Reset action economy
        p["actions_used"] = {"action": False, "bonus_action": False, "reaction": False}

        state.save()

        rest_label = "Short Rest" if rest_type == "short" else "Long Rest"
        desc = "\n".join(f"• {e}" for e in effects) if effects else "Nothing to restore."
        embed = discord.Embed(
            title=f"{p['character_name']} takes a {rest_label}",
            description=desc,
            color=0x1abc9c,
        )
        await ctx.send(embed=embed)


    # ═══════════════════════════════════════════════════════════════════════════
    # !level
    # ═══════════════════════════════════════════════════════════════════════════

    @commands.group(name="level", invoke_without_command=True)
    async def level_group(self, ctx):
        """Level up your character. Subcommands: up, set"""
        await ctx.send(
            "Usage:\n"
            "`!level up` — gain one level\n"
            "`!level set <number>` — set your level directly"
        )

    @level_group.command(name="up")
    async def level_up(self, ctx):
        """Gain one level and see what changes."""
        state, p = await self._player(ctx)
        if not p:
            return

        old_level = p["level"]
        new_level = old_level + 1
        p["level"] = new_level
        state.save()

        dm_cog = _get_dm_cog(self.bot)
        channel = (dm_cog.log_channel if dm_cog else None) or ctx.channel
        await announce_level_up(channel, p, old_level, new_level)

    @level_group.command(name="set")
    async def level_set(self, ctx, new_level: int):
        """Set your level directly. Usage: !level set 5"""
        if not (1 <= new_level <= 20):
            await ctx.send("Level must be between 1 and 20.")
            return

        state, p = await self._player(ctx)
        if not p:
            return

        old_level = p["level"]
        if new_level == old_level:
            await ctx.send(f"**{p['character_name']}** is already level {old_level}.")
            return

        p["level"] = new_level
        state.save()

        if new_level > old_level:
            dm_cog = _get_dm_cog(self.bot)
            channel = (dm_cog.log_channel if dm_cog else None) or ctx.channel
            await announce_level_up(channel, p, old_level, new_level)
        else:
            await ctx.send(
                f"**{p['character_name']}** set to level {new_level} (from {old_level})."
            )


async def setup(bot):
    await bot.add_cog(CharacterCog(bot))
