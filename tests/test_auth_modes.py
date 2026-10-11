"""Tests for the two auth modes of server.py and the run.sh guard.

They never import the real palace and never touch ~/.mempalace: every load of
server.py gets a stub ``mempalace.mcp_server``, a fresh temporary
MEMPALACE_REMOTE_STATE, and a fake network. Run from the repo root:

  uv run --isolated --no-project --with fastapi --with httpx \\
    --with python-multipart --with 'pyjwt[crypto]' \\
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path
from unittest import mock

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
RUN_SH = ROOT / "run.sh"

TEAM = "https://example-team.cloudflareaccess.com"
CERTS_URL = TEAM + "/cdn-cgi/access/certs"
AUD = "4714c1358e65fe4b408ad6d432a5f878f08194bdb4752441fd56faefa9b2b6f2"
KID = "kid-1"
OWNER = "owner@example.com"

GOOD_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(GOOD_KEY.public_key()))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


def mint(key=GOOD_KEY, alg="RS256", kid=KID, **claims) -> str:
    """A Cloudflare-shaped assertion; a claim passed as None is left out."""
    now = int(time.time())
    payload = {
        "aud": [AUD], "iss": TEAM, "exp": now + 300, "iat": now, "nbf": now,
        "email": OWNER, "sub": "user-1", "type": "app",
    }
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status


class FakeNet:
    """Stands in for every urllib fetch (PyJWT's JWKS client and /healthz)."""

    def __init__(self):
        self.jwks_error: Exception | None = None
        self.qdrant_delay = 0.0
        self.qdrant_error: Exception | None = None
        self.calls: list[tuple[str, object]] = []

    def open(self, director, fullurl, data=None, timeout=None):
        url = fullurl.full_url if isinstance(fullurl, urllib.request.Request) else fullurl
        self.calls.append((url, timeout))
        if url.endswith("/cdn-cgi/access/certs"):
            if self.jwks_error is not None:
                raise self.jwks_error
            return FakeResponse(json.dumps(_jwks()).encode())
        if url.startswith("http://qdrant.test"):
            time.sleep(self.qdrant_delay)
            if self.qdrant_error is not None:
                raise self.qdrant_error
            return FakeResponse(b"healthz check passed")
        raise AssertionError(f"unexpected fetch: {url}")

    def jwks_fetches(self) -> list:
        return [c for c in self.calls if c[0].endswith("/cdn-cgi/access/certs")]


class StubPalace:
    """mempalace + mempalace.mcp_server, recording what the server does."""

    def __init__(self):
        self.restored = 0
        self.requests: list = []
        self.pkg = types.ModuleType("mempalace")
        self.pkg.__path__ = []
        self.srv = types.ModuleType("mempalace.mcp_server")
        self.srv._restore_stdout = self._restore
        self.srv.handle_request = self._handle
        self.pkg.mcp_server = self.srv

    def _restore(self):
        self.restored += 1

    def _handle(self, payload):
        self.requests.append(payload)
        if isinstance(payload, dict) and "id" in payload:
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"stub": True}}
        return None


ACCESS_ENV = {
    "AUTH_MODE": "cloudflare-access",
    "CF_ACCESS_TEAM_DOMAIN": TEAM,
    "CF_ACCESS_AUD": AUD,
}

RPC = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}


class ServerTestCase(unittest.TestCase):
    _n = 0

    def setUp(self):
        self.net = FakeNet()
        self.palace = StubPalace()
        self.state = Path(tempfile.mkdtemp(prefix="mp-remote-test-"))
        self.addCleanup(shutil.rmtree, self.state, True)
        net = self.net

        def fake_open(director, fullurl, data=None, timeout=None):
            return net.open(director, fullurl, data, timeout)

        patcher = mock.patch.object(urllib.request.OpenerDirector, "open", fake_open)
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, **env):
        """Fresh import of server.py with exactly this environment."""
        clean = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("AUTH_MODE", "CF_ACCESS_", "MEMPALACE_", "PUBLIC_BASE_URL"))
        }
        clean["MEMPALACE_REMOTE_STATE"] = str(self.state)
        clean.update(env)
        env_patch = mock.patch.dict(os.environ, clean, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

        saved = {k: sys.modules.get(k) for k in ("mempalace", "mempalace.mcp_server")}
        sys.modules["mempalace"] = self.palace.pkg
        sys.modules["mempalace.mcp_server"] = self.palace.srv

        def restore():
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.addCleanup(restore)

        ServerTestCase._n += 1
        name = f"mempalace_remote_server_{ServerTestCase._n}"
        spec = importlib.util.spec_from_file_location(name, SERVER)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        self.addCleanup(sys.modules.pop, name, None)
        with warnings.catch_warnings():
            # server.py's @app.on_event predates this change; keep the output readable.
            warnings.filterwarnings("ignore", r"\s*on_event is deprecated", DeprecationWarning)
            spec.loader.exec_module(mod)
        return mod

    def client(self, mod) -> TestClient:
        c = TestClient(mod.app)
        c.__enter__()  # runs the startup handler
        self.addCleanup(c.__exit__, None, None, None)
        return c


class AccessModeTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.mod = self.load(**ACCESS_ENV)
        self.c = self.client(self.mod)

    def post(self, token=None, **headers):
        if token is not None:
            headers["Cf-Access-Jwt-Assertion"] = token
        return self.c.post("/mcp", json=RPC, headers=headers)

    def assertForbidden(self, token=None, **headers):
        for method in ("POST", "GET"):
            h = dict(headers)
            if token is not None:
                h["Cf-Access-Jwt-Assertion"] = token
            r = self.c.request(method, "/mcp", json=RPC if method == "POST" else None, headers=h)
            self.assertEqual(r.status_code, 403, f"{method} {r.text}")
            self.assertEqual(r.content, b'{"error":"forbidden"}')
            self.assertNotIn("www-authenticate", r.headers)
        self.assertEqual(self.palace.requests, [], "a denied request reached the palace")

    def test_startup_fetched_jwks_with_timeout(self):
        fetches = self.net.jwks_fetches()
        self.assertEqual(fetches, [(CERTS_URL, 10)])
        self.assertEqual(self.palace.restored, 1)

    def test_valid_assertion_with_unrelated_bearer_is_served(self):
        r = self.post(mint(), Authorization="Bearer xyz")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["result"], {"stub": True})
        self.assertEqual(self.palace.requests, [RPC])

    def test_string_aud_is_served(self):
        self.assertEqual(self.post(mint(aud=AUD)).status_code, 200)

    def test_get_mcp_with_valid_assertion_is_405(self):
        r = self.c.get("/mcp", headers={"Cf-Access-Jwt-Assertion": mint()})
        self.assertEqual(r.status_code, 405)
        self.assertEqual(r.headers["allow"], "POST")

    def test_missing_assertion(self):
        self.assertForbidden()

    def test_signature_from_another_key(self):
        self.assertForbidden(mint(key=OTHER_KEY))

    def test_unknown_kid_refetches_then_denies(self):
        self.mod._jwks.cooldown_duration = 0
        before = len(self.net.jwks_fetches())
        self.assertForbidden(mint(kid="kid-unknown"))
        self.assertGreater(len(self.net.jwks_fetches()), before, "no JWKS refetch")

    def test_jwks_refetch_network_error(self):
        self.mod._jwks.cooldown_duration = 0
        self.net.jwks_error = urllib.error.URLError("connection refused")
        before = len(self.net.jwks_fetches())
        self.assertForbidden(mint(kid="kid-rotated"))
        self.assertGreater(len(self.net.jwks_fetches()), before, "no JWKS refetch")

    def test_wrong_aud(self):
        self.assertForbidden(mint(aud=["another-app"]))

    def test_wrong_iss(self):
        self.assertForbidden(mint(iss="https://evil.cloudflareaccess.com"))

    def test_expired(self):
        self.assertForbidden(mint(exp=int(time.time()) - 3600))

    def test_missing_exp(self):
        self.assertForbidden(mint(exp=None))

    def test_alg_none(self):
        self.assertForbidden(mint(key=None, alg="none"))

    def test_hs256(self):
        self.assertForbidden(mint(key=b"k" * 64, alg="HS256"))

    def test_garbage_header(self):
        self.assertForbidden("not.a.jwt")

    def test_passphrase_bearer_alone_is_ignored(self):
        # Even a token the passphrase mode would accept opens nothing here.
        self.mod._store["tokens"]["tok"] = {"client_id": "c", "expires": time.time() + 999}
        self.assertTrue(self.mod._valid_bearer(
            types.SimpleNamespace(headers={"authorization": "Bearer tok"})))
        self.assertForbidden(Authorization="Bearer tok")

    def test_oauth_endpoints_are_gone(self):
        for method, path in [
            ("GET", "/authorize?response_type=code&client_id=x&redirect_uri=x&code_challenge=x"),
            ("POST", "/authorize"),
            ("POST", "/register"),
            ("POST", "/token"),
            ("GET", "/.well-known/oauth-authorization-server"),
            ("GET", "/.well-known/oauth-authorization-server/mcp"),
            ("GET", "/.well-known/openid-configuration"),
            ("GET", "/.well-known/oauth-protected-resource"),
            ("GET", "/.well-known/oauth-protected-resource/mcp"),
        ]:
            r = self.c.request(method, path)
            self.assertEqual(r.status_code, 404, f"{method} {path}")

    def test_no_oauth_state(self):
        self.post(mint())
        self.assertFalse((self.state / "oauth_state.json").exists())
        self.assertEqual(list(self.state.iterdir()), [])

    def test_healthz(self):
        r = self.c.get("/healthz")
        self.assertEqual((r.status_code, r.text), (200, "ok\n"))

    def test_logs_name_the_reason_never_the_token(self):
        bad = mint(key=OTHER_KEY)
        with self.assertLogs("mempalace-remote", level="INFO") as logs:
            self.post(bad)
            self.post(mint())
        out = "\n".join(logs.output)
        self.assertIn("access denied: signature", out)
        self.assertIn(f"access ok: {OWNER}", out)
        self.assertNotIn(bad, out)
        self.assertNotIn(bad.split(".")[2], out)


class AccessModeVariantsTest(ServerTestCase):
    def test_load_store_never_called(self):
        mod = self.load(**ACCESS_ENV)
        with mock.patch.object(mod, "_load_store") as spy:
            c = self.client(mod)
            c.post("/mcp", json=RPC, headers={"Cf-Access-Jwt-Assertion": mint()})
        spy.assert_not_called()
        self.assertFalse((self.state / "oauth_state.json").exists())

    def test_team_domain_without_scheme_and_with_slash(self):
        mod = self.load(**{**ACCESS_ENV, "CF_ACCESS_TEAM_DOMAIN": "example-team.cloudflareaccess.com/"})
        self.assertEqual(mod.CF_ACCESS_TEAM, TEAM)
        self.assertEqual(self.net.jwks_fetches()[0][0], CERTS_URL)
        r = self.client(mod).post("/mcp", json=RPC, headers={"Cf-Access-Jwt-Assertion": mint()})
        self.assertEqual(r.status_code, 200, r.text)

    def test_allowlist_case_and_spaces(self):
        mod = self.load(**ACCESS_ENV, CF_ACCESS_ALLOWED_EMAILS=" Someone@else.org , OWNER@Example.COM ")
        c = self.client(mod)
        ok = c.post("/mcp", json=RPC, headers={"Cf-Access-Jwt-Assertion": mint(email="Owner@example.com")})
        self.assertEqual(ok.status_code, 200, ok.text)
        for email in ("intruder@example.com", None):
            r = c.post("/mcp", json=RPC, headers={"Cf-Access-Jwt-Assertion": mint(email=email)})
            self.assertEqual(r.status_code, 403, email)
            self.assertEqual(r.content, b'{"error":"forbidden"}')
            self.assertNotIn("www-authenticate", r.headers)

    def test_no_allowlist_accepts_any_email(self):
        mod = self.load(**ACCESS_ENV, CF_ACCESS_ALLOWED_EMAILS="")
        c = self.client(mod)
        r = c.post("/mcp", json=RPC, headers={"Cf-Access-Jwt-Assertion": mint(email="x@y.z")})
        self.assertEqual(r.status_code, 200, r.text)

    def test_healthz_backend_down_is_503(self):
        self.net.qdrant_error = urllib.error.URLError("refused")
        mod = self.load(**ACCESS_ENV, MEMPALACE_QDRANT_URL="http://qdrant.test:6333")
        r = self.client(mod).get("/healthz")
        self.assertEqual((r.status_code, r.text), (503, "backend unavailable\n"))

    def test_slow_healthz_does_not_block_other_requests(self):
        self.net.qdrant_delay = 1.5
        mod = self.load(**ACCESS_ENV, MEMPALACE_QDRANT_URL="http://qdrant.test:6333")

        async def scenario():
            transport = httpx.ASGITransport(app=mod.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                t0 = time.monotonic()
                slow = asyncio.create_task(c.get("/healthz"))
                await asyncio.sleep(0.05)
                fast = await c.get("/")
                fast_after = time.monotonic() - t0
                health = await slow
                return fast.status_code, fast_after, health.status_code

        fast_status, fast_after, health_status = asyncio.run(scenario())
        self.assertEqual((fast_status, health_status), (200, 200))
        self.assertLess(fast_after, 0.75, "GET / waited for the backend probe")


class ImportRefusalTest(ServerTestCase):
    def assertRefuses(self, **env):
        with self.assertRaises(SystemExit) as cm:
            self.load(**env)
        self.assertEqual(self.palace.restored, 0, "the palace was imported")
        return str(cm.exception.code)

    def test_unknown_mode(self):
        self.assertIn("AUTH_MODE", self.assertRefuses(AUTH_MODE="cloudflare"))

    def test_missing_aud(self):
        self.assertIn("CF_ACCESS_AUD", self.assertRefuses(**{**ACCESS_ENV, "CF_ACCESS_AUD": ""}))

    def test_missing_team(self):
        env = dict(ACCESS_ENV)
        del env["CF_ACCESS_TEAM_DOMAIN"]
        self.assertIn("CF_ACCESS_TEAM_DOMAIN", self.assertRefuses(**env))

    def test_plain_http_team(self):
        msg = self.assertRefuses(**{**ACCESS_ENV, "CF_ACCESS_TEAM_DOMAIN": "http://x.cloudflareaccess.com"})
        self.assertIn("CF_ACCESS_TEAM_DOMAIN", msg)

    def test_allowlist_empty_after_parsing(self):
        msg = self.assertRefuses(**ACCESS_ENV, CF_ACCESS_ALLOWED_EMAILS=" , ,")
        self.assertIn("CF_ACCESS_ALLOWED_EMAILS", msg)

    def test_jwks_fetch_failure(self):
        self.net.jwks_error = urllib.error.URLError("no such host")
        self.assertIn("signing keys", self.assertRefuses(**ACCESS_ENV))

    def test_jwks_fetch_timeout(self):
        self.net.jwks_error = TimeoutError("timed out")
        self.assertIn("signing keys", self.assertRefuses(**ACCESS_ENV))
        self.assertEqual(self.net.jwks_fetches(), [(CERTS_URL, 10)])


class PassphraseModeTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.mod = self.load(MEMPALACE_REMOTE_PASSPHRASE="pw", PUBLIC_BASE_URL="https://mp.test")
        self.c = self.client(self.mod)

    def test_default_mode_needs_no_pyjwt(self):
        self.assertEqual(self.mod.AUTH_MODE, "passphrase")
        self.assertFalse(hasattr(self.mod, "jwt"))
        self.assertEqual(self.net.calls, [])

    def test_blank_mode_is_passphrase(self):
        mod = self.load(AUTH_MODE="  ", MEMPALACE_REMOTE_PASSPHRASE="pw")
        self.assertEqual(mod.AUTH_MODE, "passphrase")

    def test_register(self):
        r = self.c.post("/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]})
        self.assertEqual(r.status_code, 201)

    def test_mcp_without_bearer_is_challenged(self):
        r = self.c.post("/mcp", json=RPC)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(
            r.headers["www-authenticate"],
            'Bearer resource_metadata="https://mp.test/.well-known/oauth-protected-resource"',
        )

    def test_assertion_alone_does_not_open_passphrase_mode(self):
        r = self.c.post("/mcp", json=RPC, headers={"Cf-Access-Jwt-Assertion": mint()})
        self.assertEqual(r.status_code, 401)

    def test_authorization_server_metadata(self):
        r = self.c.get("/.well-known/oauth-authorization-server")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["issuer"], "https://mp.test")

    def test_load_store_called(self):
        # Control for AccessModeVariantsTest.test_load_store_never_called.
        mod = self.load(MEMPALACE_REMOTE_PASSPHRASE="pw")
        with mock.patch.object(mod, "_load_store") as spy:
            self.client(mod)
        spy.assert_called_once()


class LauncherTest(unittest.TestCase):
    """run.sh: the passphrase guard holds only in passphrase mode."""

    def run_sh(self, env_file_text: str) -> subprocess.CompletedProcess:
        tmp = Path(tempfile.mkdtemp(prefix="mp-remote-run-"))
        env_file = tmp / "env"
        env_file.write_text(env_file_text)
        stub = tmp / "python"
        stub.write_text('#!/bin/sh\necho "EXEC $*"\n')
        stub.chmod(0o755)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp),
            "MEMPALACE_REMOTE_ENV": str(env_file),
            "MEMPALACE_PYTHON": str(stub),
        }
        return subprocess.run(["bash", str(RUN_SH)], env=env, capture_output=True, text=True, timeout=20)

    def assertExecs(self, p):
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "EXEC server.py")

    def assertRefused(self, p):
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("EXEC", p.stdout)
        self.assertIn("MEMPALACE_REMOTE_PASSPHRASE", p.stderr)

    def test_access_mode_without_passphrase_starts(self):
        self.assertExecs(self.run_sh('AUTH_MODE="cloudflare-access"\n'))

    def test_passphrase_mode_without_passphrase_refuses(self):
        self.assertRefused(self.run_sh('AUTH_MODE="passphrase"\n'))

    def test_no_mode_line_without_passphrase_refuses(self):
        self.assertRefused(self.run_sh(""))

    def test_todays_env_file_starts(self):
        # No AUTH_MODE line at all, as in every install before this change (set -u).
        self.assertExecs(self.run_sh('MEMPALACE_REMOTE_PASSPHRASE="pw"\n'))


if __name__ == "__main__":
    unittest.main()
