"""Part search and photos, where used, old BOM Tool compare, global search."""
import base64
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..auth import CTX
from ..db import setting
from ..integrations import odoo
from ..services import analysis
from ..settings import S

router = APIRouter()


@router.get("/parts")
def parts(q: str = "", limit: int = 20, ctx=CTX):
    ctx.require("bom.read_released")
    return ctx.all("""SELECT id, code, mpn, manufacturer, description, uom, is_cmp, cost, on_hand, image_url, buy_links
                      FROM parts WHERE archived_at IS NULL AND (code ILIKE %(p)s OR mpn ILIKE %(p)s OR description ILIKE %(p)s)
                      ORDER BY (upper(code) = upper(%(q)s) OR upper(mpn) = upper(%(q)s)) DESC, code IS NULL, mpn LIMIT %(n)s""",
                   {"p": f"%{q}%", "q": q, "n": min(limit, 100)})


@router.get("/parts/{pid}/image")
def part_image(pid: int, ctx=CTX):
    """Photos come from Odoo once, then are served from the server's disk."""
    p = ctx.one("SELECT odoo_product_id FROM parts WHERE id = %s", pid)
    if not p or not p["odoo_product_id"]:
        raise HTTPException(404)
    path = Path(S.DATA_DIR, "images", f"{p['odoo_product_id']}.png")
    missing = path.with_suffix(".none")
    if not path.exists() and not missing.exists():
        if not setting(ctx.conn, "odoo_enabled"):
            raise HTTPException(404)
        try:
            data = odoo.image(S.ODOO_READ_TARGET, p["odoo_product_id"])
        except Exception:  # noqa: BLE001 - Odoo down: no photo, page still works
            raise HTTPException(404)
        path.parent.mkdir(parents=True, exist_ok=True)
        if data:
            path.write_bytes(base64.b64decode(data))
        else:
            missing.touch()
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/where-used")
def where_used(part: str = "", item: str = "", ctx=CTX):
    ctx.require("bom.read_released")
    if not part and not item:
        raise HTTPException(422, "Give a part or an item code")
    return analysis.where_used(ctx, part=part or None, item=item or None)


@router.get("/compare/legacy/{item}")
def legacy(item: str, bom_id: int | None = None, ctx=CTX):
    return analysis.legacy_compare(ctx, item, bom_id)


@router.get("/search")
def search(q: str, ctx=CTX):
    ctx.require("bom.read_released")
    p = f"%{q}%"
    return {"items": ctx.all("SELECT code, level, description FROM items WHERE archived_at IS NULL AND (code ILIKE %s OR description ILIKE %s) "
                             "ORDER BY code LIMIT 20", p, p),
            "parts": ctx.all("SELECT id, code, mpn, description FROM parts WHERE code ILIKE %s OR mpn ILIKE %s OR description ILIKE %s "
                             "ORDER BY code LIMIT 20", p, p, p)}
