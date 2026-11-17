"""Sign-in with Google or Microsoft, sessions, and the request context (who is asking).

Only people an admin added get in (a row in users, active, with a role), from any email domain. Rules on top:
a Microsoft account must come from a pinned tenant, and an address at a company domain must come from that
domain's own Google Workspace (hd), so a personal Google account can't pass for a company one.
"""
import base64
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..db import tx
from ..settings import S

router = APIRouter()


@dataclass
class Ctx:
    conn: object
    user: dict

    @property
    def uid(self):
        return self.user["id"]

    def can(self, code, family_id=None):
        return self.conn.execute("SELECT has_perm(%s, %s, %s) AS ok", (self.uid, code, family_id)).fetchone()["ok"]

    def require_read(self):
        if not self.can("bom.read_released"):
            raise HTTPException(403, {"code": "no_role", "message": "You have no role in BOMTree yet. Ask an admin."})


def get_ctx(request: Request):
    uid, seen, now = request.session.get("uid"), request.session.get("seen", 0), time.time()
    if not uid or now - seen > S.SESSION_HOURS * 3600:
        request.session.clear()
        raise HTTPException(401, {"code": "signed_out", "message": "Not signed in"})
    if now - seen > 60:
        request.session["seen"] = int(now)
    with tx() as conn:
        user = conn.execute("SELECT id, email, name FROM users WHERE id = %s AND active AND NOT is_system",
                            (uid,)).fetchone()
        if not user:                  # switched off: signed out at once
            request.session.clear()
            raise HTTPException(401, {"code": "signed_out", "message": "Not signed in"})
        conn.execute("SELECT begin_action(%s, 'App')", (uid,))
        yield Ctx(conn, user)


CTX = Depends(get_ctx, scope="function")


# Sign-in ---------------------------------------------------------------------

def audit_sign_in(conn, action, email, reason="", user_id=None):
    """Sign-ins and failed sign-ins are in the audit log, with the provider and why it failed."""
    conn.execute("INSERT INTO audit_log (user_id, source, action, entity_type, entity_id, entity_label, detail) "
                 "VALUES (%s, 'App', %s, 'users', %s, %s, %s)", (user_id, action, user_id, email, reason))


def clean_email(email):
    """A plain address, lower case, or None. Domain is not limited: who gets in is decided by the users table."""
    email = (email or "").strip().lower()
    local, at, domain = email.rpartition("@")
    if not at or not local or "@" in local or "." not in domain or any(c.isspace() for c in email):
        return None
    return email


def safe_next(url):
    """Only a path on this site: no //evil.com, no https://evil.com, no backslash tricks."""
    p = urlparse(url or "/")
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url or p.scheme or p.netloc:
        return "/"
    return url


def sign_in(request, email, provider="dev"):
    raw = (email or "").strip().lower()
    email = clean_email(raw)
    with tx() as conn:
        user = email and conn.execute(
            "SELECT id, active FROM users WHERE email = %s AND NOT is_system", (email,)).fetchone()
        if not user or not user["active"]:
            audit_sign_in(conn, "sign in failed", raw, f"{provider}: {'switched off' if user else 'not invited'}",
                          user and user["id"])
            return None
        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user["id"],))
        audit_sign_in(conn, "sign in", email, provider, user["id"])
    request.session.clear()           # nothing from before sign-in carries over
    request.session.update(uid=user["id"], seen=int(time.time()))
    return user["id"]


# OpenID Connect, authorization code flow ------------------------------------------

def provider_config(name):
    if name == "microsoft" and S.MS_CLIENT_ID:
        tenant = S.MS_TENANT_IDS[0] if len(S.MS_TENANT_IDS) == 1 else "organizations"
        base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
        return {"authorize": f"{base}/authorize", "token": f"{base}/token", "client_id": S.MS_CLIENT_ID,
                "secret": S.MS_CLIENT_SECRET, "extra": {"response_mode": "query", "prompt": "select_account"}}
    if name == "google" and S.GOOGLE_CLIENT_ID:
        return {"authorize": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
                "client_id": S.GOOGLE_CLIENT_ID, "secret": S.GOOGLE_CLIENT_SECRET, "extra": {"prompt": "select_account"}}
    return None


class Denied(Exception):
    pass


def check_claims(provider, claims, nonce):
    """Returns the email to sign in with, or raises Denied with the reason (kept in the audit log)."""
    cfg = provider_config(provider)
    now = time.time()
    if claims.get("aud") != cfg["client_id"]:
        raise Denied("token is for another app")
    if not nonce or claims.get("nonce") != nonce:
        raise Denied("nonce does not match")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - 60 or claims.get("iat", now) > now + 300:
        raise Denied("token expired or not yet valid")
    if provider == "microsoft":
        tid = claims.get("tid")
        if tid not in S.MS_TENANT_IDS:
            raise Denied(f"Microsoft tenant {tid} is not allowed")
        if claims.get("iss") != f"https://login.microsoftonline.com/{tid}/v2.0":
            raise Denied("unexpected issuer")
        # in a pinned tenant both are set by its admins; the UPN can't be edited by the user
        return claims.get("preferred_username") or claims.get("email") or ""
    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise Denied("unexpected issuer")
    if claims.get("email_verified") is not True:
        raise Denied("email not verified")
    email = (claims.get("email") or "").lower()
    domain = email.rpartition("@")[2]
    if domain in S.COMPANY_DOMAINS and (claims.get("hd") or "").lower() != domain:
        raise Denied("a company address must sign in with the company's Google Workspace account")
    return email


def decode_id_token(token):
    # The token comes straight from the provider's token endpoint over TLS, in exchange for our client secret,
    # so its signature need not be re-checked (OpenID Connect Core 3.1.3.7). The claims are checked above.
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


@router.get("/auth/config")
def auth_config():
    return {"microsoft": bool(provider_config("microsoft")), "google": bool(provider_config("google")),
            "dev_login": S.DEV_LOGIN}


@router.get("/auth/login")
def login(request: Request, provider: str = "google", next: str = "/"):
    cfg = provider_config(provider)
    if not cfg:
        raise HTTPException(503 if provider in ("microsoft", "google") else 404, f"{provider.title()} sign-in is not set up")
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    request.session.clear()
    request.session.update(oauth_state=state, oauth_nonce=nonce, oauth_provider=provider, next=safe_next(next))
    return RedirectResponse(cfg["authorize"] + "?" + urlencode({
        "client_id": cfg["client_id"], "response_type": "code", "scope": "openid email profile",
        "redirect_uri": f"{S.PUBLIC_URL}/api/v1/auth/callback", "state": state, "nonce": nonce, **cfg["extra"]}))


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    s = request.session
    expected, nonce, provider, next_url = s.pop("oauth_state", None), s.pop("oauth_nonce", None), s.pop("oauth_provider", None), s.pop("next", "/")
    cfg = provider_config(provider or "")
    if error or not code or not expected or not cfg or not secrets.compare_digest(state, expected):
        return RedirectResponse("/signin?error=failed")
    try:
        r = httpx.post(cfg["token"], timeout=20, data={
            "client_id": cfg["client_id"], "client_secret": cfg["secret"], "grant_type": "authorization_code",
            "code": code, "redirect_uri": f"{S.PUBLIC_URL}/api/v1/auth/callback"})
        claims = decode_id_token(r.json()["id_token"]) if r.is_success else None
    except Exception:  # noqa: BLE001 - provider down or a malformed answer: just a failed sign-in
        claims = None
    if not claims:
        return RedirectResponse("/signin?error=failed")
    try:
        email = check_claims(provider, claims, nonce)
    except Denied as d:
        with tx() as conn:
            audit_sign_in(conn, "sign in failed", claims.get("email") or claims.get("preferred_username") or "", f"{provider}: {d}")
        return RedirectResponse("/signin?error=denied")
    if not sign_in(request, email, provider):
        return RedirectResponse("/signin?error=denied")
    return RedirectResponse(safe_next(next_url))


class DevLogin(BaseModel):
    email: str


@router.post("/auth/dev-login")
def dev_login(request: Request, body: DevLogin):
    if not S.DEV_LOGIN:
        raise HTTPException(404)
    if not sign_in(request, body.email):
        raise HTTPException(403, "Only people an admin added can sign in")
    return {"ok": True}


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(ctx=CTX):
    roles = ctx.conn.execute("""SELECT ur.role_code AS role, f.code AS family FROM user_roles ur
                                LEFT JOIN families f ON f.id = ur.family_id WHERE ur.user_id = %s ORDER BY 1, 2""",
                             (ctx.uid,)).fetchall()
    return {"user": ctx.user, "roles": roles}
