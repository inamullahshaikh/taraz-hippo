"""BOMTree as a claude.ai connector (MCP over HTTP at /mcp), with its own OAuth so each person signs in as themselves.

claude.ai registers itself (/oauth/register), sends the person to /oauth/authorize (BOMTree's Google sign-in, then a
consent screen), and swaps the code for tokens at /oauth/token (PKCE S256). Clients, codes and tokens are signed with
SECRET_KEY, so nothing is stored; a token stops working when its user is switched off.

Tools: the three Sheets calls the BOMTree page makes (same names and arguments, answered from Postgres), list_tabs to
find your way, query (read-only SQL) and run_sql (admins).
"""
import base64
import hashlib
import html
import json
import secrets
import time
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Body, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from psycopg import pq
from psycopg.rows import tuple_row

from .auth import Ctx
from .db import tx
from .routers import sheets
from .settings import S

router = APIRouter()
SIGN = URLSafeTimedSerializer(S.SECRET_KEY)
ACCESS_SECONDS, REFRESH_SECONDS, CODE_SECONDS = 3600, 30 * 86400, 300
VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]


def signed(kind, data, max_age=None):
    try:
        return SIGN.loads(data, salt=kind, max_age=max_age)
    except (BadSignature, TypeError):
        return None


def oauth_error(error, description, status=400):
    return JSONResponse({"error": error, "error_description": description}, status)


# Discovery ----------------------------------------------------------------------

@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource():
    return {"resource": f"{S.PUBLIC_URL}/mcp", "authorization_servers": [S.PUBLIC_URL],
            "bearer_methods_supported": ["header"], "scopes_supported": ["bomtree"]}


@router.get("/.well-known/oauth-authorization-server")
def authorization_server():
    return {"issuer": S.PUBLIC_URL, "authorization_endpoint": f"{S.PUBLIC_URL}/oauth/authorize",
            "token_endpoint": f"{S.PUBLIC_URL}/oauth/token", "registration_endpoint": f"{S.PUBLIC_URL}/oauth/register",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "scopes_supported": ["bomtree"],
            "token_endpoint_auth_methods_supported": ["none"]}


# Registration, consent, tokens ---------------------------------------------------------

# Codes only ever go back to Claude: claude.ai's connector callback, or a Claude app on the person's own computer.
# Any other https address could be a stranger's site collecting a code from a tricked "Allow".
CLAUDE_CALLBACKS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")


def good_redirect(uri):
    p = urlparse(uri or "")
    return uri in CLAUDE_CALLBACKS or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1"))


@router.post("/oauth/register", status_code=201)
def register(body: dict = Body(...)):
    uris = body.get("redirect_uris") or []
    if not uris or not all(isinstance(u, str) and good_redirect(u) for u in uris):
        return oauth_error("invalid_redirect_uri", "redirect_uris must be Claude's connector callback")
    name = str(body.get("client_name") or urlparse(uris[0]).hostname)[:80]
    client_id = SIGN.dumps({"r": uris, "n": name}, salt="client")
    return {"client_id": client_id, "client_id_issued_at": int(time.time()), "client_name": name, "redirect_uris": uris,
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"]}


def check_request(client_id, redirect_uri, code_challenge, method):
    client = signed("client", client_id)
    if not client:
        raise HTTPException(400, "Unknown client: add the connector again in claude.ai")
    if redirect_uri not in client["r"] or not good_redirect(redirect_uri):   # also for clients registered earlier
        raise HTTPException(400, "This return address is not registered for the client")
    if not code_challenge or method != "S256":
        raise HTTPException(400, "PKCE (S256) is required")
    return client


@router.get("/oauth/authorize")
def authorize(request: Request, client_id: str = "", redirect_uri: str = "", state: str = "", code_challenge: str = "",
              code_challenge_method: str = "", response_type: str = "code"):
    client = check_request(client_id, redirect_uri, code_challenge, code_challenge_method)
    if response_type != "code":
        raise HTTPException(400, "Only response_type=code is supported")
    uid = request.session.get("uid")
    if not uid:
        return RedirectResponse("/signin?" + urlencode({"next": str(request.url.path) + "?" + request.url.query}))
    with tx() as conn:
        user = conn.execute("SELECT email FROM users WHERE id = %s AND active AND NOT is_system", (uid,)).fetchone()
    if not user:
        request.session.clear()
        return RedirectResponse("/signin?error=denied")
    request.session["oauth_csrf"] = csrf = secrets.token_urlsafe(24)
    fields = {"client_id": client_id, "redirect_uri": redirect_uri, "state": state, "code_challenge": code_challenge,
              "csrf": csrf}
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in fields.items())
    who, app = html.escape(user["email"]), html.escape(client["n"])
    host = html.escape(urlparse(redirect_uri).hostname or "")
    return HTMLResponse(CONSENT.format(app=app, host=host, who=who, hidden=hidden))


@router.post("/oauth/authorize")
def consent(request: Request, client_id: str = Form(...), redirect_uri: str = Form(...), state: str = Form(""),
            code_challenge: str = Form(...), csrf: str = Form(...), decision: str = Form(...)):
    check_request(client_id, redirect_uri, code_challenge, "S256")
    uid = request.session.get("uid")
    if not uid or not secrets.compare_digest(csrf, request.session.pop("oauth_csrf", "")):
        raise HTTPException(400, "The sign-in expired. Start again from claude.ai.")
    sep = "&" if "?" in redirect_uri else "?"
    if decision != "allow":
        return RedirectResponse(redirect_uri + sep + urlencode({"error": "access_denied", "state": state}), 303)
    code = SIGN.dumps({"u": uid, "c": client_id, "r": redirect_uri, "ch": code_challenge}, salt="code")
    return RedirectResponse(redirect_uri + sep + urlencode({"code": code, "state": state}), 303)


def tokens(uid, client_id):
    return {"access_token": SIGN.dumps({"u": uid, "c": client_id}, salt="access"), "token_type": "Bearer",
            "expires_in": ACCESS_SECONDS, "scope": "bomtree",
            "refresh_token": SIGN.dumps({"u": uid, "c": client_id}, salt="refresh")}


@router.post("/oauth/token")
def token(grant_type: str = Form(...), code: str = Form(""), redirect_uri: str = Form(""), client_id: str = Form(""),
          code_verifier: str = Form(""), refresh_token: str = Form("")):
    if grant_type == "authorization_code":
        c = signed("code", code, CODE_SECONDS)
        if not c or c["c"] != client_id or c["r"] != redirect_uri:
            return oauth_error("invalid_grant", "The code is wrong or expired")
        digest = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        if not secrets.compare_digest(digest, c["ch"]):
            return oauth_error("invalid_grant", "PKCE check failed")
        return tokens(c["u"], client_id)
    if grant_type == "refresh_token":
        r = signed("refresh", refresh_token, REFRESH_SECONDS)
        if not r or (client_id and r["c"] != client_id):
            return oauth_error("invalid_grant", "The refresh token is wrong or expired")
        return tokens(r["u"], r["c"])
    return oauth_error("unsupported_grant_type", grant_type)


CONSENT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOMTree · Allow access</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;background:#F2F5F4;color:#13201E;font:15px/1.5 system-ui,sans-serif}}
main{{max-width:400px;background:#fff;border:1px solid #D6DFDB;border-radius:14px;padding:26px 24px;display:flex;flex-direction:column;gap:12px}}
h1{{margin:0;font-size:1.4rem;color:#0B6B66}}p{{margin:0}}.m{{color:#5A6B67;font-size:.9rem}}
form{{display:flex;gap:8px}}button{{flex:1;padding:10px;border-radius:10px;border:1px solid #D6DFDB;background:#fff;font:inherit;font-weight:600;cursor:pointer}}
button[value=allow]{{background:#0B6B66;color:#fff;border-color:#0B6B66}}button:focus-visible{{outline:2px solid #A85B0C;outline-offset:2px}}
</style></head><body><main><h1>BOMTree</h1>
<p><b>{app}</b> ({host}) wants to read and change BOMTree and Hippo as <b>{who}</b>.</p>
<p class="m">It gets the same rights you have there. Allow only if you started this from claude.ai.</p>
<form method="post">{hidden}<button name="decision" value="deny">Deny</button><button name="decision" value="allow">Allow</button></form>
</main></body></html>"""


# MCP ----------------------------------------------------------------------------------

RANGE_ARGS = {"spreadsheetId": {"type": "string", "description": "Sheet id: the Index, a family's sheet, or Blueprints (see list_tabs)"},
              "range": {"type": "string", "description": "A1 range with the tab name, e.g. 'Items'!A:L or 'Lines'!A1:F20"}}
WRITE_ARGS = {**RANGE_ARGS, "values": {"type": "array", "items": {"type": "array"}, "description": "Rows of cell values"},
              "base": {"type": "array", "items": {"type": "array"},
                       "description": "Optional: the tab's data rows the change was made on. Rows that changed since are not overwritten; they come back as conflicts."}}
TOOLS = [
    {"name": "list_tabs", "description": "Lists BOMTree's sheets and tabs: the Index sheet, each family's sheet id, and Blueprints. "
                                         "Use these ids with get_values, update_values and append_values.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_values", "description": "Reads a tab or range from the BOMTree database, shaped exactly like the Google Sheets "
                                          "tab it replaced (header row first). Returns {values}.",
     "inputSchema": {"type": "object", "properties": RANGE_ARGS, "required": ["spreadsheetId", "range"]}},
    {"name": "update_values", "description": "Writes values into a range of a tab, like Google Sheets, and saves the change in the "
                                             "BOMTree database: items by Item, BOMs by BOM, Lines by BOM (a changed BOM becomes a new "
                                             "released version; history is kept), other tabs whole. To change one cell, write just that cell.",
     "inputSchema": {"type": "object", "properties": WRITE_ARGS, "required": ["spreadsheetId", "range", "values"]}},
    {"name": "append_values", "description": "Adds rows at the end of a tab (new items, BOMs, lines, log entries ...).",
     "inputSchema": {"type": "object", "properties": WRITE_ARGS, "required": ["spreadsheetId", "range", "values"]}},
    {"name": "query", "description": "Runs one read-only SQL query on the BOMTree database (schema bomtree: items, boms, bom_versions, "
                                     "bom_lines, parts, families, audit_log ...; views v_current_lines, v_where_used). At most 500 rows. "
                                     "For people who may read drafts in every family.",
     "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "run_sql", "description": "Admins only: runs SQL that changes data, recorded in the history under you. Prefer the "
                                       "database's own functions (create_item, start_draft, save_draft_lines, release_direct, "
                                       "grant_role ...). People are added in the People page (/admin). To change table "
                                       "structure, use the Hippo connector.",
     "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
]


def bearer_user(authorization, resource=""):
    """The signed-in person's id. resource is the connector's path after /mcp's own ("" for BOMTree, "/hippo/mcp")."""
    tok = (authorization or "").removeprefix("Bearer ").strip()
    data = signed("access", tok, ACCESS_SECONDS) if tok else None
    if not data:
        raise HTTPException(401, "Sign in first", headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{S.PUBLIC_URL}/.well-known/oauth-protected-resource{resource}"'})
    return data["u"]


@router.get("/mcp")
def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})


INFO = {"name": "BOMTree", "instructions": "BOMTree's BOMs, items and parts, in the shape of the Google Sheets they came from. "
                                           "Start with list_tabs, then get_values."}


@router.post("/mcp")
def mcp(body: dict | list = Body(...), authorization: str = Header(None)):
    uid = bearer_user(authorization)
    return reply(body, INFO, TOOLS, lambda name, args: call(uid, name, args))


def reply(body, info, tools, run):
    """Answers one JSON-RPC message or a batch. run(name, args) runs a tool and returns its data."""
    msgs = body if isinstance(body, list) else [body]
    out = [r for r in (answer(m, info, tools, run) for m in msgs) if r is not None]
    if not out:
        return Response(status_code=202)
    return JSONResponse(out if isinstance(body, list) else out[0])


def answer(m, info, tools, run):
    mid, method, params = m.get("id"), m.get("method"), m.get("params") or {}
    if mid is None:                                    # a notification: nothing to answer
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in VERSIONS else VERSIONS[0], "capabilities": {"tools": {}},
                  "serverInfo": {"name": info["name"], "version": "1.0"}, "instructions": info["instructions"]}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        result = tool_result(run, params.get("name"), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def call(uid, name, args):
    with tx() as conn:
        user = conn.execute("SELECT id, email, name FROM users WHERE id = %s AND active AND NOT is_system", (uid,)).fetchone()
        if not user:
            raise HTTPException(401, "Your BOMTree access was switched off")
        conn.execute("SELECT begin_action(%s, 'App')", (uid,))
        if name not in TOOL_FN:
            raise HTTPException(404, f"Unknown tool {name}")
        return TOOL_FN[name](Ctx(conn, user), args)


def tool_result(run, name, args):
    try:
        text = json.dumps(run(name, args), default=str)       # numeric, dates ... as text, exactly as Postgres has them
        return {"content": [{"type": "text", "text": text}], "structuredContent": json.loads(text)}
    except HTTPException as e:
        d = e.detail
        msg = d.get("message") if isinstance(d, dict) else str(d)
        return {"content": [{"type": "text", "text": msg}], "isError": True}
    except Exception as e:  # noqa: BLE001 - database rules and SQL mistakes go back to the caller as text
        msg = getattr(getattr(e, "diag", None), "message_primary", None) or str(e)
        return {"content": [{"type": "text", "text": msg}], "isError": True}


def list_tabs(ctx, _):
    fams = ctx.conn.execute("SELECT code, legacy_sheet_id FROM families WHERE archived_at IS NULL AND legacy_sheet_id IS NOT NULL "
                            "ORDER BY code").fetchall()
    return {"index": {"spreadsheetId": S.INDEX_SHEET_ID, "tabs": list(sheets.INDEX)},
            "families": [{"family": f["code"], "spreadsheetId": f["legacy_sheet_id"], "tabs": list(sheets.FAMILY)} for f in fams],
            "blueprints": {"spreadsheetId": S.BLUEPRINTS_SHEET_ID, "tabs": list(sheets.BLUEPRINTS)}}


def get_values(ctx, a):
    ctx.require_read()
    return sheets.get_values(sheets.Get(spreadsheetId=a["spreadsheetId"], range=a["range"]), ctx)


def write(mode):
    def run(ctx, a):
        return sheets.write_values(sheets.Put(**{k: a[k] for k in ("spreadsheetId", "range", "values", "base") if k in a}), ctx, mode)
    return run


TX_WORDS = ("begin", "start", "commit", "end", "rollback", "abort", "savepoint", "release", "prepare", "set", "reset",
            "discard", "lock", "listen", "copy")


def one_statement(sql, read_only):
    """One statement, no transaction control: a second statement could end this transaction and run outside it."""
    s = (sql or "").strip().rstrip(";").strip()
    first = s.split(None, 1)[0].lower() if s else ""
    if not s or ";" in s or first in TX_WORDS:
        raise HTTPException(422, "Send one SQL statement at a time, without transaction commands")
    if read_only and first not in ("select", "with", "table", "values", "explain"):
        raise HTTPException(422, "query only reads: start with SELECT or WITH")
    return s


def sql_rows(conn, sql, limit=500):
    """Runs one statement. It goes as a prepared statement, so Postgres itself refuses a second one after a ';'."""
    if not (sql or "").strip():
        raise HTTPException(422, "Send one SQL statement")
    cur = conn.cursor(row_factory=tuple_row)
    cur.execute(sql, prepare=True)
    if conn.info.transaction_status != pq.TransactionStatus.INTRANS:
        raise HTTPException(422, "Transaction commands (COMMIT, ROLLBACK ...) are not allowed: each call is one transaction")
    if cur.description is None:
        return {"status": cur.statusmessage, "rowcount": cur.rowcount}
    rows = cur.fetchmany(limit + 1)
    return {"status": cur.statusmessage, "columns": [c.name for c in cur.description], "rows": rows[:limit],
            "truncated": len(rows) > limit}


def query(ctx, a):
    if not ctx.can("bom.read_draft"):
        raise HTTPException(403, "query is for people who may read drafts in every family")
    ctx.conn.execute("SET TRANSACTION READ ONLY")
    ctx.conn.execute("SET LOCAL statement_timeout = '15s'")
    return sql_rows(ctx.conn, one_statement(a["sql"], read_only=True))


def run_sql(ctx, a):
    if not ctx.can("admin.manage"):
        raise HTTPException(403, "run_sql is for BOMTree admins")
    ctx.conn.execute("SET LOCAL statement_timeout = '30s'")
    return sql_rows(ctx.conn, one_statement(a["sql"], read_only=False))


TOOL_FN = {"list_tabs": list_tabs, "get_values": get_values, "update_values": write("update"),
           "append_values": write("append"), "query": query, "run_sql": run_sql}
