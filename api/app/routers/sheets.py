"""The BOMTree page still speaks Google Sheets (get_values / update_values / append_values on A1 ranges).
These endpoints answer those calls from Postgres, so the page runs unchanged on top of the database.

Each tab is rebuilt from its table with the sheet's exact header and column order (the page reads columns by
position). Writes are applied to the tables by app/sheetwrite.py; "Where used" is worked out by the database itself.
"""
import datetime as dt
import re
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from psycopg.rows import tuple_row
from pydantic import BaseModel

from ..auth import CTX
from ..settings import S

router = APIRouter(prefix="/sheets")
TZ = ZoneInfo(S.SHEET_TZ)

# tab -> (header, SQL giving the rows in header order). %(fam)s = family id, %(uid)s = the viewer.
INDEX = {
    "Families": (["Family", "Status", "Old tools", "Notes", "Sheet ID", "Folder", "Owner"], """
        SELECT f.code, f.status, f.old_tools, f.notes, f.legacy_sheet_id, f.drive_folder_url, u.email
        FROM families f LEFT JOIN users u ON u.id = f.owner_id WHERE f.archived_at IS NULL ORDER BY f.id"""),
    "Items": (["Item", "Prefix", "Family", "Level", "Status", "Built by", "What it is", "Today in Odoo", "Odoo code",
               "Notes", "Brain diagram", "Block (override)"], """
        SELECT i.code, i.prefix, f.code, i.level, i.status, i.built_by, i.description, i.odoo_today, i.odoo_code,
               i.notes, i.brain_url, b.name
        FROM items i JOIN families f ON f.id = i.family_id LEFT JOIN blocks b ON b.id = i.block_override_id
        WHERE i.deleted_at IS NULL ORDER BY i.id"""),
    "Conversions": (["Old tool", "Old item", "Old code", "Change", "New item", "Note"], """
        SELECT c.old_tool, c.old_item, c.old_code, c.change, coalesce(i.code, c.new_item_text), c.note
        FROM conversions c LEFT JOIN items i ON i.id = c.new_item_id ORDER BY c.seq"""),
    "Where used": (["Component", "Code", "Family"], """
        SELECT DISTINCT component, code, family FROM v_where_used ORDER BY family, component, code"""),
    "Log": (["When", "Who", "Action", "Item", "Detail"], """
        SELECT a.at, coalesce(a.actor_label, u.name), a.action, a.entity_label, a.detail
        FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
        WHERE a.entity_type = 'sheet log' ORDER BY a.at, a.id"""),
    "Library": (["Item", "Code", "Width mm", "Height mm", "Front picture", "Back picture", "Ports", "Source",
                 "Updated", "Notes"], """
        SELECT item_text, code, width_mm, height_mm, front_picture, back_picture, ports::text, source, updated_at, notes
        FROM board_library ORDER BY id"""),
    "Canvas": (["Item", "Pages", "Data", "Updated", "By", "Notes"], """
        SELECT item_text, pages, data::text, updated_at, updated_by, notes FROM canvases ORDER BY id"""),
    "Tools": (["Tool", "Archived", "By", "When", "Note"], """
        SELECT tool, CASE WHEN archived THEN 'Yes' ELSE 'No' END, changed_by, changed_at, note
        FROM old_tools ORDER BY changed_at NULLS FIRST, tool"""),
    "Blocks": (["Block", "Show order", "Level", "What it is (for new joiners)", "Name rule (items whose name matches)",
                "Match order"], """
        SELECT name, show_order, level, description, name_rule, match_order FROM blocks ORDER BY id"""),
    "Alternates": (["Group", "Base part", "Base code", "Mfr Part #", "Mfr", "DigiKey #", "Description", "Kind", "Status",
                    "Odoo code", "Search", "Approved by", "Approved on", "Note", "URL", "Product"], """
        SELECT group_name, base_part, base_code, mfr_pn, mfr, digikey_pn, description, kind, status, odoo_code, search,
               approved_by, approved_on, note, url, product FROM alternates ORDER BY seq"""),
    "Alternate searches": (["Search", "Group", "Base part", "Base code", "Kind", "Date", "By", "Rows in CSV", "Approved",
                            "File", "DigiKey filter link", "Note"], """
        SELECT search, group_name, base_part, base_code, kind, searched_on, searched_by, rows_in_csv, approved, file,
               digikey_link, note FROM alternate_searches ORDER BY seq"""),
    "Design blocks": (["Block type", "Function", "Products", "Boards", "Copies", "Lines", "Critical", "Sensitive",
                       "Normal", "Fingerprint", "Status", "Confirmed by", "Confirmed on"], """
        SELECT block_type, function, products, boards, copies, lines, critical, sensitive, normal, fingerprint, status,
               confirmed_by, confirmed_on FROM design_blocks ORDER BY seq"""),
    "Design issues": (["Board", "Ref des", "Issue", "Source", "Status"], """
        SELECT board, ref_des, issue, source, status FROM design_issues ORDER BY seq"""),
}

# A BOM the viewer may see: released (or superseded), or any state where they may read drafts.
VISIBLE = """(v.state IN ('Released', 'Superseded') OR has_perm(%(uid)s, 'bom.read_draft', i.family_id))"""
BOM_KEY = "coalesce(b.legacy_key, i.code || CASE WHEN b.option_code <> 'Standard' THEN ' · ' || b.option_code ELSE '' END)"

FAMILY = {
    "BOMs": (["BOM", "Item", "Option", "Default", "When to use", "Status", "Notes"], f"""
        SELECT {BOM_KEY}, i.code, b.option_code, CASE WHEN b.is_default THEN 'Yes' ELSE 'No' END, b.when_to_use,
               CASE WHEN b.status = 'Under review' AND v.state = 'Draft' AND v.migrated THEN 'Needs info' ELSE b.status END,
               b.notes
        FROM boms b JOIN items i ON i.id = b.item_id JOIN bom_versions v ON v.id = b.current_version_id
        WHERE i.family_id = %(fam)s AND b.deleted_at IS NULL AND {VISIBLE} ORDER BY b.id"""),
    "Lines": (["BOM", "Component", "Code", "Qty", "Ref des", "Note"], f"""
        SELECT {BOM_KEY}, l.component_label, coalesce(p.code, ''), l.qty, l.ref_des, l.note
        FROM boms b JOIN items i ON i.id = b.item_id JOIN bom_versions v ON v.id = b.current_version_id
        JOIN bom_lines l ON l.bom_version_id = v.id LEFT JOIN parts p ON p.id = l.part_id
        WHERE i.family_id = %(fam)s AND b.deleted_at IS NULL AND {VISIBLE} ORDER BY b.id, l.seq"""),
    "Old lines": (["Old item", "Old code", "Old tool", "Component", "Code", "Qty", "Ref des", "Note"], """
        SELECT old_item, old_code, old_tool, component, part_code, qty, ref_des, note
        FROM legacy_lines WHERE family_id = %(fam)s ORDER BY seq"""),
    "PNP": (["Board", "Ref des", "Name", "Package", "X (mm)", "Y (mm)", "Side", "Rotation", "Value", "Source", "Loaded"], """
        SELECT board, ref_des, name, package, x_mm, y_mm, side, rotation, value, source, loaded_at
        FROM placements WHERE family_id = %(fam)s ORDER BY seq"""),
    "Design": (["Board", "Ref des", "Block path", "Copy", "Level", "Role", "Why", "Design MPN", "Design code",
                "Confirmed by", "Confirmed on"], """
        SELECT board, ref_des, block_path, copy_label, level, role, why, design_mpn, design_code, confirmed_by, confirmed_on
        FROM design_refs WHERE family_id = %(fam)s ORDER BY seq"""),
    "Design nets": (["Board", "Net", "Kind", "Sheet", "Ends"], """
        SELECT board, net, kind, sheet, ends FROM design_nets WHERE family_id = %(fam)s ORDER BY seq"""),
}

# Blueprints: the page only reads the first column (our custom part codes).
BLUEPRINTS = {"Parts": (["Part #"], "SELECT code FROM parts WHERE is_cmp ORDER BY code")}


def cell(v):
    """A database value as the sheet shows it."""
    if v is None:
        return ""
    if isinstance(v, Decimal):
        s = format(v.normalize(), "f")
        return s.rstrip("0").rstrip(".") if "." in s else s
    if isinstance(v, dt.datetime):
        v = v.astimezone(TZ)          # a sheet date typed without a time was stored as local midnight
        return v.strftime("%Y-%m-%d" if (v.hour, v.minute, v.second) == (0, 0, 0) else "%Y-%m-%d %H:%M")
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v)


A1 = re.compile(r"^(?:'((?:[^']|'')+)'|([^!]+))!([A-Z]+)?(\d+)?(?::([A-Z]+)?(\d+)?)?$")


def col_no(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def parse_range(rng):
    """'Items'!A:L -> ("Items", first col, last col, first row, last row); None = open-ended."""
    m = A1.match(rng.strip())
    if not m:
        raise HTTPException(400, {"code": "bad_range", "message": f"Cannot read the range {rng}"})
    tab = (m.group(1) or "").replace("''", "'") or m.group(2).strip()
    c1, r1, c2, r2 = m.group(3), m.group(4), m.group(5), m.group(6)
    if m.group(5) is None and m.group(6) is None and ":" not in rng:      # a single cell like 'BOMs'!F12
        c2, r2 = c1, r1
    return (tab, col_no(c1) if c1 else 1, col_no(c2) if c2 else None,
            int(r1) if r1 else 1, int(r2) if r2 else None)


def tabs_for(conn, sheet_id):
    """Which tabs this spreadsheet id holds, and the family it belongs to (None for the Index or Blueprints)."""
    if sheet_id == S.INDEX_SHEET_ID:
        return INDEX, None
    if sheet_id == S.BLUEPRINTS_SHEET_ID:
        return BLUEPRINTS, None
    row = conn.execute("SELECT id FROM families WHERE legacy_sheet_id = %s AND archived_at IS NULL", (sheet_id,)).fetchone()
    if not row:
        raise HTTPException(404, {"code": "not_found", "message": f"No BOMTree sheet {sheet_id}"})
    return FAMILY, row["id"]


def read_tab(conn, sheet_id, tab, uid):
    tabs, fam = tabs_for(conn, sheet_id)
    if tab not in tabs:
        raise HTTPException(404, {"code": "not_found", "message": f"No tab '{tab}'"})
    header, sql = tabs[tab]
    rows = conn.cursor(row_factory=tuple_row).execute(sql, {"fam": fam, "uid": uid}).fetchall()
    return [header] + [[cell(v) for v in r] for r in rows]


def trim(row):
    """Like the Sheets API: trailing empty cells are left out."""
    while row and row[-1] == "":
        row = row[:-1]
    return row


class Get(BaseModel):
    spreadsheetId: str
    range: str


class Put(Get):
    values: list = []
    base: list | None = None       # the rows the change was made on, for changes queued in the page's own store


@router.post("/get_values")
def get_values(body: Get, ctx=CTX):
    ctx.require_read()
    tab, c1, c2, r1, r2 = parse_range(body.range)
    grid = read_tab(ctx.conn, body.spreadsheetId, tab, ctx.uid)
    out = [trim(row[c1 - 1:c2]) for row in grid[r1 - 1:r2]]
    while out and not out[-1]:
        out.pop()
    return {"range": body.range, "values": out}


@router.post("/update_values")
def update_values(body: Put, ctx=CTX):
    return write_values(body, ctx, "update")


@router.post("/append_values")
def append_values(body: Put, ctx=CTX):
    return write_values(body, ctx, "append")


def write_values(body, ctx, mode):
    from ..sheetwrite import write          # it builds on this module
    ctx.require_read()
    conflicts = write(ctx, body.spreadsheetId, body.range, body.values, mode, body.base)
    return {"updatedRange": body.range, "conflicts": conflicts}
