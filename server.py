"""mempalace-remote — public HTTP/OAuth front-end for the local MemPalace MCP server.

Model A ("tunnel"): the canonical palace and the MemPalace package stay on this
desktop. This process wraps ``mempalace.mcp_server.handle_request`` (the existing
hand-rolled JSON-RPC dispatcher) in a Streamable-HTTP MCP endpoint guarded by a
minimal OAuth 2.1 authorization server, so the Claude app can add it as a remote
custom connector. Tailscale Funnel or a Cloudflare Tunnel publishes
``127.0.0.1:$BIND_PORT`` — nothing is exposed on the home router.

Single user (Ivan). Two auth modes, picked by $AUTH_MODE:

- ``passphrase`` (default): the OAuth dance claude.ai expects is implemented end
  to end: protected-resource metadata, authorization-server metadata, dynamic
  client registration, authorization-code + PKCE(S256), and refresh tokens. The
  human gate at ``/authorize`` is a single passphrase ($MEMPALACE_REMOTE_PASSPHRASE).
- ``cloudflare-access``: Cloudflare Access (Managed OAuth) is the authorization
  server, at the edge, and a Cloudflare Tunnel publishes this port. Every /mcp
  request must carry the ``Cf-Access-Jwt-Assertion`` Access adds; the built-in
  OAuth endpoints are not served at all.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

# --- auth mode --------------------------------------------------------------
# Validated before the palace package is imported: a bad config exits with one
# clear line and never imports (let alone serves) the palace. run.sh execs this
# file, so the exit reaches systemd as a failed start.
AUTH_MODE = os.environ.get("AUTH_MODE", "").strip() or "passphrase"
if AUTH_MODE not in ("passphrase", "cloudflare-access"):
    raise SystemExit(f"AUTH_MODE={AUTH_MODE!r}: expected 'passphrase' or 'cloudflare-access'")
ACCESS_MODE = AUTH_MODE == "cloudflare-access"

JWKS_TIMEOUT = 10  # seconds, for the startup fetch and every later refetch


def _team_domain(raw: str) -> str:
    """https://<team>.cloudflareaccess.com — the `iss` of every assertion."""
    team = raw.strip().rstrip("/")
    if not team:
        raise SystemExit("CF_ACCESS_TEAM_DOMAIN is required when AUTH_MODE=cloudflare-access")
    if "://" not in team:
        team = "https://" + team
    parts = urlsplit(team)
    if parts.scheme != "https" or not parts.netloc or parts.path or parts.query or parts.fragment:
        raise SystemExit(
            f"CF_ACCESS_TEAM_DOMAIN={raw!r}: expected https://<team>.cloudflareaccess.com"
        )
    return team


def _allowed_emails(raw: Optional[str]) -> Optional[frozenset[str]]:
    """Unset or empty = no origin email check. Set but listing nobody = refuse,
    so a typo can never switch the check off."""
    if not raw:
        return None
    emails = frozenset(e.strip().lower() for e in raw.split(",") if e.strip())
    if not emails:
        raise SystemExit(f"CF_ACCESS_ALLOWED_EMAILS={raw!r} lists no email; unset it or name one")
    return emails


def _within(seconds: float, fn: Any) -> Any:
    """Run fn(), but give up after `seconds` in total. A socket timeout bounds
    each read, not DNS or a slow trickle. The worker is a daemon thread, so a
    fetch that never ends cannot hold the exit either."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            box["error"] = exc

    worker = threading.Thread(target=run, name="jwks-startup", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError(f"no answer within {seconds} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


CF_ACCESS_TEAM = ""
CF_ACCESS_AUD = ""
CF_ACCESS_ALLOWED_EMAILS: Optional[frozenset[str]] = None
_jwks: Any = None

if ACCESS_MODE:
    CF_ACCESS_TEAM = _team_domain(os.environ.get("CF_ACCESS_TEAM_DOMAIN", ""))
    CF_ACCESS_AUD = os.environ.get("CF_ACCESS_AUD", "").strip()
    if not CF_ACCESS_AUD:
        raise SystemExit("CF_ACCESS_AUD is required when AUTH_MODE=cloudflare-access")
    CF_ACCESS_ALLOWED_EMAILS = _allowed_emails(os.environ.get("CF_ACCESS_ALLOWED_EMAILS"))
    # Imported only in this mode: passphrase installs need nothing new.
    try:
        import jwt
        from jwt.algorithms import has_crypto
    except ImportError:
        raise SystemExit(
            "AUTH_MODE=cloudflare-access needs PyJWT: add --with 'pyjwt[crypto]' to the "
            "uv tool install of mempalace"
        ) from None
    if not has_crypto:
        raise SystemExit("AUTH_MODE=cloudflare-access needs pyjwt[crypto] (RS256)")
    _jwks = jwt.PyJWKClient(f"{CF_ACCESS_TEAM}/cdn-cgi/access/certs", timeout=JWKS_TIMEOUT)
    # Fetch once now: a wrong team domain or a hung fetch fails the start
    # loudly instead of turning every request into a quiet 403.
    try:
        _within(JWKS_TIMEOUT, _jwks.get_signing_keys)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"cannot load the Access signing keys from {_jwks.uri}: {exc}") from None

# Importing mempalace.mcp_server runs its module-level "stdio protection" which
# dup2(2,1)'s real stdout onto stderr (meant for the stdio transport). Undo it
# immediately so uvicorn/logging behave normally under HTTP.
from mempalace import mcp_server as mp  # noqa: E402

mp._restore_stdout()
handle_request = mp.handle_request

# --- config ---------------------------------------------------------------
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://your-machine.your-tailnet.ts.net:8443"
).rstrip("/")
PASSPHRASE = os.environ.get("MEMPALACE_REMOTE_PASSPHRASE", "")
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("BIND_PORT", "8789"))
STATE_DIR = Path(
    os.environ.get("MEMPALACE_REMOTE_STATE", os.path.expanduser("~/.mempalace/remote"))
)
STATE_FILE = STATE_DIR / "oauth_state.json"

ACCESS_TTL = 30 * 24 * 3600      # 30 days
REFRESH_TTL = 365 * 24 * 3600    # 1 year
CODE_TTL = 600                    # 10 minutes

ISSUER = PUBLIC_BASE_URL
MCP_RESOURCE = f"{PUBLIC_BASE_URL}/mcp"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mempalace-remote")

# --- tiny persisted OAuth store ------------------------------------------
# Structure: {clients:{id:{redirect_uris:[...]}}, codes:{code:{...}},
#             tokens:{tok:{...}}, refresh:{tok:{...}}}
_store_lock = asyncio.Lock()
_store: dict[str, dict] = {"clients": {}, "codes": {}, "tokens": {}, "refresh": {}}


def _load_store() -> None:
    global _store
    if STATE_FILE.exists():
        try:
            _store = json.loads(STATE_FILE.read_text("utf-8"))
            for k in ("clients", "codes", "tokens", "refresh"):
                _store.setdefault(k, {})
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read oauth state, starting fresh: %s", exc)


def _save_store() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_store), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_FILE)


def _now() -> int:
    return int(time.time())


def _gc() -> None:
    """Drop expired codes/tokens so the store doesn't grow unbounded."""
    now = _now()
    for code, v in list(_store["codes"].items()):
        if v.get("expires", 0) < now:
            _store["codes"].pop(code, None)
    for tok, v in list(_store["tokens"].items()):
        if v.get("expires", 0) < now:
            _store["tokens"].pop(tok, None)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _verify_pkce(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge or "")


# --- mempalace call serialization ----------------------------------------
# handle_request mutates module globals and touches ChromaDB + a SQLite KG,
# neither of which is concurrency-safe. Serialize every JSON-RPC call.
_mp_lock = asyncio.Lock()


async def _dispatch(payload: Any) -> Any:
    loop = asyncio.get_running_loop()
    async with _mp_lock:
        return await loop.run_in_executor(None, handle_request, payload)


# --- app ------------------------------------------------------------------
from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.responses import (  # noqa: E402
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

app = FastAPI(title="mempalace-remote", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def _startup() -> None:
    if ACCESS_MODE:
        # No OAuth state in this mode: oauth_state.json is neither read nor written.
        log.info(
            "mempalace-remote up; auth=cloudflare-access team=%s aud=%s emails=%s",
            CF_ACCESS_TEAM, CF_ACCESS_AUD,
            ",".join(sorted(CF_ACCESS_ALLOWED_EMAILS)) if CF_ACCESS_ALLOWED_EMAILS else "any",
        )
        return
    if not PASSPHRASE:
        log.error("MEMPALACE_REMOTE_PASSPHRASE is empty — /authorize will reject all logins")
    _load_store()
    log.info("mempalace-remote up; auth=passphrase issuer=%s resource=%s", ISSUER, MCP_RESOURCE)


@app.get("/")
async def root() -> PlainTextResponse:
    return PlainTextResponse("mempalace-remote: alive. MCP endpoint at /mcp\n")


# The backend probe blocks for up to 4 s, so it runs off the event loop, in its
# own small pool: /healthz is public (an Access bypass), and a flood of it must
# stall neither /mcp nor the palace calls in the default executor.
_health_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="healthz")


def _probe_backend(qurl: str) -> Response:
    import urllib.request
    try:
        with urllib.request.urlopen(qurl.rstrip("/") + "/healthz", timeout=4) as r:
            if 200 <= r.status < 300:
                return PlainTextResponse("ok\n")
    except Exception:  # noqa: BLE001
        return PlainTextResponse("backend unavailable\n", status_code=503)
    return PlainTextResponse("backend unhealthy\n", status_code=503)


@app.get("/healthz")
async def healthz() -> Response:
    # Deep check: when a remote vector backend is configured, the wrapper is
    # only healthy if that backend answers. Otherwise /healthz would lie
    # (process up, brain unreachable) and the watchdog/sentinel would miss a
    # dead backend.
    qurl = os.environ.get("MEMPALACE_QDRANT_URL")
    if qurl:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_health_pool, _probe_backend, qurl)
    return PlainTextResponse("ok\n")


# --- OAuth discovery metadata --------------------------------------------
_PRM = {
    "resource": MCP_RESOURCE,
    "authorization_servers": [ISSUER],
    "bearer_methods_supported": ["header"],
}


def _asm() -> dict:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "registration_endpoint": f"{ISSUER}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    }


# Clients differ on whether they append the resource path to the well-known
# URL (RFC 8414 §3.1 vs the path-insertion variant). Serve both shapes.
@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
async def prm() -> JSONResponse:
    return JSONResponse(_PRM)


@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/oauth-authorization-server/mcp")
@app.get("/.well-known/openid-configuration")
async def asm() -> JSONResponse:
    return JSONResponse(_asm())


# --- dynamic client registration (RFC 7591) ------------------------------
@app.post("/register")
async def register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse(
            {"error": "invalid_redirect_uri", "error_description": "redirect_uris required"},
            status_code=400,
        )
    client_id = "mp-" + secrets.token_urlsafe(16)
    async with _store_lock:
        _store["clients"][client_id] = {
            "redirect_uris": redirect_uris,
            "created": _now(),
            "client_name": body.get("client_name", ""),
        }
        _save_store()
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": _now(),
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": body.get("client_name", ""),
        },
        status_code=201,
    )


# --- authorization endpoint ----------------------------------------------
_LOGIN_HTML = """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MemPalace — accesso</title>
<style>
 body{{font-family:system-ui,sans-serif;background:#0f1115;color:#e8e8e8;display:flex;
   min-height:100vh;align-items:center;justify-content:center;margin:0}}
 form{{background:#1a1d24;padding:2rem;border-radius:14px;width:min(92vw,360px);
   box-shadow:0 10px 40px rgba(0,0,0,.5)}}
 h1{{font-size:1.1rem;margin:0 0 1rem}} .sub{{color:#9aa;font-size:.85rem;margin-bottom:1rem}}
 input{{width:100%;box-sizing:border-box;padding:.7rem;border-radius:8px;border:1px solid #333;
   background:#0f1115;color:#fff;font-size:1rem}}
 button{{width:100%;margin-top:1rem;padding:.7rem;border:0;border-radius:8px;background:#6c5ce7;
   color:#fff;font-size:1rem;cursor:pointer}}
 .err{{color:#ff7675;font-size:.85rem;margin-bottom:.6rem}}
</style></head><body>
<form method="post" action="/authorize">
 <h1>🏛️ MemPalace</h1>
 <div class="sub">Autorizza l'app Claude ad accedere al tuo Memory Palace.</div>
 {error}
 <input type="password" name="passphrase" placeholder="Passphrase" autofocus autocomplete="off">
 <input type="hidden" name="client_id" value="{client_id}">
 <input type="hidden" name="redirect_uri" value="{redirect_uri}">
 <input type="hidden" name="state" value="{state}">
 <input type="hidden" name="code_challenge" value="{code_challenge}">
 <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
 <input type="hidden" name="scope" value="{scope}">
 <button type="submit">Autorizza</button>
</form></body></html>"""


def _client_ok(client_id: str, redirect_uri: str) -> bool:
    c = _store["clients"].get(client_id)
    return bool(c) and redirect_uri in c.get("redirect_uris", [])


@app.get("/authorize")
async def authorize_get(request: Request) -> Response:
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    if q.get("response_type") != "code":
        return PlainTextResponse("unsupported_response_type", status_code=400)
    if not _client_ok(client_id, redirect_uri):
        return PlainTextResponse("invalid client_id / redirect_uri", status_code=400)
    if q.get("code_challenge_method", "S256") != "S256" or not q.get("code_challenge"):
        return PlainTextResponse("PKCE S256 required", status_code=400)
    html = _LOGIN_HTML.format(
        error="",
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=q.get("state", ""),
        code_challenge=q.get("code_challenge", ""),
        code_challenge_method=q.get("code_challenge_method", "S256"),
        scope=q.get("scope", "mcp"),
    )
    return HTMLResponse(html)


@app.post("/authorize")
async def authorize_post(
    request: Request,
    passphrase: str = Form(""),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form("S256"),
    scope: str = Form("mcp"),
) -> Response:
    if not _client_ok(client_id, redirect_uri):
        return PlainTextResponse("invalid client_id / redirect_uri", status_code=400)
    if not PASSPHRASE or not secrets.compare_digest(passphrase, PASSPHRASE):
        html = _LOGIN_HTML.format(
            error='<div class="err">Passphrase errata.</div>',
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            code_challenge=code_challenge, code_challenge_method=code_challenge_method,
            scope=scope,
        )
        return HTMLResponse(html, status_code=401)
    code = _new_token()
    async with _store_lock:
        _gc()
        _store["codes"][code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "scope": scope,
            "expires": _now() + CODE_TTL,
        }
        _save_store()
    sep = "&" if "?" in redirect_uri else "?"
    loc = f"{redirect_uri}{sep}code={code}"
    if state:
        loc += f"&state={state}"
    return RedirectResponse(loc, status_code=302)


# --- token endpoint -------------------------------------------------------
def _issue_tokens(client_id: str, scope: str) -> dict:
    access = _new_token()
    refresh = _new_token()
    _store["tokens"][access] = {
        "client_id": client_id, "scope": scope, "expires": _now() + ACCESS_TTL,
    }
    _store["refresh"][refresh] = {
        "client_id": client_id, "scope": scope, "expires": _now() + REFRESH_TTL,
    }
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_token": refresh,
        "scope": scope,
    }


@app.post("/token")
async def token(request: Request) -> JSONResponse:
    form = await request.form()
    grant = form.get("grant_type")
    async with _store_lock:
        _gc()
        if grant == "authorization_code":
            code = form.get("code", "")
            client_id = form.get("client_id", "")
            redirect_uri = form.get("redirect_uri", "")
            verifier = form.get("code_verifier", "")
            entry = _store["codes"].pop(code, None)
            if not entry or entry["expires"] < _now():
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if entry["client_id"] != client_id or entry["redirect_uri"] != redirect_uri:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if not _verify_pkce(verifier, entry["code_challenge"]):
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"}, status_code=400)
            out = _issue_tokens(client_id, entry.get("scope", "mcp"))
            _save_store()
            return JSONResponse(out)
        if grant == "refresh_token":
            rt = form.get("refresh_token", "")
            entry = _store["refresh"].pop(rt, None)
            if not entry or entry["expires"] < _now():
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            out = _issue_tokens(entry["client_id"], entry.get("scope", "mcp"))
            _save_store()
            return JSONResponse(out)
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# In cloudflare-access mode, Access is the authorization server (at the edge).
# Drop every built-in OAuth route, so the passphrase login page cannot be
# reached from any path, the tailnet included: these paths answer 404.
_OAUTH_PATHS = frozenset({
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/.well-known/openid-configuration",
    "/register",
    "/authorize",
    "/token",
})
if ACCESS_MODE:
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) not in _OAUTH_PATHS
    ]


# --- the MCP endpoint -----------------------------------------------------
def _valid_bearer(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    tok = auth[7:].strip()
    entry = _store["tokens"].get(tok)
    return bool(entry) and entry.get("expires", 0) >= _now()


def _unauthorized() -> Response:
    return JSONResponse(
        {"error": "invalid_token"},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"'
            )
        },
    )


def _check_assertion(token: str) -> tuple[bool, str]:
    """Verify a Cf-Access-Jwt-Assertion. Blocking (a new `kid` refetches the
    JWKS), so it runs in a worker thread. Never raises: returns (True, email)
    or (False, reason). The reason never contains the token."""
    try:
        key = _jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=CF_ACCESS_AUD,
            issuer=CF_ACCESS_TEAM,
            leeway=30,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        return False, "expired"
    except jwt.InvalidAudienceError:
        return False, "aud"
    except jwt.InvalidIssuerError:
        return False, "iss"
    except jwt.InvalidSignatureError:
        return False, "signature"
    except jwt.PyJWKClientError as exc:  # unknown kid, JWKS refetch failed
        return False, f"signing key ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001 — malformed, wrong alg, anything else
        return False, f"invalid ({type(exc).__name__})"
    email = claims.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    if CF_ACCESS_ALLOWED_EMAILS is not None and email not in CF_ACCESS_ALLOWED_EMAILS:
        return False, f"email {email or '(missing)'} not allowed"
    return True, email or "(no email)"


async def _authorized(request: Request) -> bool:
    if not ACCESS_MODE:
        return _valid_bearer(request)
    # The Authorization header is ignored here: what proves the caller is the
    # assertion Access adds after it has checked the client's own bearer.
    token = request.headers.get("cf-access-jwt-assertion", "")
    if token:
        loop = asyncio.get_running_loop()
        ok, detail = await loop.run_in_executor(None, _check_assertion, token)
    else:
        ok, detail = False, "missing assertion"
    if ok:
        log.info("access ok: %s %s %s", detail, request.method, request.url.path)
    else:
        log.warning("access denied: %s (%s %s)", detail, request.method, request.url.path)
    return ok


def _denied() -> Response:
    if ACCESS_MODE:
        # No WWW-Authenticate: Access owns the challenge at the edge.
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return _unauthorized()


def _sse(payload: Any) -> str:
    """Encode a JSON-RPC message as a single Server-Sent Event."""
    return f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    if not await _authorized(request):
        return _denied()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    # Streamable HTTP: clients send Accept: application/json, text/event-stream
    # and expect responses as SSE. The `initialize` response must carry an
    # Mcp-Session-Id the client echoes on subsequent calls (we accept any).
    accept = request.headers.get("accept", "")
    wants_sse = "text/event-stream" in accept
    headers: dict[str, str] = {}
    method = body.get("method") if isinstance(body, dict) else None
    if method == "initialize":
        headers["Mcp-Session-Id"] = secrets.token_hex(16)

    # Batch (older protocol) vs single object.
    if isinstance(body, list):
        responses = []
        for item in body:
            r = await _dispatch(item)
            if r is not None:
                responses.append(r)
        if not responses:
            return Response(status_code=202, headers=headers)
        if wants_sse:
            body_txt = "".join(_sse(r) for r in responses)
            return Response(body_txt, media_type="text/event-stream", headers=headers)
        return JSONResponse(responses, headers=headers)

    resp = await _dispatch(body)
    if resp is None:
        # notification — no JSON-RPC response
        return Response(status_code=202, headers=headers)
    if wants_sse:
        return Response(_sse(resp), media_type="text/event-stream", headers=headers)
    return JSONResponse(resp, headers=headers)


# Streamable HTTP clients may open GET /mcp for a server->client SSE channel.
# We don't push server-initiated messages, so reply 405 (allowed by spec).
@app.get("/mcp")
async def mcp_get(request: Request) -> Response:
    if not await _authorized(request):
        return _denied()
    return Response(status_code=405, headers={"Allow": "POST"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=BIND_HOST, port=BIND_PORT, log_level="info")
