#!/usr/bin/env python3
"""
BOMTree: move the Google Sheets into PostgreSQL.

Three commands:

  1. export   Read every BOMTree Google Sheet (the Index, every family sheet and
              the Blueprints part list) and save an exact copy in a folder.
                python bomtree_migrate.py export --key service-account.json --out snapshot

  2. load     Load that copy into PostgreSQL in ONE transaction, then check every
              row against the sheets. By default it is a dry run: it shows the
              report and saves nothing. Add --commit to keep the data; it is only
              kept when the check finds 0 errors.
                python bomtree_migrate.py load --snapshot snapshot --db "postgresql://..."
                python bomtree_migrate.py load --snapshot snapshot --db "postgresql://..." --commit

              Before cut-over you can repeat the load on top of an earlier one
              with --replace (it wipes and reloads the BOM data; users, roles
              and families are kept).

  3. check    Compare a database that was already loaded with a snapshot.
                python bomtree_migrate.py check --snapshot snapshot --db "postgresql://..."

The database address can also be given in the BOMTREE_DB environment variable.
Needs Python 3.10+ and:  pip install "psycopg[binary]" google-auth requests
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

# The sheets' IDs come from the environment (the same variables as the app), so they stay out of the code.
INDEX_ID = os.environ.get("INDEX_SHEET_ID", "")                 # BOMTree – Index
BLUEPRINTS_ID = os.environ.get("BLUEPRINTS_SHEET_ID", "")       # Blueprints (custom parts list)
DEFAULT_TZ = "Europe/Istanbul"                                  # time zone of the Index sheet

# ---------------------------------------------------------------------------
# Which sheet column goes into which database column.
# Columns are found by their header text, so moving a column in the sheet is fine.
# A tab marked optional may be missing; a required one must be there.
# ---------------------------------------------------------------------------
INDEX_TABS = {
    "Families": {"required": True, "cols": {
        "code": "Family", "status": "Status", "old_tools": "Old tools", "notes": "Notes",
        "sheet_id": "Sheet ID", "folder": "Folder", "owner": "Owner"}},
    "Items": {"required": True, "cols": {
        "code": "Item", "prefix": "Prefix", "family": "Family", "level": "Level", "status": "Status",
        "built_by": "Built by", "description": "What it is", "odoo_today": "Today in Odoo",
        "odoo_code": "Odoo code", "notes": "Notes", "brain_url": "Brain diagram",
        "block_override": "Block (override)"}},
    "Conversions": {"required": True, "cols": {
        "old_tool": "Old tool", "old_item": "Old item", "old_code": "Old code", "change": "Change",
        "new_item": "New item", "note": "Note"}},
    "Where used": {"required": False, "cols": {"component": "Component", "code": "Code", "family": "Family"}},
    "Log": {"required": True, "cols": {"when": "When", "who": "Who", "action": "Action", "item": "Item",
                                       "detail": "Detail"}},
    "Blocks": {"required": False, "cols": {
        "name": "Block", "show_order": "Show order", "level": "Level",
        "description": "What it is (for new joiners)", "name_rule": "Name rule (items whose name matches)",
        "match_order": "Match order"}},
    "Library": {"required": False, "cols": {
        "item": "Item", "code": "Code", "width": "Width mm", "height": "Height mm",
        "front": "Front picture", "back": "Back picture", "ports": "Ports", "source": "Source",
        "updated": "Updated", "notes": "Notes"}},
    "Canvas": {"required": False, "cols": {"item": "Item", "pages": "Pages", "data": "Data",
                                           "updated": "Updated", "by": "By", "notes": "Notes"}},
    "Tools": {"required": False, "cols": {"tool": "Tool", "archived": "Archived", "by": "By",
                                          "when": "When", "note": "Note"}},
    "Alternates": {"required": False, "cols": {
        "group": "Group", "base_part": "Base part", "base_code": "Base code", "mfr_pn": "Mfr Part #",
        "mfr": "Mfr", "digikey": "DigiKey #", "description": "Description", "kind": "Kind",
        "status": "Status", "odoo_code": "Odoo code", "search": "Search", "approved_by": "Approved by",
        "approved_on": "Approved on", "note": "Note", "url": "URL", "product": "Product"}},
    "Alternate searches": {"required": False, "cols": {
        "search": "Search", "group": "Group", "base_part": "Base part", "base_code": "Base code",
        "kind": "Kind", "date": "Date", "by": "By", "rows": "Rows in CSV", "approved": "Approved",
        "file": "File", "link": "DigiKey filter link", "note": "Note"}},
    "Design blocks": {"required": False, "cols": {
        "block_type": "Block type", "function": "Function", "products": "Products", "boards": "Boards",
        "copies": "Copies", "lines": "Lines", "critical": "Critical", "sensitive": "Sensitive",
        "normal": "Normal", "fingerprint": "Fingerprint", "status": "Status",
        "confirmed_by": "Confirmed by", "confirmed_on": "Confirmed on"}},
    "Design issues": {"required": False, "cols": {"board": "Board", "ref_des": "Ref des", "issue": "Issue",
                                                  "source": "Source", "status": "Status"}},
}
FAMILY_TABS = {
    "BOMs": {"required": True, "cols": {"bom": "BOM", "item": "Item", "option": "Option",
                                        "default": "Default", "when": "When to use", "status": "Status",
                                        "notes": "Notes"}},
    "Lines": {"required": True, "cols": {"bom": "BOM", "component": "Component", "code": "Code",
                                         "qty": "Qty", "ref_des": "Ref des", "note": "Note"}},
    "Old lines": {"required": True, "cols": {"old_item": "Old item", "old_code": "Old code",
                                             "old_tool": "Old tool", "component": "Component",
                                             "code": "Code", "qty": "Qty", "ref_des": "Ref des",
                                             "note": "Note"}},
    "PNP": {"required": False, "cols": {"board": "Board", "ref_des": "Ref des", "name": "Name",
                                        "package": "Package", "x": "X (mm)", "y": "Y (mm)", "side": "Side",
                                        "rotation": "Rotation", "value": "Value", "source": "Source",
                                        "loaded": "Loaded"}},
    "Design": {"required": False, "cols": {"board": "Board", "ref_des": "Ref des", "block_path": "Block path",
                                           "copy": "Copy", "level": "Level", "role": "Role", "why": "Why",
                                           "design_mpn": "Design MPN", "design_code": "Design code",
                                           "confirmed_by": "Confirmed by", "confirmed_on": "Confirmed on"}},
    "Design nets": {"required": False, "cols": {"board": "Board", "net": "Net", "kind": "Kind",
                                                "sheet": "Sheet", "ends": "Ends"}},
}

ODOO_CODE = re.compile(r"^P\d{6}$")


# ---------------------------------------------------------------------------
# Report: every problem is an error (blocks --commit) or a warning (does not)
# ---------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.counts: dict[str, int] = {}
        self.notes: list[str] = []

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"{where}: {msg}")

    def text(self, title):
        out = [f"# {title}", "", f"Errors: {len(self.errors)}    Warnings: {len(self.warnings)}", ""]
        if self.counts:
            out += ["## Rows loaded", "", "| Table | Rows |", "| --- | ---: |"]
            out += [f"| {k} | {v} |" for k, v in self.counts.items()] + [""]
        if self.notes:
            out += ["## Notes", ""] + [f"- {n}" for n in self.notes] + [""]
        if self.errors:
            out += ["## Errors (must be fixed in the sheets before --commit)", ""]
            out += [f"- {e}" for e in self.errors[:500]]
            if len(self.errors) > 500:
                out.append(f"- ... and {len(self.errors) - 500} more")
            out.append("")
        if self.warnings:
            out += ["## Warnings (data is loaded; worth a look)", ""]
            out += [f"- {w}" for w in self.warnings[:500]]
            if len(self.warnings) > 500:
                out.append(f"- ... and {len(self.warnings) - 500} more")
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Small helpers for reading cells
# ---------------------------------------------------------------------------
def clean(v) -> str:
    return "" if v is None else str(v).replace(" ", " ").strip()


def or_none(v):
    v = clean(v)
    return v or None


def to_number(v, where, rep, field, required=False, positive=False):
    s = clean(v)
    if not s:
        if required:
            rep.err(where, f"{field} is empty")
        return None
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):          # 1,234.5
        s = s.replace(",", "")
    try:
        n = Decimal(s)
    except InvalidOperation:
        rep.err(where, f"{field} '{v}' is not a number")
        return None
    if positive and n <= 0:
        rep.err(where, f"{field} must be above 0 (is {s})")
        return None
    return n


def to_int(v, where, rep, field):
    n = to_number(v, where, rep, field)
    if n is None:
        return None
    if n != n.to_integral_value():
        rep.err(where, f"{field} '{v}' is not a whole number")
        return None
    return int(n)


def to_time(v, where, rep, field, tz):
    s = clean(v).lstrip("'")
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    rep.err(where, f"{field} '{v}' is not a date (use YYYY-MM-DD or YYYY-MM-DD HH:MM)")
    return None


def to_date(v, where, rep, field, tz):
    t = to_time(v, where, rep, field, tz)
    return t.date() if t else None


def yes(v) -> bool:
    return clean(v).lower() in ("yes", "y", "true", "1", "x", "✓")


def to_json(v, where, rep, field, default):
    s = clean(v)
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        rep.err(where, f"{field} is not valid JSON ({e.msg})")
        return default


# ---------------------------------------------------------------------------
# Snapshot: an exact copy of the sheets on disk
# ---------------------------------------------------------------------------
class Tab:
    """One sheet tab: header row plus data rows, read by header name."""

    def __init__(self, sheet_title, name, rows, spec, rep, required):
        self.name, self.where = name, f"{sheet_title} › {name}"
        self.rows: list[tuple[int, dict]] = []
        self.present = bool(rows)
        if not rows:
            if required:
                rep.err(self.where, "tab is missing or empty (no header row)")
            return
        header = [clean(h) for h in rows[0]]
        pos = {}
        for key, title in spec.items():
            if title in header:
                pos[key] = header.index(title)
            else:
                rep.err(self.where, f"column '{title}' not found in the header row")
        extra = [h for h in header if h and h not in spec.values()]
        if extra:
            rep.warn(self.where, f"column(s) not migrated: {', '.join(extra)}")
        for n, raw in enumerate(rows[1:], start=2):            # n = row number as seen in the sheet
            if not any(clean(c) for c in raw):
                continue
            rec = {k: (raw[i] if i < len(raw) else "") for k, i in pos.items()}
            self.rows.append((n, rec))

    def at(self, n):
        return f"{self.where} row {n}"


def load_snapshot(folder: Path, rep: Report):
    files = {}
    for f in sorted(folder.glob("*.json")):
        if f.name == "manifest.json":
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        files[d["spreadsheetId"]] = d
    if not INDEX_ID:
        raise SystemExit("Set INDEX_SHEET_ID (and BLUEPRINTS_SHEET_ID) to the sheets' IDs first.")
    if INDEX_ID not in files:
        raise SystemExit(f"The snapshot has no Index sheet ({INDEX_ID}.json). Run export first.")
    manifest = folder / "manifest.json"
    meta = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {"folder": str(folder)}
    return files, meta


# ---------------------------------------------------------------------------
# 1. EXPORT: Google Sheets -> snapshot folder
# ---------------------------------------------------------------------------
def cmd_export(args):
    try:
        import requests
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError:
        raise SystemExit('Missing libraries. Run:  pip install google-auth requests "psycopg[binary]"')

    creds = service_account.Credentials.from_service_account_file(
        args.key, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    creds.refresh(Request())
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {creds.token}"
    base = "https://sheets.googleapis.com/v4/spreadsheets"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def fetch(sheet_id, only_tabs=None):
        r = s.get(f"{base}/{sheet_id}", params={"fields": "properties(title,timeZone),sheets.properties.title"})
        if r.status_code == 403:
            raise SystemExit(f"No access to sheet {sheet_id}. Share it (Viewer) with {creds.service_account_email}")
        r.raise_for_status()
        meta = r.json()
        titles = [x["properties"]["title"] for x in meta["sheets"]]
        if only_tabs:
            titles = [t for t in titles if t in only_tabs]
        ranges = ["'" + t.replace("'", "''") + "'" for t in titles]
        tabs = {}
        for i in range(0, len(ranges), 50):
            r = s.get(f"{base}/{sheet_id}/values:batchGet",
                      params=[("ranges", x) for x in ranges[i:i + 50]] +
                             [("valueRenderOption", "FORMATTED_VALUE"), ("majorDimension", "ROWS")])
            r.raise_for_status()
            for t, vr in zip(titles[i:i + 50], r.json().get("valueRanges", [])):
                tabs[t] = vr.get("values", [])
        doc = {"spreadsheetId": sheet_id, "title": meta["properties"]["title"],
               "timeZone": meta["properties"].get("timeZone"), "tabs": tabs}
        (out / f"{sheet_id}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        print(f"  {doc['title']}: " + ", ".join(f"{t} {max(len(v) - 1, 0)}" for t, v in tabs.items()))
        return doc

    print("Reading the BOMTree sheets ...")
    index = fetch(args.index)
    files = [{"id": args.index, "title": index["title"]}]
    fam_rows = index["tabs"].get("Families", [])
    if not fam_rows:
        raise SystemExit("The Index sheet has no Families tab.")
    hdr = fam_rows[0]
    c_fam, c_sid = hdr.index("Family"), hdr.index("Sheet ID")
    for row in fam_rows[1:]:
        sid = clean(row[c_sid]) if c_sid < len(row) else ""
        if sid:
            d = fetch(sid)
            files.append({"id": sid, "title": d["title"], "family": clean(row[c_fam])})
    if args.blueprints:
        d = fetch(args.blueprints, only_tabs={"Parts"})
        files.append({"id": args.blueprints, "title": d["title"]})
    (out / "manifest.json").write_text(json.dumps({
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "index_id": args.index, "files": files}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(files)} sheets to {out}/")


# ---------------------------------------------------------------------------
# 2. LOAD: snapshot -> PostgreSQL (one transaction)
# ---------------------------------------------------------------------------
class Loader:
    def __init__(self, conn, files, meta, rep: Report, tz):
        self.c, self.files, self.meta, self.rep, self.tz = conn, files, meta, rep, tz
        self.cur = conn.cursor()
        self.family_id: dict[str, int] = {}
        self.family_sheet: dict[str, str] = {}
        self.item_id: dict[str, int] = {}
        self.item_family: dict[str, str] = {}
        self.part_by_code: dict[str, int] = {}
        self.part_by_name: dict[str, int] = {}
        self.block_id: dict[str, int] = {}
        self.cmp_codes: set[str] = set()
        self.bom_keys: dict[str, str] = {}
        self.sheet_lines: dict[str, list[dict]] = defaultdict(list)   # bom key -> lines as in the sheet

    # -- helpers -------------------------------------------------------------
    def q(self, sql, params=None):
        self.cur.execute(sql, params)
        return self.cur

    def one(self, sql, params=None):
        return self.q(sql, params).fetchone()[0]

    def index_tab(self, name):
        spec = INDEX_TABS[name]
        d = self.files[INDEX_ID]
        return Tab(d.get("title", "Index"), name, d["tabs"].get(name, []), spec["cols"], self.rep, spec["required"])

    def family_tab(self, fam, name):
        spec = FAMILY_TABS[name]
        d = self.files[self.family_sheet[fam]]
        return Tab(d.get("title", fam), name, d["tabs"].get(name, []), spec["cols"], self.rep, spec["required"])

    # -- steps ---------------------------------------------------------------
    def prepare(self, replace):
        self.q("SET LOCAL search_path = bomtree, hippo, public")
        self.q("SET LOCAL lock_timeout = '10s'")
        self.q("SELECT set_config('bomtree.migration', 'on', true)")
        self.q("SELECT set_config('bomtree.source', 'Migration', true)")
        self.migration_user = self.one("SELECT id FROM users WHERE email = 'migration@bomtree.local'")
        has_data = self.one("SELECT EXISTS (SELECT 1 FROM items)")
        if has_data and not replace:
            raise SystemExit("The database already has BOM data. Use --replace to wipe and reload it "
                             "(only before cut-over), or load into an empty database.")
        if has_data and replace:
            # any change not made by the migration (new versions, edits to migrated drafts, approvals,
            # comments ...) is real work that the wipe below would delete
            if self.one("""SELECT (SELECT count(*) FROM bom_versions WHERE NOT migrated)
                                + (SELECT count(*) FROM audit_log WHERE source <> 'Migration'
                                     AND entity_type NOT IN ('users', 'user_roles', 'families'))
                                + (SELECT count(*) FROM comments) + (SELECT count(*) FROM imports)
                                + (SELECT count(*) FROM odoo_jobs)""") > 0:
                raise SystemExit("The database has work done in the app after the migration (new versions, "
                                 "edits, approvals or comments). --replace would delete that work, so it is refused.")
            self.q("""TRUNCATE bom_lines, approvals, odoo_jobs, bom_versions, change_requests, boms,
                      conversions, board_library, canvases, imports, comments, attachments, items, parts,
                      blocks, legacy_lines, old_tools, placements, design_refs, design_nets,
                      design_blocks, design_issues, alternates, alternate_searches RESTART IDENTITY""")
            # remove the rows an earlier migration wrote; app history is kept
            self.q("ALTER TABLE audit_log DISABLE TRIGGER append_only")
            self.q("DELETE FROM audit_log WHERE source = 'Migration'")
            self.q("ALTER TABLE audit_log ENABLE TRIGGER append_only")
            self.rep.notes.append("--replace: earlier migrated BOM data was wiped and reloaded "
                                  "(users, roles and families kept).")

    def load_families(self):
        t = self.index_tab("Families")
        seen = set()
        for n, r in t.rows:
            code = clean(r["code"])
            if not code:
                self.rep.err(t.at(n), "Family is empty")
                continue
            if code in seen:
                self.rep.err(t.at(n), f"family {code} is listed twice")
                continue
            seen.add(code)
            sid = clean(r["sheet_id"])
            if not sid:
                self.rep.warn(t.at(n), f"family {code} has no Sheet ID: it has no BOMs to load")
            elif sid not in self.files:
                self.rep.err(t.at(n), f"sheet {sid} of family {code} is not in the snapshot")
            owner = None
            if clean(r["owner"]):
                row = self.q("SELECT id FROM users WHERE email = %s OR name = %s", (clean(r["owner"]),) * 2).fetchone()
                owner = row[0] if row else None
                if not owner:
                    self.rep.warn(t.at(n), f"owner '{clean(r['owner'])}' is not a user yet; add the user, then set the owner")
            fid = self.one("""
                INSERT INTO families (code, name, status, old_tools, notes, drive_folder_url, legacy_sheet_id, owner_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET status = excluded.status, old_tools = excluded.old_tools,
                    notes = excluded.notes, drive_folder_url = excluded.drive_folder_url,
                    legacy_sheet_id = excluded.legacy_sheet_id,
                    owner_id = coalesce(excluded.owner_id, families.owner_id), archived_at = NULL
                RETURNING id""",
                (code, code, clean(r["status"]) or "Loaded", or_none(r["old_tools"]), or_none(r["notes"]),
                 or_none(r["folder"]), or_none(sid), owner))
            self.family_id[code] = fid
            if sid in self.files:
                self.family_sheet[code] = sid
        gone = self.q("SELECT code FROM families WHERE code <> ALL(%s) AND archived_at IS NULL",
                      (list(seen),)).fetchall()
        for (c,) in gone:
            self.rep.warn("Families", f"family {c} is in the database but no longer in the sheet")
        self.rep.counts["families"] = len(self.family_id)

    def load_blocks(self):
        t = self.index_tab("Blocks")
        for n, r in t.rows:
            name = clean(r["name"])
            if not name:
                continue
            self.block_id[name] = self.one("""
                INSERT INTO blocks (name, show_order, level, description, name_rule, match_order)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (name, to_int(r["show_order"], t.at(n), self.rep, "Show order") or n,
                 or_none(r["level"]), or_none(r["description"]), or_none(r["name_rule"]),
                 to_int(r["match_order"], t.at(n), self.rep, "Match order")))
        self.rep.counts["blocks"] = len(self.block_id)

    def load_cmp_codes(self):
        d = self.files.get(BLUEPRINTS_ID)
        if not d:
            self.rep.warn("Blueprints", "the Blueprints part list is not in the snapshot: no part is marked CMP")
            return
        rows = d["tabs"].get("Parts", [])
        self.cmp_codes = {clean(r[0]) for r in rows[1:] if r and ODOO_CODE.match(clean(r[0]))}
        self.rep.notes.append(f"{len(self.cmp_codes)} custom (CMP) part codes read from Blueprints")

    def load_items(self):
        t = self.index_tab("Items")
        for n, r in t.rows:
            code = clean(r["code"])
            where = t.at(n)
            if not code:
                self.rep.err(where, "Item is empty")
                continue
            if code in self.item_id:
                self.rep.err(where, f"item {code} is listed twice")
                continue
            fam = clean(r["family"])
            if fam not in self.family_id:
                self.rep.err(where, f"family '{fam}' of item {code} is not in the Families tab")
                continue
            level = clean(r["level"]).upper()
            if level not in ("FG", "SFG"):
                self.rep.err(where, f"Level must be FG or SFG (is '{r['level']}')")
                continue
            odoo = clean(r["odoo_code"]) or None
            if odoo and not ODOO_CODE.match(odoo):
                self.rep.err(where, f"Odoo code '{odoo}' should look like P012345")
                continue
            status = clean(r["status"]) or "Proposed"
            blk = clean(r["block_override"])
            if blk and blk not in self.block_id:
                self.rep.warn(where, f"block override '{blk}' is not in the Blocks tab; ignored")
            prefix = clean(r["prefix"]) or code.split("-")[0]
            self.item_id[code] = self.one("""
                INSERT INTO items (code, prefix, family_id, level, status, built_by, description, odoo_today,
                                   odoo_code, notes, brain_url, block_override_id, archived_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now() END) RETURNING id""",
                (code, prefix, self.family_id[fam], level, status, or_none(r["built_by"]),
                 or_none(r["description"]), or_none(r["odoo_today"]), odoo, or_none(r["notes"]),
                 or_none(r["brain_url"]), self.block_id.get(blk), status.lower() == "archived"))
            self.item_family[code] = fam
        dup = self.q("SELECT odoo_code, string_agg(code, ', ') FROM items WHERE odoo_code IS NOT NULL "
                     "GROUP BY odoo_code HAVING count(*) > 1").fetchall()
        for oc, codes in dup:
            self.rep.warn("Items", f"Odoo code {oc} is used by more than one item: {codes}")
        self.rep.counts["items"] = len(self.item_id)

    def part(self, name, code, where):
        """Find or create the part for a BOM line. Parts are keyed by Odoo code."""
        if code:
            if not ODOO_CODE.match(code):
                self.rep.err(where, f"Code '{code}' should look like P012345")
                return None
            pid = self.part_by_code.get(code)
            if pid is None:
                pid = self.one("INSERT INTO parts (code, mpn, is_cmp) VALUES (%s, %s, %s) RETURNING id",
                               (code, name, code in self.cmp_codes))
                self.part_by_code[code] = pid
                self.part_name_of[pid] = name
            elif self.part_name_of[pid] != name:
                self.rep.warn(where, f"code {code} is called '{name}' here but "
                                     f"'{self.part_name_of[pid]}' elsewhere; the first name is kept on the part")
            return pid
        pid = self.part_by_name.get(name)
        if pid is None:
            pid = self.one("INSERT INTO parts (code, mpn) VALUES (NULL, %s) RETURNING id", (name,))
            self.part_by_name[name] = pid
            self.part_name_of[pid] = name
            self.rep.warn(where, f"part '{name}' has no Odoo code")
        return pid

    def load_boms(self):
        self.part_name_of: dict[int, str] = {}
        totals = Counter()
        # sheet status -> (boms.status, state of the migrated version 1)
        # "Needs info" = unfinished, set up like a new BOM in the app (create_bom): Under review + Draft
        status_map = {"active": ("Active", "Released"), "under review": ("Under review", "In review"),
                      "archived": ("Archived", "Released"), "not to be used": ("Archived", "Released"),
                      "needs info": ("Under review", "Draft")}
        for fam in sorted(self.family_sheet):
            bt, lt = self.family_tab(fam, "BOMs"), self.family_tab(fam, "Lines")
            version_of: dict[str, int] = {}
            status_of: dict[str, str] = {}
            defaults = Counter()
            for n, r in bt.rows:
                key, item = clean(r["bom"]), clean(r["item"])
                where = bt.at(n)
                if not key:
                    self.rep.err(where, "BOM is empty")
                    continue
                if key in self.bom_keys:
                    self.rep.err(where, f"BOM '{key}' is also in {self.bom_keys[key]}")
                    continue
                if item not in self.item_id:
                    self.rep.err(where, f"item '{item}' is not in the Items tab")
                    continue
                if self.item_family[item] != fam:
                    self.rep.warn(where, f"item {item} belongs to family {self.item_family[item]} in the Items tab")
                status = clean(r["status"]) or "Active"
                if status.lower() not in status_map:
                    self.rep.err(where, "Status must be Active, Under review, Needs info, Not to be used or "
                                        f"Archived (is '{status}')")
                    continue
                status, state = status_map[status.lower()]
                is_default = yes(r["default"])
                if is_default and status != "Archived":
                    defaults[item] += 1
                    if defaults[item] > 1:
                        self.rep.err(where, f"item {item} has more than one default BOM")
                        continue
                option = clean(r["option"]) or "Standard"
                bom_id = self.one("""
                    INSERT INTO boms (item_id, option_code, legacy_key, is_default, when_to_use, status, notes, archived_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now() END) RETURNING id""",
                    (self.item_id[item], option, key, is_default, or_none(r["when"]), status,
                     or_none(r["notes"]), status == "Archived"))
                ver = self.one("""INSERT INTO bom_versions (bom_id, version_no, state, migrated, created_by, change_note)
                                  VALUES (%s, 1, 'Draft', true, %s, 'Loaded from the Google Sheets') RETURNING id""",
                               (bom_id, self.migration_user))
                version_of[key], status_of[key] = ver, state
                self.bom_keys[key] = bt.where
            # lines, in sheet order
            seq = Counter()
            rows = []
            for n, r in lt.rows:
                key, comp, code = clean(r["bom"]), clean(r["component"]), clean(r["code"])
                where = lt.at(n)
                if key not in version_of:
                    self.rep.err(where, f"BOM '{key}' is not in the BOMs tab of {fam}")
                    continue
                if not comp:
                    self.rep.err(where, "Component is empty")
                    continue
                qty = to_number(r["qty"], where, self.rep, "Qty", required=True, positive=True)
                if qty is None:
                    continue
                self.sheet_lines[key].append({"component": comp, "code": code, "qty": qty,
                                              "ref_des": clean(r["ref_des"]), "note": clean(r["note"])})
                child = self.item_id.get(comp)
                part = None
                if child is None:
                    part = self.part(comp, code, where)
                    if part is None:
                        continue
                elif code:
                    self.rep.warn(where, f"sub-assembly {comp} has a code ({code}) on the line; "
                                         "the item's own Odoo code is used")
                seq[key] += 1
                rows.append((version_of[key], seq[key], part, child, comp, qty,
                             or_none(r["ref_des"]), or_none(r["note"])))
            self.cur.executemany("""INSERT INTO bom_lines (bom_version_id, seq, part_id, child_item_id,
                                    component_label, qty, ref_des, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
            # release the versions (Draft -> Released is allowed only for migrated versions)
            for key, ver in version_of.items():
                if not seq[key]:
                    self.rep.warn(bt.where, f"BOM '{key}' has no lines")
                st = status_of[key]
                if st == "Released":
                    self.q("""UPDATE bom_versions SET state = 'Released', released_by = %s, released_at = now()
                              WHERE id = %s""", (self.migration_user, ver))
                elif st == "In review":
                    self.q("""UPDATE bom_versions SET state = 'In review', submitted_by = %s, submitted_at = now()
                              WHERE id = %s""", (self.migration_user, ver))
                self.q("UPDATE boms SET current_version_id = %s WHERE id = (SELECT bom_id FROM bom_versions WHERE id = %s)",
                       (ver, ver))
            totals["boms"] += len(version_of)
            totals["bom_lines"] += len(rows)
        no_bom = self.q("""SELECT i.code FROM items i WHERE NOT EXISTS (SELECT 1 FROM boms b WHERE b.item_id = i.id)
                           AND i.archived_at IS NULL ORDER BY 1""").fetchall()
        for (c,) in no_bom:
            self.rep.warn("Items", f"item {c} has no BOM")
        self.rep.counts["boms"] = totals["boms"]
        self.rep.counts["bom_versions"] = totals["boms"]
        self.rep.counts["bom_lines"] = totals["bom_lines"]
        self.rep.counts["parts"] = len(self.part_by_code) + len(self.part_by_name)

    def load_family_extras(self):
        counts = Counter()
        for fam in sorted(self.family_sheet):
            fid = self.family_id[fam]
            t = self.family_tab(fam, "Old lines")
            rows = []
            for i, (n, r) in enumerate(t.rows, 1):
                if not clean(r["old_item"]):
                    self.rep.err(t.at(n), "Old item is empty")
                    continue
                rows.append((fid, i, clean(r["old_item"]), or_none(r["old_code"]), or_none(r["old_tool"]),
                             or_none(r["component"]), or_none(r["code"]),
                             to_number(r["qty"], t.at(n), self.rep, "Qty"),
                             or_none(r["ref_des"]), or_none(r["note"])))
            self.cur.executemany("""INSERT INTO legacy_lines (family_id, seq, old_item, old_code, old_tool, component,
                                    part_code, qty, ref_des, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
            counts["legacy_lines"] += len(rows)

            t = self.family_tab(fam, "PNP")
            rows = []
            for i, (n, r) in enumerate(t.rows, 1):
                w = t.at(n)
                side = clean(r["side"]).capitalize() or None
                if side and side not in ("Top", "Bottom"):
                    self.rep.err(w, f"Side must be Top or Bottom (is '{r['side']}')")
                    continue
                if not clean(r["board"]) or not clean(r["ref_des"]):
                    self.rep.err(w, "Board and Ref des are needed")
                    continue
                rows.append((fid, i, clean(r["board"]), clean(r["ref_des"]), or_none(r["name"]), or_none(r["package"]),
                             to_number(r["x"], w, self.rep, "X (mm)"), to_number(r["y"], w, self.rep, "Y (mm)"),
                             side, to_number(r["rotation"], w, self.rep, "Rotation"), or_none(r["value"]),
                             or_none(r["source"]), to_time(r["loaded"], w, self.rep, "Loaded", self.tz)))
            self.cur.executemany("""INSERT INTO placements (family_id, seq, board, ref_des, name, package, x_mm, y_mm,
                                    side, rotation, value, source, loaded_at)
                                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
            counts["placements"] += len(rows)

            t = self.family_tab(fam, "Design")
            rows = []
            for i, (n, r) in enumerate(t.rows, 1):
                w = t.at(n)
                if not clean(r["board"]) or not clean(r["ref_des"]):
                    self.rep.err(w, "Board and Ref des are needed")
                    continue
                rows.append((fid, i, clean(r["board"]), clean(r["ref_des"]), or_none(r["block_path"]),
                             or_none(r["copy"]), or_none(r["level"]), or_none(r["role"]), or_none(r["why"]),
                             or_none(r["design_mpn"]), or_none(r["design_code"]), or_none(r["confirmed_by"]),
                             to_date(r["confirmed_on"], w, self.rep, "Confirmed on", self.tz)))
            self.cur.executemany("""INSERT INTO design_refs (family_id, seq, board, ref_des, block_path, copy_label,
                                    level, role, why, design_mpn, design_code, confirmed_by, confirmed_on)
                                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
            counts["design_refs"] += len(rows)

            t = self.family_tab(fam, "Design nets")
            rows = []
            for i, (n, r) in enumerate(t.rows, 1):
                if not clean(r["board"]) or not clean(r["net"]):
                    self.rep.err(t.at(n), "Board and Net are needed")
                    continue
                rows.append((fid, i, clean(r["board"]), clean(r["net"]), or_none(r["kind"]),
                             or_none(r["sheet"]), or_none(r["ends"])))
            self.cur.executemany("""INSERT INTO design_nets (family_id, seq, board, net, kind, sheet, ends)
                                    VALUES (%s,%s,%s,%s,%s,%s,%s)""", rows)
            counts["design_nets"] += len(rows)
        self.rep.counts.update(counts)

    def load_index_extras(self):
        rep, tz = self.rep, self.tz
        # Conversions
        t = self.index_tab("Conversions")
        rows = []
        for i, (n, r) in enumerate(t.rows, 1):
            new = clean(r["new_item"])
            new_id = self.item_id.get(new)
            if new and new_id is None:
                rep.warn(t.at(n), f"new item '{new}' is not in the Items tab; kept as text")
            rows.append((i, or_none(r["old_tool"]), or_none(r["old_item"]), or_none(r["old_code"]),
                         or_none(r["change"]), new_id, or_none(new), or_none(r["note"])))
        self.cur.executemany("""INSERT INTO conversions (seq, old_tool, old_item, old_code, change, new_item_id,
                                new_item_text, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        rep.counts["conversions"] = len(rows)

        # Library
        t = self.index_tab("Library")
        rows = []
        for n, r in t.rows:
            w, item = t.at(n), clean(r["item"])
            rows.append((self.item_id.get(item), item, or_none(r["code"]),
                         to_number(r["width"], w, rep, "Width mm"), to_number(r["height"], w, rep, "Height mm"),
                         or_none(r["front"]), or_none(r["back"]),
                         json.dumps(to_json(r["ports"], w, rep, "Ports", [])), or_none(r["source"]),
                         to_time(r["updated"], w, rep, "Updated", tz), or_none(r["notes"])))
        self.cur.executemany("""INSERT INTO board_library (item_id, item_text, code, width_mm, height_mm, front_picture,
                                back_picture, ports, source, updated_at, notes)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""", rows)
        rep.counts["board_library"] = len(rows)

        # Canvas
        t = self.index_tab("Canvas")
        rows = []
        for n, r in t.rows:
            w, item = t.at(n), clean(r["item"])
            data = to_json(r["data"], w, rep, "Data", None)
            rows.append((self.item_id.get(item), item, or_none(r["pages"]),
                         json.dumps(data) if data is not None else None,
                         to_time(r["updated"], w, rep, "Updated", tz), or_none(r["by"]), or_none(r["notes"])))
        self.cur.executemany("""INSERT INTO canvases (item_id, item_text, pages, data, updated_at, updated_by, notes)
                                VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s)""", rows)
        rep.counts["canvases"] = len(rows)

        # Tools
        t = self.index_tab("Tools")
        rows = []
        for n, r in t.rows:
            if clean(r["tool"]):
                rows.append((clean(r["tool"]), yes(r["archived"]), or_none(r["by"]),
                             to_time(r["when"], t.at(n), rep, "When", tz), or_none(r["note"])))
        self.cur.executemany("""INSERT INTO old_tools (tool, archived, changed_by, changed_at, note)
                                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tool) DO UPDATE SET archived = excluded.archived,
                                changed_by = excluded.changed_by, changed_at = excluded.changed_at, note = excluded.note""",
                             rows)
        rep.counts["old_tools"] = len(rows)

        # Alternates
        t = self.index_tab("Alternates")
        rows = []
        for i, (n, r) in enumerate(t.rows, 1):
            rows.append((i, clean(r["group"]) or clean(r["base_part"]), or_none(r["base_part"]), or_none(r["base_code"]),
                         or_none(r["mfr_pn"]), or_none(r["mfr"]), or_none(r["digikey"]), or_none(r["description"]),
                         or_none(r["kind"]), or_none(r["status"]), or_none(r["odoo_code"]), or_none(r["search"]),
                         or_none(r["approved_by"]), to_date(r["approved_on"], t.at(n), rep, "Approved on", tz),
                         or_none(r["note"]), or_none(r["url"]), or_none(r["product"])))
        self.cur.executemany("""INSERT INTO alternates (seq, group_name, base_part, base_code, mfr_pn, mfr, digikey_pn,
                                description, kind, status, odoo_code, search, approved_by, approved_on, note, url, product)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        rep.counts["alternates"] = len(rows)

        # Alternate searches
        t = self.index_tab("Alternate searches")
        rows = []
        for i, (n, r) in enumerate(t.rows, 1):
            w = t.at(n)
            rows.append((i, or_none(r["search"]), or_none(r["group"]), or_none(r["base_part"]), or_none(r["base_code"]),
                         or_none(r["kind"]), to_date(r["date"], w, rep, "Date", tz), or_none(r["by"]),
                         to_int(r["rows"], w, rep, "Rows in CSV"), or_none(r["approved"]), or_none(r["file"]),
                         or_none(r["link"]), or_none(r["note"])))
        self.cur.executemany("""INSERT INTO alternate_searches (seq, search, group_name, base_part, base_code, kind,
                                searched_on, searched_by, rows_in_csv, approved, file, digikey_link, note)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        rep.counts["alternate_searches"] = len(rows)

        # Design blocks
        t = self.index_tab("Design blocks")
        rows = []
        for i, (n, r) in enumerate(t.rows, 1):
            w = t.at(n)
            rows.append((i, clean(r["block_type"]), or_none(r["function"]), or_none(r["products"]), or_none(r["boards"]),
                         to_int(r["copies"], w, rep, "Copies"), to_int(r["lines"], w, rep, "Lines"),
                         to_int(r["critical"], w, rep, "Critical"), to_int(r["sensitive"], w, rep, "Sensitive"),
                         to_int(r["normal"], w, rep, "Normal"), or_none(r["fingerprint"]), or_none(r["status"]),
                         or_none(r["confirmed_by"]), to_date(r["confirmed_on"], w, rep, "Confirmed on", tz)))
        self.cur.executemany("""INSERT INTO design_blocks (seq, block_type, function, products, boards, copies, lines,
                                critical, sensitive, normal, fingerprint, status, confirmed_by, confirmed_on)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        rep.counts["design_blocks"] = len(rows)

        # Design issues
        t = self.index_tab("Design issues")
        rows = []
        for i, (n, r) in enumerate(t.rows, 1):
            if not clean(r["board"]) or not clean(r["issue"]):
                rep.err(t.at(n), "Board and Issue are needed")
                continue
            rows.append((i, clean(r["board"]), or_none(r["ref_des"]), clean(r["issue"]), or_none(r["source"]),
                         clean(r["status"]) or "Open"))
        self.cur.executemany("""INSERT INTO design_issues (seq, board, ref_des, issue, source, status)
                                VALUES (%s,%s,%s,%s,%s,%s)""", rows)
        rep.counts["design_issues"] = len(rows)

        # Log -> history (audit_log), oldest first
        t = self.index_tab("Log")
        rows = []
        for n, r in t.rows:
            when = to_time(r["when"], t.at(n), rep, "When", tz)
            rows.append((when or dt.datetime.now(dt.timezone.utc), or_none(r["who"]), clean(r["action"]) or "note",
                         or_none(r["item"]), or_none(r["detail"])))
        rows.sort(key=lambda x: x[0])
        self.cur.executemany("""INSERT INTO audit_log (at, actor_label, source, action, entity_type, entity_label, detail)
                                VALUES (%s,%s,'Migration',%s,'sheet log',%s,%s)""", rows)
        rep.counts["audit_log (old Log tab)"] = len(rows)

    def finish(self, started):
        counts = dict(self.rep.counts)
        self.q("""INSERT INTO audit_log (user_id, source, action, entity_type, detail, after)
                  VALUES (%s, 'Migration', 'Seeded', 'database', 'Loaded from the Google Sheets', %s::jsonb)""",
               (self.migration_user, json.dumps(counts)))
        self.q("""INSERT INTO migration_runs (started_at, finished_at, snapshot, counts, errors, warnings, report)
                  VALUES (%s, now(), %s::jsonb, %s::jsonb, %s, %s, %s)""",
               (started, json.dumps(self.meta), json.dumps(counts), len(self.rep.errors), len(self.rep.warnings),
                self.rep.text("BOMTree migration")))


# ---------------------------------------------------------------------------
# The check: compare what is in the database with the snapshot, line by line
# ---------------------------------------------------------------------------
def norm_qty(q):
    q = Decimal(q)
    return q.quantize(Decimal("0.0001"))


def run_checks(cur, files, rep: Report):
    cur.execute("SET LOCAL search_path = bomtree, hippo, public")
    idx = files[INDEX_ID]
    # Items: every item in the sheet is in the database with the same family, level and Odoo code
    t = Tab(idx.get("title", "Index"), "Items", idx["tabs"].get("Items", []), INDEX_TABS["Items"]["cols"], Report(), True)
    sheet_items = {clean(r["code"]): r for _, r in t.rows if clean(r["code"])}
    cur.execute("""SELECT i.code, f.code, i.level, coalesce(i.odoo_code, '') FROM items i JOIN families f ON f.id = i.family_id""")
    db_items = {c: (f, l, o) for c, f, l, o in cur.fetchall()}
    for code, r in sheet_items.items():
        got = db_items.get(code)
        want = (clean(r["family"]), clean(r["level"]).upper(), clean(r["odoo_code"]))
        if got is None:
            rep.err("Check › Items", f"{code} is in the sheet but not in the database")
        elif got != want:
            rep.err("Check › Items", f"{code}: sheet {want} vs database {got}")
    for code in db_items.keys() - sheet_items.keys():
        rep.err("Check › Items", f"{code} is in the database but not in the sheet")

    # BOM lines: same lines, same order, same component, code, qty, ref des and note
    cur.execute("""SELECT b.legacy_key, l.seq, l.component_label, coalesce(p.code, ''), l.qty,
                          coalesce(l.ref_des, ''), coalesce(l.note, '')
                   FROM boms b JOIN bom_lines l ON l.bom_version_id = b.current_version_id
                   LEFT JOIN parts p ON p.id = l.part_id ORDER BY b.legacy_key, l.seq""")
    db_lines = defaultdict(list)
    for key, _seq, comp, code, qty, ref, note in cur.fetchall():
        db_lines[key].append((comp, code, norm_qty(qty), ref, note))
    cur.execute("SELECT legacy_key FROM boms")
    db_boms = {k for (k,) in cur.fetchall()}

    sheet_boms, n_lines, n_checked = set(), 0, 0
    fam_tab = Tab(idx.get("title", "Index"), "Families", idx["tabs"].get("Families", []),
                  INDEX_TABS["Families"]["cols"], Report(), True)
    for _, fr in fam_tab.rows:
        sid = clean(fr["sheet_id"])
        if sid not in files:
            continue
        d = files[sid]
        bt = Tab(d.get("title", sid), "BOMs", d["tabs"].get("BOMs", []), FAMILY_TABS["BOMs"]["cols"], Report(), True)
        lt = Tab(d.get("title", sid), "Lines", d["tabs"].get("Lines", []), FAMILY_TABS["Lines"]["cols"], Report(), True)
        sheet_boms |= {clean(r["bom"]) for _, r in bt.rows if clean(r["bom"])}
        want = defaultdict(list)
        for _, r in lt.rows:
            key = clean(r["bom"])
            try:
                q = norm_qty(clean(r["qty"]).replace(",", ""))
            except InvalidOperation:
                continue
            comp = clean(r["component"])
            code = "" if comp in sheet_items else clean(r["code"])
            want[key].append((comp, code, q, clean(r["ref_des"]), clean(r["note"])))
        for key, lines in want.items():
            n_lines += len(lines)
            got = db_lines.get(key, [])
            if got == lines:
                n_checked += len(lines)
                continue
            for i in range(max(len(lines), len(got))):
                a = lines[i] if i < len(lines) else None
                b = got[i] if i < len(got) else None
                if a != b:
                    rep.err(f"Check › {d.get('title', sid)} › {key}", f"line {i + 1}: sheet {a} vs database {b}")
                    break
            else:
                n_checked += len(lines)
    for k in sheet_boms - db_boms:
        rep.err("Check › BOMs", f"'{k}' is in the sheets but not in the database")
    for k in db_boms - sheet_boms:
        rep.err("Check › BOMs", f"'{k}' is in the database but not in the sheets")
    rep.notes.append(f"Check: {len(sheet_items)} items, {len(sheet_boms)} BOMs and {n_checked} of {n_lines} "
                     f"BOM lines match the sheets exactly")

    # Where used (the sheet tab is worked out by the old app; differences are warnings)
    wu = Tab(idx.get("title", "Index"), "Where used", idx["tabs"].get("Where used", []),
             INDEX_TABS["Where used"]["cols"], Report(), False)
    if wu.present:
        sheet_wu = {(clean(r["component"]), clean(r["family"])) for _, r in wu.rows}
        cur.execute("SELECT DISTINCT component, family FROM v_where_used")
        db_wu = set(cur.fetchall())
        miss, extra = sheet_wu - db_wu, db_wu - sheet_wu
        if miss or extra:
            rep.warn("Check › Where used", f"{len(miss)} rows only in the sheet tab, {len(extra)} only in the database "
                                           f"(the sheet tab may be out of date); e.g. {sorted(miss)[:3]} {sorted(extra)[:3]}")
        else:
            rep.notes.append(f"Check: Where used matches the sheet tab ({len(db_wu)} component/family pairs)")

    # Health checks that do not block the load
    cur.execute("""SELECT p.mpn, string_agg(p.code, ', ' ORDER BY p.code) FROM parts p
                   WHERE p.code IS NOT NULL GROUP BY p.mpn HAVING count(*) > 1""")
    for mpn, codes in cur.fetchall():
        rep.warn("Check › Parts", f"'{mpn}' has more than one Odoo code: {codes} (duplicate Odoo products?)")
    cur.execute("""SELECT b.legacy_key, r.ref, count(*) FROM boms b
                   JOIN bom_lines l ON l.bom_version_id = b.current_version_id,
                   LATERAL regexp_split_to_table(l.ref_des, '\\s+') AS r(ref)
                   WHERE r.ref <> '' GROUP BY 1, 2 HAVING count(*) > 1""")
    for key, ref, n in cur.fetchall():
        rep.warn("Check › Ref des", f"'{ref}' is used {n} times in {key}")
    cur.execute("SELECT count(*) FROM items WHERE odoo_code IS NULL AND archived_at IS NULL")
    n = cur.fetchone()[0]
    if n:
        rep.notes.append(f"{n} items have no Odoo code yet (normal for new SFGs)")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def connect(args):
    try:
        import psycopg
    except ImportError:
        raise SystemExit('Missing library. Run:  pip install "psycopg[binary]"')
    dsn = args.db or os.environ.get("BOMTREE_DB")
    if not dsn:
        raise SystemExit("Give the database with --db or the BOMTREE_DB environment variable.")
    return psycopg.connect(dsn, autocommit=False)


def write_report(rep, folder, title):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    path = Path(folder) / f"report-{stamp}.md"
    path.write_text(rep.text(title), encoding="utf-8")
    return path


def cmd_load(args):
    rep = Report()
    files, meta = load_snapshot(Path(args.snapshot), rep)
    tz = ZoneInfo(args.tz or files[INDEX_ID].get("timeZone") or DEFAULT_TZ)
    started = dt.datetime.now(dt.timezone.utc)
    conn = connect(args)
    try:
        with conn.transaction():                       # everything below is ONE transaction
            ld = Loader(conn, files, meta, rep, tz)
            ld.prepare(args.replace)
            print("Loading families, blocks and items ...")
            ld.load_families(); ld.load_blocks(); ld.load_cmp_codes(); ld.load_items()
            print("Loading BOMs and lines ...")
            ld.load_boms()
            print("Loading old lines, pick-and-place and design data ...")
            ld.load_family_extras(); ld.load_index_extras()
            print("Checking every row against the sheets ...")
            run_checks(ld.cur, files, rep)
            ld.finish(started)
            keep = args.commit and not rep.errors
            if not keep:
                raise _Rollback()
    except _Rollback:
        pass
    finally:
        conn.close()
    path = write_report(rep, args.snapshot, "BOMTree migration")
    print()
    print(f"Errors: {len(rep.errors)}   Warnings: {len(rep.warnings)}   Report: {path}")
    for k, v in rep.counts.items():
        print(f"  {k:<26}{v:>7}")
    for e in rep.errors[:15]:
        print("  ERROR  " + e)
    if len(rep.errors) > 15:
        print(f"  ... {len(rep.errors) - 15} more errors in the report")
    if args.commit and not rep.errors:
        print("\nSAVED. The data is now in PostgreSQL.")
        return 0
    if args.commit:
        print("\nNOT SAVED: fix the errors in the sheets, export again and re-run.")
        return 1
    print("\nDRY RUN: nothing was saved. Add --commit to keep the data.")
    return 1 if rep.errors else 0


def cmd_check(args):
    rep = Report()
    files, _ = load_snapshot(Path(args.snapshot), rep)
    conn = connect(args)
    try:
        with conn.transaction():
            cur = conn.cursor()
            run_checks(cur, files, rep)
    finally:
        conn.close()
    path = write_report(rep, args.snapshot, "BOMTree check")
    for n in rep.notes:
        print(n)
    print(f"Errors: {len(rep.errors)}   Warnings: {len(rep.warnings)}   Report: {path}")
    return 1 if rep.errors else 0


class _Rollback(Exception):
    """Raised inside the transaction to undo it (dry run, or errors found)."""


def main():
    p = argparse.ArgumentParser(description="Move the BOMTree Google Sheets into PostgreSQL.")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="copy the Google Sheets into a snapshot folder")
    e.add_argument("--key", required=True, help="Google service-account JSON key file")
    e.add_argument("--out", default="snapshot")
    e.add_argument("--index", default=INDEX_ID, required=not INDEX_ID, help="Index sheet ID (default: INDEX_SHEET_ID)")
    e.add_argument("--blueprints", default=BLUEPRINTS_ID, help="Blueprints sheet ID (default: BLUEPRINTS_SHEET_ID; '' to skip)")
    for name, helptext in (("load", "load a snapshot into PostgreSQL"), ("check", "compare the database with a snapshot")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--snapshot", default="snapshot")
        s.add_argument("--db", help="postgresql://user:password@host:5432/bomtree (or set BOMTREE_DB)")
        if name == "load":
            s.add_argument("--commit", action="store_true", help="keep the data (only if there are 0 errors)")
            s.add_argument("--replace", action="store_true", help="wipe earlier migrated BOM data and reload it")
            s.add_argument("--tz", help=f"time zone of dates in the sheets (default: the sheet's, else {DEFAULT_TZ})")
    a = p.parse_args()
    if a.cmd == "export":
        cmd_export(a)
        return 0
    return cmd_load(a) if a.cmd == "load" else cmd_check(a)


if __name__ == "__main__":
    sys.exit(main())
