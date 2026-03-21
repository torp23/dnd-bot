import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"🎲 DnD Bot is online as {bot.user}")
    await bot.load_extension("dm_cog")
    await bot.load_extension("dice_cog")
    await bot.load_extension("character_cog")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: {error.param}")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        await ctx.send(f"❌ Error: {str(error)}")
        raise error

if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_TOKEN"))
