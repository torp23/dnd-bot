import logging
import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
from logger import setup_logging

load_dotenv()
setup_logging()

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logger.info(f"Bot online as {bot.user} (id={bot.user.id})")
    for ext in ("dm_cog", "dice_cog", "character_cog"):
        try:
            await bot.load_extension(ext)
            logger.info(f"Loaded extension: {ext}")
        except Exception as e:
            logger.error(f"Failed to load extension {ext}: {e}", exc_info=True)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        logger.warning(f"[Command] Missing argument in !{ctx.command}: {error.param} (user={ctx.author})")
        await ctx.send(f"❌ Missing argument: {error.param}")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        logger.error(f"[Command] Unhandled error in !{ctx.command} (user={ctx.author}): {error}", exc_info=True)
        await ctx.send(f"❌ Error: {str(error)}")
        raise error

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical("DISCORD_TOKEN is not set — cannot start.")
        raise SystemExit(1)
    logger.info("Starting bot...")
    bot.run(token)
