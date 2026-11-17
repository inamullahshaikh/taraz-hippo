"""Tests run against a throwaway bomtree_test database, built fresh from the repo's SQL files 1-3 and 5.

Set DATABASE_URL to any Postgres 15+ server you may create databases on, e.g.
  docker run -d --name bt-testdb -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:18
  DATABASE_URL=postgresql://postgres:x@localhost:55432/postgres pytest
"""
import os
from pathlib import Path

import psycopg
import pytest

# The 30 Oct tests are for the old 18-table schema; they come back as their code is ported (step 2: editing).
collect_ignore = ["test_api.py", "test_odoo.py", "test_rbac.py", "test_rules.py"]

REPO = Path(__file__).parent.parent.parent
BASE = os.environ["DATABASE_URL"]
TEST_URL = BASE.rsplit("/", 1)[0] + "/bomtree_test"
SERVER = BASE.split("@", 1)[1].rsplit("/", 1)[0]          # host:port
# before the app is imported: settings are read once
os.environ.update(DATABASE_URL=TEST_URL, DEV_LOGIN="1", ENVIRONMENT="local", GOOGLE_CLIENT_ID="test-client",
                  MS_CLIENT_ID="", COMPANY_DOMAINS="taraz.tech",
                  INDEX_SHEET_ID="sheet-index", BLUEPRINTS_SHEET_ID="sheet-blueprints",
                  **{f"HIPPO_{lvl.upper()}_URL": f"postgresql://hippo_{lvl}:x@{SERVER}/bomtree_test"
                     for lvl in ("read", "write", "admin")})


def pytest_sessionstart(session):
    with psycopg.connect(BASE, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS bomtree_test WITH (FORCE)")
        c.execute("CREATE DATABASE bomtree_test")
    with psycopg.connect(TEST_URL, autocommit=True) as c:
        for f in ("1_schema.sql", "2_rules_and_transactions.sql", "3_security_and_seed.sql", "5_page_editing.sql",
                  "6_hippo.sql"):
            c.execute((REPO / f).read_text("utf-8"))
        for lvl in ("read", "write", "admin"):
            c.execute(f"ALTER ROLE hippo_{lvl} LOGIN PASSWORD 'x'")


SEED = """
SET search_path = bomtree, hippo, public;
SELECT set_config('bomtree.migration', 'on', false), set_config('bomtree.source', 'Migration', false);
INSERT INTO families (code, name, legacy_sheet_id) VALUES ('USM', 'USM', 'sheet-usm');
INSERT INTO items (code, prefix, family_id, level, odoo_code) VALUES
  ('USM-3IV', 'FG', 1, 'FG', 'P016569'), ('PCBA-USM', 'PCBA', 1, 'SFG', NULL), ('PackA-USM', 'PackA', 1, 'SFG', NULL);
INSERT INTO parts (code, mpn) VALUES ('P000001', 'R1K');
INSERT INTO boms (item_id, option_code, legacy_key, is_default, status) VALUES
  (1, 'Standard', 'USM-3IV', true, 'Active'), (2, 'Standard', 'PCBA-USM', true, 'Active'),
  (3, 'Standard', 'PackA-USM', true, 'Under review');
INSERT INTO bom_versions (bom_id, version_no, state, migrated, created_by)
  SELECT b, 1, 'Draft', true, (SELECT id FROM users WHERE email = 'migration@bomtree.local') FROM generate_series(1, 3) b;
INSERT INTO bom_lines (bom_version_id, seq, child_item_id, part_id, component_label, qty, ref_des) VALUES
  (1, 1, 2, NULL, 'PCBA-USM', 1, NULL), (2, 1, NULL, 1, 'R1K', 2.5, 'R1 R2'), (3, 1, NULL, 1, 'R1K', 4, NULL);
UPDATE bom_versions SET state = 'Released', released_at = now() WHERE id IN (1, 2);
UPDATE boms SET current_version_id = id;
SELECT add_user('admin@taraz.tech', 'Admin', 'admin'), add_user('guest@partner.org', 'Guest', 'viewer'),
       add_user('gone@partner.org', 'Gone', 'viewer'), add_user('reader@taraz.tech', 'Reader', 'viewer'),
       add_user('writer@taraz.tech', 'Writer', 'viewer');
UPDATE users SET active = false WHERE email = 'gone@partner.org';
INSERT INTO hippo.access (user_id, level)
SELECT id, CASE email WHEN 'admin@taraz.tech' THEN 'admin' WHEN 'reader@taraz.tech' THEN 'read' ELSE 'write' END
FROM users WHERE email IN ('admin@taraz.tech', 'reader@taraz.tech', 'writer@taraz.tech');
"""


@pytest.fixture(scope="session")
def client():
    with psycopg.connect(TEST_URL, autocommit=True) as c:
        c.execute(SEED)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as cl:
        yield cl


