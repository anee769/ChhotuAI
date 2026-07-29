"""
main.py — ChhotuAI FastAPI backend (spec Sections 9 & 10).

One page, four tabs. Voice-first stock & margin ledger. All AI = Sarvam only.
Storage behind Repo. Stock is always derived from the event log — never stored.

Run:  uvicorn backend.main:app --reload --port 8000   (from repo root)
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import (FastAPI, UploadFile, File, Form, Body, Header,
                     HTTPException, Request, Response)
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sarvam_client
import matcher as M
import ledger as L
# Real date, not a constant frozen at import; CHHOTU_TODAY pins it for the demo.
# Qualified on purpose: this module already has an /api/today route handler
# named today(), which would shadow a bare `from clock import today`.
import clock
import learning as LEARN
import nlp
import crm
from repo import JsonRepo
import sqlrepo
import auth
import documents
import samvaad_runtime
import presentations
from contextvars import ContextVar

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FRONTEND_ASSETS = ROOT / "frontend" / "assets"

app = FastAPI(title="Chhotu.ai — Awaaz se hisaab")

# The ledger is now per-user, but 113 call sites across this file,
# conversation.py, crm.py and learning.py say `repo.something`. Rather than
# rewrite every one, `repo` becomes a proxy that forwards to whichever user's
# repo the current request resolved to. Attribute access outside a request, or
# without a session, raises 401 rather than quietly reading nobody's books.
_CURRENT: ContextVar = ContextVar("chhotu_repo", default=None)
_CURRENT_USER: ContextVar = ContextVar("chhotu_user", default=None)


class _RepoProxy:
    def __getattr__(self, name):
        target = _CURRENT.get()
        if target is None:
            raise HTTPException(status_code=401, detail="Login required.")
        return getattr(target, name)


repo = _RepoProxy()


def current_user() -> dict:
    user = _CURRENT_USER.get()
    if user is None:
        raise HTTPException(status_code=401, detail="Login required.")
    return user


def bind_user(user: dict):
    """Attach a user (and their ledger) to this request."""
    _CURRENT_USER.set(user)
    _CURRENT.set(sqlrepo.SqlRepo(user["user_id"]))

# uvicorn loads this file as the package module "backend.main", but it also
# inserts backend/ onto sys.path (above) so that bare imports like
# `import matcher` work everywhere in this codebase. That same sys.path entry
# means a bare `import main` (used throughout conversation.py, lazily, inside
# request handlers) does NOT resolve to this already-loaded module — it
# re-executes this entire file as a SECOND, independent top-level module,
# with its OWN `repo = JsonRepo()`. Every commit made via conversation.py's
# `import main; main._write_events(...)` was silently writing into that
# shadow copy's repo instead of this one: correctly persisted to disk (both
# instances point at the same files), but invisible to /api/dashboard and any
# other read on THIS running app until the process was restarted and both
# happened to reload from the same now-consistent files. Registering this
# module under the bare name up front makes any later `import main` resolve
# to this exact object instead of creating a shadow one.
sys.modules.setdefault("main", sys.modules[__name__])
app.mount("/assets", StaticFiles(directory=str(FRONTEND_ASSETS)), name="assets")


# Paths that must work before there is a session: the shell, its assets, the
# login endpoints themselves, and document links (Twilio fetches those and
# cannot authenticate — they are guarded by an unguessable token instead).
# /api/cron/ is exempt from the SESSION check only — a scheduled run has no
# logged-in user. It is not unguarded: the handler verifies CRON_SECRET itself
# and picks its own user per shop.
_OPEN_PREFIXES = ("/api/auth/", "/api/cron/", "/api/agent/", "/assets/", "/d/", "/data/")
_OPEN_EXACT = ("/", "/favicon.ico", "/api/health")


@app.middleware("http")
async def _resolve_user(request, call_next):
    path = request.url.path
    if path in _OPEN_EXACT or path.startswith(_OPEN_PREFIXES):
        return await call_next(request)
    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not token:
        token = request.cookies.get("chhotu_session") or ""
    user = None
    try:
        user = auth.user_for_token(token) if token else None
    except Exception:
        user = None
    if user:
        bind_user(user)
    elif path.startswith("/api/"):
        return JSONResponse({"detail": "Login required."}, status_code=401)
    return await call_next(request)


# The invoice tab previews a scanned bill out of data/, but mounting the whole
# directory served the ledger with it — customers.json (names and phone
# numbers), events.json, receivables.json were all a plain GET away with no
# auth. Serve image files only, by extension, and resolve the path to prove it
# stays inside data/ so "../.env" style names can't escape.
_VIEWABLE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf"}


@app.get("/data/{name}")
def data_file(name: str):
    target = (DATA / name).resolve()
    if (target.suffix.lower() not in _VIEWABLE_SUFFIXES
            or DATA.resolve() not in target.parents
            or not target.is_file()):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(target)


def by_id() -> dict:
    return {s["sku_id"]: s for s in repo.load_catalogue()}


# Bulbul reads native Devanagari far more reliably than an ad-hoc romanized
# spelling — "sariya" was coming out as "saariya". Our reply text is written
# in Hinglish (romanized Hindi) throughout, so swap the common Hindi words
# to Devanagari right before hi-IN synthesis; borrowed English words
# (tonne, stock, cash…) are left alone since Hindi speakers/TTS handle those
# fine as-is.
_HINGLISH_TO_DEVANAGARI = {
    "sariya": "सरिया", "saria": "सरिया", "cement": "सीमेंट", "kitna": "कितना",
    "hai": "है", "becha": "बेचा", "bechi": "बेची", "bori": "बोरी",
    "diya": "दिया", "likh": "लिख", "mein": "में", "nahi": "नहीं",
    "udhaar": "उधार", "aur": "और", "chahiye": "चाहिए", "kaunsa": "कौनसा",
    "theek": "ठीक", "kya": "क्या", "kharida": "खरीदा", "khareeda": "खरीदा",
    "bika": "बिका", "maal": "माल", "gina": "गिना", "gin": "गिन",
    "rakhte": "रखते", "hamne": "हमने", "wala": "वाला", "poochho": "पूछो",
    "bolo": "बोलो", "naam": "नाम", "bataiye": "बताइए", "rupaye": "रुपये",
    "abhi": "अभी", "tak": "तक", "kab": "कब", "purani": "पुरानी",
}
_HINGLISH_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _HINGLISH_TO_DEVANAGARI) + r")\b",
    re.I,
)


def _devanagari_for_tts(text: str) -> str:
    return _HINGLISH_RE.sub(lambda m: _HINGLISH_TO_DEVANAGARI[m.group(1).lower()], text)


# ---------------------------------------------------------------------------
# Auth — phone and password
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "today": clock.today().isoformat()}


def _auth_response(user: dict):
    """Issue the same durable session for login and signup."""
    token = auth.issue_session(user["user_id"])
    out = JSONResponse({"token": token, "user": user})
    out.set_cookie("chhotu_session", token, max_age=auth.SESSION_DAYS * 86400,
                   httponly=True, samesite="lax",
                   secure=bool(os.environ.get("VERCEL")))
    return out


@app.post("/api/auth/login")
def auth_login(payload: dict = Body(...)):
    try:
        return _auth_response(auth.authenticate(
            payload.get("phone", ""), payload.get("password", "")))
    except auth.UserNotFound as e:
        return JSONResponse({"detail": str(e), "code": "signup_required"},
                            status_code=404)
    except auth.PasswordSetupRequired as e:
        return JSONResponse({"detail": str(e), "code": "password_setup_required"},
                            status_code=409)
    except auth.InvalidCredentials as e:
        return JSONResponse({"detail": str(e), "code": "invalid_credentials"},
                            status_code=401)


@app.post("/api/auth/signup")
def auth_signup(payload: dict = Body(...)):
    try:
        return _auth_response(auth.signup(
            payload.get("phone", ""), payload.get("name", ""),
            payload.get("password", "")))
    except auth.AccountExists as e:
        return JSONResponse({"detail": str(e), "code": "sign_in_required"},
                            status_code=409)
    except auth.InvalidCredentials as e:
        return JSONResponse({"detail": str(e), "code": "invalid_credentials"},
                            status_code=401)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    auth.revoke_session(token or request.cookies.get("chhotu_session") or "")
    out = JSONResponse({"ok": True})
    out.delete_cookie("chhotu_session")
    return out


@app.get("/api/me")
def me():
    user = current_user()
    cfg = repo.load_config()
    return {**user, "shop_name": cfg.get("shop_name") or user.get("shop_name") or ""}


@app.post("/api/onboarding")
def onboarding(payload: dict = Body(...)):
    """Finish registration with company details; owner identity came at signup."""
    user = current_user()
    shop = (payload.get("shop_name") or "").strip()
    if not shop:
        raise HTTPException(400, "Company name is required.")
    gstin = (payload.get("gstin") or "").strip().upper()
    address = (payload.get("address") or "").strip()
    if gstin and not re.fullmatch(
            r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", gstin):
        raise HTTPException(400, "Enter a valid GSTIN or leave it blank.")
    auth.complete_onboarding(user["user_id"], shop_name=shop)
    repo.save_config({
        "shop_name": shop,
        "gstin": gstin,
        "address": address,
        "phone": user.get("phone") or "",
    })
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sending: bill to a customer, summary to the owner, reminders to debtors
# ---------------------------------------------------------------------------
@app.post("/api/send/bill")
def send_bill(payload: dict = Body(...)):
    import notify
    customer = payload.get("customer") or {}
    if customer.get("customer_id") and not customer.get("phone"):
        customer = repo.customer(customer["customer_id"]) or customer
    try:
        return notify.send_bill(
            repo, current_user(), customer, payload.get("items") or [],
            payment=payload.get("payment") or "cash",
            due_on=payload.get("payment_deadline"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not send: {str(e)[:200]}")


@app.post("/api/send/summary")
def send_summary(payload: dict = Body(...)):
    import notify
    period = "week" if payload.get("period") == "week" else "day"
    try:
        return notify.send_summary(repo, current_user(), period=period)
    except Exception as e:
        raise HTTPException(502, f"Could not send: {str(e)[:200]}")


@app.post("/api/send/reminders")
def send_reminders(payload: dict = Body(default={}),
                   x_cron_token: str = Header(default="")):
    """Credit reminders, two days before the deadline.

    Also the scheduler's entry point, so it accepts a cron token as well as a
    session — a nightly job has no logged-in user to act as.
    """
    import notify
    days = int((payload or {}).get("days_before", 2))
    expected = (os.environ.get("CHHOTU_CRON_TOKEN") or "").strip()
    if expected and secrets.compare_digest(x_cron_token or "", expected):
        out = []
        for u in auth.all_users():
            bind_user(u)
            out.append({"user": u["phone"],
                        **notify.send_due_reminders(repo, u, days_before=days)})
        return {"runs": out}
    return notify.send_due_reminders(repo, current_user(), days_before=days)


@app.get("/api/cron/reminders")
def cron_reminders(request: Request):
    """Nightly credit reminders, for Vercel Cron.

    Cron can only issue a GET and carries no session, so this authenticates on
    a shared secret instead and then runs once per shop. Vercel sends
    `Authorization: Bearer $CRON_SECRET`; CHHOTU_CRON_TOKEN is accepted too so
    the job can be triggered by hand.
    """
    import notify
    expected = (os.environ.get("CRON_SECRET")
                or os.environ.get("CHHOTU_CRON_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(503, "CRON_SECRET is not configured.")
    supplied = ((request.headers.get("authorization") or "")
                .removeprefix("Bearer ").strip()
                or request.headers.get("x-cron-token") or "")
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(403, "Bad cron token.")
    runs = []
    for u in auth.all_users():
        bind_user(u)
        try:
            runs.append({"shop": u.get("shop_name") or u["phone"],
                         **notify.send_due_reminders(repo, u, days_before=2)})
        except Exception as e:
            # One shop's failure must not stop the rest of the run.
            runs.append({"shop": u["phone"], "error": str(e)[:160]})
    return {"ran_at": clock.today().isoformat(), "shops": len(runs), "runs": runs}


# ---------------------------------------------------------------------------
# Voice agent (Samvaad) tool webhook
# ---------------------------------------------------------------------------
def _agent_auth_refusal(error: str = "bad_agent_secret") -> dict:
    """Return a tool-level refusal instead of an opaque HTTP error.

    Samvaad treats non-2xx HTTP responses as transport failures and the model
    may improvise a reason such as "item not found". A 200 here does not grant
    access: no tenant is opened and no tool runs. It only makes the refusal
    available to the model in the same structured shape as every other tool.
    """
    import agent
    out = {
        "ok": False,
        "authorised": False,
        "error": error,
        "recorded": False,
        "added": False,
        "updated": False,
        "sent": False,
        "found": False,
        "speak": "Agent authentication configure nahi hai. Koi entry nahi hui.",
    }
    return {**out, "facts": agent._facts(out)}


@app.post("/api/agent/tool")
def agent_tool(request: Request,
               payload: dict = Body(...),
               x_agent_secret: str = Header(default=""),
               authorization: str = Header(default="")):
    """Called by the Samvaad agent mid-conversation to touch the ledger.

    A phone call carries no session, so this authenticates twice over: a shared
    secret proves the request came from our agent, and the caller's identity —
    their number on a phone call, or the shop_key variable for in-app voice —
    decides whose books are opened. Both must hold.

    Args may arrive nested under "args" or flattened alongside the tool name;
    the console's cURL editor makes the flat shape easy to produce by accident,
    and a tool call that silently loses its arguments is worse than one that
    accepts both.
    """
    import agent
    # The Samvaad console's Auth section offers Bearer / Api Key / Basic, not a
    # free-form header, so the same secret may arrive either way. Accept both
    # rather than forcing the credential to live in a plain header field, which
    # is where the console warns against putting it.
    # Third accepted position: the request body. The console's secret store
    # resolves in its editor but NOT in a running conversation, where the
    # header arrives empty every time. Agent variables do resolve live, so a
    # secret passed as {{agent_secret}} in the body is the only route that
    # currently works end to end. Same strength as the header over TLS; the
    # difference is only where the console keeps it.
    body_secret = str(payload.get("secret") or "").strip()
    supplied = (x_agent_secret
                or (authorization or "").removeprefix("Bearer ").strip()
                or (body_secret if "{{" not in body_secret else ""))
    # Logged before the secret check, not after. A rejected call is the failure
    # that most needs a trace, and it was the one leaving no trace at all: the
    # caller heard "entry nahi ho payi" and the server recorded a bare 200.
    # Header NAMES only, never values. Which headers a console actually sends
    # is the one thing that cannot be guessed from the outside, and four
    # different auth configurations all failed the same silent way without it.
    line = (f"[agent] tool={payload.get('tool')!r} "
            f"secret={'present' if supplied else 'MISSING'} "
            f"caller={'set' if str(payload.get('caller') or '').strip() else 'EMPTY'}")
    if not supplied:
        # The header list is thirty entries of Vercel plumbing. It earns its
        # place only when the secret is missing, which is the one case where
        # knowing whether the header arrived at all decides the diagnosis.
        line += " headers=" + str(sorted(
            k.lower() for k in request.headers
            if k.lower() not in ("cookie", "authorization")))
    print(line, flush=True)
    try:
        if not agent.verify_secret(supplied):
            return _agent_auth_refusal()
    except agent.AgentError as e:
        print(f"[agent] auth configuration failed: {type(e).__name__}",
              flush=True)
        return _agent_auth_refusal("agent_secret_not_configured")
    args = payload.get("args")
    if isinstance(args, str):
        # The console's Body tab types `args` as JSON but can send it as a
        # string. Parsing it here is the difference between a working tool and
        # one that silently receives nothing.
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            args = None
    if not isinstance(args, dict):
        args = {k: v for k, v in payload.items()
                if k not in ("tool", "caller", "From", "shop_key", "args",
                             "secret")}
    # Log the SHAPE of every agent call, never the values: debugging a voice
    # agent from the outside is guesswork without knowing what actually
    # arrived, and "it errored" is all the caller ever hears.
    # Argument NAMES were not enough: a stock take arrived with item, qty and
    # unit all present and still refused, and nothing recorded which of them
    # the tool could not use. Values here are quantities and product names the
    # caller said out loud, not credentials.
    shown = {k: v for k, v in args.items() if k not in ("request_id",)}
    print(f"[agent] accepted tool={payload.get('tool')!r} "
          f"shop_key={'set' if str(payload.get('shop_key') or '').strip() else 'EMPTY'} "
          f"args={json.dumps(shown, ensure_ascii=False)[:300]}", flush=True)
    return agent.handle(payload.get("tool") or "",
                        payload.get("caller") or payload.get("From") or "",
                        args, key=payload.get("shop_key") or "")


@app.post("/api/agent/tool/{tool_name}")
def agent_tool_named(tool_name: str, request: Request,
                     payload: dict = Body(default={}),
                     x_agent_secret: str = Header(default=""),
                     authorization: str = Header(default="")):
    """Same webhook, but the tool is named in the PATH and the body is nothing
    but the model's own arguments.

    The console substitutes declared variables in a request body and does not
    substitute the model's tool arguments, so a body written as
    {"item": "{{item}}"} arrives with that placeholder intact, every time, on
    every tool that takes an argument. Moving the tool name into the path and
    the identity into the query string leaves the body free to be whatever the
    model composed, which is the one thing that does arrive intact.

    Identity comes from the query string here. That is a real trade-off: URLs
    are recorded in access logs in a way that headers and bodies are not, so
    prefer /api/agent/tool with a header when the console can manage it.
    """
    import agent
    q = request.query_params
    supplied = (x_agent_secret
                or (authorization or "").removeprefix("Bearer ").strip()
                or str(q.get("secret") or "").strip())
    if "{{" in supplied:
        supplied = ""
    print(f"[agent] path-tool={tool_name!r} "
          f"secret={'present' if supplied else 'MISSING'} "
          f"args={json.dumps(payload, ensure_ascii=False)[:300]}", flush=True)
    try:
        if not agent.verify_secret(supplied):
            return _agent_auth_refusal()
    except agent.AgentError as e:
        print(f"[agent] auth configuration failed: {type(e).__name__}",
              flush=True)
        return _agent_auth_refusal("agent_secret_not_configured")
    args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
    clean_args = {
        k: v for k, v in (args or {}).items()
        if k not in ("tool", "caller", "secret", "shop_key")
    }
    result = agent.handle(
        tool_name, q.get("caller") or q.get("from") or "",
        clean_args, key=q.get("shop_key") or "")
    if str(q.get("debug") or "").lower() in ("1", "true", "yes"):
        # Opt-in and sanitised. This is intentionally absent from normal tool
        # replies, but lets a dashboard test prove what crossed the HTTP
        # boundary rather than relying on the model's tool card.
        result["_trace"] = {
            "tool": tool_name,
            "received_args": agent._scrub({
                k: v for k, v in clean_args.items() if k != "request_id"
            }),
            "caller_present": bool(q.get("caller") or q.get("from")),
            "shop_key_present": bool(q.get("shop_key")),
        }
        result["facts"] = agent._facts(result)
    return result


@app.get("/api/agent/tools")
def agent_tools():
    """The tool catalogue, for wiring up the Samvaad console."""
    import agent
    return {"tools": agent.manifest()}


@app.get("/api/voice/session")
def voice_session():
    """Hand the logged-in browser its shop_key for the in-app voice session.

    Deliberately NOT under /api/agent/ (which is open so Samvaad can reach the
    webhook) — this one must sit behind the session check, since the key it
    returns is what proves shop identity to every tool.
    """
    import agent
    user = current_user()
    try:
        return {"shop_key": agent.shop_key(user["user_id"]),
                "shop": user.get("shop_name") or "",
                "owner": user.get("name") or "",
                "caller_number": re.sub(r"\D", "", user.get("phone") or ""),
                "samvaad": samvaad_runtime.browser_config()}
    except (agent.AgentError, samvaad_runtime.SamvaadConfigurationError) as e:
        raise HTTPException(503, str(e))


@app.get("/api/voice/presentations")
def voice_presentations(since: str):
    """Cards emitted by Samvaad tools during this browser's current session."""
    try:
        start = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.astimezone()
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid since timestamp.")
    return {"presentations":
            presentations.list_since(current_user()["user_id"], start)}


@app.get(
    "/api/voice/samvaad/orgs/{org_id}/workspaces/{workspace_id}"
    "/apps/{app_id}/url"
)
async def samvaad_signed_url(
    org_id: str,
    workspace_id: str,
    app_id: str,
    interaction_type: str = "call",
    version: int | None = None,
):
    """Issue a short-lived Samvaad URL to the logged-in browser.

    Authentication is enforced by the normal /api middleware.  The browser
    receives a signed session URL, never SAMVAAD_API_KEY itself.
    """
    current_user()
    try:
        payload = await samvaad_runtime.get_signed_url(
            org_id,
            workspace_id,
            app_id,
            interaction_type=interaction_type,
            version=version,
        )
        return JSONResponse(
            payload,
            headers={"Cache-Control": "no-store, private"},
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except samvaad_runtime.SamvaadConfigurationError as exc:
        raise HTTPException(503, str(exc))


# ---------------------------------------------------------------------------
# Generated documents (Twilio fetches these; token is the only guard)
# ---------------------------------------------------------------------------
@app.get("/d/{token}/{filename}")
def serve_document(token: str, filename: str):
    doc = documents.fetch(token)
    if not doc:
        raise HTTPException(404, "This link has expired.")
    return Response(content=doc["content"], media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'inline; filename="{doc["filename"]}"'})


# ---------------------------------------------------------------------------
# Static page
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# State / learning toggle
# ---------------------------------------------------------------------------
@app.get("/api/state")
def state():
    return {
        "shop": repo.load_config().get("shop_name") or "My Shop",
        "today": clock.today().isoformat(),
        "catalogue": repo.load_catalogue(),
        "learning_state": repo.learning_state(),
        "learning_counts": repo.learning_counts(),
    }


@app.get("/api/config")
def get_config():
    return repo.load_config()


@app.post("/api/config")
def set_config(payload: dict = Body(...)):
    repo.save_config(payload)
    return repo.load_config()


@app.post("/api/toggle")
def toggle(payload: dict = Body(...)):
    which = payload.get("which", "day1")
    repo.set_learning_state("day60" if which == "day60" else "day1")
    if repo.learning_state() == "day60":
        seed_path = DATA / "learning_day60.json"
        if seed_path.exists():
            repo.seed_learning(json.loads(seed_path.read_text(encoding="utf-8")))
    return {"learning_state": repo.learning_state(),
            "learning_counts": repo.learning_counts()}


# ---------------------------------------------------------------------------
# Transcription (Saaras) — both modes
# ---------------------------------------------------------------------------
@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)):
    audio = await file.read()
    if not sarvam_client.has_key():
        raise HTTPException(400, "SARVAM_API_KEY not set — type the phrase instead (text box).")
    out = sarvam_client.transcribe_both(audio, file.filename or "audio.wav")
    return out


# ---------------------------------------------------------------------------
# Sale / delivery / count parsing  (LLM tool-call, nlp fallback)
# ---------------------------------------------------------------------------
_RECORD_SALE_TOOL = [{
    "type": "function",
    "function": {
        "name": "record_sale",
        "description": "Extract line items from a shopkeeper's spoken sale/delivery. "
                       "Resolve any mid-sentence self-correction (e.g. 'do ton nahi teen ton' -> 3).",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_phrase": {"type": "string"},
                            "qty": {"type": "number"},
                            "unit": {"type": "string"},
                            "rate": {"type": "number"},
                            "payment": {"type": "string", "enum": ["cash", "credit"]},
                        },
                        "required": ["product_phrase", "qty"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}]


def _llm_items(transcript: str):
    if not sarvam_client.has_key():
        return None
    try:
        resp = sarvam_client.chat(
            [{"role": "system", "content": "You extract sale line items from Hindi/English "
              "code-mixed speech for a hardware shop. Always call record_sale."},
             {"role": "user", "content": transcript}],
            tools=_RECORD_SALE_TOOL, tool_choice="auto")
        msg = sarvam_client.chat_message(resp)
        for tc in msg.get("tool_calls", []) or []:
            if tc["function"]["name"] == "record_sale":
                args = json.loads(tc["function"]["arguments"])
                return args.get("items", [])
    except Exception:
        return None
    return None


def _resolve_items(items: list, flow: str, learning: dict, catalogue: list) -> list:
    out = []
    for it in items:
        phrase = it.get("product_phrase", "")
        m = M.match(phrase, catalogue, learning, flow)
        row = {"phrase": phrase, "qty": it.get("qty"), "unit": it.get("unit"),
               "rate": it.get("rate"), "payment": it.get("payment"),
               "self_corrected": it.get("self_corrected", False), "match": m}
        if m["status"] == "matched":
            sku = repo.sku(m["sku_id"])
            if not it.get("unit"):
                u = M.resolve_unit(phrase, sku, learning)
                row["unit"] = u["unit"]
                row["unit_assumed"] = u["assumed"]
        out.append(row)
    return out


@app.post("/api/parse")
async def parse(payload: dict = Body(...)):
    """Body: {text?, flow}. Returns transcript, resolved items, temporal."""
    flow = payload.get("flow", "live_sale")
    text = payload.get("text", "").strip()
    catalogue = repo.load_catalogue()
    learning = repo.load_learning()

    # Self-correction ("...nahi teen ton") is handled deterministically by the
    # nlp parser (the LLM tends to emit both the struck and corrected value as
    # separate items). For everything else, Sarvam-30B tool-calling is primary.
    import re as _re
    has_negation = bool(_re.search(r"\bnah?in?\b|nahi+", text.lower()))
    if has_negation:
        items = nlp.parse_sale_utterance(text)
    else:
        items = _llm_items(text) or nlp.parse_sale_utterance(text)
    resolved = _resolve_items(items, flow, learning, catalogue)
    temporal = M.resolve_temporal(text, clock.today())
    temporal["date"] = temporal["date"].isoformat()
    return {"transcript": text, "items": resolved, "temporal": temporal}


@app.post("/api/converse")
def converse(payload: dict = Body(...)):
    """
    Voice conversation turn (spec: all follow-ups by voice).
    Body: {state?, text, flow}. state=None starts a new dialogue from `text`;
    otherwise `text` is the answer to the last question. Returns the next thing
    Chhotu should SAY (text + Bulbul audio), whether to keep listening, the
    running summary, and — when done — the committed result.
    """
    import conversation
    state = payload.get("state")
    text = payload.get("text", "")
    flow = payload.get("flow", "live_sale")
    out = conversation.converse(state, text, flow, repo)
    # speak the question/confirmation — in whichever language this
    # conversation has been detected in, or the reply comes out Hindi-accented
    # even when the text itself is English (e.g. numbers get read the Hindi way).
    tts_lang = "en-IN" if (out.get("state") or {}).get("lang") == "en" else "hi-IN"
    out["say_audio_b64"] = None
    if out.get("say") and sarvam_client.has_key():
        speak_text = _devanagari_for_tts(out["say"]) if tts_lang == "hi-IN" else out["say"]
        try:
            out["say_audio_b64"] = sarvam_client.text_to_speech(speak_text, tts_lang)
        except Exception as e:
            out["tts_error"] = str(e)
    out["learning_counts"] = repo.learning_counts()
    return out


@app.post("/api/say")
def say(payload: dict = Body(...)):
    """TTS a short line (used for the 'Haan, boliye' wake prompt)."""
    text = payload.get("text", "")
    if not text or not sarvam_client.has_key():
        return {"audio_b64": None}
    try:
        return {"audio_b64": sarvam_client.text_to_speech(_devanagari_for_tts(text), "hi-IN")}
    except Exception as e:
        return {"audio_b64": None, "error": str(e)}


@app.post("/api/resolve")
def resolve(payload: dict = Body(...)):
    """Re-run the matcher for a refined phrase (after a disambiguation tap)."""
    phrase = payload.get("phrase", "")
    flow = payload.get("flow", "live_sale")
    m = M.match(phrase, repo.load_catalogue(), repo.load_learning(), flow)
    return m


# ---------------------------------------------------------------------------
# Commit events  (sale / delivery / opening_balance / stock_take)
# ---------------------------------------------------------------------------
@app.post("/api/commit")
def commit(payload: dict = Body(...)):
    """
    Body: {type, items:[{sku_id, qty, unit, rate?, payment?, spoken?, rejected?,
    was_tap?}], occurred_on?, precision?}
    Appends events (never mutates stock), records learning, returns affected
    stock recomputed from the log (so back-dated inserts show recompute).
    """
    etype = payload["type"]
    occurred_on = payload.get("occurred_on") or clock.today().isoformat()
    precision = payload.get("precision", "exact")
    result = _write_events(etype, payload.get("items", []), occurred_on, precision,
                           payload.get("source", "voice_live"))
    # learn from the confirmation/tap (tap flow only; conversation learns inline)
    for it in payload.get("items", []):
        if it.get("spoken") and it.get("sku_id"):
            LEARN.record_confirmation(repo, it["spoken"], it["sku_id"],
                                      rejected=it.get("rejected", []),
                                      unit=it.get("unit"),
                                      was_tap=it.get("was_tap", False))
    result["learning_counts"] = repo.learning_counts()
    return result


def _write_events(etype: str, items: list, occurred_on: str, precision: str,
                  source: str, request_id: str = "") -> dict:
    """Append events (never mutates stock) and return recomputed affected stock."""
    catalogue_by = by_id()
    committed = []
    for it in items:
        sku_id = it.get("sku_id")
        sku = catalogue_by.get(sku_id)
        if not sku:
            continue
        quoted_rate = it.get("rate")
        rate_unit = it.get("rate_unit")
        rate_assumed = False
        if etype == "sale" and quoted_rate is None:
            cost = L.landed_cost_as_of(sku, repo.all_events(), L._d(occurred_on))
            quoted_rate = round((cost or 0) * 1.10, 1)
            rate_unit = L.base_unit(sku)
            rate_assumed = True
        base_rate = (L.rate_to_base(float(quoted_rate), rate_unit, sku)
                     if quoted_rate is not None and rate_unit
                     else quoted_rate)
        conf = L.compute_confidence(0.9, occurred_on,
                                    datetime.now().isoformat(timespec="seconds"),
                                    precision)
        ev = {
            "type": etype, "sku_id": sku_id,
            "qty": float(it["qty"]), "unit": it.get("unit", sku["default_unit"]),
            # `rate` remains per base unit for all ledger math. Preserve the
            # owner's quoted rate/unit separately for bills and audit display.
            "rate": base_rate,
            "quoted_rate": quoted_rate if rate_unit else None,
            "rate_unit": rate_unit,
            "payment": it.get("payment") if etype == "sale" else None,
            "customer_id": it.get("customer_id") if etype == "sale" else None,
            "payment_deadline": it.get("payment_deadline") if etype == "sale" else None,
            "occurred_on": occurred_on, "precision": precision,
            "count_precision": it.get("count_precision"),
            "occurred_at": None, "confidence": conf, "source": source,
            # request_id rides along so a retried write can be recognised as
            # the same write rather than appended a second time.
            "evidence": {"transcript": it.get("spoken", ""),
                         **({"request_id": request_id} if request_id else {})},
        }
        eid = repo.append_event(ev)
        amount = (L.line_amount(
            float(it["qty"]), it.get("unit", sku["default_unit"]),
            float(quoted_rate), rate_unit, sku)
            if quoted_rate is not None else 0)
        committed.append({
            "event_id": eid, "sku_id": sku_id,
            "rate": quoted_rate, "rate_unit": rate_unit,
            "base_rate": base_rate, "rate_assumed": rate_assumed,
            "amount": round(amount, 2),
        })
    events = repo.all_events()
    affected = {}
    for c in committed:
        sku = catalogue_by[c["sku_id"]]
        affected[c["sku_id"]] = _stock_view(sku, L._stock_detail(sku, events))
    return {"committed": committed, "affected_stock": affected}


# ---------------------------------------------------------------------------
# Customers / credit / payments
# ---------------------------------------------------------------------------
@app.get("/api/customers")
def customers():
    return {
        "customers": crm.accounts(repo),
        "analytics": crm.analytics(repo, clock.today()),
    }


@app.post("/api/customers")
def save_customer(payload: dict = Body(...)):
    try:
        return repo.upsert_customer(payload.get("phone", ""), payload.get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/customers/{customer_id}")
def update_customer(customer_id: str, payload: dict = Body(...)):
    if not repo.customer(customer_id):
        raise HTTPException(404, "Customer not found.")
    try:
        return repo.update_customer(
            customer_id, payload.get("phone", ""), payload.get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/customers/{customer_id}")
def delete_customer(customer_id: str):
    if not repo.customer(customer_id):
        raise HTTPException(404, "Customer not found.")
    if not repo.delete_customer(customer_id):
        raise HTTPException(
            409,
            "This customer has sales, credit, or payment history and cannot "
            "be deleted. Historical accounts must remain auditable.",
        )
    return {"deleted": True, "customer_id": customer_id}


@app.post("/api/customers/{customer_id}/payments")
def record_payment(customer_id: str, payload: dict = Body(...)):
    customer = repo.customer(customer_id)
    if not customer:
        raise HTTPException(404, "customer not found")
    try:
        amount = float(payload.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    account = crm.account(repo, customer_id)
    if amount <= 0:
        raise HTTPException(400, "payment amount must be positive")
    if not account or amount > account["outstanding"] + 0.01:
        raise HTTPException(400, "payment exceeds outstanding credit")
    row = repo.add_payment(
        customer_id, amount, payload.get("paid_on") or clock.today().isoformat(),
        payload.get("note", ""),
    )
    return {"payment": row, "account": crm.account(repo, customer_id)}


@app.post("/api/customers/{customer_id}/reminder")
def send_customer_reminder(customer_id: str):
    import notify
    if not repo.customer(customer_id):
        raise HTTPException(404, "Customer not found.")
    try:
        return notify.send_customer_reminder(
            repo, current_user(), customer_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Reminder could not be sent: {str(e)[:160]}")


def _stock_view(sku: dict, det: dict) -> dict:
    if det["qty"] == L.UNCOUNTED:
        return {"display": "abhi tak gina nahi", "uncounted": True,
                "unit": sku.get("default_unit")}
    qty = det["qty"]
    prefix = "~" if det.get("estimated") else ""
    unit = det.get("unit", "")
    qs = f"{qty:g}"  # 8.0 -> "8", 4.2 -> "4.2"
    if qty < 0:
        return {"display": f"0 {unit} — recorded se {prefix}{abs(qty):g} zyada bika, gin lo",
                "qty": qty, "uncounted": False, "oversold": True, "unit": unit}
    return {"display": f'{prefix}{qs} {unit}',
            "qty": qty, "uncounted": False, "estimated": det.get("estimated"),
            "unit": unit}


def _low_stock_items(target_repo=None, *, lookback_days: int = 30,
                     cover_days: int = 7, limit: int = 4) -> list:
    """Return counted products likely to run out within ``cover_days``.

    A product is low when fewer than five default units remain, or when this
    shop's own recent sales velocity suggests it will run out within the cover
    window. The quantity floor catches visibly low stock even when there is not
    enough sales history for a time-based estimate.
    """
    source = target_repo or repo
    today = clock.today()
    start = today - timedelta(days=max(1, lookback_days) - 1)
    events = source.all_events()
    rows = []
    for sku in source.load_catalogue():
        detail = L._stock_detail(sku, events, today)
        if detail["qty"] == L.UNCOUNTED:
            continue
        below_quantity_floor = float(detail["qty"]) < 5
        sold_base = 0.0
        for event in events:
            if event.get("type") != "sale" or event.get("sku_id") != sku["sku_id"]:
                continue
            occurred = L._d(event["occurred_on"])
            if start <= occurred <= today:
                sold_base += max(
                    0.0,
                    L.to_base(float(event.get("qty") or 0),
                              event.get("unit") or L.base_unit(sku), sku),
                )
        remaining_base = max(0.0, float(detail.get("base") or 0))
        days_left = None
        if sold_base > 0:
            daily_velocity = sold_base / max(1, lookback_days)
            days_left = remaining_base / daily_velocity
        running_out_soon = days_left is not None and days_left <= cover_days
        if not below_quantity_floor and not running_out_soon:
            continue
        view = _stock_view(sku, detail)
        rows.append({
            "sku_id": sku["sku_id"],
            "canonical": sku["canonical"],
            "stock": view["display"],
            "days_left": round(days_left, 1) if days_left is not None else None,
            "out_of_stock": remaining_base <= 0,
            "reason": ("below_5" if below_quantity_floor
                       else "seven_day_cover"),
        })
    rows.sort(key=lambda row: (
        not row["out_of_stock"],
        row["days_left"] if row["days_left"] is not None else float("inf"),
        row["canonical"],
    ))
    return rows[:max(0, limit)]


# ---------------------------------------------------------------------------
# Stock listing + reconciliation
# ---------------------------------------------------------------------------
@app.get("/api/stock")
def stock(as_of: str = None):
    events = repo.all_events()
    ao = L._d(as_of) if as_of else None
    rows = []
    for sku in repo.load_catalogue():
        det = L._stock_detail(sku, events, ao)
        attrs = sku.get("attributes") or {}
        rows.append({"sku_id": sku["sku_id"], "canonical": sku["canonical"],
                     "family": sku["family"], "brand": attrs.get("brand"),
                     "type": attrs.get("type") or (
                         f"{attrs['diameter_mm']}mm {attrs.get('grade', '')}".strip()
                         if attrs.get("diameter_mm") else None),
                     "default_unit": sku.get("default_unit"),
                     "cost_price": sku.get("opening_cost_per_kg"),
                     "selling_rate": attrs.get("selling_rate"),
                     "gst_rate": sku.get("gst_rate"),
                     **_stock_view(sku, det)})
    return {"as_of": (as_of or clock.today().isoformat()), "rows": rows}


@app.post("/api/stock")
def add_stock_item(payload: dict = Body(...)):
    import agent as AGENT
    attributes = dict(payload.get("attributes") or {})
    if payload.get("type"):
        attributes["type"] = str(payload["type"]).strip()
    result = AGENT.add_item(
        repo, current_user(), {**payload, "attributes": attributes})
    if not result.get("added"):
        raise HTTPException(400, result.get("speak") or "Product add nahi hua.")
    return result


@app.patch("/api/stock/{sku_id}")
def update_stock_item(sku_id: str, payload: dict = Body(...)):
    import agent as AGENT
    if not repo.sku(sku_id):
        raise HTTPException(404, "Product inventory mein nahi mila.")
    result = AGENT.update_item(
        repo, current_user(), {**payload, "sku_id": sku_id})
    if not result.get("updated"):
        raise HTTPException(400, result.get("speak") or "Product update nahi hua.")
    return result


@app.delete("/api/stock/{sku_id}")
def delete_stock_item(sku_id: str):
    sku = repo.sku(sku_id)
    if not sku:
        raise HTTPException(404, "Product inventory mein nahi mila.")
    if repo.events_for_sku(sku_id):
        raise HTTPException(
            409,
            "Is product ka ledger history hai, isliye ise delete nahi kar sakte.",
        )
    if not repo.delete_sku(sku_id):
        raise HTTPException(409, "Product delete nahi ho saka. Dobara try karein.")
    return {"deleted": True, "sku_id": sku_id, "name": sku["canonical"]}


@app.get("/api/reconciliations")
def reconciliations():
    events = repo.all_events()
    catalogue_by = by_id()
    out = []
    for e in events:
        if e["type"] != "stock_take":
            continue
        sku = catalogue_by.get(e["sku_id"])
        if not sku:
            continue
        r = L.reconciliation_delta(sku, events, e)
        if r.get("delta"):
            out.append({"sku_id": e["sku_id"], "canonical": sku["canonical"],
                        "occurred_on": e["occurred_on"], **r})
    return {"reconciliations": out}


# ---------------------------------------------------------------------------
# Today — margin + cash/credit split + provisional + TTS
# ---------------------------------------------------------------------------
def _today_summary() -> dict:
    events = repo.all_events()
    m = L.margin_for_day(by_id(), events, clock.today())
    for line in m.get("lines", []):
        cust = repo.customer(line["customer_id"]) if line.get("customer_id") else None
        line["customer_name"] = cust["name"] if cust else None
    # week-precision sales anywhere in the current week -> "plus N purani entries"
    week_extra = 0
    for e in events:
        if e["type"] == "sale" and e.get("precision") == "week":
            d = L._d(e["occurred_on"])
            if 0 <= (clock.today() - d).days <= 7:
                week_extra += 1
    m["provisional_extra"] = max(m["provisional_extra"], week_extra)
    return m


@app.get("/api/today")
def today():
    return _today_summary()


@app.get("/api/today/tts")
def today_tts():
    m = _today_summary()
    text = (f"Aaj ki total sale {int(m['total'])} rupaye. "
            f"Margin abhi tak {int(m['margin'])} rupaye. "
            f"Cash aaya {int(m['cash'])} rupaye. "
            f"Udhaar gaya {int(m['credit'])} rupaye.")
    low_stock = _low_stock_items(repo)
    if low_stock:
        warnings = [
            (f"{row['canonical']} khatam ho gaya"
             if row["out_of_stock"]
             else f"{row['canonical']} sirf {row['stock']} bacha hai")
            for row in low_stock[:3]
        ]
        text += f" Stock alert: {', '.join(warnings)}."
    if m["provisional_extra"]:
        text += f" Plus {m['provisional_extra']} purani entries baaki hain."
    if not sarvam_client.has_key():
        return {"text": text, "audio_b64": None}
    try:
        b64 = sarvam_client.text_to_speech(_devanagari_for_tts(text), language_code="hi-IN")
        return {"text": text, "audio_b64": b64}
    except Exception as e:
        return {"text": text, "audio_b64": None, "error": str(e)}


# ---------------------------------------------------------------------------
# Dashboard  (spec 9.6 — build last)
# ---------------------------------------------------------------------------
@app.get("/api/dashboard")
def dashboard():
    events = repo.all_events()
    catalogue = repo.load_catalogue()
    catalogue_by = {s["sku_id"]: s for s in catalogue}
    from datetime import timedelta

    # margin trend, last 30 days
    trend = []
    for i in range(29, -1, -1):
        d = clock.today() - timedelta(days=i)
        mm = L.margin_for_day(catalogue_by, events, d)
        trend.append({"date": d.isoformat(), "margin": mm["margin"]})

    # steel price rise marker (first day a delivery rate jumps)
    rise_date = None
    for sid in ["TMT_12_FE500D_TATA"]:
        deliveries = sorted([e for e in events if e["sku_id"] == sid
                             and e["type"] == "delivery" and e.get("rate")],
                            key=lambda e: e["occurred_on"])
        for a, b in zip(deliveries, deliveries[1:]):
            if b["rate"] > a["rate"] * 1.03:
                rise_date = b["occurred_on"]
                break

    # CRM analytics also gives the current receivable balance after payments,
    # unlike a raw sum of every historical credit sale.
    customer_analytics = crm.analytics(repo, clock.today())
    outstanding = customer_analytics["outstanding_credit"]
    inv_value = 0.0
    excluded = 0
    frozen = []
    for sku in catalogue:
        det = L._stock_detail(sku, events, clock.today())
        if det["qty"] == L.UNCOUNTED:
            excluded += 1
            continue
        cost = L.landed_cost_as_of(sku, events, clock.today()) or 0
        base_qty = det.get("base") or 0
        val = cost * base_qty
        inv_value += val
        # frozen capital: counted, zero movement in 60 days
        sales = [e for e in events if e["sku_id"] == sku["sku_id"]
                 and e["type"] in ("sale", "delivery")
                 and 0 <= (clock.today() - L._d(e["occurred_on"])).days <= 60]
        if not sales and base_qty > 0:
            frozen.append({"sku_id": sku["sku_id"], "canonical": sku["canonical"],
                           "value": round(val, 0),
                           "stock": _stock_view(sku, det)["display"]})
    frozen.sort(key=lambda x: -x["value"])

    return {
        "margin_trend": trend, "steel_rise_date": rise_date,
        "money_position": {"outstanding_credit": round(outstanding, 0),
                           "inventory_value": round(inv_value, 0),
                           "uncounted_excluded": excluded},
        "frozen_capital": frozen[:12],
        "frozen_total": round(sum(f["value"] for f in frozen), 0),
        "customer_analytics": customer_analytics,
    }


# ---------------------------------------------------------------------------
# Invoice ingestion (spec 9.1) — catalogue + landed cost ONLY, no stock
# ---------------------------------------------------------------------------
def _compute_landed(lines: list) -> list:
    """Strip GST (use taxable value), prorate freight by value, /qty (Section 4)."""
    goods = [l for l in lines if l.get("taxable_value") and l.get("qty")
             and l.get("certain") and not l.get("is_freight")]
    total_taxable = sum(l["taxable_value"] for l in goods)
    freight_total = sum(l.get("freight_value", 0) for l in lines if l.get("is_freight"))
    catalogue = repo.load_catalogue()
    learning = repo.load_learning()
    out = []
    for l in lines:
        row = dict(l)
        if l.get("is_freight"):
            row["display"] = f"Freight ₹{l['freight_value']:.0f} (prorated by value)"
            out.append(row)
            continue
        # match to catalogue
        if l.get("product_phrase"):
            m = M.match(l["product_phrase"], catalogue, learning, "delivery")
            row["match"] = m
        if l.get("certain") and l.get("qty") and l.get("taxable_value"):
            sku = None
            mm = row.get("match", {})
            if mm.get("status") == "matched":
                sku = repo.sku(mm["sku_id"])
            base_qty = L.to_base(l["qty"], l.get("unit", "unit"), sku) if sku else l["qty"]
            prorated = freight_total * (l["taxable_value"] / total_taxable) if total_taxable else 0
            landed = (l["taxable_value"] + prorated) / base_qty if base_qty else None
            row["prorated_freight"] = round(prorated, 1)
            row["landed_cost_per_unit"] = round(landed, 2) if landed else None
            gross = l["taxable_value"] * (1 + l.get("gst_rate", 0) / 100)
            row["gross_would_be"] = round(gross / base_qty, 2) if base_qty else None
        out.append(row)
    return out


@app.post("/api/invoice")
async def invoice(file: UploadFile = File(None)):
    """
    Parse an invoice photo -> line items + landed cost. NO stock events created.
    Uses Sarvam Document Digitization when a key is present; otherwise the
    deterministic fixture so the beat is rock-solid on stage.
    """
    used_fixture = True
    lines = None
    if file is not None and sarvam_client.has_key():
        try:
            data = await file.read()
            suffix = Path(file.filename or "invoice.png").suffix or ".png"
            tmp = ROOT / "debug" / f"uploaded_invoice{suffix}"
            tmp.write_bytes(data)
            parsed = sarvam_client.parse_document(str(tmp))
            lines = _lines_from_markdown(parsed.get("markdown", ""))
            used_fixture = not bool(lines)
        except Exception as e:
            print("invoice live parse failed, using fixture:", repr(e)[:200])
            used_fixture = True
    if lines is None or used_fixture:
        fx = json.loads((DATA / "invoice_fixture.json").read_text(encoding="utf-8"))
        lines = fx["lines"]
        meta = {k: fx[k] for k in ("supplier", "invoice_no", "invoice_date", "image")}
    else:
        meta = {"supplier": "Parsed invoice", "invoice_no": "-",
                "invoice_date": clock.today().isoformat(), "image": None}

    priced = _compute_landed(lines)
    return {"used_fixture": used_fixture, "meta": meta, "lines": priced,
            "has_key": sarvam_client.has_key()}


_UNIT_MAP = {"mt": "tonne", "ton": "tonne", "tonne": "tonne", "bags": "bori",
             "bag": "bori", "bori": "bori", "pcs": "piece", "nos": "piece",
             "piece": "piece", "ltr": "litre", "litre": "litre"}


def _num(s: str):
    s = re.sub(r"[^\d.]", "", s or "")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _lines_from_table(md: str) -> list:
    """
    Deterministically parse the Document Digitization HTML table into line items.
    Reliable and fast — no LLM reasoning-token limits. GST stripping / freight
    proration happen later in _compute_landed (Section 4).
    """
    import re as _re
    rows = _re.findall(r"<tr>(.*?)</tr>", md, _re.S | _re.I)
    out, idx = [], 0
    for row in rows:
        cells = [_re.sub(r"<[^>]+>", "", c).strip()
                 for c in _re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, _re.S | _re.I)]
        cells = [c for c in cells if c != ""]
        if not cells:
            continue
        joined = " ".join(cells).lower()
        if "description" in joined or joined.startswith(("qty", "rate", "amount")):
            continue  # header
        if any(k in joined for k in ("sub total", "grand total", "cgst", "sgst",
                                     "igst", "total")):
            continue  # totals
        desc = cells[0]
        amount = _num(cells[-1]) if len(cells) >= 2 else None
        y0 = 0.27 + idx * 0.065
        bbox = [0.06, round(y0, 3), 0.94, round(y0 + 0.055, 3)]
        idx += 1
        # freight / transport
        if "freight" in joined or "transport" in joined:
            out.append({"line_id": f"L{idx}", "raw_text": " | ".join(cells),
                        "is_freight": True, "freight_value": amount or 0,
                        "certain": True, "bbox": bbox})
            continue
        # illegible / smudged
        alpha = sum(ch.isalpha() for ch in desc)
        if "smudg" in joined or "~" in desc or alpha < 3:
            out.append({"line_id": f"L{idx}", "raw_text": " | ".join(cells),
                        "product_phrase": None, "qty": None, "unit": None,
                        "taxable_value": None, "gst_rate": None, "certain": False,
                        "uncertain_reason": "row illegible — crop shown, not guessed",
                        "bbox": bbox})
            continue
        # qty + unit
        qty_cell = cells[1] if len(cells) >= 3 else ""
        qty = _num(qty_cell)
        um = _re.search(r"[a-zA-Z]+", qty_cell)
        unit = _UNIT_MAP.get(um.group(0).lower(), None) if um else None
        handwritten = bool(_re.search(r"@|/|↑|\bcorr", qty_cell)) or qty is None
        gst = 28 if "cement" in desc.lower() else 18
        line = {"line_id": f"L{idx}", "raw_text": " | ".join(cells),
                "product_phrase": desc, "qty": None if handwritten else qty,
                "unit": unit or "tonne", "taxable_value": amount,
                "gst_rate": gst, "certain": not handwritten, "bbox": bbox}
        if handwritten:
            line["uncertain_reason"] = f"handwritten/ambiguous qty: '{qty_cell}' — confirm"
            line["handwritten_note"] = qty_cell
        out.append(line)
    return out


def _lines_from_markdown(md: str) -> list:
    """
    Structure the digitized invoice table into line items. An LLM extraction
    over the actual markdown/HTML text is the PRIMARY path — real invoice
    tables vary in column order and count (Description/HSN/Qty/Unit/Rate/
    Amount, or fewer), and the position-based regex parser below picked the
    wrong column on a real uploaded invoice (read a per-kg RATE as the line's
    total taxable Amount, producing an absurd ₹0.95/kg landed cost). The
    regex table parser is kept only as a fallback for when there's no API
    key or the model call fails outright.
    """
    if md.strip() and sarvam_client.has_key():
        sys_p = (
            "You convert a hardware-shop purchase invoice (given as markdown/HTML "
            "extracted from a scanned document) into JSON. Output ONLY the JSON "
            "object, nothing else. Schema: "
            '{"lines":[{"product_phrase":str,"qty":number|null,"unit":str,'
            '"taxable_value":number,"gst_rate":number,"certain":bool,'
            '"is_freight":bool,"freight_value":number}]}.\n'
            "Column rules — the table may have Description/HSN/Qty/Unit/Rate/"
            "Amount in ANY order, and not every column is always present:\n"
            "- taxable_value is the line's TOTAL pre-GST value (Amount/Value "
            "column) — this is qty × rate, NOT the per-unit rate itself. If "
            "only a per-unit Rate is given with no Amount column, compute "
            "taxable_value = qty × rate yourself.\n"
            "- qty and unit come from the Qty/Unit column(s), never confused "
            "with a rate or amount.\n"
            "- gst_rate is a percentage (e.g. 18, 28), read from a GST/Tax% "
            "column if present, else infer 28 for cement and 18 for steel/TMT.\n"
            "- Illegible/smudged/handwritten-corrected rows: certain=false, and "
            "keep the struck/handwritten value visible in product_phrase.\n"
            "- Transport/Freight rows: is_freight=true with freight_value set "
            "to that row's amount; do not give it a product_phrase or qty.\n"
            "- Skip header rows and Sub Total/GST/Grand Total rows entirely.\n"
            "Be terse — no explanation, just the JSON."
        )
        try:
            out = sarvam_client.chat_json(
                [{"role": "system", "content": sys_p},
                 {"role": "user", "content": md[:6000]}],
                temperature=0.1, max_tokens=4096, timeout=75)
            lines = out.get("lines") if isinstance(out, dict) else None
            if lines:
                return lines
        except Exception as e:
            print("invoice LLM extraction failed, falling back to table regex:",
                  repr(e)[:200])
    return _lines_from_table(md)


@app.post("/api/invoice/commit")
def invoice_commit(payload: dict = Body(...)):
    """
    Commit chosen invoice lines -> catalogue rows + landed cost, AND a
    delivery event for each line's counted quantity — the invoice IS the
    delivery, so a restock should show up in the ledger, not just the cost
    basis. A line the owner hasn't confirmed yet (illegible/handwritten qty,
    "certain": false) still gets NO stock event until they resolve it, same
    as before. A line that doesn't match any existing SKU (a genuinely new
    product on this invoice) gets provisioned into the catalogue first,
    exactly like a voice-reported purchase of something not yet stocked.
    """
    import conversation as C
    lines = payload.get("lines", [])
    committed = []
    for l in lines:
        m = l.get("match", {})
        sku_id = m.get("sku_id") if m.get("status") == "matched" else l.get("sku_id")
        landed = l.get("landed_cost_per_unit")
        if not landed:
            continue
        catalogue_by = by_id()
        freshly_provisioned = not (sku_id and sku_id in catalogue_by)
        if freshly_provisioned:
            phrase = l.get("product_phrase") or ""
            family = ("cement" if re.search(r"cement", phrase, re.I) else
                     "tmt" if re.search(r"\btmt\b|sariya|\bbar\b", phrase, re.I) else None)
            fake_item = {"name": phrase, "unit": l.get("unit")}
            sid = C._provision_new_sku(fake_item, family, repo) if family else None
            # Family-specific provisioning needs a recognizable diameter/type
            # (e.g. "Ambuja Cement 50kg" names no OPC/PPC grade at all) — fall
            # back to a generic entry rather than silently dropping the line.
            sid = sid or C._provision_generic_sku(fake_item, repo)
            if not sid:
                continue
            sku_id = sid
        repo.upsert_sku({"sku_id": sku_id, "landed_cost_per_kg": landed,
                         "opening_cost_per_kg": landed})
        sku = repo.sku(sku_id)
        stock_added = False
        qty = l.get("qty")
        if l.get("certain") and qty:
            ev = {"sku_id": sku_id, "qty": qty, "unit": l.get("unit") or sku["default_unit"],
                 "rate": landed, "rate_unit": L.base_unit(sku),
                 "spoken": l.get("product_phrase", "")}
            # A product new to the catalogue this call has no prior stock to
            # reconcile against — this quantity directly becomes the known
            # count. An EXISTING product's delivery stays a plain delivery —
            # there could be untracked stock already on the shelf, so it's
            # left for a real physical count rather than assumed.
            etype = "opening_balance" if freshly_provisioned else "delivery"
            _write_events(etype, [ev], clock.today().isoformat(), "exact", "invoice_photo")
            stock_added = True
        committed.append({"sku_id": sku_id, "landed_cost": landed,
                          "value": l.get("taxable_value"), "stock_added": stock_added})
    # top-30 checklist ranked by purchase value; flag which still need counting
    events = repo.all_events()
    checklist = []
    ranked = sorted([c for c in committed if c.get("value")],
                    key=lambda c: -c["value"])
    for c in ranked[:30]:
        sku = repo.sku(c["sku_id"])
        det = L._stock_detail(sku, events, clock.today())
        checklist.append({"sku_id": c["sku_id"], "canonical": sku["canonical"],
                          "purchase_value": c["value"],
                          "needs_count": det["qty"] == L.UNCOUNTED,
                          "stock": _stock_view(sku, det)["display"]})
    return {"committed": committed, "checklist": checklist,
            "note": "Catalogue + landed cost updated, and stock quantity added "
                    "for every confirmed line."}


# ---------------------------------------------------------------------------
# GST invoice PDF (bill trigger)
# ---------------------------------------------------------------------------
@app.post("/api/bill")
def bill(payload: dict = Body(...)):
    """The bill the UI downloads — the SAME document that goes out on WhatsApp.

    It used to be a second, thinner rendering with the shop name hardcoded, so
    a printed bill and a sent bill disagreed and neither carried the owner's
    identity. One generator now, one format, per-user letterhead.
    """
    import notify
    import pdfs
    user = current_user()
    customer = payload.get("customer") or {}
    if customer.get("customer_id") and not customer.get("name"):
        customer = repo.customer(customer["customer_id"]) or customer
    who = notify._identity(repo, user)
    on = clock.today()

    rows, subtotal, gst_total = [], 0.0, 0.0
    for it in payload.get("items", []):
        sku = repo.sku(it["sku_id"]) or {}
        qty = float(it["qty"])
        unit = it.get("unit") or sku.get("default_unit")
        rate = float(it.get("rate") or 0)
        amount = L.line_amount(qty, unit, rate, it.get("rate_unit") or unit, sku)
        gst_total += amount * float(repo.gst_rate_for(sku)) / 100.0
        subtotal += amount
        rows.append({"name": sku.get("canonical", it["sku_id"]), "qty": qty,
                     "unit": unit, "rate": rate, "amount": amount})
    total = subtotal + gst_total
    bill_no = payload.get("bill_no") or f"{on:%Y%m%d}-{int(total) % 10000:04d}"
    payment = payload.get("payment") or (payload.get("items") or [{}])[0].get("payment") or "cash"

    pdf = pdfs.bill_pdf(
        shop=who["shop"], owner=who["owner"], customer=customer, lines=rows,
        subtotal=subtotal, gst=gst_total, total=total, bill_no=bill_no, on=on,
        payment=payment, due_on=payload.get("payment_deadline"),
        gstin=who["gstin"], phone=who["phone"], address=who["address"])
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="bill-{bill_no}.pdf"'})


# /api/reset was removed with the move to multi-tenancy. It called seed.main(),
# which rewrites the JSON files this app no longer reads, and there is no
# sensible meaning for "reset" once several shops share one database — the
# nearest honest equivalent is deleting your own account, which is a different
# feature with a different confirmation.


if __name__ == "__main__":
    if "--demo" in sys.argv:
        import seed
        seed.main()
        print("Demo state reset.")
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000)
