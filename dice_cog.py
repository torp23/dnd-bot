import asyncio
import random

import discord
from discord.ext import commands


VALID_DICE = {"d4": 4, "d6": 6, "d8": 8, "d10": 10, "d12": 12, "d20": 20, "d100": 100}


class DiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="roll", aliases=["r"])
    async def roll_dice(self, ctx, die: str = ""):
        """Roll dice interactively. Usage: !roll d20, !roll d6, !roll d4 ..."""
        die = die.lower().strip()

        if die not in VALID_DICE:
            options = "  ".join(f"`{d}`" for d in VALID_DICE)
            embed = discord.Embed(
                description=f"Choose a die:\n{options}",
                color=0x8e44ad,
            )
            embed.set_author(name="Dice Roller")
            await ctx.send(embed=embed)
            return

        die_size = VALID_DICE[die]

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        def _cancelled(msg):
            return msg.content.strip().lower().startswith("!resetroll")

        TIMEOUT = 30.0

        # ── Step 1: Number of dice ────────────────────────────────────────────
        embed = discord.Embed(
            description=f"How many **{die}** to roll?\n*(reply with a number, or `!resetroll` to cancel)*",
            color=0x8e44ad,
        )
        embed.set_author(name=f"Rolling {die} — Step 1/3")
        await ctx.send(embed=embed)

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=TIMEOUT)
            if _cancelled(msg):
                await ctx.send("Roll cancelled.")
                return
            num_dice = max(1, min(100, int(msg.content.strip())))
        except asyncio.TimeoutError:
            await ctx.send("Roll cancelled — timed out waiting for number of dice.")
            return
        except ValueError:
            await ctx.send("Roll cancelled — that doesn't look like a number.")
            return

        # ── Step 2: Roll type ────────────────────────────────────────────────
        embed = discord.Embed(
            description="Roll type?\n`adv` — Advantage\n`dis` — Disadvantage\n`normal` — Normal\n*(or `!resetroll` to cancel)*",
            color=0x8e44ad,
        )
        embed.set_author(name=f"Rolling {num_dice}{die} — Step 2/3")
        await ctx.send(embed=embed)

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=TIMEOUT)
            if _cancelled(msg):
                await ctx.send("Roll cancelled.")
                return
            content = msg.content.strip().lower()
            if content.startswith("adv"):
                roll_type = "adv"
            elif content.startswith("dis"):
                roll_type = "dis"
            else:
                roll_type = "normal"
        except asyncio.TimeoutError:
            await ctx.send("Roll cancelled — timed out waiting for roll type.")
            return

        # ── Step 3: Modifier ─────────────────────────────────────────────────
        embed = discord.Embed(
            description="Modifier?\n*(e.g. `+3`, `-2`, or `0`, or `!resetroll` to cancel)*",
            color=0x8e44ad,
        )
        embed.set_author(name=f"Rolling {num_dice}{die} ({roll_type}) — Step 3/3")
        await ctx.send(embed=embed)

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=TIMEOUT)
            if _cancelled(msg):
                await ctx.send("Roll cancelled.")
                return
            modifier = int(msg.content.strip().lstrip("+"))
        except asyncio.TimeoutError:
            await ctx.send("Roll cancelled — timed out waiting for modifier.")
            return
        except ValueError:
            modifier = 0

        # ── Roll ─────────────────────────────────────────────────────────────
        mod_str = f" + {modifier}" if modifier > 0 else (f" - {abs(modifier)}" if modifier < 0 else "")

        if roll_type in ("adv", "dis"):
            set_a = [random.randint(1, die_size) for _ in range(num_dice)]
            set_b = [random.randint(1, die_size) for _ in range(num_dice)]
            total_a, total_b = sum(set_a), sum(set_b)

            if roll_type == "adv":
                kept, dropped = (set_a, set_b) if total_a >= total_b else (set_b, set_a)
                color = 0x2ecc71
                label = "Advantage"
            else:
                kept, dropped = (set_a, set_b) if total_a <= total_b else (set_b, set_a)
                color = 0xe67e22
                label = "Disadvantage"

            total = sum(kept) + modifier
            embed = discord.Embed(color=color)
            embed.set_author(name=f"🎲 {ctx.author.display_name} — {num_dice}{die} with {label}")
            embed.add_field(name="Kept", value=f"`{kept}`", inline=True)
            embed.add_field(name="Dropped", value=f"~~`{dropped}`~~", inline=True)
            embed.add_field(name="Result", value=f"**{total}**{mod_str}", inline=False)

        else:
            rolls = [random.randint(1, die_size) for _ in range(num_dice)]
            total = sum(rolls) + modifier
            embed = discord.Embed(color=0xe74c3c)
            embed.set_author(name=f"🎲 {ctx.author.display_name} — {num_dice}{die}")
            embed.add_field(name="Rolls", value=f"`{rolls}`", inline=True)
            embed.add_field(name="Result", value=f"**{total}**{mod_str}", inline=True)

        await ctx.send(embed=embed)

    @commands.command(name="resetroll")
    async def reset_roll(self, ctx):
        """Cancel an in-progress dice roll. Type this at any prompt during !roll."""
        await ctx.send("No roll is currently waiting for your input.")

    @commands.command(name="stats")
    async def roll_stats(self, ctx):
        """Roll a full set of ability scores (4d6 drop lowest, x6)"""
        scores = []
        for _ in range(6):
            rolls = [random.randint(1, 6) for _ in range(4)]
            total = sum(sorted(rolls)[1:])
            scores.append((total, rolls))

        lines = []
        stat_names = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
        for name, (total, rolls) in zip(stat_names, scores):
            dropped = min(rolls)
            kept = sorted(rolls)[1:]
            lines.append(f"**{name}**: {total}  `{kept}` ~~{dropped}~~")

        embed = discord.Embed(
            title=f"🎲 {ctx.author.display_name}'s Ability Scores",
            description="\n".join(lines),
            color=0x9b59b6,
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(DiceCog(bot))
