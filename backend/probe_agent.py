"""Place a scripted phone call to the Samvaad agent, without a microphone.

Everything else in this repo tests our half of the handshake: /api/agent/tool
answers correctly for all 22 tools. That proves nothing about whether Samvaad
actually calls it, which is the half that broke first (an unresolved
{{shop_key}} shadowing the caller, found by reading a screenshot rather than
by testing). This closes the loop.

The agent exposes a CALL channel only, so a text conversation is not an
option. Instead: synthesise each line with Sarvam TTS, push it in as 20ms PCM
frames through a custom audio interface, collect the agent's audio, and
transcribe it back with Sarvam STT.

    python3 backend/probe_agent.py "cement kitna hai" "aaj ka summary"

Needs SAMVAAD_API_KEY and SARVAM_API_KEY in .env, and ffmpeg on PATH.

Two things the runtime insists on, both learned the hard way:
  * version=1 is required while the agent is a DRAFT. Without it the signed
    URL endpoint 404s with "App not found for the interaction".
  * the input must be exactly 16k mono s16le and paced in real time. Send it
    faster and the agent's VAD never sees a pause, so it never replies.
"""
import asyncio, base64, io, os, subprocess, sys, wave
import httpx
from pydantic import SecretStr
from sarvam_conv_ai_sdk import (AsyncSamvaadAgent, AsyncAudioInterface,
                                InteractionConfig, InteractionType)
from sarvam_conv_ai_sdk.messages.config import UserIdentifierType as UIT

RATE = 16000
FRAME = int(RATE * 0.02) * 2          # 20ms of 16-bit mono
LINES = sys.argv[1:] or ["cement kitna hai"]


def tts(text: str) -> bytes:
    """Sarvam TTS, then ffmpeg to exactly 16k mono s16le.

    bulbul:v3 rejects speech_sample_rate, and its wav is not 16k, so resample
    rather than trusting whatever comes back. The agent's VAD is unforgiving
    about a wrong rate: it hears the wrong pitch and never triggers.
    """
    r = httpx.post("https://api.sarvam.ai/text-to-speech",
                   headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                   json={"text": text, "target_language_code": "hi-IN",
                         "speaker": os.environ.get("SARVAM_TTS_SPEAKER", "simran"),
                         "model": "bulbul:v3", "output_audio_codec": "wav"},
                   timeout=60)
    r.raise_for_status()
    wav = base64.b64decode(r.json()["audios"][0])
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"],
        input=wav, capture_output=True, check=True)
    return out.stdout


class ScriptedLine(AsyncAudioInterface):
    """A caller who says one thing, then listens."""

    def __init__(self):
        self.heard = bytearray()
        self._cb = None
        self._task = None

    async def start(self, input_callback):
        self._cb = input_callback
        self._task = asyncio.create_task(self._speak())

    async def _speak(self):
      try:
        await asyncio.sleep(2.0)                     # let the greeting land
        for line in LINES:
            pcm = tts(line)
            print(f"CALLER> {line}  ({len(pcm)/2/RATE:.1f}s)", flush=True)
            for i in range(0, len(pcm), FRAME):
                await self._cb(pcm[i:i + FRAME], FRAME // 2)
                await asyncio.sleep(0.02)            # real time, or VAD trips
            # trailing silence tells the agent the caller has stopped
            for _ in range(60):
                await self._cb(b"\x00" * FRAME, FRAME // 2)
                await asyncio.sleep(0.02)
            await asyncio.sleep(14)                  # let it answer + use tools
      except Exception as e:
        import traceback; traceback.print_exc()

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def output(self, audio: bytes, sample_rate=None):
        self.heard.extend(audio)

    def interrupt(self):
        pass


async def main():
    cfg = InteractionConfig(
        org_id="019f9945-ebf7-77f9-b60b-dc1963284e44",
        workspace_id="019f9945-ebfb-76ac-9855-2f2c5985abbb",
        app_id="Voice-Assis-9018c9fb-e7c8",
        version=1,                                   # the agent is still a draft
        user_identifier="917006322772",              # this is the shop's identity
        user_identifier_type=UIT.PHONE_NUMBER,
        interaction_type=InteractionType.CALL,
        sample_rate=RATE,
        agent_variables={"caller_number": "917006322772"},
    )
    async def on_transcript(m):
        print(f"[{getattr(m,'role','?')}] {getattr(m,'text','')}", flush=True)
    async def on_text(m):
        t = getattr(m, "text", None)
        if t:
            print(f"AGENT> {t}", flush=True)
    async def on_event(e):
        print(f"EVENT> {type(e).__name__}", flush=True)

    line = ScriptedLine()
    agent = AsyncSamvaadAgent(api_key=SecretStr(os.environ["SAMVAAD_API_KEY"]),
                              config=cfg, audio_interface=line,
                              text_callback=on_text,
                              transcript_callback=on_transcript,
                              event_callback=on_event)
    await agent.start()
    await agent.wait_for_connect()
    print("CONNECTED", agent.get_interaction_id(), flush=True)
    await asyncio.sleep(6 + 20 * len(LINES))
    await agent.stop()
    print(f"[agent audio: {len(line.heard)/2/RATE:.1f}s]", flush=True)
    if line.heard:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes(bytes(line.heard))
        r = httpx.post("https://api.sarvam.ai/speech-to-text",
                       headers={"api-subscription-key":
                                os.environ["SARVAM_API_KEY"]},
                       files={"file": ("agent.wav", buf.getvalue(), "audio/wav")},
                       data={"model": "saaras:v3"}, timeout=120)
        if r.status_code == 200:
            print("AGENT>", r.json().get("transcript"), flush=True)
        else:
            print("[stt failed]", r.status_code, flush=True)

asyncio.run(main())
