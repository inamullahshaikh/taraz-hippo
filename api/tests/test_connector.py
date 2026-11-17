"""BOMTree as a claude.ai connector: OAuth sign-in as yourself, then the page's Sheets calls and SQL over MCP."""
import base64
import hashlib
import re
from urllib.parse import parse_qs, urlparse

import psycopg

from app.settings import S
from conftest import TEST_URL

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def sign_in_connector(client, email):
    """The flow claude.ai runs: register, authorize (signed in, consent), swap the code for tokens."""
    client.cookies.clear()
    client_id = client.post("/oauth/register", json={"redirect_uris": [CALLBACK], "client_name": "Claude"}).json()["client_id"]
    verifier = "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"client_id": client_id, "redirect_uri": CALLBACK, "state": "s1", "code_challenge": challenge,
         "code_challenge_method": "S256", "response_type": "code"}
    r = client.get("/oauth/authorize", params=q, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("/signin?next=%2Foauth%2Fauthorize")
    assert client.post("/api/v1/auth/dev-login", json={"email": email}).status_code == 200
    page = client.get("/oauth/authorize", params=q)
    assert email in page.text and "Claude" in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = client.post("/oauth/authorize", data={**{k: q[k] for k in ("client_id", "redirect_uri", "state", "code_challenge")},
                                              "csrf": csrf, "decision": "allow"}, follow_redirects=False)
    back = parse_qs(urlparse(r.headers["location"]).query)
    assert back["state"] == ["s1"]
    form = {"grant_type": "authorization_code", "code": back["code"][0], "redirect_uri": CALLBACK, "client_id": client_id}
    assert client.post("/oauth/token", data={**form, "code_verifier": "wrong" * 10}).json()["error"] == "invalid_grant"
    tok = client.post("/oauth/token", data={**form, "code_verifier": verifier}).json()
    client.cookies.clear()                                   # from here on only the token counts
    return tok, client_id


def rpc(client, token, method, params=None, id=1):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}},
                    headers={"Authorization": f"Bearer {token}"})
    return r.json()["result"]


def tool(client, token, name, **args):
    return rpc(client, token, "tools/call", {"name": name, "arguments": args})


def test_discovery_and_no_token(client):
    assert client.get("/.well-known/oauth-protected-resource").json()["authorization_servers"] == [S.PUBLIC_URL]
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"] and meta["registration_endpoint"].endswith("/oauth/register")
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "resource_metadata=" in r.headers["www-authenticate"]
    assert client.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}).status_code == 400


def test_admin_connector_reads_writes_and_runs_sql(client):
    tok, client_id = sign_in_connector(client, "admin@taraz.tech")
    t = tok["access_token"]
    assert rpc(client, t, "initialize", {"protocolVersion": "2025-06-18"})["protocolVersion"] == "2025-06-18"
    assert {x["name"] for x in rpc(client, t, "tools/list")["tools"]} == {
        "list_tabs", "get_values", "update_values", "append_values", "query", "run_sql"}
    tabs = tool(client, t, "list_tabs")["structuredContent"]
    assert tabs["families"][0]["spreadsheetId"] == "sheet-usm"
    items = tool(client, t, "get_values", spreadsheetId=S.INDEX_SHEET_ID, range="'Items'!A1:C2")["structuredContent"]
    assert items["values"][0] == ["Item", "Prefix", "Family"]
    r = tool(client, t, "append_values", spreadsheetId=S.INDEX_SHEET_ID, range="'Log'!A1",
             values=[["", "Claude", "Note", "USM-3IV", "from the connector"]])
    assert not r.get("isError"), r
    q = tool(client, t, "query", sql="SELECT count(*) FROM bomtree.items")["structuredContent"]
    assert q["columns"] == ["count"] and q["rows"][0][0] >= 3
    assert tool(client, t, "query", sql="select 1; delete from bomtree.bom_lines")["isError"]
    assert tool(client, t, "query", sql="delete from bomtree.bom_lines")["isError"]
    assert tool(client, t, "query", sql="COMMIT")["isError"]
    r = tool(client, t, "run_sql", sql="SELECT bomtree.add_user('mcp@partner.org', 'Via connector', 'viewer')")
    assert not r.get("isError"), r
    refreshed = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                                   "client_id": client_id}).json()
    assert rpc(client, refreshed["access_token"], "ping") == {}


def test_viewer_connector_reads_only_and_switched_off_users_lose_it(client):
    tok, _ = sign_in_connector(client, "guest@partner.org")
    t = tok["access_token"]
    assert tool(client, t, "get_values", spreadsheetId=S.INDEX_SHEET_ID, range="'Items'!A1:A1")["structuredContent"]["values"] == [["Item"]]
    assert tool(client, t, "query", sql="SELECT 1")["isError"]
    assert tool(client, t, "run_sql", sql="SELECT 1")["isError"]
    assert tool(client, t, "update_values", spreadsheetId="sheet-usm", range="'BOMs'!F2", values=[["Archived"]])["isError"]
    with psycopg.connect(TEST_URL, autocommit=True) as c:
        c.execute("UPDATE hippo.users SET active = false WHERE email = 'guest@partner.org'")
    try:
        r = tool(client, t, "get_values", spreadsheetId=S.INDEX_SHEET_ID, range="'Items'!A1:A1")
        assert r["isError"] and "switched off" in r["content"][0]["text"]
    finally:
        with psycopg.connect(TEST_URL, autocommit=True) as c:
            c.execute("UPDATE hippo.users SET active = true WHERE email = 'guest@partner.org'")
