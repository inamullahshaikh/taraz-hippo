"""Files made from a BOM version: Excel, Google Sheet, PDF, JLCPCB and PCBWay assembly BOMs."""
import uuid
from pathlib import Path

from fastapi import HTTPException

from ..db import setting
from ..integrations import drive
from ..settings import S
from . import boms

COLUMNS = ["Seq", "Component", "Code", "MPN / description", "Manufacturer", "Qty", "UoM", "Ref des", "Note", "CMP/OTS", "Cost"]


def _rows(ctx, v, q="", line_ids=None):
    out = []
    for l in boms.lines(ctx, v["id"]):
        row = [l["seq"], l["part_code"] or l["child_code"], l["part_code"] or "", l["mpn"] or l["child_description"] or "",
               l["manufacturer"] or "", float(l["qty"]), l["uom"], l["ref_des"], l["note"],
               "Sub-assembly" if l["child_item_id"] else ("CMP" if l["is_cmp"] else "OTS"),
               float(l["cost"]) if l["cost"] is not None else None]
        if line_ids and l["id"] not in line_ids:
            continue
        if q and q.lower() not in " ".join(str(c) for c in row).lower():
            continue
        out.append((l, row))
    return out


def _store(name, write):
    token = uuid.uuid4().hex
    path = Path(S.DATA_DIR, "exports", token, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    write(path)
    return {"url": f"/api/v1/exports/files/{token}/{name}", "name": name}


def _xlsx(rows, header):
    from openpyxl import Workbook

    def write(path):
        wb = Workbook()
        ws = wb.active
        ws.append(header)
        for r in rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        wb.save(path)
    return write


def _pdf(ctx, v, rows):
    from fpdf import FPDF
    approvals = ctx.all("""SELECT a.decision, a.comment, a.decided_at, u.name FROM approvals a JOIN users u ON u.id = a.reviewer_id
                           WHERE a.bom_version_id = %s ORDER BY a.decided_at""", v["id"])
    people = {r["id"]: r["name"] for r in ctx.all("SELECT id, name FROM users WHERE id = ANY(%s)",
                                                  [x for x in (v["created_by"], v["approved_by"], v["released_by"]) if x])}
    t = lambda s: str("" if s is None else s).encode("latin-1", "replace").decode("latin-1")

    def write(path):
        pdf = FPDF(orientation="L", format="A4")
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 8, t(f"{v['item_code']}  ·  {v['option_code']}  ·  v{v['version_no']}  ({v['state']})"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, t(f"Made by {people.get(v['created_by'], '-')}   Approved by {people.get(v['approved_by'], '-')} "
                         f"{v['approved_at'] or ''}   Released by {people.get(v['released_by'], '-')} {v['released_at'] or ''}"),
                 new_x="LMARGIN", new_y="NEXT")
        for a in approvals:
            pdf.cell(0, 5, t(f"{a['decided_at']:%Y-%m-%d} {a['name']}: {a['decision']} {a['comment']}"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        widths = [10, 45, 20, 70, 30, 14, 70]
        pdf.set_font("Helvetica", "B", 8)
        for w, h in zip(widths, ["#", "Component", "Code", "MPN / description", "Manufacturer", "Qty", "Ref des"]):
            pdf.cell(w, 6, h, border=1)
        pdf.ln()
        pdf.set_font("Helvetica", "", 7)
        for _, r in rows:
            for w, c in zip(widths, [r[0], r[1], r[2], r[3], r[4], f"{r[5]:g}", r[7]]):
                pdf.cell(w, 5, t(c)[: int(w / 1.6)], border=1)
            pdf.ln()
        pdf.output(str(path))
    return write


def make(ctx, version_id, fmt, q="", line_ids=None, folder_id=None):
    v = boms.version(ctx, version_id)
    if fmt != "pdf":
        ctx.require("export.create", v["family_id"])
    rows = _rows(ctx, v, q, line_ids)
    base = f"{v['item_code']}_{v['option_code']}_v{v['version_no']}"
    if fmt == "xlsx":
        return _store(base + ".xlsx", _xlsx([r for _, r in rows], COLUMNS))
    if fmt == "pdf":
        return _store(base + ".pdf", _pdf(ctx, v, rows))
    parts = [(l, r) for l, r in rows if l["part_id"]]
    if fmt == "jlc":
        return _store(base + "_JLCPCB.xlsx", _xlsx(
            [[l["mpn"], l["ref_des"].replace(" ", ","), "", ""] for l, _ in parts],
            ["Comment", "Designator", "Footprint", "JLCPCB Part #"]))
    if fmt == "pcbway":
        return _store(base + "_PCBWay.xlsx", _xlsx(
            [[n, l["ref_des"].replace(" ", ","), float(l["qty"]), l["manufacturer"], l["mpn"], l["description"], "", ""]
             for n, (l, _) in enumerate(parts, 1)],
            ["Item #", "Designator", "Qty", "Manufacturer", "Mfg Part #", "Description / Value", "Package/Footprint", "Type"]))
    if fmt == "gsheet":
        if not setting(ctx.conn, "drive_enabled"):
            raise HTTPException(409, "Google Drive is switched off on the Admin page")
        folder = folder_id or ctx.one("SELECT drive_folder_id FROM families WHERE id = %s", v["family_id"])["drive_folder_id"]
        try:
            url = drive.create_sheet(base, folder, [COLUMNS] + [r for _, r in rows])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Google Drive: {e}")
        return {"url": url, "name": base}
    raise HTTPException(422, "Format must be xlsx, gsheet, pdf, jlc or pcbway")
