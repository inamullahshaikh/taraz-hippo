"""One BOM version: read, explode, diff, edit lines, and move through Draft -> In review -> Approved -> Released."""
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..auth import CTX
from ..services import analysis, boms

router = APIRouter()


@router.get("/versions/{vid}")
def get_version(vid: int, explode: bool = False, ctx=CTX):
    v = boms.version(ctx, vid)
    prev = boms.last_released(ctx, v["bom_id"], v["version_no"])
    return {
        "version": v,
        "lines": boms.explode(ctx, vid) if explode else boms.lines(ctx, vid),
        "approvals": ctx.all("""SELECT a.decision, a.comment, a.decided_at, u.name FROM approvals a JOIN users u ON u.id = a.reviewer_id
                                WHERE a.bom_version_id = %s ORDER BY a.decided_at""", vid),
        "people": {r["id"]: r["name"] for r in ctx.all("SELECT id, name FROM users WHERE id = ANY(%s)",
                                                       [x for x in (v["created_by"], v["approved_by"], v["released_by"]) if x])},
        "previous_released_id": prev and prev["id"],
    }


@router.get("/versions/{a}/diff/{b}")
def diff(a: int, b: int, ctx=CTX):
    va, vb = boms.version(ctx, a), boms.version(ctx, b)
    return {"from": {"id": a, "version_no": va["version_no"], "item_code": va["item_code"], "option_code": va["option_code"]},
            "to": {"id": b, "version_no": vb["version_no"], "item_code": vb["item_code"], "option_code": vb["option_code"]},
            **boms.diff(boms.lines(ctx, a), boms.lines(ctx, b))}


class LineIn(BaseModel):
    id: int | None = None
    part_id: int | None = None
    child_item_id: int | None = None
    qty: Decimal
    uom: str = "Units"
    ref_des: str = ""
    note: str = ""


class LinesIn(BaseModel):
    lock_version: int
    lines: list[LineIn]


@router.put("/versions/{vid}/lines")
def save_lines(vid: int, body: LinesIn, ctx=CTX):
    return boms.save_lines(ctx, vid, body.lock_version, [l.model_dump() for l in body.lines])


class BulkIn(BaseModel):
    lock_version: int
    line_ids: list[int]
    op: Literal["set_note", "add_note", "set_qty", "remove"]
    value: str | None = None


@router.post("/versions/{vid}/lines/bulk")
def bulk(vid: int, body: BulkIn, ctx=CTX):
    return boms.bulk(ctx, vid, body.lock_version, body.line_ids, body.op, body.value)


class SubmitIn(BaseModel):
    note: str = ""


@router.post("/versions/{vid}/submit")
def submit(vid: int, body: SubmitIn = SubmitIn(), ctx=CTX):
    return boms.submit(ctx, vid, body.note)


class ReviewIn(BaseModel):
    decision: Literal["Approve", "Reject", "Ask changes"]
    comment: str = ""


@router.post("/versions/{vid}/review")
def review(vid: int, body: ReviewIn, ctx=CTX):
    return boms.review(ctx, vid, body.decision, body.comment)


@router.post("/versions/{vid}/release")
def release(vid: int, ctx=CTX):
    return boms.release(ctx, vid)


@router.post("/versions/{vid}/restore")
def restore(vid: int, ctx=CTX):
    return boms.restore(ctx, vid)


@router.get("/versions/{vid}/cost")
def cost(vid: int, qty: Decimal = Decimal(1), ctx=CTX):
    return analysis.cost(ctx, vid, qty)


@router.get("/versions/{vid}/shortage")
def shortage(vid: int, qty: Decimal = Decimal(1), ctx=CTX):
    return analysis.shortage(ctx, vid, qty)
