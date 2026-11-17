"""Odoo push against a fake Odoo (always) and against Odoo staging (when ODOO_STAGING_TEST=1)."""
import os

import psycopg
import pytest
from psycopg.rows import dict_row

from app.integrations import odoo

TEST_URL = os.environ["DATABASE_URL"]


class FakeOdoo:
    target = "staging"

    def __init__(self, products, fail_on=None):
        self.products = products          # code -> id
        self.calls, self.fail_on = [], fail_on

    def search_read(self, model, domain, fields, **kw):
        assert model == "product.product"
        return [{"id": pid, "default_code": code, "product_tmpl_id": [pid + 1000, code], "uom_id": [1, "Units"]}
                for code, pid in self.products.items() if code in domain[0][2]]

    def call(self, model, method, *args, **kw):
        self.calls.append((model, method, args))
        if (model, method) == self.fail_on:
            raise RuntimeError("Odoo said no")
        return {"fields_get": {"x_ref_des": {}}, "create": 777, "search": [555], "write": True, "unlink": True}[method]


@pytest.fixture
def released():
    """A released version of an item with one part and one released sub-assembly."""
    with psycopg.connect(TEST_URL, row_factory=dict_row) as c, c.transaction(force_rollback=True):
        one = lambda q, *a: c.execute(q, a).fetchone()["id"]
        fam = one("INSERT INTO families (code) VALUES ('ODO') RETURNING id")
        top = one("INSERT INTO items (code, family_id, level, odoo_code) VALUES ('TOP-1', %s, 'FG', 'P100001') RETURNING id", fam)
        sub = one("INSERT INTO items (code, family_id, level, odoo_code) VALUES ('SUB-1', %s, 'SFG', 'P100002') RETURNING id", fam)
        part = one("INSERT INTO parts (code, mpn) VALUES ('P100003', 'RES') RETURNING id")
        vids = {}
        for item_id in (sub, top):
            b = one("INSERT INTO boms (item_id, option_code, is_default) VALUES (%s, 'BD', true) RETURNING id", item_id)
            vids[item_id] = one("INSERT INTO bom_versions (bom_id, version_no, created_by) VALUES (%s, 3, 1) RETURNING id", b)
        c.execute("INSERT INTO bom_lines (bom_version_id, seq, part_id, qty, ref_des) VALUES (%s, 1, %s, 2, 'R1 R2')", (vids[sub], part))
        c.execute("INSERT INTO bom_lines (bom_version_id, seq, part_id, qty, ref_des) VALUES (%s, 1, %s, 4, 'R9')", (vids[top], part))
        c.execute("INSERT INTO bom_lines (bom_version_id, seq, child_item_id, qty) VALUES (%s, 2, %s, 1)", (vids[top], sub))
        c.execute("UPDATE bom_versions SET state = 'Released' WHERE id = ANY(%s)", (list(vids.values()),))
        yield c, vids[top]


ALL = {"P100001": 1, "P100002": 2, "P100003": 3}


def test_push_creates_bom_and_switches_off_the_old_one(released):
    conn, vid = released
    fake = FakeOdoo(ALL)
    request, response = odoo.push_bom(conn, fake, vid)
    assert request["code"] == "BOMTree BD v3" and request["product_id"] == 1
    assert [l[2]["product_id"] for l in request["bom_line_ids"]] == [3, 2]
    assert request["bom_line_ids"][0][2]["x_ref_des"] == "R9"
    assert ("mrp.bom", "write", ([555], {"active": False})) in fake.calls
    assert conn.execute("SELECT odoo_bom_id FROM bom_versions WHERE id = %s", (vid,)).fetchone()["odoo_bom_id"] == 777


def test_missing_code_stops_before_anything_is_written(released):
    conn, vid = released
    fake = FakeOdoo({"P100001": 1, "P100002": 2})
    with pytest.raises(odoo.OdooError) as e:
        odoo.push_bom(conn, fake, vid)
    assert e.value.missing == ["P100003: not found in Odoo staging"]
    assert not [c for c in fake.calls if c[1] in ("create", "write")]


def test_failed_switch_off_removes_the_new_bom(released):
    conn, vid = released
    fake = FakeOdoo(ALL, fail_on=("mrp.bom", "write"))
    with pytest.raises(RuntimeError):
        odoo.push_bom(conn, fake, vid)
    assert ("mrp.bom", "unlink", ([777],)) in fake.calls


def test_only_released_versions_are_pushed(released):
    conn, vid = released
    conn.execute("UPDATE bom_versions SET state = 'Superseded' WHERE id = %s", (vid,))
    with pytest.raises(odoo.OdooError):
        odoo.push_bom(conn, FakeOdoo(ALL), vid)


@pytest.mark.skipif(os.environ.get("ODOO_STAGING_TEST") != "1", reason="set ODOO_STAGING_TEST=1 and ODOO_STAGING_* to run against Odoo staging")
def test_staging_missing_code_stops_cleanly(released):
    conn, vid = released
    with pytest.raises(odoo.OdooError) as e:
        odoo.push_bom(conn, odoo.connect("staging"), vid)   # P1000xx codes don't exist in Odoo
    assert e.value.missing
