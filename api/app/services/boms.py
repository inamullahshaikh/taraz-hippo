"""BOM versions: reading, exploding, diffing, editing drafts and moving them through their states."""
import re
from decimal import Decimal

from fastapi import HTTPException

from ..rbac import forbid
from . import notify

LINE_SQL = """
SELECT l.id, l.seq, l.qty, l.uom, l.ref_des, l.note, l.part_id, l.child_item_id,
       p.code AS part_code, p.mpn, p.manufacturer, p.description, p.is_cmp, p.image_url, p.buy_links,
       p.cost, p.on_hand, p.odoo_product_id,
       ci.code AS child_code, ci.description AS child_description, ci.odoo_product_id AS child_odoo_product_id
FROM bom_lines l
LEFT JOIN parts p ON p.id = l.part_id
LEFT JOIN items ci ON ci.id = l.child_item_id
WHERE l.bom_version_id = %s
ORDER BY l.seq, l.id
"""


def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# Reading ---------------------------------------------------------------------

def version(ctx, vid, code=None):
    """The version with its BOM, item and family; checks the person may read it (or `code` if given)."""
    v = ctx.one("""SELECT v.*, b.item_id, b.option_code, b.is_default, b.current_version_id, b.archived_at AS bom_archived_at,
                          i.code AS item_code, i.family_id, i.odoo_product_id AS item_odoo_product_id, i.odoo_code AS item_odoo_code
                   FROM bom_versions v JOIN boms b ON b.id = v.bom_id JOIN items i ON i.id = b.item_id
                   WHERE v.id = %s""", vid)
    if not v:
        raise HTTPException(404, "Version not found")
    ctx.require(code or ctx.read_code(v["state"]), v["family_id"])
    return v


def lines(ctx, vid):
    return ctx.all(LINE_SQL, vid)


def child_version(ctx, item_id):
    """The version a sub-assembly line opens to: its default BOM's Released version, or the newest
    version the person may read."""
    rows = ctx.all("""SELECT v.id, v.version_no, v.state, b.option_code, i.family_id, i.code AS item_code
                      FROM boms b JOIN items i ON i.id = b.item_id JOIN bom_versions v ON v.bom_id = b.id
                      WHERE b.item_id = %s AND b.is_default AND b.archived_at IS NULL AND v.state <> 'Superseded'
                      ORDER BY (v.state = 'Released') DESC, v.version_no DESC""", item_id)
    return next((r for r in rows if ctx.can(ctx.read_code(r["state"]), r["family_id"])), None)


def explode(ctx, vid, depth=0):
    out = lines(ctx, vid)
    if depth > 25:
        return out
    for line in out:
        if line["child_item_id"]:
            cv = child_version(ctx, line["child_item_id"])
            line["children"] = cv and {"version": cv, "lines": explode(ctx, cv["id"], depth + 1)}
    return out


def summarize(rows):
    """Lines grouped by component, so a diff reads 'qty 3 -> 4' rather than 'removed + added'."""
    out = {}
    for r in rows:
        k = ("part", r["part_id"]) if r["part_id"] else ("item", r["child_item_id"])
        s = out.setdefault(k, {"component": r["part_code"] or r["child_code"], "mpn": r["mpn"] or r["child_description"] or "",
                               "sub_assembly": bool(r["child_item_id"]), "qty": Decimal(0), "refs": set(), "notes": set()})
        s["qty"] += r["qty"]
        s["refs"] |= set(r["ref_des"].split())
        if r["note"]:
            s["notes"].add(r["note"])
    return out


def _fmt(s):
    return {"component": s["component"], "mpn": s["mpn"], "sub_assembly": s["sub_assembly"], "qty": s["qty"],
            "ref_des": " ".join(sorted(s["refs"], key=natkey)), "note": " | ".join(sorted(s["notes"]))}


def diff(rows_a, rows_b):
    a, b = summarize(rows_a), summarize(rows_b)
    changed = []
    for k in a.keys() & b.keys():
        x, y = _fmt(a[k]), _fmt(b[k])
        if (x["qty"], x["ref_des"], x["note"]) != (y["qty"], y["ref_des"], y["note"]):
            changed.append({"component": x["component"], "mpn": x["mpn"], "before": x, "after": y})
    key = lambda r: natkey(r["component"] or "")
    return {"added": sorted((_fmt(b[k]) for k in b.keys() - a.keys()), key=key),
            "removed": sorted((_fmt(a[k]) for k in a.keys() - b.keys()), key=key),
            "changed": sorted(changed, key=key)}


def last_released(ctx, bom_id, before_no=None):
    return ctx.one("""SELECT id, version_no FROM bom_versions WHERE bom_id = %s AND state IN ('Released', 'Superseded')
                      AND (%s::int IS NULL OR version_no < %s) ORDER BY version_no DESC LIMIT 1""", bom_id, before_no, before_no)


# Editing a draft -------------------------------------------------------------

def _bump(ctx, v, lock_version):
    if v["state"] != "Draft":
        raise HTTPException(409, f"v{v['version_no']} is {v['state']}: only a Draft can be edited")
    row = ctx.one("UPDATE bom_versions SET lock_version = lock_version + 1 WHERE id = %s AND lock_version = %s "
                  "RETURNING lock_version", v["id"], lock_version)
    if not row:
        raise HTTPException(409, {"message": "Someone else saved this draft first. Reload to see their changes.", "code": "stale"})
    return row["lock_version"]


def check_lines(new_lines):
    errors = []
    for n, l in enumerate(new_lines, 1):
        if (l.get("part_id") is None) == (l.get("child_item_id") is None):
            errors.append({"line": n, "reason": "pick a part or a sub-assembly"})
        try:
            if Decimal(str(l.get("qty"))) <= 0:
                errors.append({"line": n, "reason": "qty must be above 0"})
        except Exception:
            errors.append({"line": n, "reason": "qty must be a number"})
    if errors:
        raise HTTPException(422, {"message": f"Line {errors[0]['line']}: {errors[0]['reason']}", "errors": errors})


def save_lines(ctx, vid, lock_version, new_lines):
    """Save the full line list of a draft. Unchanged lines are left alone, so the audit log shows real edits only."""
    v = version(ctx, vid, "bom.edit")
    check_lines(new_lines)
    lock = _bump(ctx, v, lock_version)
    old = {r["id"] for r in ctx.all("SELECT id FROM bom_lines WHERE bom_version_id = %s", vid)}
    keep = []
    for seq, l in enumerate(new_lines, 1):
        vals = (seq, l.get("part_id"), l.get("child_item_id"), l["qty"], l.get("uom") or "Units",
                " ".join((l.get("ref_des") or "").split()), l.get("note") or "")
        if l.get("id") in old:
            keep.append(l["id"])
            ctx.conn.execute("""UPDATE bom_lines SET seq = %s, part_id = %s, child_item_id = %s, qty = %s, uom = %s, ref_des = %s, note = %s
                                WHERE id = %s AND (seq, part_id, child_item_id, qty, uom, ref_des, note)
                                      IS DISTINCT FROM (%s, %s, %s, %s::numeric, %s, %s, %s)""", (*vals, l["id"], *vals))
        else:
            keep.append(ctx.one("""INSERT INTO bom_lines (bom_version_id, seq, part_id, child_item_id, qty, uom, ref_des, note)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", vid, *vals)["id"])
    ctx.conn.execute("DELETE FROM bom_lines WHERE bom_version_id = %s AND NOT (id = ANY(%s))", (vid, keep))
    return {"lock_version": lock}


def bulk(ctx, vid, lock_version, line_ids, op, value=None):
    """Mass edit: set_note, add_note, set_qty, remove."""
    v = version(ctx, vid, "bom.edit")
    if op == "set_qty":
        check_lines([{"part_id": 1, "qty": value}])
    lock = _bump(ctx, v, lock_version)
    sql = {"set_note": "UPDATE bom_lines SET note = %(v)s",
           "add_note": "UPDATE bom_lines SET note = trim(note || ' ' || %(v)s)",
           "set_qty": "UPDATE bom_lines SET qty = %(v)s",
           "remove": "DELETE FROM bom_lines"}.get(op)
    if not sql:
        raise HTTPException(422, f"Unknown mass edit '{op}'")
    n = ctx.conn.execute(sql + " WHERE bom_version_id = %(vid)s AND id = ANY(%(ids)s)",
                         {"v": value, "vid": vid, "ids": line_ids}).rowcount
    return {"lock_version": lock, "changed": n}


# States ----------------------------------------------------------------------

def new_draft(ctx, bom_id, from_vid=None, note="", cr_id=None):
    """Open the next version as a Draft, copying the lines of `from_vid`."""
    n = ctx.one("SELECT coalesce(max(version_no), 0) + 1 AS n FROM bom_versions WHERE bom_id = %s", bom_id)["n"]
    vid = ctx.one("""INSERT INTO bom_versions (bom_id, version_no, created_by, change_note, change_request_id)
                     VALUES (%s, %s, %s, %s, %s) RETURNING id""", bom_id, n, ctx.uid, note, cr_id)["id"]
    if from_vid:
        ctx.conn.execute("""INSERT INTO bom_lines (bom_version_id, seq, part_id, child_item_id, qty, uom, ref_des, note)
                            SELECT %s, seq, part_id, child_item_id, qty, uom, ref_des, note FROM bom_lines WHERE bom_version_id = %s""",
                         (vid, from_vid))
    ctx.conn.execute("UPDATE boms SET current_version_id = coalesce(current_version_id, %s) WHERE id = %s", (vid, bom_id))
    return {"id": vid, "version_no": n}


def restore(ctx, vid):
    v = version(ctx, vid)
    ctx.require("version.restore", v["family_id"])
    return new_draft(ctx, v["bom_id"], vid, f"Restored from v{v['version_no']}")


def submit(ctx, vid, note=""):
    v = version(ctx, vid, "bom.submit")
    if v["state"] != "Draft":
        raise HTTPException(409, f"v{v['version_no']} is {v['state']}, not a Draft")
    if not ctx.one("SELECT 1 FROM bom_lines WHERE bom_version_id = %s LIMIT 1", vid):
        raise HTTPException(422, "An empty BOM cannot be sent for review")
    ctx.conn.execute("UPDATE bom_versions SET state = 'In review', submitted_at = now(), "
                     "change_note = CASE WHEN %s = '' THEN change_note ELSE %s END WHERE id = %s", (note, note, vid))
    notify.reviewers(ctx, v)
    return {"state": "In review"}


def review(ctx, vid, decision, comment=""):
    v = version(ctx, vid, "version.approve")
    if v["state"] != "In review":
        raise HTTPException(409, f"v{v['version_no']} is {v['state']}, not In review")
    if v["created_by"] == ctx.uid:
        forbid("version.approve", "Four eyes: the person who made a version cannot approve it")
    if decision not in ("Approve", "Reject", "Ask changes"):
        raise HTTPException(422, "Decision must be Approve, Reject or Ask changes")
    ctx.conn.execute("INSERT INTO approvals (bom_version_id, reviewer_id, decision, comment) VALUES (%s, %s, %s, %s)",
                     (vid, ctx.uid, decision, comment))
    if decision == "Approve":
        ctx.conn.execute("UPDATE bom_versions SET state = 'Approved', approved_by = %s, approved_at = now() WHERE id = %s", (ctx.uid, vid))
    else:
        ctx.conn.execute("UPDATE bom_versions SET state = 'Draft' WHERE id = %s", (vid,))
    notify.maker(ctx, v, decision, comment)
    return {"state": "Approved" if decision == "Approve" else "Draft"}


def release(ctx, vid):
    v = version(ctx, vid, "version.release")
    if v["state"] != "Approved":
        raise HTTPException(409, f"v{v['version_no']} is {v['state']}: only an Approved version can be released")
    ctx.conn.execute("UPDATE bom_versions SET state = 'Superseded' WHERE bom_id = %s AND state = 'Released'", (v["bom_id"],))
    ctx.conn.execute("UPDATE bom_versions SET state = 'Released', released_by = %s, released_at = now() WHERE id = %s", (ctx.uid, vid))
    ctx.conn.execute("UPDATE boms SET current_version_id = %s, status = 'Active' WHERE id = %s", (vid, v["bom_id"]))
    if v["change_request_id"]:
        ctx.conn.execute("UPDATE change_requests SET state = 'Done' WHERE id = %s", (v["change_request_id"],))
    stale = stale_parents(ctx, v["item_id"])
    notify.released(ctx, v, stale)
    return {"state": "Released", "stale_parents": stale}


def stale_parents(ctx, item_id):
    """Parent BOMs whose live version was released before this sub-assembly's newest release."""
    return ctx.all("""SELECT DISTINCT i.code AS item_code, b.option_code, pv.version_no, f.owner_id
                      FROM bom_lines l
                      JOIN bom_versions pv ON pv.id = l.bom_version_id AND pv.state = 'Released'
                      JOIN boms b ON b.id = pv.bom_id JOIN items i ON i.id = b.item_id JOIN families f ON f.id = i.family_id
                      WHERE l.child_item_id = %s AND pv.released_at < now() - interval '1 second'""", item_id)
