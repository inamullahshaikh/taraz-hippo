"""Cost roll-up, shortage, where used, and old BOM Tool vs new."""
from collections import defaultdict
from decimal import Decimal

from fastapi import HTTPException

from . import boms


def cost(ctx, vid, qty=1):
    v = boms.version(ctx, vid)
    ctx.require("cost.read", v["family_id"])
    qty = Decimal(str(qty))
    missing, rows = [], []

    def walk(lines, mult, top):
        total = Decimal(0)
        for l in lines:
            n = l["qty"] * mult
            if l["part_id"]:
                ext = None if l["cost"] is None else l["cost"] * n
                if ext is None:
                    missing.append(l["part_code"] or l["mpn"])
            else:
                kids = l.get("children")
                ext = walk(kids["lines"], n, False) if kids else None
                if not kids:
                    missing.append(f"{l['child_code']} (no BOM)")
            total += ext or 0
            if top:
                rows.append({"line_id": l["id"], "component": l["part_code"] or l["child_code"], "qty": n,
                             "unit_cost": l["cost"] if l["part_id"] else (ext / n if ext is not None else None),
                             "extended": ext})
        return total

    total = walk(boms.explode(ctx, vid), qty, True)
    return {"qty": qty, "total": total, "unit": total / qty, "lines": rows, "missing_cost": sorted(set(missing))}


def flatten(lines, mult, need):
    """Leaf parts needed, multiplied through every sub-assembly level.
    ponytail: ignores stock of the sub-assemblies themselves; count SFG stock if planners need it."""
    for l in lines:
        n = l["qty"] * mult
        if l["part_id"]:
            p = need.setdefault(l["part_id"], {"code": l["part_code"], "mpn": l["mpn"], "on_hand": l["on_hand"], "need": Decimal(0)})
            p["need"] += n
        elif l.get("children"):
            flatten(l["children"]["lines"], n, need)
    return need


def shortage(ctx, vid, qty=1):
    v = boms.version(ctx, vid)
    ctx.require("cost.read", v["family_id"])
    need = flatten(boms.explode(ctx, vid), Decimal(str(qty)), {})
    short = [{**p, "short": p["need"] - (p["on_hand"] or 0)} for p in need.values() if p["need"] > (p["on_hand"] or 0)]
    return {"qty": qty, "parts": len(need), "short": sorted(short, key=lambda p: -p["short"])}


def where_used(ctx, part=None, item=None):
    if part:
        target = ctx.one("SELECT id, code FROM parts WHERE upper(code) = upper(%s) OR mpn = %s LIMIT 1", part, part)
        col = "part_id"
    else:
        target = ctx.one("SELECT id, code FROM items WHERE upper(code) = upper(%s)", item or "")
        col = "child_item_id"
    if not target:
        raise HTTPException(404, "Not found")
    rows = ctx.all(f"""SELECT w.bom_id, w.bom_version_id, w.version_no, w.state, w.option_code, w.qty, w.ref_des,
                              i.id AS item_id, i.code AS item_code, i.level, f.id AS family_id, f.code AS family
                       FROM where_used w JOIN items i ON i.id = w.item_id JOIN families f ON f.id = w.family_id
                       WHERE w.{col} = %s ORDER BY f.code, i.code, w.option_code""", target["id"])
    rows = [r for r in rows if ctx.can(ctx.read_code(r["state"]), r["family_id"])]
    # indirect: everything that contains those items, any depth, through released versions
    indirect = ctx.all("""WITH RECURSIVE up(item_id) AS (
                            SELECT unnest(%s::bigint[])
                            UNION
                            SELECT b.item_id FROM up JOIN bom_lines l ON l.child_item_id = up.item_id
                            JOIN bom_versions v ON v.id = l.bom_version_id AND v.state = 'Released'
                            JOIN boms b ON b.id = v.bom_id)
                          SELECT i.code AS item_code, f.code AS family FROM up JOIN items i ON i.id = up.item_id
                          JOIN families f ON f.id = i.family_id WHERE NOT (up.item_id = ANY(%s::bigint[])) ORDER BY 2, 1""",
                       [r["item_id"] for r in rows], [r["item_id"] for r in rows])
    return {"target": target["code"], "direct": rows, "indirect": indirect}


def legacy_compare(ctx, item_code, bom_id=None):
    """Old BOM Tool lines (through Conversions) against the item's current version, by part code."""
    item = ctx.one("SELECT id, code, family_id FROM items WHERE upper(code) = upper(%s)", item_code)
    if not item:
        raise HTTPException(404, "Item not found")
    ctx.require("bom.read_released", item["family_id"])
    conv = ctx.all("SELECT old_tool, old_item, old_code, change, note FROM conversions WHERE new_item_id = %s", item["id"])
    old_items = [c["old_item"] for c in conv]
    old = defaultdict(lambda: {"qty": Decimal(0), "refs": set(), "mpn": ""})
    for r in ctx.all("SELECT part_code, mpn, qty, ref_des FROM legacy_lines WHERE old_item = ANY(%s)", old_items):
        o = old[r["part_code"] or r["mpn"]]
        o["qty"] += r["qty"] or 0
        o["refs"] |= set(r["ref_des"].split())
        o["mpn"] = r["mpn"]
    bom = ctx.one("""SELECT b.id, b.current_version_id FROM boms b WHERE b.item_id = %s AND b.archived_at IS NULL
                     AND (b.id = %s OR (%s::bigint IS NULL AND b.is_default))""", item["id"], bom_id, bom_id)
    new = defaultdict(lambda: {"qty": Decimal(0), "refs": set(), "mpn": ""})
    if bom and bom["current_version_id"]:
        boms.version(ctx, bom["current_version_id"])
        for l in boms.lines(ctx, bom["current_version_id"]):
            n = new[l["part_code"] or l["child_code"]]
            n["qty"] += l["qty"]
            n["refs"] |= set(l["ref_des"].split())
            n["mpn"] = l["mpn"] or l["child_description"] or ""
    rows = []
    for code in sorted(old.keys() | new.keys(), key=boms.natkey):
        o, n = old.get(code), new.get(code)
        status = "only old" if not n else "only new" if not o else "same" if (o["qty"], o["refs"]) == (n["qty"], n["refs"]) else "changed"
        rows.append({"code": code, "mpn": (n or o)["mpn"], "status": status,
                     "old_qty": o and o["qty"], "new_qty": n and n["qty"],
                     "old_ref_des": o and " ".join(sorted(o["refs"], key=boms.natkey)),
                     "new_ref_des": n and " ".join(sorted(n["refs"], key=boms.natkey))})
    return {"item": item["code"], "conversions": conv, "rows": rows}
