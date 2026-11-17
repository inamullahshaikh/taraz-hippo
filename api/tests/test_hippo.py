"""Hippo: each level enforced by Postgres, preview then confirm, the change log, and the People page."""
import psycopg

from conftest import TEST_URL
from test_connector import sign_in_connector


def one(sql):
    with psycopg.connect(TEST_URL) as c:
        return c.execute(sql).fetchone()


def rpc(client, token, method, params=None):
    r = client.post("/hippo/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                    headers={"Authorization": f"Bearer {token}"})
    return r.json()["result"]


def tool(client, token, name, **args):
    return rpc(client, token, "tools/call", {"name": name, "arguments": args})


def token(client, email):
    return sign_in_connector(client, email)[0]["access_token"]


def test_discovery(client):
    r = client.post("/hippo/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "oauth-protected-resource/hippo/mcp" in r.headers["www-authenticate"]
    assert client.get("/.well-known/oauth-protected-resource/hippo/mcp").json()["resource"].endswith("/hippo/mcp")
    # sign-in codes only ever go back to Claude, never to another site
    assert client.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]}).status_code == 400
    assert client.post("/oauth/register", json={"redirect_uris": ["http://localhost:3118/callback"]}).status_code == 201


def test_app_login_only_records_sign_ins():
    """A BOMTree admin's SQL runs as the app's login: it must not be able to rewrite who someone is."""
    with psycopg.connect(TEST_URL) as c:
        c.execute("SET ROLE bomtree_app")
        c.execute("UPDATE hippo.users SET last_login_at = now() WHERE email = 'guest@partner.org'")
        for bad in ("UPDATE hippo.users SET email = 'me@evil.example' WHERE email = 'admin@taraz.tech'",
                    "INSERT INTO hippo.users (email, name) VALUES ('x@evil.example', 'X')",
                    "UPDATE hippo.access SET level = 'admin'"):
            c.execute("SAVEPOINT s")
            try:
                c.execute(bad)
                raise AssertionError(f"allowed: {bad}")
            except psycopg.errors.InsufficientPrivilege:
                c.execute("ROLLBACK TO SAVEPOINT s")
        c.rollback()


def test_levels_preview_confirm_and_log(client):
    admin, writer = token(client, "admin@taraz.tech"), token(client, "writer@taraz.tech")
    reader, guest = token(client, "reader@taraz.tech"), token(client, "guest@partner.org")
    assert rpc(client, admin, "initialize", {"protocolVersion": "2025-06-18"})["serverInfo"]["name"] == "Hippo"
    assert {t["name"] for t in rpc(client, admin, "tools/list")["tools"]} == {"query", "run_sql"}

    # no Hippo level: nothing; read: reads only, one statement at a time
    assert tool(client, guest, "query", sql="SELECT 1")["isError"]
    assert tool(client, reader, "query", sql="SELECT count(*) FROM bomtree.items")["structuredContent"]["rows"][0][0] >= 3
    assert "multiple commands" in tool(client, reader, "query", sql="SELECT 1; SELECT 2")["content"][0]["text"]
    assert tool(client, reader, "query", sql="DELETE FROM bomtree.parts")["isError"]
    assert tool(client, reader, "run_sql", sql="SELECT 1", reason="r")["isError"]

    # admin changes structure: a new schema, a table with a rule, a function (with ; inside), an index on BOMTree
    def admin_sql(sql, **kw):
        r = tool(client, admin, "run_sql", sql=sql, reason="costs come to Hippo", confirm=True, **kw)
        assert not r.get("isError"), r
        return r["structuredContent"]
    admin_sql("CREATE SCHEMA costs", books_note="Books/Hippo Database.md")
    admin_sql("CREATE TABLE costs.part_cost (code text PRIMARY KEY, usd numeric NOT NULL CHECK (usd >= 0))")
    admin_sql("CREATE FUNCTION costs.total() RETURNS numeric LANGUAGE plpgsql AS $$ BEGIN RETURN (SELECT sum(usd) FROM costs.part_cost); END $$")
    admin_sql("CREATE INDEX parts_mpn_lower_idx ON bomtree.parts (lower(mpn))")
    assert one("SELECT who, books_note FROM hippo.change_log WHERE command = 'CREATE SCHEMA' AND books_note IS NOT NULL") == (
        "admin@taraz.tech", "Books/Hippo Database.md")
    # the event trigger logs the same change too, naming the object and the real database login
    assert one("SELECT who, db_login FROM hippo.change_log WHERE objects = 'bomtree.parts_mpn_lower_idx'") == (
        "admin@taraz.tech", "hippo_admin")

    # write changes rows (in tables an admin made later too): preview first, then confirm
    ins = "INSERT INTO costs.part_cost VALUES ('P000001', 0.25)"
    p = tool(client, writer, "run_sql", sql=ins, reason="DigiKey price")["structuredContent"]
    assert p["saved"] is False and p["status"] == "INSERT 0 1" and one("SELECT count(*) FROM costs.part_cost") == (0,)
    s = tool(client, writer, "run_sql", sql=ins, reason="DigiKey price", confirm=True)["structuredContent"]
    assert s["saved"] is True and one("SELECT usd FROM costs.part_cost")[0] == 0.25
    assert one("SELECT who, command, reason FROM hippo.change_log ORDER BY id DESC LIMIT 1") == (
        "writer@taraz.tech", "INSERT 0 1", "DigiKey price")
    assert tool(client, reader, "query", sql="SELECT costs.total()")["structuredContent"]["rows"] == [["0.25"]]
    # BOMTree's own audit log names the person
    tool(client, writer, "run_sql", sql="UPDATE bomtree.families SET notes = 'via Hippo' WHERE code = 'USM'", reason="r",
         confirm=True)
    assert one("SELECT u.email FROM bomtree.audit_log a JOIN hippo.users u ON u.id = a.user_id "
               "WHERE a.entity_type = 'families' ORDER BY a.id DESC LIMIT 1") == ("writer@taraz.tech",)

    # write may not change structure, people, rights or the log; nobody may end the transaction or skip the reason
    for bad in ("ALTER TABLE costs.part_cost ADD COLUMN note text", "UPDATE hippo.access SET level = 'admin'",
                "DELETE FROM bomtree.user_roles", "DELETE FROM hippo.change_log", "COMMIT",
                "INSERT INTO hippo.change_log (who, db_login, command, statement) VALUES ('x', 'postgres', 'x', 'x')"):
        assert tool(client, writer, "run_sql", sql=bad, reason="r", confirm=True)["isError"], bad
    assert tool(client, writer, "run_sql", sql="SELECT 1", reason=" ")["isError"]
    # admins can't touch the log either
    for bad in ("DELETE FROM hippo.change_log", "DROP TABLE hippo.change_log", "DROP EVENT TRIGGER hippo_log_ddl"):
        assert tool(client, admin, "run_sql", sql=bad, reason="r", confirm=True)["isError"], bad

    # structure changed outside the connector is logged too
    with psycopg.connect(TEST_URL, autocommit=True) as c:
        c.execute("CREATE TABLE costs.scratch (a int)")
    assert one("SELECT who, objects FROM hippo.change_log ORDER BY id DESC LIMIT 1") == ("postgres", "costs.scratch")


def sign_in(client, email):
    client.cookies.clear()
    assert client.post("/api/v1/auth/dev-login", json={"email": email}).status_code == 200


def test_people_page(client):
    client.cookies.clear()
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/signin?next=/admin"
    sign_in(client, "writer@taraz.tech")
    assert client.get("/api/v1/hippo/people").status_code == 403          # only Hippo admins
    sign_in(client, "admin@taraz.tech")
    assert "Hippo" in client.get("/admin").text

    def find(d, email):
        return next(p for p in d["people"] if p["email"] == email)

    new = {"email": "New.Person@Partner.org", "name": "New Person", "access": "read",
           "roles": [{"role": "designer", "family": "USM"}]}
    d = client.post("/api/v1/hippo/people", json=new).json()
    p = find(d, "new.person@partner.org")
    assert p["access"] == "read" and p["roles"] == [{"role": "designer", "family": "USM"}] and p["active"]
    assert client.post("/api/v1/hippo/people", json=new).status_code == 409                     # already there
    assert client.post("/api/v1/hippo/people", json={**p, "roles": [{"role": "boss"}]}).status_code == 422
    assert client.post("/api/v1/hippo/people", json={**p, "access": "owner"}).status_code == 422

    d = client.post("/api/v1/hippo/people", json={**p, "name": "Renamed", "active": False, "access": None,
                                                   "roles": [{"role": "viewer"}]}).json()
    p = find(d, "new.person@partner.org")
    assert (p["name"], p["active"], p["access"], p["roles"]) == ("Renamed", False, None, [{"role": "viewer", "family": None}])
    assert client.post("/api/v1/auth/dev-login", json={"email": "new.person@partner.org"}).status_code == 403  # switched off

    sign_in(client, "admin@taraz.tech")
    me = find(client.get("/api/v1/hippo/people").json(), "admin@taraz.tech")
    assert client.post("/api/v1/hippo/people", json={**me, "access": "write"}).status_code == 409   # the last admin stays
    assert client.post("/api/v1/hippo/people", json={**me, "active": False}).status_code == 409
    assert "new.person@partner.org" in one("SELECT statement FROM hippo.change_log WHERE command = 'people' "
                                           "ORDER BY id DESC LIMIT 1")[0]
