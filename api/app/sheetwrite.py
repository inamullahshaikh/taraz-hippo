"""Writes from the BOMTree page (update_values / append_values on one tab) applied to Postgres.

The write is applied to the tab as the writer sees it (`base`; by default the tab as Postgres has it now), then the
result is compared with `base` row by row, by key, and only what changed is saved. A row that changed in Postgres
since `base` is not overwritten: it is returned as a conflict. (Live page writes pass no base, so they never
conflict; changes queued from the page's own store pass the copy they were made on.)

Keys: Items by item, BOMs by BOM, Lines by BOM (all its lines together), Blocks by block. The other tabs only mirror
sheet data, so they are saved whole.
"""
import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .routers.sheets import TZ, parse_range, read_tab, tabs_for

ODOO_CODE = re.compile(r"^P\d{6}$")
# sheet status -> boms.status (the page writes To review; the sheets also had the migration's words)
BOM_STATUS = {"active": "Active", "to review": "Under review", "under review": "Under review",
              "needs info": "Under review", "archived": "Archived", "not to be used": "Archived"}


def bad(msg):
    raise HTTPException(422, {"code": "invalid", "message": msg})


# cells -------------------------------------------------------------------------

def t(v):
    """A cell as text: what the sheet stores (a leading ' only marks text in Sheets)."""
    s = "" if v is None else str(v).replace(" ", " ").strip()
    return s[1:] if s.startswith("'") else s


def opt(v):
    return t(v) or None


def num(v, field):
    s = t(v)
    if not s:
        return None
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        bad(f"{field} '{s}' is not a number")


def whole(v, field):
    n = num(v, field)
    if n is not None and n != n.to_integral_value():
        bad(f"{field} '{t(v)}' is not a whole number")
    return None if n is None else int(n)


def when(v, field):
    s = t(v)
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    bad(f"{field} '{s}' is not a date (use YYYY-MM-DD or YYYY-MM-DD HH:MM)")


def day(v, field):
    d = when(v, field)
    return d.date() if d else None


def yes(v):
    return t(v).lower() in ("yes", "y", "true", "1", "x", "✓")


def js(v, field, default):
    s = t(v)
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        bad(f"{field} is not valid JSON ({e.msg})")


# the write itself ------------------------------------------------------------------

def apply(grid, mode, c1, r1, values, width):
    """The tab after the write, as Sheets would have it."""
    out = [list(r) + [""] * (width - len(r)) for r in grid]
    if mode == "append":
        last = max((i for i, r in enumerate(out) if any(t(c) for c in r)), default=0)
        out = out[:last + 1]
        start = len(out)
    else:
        start = r1 - 1
    for i, vals in enumerate(values or []):
        r = start + i
        while len(out) <= r:
            out.append([""] * width)
        for j, v in enumerate(vals or []):
            c = c1 - 1 + j
            if c < width:
                out[r][c] = "" if v is None else str(v)
    return out


def body(grid, width):
    """Data rows as clean text, blank rows left out (the page clears rows by writing blanks)."""
    return [[t(c) for c in (list(r) + [""] * width)[:width]] for r in grid[1:] if any(t(c) for c in r)]


def write(ctx, sheet_id, rng, values, mode, base=None):
    tab, c1, _, r1, _ = parse_range(rng)
    tabs, fam = tabs_for(ctx.conn, sheet_id)
    if tab not in tabs:
        raise HTTPException(404, {"code": "not_found", "message": f"No tab '{tab}'"})
    if tab == "Where used":          # worked out live by the database
        return []
    header = tabs[tab][0]
    cur = read_tab(ctx.conn, sheet_id, tab, ctx.uid)
    start = [header] + base if base is not None else cur
    new = apply(start, mode, c1, r1, values, len(header))
    if [t(c) for c in new[0]] != header:
        bad(f"The columns of {tab} can't be changed here")
    w = len(header)
    old_rows, new_rows, cur_rows = body(start, w), body(new, w), body(cur, w)
    if tab in ("Families", "Parts"):
        bad(f"{tab} is changed by an admin in the database, not from the page")
    if fam is not None and not ctx.can("bom.edit", fam):
        raise HTTPException(403, {"code": "no_permission", "message": "You can't change this family's BOMs"})
    if fam is None and tab != "Items" and not edits_somewhere(ctx):
        raise HTTPException(403, {"code": "no_permission", "message": "You can't change BOMTree data"})
    if tab == "Log":
        return log(ctx, old_rows, new_rows)
    fn = {"Items": items, "BOMs": boms, "Lines": lines, "Blocks": blocks}.get(tab)
    if fn:
        return fn(ctx, fam, cur_rows, old_rows, new_rows)
    return replace_whole(ctx, fam, tab, cur_rows, old_rows, new_rows)


def edits_somewhere(ctx):
    return ctx.conn.execute("""SELECT has_perm(%(u)s, 'bom.edit', NULL)
                               OR EXISTS (SELECT 1 FROM families f WHERE has_perm(%(u)s, 'bom.edit', f.id)) AS ok""",
                            {"u": ctx.uid}).fetchone()["ok"]


def changed(cur, old, new, key):
    """{key: new value or None (removed)} for what the writer changed, and the keys that changed under them."""
    C, B, N = group(cur, key), group(old, key), group(new, key)
    out, conflicts = {}, []
    for k in list(B) + [k for k in N if k not in B]:
        if B.get(k) == N.get(k):
            continue
        if C.get(k) != B.get(k):
            conflicts.append(k)
            continue
        out[k] = N.get(k)
    return out, conflicts


def group(rows, key):
    out = {}
    for r in rows:
        k = key(r)
        if not k:
            bad(f"A row has an empty first column: {r[:4]}")
        out.setdefault(k, []).append(r)
    return out


def single(rows, what):
    if rows is not None and len(rows) > 1:
        bad(f"{what} {rows[0][0]} is listed twice")
    return rows and rows[0]


def q(ctx, sql, *args):
    return ctx.conn.execute(sql, args)


# Items --------------------------------------------------------------------------

def items(ctx, _, cur, old, new):
    todo, conflicts = changed(cur, old, new, lambda r: r[0])
    for code, rows in todo.items():
        row = single(rows, "Item")
        it = q(ctx, "SELECT id, family_id, deleted_at FROM items WHERE code = %s", code).fetchone()
        if row is None:                                        # removed from the page: hidden, kept for history
            if it:
                need(ctx, "item.archive", it["family_id"])
                q(ctx, "UPDATE items SET deleted_at = now(), archived_at = coalesce(archived_at, now()) WHERE id = %s", it["id"])
            continue
        code, prefix, fam, level, status, by, what, today, odoo, notes, brain, block = row
        f = q(ctx, "SELECT id FROM families WHERE code = %s AND archived_at IS NULL", fam).fetchone()
        if not f:
            bad(f"Item {code}: family '{fam}' is not a BOMTree family")
        level = level.upper()
        if level not in ("FG", "SFG"):
            bad(f"Item {code}: Level must be FG or SFG (is '{level}')")
        if odoo and not ODOO_CODE.match(odoo):
            bad(f"Item {code}: Odoo code '{odoo}' should look like P012345")
        blk = q(ctx, "SELECT id FROM blocks WHERE name = %s", block).fetchone() if block else None
        vals = {"prefix": prefix or code.split("-")[0], "family_id": f["id"], "level": level, "status": status or "Proposed",
                "built_by": by or None, "description": what or None, "odoo_today": today or None,
                "odoo_code": odoo or None, "notes": notes or None, "brain_url": brain or None,
                "block_override_id": blk and blk["id"], "archived": (status or "").lower() == "archived"}
        if it and it["deleted_at"] is None:
            need(ctx, "bom.edit", it["family_id"])
        need(ctx, "item.create" if not it or it["deleted_at"] else "bom.edit", f["id"])
        sets = ("prefix = %(prefix)s, family_id = %(family_id)s, level = %(level)s, status = %(status)s, "
                "built_by = %(built_by)s, description = %(description)s, odoo_today = %(odoo_today)s, "
                "odoo_code = %(odoo_code)s, notes = %(notes)s, brain_url = %(brain_url)s, "
                "block_override_id = %(block_override_id)s, deleted_at = NULL, "
                "archived_at = CASE WHEN %(archived)s THEN coalesce(archived_at, now()) END")
        if it:
            ctx.conn.execute(f"UPDATE items SET {sets} WHERE id = %(id)s", {**vals, "id": it["id"]})
        else:
            ctx.conn.execute("""INSERT INTO items (code, prefix, family_id, level, status, built_by, description, odoo_today,
                                odoo_code, notes, brain_url, block_override_id, archived_at)
                                VALUES (%(code)s, %(prefix)s, %(family_id)s, %(level)s, %(status)s, %(built_by)s,
                                %(description)s, %(odoo_today)s, %(odoo_code)s, %(notes)s, %(brain_url)s,
                                %(block_override_id)s, CASE WHEN %(archived)s THEN now() END)""", {**vals, "code": code})
    return [{"tab": "Items", "key": k} for k in conflicts]


def need(ctx, perm, family_id):
    if not ctx.can(perm, family_id):
        raise HTTPException(403, {"code": "no_permission", "message": f"You don't have the right '{perm}' for that family"})


# BOMs and Lines --------------------------------------------------------------------

BOM_OF_FAMILY = """SELECT b.id, b.item_id, b.option_code, b.status, b.current_version_id, b.deleted_at, i.code AS item
                   FROM boms b JOIN items i ON i.id = b.item_id
                   WHERE i.family_id = %s AND coalesce(b.legacy_key,
                         i.code || CASE WHEN b.option_code <> 'Standard' THEN ' · ' || b.option_code ELSE '' END) = %s"""


def boms(ctx, fam, cur, old, new):
    todo, conflicts = changed(cur, old, new, lambda r: r[0])
    touched = set()
    for key, rows in todo.items():
        row = single(rows, "BOM")
        b = q(ctx, BOM_OF_FAMILY, fam, key).fetchone()
        if row is None:                                        # removed: hidden, kept with its versions
            if b:
                q(ctx, "UPDATE boms SET deleted_at = now(), archived_at = coalesce(archived_at, now()), "
                       "is_default = false, status = 'Archived' WHERE id = %s", b["id"])
            continue
        key, item, option, default, when_to_use, status, notes = row
        st = BOM_STATUS.get(status.lower() or "active")
        if not st:
            bad(f"BOM {key}: Status must be Active, To review or Archived (is '{status}')")
        it = q(ctx, "SELECT id, family_id FROM items WHERE code = %s AND deleted_at IS NULL", item).fetchone()
        if not it:
            bad(f"BOM {key}: item '{item}' is not in the Items tab")
        vals = {"item_id": it["id"], "option": option or "Standard", "when": when_to_use or None, "status": st,
                "notes": notes or None, "key": key, "archived": st == "Archived"}
        if b:
            q(ctx, "UPDATE boms SET is_default = false WHERE id = %s", b["id"])   # defaults are set below, once each
            ctx.conn.execute("""UPDATE boms SET item_id = %(item_id)s, option_code = %(option)s, when_to_use = %(when)s,
                                status = %(status)s, notes = %(notes)s, legacy_key = %(key)s, deleted_at = NULL,
                                archived_at = CASE WHEN %(archived)s THEN coalesce(archived_at, now()) END
                                WHERE id = %(id)s""", {**vals, "id": b["id"]})
            bom_id = b["id"]
        else:
            bom_id = ctx.conn.execute("""INSERT INTO boms (item_id, option_code, legacy_key, is_default, when_to_use, status,
                                         notes, archived_at)
                                         VALUES (%(item_id)s, %(option)s, %(key)s, false, %(when)s, %(status)s, %(notes)s,
                                         CASE WHEN %(archived)s THEN now() END) RETURNING id""", vals).fetchone()["id"]
            ver = q(ctx, "INSERT INTO bom_versions (bom_id, version_no, state, created_by, change_note) "
                         "VALUES (%s, 1, 'Draft', %s, 'Made in the BOMTree page') RETURNING id", bom_id, ctx.uid).fetchone()["id"]
            q(ctx, "UPDATE boms SET current_version_id = %s WHERE id = %s", ver, bom_id)
        touched.add((bom_id, yes(default)))
    for bom_id, is_default in touched:
        if is_default:
            q(ctx, "UPDATE boms SET is_default = false WHERE item_id = (SELECT item_id FROM boms WHERE id = %s) "
                   "AND id <> %s AND is_default", bom_id, bom_id)
            q(ctx, "UPDATE boms SET is_default = true WHERE id = %s", bom_id)
        settle(ctx, bom_id, from_released=False)
    return [{"tab": "BOMs", "key": k} for k in conflicts]


def lines(ctx, fam, cur, old, new):
    todo, conflicts = changed(cur, old, new, lambda r: r[0])
    for key, rows in todo.items():
        b = q(ctx, BOM_OF_FAMILY + " AND b.deleted_at IS NULL", fam, key).fetchone()
        if not b:
            bad(f"BOM '{key}' is not in the BOMs tab of this family")
        payload = [line_json(ctx, key, r) for r in rows or []]
        v = q(ctx, "SELECT id, state, lock_version FROM bom_versions WHERE id = %s", b["current_version_id"]).fetchone()
        was_released = v["state"] == "Released"
        if was_released:
            vid = q(ctx, "SELECT start_draft(%s, %s) AS id", ctx.uid, b["id"]).fetchone()["id"]
            lock = 0
        else:
            if v["state"] in ("In review", "Approved"):      # changed again before it was released: back to draft
                q(ctx, "UPDATE bom_versions SET state = 'Draft' WHERE id = %s", v["id"])
            vid, lock = v["id"], v["lock_version"]
        q(ctx, "SELECT save_draft_lines(%s, %s, %s, %s::jsonb)", ctx.uid, vid, lock, json.dumps(payload))
        settle(ctx, b["id"], from_released=was_released, draft_id=vid)
    return [{"tab": "Lines", "key": k} for k in conflicts]


def settle(ctx, bom_id, from_released, draft_id=None):
    """A page save is the BOM: a draft made from a released version is released at once; a BOM that was
    never released (Needs info) is released when it is set Active and has lines."""
    b = q(ctx, "SELECT status, current_version_id FROM boms WHERE id = %s", bom_id).fetchone()
    vid = draft_id or b["current_version_id"]
    v = q(ctx, "SELECT state, (SELECT count(*) FROM bom_lines WHERE bom_version_id = %s) AS n FROM bom_versions WHERE id = %s",
          vid, vid).fetchone()
    if v["state"] == "Draft" and (from_released or (b["status"] == "Active" and v["n"] > 0)):
        q(ctx, "SELECT release_direct(%s, %s)", ctx.uid, vid)


def line_json(ctx, key, r):
    comp, code, qty, ref, note = r[1], r[2], r[3], r[4], r[5]
    if not comp:
        bad(f"BOM {key}: a line has no Component")
    n = num(qty, f"BOM {key} › {comp} › Qty")
    if n is None or n <= 0:
        bad(f"BOM {key} › {comp}: Qty must be above 0")
    out = {"component_label": comp, "qty": str(n), "ref_des": ref, "note": note}
    it = q(ctx, "SELECT id FROM items WHERE code = %s AND deleted_at IS NULL", comp).fetchone()
    if it:
        return {**out, "child_item_id": it["id"]}
    if code:
        if not ODOO_CODE.match(code):
            bad(f"BOM {key} › {comp}: Code '{code}' should look like P012345")
        p = q(ctx, "SELECT id FROM parts WHERE code = %s", code).fetchone() \
            or q(ctx, "INSERT INTO parts (code, mpn) VALUES (%s, %s) RETURNING id", code, comp).fetchone()
    else:
        p = q(ctx, "SELECT id FROM parts WHERE code IS NULL AND mpn = %s", comp).fetchone() \
            or q(ctx, "INSERT INTO parts (code, mpn) VALUES (NULL, %s) RETURNING id", comp).fetchone()
    return {**out, "part_id": p["id"]}


# Blocks (items point at them, so kept by name) -----------------------------------------

def blocks(ctx, _, cur, old, new):
    todo, conflicts = changed(cur, old, new, lambda r: r[0])
    for name, rows in todo.items():
        row = single(rows, "Block")
        if row is None:
            if q(ctx, "SELECT 1 FROM items i JOIN blocks b ON b.id = i.block_override_id WHERE b.name = %s", name).fetchone():
                bad(f"Block {name} is still set on some items")
            q(ctx, "DELETE FROM blocks WHERE name = %s", name)
            continue
        vals = (whole(row[1], "Show order") or 99, row[2] or None, row[3] or None, row[4] or None, whole(row[5], "Match order"))
        q(ctx, """INSERT INTO blocks (name, show_order, level, description, name_rule, match_order)
                  VALUES (%s, %s, %s, %s, %s, %s)
                  ON CONFLICT (name) DO UPDATE SET show_order = excluded.show_order, level = excluded.level,
                  description = excluded.description, name_rule = excluded.name_rule, match_order = excluded.match_order""",
          name, *vals)
    return [{"tab": "Blocks", "key": k} for k in conflicts]


# Log: only new rows are added ------------------------------------------------------------

def log(ctx, old, new):
    if new[:len(old)] != old:
        bad("Rows of the Log can't be changed, only added")
    for _, who, action, item, detail in new[len(old):]:
        q(ctx, """INSERT INTO audit_log (user_id, actor_label, source, action, entity_type, entity_label, detail)
                  VALUES (%s, %s, current_source(), %s, 'sheet log', %s, %s)""",
          ctx.uid, who or ctx.user["name"], action or "note", item or None, detail or None)
    return []


# Tabs that only mirror sheet data: saved whole -------------------------------------------

def item_id(ctx, code):
    r = q(ctx, "SELECT id FROM items WHERE code = %s AND deleted_at IS NULL", code).fetchone() if code else None
    return r and r["id"]


# tab -> (table, family-scoped, has seq, row -> {column: value})
WHOLE = {
    "Old lines": ("legacy_lines", True, True, lambda c, r: dict(
        old_item=r[0] or bad("Old lines: Old item is empty"), old_code=opt(r[1]), old_tool=opt(r[2]), component=opt(r[3]),
        part_code=opt(r[4]), qty=num(r[5], "Qty"), ref_des=opt(r[6]), note=opt(r[7]))),
    "PNP": ("placements", True, True, lambda c, r: dict(
        board=r[0] or bad("PNP: Board is empty"), ref_des=r[1] or bad("PNP: Ref des is empty"), name=opt(r[2]),
        package=opt(r[3]), x_mm=num(r[4], "X (mm)"), y_mm=num(r[5], "Y (mm)"), side=side(r[6]),
        rotation=num(r[7], "Rotation"), value=opt(r[8]), source=opt(r[9]), loaded_at=when(r[10], "Loaded"))),
    "Design": ("design_refs", True, True, lambda c, r: dict(
        board=r[0] or bad("Design: Board is empty"), ref_des=r[1] or bad("Design: Ref des is empty"), block_path=opt(r[2]),
        copy_label=opt(r[3]), level=opt(r[4]), role=opt(r[5]), why=opt(r[6]), design_mpn=opt(r[7]),
        design_code=opt(r[8]), confirmed_by=opt(r[9]), confirmed_on=day(r[10], "Confirmed on"))),
    "Design nets": ("design_nets", True, True, lambda c, r: dict(
        board=r[0] or bad("Design nets: Board is empty"), net=r[1] or bad("Design nets: Net is empty"), kind=opt(r[2]),
        sheet=opt(r[3]), ends=opt(r[4]))),
    "Conversions": ("conversions", False, True, lambda c, r: dict(
        old_tool=opt(r[0]), old_item=opt(r[1]), old_code=opt(r[2]), change=opt(r[3]), new_item_id=item_id(c, r[4]),
        new_item_text=opt(r[4]), note=opt(r[5]))),
    "Alternates": ("alternates", False, True, lambda c, r: dict(
        group_name=r[0] or r[1] or bad("Alternates: Group is empty"), base_part=opt(r[1]), base_code=opt(r[2]),
        mfr_pn=opt(r[3]), mfr=opt(r[4]), digikey_pn=opt(r[5]), description=opt(r[6]), kind=opt(r[7]), status=opt(r[8]),
        odoo_code=opt(r[9]), search=opt(r[10]), approved_by=opt(r[11]), approved_on=day(r[12], "Approved on"),
        note=opt(r[13]), url=opt(r[14]), product=opt(r[15]))),
    "Alternate searches": ("alternate_searches", False, True, lambda c, r: dict(
        search=opt(r[0]), group_name=opt(r[1]), base_part=opt(r[2]), base_code=opt(r[3]), kind=opt(r[4]),
        searched_on=day(r[5], "Date"), searched_by=opt(r[6]), rows_in_csv=whole(r[7], "Rows in CSV"), approved=opt(r[8]),
        file=opt(r[9]), digikey_link=opt(r[10]), note=opt(r[11]))),
    "Design blocks": ("design_blocks", False, True, lambda c, r: dict(
        block_type=r[0] or bad("Design blocks: Block type is empty"), function=opt(r[1]), products=opt(r[2]),
        boards=opt(r[3]), copies=whole(r[4], "Copies"), lines=whole(r[5], "Lines"), critical=whole(r[6], "Critical"),
        sensitive=whole(r[7], "Sensitive"), normal=whole(r[8], "Normal"), fingerprint=opt(r[9]), status=opt(r[10]),
        confirmed_by=opt(r[11]), confirmed_on=day(r[12], "Confirmed on"))),
    "Design issues": ("design_issues", False, True, lambda c, r: dict(
        board=r[0] or bad("Design issues: Board is empty"), ref_des=opt(r[1]), issue=r[2] or bad("Design issues: Issue is empty"),
        source=opt(r[3]), status=r[4] or "Open")),
    "Library": ("board_library", False, False, lambda c, r: dict(
        item_id=item_id(c, r[0]), item_text=r[0] or bad("Library: Item is empty"), code=opt(r[1]),
        width_mm=num(r[2], "Width mm"), height_mm=num(r[3], "Height mm"), front_picture=opt(r[4]), back_picture=opt(r[5]),
        ports=Jsonb(js(r[6], "Ports", [])), source=opt(r[7]), updated_at=when(r[8], "Updated"), notes=opt(r[9]))),
    "Canvas": ("canvases", False, False, lambda c, r: dict(
        item_id=item_id(c, r[0]), item_text=r[0] or bad("Canvas: Item is empty"), pages=opt(r[1]),
        data=Jsonb(js(r[2], "Data", None)) if t(r[2]) else None, updated_at=when(r[3], "Updated"),
        updated_by=opt(r[4]), notes=opt(r[5]))),
    "Tools": ("old_tools", False, False, lambda c, r: dict(
        tool=r[0] or bad("Tools: Tool is empty"), archived=yes(r[1]), changed_by=opt(r[2]), changed_at=when(r[3], "When"),
        note=opt(r[4]))),
}


def side(v):
    s = t(v).capitalize()
    if s and s not in ("Top", "Bottom"):
        bad(f"Side must be Top or Bottom (is '{t(v)}')")
    return s or None


def replace_whole(ctx, fam, tab, cur, old, new):
    if old == new:
        return []
    if cur != old:
        return [{"tab": tab, "key": "(whole tab)"}]
    table, per_family, has_seq, conv = WHOLE[tab]
    rows = [conv(ctx, r) for r in new]
    where = "WHERE family_id = %s" if per_family else ""
    ctx.conn.execute(f"DELETE FROM {table} {where}", (fam,) if per_family else ())
    for i, r in enumerate(rows, 1):
        if per_family:
            r["family_id"] = fam
        if has_seq:
            r["seq"] = i
        cols = list(r)
        ctx.conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('%(' + c + ')s' for c in cols)})", r)
    return []
