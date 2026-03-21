"""
Text-to-speech for DM voice responses using gTTS.
Requires FFmpeg to be installed on the system.
"""
import asyncio
import io

import discord
from gtts import gTTS


def _generate_mp3(text: str) -> io.BytesIO:
    tts = gTTS(text=text, lang='en', slow=False)
    fp = io.BytesIO()
    tts.write_to_fp(fp)
    fp.seek(0)
    return fp


async def generate_tts_source(text: str) -> discord.AudioSource:
    """Generate a discord AudioSource from text. Runs gTTS in executor to avoid blocking."""
    loop = asyncio.get_running_loop()
    fp = await loop.run_in_executor(None, _generate_mp3, text)
    return discord.FFmpegOpusAudio(fp, pipe=True, before_options="-f mp3")
