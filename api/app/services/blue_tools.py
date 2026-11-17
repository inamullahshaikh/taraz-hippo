"""Blue's 12 tools. Each is a thin wrapper over the same services the API uses, run with the chatting
person's rights (ctx.source = 'Blue' also applies the deny list). Tools that would change a BOM only
return a proposal: the person still presses Save."""
from decimal import Decimal

from fastapi import HTTPException

from ..integrations import drive
from . import analysis, boms, exports, imports

S = {"type": "string"}
I = {"type": "integer"}


def tool(name, description, props, required=()):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}}


TOOLS = [
    tool("search_items", "Find products and SFGs by code or text, optionally in one family or level (FG/SFG).",
         {"q": S, "family": S, "level": {"type": "string", "enum": ["FG", "SFG"]}}),
    tool("get_bom", "Read one BOM version. Without option_code the default BOM is used; without version_no the current one. "
                    "explode=true opens sub-assemblies to any depth.",
         {"item_code": S, "option_code": S, "version_no": I, "explode": {"type": "boolean"}}, ["item_code"]),
    tool("filter_lines", "Lines of a version that match text (code, MPN, description, note), a ref des prefix (e.g. 'C', 'U2'), "
                         "or a kind: CMP (custom part), OTS (bought part) or SUB (sub-assembly).",
         {"version_id": I, "text": S, "ref_prefix": S, "kind": {"type": "string", "enum": ["CMP", "OTS", "SUB"]}}, ["version_id"]),
    tool("where_used", "Every BOM, across families, that uses a part (code or MPN) or a sub-assembly (item code).",
         {"part": S, "item": S}),
    tool("compare", "Diff two versions (added, removed, changed lines), or with legacy_item the old BOM Tool lines vs the item's current BOM.",
         {"from_version_id": I, "to_version_id": I, "legacy_item": S}),
    tool("cost_and_shortage", "Rolled-up cost and parts short in stock, for building qty units of a version.",
         {"version_id": I, "qty": {"type": "number"}}, ["version_id"]),
    tool("export", "Make a file of a version: xlsx, gsheet (Google Sheet in the family folder, or folder_id), pdf, jlc or pcbway. "
                   "filter keeps lines containing that text. Returns a link.",
         {"version_id": I, "format": {"type": "string", "enum": ["xlsx", "gsheet", "pdf", "jlc", "pcbway"]}, "filter": S, "folder_id": S},
         ["version_id", "format"]),
    tool("find_drive_folder", "Find a Google Drive folder by name, for an export.", {"name": S}, ["name"]),
    tool("propose_bulk_edit", "Prepare a mass edit on an open Draft: set_note, add_note, set_qty or remove, on the given line ids. "
                              "Nothing changes until the person presses Save.",
         {"version_id": I, "line_ids": {"type": "array", "items": I}, "op": {"type": "string", "enum": ["set_note", "add_note", "set_qty", "remove"]},
          "value": S, "reason": S}, ["version_id", "line_ids", "op"]),
    tool("match_import_lines", "Read a DipTrace import: lines still unclear and the candidate parts for each. Suggest answers with "
                               "suggestions = {line index: part id}; the person applies them.",
         {"import_id": I, "suggestions": {"type": "object", "additionalProperties": I}}, ["import_id"]),
    tool("add_comment", "Comment on an item, BOM, version or change request (entity_type: item, bom, version, change_request).",
         {"entity_type": {"type": "string", "enum": ["item", "bom", "version", "change_request"]}, "entity_id": I, "line_id": I, "body": S},
         ["entity_type", "entity_id", "body"]),
    tool("draft_change_request", "Fill in a change request form for a released BOM, for the person to check and send.",
         {"bom_id": I, "reason": S, "impact": S, "priority": {"type": "string", "enum": ["Low", "Normal", "Urgent"]}}, ["bom_id", "reason"]),
]


def compact(lines, depth=0):
    out = []
    for l in lines:
        out.append({"line_id": l["id"], "depth": depth, "code": l["part_code"] or l["child_code"],
                    "mpn": l["mpn"] or l["child_description"], "qty": l["qty"], "ref_des": l["ref_des"], "note": l["note"] or None,
                    "kind": "SUB" if l["child_item_id"] else ("CMP" if l["is_cmp"] else "OTS")})
        kids = l.get("children")
        if kids:
            out[-1]["opens"] = f"{kids['version']['item_code']} {kids['version']['option_code']} v{kids['version']['version_no']}"
            out += compact(kids["lines"], depth + 1)
    return out


def resolve_version(ctx, item_code, option_code=None, version_no=None):
    b = ctx.one("""SELECT b.id, b.current_version_id FROM boms b JOIN items i ON i.id = b.item_id
                   WHERE upper(i.code) = upper(%s) AND b.archived_at IS NULL
                     AND (upper(b.option_code) = upper(%s) OR (%s::text IS NULL AND b.is_default))""", item_code, option_code, option_code)
    if not b:
        raise HTTPException(404, f"No BOM {option_code or '(default)'} for {item_code}")
    if version_no:
        v = ctx.one("SELECT id FROM bom_versions WHERE bom_id = %s AND version_no = %s", b["id"], version_no)
        if not v:
            raise HTTPException(404, f"No v{version_no}")
        return v["id"]
    return b["current_version_id"]


def run(ctx, name, args, emit):
    """Run one tool; returns a JSON-able result. emit(event) sends proposals and files to the page."""
    a = args
    if name == "search_items":
        p = f"%{a.get('q', '')}%"
        return ctx.all("""SELECT i.code, i.level, i.status, i.description, f.code AS family FROM items i JOIN families f ON f.id = i.family_id
                          WHERE i.archived_at IS NULL AND (i.code ILIKE %s OR i.description ILIKE %s)
                            AND (%s::text IS NULL OR upper(f.code) = upper(%s)) AND (%s::text IS NULL OR i.level = %s)
                          ORDER BY f.code, i.code LIMIT 40""", p, p, a.get("family"), a.get("family"), a.get("level"), a.get("level"))
    if name == "get_bom":
        vid = resolve_version(ctx, a["item_code"], a.get("option_code"), a.get("version_no"))
        v = boms.version(ctx, vid)
        rows = boms.explode(ctx, vid) if a.get("explode") else boms.lines(ctx, vid)
        return {"version_id": vid, "bom_id": v["bom_id"], "item": v["item_code"], "option": v["option_code"],
                "version_no": v["version_no"], "state": v["state"], "lines": compact(rows)}
    if name == "filter_lines":
        boms.version(ctx, a["version_id"])
        out = []
        for l in compact(boms.lines(ctx, a["version_id"])):
            hay = " ".join(str(x) for x in (l["code"], l["mpn"], l["note"])).lower()
            if a.get("text") and a["text"].lower() not in hay:
                continue
            if a.get("ref_prefix") and not any(r.upper().startswith(a["ref_prefix"].upper()) for r in l["ref_des"].split()):
                continue
            if a.get("kind") and l["kind"] != a["kind"]:
                continue
            out.append(l)
        return out
    if name == "where_used":
        return analysis.where_used(ctx, part=a.get("part"), item=a.get("item"))
    if name == "compare":
        if a.get("legacy_item"):
            return analysis.legacy_compare(ctx, a["legacy_item"])
        boms.version(ctx, a["from_version_id"])
        boms.version(ctx, a["to_version_id"])
        return boms.diff(boms.lines(ctx, a["from_version_id"]), boms.lines(ctx, a["to_version_id"]))
    if name == "cost_and_shortage":
        q = Decimal(str(a.get("qty") or 1))
        c, s = analysis.cost(ctx, a["version_id"], q), analysis.shortage(ctx, a["version_id"], q)
        return {"qty": q, "total": c["total"], "unit": c["unit"], "missing_cost": c["missing_cost"], "short": s["short"][:50]}
    if name == "export":
        out = exports.make(ctx, a["version_id"], a["format"], a.get("filter", ""), None, a.get("folder_id"))
        emit({"type": "file", **out})
        return out
    if name == "find_drive_folder":
        ctx.require("export.create")
        return drive.find_folders(a["name"])
    if name == "propose_bulk_edit":
        v = boms.version(ctx, a["version_id"], "bom.edit")
        if v["state"] != "Draft":
            raise HTTPException(409, f"v{v['version_no']} is {v['state']}: only a Draft can be edited")
        have = {r["id"] for r in ctx.all("SELECT id FROM bom_lines WHERE bom_version_id = %s", a["version_id"])}
        ids = [i for i in a["line_ids"] if i in have]
        emit({"type": "proposal", "kind": "bulk_edit", "version_id": a["version_id"], "lock_version": v["lock_version"],
              "line_ids": ids, "op": a["op"], "value": a.get("value"), "reason": a.get("reason", "")})
        return {"proposed": len(ids), "note": "Shown to the person; nothing is saved until they press Save."}
    if name == "match_import_lines":
        view = imports.get(ctx, a["import_id"])
        if a.get("suggestions"):
            emit({"type": "proposal", "kind": "import_answers", "import_id": a["import_id"], "answers": a["suggestions"]})
        return {"open": [{"index": i, "question": l["question"], "candidates": l["candidates"]}
                         for i, l in enumerate(view["lines"]) if not l["match"] and not l.get("skip")]}
    if name == "add_comment":
        from ..routers.work import CommentIn, add_comment
        return add_comment(ctx, CommentIn(**a))
    if name == "draft_change_request":
        from ..routers.work import family_of
        ctx.require("cr.create", family_of(ctx, "bom", a["bom_id"]))
        emit({"type": "proposal", "kind": "change_request", **a})
        return {"note": "The form is filled in on the page; the person checks it and sends it."}
    raise HTTPException(404, f"Unknown tool {name}")
