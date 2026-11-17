"""DipTrace BOM import: read the file, match lines to parts, ask about unclear ones, preview, save as a draft."""
import csv
import io
import re
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from ..db import Jsonb
from . import boms

HEADERS = {
    "ref_des": ("refdes", "ref des", "designator", "designators", "reference", "references"),
    "value": ("value", "comment"),
    "name": ("name", "part name", "mpn", "manufacturer part number", "mfr. part #", "mfr part number", "part number", "part"),
    "pattern": ("pattern", "footprint", "package"),
    "qty": ("quantity", "qty", "count"),
    "manufacturer": ("manufacturer", "mfr", "mfr."),
    "code": ("code", "odoo code", "internal code", "p#"),
}
CODE = re.compile(r"\bP\d{6}\b", re.I)


def parse(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        raise HTTPException(422, "The file is empty")
    head = [h.strip().lower() for h in rows[0]]
    col = {k: next((i for i, h in enumerate(head) if h in names), None) for k, names in HEADERS.items()}
    if col["ref_des"] is None and col["name"] is None and col["value"] is None:
        raise HTTPException(422, "No RefDes, Name or Value column found. Export the BOM from DipTrace as CSV with a header row.")
    out = []
    for r in rows[1:]:
        get = lambda k: r[col[k]].strip() if col[k] is not None and col[k] < len(r) else ""
        refs = " ".join(re.split(r"[,;\s]+", get("ref_des"))).strip()
        try:
            qty = Decimal(get("qty").replace(",", ".")) if get("qty") else Decimal(len(refs.split()) or 1)
        except InvalidOperation:
            qty = Decimal(len(refs.split()) or 1)
        code = get("code") or next((m.group(0) for c in r for m in [CODE.search(c)] if m), "")
        out.append({"ref_des": refs, "value": get("value"), "name": get("name"), "pattern": get("pattern"),
                    "manufacturer": get("manufacturer"), "code": code.upper(), "qty": str(qty)})
    return out


def match(ctx, lines):
    for l in lines:
        hit = None
        if l["code"]:
            hit = ctx.one("SELECT id, code, mpn, description FROM parts WHERE code = %s", l["code"])
        for text in (l["name"], l["value"]):
            if not hit and text:
                hit = ctx.one("SELECT id, code, mpn, description FROM parts WHERE lower(mpn) = lower(%s) AND archived_at IS NULL "
                              "ORDER BY code IS NULL, id LIMIT 1", text)
        l["match"] = hit
        l["candidates"], l["question"] = [], None
        if not hit:
            words = [w for w in (l["name"], l["value"]) if w]
            l["candidates"] = ctx.all("""SELECT id, code, mpn, description FROM parts
                                         WHERE archived_at IS NULL AND (mpn ILIKE ANY(%s) OR description ILIKE ANY(%s))
                                         ORDER BY code IS NULL, length(mpn) LIMIT 5""",
                                      [f"%{w}%" for w in words] or ["-"], [f"%{w}%" for w in words] or ["-"])
            l["question"] = f"Which part is '{l['name'] or l['value']}' ({l['ref_des'] or 'no ref des'})?"
    return lines


def create(ctx, file_name, data, item_code, option_code):
    item = ctx.one("SELECT id, code, family_id FROM items WHERE upper(code) = upper(%s) AND archived_at IS NULL", item_code)
    if not item:
        raise HTTPException(404, "Item not found")
    ctx.require("import.run", item["family_id"])
    lines = match(ctx, parse(data))
    open_q = sum(1 for l in lines if not l["match"])
    row = ctx.one("""INSERT INTO imports (file_name, family_id, item_id, status, created_by, result)
                     VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""", file_name, item["family_id"], item["id"],
                  "Waiting answers" if open_q else "Matching", ctx.uid,
                  Jsonb({"option_code": option_code or "Standard", "lines": lines}))
    return get(ctx, row["id"])


def _load(ctx, import_id):
    imp = ctx.one("SELECT i.*, it.code AS item_code FROM imports i JOIN items it ON it.id = i.item_id WHERE i.id = %s", import_id)
    if not imp:
        raise HTTPException(404, "Import not found")
    ctx.require("import.run", imp["family_id"])
    return imp


def _bom(ctx, imp):
    return ctx.one("SELECT * FROM boms WHERE item_id = %s AND upper(option_code) = upper(%s) AND archived_at IS NULL",
                   imp["item_id"], imp["result"]["option_code"])


def _new_lines(ctx, lines):
    chosen = [l for l in lines if l.get("match")]
    parts = {p["id"]: p for p in ctx.all("SELECT id, code AS part_code, mpn, description FROM parts WHERE id = ANY(%s)",
                                         [l["match"]["id"] for l in chosen])}
    return [{"part_id": l["match"]["id"], "child_item_id": None, "qty": Decimal(l["qty"]), "ref_des": l["ref_des"],
             "note": "", "uom": "Units", **parts[l["match"]["id"]], "child_code": None, "child_description": None}
            for l in chosen]


def get(ctx, import_id):
    imp = _load(ctx, import_id)
    lines = imp["result"]["lines"]
    bom = _bom(ctx, imp)
    current = boms.lines(ctx, bom["current_version_id"]) if bom and bom["current_version_id"] else []
    return {"id": imp["id"], "file_name": imp["file_name"], "status": imp["status"], "item_code": imp["item_code"],
            "option_code": imp["result"]["option_code"], "lines": lines,
            "open_questions": sum(1 for l in lines if not l["match"] and not l.get("skip")),
            "preview": boms.diff(current, _new_lines(ctx, lines))}


def answer(ctx, import_id, answers):
    """answers: {line index: part id, or null to leave the line out}."""
    imp = _load(ctx, import_id)
    if imp["status"] in ("Saved", "Cancelled"):
        raise HTTPException(409, f"This import is {imp['status']}")
    lines = imp["result"]["lines"]
    for idx, part_id in answers.items():
        l = lines[int(idx)]
        if part_id is None:
            l["skip"], l["match"] = True, None
        else:
            l["match"], l["skip"] = ctx.one("SELECT id, code, mpn, description FROM parts WHERE id = %s", part_id), False
    still = sum(1 for l in lines if not l["match"] and not l.get("skip"))
    ctx.conn.execute("UPDATE imports SET result = %s, status = %s WHERE id = %s",
                     (Jsonb({**imp["result"], "lines": lines}), "Waiting answers" if still else "Matching", import_id))
    return get(ctx, import_id)


def save(ctx, import_id):
    imp = _load(ctx, import_id)
    view = get(ctx, import_id)
    if view["open_questions"]:
        raise HTTPException(422, f"{view['open_questions']} lines still need an answer")
    if imp["status"] == "Saved":
        raise HTTPException(409, "Already saved")
    ctx.require("bom.edit", imp["family_id"])
    bom = _bom(ctx, imp)
    if not bom:
        default = not ctx.one("SELECT 1 FROM boms WHERE item_id = %s AND archived_at IS NULL", imp["item_id"])
        bom = ctx.one("INSERT INTO boms (item_id, option_code, is_default) VALUES (%s, %s, %s) RETURNING *",
                      imp["item_id"], imp["result"]["option_code"], default)
    draft = ctx.one("SELECT id, lock_version FROM bom_versions WHERE bom_id = %s AND state = 'Draft'", bom["id"])
    if not draft:
        if ctx.one("SELECT 1 FROM bom_versions WHERE bom_id = %s AND state IN ('In review', 'Approved')", bom["id"]):
            raise HTTPException(409, "This BOM has a version in review. Finish that first.")
        d = boms.new_draft(ctx, bom["id"], note=f"DipTrace import {imp['file_name']}")
        draft = {"id": d["id"], "lock_version": 0}
    new = [{"part_id": l["part_id"], "qty": l["qty"], "ref_des": l["ref_des"]} for l in _new_lines(ctx, imp["result"]["lines"])]
    ctx.conn.execute("SELECT set_config('app.source', 'Import', true)")
    boms.save_lines(ctx, draft["id"], draft["lock_version"], new)
    ctx.conn.execute("UPDATE imports SET status = 'Saved' WHERE id = %s", (import_id,))
    v = ctx.one("SELECT version_no FROM bom_versions WHERE id = %s", draft["id"])
    return {"version_id": draft["id"], "version_no": v["version_no"], "option_code": bom["option_code"]}
