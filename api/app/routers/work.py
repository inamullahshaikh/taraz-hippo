"""Comments, change requests, imports, exports, and the Home page."""
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..auth import CTX
from ..services import boms, exports, imports, notify
from ..settings import S

router = APIRouter()

FAMILY_OF = {
    "item": "SELECT family_id FROM items WHERE id = %s",
    "bom": "SELECT i.family_id FROM boms b JOIN items i ON i.id = b.item_id WHERE b.id = %s",
    "version": "SELECT i.family_id FROM bom_versions v JOIN boms b ON b.id = v.bom_id JOIN items i ON i.id = b.item_id WHERE v.id = %s",
    "change_request": "SELECT i.family_id FROM change_requests c JOIN boms b ON b.id = c.bom_id JOIN items i ON i.id = b.item_id WHERE c.id = %s",
}


def family_of(ctx, entity_type, entity_id):
    if entity_type not in FAMILY_OF:
        raise HTTPException(422, f"Comments go on {', '.join(FAMILY_OF)}")
    row = ctx.one(FAMILY_OF[entity_type], entity_id)
    if not row:
        raise HTTPException(404, "Not found")
    return row["family_id"]


# Comments ----------------------------------------------------------------------

class CommentIn(BaseModel):
    entity_type: str
    entity_id: int
    line_id: int | None = None
    body: str


@router.get("/comments")
def comments(entity_type: str, entity_id: int, ctx=CTX):
    ctx.require("bom.read_released", family_of(ctx, entity_type, entity_id))
    return ctx.all("""SELECT c.*, u.name AS user_name FROM comments c JOIN users u ON u.id = c.user_id
                      WHERE c.entity_type = %s AND c.entity_id = %s ORDER BY c.created_at""", entity_type, entity_id)


def add_comment(ctx, body: CommentIn):
    ctx.require("comment.create", family_of(ctx, body.entity_type, body.entity_id))
    if not body.body.strip():
        raise HTTPException(422, "Write something")
    return ctx.one("INSERT INTO comments (entity_type, entity_id, line_id, user_id, body) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                   body.entity_type, body.entity_id, body.line_id, ctx.uid, body.body.strip())


@router.post("/comments")
def create_comment(body: CommentIn, ctx=CTX):
    return add_comment(ctx, body)


class CommentPatch(BaseModel):
    resolved: bool


@router.patch("/comments/{cid}")
def update_comment(cid: int, body: CommentPatch, ctx=CTX):
    c = ctx.one("SELECT * FROM comments WHERE id = %s", cid)
    if not c:
        raise HTTPException(404)
    ctx.require("comment.create", family_of(ctx, c["entity_type"], c["entity_id"]))
    return ctx.one("UPDATE comments SET resolved = %s WHERE id = %s RETURNING *", body.resolved, cid)


# Change requests ---------------------------------------------------------------

class CrIn(BaseModel):
    bom_id: int
    reason: str
    impact: str = ""
    priority: Literal["Low", "Normal", "Urgent"] = "Normal"


@router.get("/change-requests")
def change_requests(state: str = "", ctx=CTX):
    ctx.require("bom.read_released")
    return ctx.all("""SELECT c.*, b.option_code, i.code AS item_code, i.family_id, f.code AS family_code,
                             ru.name AS requested_by_name, du.name AS decided_by_name,
                             (SELECT json_agg(json_build_object('id', v.id, 'version_no', v.version_no, 'state', v.state))
                              FROM bom_versions v WHERE v.change_request_id = c.id) AS versions
                      FROM change_requests c JOIN boms b ON b.id = c.bom_id JOIN items i ON i.id = b.item_id
                      JOIN families f ON f.id = i.family_id JOIN users ru ON ru.id = c.requested_by LEFT JOIN users du ON du.id = c.decided_by
                      WHERE %s = '' OR c.state = %s ORDER BY c.id DESC""", state, state)


def raise_cr(ctx, body: CrIn):
    fam = family_of(ctx, "bom", body.bom_id)
    ctx.require("cr.create", fam)
    if not body.reason.strip():
        raise HTTPException(422, "A change request needs a reason")
    cr = ctx.one("INSERT INTO change_requests (bom_id, reason, impact, priority, requested_by) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                 body.bom_id, body.reason.strip(), body.impact, body.priority, ctx.uid)
    notify.change_request(ctx, cr, fam)
    return cr


@router.post("/change-requests")
def create_cr(body: CrIn, ctx=CTX):
    return raise_cr(ctx, body)


class DecideIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = ""


@router.post("/change-requests/{cid}/decide")
def decide_cr(cid: int, body: DecideIn, ctx=CTX):
    cr = ctx.one("SELECT * FROM change_requests WHERE id = %s", cid)
    if not cr:
        raise HTTPException(404)
    fam = family_of(ctx, "change_request", cid)
    ctx.require("cr.approve", fam)
    if cr["state"] != "Open":
        raise HTTPException(409, f"{cr['number']} is {cr['state']}")
    draft = None
    if body.decision == "approve":
        released = ctx.one("SELECT id FROM bom_versions WHERE bom_id = %s AND state = 'Released'", cr["bom_id"])
        cur = released or ctx.one("SELECT current_version_id AS id FROM boms WHERE id = %s", cr["bom_id"])
        draft = boms.new_draft(ctx, cr["bom_id"], cur and cur["id"], f"{cr['number']}: {cr['reason']}", cid)
    state = "Approved" if body.decision == "approve" else "Rejected"
    cr = ctx.one("UPDATE change_requests SET state = %s, decided_by = %s, decided_at = now() WHERE id = %s RETURNING *", state, ctx.uid, cid)
    if body.comment:
        ctx.conn.execute("INSERT INTO comments (entity_type, entity_id, user_id, body) VALUES ('change_request', %s, %s, %s)",
                         (cid, ctx.uid, body.comment))
    notify.change_request(ctx, cr, fam, decided=state)
    return {**cr, "draft": draft}


# Imports -----------------------------------------------------------------------

@router.post("/imports")
def create_import(file: UploadFile = File(...), item: str = Form(...), option_code: str = Form("Standard"), ctx=CTX):
    data = file.file.read()
    if len(data) > 5_000_000:
        raise HTTPException(422, "File too large (5 MB max)")
    return imports.create(ctx, file.filename or "diptrace.csv", data, item, option_code)


@router.get("/imports/{iid}")
def get_import(iid: int, ctx=CTX):
    return imports.get(ctx, iid)


class AnswersIn(BaseModel):
    answers: dict[str, int | None]


@router.post("/imports/{iid}/answers")
def answer_import(iid: int, body: AnswersIn, ctx=CTX):
    return imports.answer(ctx, iid, body.answers)


@router.post("/imports/{iid}/save")
def save_import(iid: int, ctx=CTX):
    return imports.save(ctx, iid)


# Exports -----------------------------------------------------------------------

class ExportIn(BaseModel):
    version: int
    format: Literal["xlsx", "gsheet", "pdf", "jlc", "pcbway"]
    filter: str = ""
    line_ids: list[int] | None = None
    folder_id: str | None = None


@router.post("/exports")
def create_export(body: ExportIn, ctx=CTX):
    return exports.make(ctx, body.version, body.format, body.filter, body.line_ids, body.folder_id)


@router.get("/exports/files/{token}/{name}")
def export_file(token: str, name: str, ctx=CTX):
    path = Path(S.DATA_DIR, "exports", token, name)
    if not token.isalnum() or "/" in name or ".." in name or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=name)


# Home --------------------------------------------------------------------------

@router.get("/home")
def home(ctx=CTX):
    rows = lambda sql, *a: ctx.all(sql, *a)
    reviews = [r for r in rows("""SELECT v.id, v.version_no, v.submitted_at, b.option_code, i.code AS item_code, i.family_id, u.name AS made_by
                                  FROM bom_versions v JOIN boms b ON b.id = v.bom_id JOIN items i ON i.id = b.item_id
                                  JOIN users u ON u.id = v.created_by WHERE v.state = 'In review' AND v.created_by <> %s
                                  ORDER BY v.submitted_at""", ctx.uid) if ctx.can("version.approve", r["family_id"])]
    to_release = [r for r in rows("""SELECT v.id, v.version_no, v.approved_at, b.option_code, i.code AS item_code, i.family_id
                                     FROM bom_versions v JOIN boms b ON b.id = v.bom_id JOIN items i ON i.id = b.item_id
                                     WHERE v.state = 'Approved' ORDER BY v.approved_at""") if ctx.can("version.release", r["family_id"])]
    return {
        "drafts": rows("""SELECT v.id, v.version_no, v.state, v.updated_at, b.option_code, i.code AS item_code
                          FROM bom_versions v JOIN boms b ON b.id = v.bom_id JOIN items i ON i.id = b.item_id
                          WHERE v.created_by = %s AND v.state IN ('Draft', 'In review', 'Approved') ORDER BY v.updated_at DESC""", ctx.uid),
        "reviews": reviews,
        "to_release": to_release,
        "change_requests": [c for c in change_requests("Open", ctx) if c["requested_by"] == ctx.uid or ctx.can("cr.approve", c["family_id"])],
        "releases": rows("""SELECT v.id, v.version_no, v.released_at, v.migrated, b.option_code, i.code AS item_code, u.name AS released_by
                            FROM bom_versions v JOIN boms b ON b.id = v.bom_id JOIN items i ON i.id = b.item_id
                            LEFT JOIN users u ON u.id = v.released_by WHERE v.state = 'Released' AND NOT v.migrated
                            ORDER BY v.released_at DESC LIMIT 15"""),
        "failed_pushes": [j for j in rows("""SELECT j.id, j.kind, j.target, j.error, j.finished_at, i.code AS item_code, i.family_id
                                             FROM odoo_jobs j LEFT JOIN bom_versions v ON v.id = j.bom_version_id
                                             LEFT JOIN boms b ON b.id = v.bom_id LEFT JOIN items i ON i.id = b.item_id
                                             WHERE j.status = 'Failed' AND j.finished_at > now() - interval '14 days' ORDER BY j.id DESC""")
                          if ctx.can("odoo.push", j["family_id"])],
    }
