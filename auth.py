"""Minimal single-user OAuth 2.1 for the connector.

claude.ai registers itself (dynamic client registration) and sends the user to /login.
The user types the access code (kept in the private bucket at config/mcp_path_token)
once, and claude.ai gets tokens. Every code and token is a signed, self-contained blob, so
nothing but registered clients needs storing. Changing the access code invalidates all
tokens at once.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from typing import Callable

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

ACCESS_TTL = 7 * 24 * 3600
REFRESH_TTL = 365 * 24 * 3600
CODE_TTL = 300
LOGIN_TTL = 900


class SignedBlobs:
    def __init__(self, key: bytes):
        self.key = key

    def dump(self, kind: str, data: dict, ttl: int) -> str:
        body = dict(data, k=kind, x=int(time.time()) + ttl, n=secrets.token_hex(4))
        raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).rstrip(b"=")
        sig = base64.urlsafe_b64encode(hmac.new(self.key, raw, hashlib.sha256).digest()[:24]).rstrip(b"=")
        return f"{raw.decode()}.{sig.decode()}"

    def load(self, kind: str, blob: str) -> dict | None:
        try:
            raw, sig = blob.encode().split(b".")
            want = base64.urlsafe_b64encode(hmac.new(self.key, raw, hashlib.sha256).digest()[:24]).rstrip(b"=")
            if not hmac.compare_digest(sig, want):
                return None
            body = json.loads(base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4)))
        except Exception:
            return None
        if body.get("k") != kind or body.get("x", 0) < time.time():
            return None
        return body


class SingleUserProvider:
    def __init__(self, access_code: str, base_url: str, bucket_factory: Callable | None):
        self.access_code = access_code
        self.base_url = base_url.rstrip("/")
        self.blobs = SignedBlobs(hashlib.sha256(b"mushtariy-oauth:" + access_code.encode()).digest())
        self._bucket = bucket_factory
        self._clients: dict[str, OAuthClientInformationFull] = {}

    # ---- clients (stored in the private bucket so they survive scale-to-zero)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id in self._clients:
            return self._clients[client_id]
        if not self._bucket:
            return None

        def _read():
            blob = self._bucket().blob(f"oauth/clients/{client_id}.json")
            return blob.download_as_text() if blob.exists() else None

        raw = await asyncio.to_thread(_read)
        if raw:
            self._clients[client_id] = OAuthClientInformationFull.model_validate_json(raw)
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        if self._bucket:
            await asyncio.to_thread(
                lambda: self._bucket()
                .blob(f"oauth/clients/{client_info.client_id}.json")
                .upload_from_string(client_info.model_dump_json(), content_type="application/json")
            )

    # ---- authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req = self.blobs.dump(
            "login",
            {
                "c": client.client_id,
                "ch": params.code_challenge,
                "r": str(params.redirect_uri),
                "re": params.redirect_uri_provided_explicitly,
                "s": params.scopes or [],
                "st": params.state,
                "rs": params.resource,
            },
            LOGIN_TTL,
        )
        return f"{self.base_url}/login?req={req}"

    async def login_page(self, request: Request) -> Response:
        if request.method == "GET":
            req = request.query_params.get("req", "")
            if not self.blobs.load("login", req):
                return HTMLResponse(_page("This sign-in link has expired. Start again from Claude."), 400)
            return HTMLResponse(_page(form_req=req))

        form = await request.form()
        req = str(form.get("req", ""))
        code_in = str(form.get("code", "")).strip()
        data = self.blobs.load("login", req)
        if not data:
            return HTMLResponse(_page("This sign-in link has expired. Start again from Claude."), 400)
        if not hmac.compare_digest(code_in.encode(), self.access_code.encode()):
            await asyncio.sleep(1.5)
            return HTMLResponse(_page("That code isn't right.", form_req=req), 401)
        code = self.blobs.dump(
            "code",
            {k: data[k] for k in ("c", "ch", "r", "re", "s", "rs")},
            CODE_TTL,
        )
        return RedirectResponse(construct_redirect_uri(data["r"], code=code, state=data.get("st")), status_code=302)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        d = self.blobs.load("code", authorization_code)
        if not d or d["c"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=d["s"],
            expires_at=d["x"],
            client_id=d["c"],
            code_challenge=d["ch"],
            redirect_uri=d["r"],
            redirect_uri_provided_explicitly=d["re"],
            resource=d.get("rs"),
        )

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        base = {"c": client_id, "s": scopes, "rs": resource}
        return OAuthToken(
            access_token=self.blobs.dump("at", base, ACCESS_TTL),
            expires_in=ACCESS_TTL,
            refresh_token=self.blobs.dump("rt", base, REFRESH_TTL),
            scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str):
        d = self.blobs.load("rt", refresh_token)
        if not d or d["c"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=d["c"], scopes=d["s"], expires_at=d["x"], resource=d.get("rs"))

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str):
        d = self.blobs.load("at", token)
        if not d:
            return None
        return AccessToken(token=token, client_id=d["c"], scopes=d["s"], expires_at=d["x"], resource=d.get("rs"))

    async def revoke_token(self, token) -> None:
        return None  # stateless; rotate the access code to revoke everything


def _page(message: str = "", form_req: str | None = None) -> str:
    form = ""
    if form_req:
        form = f"""
      <form method="post" action="/login">
        <input type="hidden" name="req" value="{html.escape(form_req)}">
        <label for="code">Access code</label>
        <input id="code" name="code" type="password" autocomplete="off" autofocus required>
        <button type="submit">Connect</button>
      </form>"""
    msg = f'<p class="msg">{html.escape(message)}</p>' if message else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Mushtariy connector</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f6f5f2;color:#1d1d1b;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px}}
main{{background:#fff;border:1px solid #e3e1db;border-radius:12px;padding:28px;max-width:360px;width:100%}}
h1{{font-size:18px;margin:0 0 6px}} p{{margin:0 0 18px;color:#5c5a55;font-size:14px}}
label{{display:block;font-size:13px;margin-bottom:6px}}
input[type=password]{{width:100%;box-sizing:border-box;padding:10px;border:1px solid #cfccc4;border-radius:8px;font-size:15px}}
button{{margin-top:14px;width:100%;padding:10px;border:0;border-radius:8px;background:#1d1d1b;color:#fff;font-size:15px;cursor:pointer}}
.msg{{color:#a3322a}}
@media (prefers-color-scheme:dark){{body{{background:#161615;color:#eee}}main{{background:#1f1f1e;border-color:#333}}
p{{color:#aaa}}input[type=password]{{background:#161615;color:#eee;border-color:#444}}button{{background:#eee;color:#161615}}}}
</style></head><body><main>
<h1>Connect Claude to @mushtariy.beta</h1>
<p>Enter the access code from the private bucket (config/mcp_path_token).</p>
{msg}{form}
</main></body></html>"""
