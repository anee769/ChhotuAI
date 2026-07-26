"""
sarvam_client.py — single wrapper over all Sarvam AI capability used by ChhotuAI.

Contract verified against docs.sarvam.ai on 2026-07-26 (see Section 1 report):
  STT   POST https://api.sarvam.ai/speech-to-text     model saaras:v3   multipart
        modes: transcribe | translate | verbatim | translit | codemix
  TTS   POST https://api.sarvam.ai/text-to-speech     model bulbul:v3   json
        v3 controls: pace (0.5-2.0), temperature; pitch/loudness are v2-only.
  CHAT  POST https://api.sarvam.ai/v1/chat/completions  model sarvam-30b  json
        OpenAI-compatible tools/tool_calls/tool_choice.  NOTE the /v1/ prefix.
  DOC   sarvam-ai SDK  client.document_intelligence.create_job(...)  (batch job)
        -> upload_file -> start -> wait_until_complete -> download_output (zip)

Auth header `api-subscription-key` works on every raw endpoint.

Every AI capability here is Sarvam-only. No third-party models, no embeddings.
"""
from __future__ import annotations

import base64
import json
import os
import time
import tempfile
from pathlib import Path
from typing import Any, Optional

import requests


def _load_dotenv():
    """Tiny .env loader (no dependency) so the key lives in one gitignored file."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = "https://api.sarvam.ai"
API_KEY = os.environ.get("SARVAM_API_KEY", "")
TIMEOUT = 20  # seconds, per spec
MAX_RETRIES = 3
DEBUG_SAVE_RESPONSES = os.environ.get("DEBUG_SAVE_RESPONSES", "1") == "1"
DEBUG_DIR = Path(__file__).resolve().parent.parent / "debug"

STT_MODEL = "saaras:v3"
TTS_MODEL = "bulbul:v3"
CHAT_MODEL = os.environ.get("SARVAM_CHAT_MODEL", "sarvam-30b")
TTS_SPEAKER = os.environ.get("SARVAM_TTS_SPEAKER", "shubh")  # v3 default


class SarvamError(RuntimeError):
    pass


def has_key() -> bool:
    return bool(API_KEY)


def _headers(json_body: bool = True) -> dict:
    h = {"api-subscription-key": API_KEY}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _dump(name: str, payload: Any) -> None:
    if not DEBUG_SAVE_RESPONSES:
        return
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%H%M%S")
        p = DEBUG_DIR / f"{ts}_{name}.json"
        with open(p, "w", encoding="utf-8") as f:
            if isinstance(payload, (dict, list)):
                json.dump(payload, f, ensure_ascii=False, indent=2)
            else:
                f.write(str(payload))
    except Exception:
        pass  # debug dumping must never break a request


def _request(method: str, path: str, *, files=None, data=None, json_body=None,
             timeout: int = None, max_retries: int = None) -> dict:
    """Raw HTTP with retry + timeout. Returns parsed JSON dict."""
    if not API_KEY:
        raise SarvamError(
            "SARVAM_API_KEY not set. Export it before starting the server."
        )
    url = f"{BASE_URL}{path}"
    last_err: Optional[Exception] = None
    attempts = max_retries if max_retries is not None else MAX_RETRIES
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.request(
                method,
                url,
                headers=_headers(json_body=json_body is not None),
                files=files,
                data=data,
                json=json_body,
                timeout=timeout or TIMEOUT,
            )
            if resp.status_code == 200:
                out = resp.json()
                _dump(path.strip("/").replace("/", "_"), out)
                return out
            # 429 / 5xx -> retry; 4xx (except 429) -> fail fast
            if resp.status_code in (429, 500, 502, 503) and attempt < attempts:
                time.sleep(0.8 * attempt)
                continue
            _dump(f"error_{resp.status_code}", resp.text)
            raise SarvamError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
        except requests.RequestException as e:
            last_err = e
            if attempt < attempts:
                time.sleep(0.8 * attempt)
                continue
    raise SarvamError(f"{method} {path} failed after {attempts} tries: {last_err}")


# ---------------------------------------------------------------------------
# Speech-to-text  (Saaras v3)
# ---------------------------------------------------------------------------
def speech_to_text(audio_bytes: bytes, filename: str = "audio.wav",
                   mode: str = "codemix", language_code: str = "hi-IN") -> dict:
    """
    Returns {"transcript": str, "language_code": str, ...}.
    mode="codemix" -> natural code-mixed transcript for display.
    mode="translit" -> romanized string for the matcher.
    """
    files = {"file": (filename, audio_bytes, "application/octet-stream")}
    data = {"model": STT_MODEL, "mode": mode, "language_code": language_code}
    return _request("POST", "/speech-to-text", files=files, data=data)


def transcribe_both(audio_bytes: bytes, filename: str = "audio.wav") -> dict:
    """
    One responsive code-mixed transcription used for both display and parsing.
    The previous second transliteration request doubled voice-turn latency.
    """
    # One STT request keeps a voice turn responsive. The LLM understands native
    # Hindi/English code-mix directly, so a second transliteration request was
    # pure latency and could double the time before the conversation even began.
    display = speech_to_text(audio_bytes, filename, mode="codemix")
    transcript = display.get("transcript", "")
    romanized = transcript
    return {
        "transcript": transcript,
        "romanized": romanized,
        "language_code": display.get("language_code"),
    }


# ---------------------------------------------------------------------------
# Text-to-speech  (Bulbul v3)
# ---------------------------------------------------------------------------
def text_to_speech(text: str, language_code: str = "hi-IN",
                   speaker: str = None, pace: float = 1.0) -> str:
    """Returns a base64-encoded mp3 string (first audio chunk)."""
    body = {
        "text": text[:2400],
        "target_language_code": language_code,
        "speaker": speaker or TTS_SPEAKER,
        "model": TTS_MODEL,
        "pace": pace,
        "output_audio_codec": "mp3",
    }
    out = _request("POST", "/text-to-speech", json_body=body,
                   timeout=10, max_retries=1)
    audios = out.get("audios") or []
    if not audios:
        raise SarvamError("TTS returned no audio")
    return audios[0]


# ---------------------------------------------------------------------------
# Chat completions  (sarvam-30b, tool calling)
# ---------------------------------------------------------------------------
def chat(messages: list, tools: list = None, tool_choice: Any = None,
         temperature: float = 0.1, model: str = None, max_tokens: int = 4000,
         reasoning_effort: str = None, timeout: int = 75) -> dict:
    """Raw OpenAI-compatible chat completion. Returns the full response dict.

    NOTE: sarvam-30b is a reasoning model — it emits `reasoning_content` and
    only then the final `content`. A small max_tokens truncates it mid-reasoning
    (finish_reason=length) leaving content=None, so keep this generous.
    reasoning_effort='low' trims latency (still reasons, but less).
    """
    body: dict = {
        "model": model or CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
    return _request("POST", "/v1/chat/completions", json_body=body, timeout=timeout,
                    max_retries=1)


def chat_message(resp: dict) -> dict:
    """Extract the assistant message from a chat response."""
    return resp["choices"][0]["message"]


def chat_json(messages: list, temperature: float = 0.1,
              reasoning_effort: str = None, max_tokens: int = 4000,
              timeout: int = 75) -> dict:
    """
    Ask the model for a single JSON object and parse it. Tolerant of code fences.
    Used by the matcher's semantic rerank and invoice line extraction.

    NOTE: reasoning models spend tokens on `reasoning_content` BEFORE the final
    JSON. If max_tokens is too small the response ends with finish_reason=length
    and content=None. Callers that see complex inputs should pass a larger budget.
    """
    resp = chat(messages, temperature=temperature, reasoning_effort=reasoning_effort,
                max_tokens=max_tokens, timeout=timeout)
    msg = chat_message(resp)
    # reasoning models sometimes leave the JSON only in reasoning_content
    content = msg.get("content") or msg.get("reasoning_content") or ""
    return _extract_json(content)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    # find outermost braces
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Document intelligence  (batch job via official SDK)
# ---------------------------------------------------------------------------
def parse_document(file_path: str, language: str = "en-IN") -> dict:
    """
    Run the Sarvam Document Digitization batch job and return reassembled text.

    Returns {"markdown": str, "blocks": [ {text, reading_order, layout_tag,
    bbox?} ], "ok": bool}. Raises SarvamError on hard failure so callers can
    fall back to the seeded catalogue costs (spec: never let the best beat
    depend on the riskiest integration).
    """
    if not API_KEY:
        raise SarvamError("SARVAM_API_KEY not set")
    try:
        from sarvamai import SarvamAI  # official SDK, Sarvam-only
    except ImportError as e:
        raise SarvamError(f"sarvam-ai SDK not installed: {e}")

    client = SarvamAI(api_subscription_key=API_KEY)
    with tempfile.TemporaryDirectory() as td:
        try:
            # job_parameters=dict(...), upload_files(file_paths=[...]) (plural),
            # download_outputs(output_dir=...) (a directory, not a zip) — the
            # actual current SDK surface; the previous create_job(language=,
            # output_format=)/upload_file(path)/download_output(zip_path) shapes
            # were stale and likely why live digitization wasn't working right.
            job = client.document_intelligence.create_job(
                job_parameters=dict(language=language, output_format="html")
            )
            job.upload_files(file_paths=[file_path])
            job.start()
            job.wait_until_complete()
            job.download_outputs(output_dir=td)
        except Exception as e:
            raise SarvamError(f"document_intelligence job failed: {e}")
        return _read_doc_dir(td)


def _read_doc_dir(dir_path: str) -> dict:
    blocks: list = []
    text_files: list = []  # (name, content) for html/md/txt fallback
    for root, _dirs, files in os.walk(dir_path):
        for fn in files:
            path = os.path.join(root, fn)
            if fn.endswith(".json"):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    continue
                if isinstance(data, dict):
                    blocks.extend(data.get("blocks", []))
            elif fn.endswith((".html", ".htm", ".md", ".txt")):
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        text_files.append((fn, f.read()))
                except Exception:
                    pass
    if blocks:
        blocks.sort(key=lambda x: x.get("reading_order", 0))
        skip = {"page_header", "page_footer", "page_number"}
        lines = [b.get("text", "") for b in blocks if b.get("layout_tag") not in skip]
        return {"ok": True, "markdown": "\n".join(l for l in lines if l), "blocks": blocks}
    # fallback: biggest text-like file (strip crude html tags)
    if text_files:
        name, content = max(text_files, key=lambda kv: len(kv[1]))
        import re as _re
        md = _re.sub(r"<[^>]+>", " ", content) if name.endswith(("html", "htm")) else content
        md = _re.sub(r"[ \t]+", " ", md)
        return {"ok": True, "markdown": md.strip(), "blocks": []}
    return {"ok": False, "markdown": "", "blocks": []}
