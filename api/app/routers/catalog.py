"""Families, items and BOM options."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CTX
from ..db import update_row
from ..services import boms

router = APIRouter()


class FamilyIn(BaseModel):
    code: str | None = None
    name: str | None = None
    status: str | None = None
    old_tools: str | None = None
    notes: str | None = None
    drive_folder_id: str | None = None
    owner_id: int | None = None
    odoo_target: str | None = None


@router.get("/families")
def families(ctx=CTX):
    ctx.require("bom.read_released")
    return ctx.all("""SELECT f.*, u.name AS owner_name,
                             (SELECT count(*) FROM items i WHERE i.family_id = f.id AND i.archived_at IS NULL) AS item_count
                      FROM families f LEFT JOIN users u ON u.id = f.owner_id WHERE f.archived_at IS NULL ORDER BY f.code""")


@router.post("/families")
def create_family(body: FamilyIn, ctx=CTX):
    ctx.require("admin.manage")
    if not body.code:
        raise HTTPException(422, "A family needs a code")
    data = body.model_dump(exclude_none=True)
    data.setdefault("name", body.code)
    cols = ", ".join(data)
    return ctx.one(f"INSERT INTO families ({cols}) VALUES ({', '.join(['%s'] * len(data))}) RETURNING *", *data.values())


@router.patch("/families/{fid}")
def update_family(fid: int, body: FamilyIn, ctx=CTX):
    ctx.require("admin.manage")
    row = update_row(ctx.conn, "families", fid, body.model_dump(exclude_unset=True))
    if not row:
        raise HTTPException(404, "Family not found")
    return row


# Items -------------------------------------------------------------------------

class ItemIn(BaseModel):
    code: str | None = None
    prefix: str | None = None
    family_id: int | None = None
    level: str | None = None
    status: str | None = None
    built_by: str | None = None
    description: str | None = None
    odoo_today: str | None = None
    odoo_code: str | None = None
    notes: str | None = None
    brain_url: str | None = None


def item_row(ctx, code):
    item = ctx.one("""SELECT i.*, f.code AS family_code, f.drive_folder_id, f.odoo_target
                      FROM items i JOIN families f ON f.id = i.family_id WHERE upper(i.code) = upper(%s)""", code)
    if not item:
        raise HTTPException(404, f"Item {code} not found")
    return item


@router.get("/items")
def items(family: str = "", level: str = "", q: str = "", archived: bool = False, ctx=CTX):
    ctx.require("bom.read_released")
    rows = ctx.all("""SELECT i.id, i.code, i.prefix, i.level, i.status, i.description, i.archived_at, f.code AS family_code, f.id AS family_id,
                             (SELECT v.state FROM boms b JOIN bom_versions v ON v.id = b.current_version_id
                              WHERE b.item_id = i.id AND b.is_default AND b.archived_at IS NULL) AS default_state
                      FROM items i JOIN families f ON f.id = i.family_id
                      WHERE (%(f)s = '' OR upper(f.code) = upper(%(f)s)) AND (%(l)s = '' OR i.level = %(l)s)
                        AND (%(q)s = '' OR i.code ILIKE '%%' || %(q)s || '%%' OR i.description ILIKE '%%' || %(q)s || '%%')
                        AND ((i.archived_at IS NOT NULL) = %(a)s)
                      ORDER BY f.code, i.level, i.code""", {"f": family, "l": level, "q": q, "a": archived})
    for r in rows:   # people who can't see drafts don't learn that one is in progress either
        if r["default_state"] not in (None, "Released", "Superseded") and not ctx.can("bom.read_draft", r["family_id"]):
            r["default_state"] = None
    return rows


@router.get("/items/{code}")
def item(code: str, ctx=CTX):
    it = item_row(ctx, code)
    ctx.require("bom.read_released", it["family_id"])
    bl = ctx.all("SELECT * FROM boms WHERE item_id = %s ORDER BY is_default DESC, archived_at NULLS FIRST, option_code", it["id"])
    for b in bl:
        b["versions"] = [v for v in ctx.all("""SELECT id, version_no, state, migrated, released_at, change_note
                                               FROM bom_versions WHERE bom_id = %s ORDER BY version_no""", b["id"])
                         if ctx.can(ctx.read_code(v["state"]), it["family_id"])]
    it["boms"] = bl
    it["attachments"] = ctx.all("SELECT * FROM attachments WHERE entity_type = 'item' AND entity_id = %s AND archived_at IS NULL", it["id"])
    it["used_in"] = ctx.all("""SELECT DISTINCT pi.code FROM bom_lines l JOIN bom_versions v ON v.id = l.bom_version_id AND v.state = 'Released'
                               JOIN boms b ON b.id = v.bom_id JOIN items pi ON pi.id = b.item_id WHERE l.child_item_id = %s ORDER BY 1""", it["id"])
    return it


@router.post("/items")
def create_item(body: ItemIn, ctx=CTX):
    if not (body.code and body.family_id and body.level):
        raise HTTPException(422, "An item needs a code, a family and a level")
    ctx.require("item.create", body.family_id)
    data = body.model_dump(exclude_none=True)
    data["code"] = " ".join(data["code"].split())
    data.setdefault("prefix", data["code"].split("-")[0])
    data.setdefault("status", "Proposed")
    return ctx.one(f"INSERT INTO items ({', '.join(data)}) VALUES ({', '.join(['%s'] * len(data))}) RETURNING *", *data.values())


@router.patch("/items/{code}")
def update_item(code: str, body: ItemIn, ctx=CTX):
    it = item_row(ctx, code)
    ctx.require("item.create", it["family_id"])
    data = body.model_dump(exclude_unset=True)
    if "family_id" in data:
        ctx.require("item.create", data["family_id"])
    return update_row(ctx.conn, "items", it["id"], data)


@router.post("/items/{code}/archive")
def archive_item(code: str, ctx=CTX):
    it = item_row(ctx, code)
    ctx.require("item.archive", it["family_id"])
    ctx.conn.execute("UPDATE boms SET archived_at = now(), status = 'Archived' WHERE item_id = %s AND archived_at IS NULL", (it["id"],))
    ctx.conn.execute("UPDATE items SET archived_at = now() WHERE id = %s", (it["id"],))
    return {"ok": True}


# BOM options -------------------------------------------------------------------

class BomIn(BaseModel):
    option_code: str | None = None
    is_default: bool | None = None
    when_to_use: str | None = None
    notes: str | None = None


def bom_row(ctx, bom_id):
    b = ctx.one("SELECT b.*, i.family_id, i.code AS item_code FROM boms b JOIN items i ON i.id = b.item_id WHERE b.id = %s", bom_id)
    if not b:
        raise HTTPException(404, "BOM not found")
    return b


@router.post("/items/{code}/boms")
def create_bom(code: str, body: BomIn, ctx=CTX):
    it = item_row(ctx, code)
    ctx.require("bom.edit", it["family_id"])
    first = not ctx.one("SELECT 1 FROM boms WHERE item_id = %s AND archived_at IS NULL", it["id"])
    b = ctx.one("""INSERT INTO boms (item_id, option_code, is_default, when_to_use, notes) VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                it["id"], " ".join((body.option_code or "Standard").split()), first, body.when_to_use or "", body.notes or "")
    b["version"] = boms.new_draft(ctx, b["id"], note="New BOM")
    return b


@router.patch("/boms/{bom_id}")
def update_bom(bom_id: int, body: BomIn, ctx=CTX):
    b = bom_row(ctx, bom_id)
    ctx.require("bom.edit", b["family_id"])
    data = body.model_dump(exclude_unset=True)
    if data.pop("is_default", None):
        ctx.conn.execute("UPDATE boms SET is_default = false WHERE item_id = %s AND id <> %s AND is_default", (b["item_id"], bom_id))
        data["is_default"] = True
    return update_row(ctx.conn, "boms", bom_id, data) or b


@router.post("/boms/{bom_id}/archive")
def archive_bom(bom_id: int, ctx=CTX):
    b = bom_row(ctx, bom_id)
    ctx.require("item.archive", b["family_id"])
    ctx.conn.execute("UPDATE boms SET archived_at = now(), status = 'Archived', is_default = false WHERE id = %s", (bom_id,))
    nxt = ctx.one("SELECT id FROM boms WHERE item_id = %s AND archived_at IS NULL ORDER BY id LIMIT 1", b["item_id"])
    if b["is_default"] and nxt:
        ctx.conn.execute("UPDATE boms SET is_default = true WHERE id = %s", (nxt["id"],))
    return {"ok": True, "new_default_bom_id": nxt and b["is_default"] and nxt["id"]}


@router.get("/boms/{bom_id}/versions")
def versions(bom_id: int, ctx=CTX):
    b = bom_row(ctx, bom_id)
    ctx.require("bom.read_released", b["family_id"])
    rows = ctx.all("""SELECT v.id, v.version_no, v.state, v.migrated, v.created_at, v.submitted_at, v.approved_at, v.released_at,
                             v.change_note, v.odoo_bom_id, v.change_request_id, cr.number AS change_request,
                             mu.name AS created_by, au.name AS approved_by, ru.name AS released_by,
                             (SELECT count(*) FROM bom_lines l WHERE l.bom_version_id = v.id) AS line_count
                      FROM bom_versions v JOIN users mu ON mu.id = v.created_by
                      LEFT JOIN users au ON au.id = v.approved_by LEFT JOIN users ru ON ru.id = v.released_by
                      LEFT JOIN change_requests cr ON cr.id = v.change_request_id
                      WHERE v.bom_id = %s ORDER BY v.version_no DESC""", bom_id)
    return {"bom": b, "versions": [v for v in rows if ctx.can(ctx.read_code(v["state"]), b["family_id"])]}
