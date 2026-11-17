"""Odoo 13 over XML-RPC: nightly reads into the app, and pushing a Released version as an mrp.bom.

One API user per Odoo database ("BOMTree API", manufacturing and inventory rights only).
"""
import xmlrpc.client

from ..db import Jsonb
from ..settings import S


class OdooError(Exception):
    def __init__(self, message, missing=None):
        super().__init__(message)
        self.missing = missing or []


class Odoo:
    def __init__(self, target):
        c = S.ODOO[target]
        if not c["url"]:
            raise OdooError(f"Odoo {target} is not set up (ODOO_{target.upper()}_URL)")
        self.target, self.db, self.key = target, c["db"], c["key"]
        self.uid = xmlrpc.client.ServerProxy(f"{c['url']}/xmlrpc/2/common").authenticate(c["db"], c["user"], c["key"], {})
        if not self.uid:
            raise OdooError(f"Odoo {target} login failed")
        self.models = xmlrpc.client.ServerProxy(f"{c['url']}/xmlrpc/2/object", allow_none=True)

    def call(self, model, method, *args, **kw):
        return self.models.execute_kw(self.db, self.uid, self.key, model, method, list(args), kw)

    def search_read(self, model, domain, fields, **kw):
        return self.call(model, "search_read", domain, fields=fields, **kw)

    def batches(self, model, domain, fields, size=1000):
        offset = 0
        while True:
            rows = self.search_read(model, domain, fields, limit=size, offset=offset, order="id")
            yield from rows
            if len(rows) < size:
                return
            offset += size


def connect(target):
    return Odoo(target)


def norm(code):
    return " ".join((code or "").split()).upper()


# Reads -----------------------------------------------------------------------

def sync_parts(conn, odoo):
    """All Odoo products with a code into parts; items linked to their product by Odoo code."""
    currency = (odoo.search_read("res.company", [], ["currency_id"], limit=1) or [{}])[0].get("currency_id")
    n, dupes = 0, []
    for p in odoo.batches("product.product", [["default_code", "!=", False]],
                          ["default_code", "name", "uom_id", "standard_price", "qty_available"]):
        vals = {"id": p["id"], "code": norm(p["default_code"]), "name": p["name"], "uom": p["uom_id"][1] if p["uom_id"] else "Units",
                "cost": p["standard_price"], "qty": p["qty_available"], "cur": currency and currency[1]}
        try:
            with conn.transaction():
                if conn.execute("""UPDATE parts SET code = %(code)s, mpn = CASE WHEN mpn = '' THEN %(name)s ELSE mpn END,
                                   description = %(name)s, uom = %(uom)s, cost = %(cost)s, cost_currency = %(cur)s, on_hand = %(qty)s,
                                   last_synced_at = now() WHERE odoo_product_id = %(id)s""", vals).rowcount == 0:
                    conn.execute("""INSERT INTO parts (odoo_product_id, code, mpn, description, uom, cost, cost_currency, on_hand, last_synced_at)
                                    VALUES (%(id)s, %(code)s, %(name)s, %(name)s, %(uom)s, %(cost)s, %(cur)s, %(qty)s, now())
                                    ON CONFLICT (code) DO UPDATE SET odoo_product_id = EXCLUDED.odoo_product_id,
                                      description = EXCLUDED.description, uom = EXCLUDED.uom, cost = EXCLUDED.cost,
                                      cost_currency = EXCLUDED.cost_currency, on_hand = EXCLUDED.on_hand, last_synced_at = now()""", vals)
            n += 1
        except Exception as e:  # noqa: BLE001 - one bad product must not stop the sync
            if "duplicate key" not in str(e):
                raise
            dupes.append(vals["code"])
    conn.execute("UPDATE parts SET image_url = '/api/v1/parts/' || id || '/image' WHERE odoo_product_id IS NOT NULL AND image_url IS NULL")
    linked = conn.execute("""UPDATE items i SET odoo_product_id = p.odoo_product_id FROM parts p
                             WHERE p.code = i.odoo_code AND i.odoo_product_id IS DISTINCT FROM p.odoo_product_id""").rowcount
    return {"products": n, "duplicate_codes": dupes, "items_linked": linked}


def sync_costs(conn, odoo):
    ids = [r["odoo_product_id"] for r in conn.execute("SELECT odoo_product_id FROM parts WHERE odoo_product_id IS NOT NULL")]
    n = 0
    for i in range(0, len(ids), 1000):
        for p in odoo.search_read("product.product", [["id", "in", ids[i:i + 1000]]], ["standard_price", "qty_available"]):
            n += conn.execute("UPDATE parts SET cost = %s, on_hand = %s, last_synced_at = now() WHERE odoo_product_id = %s",
                              (p["standard_price"], p["qty_available"], p["id"])).rowcount
    return {"parts": n}


def sync_boms(conn, odoo):
    """Odoo's current mrp.bom for each item, for the "in Odoo today" panel. Read only."""
    items = conn.execute("SELECT id, odoo_code FROM items WHERE odoo_code IS NOT NULL").fetchall()
    prods = {norm(p["default_code"]): p for p in odoo.search_read(
        "product.product", [["default_code", "in", [i["odoo_code"] for i in items]]], ["default_code", "product_tmpl_id"])}
    tmpl_ids = list({p["product_tmpl_id"][0] for p in prods.values()})
    heads = odoo.search_read("mrp.bom", [["product_tmpl_id", "in", tmpl_ids]], ["code", "product_tmpl_id", "active", "sequence"])
    ref = "x_ref_des" if "x_ref_des" in odoo.call("mrp.bom.line", "fields_get", ["x_ref_des"]) else None
    lines = odoo.search_read("mrp.bom.line", [["bom_id", "in", [h["id"] for h in heads]]],
                             ["bom_id", "product_id", "product_qty"] + ([ref] if ref else []))
    by_bom = {}
    for l in lines:
        name = l["product_id"][1]
        code = name[1:name.index("]")] if name.startswith("[") and "]" in name else ""
        by_bom.setdefault(l["bom_id"][0], []).append({"code": code, "name": name, "qty": l["product_qty"],
                                                      "ref_des": (l.get(ref) or "") if ref else ""})
    n = 0
    for it in items:
        p = prods.get(norm(it["odoo_code"]))
        mine = [{"id": h["id"], "code": h["code"] or f"BoM {h['id']}", "active": h["active"], "lines": by_bom.get(h["id"], [])}
                for h in heads if p and h["product_tmpl_id"][0] == p["product_tmpl_id"][0]]
        n += conn.execute("UPDATE items SET odoo_boms = %s WHERE id = %s", (Jsonb(mine), it["id"])).rowcount
    return {"items": n, "boms": len(heads)}


def image(target, odoo_product_id):
    rows = connect(target).call("product.product", "read", [odoo_product_id], fields=["image_128"])
    return rows and rows[0].get("image_128")


# Push ------------------------------------------------------------------------

def push_bom(conn, odoo, vid):
    """Check everything, create the new mrp.bom with all its lines in one call, then switch off the previous
    BOMTree BOM for that option. If switching off fails, the new BOM is removed again."""
    v = conn.execute("""SELECT v.id, v.version_no, v.state, b.option_code, b.is_default, i.code AS item_code, i.odoo_code
                        FROM bom_versions v JOIN boms b ON b.id = v.bom_id JOIN items i ON i.id = b.item_id
                        WHERE v.id = %s""", (vid,)).fetchone()
    if not v or v["state"] != "Released":
        raise OdooError("Only a Released version can be pushed")
    lines = conn.execute("""SELECT l.seq, l.qty, l.ref_des, p.code AS part_code, p.mpn, ci.code AS child_code, ci.odoo_code AS child_odoo_code,
                                   EXISTS (SELECT 1 FROM boms cb JOIN bom_versions cv ON cv.bom_id = cb.id
                                           WHERE cb.item_id = ci.id AND cb.is_default AND cv.state = 'Released') AS child_released
                            FROM bom_lines l LEFT JOIN parts p ON p.id = l.part_id LEFT JOIN items ci ON ci.id = l.child_item_id
                            WHERE l.bom_version_id = %s ORDER BY l.seq""", (vid,)).fetchall()
    missing = [] if v["odoo_code"] else [f"{v['item_code']}: the item has no Odoo code"]
    codes = {norm(v["odoo_code"])} if v["odoo_code"] else set()
    for l in lines:
        if l["child_code"]:
            if not l["child_odoo_code"]:
                missing.append(f"{l['child_code']}: sub-assembly has no Odoo code")
            if not l["child_released"]:
                missing.append(f"{l['child_code']}: sub-assembly has no Released version")
            l["code"] = norm(l["child_odoo_code"])
        else:
            if not l["part_code"]:
                missing.append(f"{l['mpn']}: part has no Odoo code")
            l["code"] = norm(l["part_code"])
        codes.add(l["code"])
    codes.discard("")
    found = {norm(p["default_code"]): p for p in odoo.search_read(
        "product.product", [["default_code", "in", sorted(codes)]], ["default_code", "product_tmpl_id", "uom_id"])}
    missing += [f"{c}: not found in Odoo {odoo.target}" for c in sorted(codes - found.keys())]
    if missing:
        raise OdooError(f"Push stopped: {len(missing)} codes missing", missing)

    head = found[norm(v["odoo_code"])]
    tag = f"BOMTree {v['option_code']} v{v['version_no']}"
    has_ref = "x_ref_des" in odoo.call("mrp.bom.line", "fields_get", ["x_ref_des"])
    request = {
        "product_tmpl_id": head["product_tmpl_id"][0], "product_id": head["id"], "code": tag, "product_qty": 1,
        "type": "normal", "sequence": 1 if v["is_default"] else 10,
        "bom_line_ids": [[0, 0, {"product_id": found[l["code"]]["id"], "product_qty": float(l["qty"]),
                                 "product_uom_id": found[l["code"]]["uom_id"][0], "sequence": l["seq"],
                                 **({"x_ref_des": l["ref_des"]} if has_ref and l["ref_des"] else {})}] for l in lines],
    }
    new_id = odoo.call("mrp.bom", "create", request)
    try:
        old = odoo.call("mrp.bom", "search", [["product_tmpl_id", "=", head["product_tmpl_id"][0]],
                                              ["code", "=like", f"BOMTree {v['option_code']} v%"], ["id", "!=", new_id]])
        if old:
            odoo.call("mrp.bom", "write", old, {"active": False})
    except Exception:
        odoo.call("mrp.bom", "unlink", [new_id])
        raise
    conn.execute("UPDATE bom_versions SET odoo_bom_id = %s WHERE id = %s", (new_id, vid))
    return request, {"odoo_bom_id": new_id, "code": tag, "lines": len(lines), "switched_off": old}
