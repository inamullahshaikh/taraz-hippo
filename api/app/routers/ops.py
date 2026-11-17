"""Odoo push and sync, audit, admin (users, roles, settings, health, full export)."""
import io
import json
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..auth import CTX, allowed_email
from ..db import Jsonb, setting
from ..integrations import drive
from ..rbac.roles import ROLES
from ..services import boms
from ..settings import S

router = APIRouter()


# Odoo --------------------------------------------------------------------------

@router.post("/versions/{vid}/odoo-push")
def odoo_push(vid: int, ctx=CTX):
    v = boms.version(ctx, vid, "odoo.push")
    if not setting(ctx.conn, "odoo_enabled"):
        raise HTTPException(409, "Odoo is switched off on the Admin page")
    if v["state"] != "Released":
        raise HTTPException(409, "Only a Released version can be pushed to Odoo")
    target = ctx.one("SELECT odoo_target FROM families WHERE id = %s", v["family_id"])["odoo_target"]
    if ctx.one("SELECT 1 FROM odoo_jobs WHERE bom_version_id = %s AND status IN ('Queued', 'Running')", vid):
        raise HTTPException(409, "A push of this version is already queued")
    return ctx.one("""INSERT INTO odoo_jobs (bom_version_id, kind, target, requested_by) VALUES (%s, 'push_bom', %s, %s) RETURNING *""",
                   vid, target, ctx.uid)


@router.get("/odoo/jobs")
def odoo_jobs(status: str = "", limit: int = 100, ctx=CTX):
    rows = ctx.all("""SELECT j.*, v.version_no, b.option_code, i.code AS item_code, i.family_id, u.name AS requested_by_name
                      FROM odoo_jobs j LEFT JOIN bom_versions v ON v.id = j.bom_version_id LEFT JOIN boms b ON b.id = v.bom_id
                      LEFT JOIN items i ON i.id = b.item_id LEFT JOIN users u ON u.id = j.requested_by
                      WHERE %s = '' OR j.status = %s ORDER BY j.id DESC LIMIT %s""", status, status, min(limit, 500))
    visible = [r for r in rows if ctx.can("odoo.push", r["family_id"])]
    if not visible and not any(ctx.can("odoo.push", f) for _, f in ctx.roles):
        ctx.require("odoo.push")
    return visible


@router.post("/odoo/jobs/{jid}/retry")
def odoo_retry(jid: int, ctx=CTX):
    j = ctx.one("""SELECT j.*, i.family_id FROM odoo_jobs j LEFT JOIN bom_versions v ON v.id = j.bom_version_id
                   LEFT JOIN boms b ON b.id = v.bom_id LEFT JOIN items i ON i.id = b.item_id WHERE j.id = %s""", jid)
    if not j:
        raise HTTPException(404)
    ctx.require("odoo.push" if j["kind"] == "push_bom" else "admin.manage", j["family_id"])
    if j["status"] != "Failed":
        raise HTTPException(409, "Only a failed job can be retried")
    return ctx.one("""INSERT INTO odoo_jobs (bom_version_id, kind, target, requested_by, request)
                      VALUES (%s, %s, %s, %s, %s) RETURNING *""", j["bom_version_id"], j["kind"], j["target"], ctx.uid,
                   Jsonb({"retry_of": jid}))


class SyncIn(BaseModel):
    kind: Literal["sync_parts", "sync_costs", "sync_boms"]


@router.post("/odoo/sync")
def odoo_sync(body: SyncIn, ctx=CTX):
    ctx.require("admin.manage")
    return ctx.one("INSERT INTO odoo_jobs (kind, target, requested_by) VALUES (%s, %s, %s) RETURNING *",
                   body.kind, S.ODOO_READ_TARGET, ctx.uid)


# Audit -------------------------------------------------------------------------

@router.get("/audit")
def audit(user: str = "", entity: str = "", action: str = "", source: str = "", date_from: str = "", date_to: str = "",
          limit: int = 200, ctx=CTX):
    everywhere = ctx.can("audit.read")
    fams = [f for _, f in ctx.roles if f and ctx.can("audit.read", f)]
    if not everywhere and not fams:
        ctx.require("audit.read")
    item = ctx.one("SELECT id FROM items WHERE upper(code) = upper(%s)", entity) if entity else None
    return ctx.all("""SELECT a.*, u.name AS user_name, u.email AS user_email, audit_family(a) AS family_id, i.code AS item_code
                      FROM audit_log a LEFT JOIN users u ON u.id = a.user_id LEFT JOIN items i ON i.id = audit_item(a)
                      WHERE (%(all)s OR audit_family(a) = ANY(%(fams)s))
                        AND (%(user)s = '' OR u.email ILIKE '%%' || %(user)s || '%%' OR u.name ILIKE '%%' || %(user)s || '%%')
                        AND (%(entity)s = '' OR (%(item)s::bigint IS NOT NULL AND audit_item(a) = %(item)s) OR a.entity_type = %(entity)s)
                        AND (%(action)s = '' OR a.action ILIKE %(action)s || '%%')
                        AND (%(source)s = '' OR a.source = %(source)s)
                        AND (%(df)s = '' OR a.at >= %(df)s::date) AND (%(dt)s = '' OR a.at < %(dt)s::date + 1)
                      ORDER BY a.id DESC LIMIT %(n)s""",
                   {"all": everywhere, "fams": fams, "user": user, "entity": entity, "item": item and item["id"],
                    "action": action, "source": source, "df": date_from, "dt": date_to, "n": min(limit, 1000)})


# Admin: users and roles ----------------------------------------------------------

@router.get("/admin/users")
def users(ctx=CTX):
    ctx.require("admin.manage")
    return ctx.all("""SELECT u.id, u.email, u.name, u.active, u.last_login_at, u.created_at,
                             coalesce(json_agg(json_build_object('role', r.code, 'family_id', ur.family_id) ORDER BY r.id)
                                      FILTER (WHERE r.id IS NOT NULL), '[]') AS roles
                      FROM users u LEFT JOIN user_roles ur ON ur.user_id = u.id LEFT JOIN roles r ON r.id = ur.role_id
                      WHERE u.email <> 'system@bomtree' GROUP BY u.id ORDER BY u.active DESC, u.name""")


class UserIn(BaseModel):
    email: str | None = None
    name: str | None = None
    active: bool | None = None


@router.post("/admin/users")
def create_user(body: UserIn, ctx=CTX):
    ctx.require("admin.manage")
    email = allowed_email(body.email)
    if not email:
        raise HTTPException(422, f"Only company accounts: @{', @'.join(S.ALLOWED_DOMAINS)}")
    u = ctx.one("INSERT INTO users (email, name) VALUES (%s, %s) RETURNING *", email, body.name or email)
    ctx.conn.execute("INSERT INTO user_roles (user_id, role_id, granted_by) SELECT %s, id, %s FROM roles WHERE code = 'viewer'", (u["id"], ctx.uid))
    return u


def _admins_left(ctx):
    return ctx.one("""SELECT count(DISTINCT u.id) AS n FROM users u JOIN user_roles ur ON ur.user_id = u.id JOIN roles r ON r.id = ur.role_id
                      WHERE r.code = 'admin' AND ur.family_id IS NULL AND u.active""")["n"]


@router.patch("/admin/users/{uid}")
def update_user(uid: int, body: UserIn, ctx=CTX):
    ctx.require("admin.manage")
    data = body.model_dump(exclude_unset=True, exclude={"email"})
    u = ctx.one("UPDATE users SET name = coalesce(%s, name), active = coalesce(%s, active) WHERE id = %s RETURNING *",
                data.get("name"), data.get("active"), uid)
    if not u:
        raise HTTPException(404)
    if _admins_left(ctx) == 0:
        raise HTTPException(409, "That would leave no active Admin")
    return u


class RoleIn(BaseModel):
    role: str
    family_id: int | None = None


@router.put("/admin/users/{uid}/roles")
def set_roles(uid: int, body: list[RoleIn], ctx=CTX):
    ctx.require("admin.manage")
    if any(r.role not in ROLES for r in body):
        raise HTTPException(422, f"Roles are {', '.join(ROLES)}")
    want = {(r.role, r.family_id) for r in body}
    have = {(r["code"], r["family_id"]): r["id"] for r in ctx.all(
        "SELECT ur.id, r.code, ur.family_id FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = %s", uid)}
    for k in have.keys() - want:
        ctx.conn.execute("DELETE FROM user_roles WHERE id = %s", (have[k],))
    for role, fam in want - have.keys():
        ctx.conn.execute("INSERT INTO user_roles (user_id, role_id, family_id, granted_by) SELECT %s, id, %s, %s FROM roles WHERE code = %s",
                         (uid, fam, ctx.uid, role))
    if _admins_left(ctx) == 0:
        raise HTTPException(409, "That would leave no active Admin")
    return {"roles": [{"role": r, "family_id": f} for r, f in sorted(want, key=str)]}


# Admin: settings, health, full export ------------------------------------------

@router.get("/admin/settings")
def get_settings(ctx=CTX):
    ctx.require("admin.manage")
    return {r["key"]: r["value"] for r in ctx.all("SELECT key, value FROM settings ORDER BY key")}


@router.put("/admin/settings")
def put_settings(body: dict[str, bool], ctx=CTX):
    ctx.require("admin.manage")
    for k, val in body.items():
        if not ctx.conn.execute("UPDATE settings SET value = %s WHERE key = %s", (Jsonb(val), k)).rowcount:
            raise HTTPException(422, f"Unknown setting {k}")
    return get_settings(ctx)


def _check(fn):
    try:
        return {"ok": True, "detail": fn() or ""}
    except Exception as e:  # noqa: BLE001 - health shows the failure instead of raising
        return {"ok": False, "detail": str(e)[:300]}


def _status_file(name):
    p = Path(S.BACKUP_DIR, "status", name)
    return json.loads(p.read_text()) if p.exists() else None


@router.get("/admin/health")
def health(ctx=CTX):
    ctx.require("admin.manage")
    beat = Path(S.DATA_DIR, "worker.heartbeat")
    odoo = {t: _check(lambda t=t: httpx.post(f"{S.ODOO[t]['url']}/web/webclient/version_info", json={"jsonrpc": "2.0", "params": {}},
                                             timeout=5).json()["result"]["server_version"]) if S.ODOO[t]["url"]
            else {"ok": False, "detail": "not set up"} for t in S.ODOO}
    backup = _status_file("backup.json")
    spend = ctx.one("""SELECT coalesce(sum((after->>'cost_usd')::numeric), 0) AS usd, count(*) AS messages FROM audit_log
                       WHERE action = 'blue chat' AND at >= date_trunc('month', now())""")
    return {
        "database": _check(lambda: ctx.one("SELECT version() AS v")["v"].split(",")[0]),
        "worker": {"ok": beat.exists() and time.time() - beat.stat().st_mtime < 120,
                   "detail": beat.exists() and datetime.fromtimestamp(beat.stat().st_mtime, timezone.utc).isoformat()},
        "odoo": odoo,
        "drive": _check(drive.ping) if S.GOOGLE_SA_FILE else {"ok": False, "detail": "not set up"},
        "claude": {"ok": bool(S.ANTHROPIC_API_KEY), "detail": S.CLAUDE_MODEL if S.ANTHROPIC_API_KEY else "not set up"},
        "claude_spend_this_month": spend,
        "last_backup": backup, "last_restore_test": _status_file("restore_test.json"),
        "failed_jobs_24h": ctx.one("SELECT count(*) AS n FROM odoo_jobs WHERE status = 'Failed' AND finished_at > now() - interval '1 day'")["n"],
        "settings": get_settings(ctx),
        "environment": S.ENVIRONMENT,
    }


EXPORT_TABLES = ["families", "items", "boms", "bom_versions", "bom_lines", "parts", "conversions", "legacy_lines", "users", "roles",
                 "user_roles", "change_requests", "approvals", "comments", "attachments", "imports", "odoo_jobs", "audit_log", "settings"]


@router.get("/admin/export")
def full_export(ctx=CTX):
    ctx.require("admin.manage")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in EXPORT_TABLES:
            out = io.StringIO()
            with ctx.conn.cursor().copy(f"COPY {t} TO STDOUT WITH (FORMAT csv, HEADER)") as cp:
                for chunk in cp:
                    out.write(bytes(chunk).decode())
            z.writestr(f"{t}.csv", out.getvalue())
    ctx.conn.execute("INSERT INTO audit_log (user_id, source, action, entity_type) VALUES (%s, 'Admin', 'full export', 'database')", (ctx.uid,))
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename=bomtree-{datetime.now():%Y-%m-%d}.zip"})
