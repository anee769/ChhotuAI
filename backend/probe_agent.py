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


def transcribe(pcm: bytes) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(pcm)
    r = httpx.post("https://api.sarvam.ai/speech-to-text",
                   headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                   files={"file": ("t.wav", buf.getvalue(), "audio/wav")},
                   data={"model": "saaras:v3"}, timeout=120)
    return r.json().get("transcript", "") if r.status_code == 200 else \
        f"[stt {r.status_code}]"


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
    """A caller who says one thing, waits for the reply, then says the next.

    The first version slept a fixed 14s per line and talked straight over the
    agent, which showed up as ServerUserInterruptEvent and a transcript with
    two answers mashed together. Wait for the agent's audio to actually stop
    instead, and keep each turn in its own buffer so it can be read back
    separately.
    """

    # The ENDING block tells the agent to hang up on a silent caller, and it
    # does: waiting 45s for a greeting got the call terminated every time.
    # Answer promptly, like a person would.
    QUIET = 1.2          # seconds of silence that means "it has finished"
    MAX_WAIT = 20.0      # a tool call plus a reply should never take longer
    GREETING = 2.5       # do not wait for it, just let it start

    def __init__(self, clips):
        # Rendered before the call. A TTS round trip mid-conversation is 1-3s
        # of extra silence, and the agent nudges a quiet caller after 10s and
        # then hangs up, so the reply has to be ready to play instantly.
        self.clips = clips
        self.turns: list[bytearray] = []
        self.current = bytearray()
        self._last_out = 0.0
        self._cb = None
        self._task = None

    async def start(self, input_callback):
        self._cb = input_callback
        self._task = asyncio.create_task(self._run())

    async def _send(self, pcm: bytes):
        for i in range(0, len(pcm), FRAME):
            await self._cb(pcm[i:i + FRAME], FRAME // 2)
            await asyncio.sleep(0.02)          # real time, or the VAD never trips

    async def _silence(self, seconds: float):
        for _ in range(int(seconds / 0.02)):
            await self._cb(b"\x00" * FRAME, FRAME // 2)
            await asyncio.sleep(0.02)

    async def _wait_for_quiet(self):
        """Listen without transmitting.

        Streaming silence here closed the connection every time: a stream of
        empty frames is not the same as an open line with nobody talking.
        """
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < self.MAX_WAIT:
            await asyncio.sleep(0.3)
            quiet = asyncio.get_event_loop().time() - self._last_out
            if self._last_out and quiet > self.QUIET:
                return
        print("[timed out waiting for the agent]", flush=True)

    async def _run(self):
        try:
            await asyncio.sleep(self.GREETING)  # let the greeting start
            for line, pcm in zip(LINES, self.clips):
                self.turns.append(self.current)
                self.current = bytearray()
                print(f"CALLER> {line}", flush=True)
                # Lead-in silence. Without it the server's VAD opens late and
                # eats the first word or two: "Fevicol SH add kar do" arrived
                # as "cost price 300", so the agent kept insisting it had not
                # been given an item name.
                await self._silence(1.0)
                await self._send(pcm)
                # Long enough for the server's VAD to close the utterance.
                # At 0.5s it did not, so the next line arrived glued onto this
                # one and the agent kept re-asking a question it had already
                # asked instead of acting on the answer.
                await self._silence(2.0)
                await self._wait_for_quiet()
            self.turns.append(self.current)
        except asyncio.CancelledError:
            raise
        except Exception:
            import traceback
            traceback.print_exc()

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def output(self, audio: bytes, sample_rate=None):
        self.current.extend(audio)
        self._last_out = asyncio.get_event_loop().time()

    def interrupt(self):
        pass


async def main():
    cfg = InteractionConfig(
        org_id="019f9945-ebf7-77f9-b60b-dc1963284e44",
        workspace_id="019f9945-ebfb-76ac-9855-2f2c5985abbb",
        app_id="Voice-Assis-9018c9fb-e7c8",
        version=4,                                   # newest committed snapshot
        user_identifier="917006322772",              # this is the shop's identity
        user_identifier_type=UIT.PHONE_NUMBER,
        interaction_type=InteractionType.CALL,
        sample_rate=RATE,
        agent_variables={"caller_number": "917006322772"},
    )
    async def on_transcript(m):
        try:
            d = m.model_dump()
        except Exception:
            d = {"raw": str(m)}
        who = d.get("role") or d.get("speaker") or "?"
        said = d.get("text") or d.get("transcript") or d.get("content") or d
        print(f"[{who}] {said}", flush=True)
    async def on_text(m):
        try:
            d = m.model_dump()
        except Exception:
            d = {"raw": str(m)}
        t = d.get("text") or d.get("content")
        if t:
            print(f"TEXT> {d.get('type','')} {t}", flush=True)
    async def on_event(e):
        try:
            detail = e.model_dump()
        except Exception:
            detail = str(e)
        print(f"EVENT> {type(e).__name__} {detail}", flush=True)

    print("rendering speech...", flush=True)
    clips = [tts(t) for t in LINES]
    line = ScriptedLine(clips)
    agent = AsyncSamvaadAgent(api_key=SecretStr(os.environ["SAMVAAD_API_KEY"]),
                              config=cfg, audio_interface=line,
                              text_callback=on_text,
                              transcript_callback=on_transcript,
                              event_callback=on_event)
    await agent.start()
    await agent.wait_for_connect()
    print("CONNECTED", flush=True)
    while line._task and not line._task.done():
        await asyncio.sleep(0.5)
    await asyncio.sleep(1.0)
    await agent.stop()
    for n, turn in enumerate(line.turns):
        if len(turn) < RATE:          # under a second is not speech
            continue
        print(f"AGENT[{n}]> {transcribe(bytes(turn))}", flush=True)

asyncio.run(main())
