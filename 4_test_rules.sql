-- =====================================================================
-- BOMTree · rule tests (optional)
-- Proves the safety rules and transactions work on a loaded database:
-- rights per role and family, the save lock, four eyes, frozen released
-- versions, no cycles, audit log, and what the app login may do.
--
-- Run it after a migration (staging or a copy):
--   psql "$BOMTREE_DB" -f 4_test_rules.sql
-- Everything runs inside one transaction that is rolled back at the end,
-- so the test users and drafts it makes are never saved.
-- Expected last line:  42 passed, 0 failed
-- =====================================================================
\set ON_ERROR_STOP 1
BEGIN;
SET search_path = bomtree, public;
SET client_min_messages = warning;
CREATE TEMP TABLE results (n serial, test text, ok boolean, detail text);
CREATE FUNCTION pg_temp.expect(p_test text, p_sql text, p_code text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    EXECUTE p_sql;
    INSERT INTO results (test, ok, detail) VALUES (p_test, p_code IS NULL, 'ran without error');
  EXCEPTION WHEN OTHERS THEN
    INSERT INTO results (test, ok, detail) VALUES (p_test, SQLSTATE = p_code, SQLSTATE || ': ' || SQLERRM);
  END;
END $$;
CREATE FUNCTION pg_temp.check(p_test text, p_ok boolean, p_detail text DEFAULT '') RETURNS void LANGUAGE sql AS $$
  INSERT INTO results (test, ok, detail) VALUES (p_test, p_ok, p_detail) $$;

-- people
SELECT add_user('admin@t.test','Admin','admin') AS admin \gset
SELECT add_user('designer@t.test','Designer USM','designer','USM') AS des \gset
SELECT add_user('designer2@t.test','Designer GDX','designer','GDX') AS des2 \gset
SELECT add_user('reviewer@t.test','Reviewer USM','reviewer','USM') AS rev \gset
SELECT add_user('owner@t.test','Owner USM','family_owner','USM') AS own \gset
SELECT add_user('planner@t.test','Planner','planner') AS pla \gset
SELECT add_user('viewer@t.test','Viewer','viewer') AS vie \gset

SELECT b.id AS bom, b.current_version_id AS v1 FROM boms b WHERE legacy_key = 'PCBA_SMD-USM · BD' \gset
SELECT id AS fg FROM items WHERE code = 'USM-3IV' \gset

-- rights
SELECT pg_temp.check('planner reads drafts in any family', has_perm(:pla, 'bom.read_draft', (SELECT id FROM families WHERE code='GDX')));
SELECT pg_temp.check('viewer cannot read drafts', NOT has_perm(:vie, 'bom.read_draft', (SELECT id FROM families WHERE code='USM')));
SELECT pg_temp.check('USM designer cannot edit GDX', NOT has_perm(:des, 'bom.edit', (SELECT id FROM families WHERE code='GDX')));
SELECT pg_temp.check('USM designer can export everywhere (global)', has_perm(:des, 'export.create', (SELECT id FROM families WHERE code='GDX')));
SELECT pg_temp.expect('viewer cannot open a draft', format('SELECT start_draft(%s,%s)', :vie, :bom), 'BT403');
SELECT pg_temp.expect('GDX designer cannot open a USM draft', format('SELECT start_draft(%s,%s)', :des2, :bom), 'BT403');

-- draft, save, optimistic lock
SELECT begin_action(:des, 'App');
SELECT start_draft(:des, :bom) AS v2 \gset
SELECT pg_temp.check('draft copies all released lines',
  (SELECT count(*) FROM bom_lines WHERE bom_version_id = :v2) = (SELECT count(*) FROM bom_lines WHERE bom_version_id = :v1));
SELECT pg_temp.expect('second open draft refused', format('SELECT start_draft(%s,%s)', :des, :bom), 'BT409');
SELECT jsonb_agg(jsonb_build_object('part_code', p.code, 'item_code', ci.code, 'qty', l.qty, 'ref_des', l.ref_des, 'note', l.note) - CASE WHEN p.code IS NULL THEN 'part_code' ELSE 'item_code' END ORDER BY l.seq) AS lines
FROM bom_lines l LEFT JOIN parts p ON p.id=l.part_id LEFT JOIN items ci ON ci.id=l.child_item_id WHERE bom_version_id = :v2 \gset
SELECT save_draft_lines(:des, :v2, 0, (:'lines'::jsonb) || '[{"part_code":"P015326","qty":2,"ref_des":"C900 C901","note":"test line"}]'::jsonb) AS lock1 \gset
SELECT pg_temp.check('save returns the next lock', :lock1 = 1);
SELECT pg_temp.check('saved line count', (SELECT count(*) FROM bom_lines WHERE bom_version_id=:v2) = jsonb_array_length(:'lines'::jsonb) + 1);
SELECT pg_temp.expect('stale lock refused (someone saved first)', format('SELECT save_draft_lines(%s,%s,0,%L::jsonb)', :des, :v2, :'lines'), 'BT409');
SELECT pg_temp.expect('qty 0 refused', format('SELECT save_draft_lines(%s,%s,1,%L::jsonb)', :des, :v2, '[{"part_code":"P015326","qty":0}]'), 'BT422');
SELECT pg_temp.expect('unknown part refused', format('SELECT save_draft_lines(%s,%s,1,%L::jsonb)', :des, :v2, '[{"part_code":"P999999","qty":1}]'), 'BT422');
SELECT pg_temp.expect('cycle refused (SMD board containing its own FG)', format('SELECT save_draft_lines(%s,%s,1,%L::jsonb)', :des, :v2, '[{"item_code":"USM-3IV","qty":1}]'), 'BT422');
SELECT pg_temp.check('failed saves left the draft untouched',
  (SELECT count(*) FROM bom_lines WHERE bom_version_id=:v2) = jsonb_array_length(:'lines'::jsonb) + 1
  AND (SELECT lock_version FROM bom_versions WHERE id=:v2) = 1);

-- review and release
SELECT submit_version(:des, :v2);
SELECT pg_temp.expect('designer cannot approve', format('SELECT review_version(%s,%s,%L)', :des, :v2, 'Approve'), 'BT403');
SELECT pg_temp.expect('lines locked while in review', format('UPDATE bom_lines SET qty = 5 WHERE bom_version_id = %s', :v2), 'BT409');
SELECT review_version(:rev, :v2, 'Approve', 'looks right');
SELECT pg_temp.expect('reviewer cannot release', format('SELECT release_version(%s,%s)', :rev, :v2), 'BT403');
SELECT release_version(:own, :v2);
SELECT pg_temp.check('v2 released, v1 superseded, BOM points at v2',
  (SELECT state FROM bom_versions WHERE id=:v2)='Released' AND (SELECT state FROM bom_versions WHERE id=:v1)='Superseded'
  AND (SELECT current_version_id FROM boms WHERE id=:bom)=:v2);
SELECT pg_temp.check('new line visible in current lines', EXISTS (SELECT 1 FROM v_current_lines WHERE bom_id=:bom AND ref_des='C900 C901'));
SELECT pg_temp.expect('released lines frozen', format('DELETE FROM bom_lines WHERE bom_version_id = %s', :v2), 'BT409');
SELECT pg_temp.expect('released version frozen', format('UPDATE bom_versions SET change_note = %L WHERE id = %s', 'x', :v2), 'BT409');
SELECT pg_temp.expect('superseded cannot come back', format('UPDATE bom_versions SET state = %L WHERE id = %s', 'Released', :v1), 'BT409');
SELECT pg_temp.check('odoo link can still be filled on a released version', true);
UPDATE bom_versions SET odoo_bom_id = 4242 WHERE id = :v2;

-- four eyes: the owner makes and submits a draft, then tries to approve it
SELECT start_draft(:own, :bom) AS v3 \gset
SELECT submit_version(:own, :v3);
SELECT pg_temp.expect('maker cannot approve own version (four eyes)', format('SELECT review_version(%s,%s,%L)', :own, :v3, 'Approve'), 'BT422');
SELECT review_version(:rev, :v3, 'Ask changes', 'fix C900');
SELECT pg_temp.check('ask changes sends it back to Draft', (SELECT state FROM bom_versions WHERE id=:v3)='Draft');
SELECT pg_temp.expect('a draft with review history cannot be deleted', format('DELETE FROM bom_versions WHERE id = %s', :v3), '23503');
SELECT submit_version(:own, :v3);
SELECT review_version(:rev, :v3, 'Approve');
SELECT release_version(:own, :v3);
SELECT pg_temp.check('v3 released, v2 superseded', (SELECT state FROM bom_versions WHERE id=:v3)='Released' AND (SELECT state FROM bom_versions WHERE id=:v2)='Superseded');
SELECT pg_temp.expect('a released version cannot be deleted', format('DELETE FROM bom_versions WHERE id = %s', :v2), 'BT409');

-- restore an old version as a draft
SELECT restore_version(:des, :v1) AS v4 \gset
SELECT pg_temp.check('restore copies the old lines',
  (SELECT count(*) FROM bom_lines WHERE bom_version_id=:v4) = (SELECT count(*) FROM bom_lines WHERE bom_version_id=:v1)
  AND (SELECT restored_from_id FROM bom_versions WHERE id=:v4) = :v1);
DELETE FROM bom_versions WHERE id = :v4;   -- a fresh draft can be thrown away
SELECT pg_temp.check('fresh draft and its lines deleted', NOT EXISTS (SELECT 1 FROM bom_lines WHERE bom_version_id=:v4));

-- change request
SELECT raise_change_request(:pla, :bom, 'Swap U1 to TLE4284', 'Stock') AS cr \gset
SELECT pg_temp.expect('designer cannot approve a change request', format('SELECT decide_change_request(%s,%s,true)', :des, :cr), 'BT403');
SELECT decide_change_request(:own, :cr, true) AS v5 \gset
SELECT pg_temp.check('approved request opens a linked draft',
  (SELECT change_request_id FROM bom_versions WHERE id=:v5) = :cr AND (SELECT state FROM change_requests WHERE id=:cr)='Approved');

-- archive
SELECT pg_temp.expect('cannot archive an item still used in a BOM',
  format('SELECT archive_item(%s,(SELECT id FROM items WHERE code=%L))', :own, 'PCBA_SMD-USM'), 'BT409');

-- default BOM
SELECT pg_temp.expect('two default BOMs for one item refused',
  format('UPDATE boms SET is_default = true WHERE item_id = (SELECT item_id FROM boms WHERE id=%s)', :bom), '23505');

-- explode
SELECT pg_temp.check('explode USM-3IV x10 reaches the SMD parts',
  EXISTS (SELECT 1 FROM explode('USM-3IV', 10) WHERE level >= 2 AND NOT is_subassembly),
  (SELECT count(*)::text || ' rows, max depth ' || max(level) FROM explode('USM-3IV', 10)));

-- audit
SELECT pg_temp.check('every action is in the audit log with the user',
  (SELECT count(*) FROM audit_log WHERE user_id = :des AND action = 'save lines') = 1
  AND EXISTS (SELECT 1 FROM audit_log WHERE entity_type='bom_versions' AND entity_id=:v2 AND after ? 'state'));
SELECT pg_temp.expect('audit rows cannot be changed', 'UPDATE audit_log SET detail = ''x''', 'BT403');
SELECT pg_temp.expect('audit rows cannot be deleted', 'DELETE FROM audit_log', 'BT403');

-- database login for the app
GRANT ALL ON results TO bomtree_app; GRANT ALL ON SEQUENCE results_n_seq TO bomtree_app;
SET ROLE bomtree_app;
SELECT pg_temp.expect('app login cannot delete items', 'DELETE FROM bomtree.items WHERE false', '42501');
SELECT pg_temp.expect('app login cannot truncate the audit log', 'TRUNCATE bomtree.audit_log', '42501');
SELECT pg_temp.expect('app login can read', 'SELECT count(*) FROM bomtree.items', NULL);
RESET ROLE;

SELECT CASE WHEN ok THEN 'PASS' ELSE 'FAIL' END || '  ' || test || CASE WHEN ok THEN '' ELSE '   <- ' || detail END FROM results ORDER BY n;
SELECT count(*) FILTER (WHERE ok) || ' passed, ' || count(*) FILTER (WHERE NOT ok) || ' failed' FROM results;

ROLLBACK;   -- nothing from this test is kept
