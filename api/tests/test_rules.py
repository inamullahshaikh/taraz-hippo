"""The rules the database enforces itself, even if the app has a bug."""
import os

import psycopg
import pytest

TEST_URL = os.environ["DATABASE_URL"]
SYSTEM = 1   # the seeded migration user


@pytest.fixture
def db():
    with psycopg.connect(TEST_URL) as c, c.transaction(force_rollback=True):
        yield c


def one(db, sql, *args):
    return db.execute(sql, args).fetchone()[0]


def item(db, code):
    fam = one(db, "INSERT INTO families (code) VALUES (%s) RETURNING id", "F-" + code)
    return one(db, "INSERT INTO items (code, family_id, level) VALUES (%s, %s, 'SFG') RETURNING id", code, fam)


def version(db, item_id):
    b = one(db, "INSERT INTO boms (item_id, is_default) VALUES (%s, true) RETURNING id", item_id)
    return one(db, "INSERT INTO bom_versions (bom_id, version_no, created_by) VALUES (%s, 1, %s) RETURNING id", b, SYSTEM)


def part(db, code):
    return one(db, "INSERT INTO parts (code) VALUES (%s) RETURNING id", code)


LINE = "INSERT INTO bom_lines (bom_version_id, seq, part_id, child_item_id, qty) VALUES (%s, 1, %s, %s, %s)"


def line(db, v, part_id=None, child=None, qty=1):
    db.execute(LINE, (v, part_id, child, qty))


def refused(db, sql, *args):
    with pytest.raises(psycopg.Error), db.transaction():
        db.execute(sql, args)


def test_line_needs_a_part_or_an_item_and_qty_above_zero(db):
    v, p, b = version(db, item(db, "A")), part(db, "P1"), item(db, "B")
    line(db, v, p)
    refused(db, LINE, v, p, None, 0)
    refused(db, LINE, v, None, None, 1)
    refused(db, LINE, v, p, b, 1)


def test_released_version_is_frozen(db):
    v, p = version(db, item(db, "A")), part(db, "P1")
    line(db, v, p)
    db.execute("UPDATE bom_versions SET state = 'Released' WHERE id = %s", (v,))
    refused(db, LINE, v, p, None, 1)
    refused(db, "UPDATE bom_lines SET qty = 2 WHERE bom_version_id = %s", v)
    refused(db, "DELETE FROM bom_lines WHERE bom_version_id = %s", v)
    refused(db, "UPDATE bom_versions SET change_note = 'x' WHERE id = %s", v)
    refused(db, "UPDATE bom_versions SET state = 'Draft' WHERE id = %s", v)
    refused(db, "DELETE FROM bom_versions WHERE id = %s", v)
    db.execute("UPDATE bom_versions SET odoo_bom_id = 42 WHERE id = %s", (v,))   # recording the Odoo push is allowed
    db.execute("UPDATE bom_versions SET state = 'Superseded' WHERE id = %s", (v,))


def test_one_open_and_one_released_version_per_bom(db):
    v = version(db, item(db, "A"))
    new = "INSERT INTO bom_versions (bom_id, version_no, created_by, state) SELECT bom_id, %s, 1, %s FROM bom_versions WHERE id = %s"
    refused(db, new, 2, "Draft", v)
    db.execute("UPDATE bom_versions SET state = 'Released' WHERE id = %s", (v,))
    refused(db, new, 2, "Released", v)


def test_bom_cannot_contain_itself(db):
    a, b = item(db, "A"), item(db, "B")
    va, vb = version(db, a), version(db, b)
    line(db, va, child=b)
    refused(db, LINE, vb, None, a, 1)   # B -> A -> B
    refused(db, LINE, va, None, a, 1)   # A -> A


def test_exactly_one_default_bom(db):
    i = item(db, "A")
    version(db, i)
    refused(db, "INSERT INTO boms (item_id, option_code, is_default) VALUES (%s, 'BC', true)", i)
    with pytest.raises(psycopg.Error), db.transaction():
        db.execute("UPDATE boms SET is_default = false WHERE item_id = %s", (i,))
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_unique_codes_ignore_case(db):
    item(db, "PCBA-X")
    refused(db, "INSERT INTO items (code, family_id, level) SELECT 'pcba-x', family_id, 'SFG' FROM items WHERE code = 'PCBA-X'")


def test_nothing_deleted_and_every_change_audited(db):
    db.execute("SELECT set_config('app.source', 'App', true)")
    i = item(db, "A")
    refused(db, "DELETE FROM items WHERE id = %s", i)
    assert db.execute("SELECT action, source, after->>'code' FROM audit_log WHERE entity_type = 'items' AND entity_id = %s",
                      (i,)).fetchone() == ("insert", "App", "A")
    db.execute("UPDATE items SET archived_at = now() WHERE id = %s", (i,))
    assert one(db, "SELECT action FROM audit_log WHERE entity_type = 'items' AND entity_id = %s ORDER BY id DESC LIMIT 1", i) == "archive"
    refused(db, "UPDATE audit_log SET action = 'x'")
    refused(db, "DELETE FROM audit_log")


def test_four_eyes(db):
    u = one(db, "INSERT INTO users (email) VALUES ('maker@taraztechnologies.com') RETURNING id")
    v = version(db, item(db, "A"))
    db.execute("UPDATE bom_versions SET created_by = %s WHERE id = %s", (u, v))
    refused(db, "UPDATE bom_versions SET approved_by = %s WHERE id = %s", u, v)
    refused(db, "INSERT INTO approvals (bom_version_id, reviewer_id, decision) VALUES (%s, %s, 'Approve')", v, u)
