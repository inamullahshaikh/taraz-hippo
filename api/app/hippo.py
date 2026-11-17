"""Hippo: Taraz's shared database. BOMTree is one part of it (schema bomtree); people and the change log are in hippo.

- /hippo/mcp is the Hippo connector for claude.ai, with the same sign-in as BOMTree's. Two tools: query and run_sql.
  A person's level in hippo.access (read, write or admin) picks the database login their SQL runs as, so Postgres
  itself enforces it: read only reads, write changes rows, admin owns the tables and may change structure too.
- /admin is the People page: Hippo admins add people and set their Hippo level and BOMTree roles.
Every change made here goes to hippo.change_log; structure changes made any other way get there by an event trigger.
"""
from contextlib import contextmanager
from pathlib import Path

import psycopg
from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from psycopg.rows import dict_row
from pydantic import BaseModel

from . import connector
from .auth import CTX, clean_email
from .db import tx
from .settings import S

router = APIRouter()
LEVELS = ("read", "write", "admin")
PAGE = Path(__file__).parent / "static" / "admin.html"


def connect(level):
    """A fresh connection as the level's login for each call, so nothing one person's SQL sets reaches the next.
    lock_timeout: a structure change waiting on a busy table soon gives up instead of holding up everyone behind it.
    connect_timeout: an unreachable database fails the call, not the app's worker threads."""
    url = {"read": S.HIPPO_READ_URL, "write": S.HIPPO_WRITE_URL, "admin": S.HIPPO_ADMIN_URL}[level]
    if not url:
        raise HTTPException(503, "Hippo is not set up on this server yet (HIPPO_*_URL)")
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=5,
                           options="-c statement_timeout=60s -c lock_timeout=2s")


def log(conn, who, command, statement, reason=None, books_note=None):
    conn.execute("INSERT INTO hippo.change_log (who, command, statement, reason, books_note) VALUES (%s, %s, %s, %s, %s)",
                 (who, command, statement, reason, books_note or None))


def acting_as(conn, uid, email):
    """Changes are recorded under the person: hippo.who for the change log, bomtree.user_id for BOMTree's audit log."""
    conn.execute("SELECT set_config('hippo.who', %s, true), set_config('bomtree.user_id', %s, true)", (email, str(uid)))


# The connector ------------------------------------------------------------------------

INFO = {"name": "Hippo", "instructions": (
    "Hippo is Taraz's shared database. Each part has its own schema: bomtree (BOMs, items, parts, alternates ...), "
    "hippo (people, access levels, change_log), and any added later. Always write schema.table. Find tables with "
    "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', "
    "'information_schema'), and columns in information_schema.columns. Read with query. Change with run_sql: the first "
    "call is a preview that saves nothing; show the person what it did, then call again with confirm: true. Change "
    "bomtree tables with ALTER TABLE; never drop and re-create them, because the BOMTree app's rights and audit "
    "triggers are on the existing tables.")}

TOOLS = [
    {"name": "query", "description": "Runs one read-only SQL statement on Hippo and returns at most 500 rows. "
                                     "For everyone with Hippo access.",
     "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "run_sql", "description": "Changes Hippo with one SQL statement. People with write access change rows; admins "
                                       "may also change structure: schemas, tables, columns, constraints, indexes, views, "
                                       "functions, triggers and grants. Without confirm it is a preview: it runs, returns "
                                       "the result and undoes it. With confirm: true it is saved, and goes in "
                                       "hippo.change_log under your name with the reason and the Books note.",
     "inputSchema": {"type": "object", "properties": {
         "sql": {"type": "string"},
         "reason": {"type": "string", "description": "Why, in one sentence"},
         "books_note": {"type": "string", "description": "The Books note that records the decision, if there is one"},
         "confirm": {"type": "boolean", "description": "true saves the change; leave it out for a preview"}},
         "required": ["sql", "reason"]}},
]


@router.get("/.well-known/oauth-protected-resource/hippo/mcp")
def protected_resource():
    return {**connector.protected_resource(), "resource": f"{S.PUBLIC_URL}/hippo/mcp"}


@router.get("/hippo/mcp")
def mcp_get():
    return connector.mcp_get()


@router.post("/hippo/mcp")
def mcp(body: dict | list = Body(...), authorization: str = Header(None)):
    uid = connector.bearer_user(authorization, "/hippo/mcp")
    return connector.reply(body, INFO, TOOLS, lambda name, args: call(uid, name, args))


def need(me, level):
    if not me["level"] or LEVELS.index(me["level"]) < LEVELS.index(level):
        raise HTTPException(403, f"This needs {level} access to Hippo. Ask a Hippo admin (People page: /admin).")


def call(uid, name, args):
    with tx() as conn:
        me = conn.execute("""SELECT u.id, u.email, a.level FROM hippo.users u LEFT JOIN hippo.access a ON a.user_id = u.id
                             WHERE u.id = %s AND u.active AND NOT u.is_system""", (uid,)).fetchone()
    if not me:
        raise HTTPException(401, "Your access was switched off")
    if name == "query":
        need(me, "read")
        with connect("read") as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            return connector.sql_rows(conn, args.get("sql"))
    if name == "run_sql":
        need(me, "write")
        reason = (args.get("reason") or "").strip()
        if not reason:
            raise HTTPException(422, "Give a reason: it goes in the change log")
        with connect(me["level"]) as conn:
            with conn.transaction():
                acting_as(conn, uid, me["email"])
                out = connector.sql_rows(conn, args.get("sql"))
                if args.get("confirm") is not True:
                    out.update(saved=False, note="Preview: nothing was saved. Show the person, then call again with confirm: true.")
                    raise psycopg.Rollback()
                log(conn, me["email"], out["status"], args["sql"], reason, args.get("books_note"))
                out["saved"] = True
        return out
    raise HTTPException(404, f"Unknown tool {name}")


# The People page ----------------------------------------------------------------------

@router.get("/admin")
def admin_page(request: Request):
    if not request.session.get("uid"):
        return RedirectResponse("/signin?next=/admin")
    return FileResponse(PAGE, headers={"Cache-Control": "no-store"})


@contextmanager
def as_admin(ctx):
    """The page's changes run as hippo_admin: the BOMTree app's own login may only read Hippo levels.
    The check uses the app's login, so nobody else ever makes the page open an admin connection."""
    if not ctx.conn.execute("SELECT 1 FROM hippo.access WHERE user_id = %s AND level = 'admin'", (ctx.uid,)).fetchone():
        raise HTTPException(403, {"code": "not_admin", "message": "Only Hippo admins can manage people."})
    with connect("admin") as conn, conn.transaction():
        acting_as(conn, ctx.uid, ctx.user["email"])
        yield conn


PEOPLE = """
SELECT u.id, u.email, u.name, u.active, u.last_login_at, a.level AS access,
       coalesce(json_agg(json_build_object('role', ur.role_code, 'family', f.code) ORDER BY ur.role_code, f.code)
                FILTER (WHERE ur.id IS NOT NULL), '[]') AS roles
FROM hippo.users u
LEFT JOIN hippo.access a        ON a.user_id = u.id
LEFT JOIN bomtree.user_roles ur ON ur.user_id = u.id
LEFT JOIN bomtree.families f    ON f.id = ur.family_id
WHERE NOT u.is_system
GROUP BY u.id, a.level
ORDER BY u.active DESC, lower(u.name)"""


def listing(conn, ctx):
    return {"me": ctx.user["email"], "people": conn.execute(PEOPLE).fetchall(),
            "roles": conn.execute("SELECT code, name, description FROM bomtree.roles ORDER BY name").fetchall(),
            "families": [r["code"] for r in conn.execute("SELECT code FROM bomtree.families WHERE archived_at IS NULL ORDER BY code")]}


@router.get("/api/v1/hippo/people")
def people(ctx=CTX):
    with as_admin(ctx) as conn:
        return listing(conn, ctx)


class Role(BaseModel):
    role: str
    family: str | None = None          # None = all families


class Person(BaseModel):
    id: int | None = None              # None = add
    email: str
    name: str
    active: bool = True
    access: str | None = None          # read, write, admin; None = no Hippo access
    roles: list[Role] = []             # BOMTree roles


@router.post("/api/v1/hippo/people")
def save(p: Person, ctx=CTX):
    email, name = clean_email(p.email), p.name.strip()
    if not email or not name:
        raise HTTPException(422, "A name and a valid email address are needed")
    if p.access not in (None, *LEVELS):
        raise HTTPException(422, f"Unknown Hippo level {p.access}")
    with as_admin(ctx) as conn:
        if p.id is None:
            row = conn.execute("INSERT INTO hippo.users (email, name, active) VALUES (%s, %s, %s) "
                               "ON CONFLICT (email) DO NOTHING RETURNING id", (email, name, p.active)).fetchone()
            if not row:
                raise HTTPException(409, f"{email} is already in the list")
        else:
            row = conn.execute("UPDATE hippo.users SET email = %s, name = %s, active = %s WHERE id = %s AND NOT is_system "
                               "RETURNING id", (email, name, p.active, p.id)).fetchone()
            if not row:
                raise HTTPException(404, "No such person")
        uid = row["id"]
        if p.access:
            conn.execute("""INSERT INTO hippo.access (user_id, level, granted_by) VALUES (%s, %s, %s)
                            ON CONFLICT (user_id) DO UPDATE SET level = excluded.level, granted_by = excluded.granted_by,
                                                                granted_at = now()
                            WHERE hippo.access.level <> excluded.level""", (uid, p.access, ctx.uid))
        else:
            conn.execute("DELETE FROM hippo.access WHERE user_id = %s", (uid,))
        set_roles(conn, uid, p.roles, ctx.uid)
        if not conn.execute("SELECT 1 FROM hippo.access a JOIN hippo.users u ON u.id = a.user_id "
                            "WHERE a.level = 'admin' AND u.active").fetchone():
            raise HTTPException(409, "Hippo must keep at least one active admin")
        roles = ", ".join(f"{r.role} ({r.family or 'all families'})" for r in p.roles) or "none"
        log(conn, ctx.user["email"], "people", f"{email} ({name}): {'on' if p.active else 'switched off'}; "
                                               f"Hippo: {p.access or 'no access'}; BOMTree roles: {roles}")
        return listing(conn, ctx)


def set_roles(conn, uid, roles, by):
    """Makes the person's BOMTree roles exactly these, touching only the ones that change (each one is audited)."""
    families = {r["code"]: r["id"] for r in conn.execute("SELECT code, id FROM bomtree.families")}
    known = {r["code"] for r in conn.execute("SELECT code FROM bomtree.roles")}
    want = set()
    for r in roles:
        if r.role not in known or (r.family and r.family not in families):
            raise HTTPException(422, f"Unknown BOMTree role or family: {r.role} {r.family or ''}".strip())
        want.add((r.role, families.get(r.family)))
    have = {(r["role_code"], r["family_id"])
            for r in conn.execute("SELECT role_code, family_id FROM bomtree.user_roles WHERE user_id = %s", (uid,))}
    for role, family in have - want:
        conn.execute("DELETE FROM bomtree.user_roles WHERE user_id = %s AND role_code = %s "
                     "AND family_id IS NOT DISTINCT FROM %s", (uid, role, family))
    for role, family in want - have:
        conn.execute("INSERT INTO bomtree.user_roles (user_id, role_code, family_id, granted_by) VALUES (%s, %s, %s, %s)",
                     (uid, role, family, by))
