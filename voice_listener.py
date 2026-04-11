"""
Handles voice channel audio capture and transcription via Google Cloud Speech-to-Text.
Uses discord.py's voice receive feature with a custom AudioSink.
"""
import asyncio
import io
import os
import wave
import struct
import discord
from discord.ext import voice_recv
from google.cloud import speech

# Audio config — discord sends 48kHz stereo PCM
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16-bit

# Silence threshold and buffer settings
SILENCE_THRESHOLD = 500      # RMS below this = silence
MIN_SPEECH_DURATION = 0.5    # seconds of audio before processing
SILENCE_DURATION = 1.5       # seconds of silence before we cut and transcribe


class PlayerAudioBuffer:
    """Accumulates audio for a single player and detects speech end."""

    def __init__(self, user_id: int, username: str, callback):
        self.user_id = user_id
        self.username = username
        self.callback = callback  # async fn(user_id, username, transcript)
        self.buffer = bytearray()
        self.silent_frames = 0
        self.speaking = False
        self.processing = False  # guard against duplicate task creation
        self.frames_per_check = int(SAMPLE_RATE * 0.02)  # 20ms frames

    def add_audio(self, pcm_data: bytes):
        rms = calculate_rms(pcm_data)
        if rms > SILENCE_THRESHOLD:
            self.speaking = True
            self.silent_frames = 0
            self.buffer.extend(pcm_data)
        elif self.speaking:
            self.silent_frames += 1
            self.buffer.extend(pcm_data)
            # If we've had enough silence after speech, process
            silence_secs = (self.silent_frames * 0.02)
            if silence_secs >= SILENCE_DURATION and not self.processing:
                self.processing = True
                task = asyncio.create_task(self._process_buffer())
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)

    async def _process_buffer(self):
        if len(self.buffer) < SAMPLE_RATE * SAMPLE_WIDTH * MIN_SPEECH_DURATION:
            self.reset()
            return

        audio_data = bytes(self.buffer)
        self.reset()

        transcript = await transcribe_audio(audio_data)
        if transcript:
            await self.callback(self.user_id, self.username, transcript)

    def reset(self):
        self.buffer = bytearray()
        self.silent_frames = 0
        self.speaking = False
        self.processing = False


class DnDVoiceSink(voice_recv.AudioSink):
    """Custom audio sink that routes audio to per-player buffers."""

    def __init__(self, on_transcript_callback):
        super().__init__()
        self.on_transcript = on_transcript_callback
        self.player_buffers: dict[int, PlayerAudioBuffer] = {}

    def wants_opus(self) -> bool:
        return False  # We want decoded PCM

    def write(self, user, data):
        if user is None:
            return
        if user.id not in self.player_buffers:
            self.player_buffers[user.id] = PlayerAudioBuffer(
                user_id=user.id,
                username=user.display_name,
                callback=self.on_transcript
            )
        self.player_buffers[user.id].add_audio(data.pcm)

    def cleanup(self):
        self.player_buffers.clear()


def calculate_rms(pcm_data: bytes) -> float:
    """Calculate root mean square of PCM audio to detect speech."""
    if len(pcm_data) < 2:
        return 0
    count = len(pcm_data) // 2
    samples = struct.unpack(f"<{count}h", pcm_data[:count * 2])
    if not samples:
        return 0
    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
    return rms


def pcm_to_wav(pcm_data: bytes) -> bytes:
    """Wrap raw PCM in a WAV container for the STT API."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    return buf.getvalue()


async def transcribe_audio(pcm_data: bytes) -> str | None:
    """Send audio to Google Cloud STT and return transcript."""
    try:
        client = speech.SpeechClient()
        wav_data = pcm_to_wav(pcm_data)

        audio = speech.RecognitionAudio(content=wav_data)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=SAMPLE_RATE,
            audio_channel_count=CHANNELS,
            language_code="en-US",
            model="latest_long",
            enable_automatic_punctuation=True,
        )

        # Run in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.recognize(config=config, audio=audio)
        )

        if response.results:
            transcript = response.results[0].alternatives[0].transcript
            return transcript.strip()
    except Exception as e:
        print(f"STT Error: {e}")
    return None
