-- =====================================================================
-- BOMTree · PostgreSQL schema  (file 5, after files 1-3; safe to run again)
-- What the BOMTree page needs to edit the database the way it edited the sheets.
--
--  * Removing an item or BOM in the page hides it (deleted_at) but keeps it, with its versions,
--    for history and for the BOMs that used it.
--  * Saving a BOM in the page makes a new version and releases it at once, recorded under the
--    person who saved it (release_direct). The draft -> review -> release flow stays available.
--  * The app login may replace the rows of the tables that only mirror sheet tabs.
-- =====================================================================

BEGIN;
SET search_path = bomtree, hippo, public;

ALTER TABLE items ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE boms  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

-- version rules: as in file 2, plus Draft -> Released inside release_direct
CREATE OR REPLACE FUNCTION trg_version_rules() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_old jsonb; v_new jsonb;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.state <> 'Draft' THEN
      RAISE EXCEPTION 'Only a draft can be deleted (this one is %)', OLD.state USING ERRCODE = 'BT409';
    END IF;
    RETURN OLD;
  END IF;

  IF NEW.state <> OLD.state THEN
    IF NOT (   (OLD.state = 'Draft'     AND NEW.state = 'In review')
            OR (OLD.state = 'In review' AND NEW.state IN ('Draft','Approved'))
            OR (OLD.state = 'Approved'  AND NEW.state IN ('Released','Draft'))
            OR (OLD.state = 'Released'  AND NEW.state = 'Superseded')
            OR (OLD.state = 'Draft'     AND NEW.state = 'Released'
                AND (NEW.migrated OR current_setting('bomtree.direct', true) = 'on')) ) THEN
      RAISE EXCEPTION 'A version cannot go from % to %', OLD.state, NEW.state USING ERRCODE = 'BT409';
    END IF;
  ELSIF OLD.state IN ('Released','Superseded') THEN
    -- frozen: only the Odoo link may still be filled in
    v_old := to_jsonb(OLD) - 'odoo_bom_id' - 'updated_at';
    v_new := to_jsonb(NEW) - 'odoo_bom_id' - 'updated_at';
    IF v_old <> v_new THEN
      RAISE EXCEPTION 'A % version cannot be changed', OLD.state USING ERRCODE = 'BT409';
    END IF;
  END IF;
  RETURN NEW;
END $$;

-- A draft saved in the page becomes the released version at once; the old one is superseded.
CREATE OR REPLACE FUNCTION release_direct(p_user bigint, p_version_id bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_ver bom_versions%ROWTYPE;
BEGIN
  PERFORM require_perm(p_user, 'bom.edit', family_of_version(p_version_id));
  SELECT * INTO v_ver FROM bom_versions WHERE id = p_version_id FOR UPDATE;
  IF v_ver.state IS DISTINCT FROM 'Draft' THEN
    RAISE EXCEPTION 'Only a draft can be released from the page (this one is %)', v_ver.state USING ERRCODE = 'BT409';
  END IF;
  PERFORM 1 FROM boms WHERE id = v_ver.bom_id FOR UPDATE;
  PERFORM set_config('bomtree.direct', 'on', true);
  UPDATE bom_versions SET state = 'Superseded' WHERE bom_id = v_ver.bom_id AND state = 'Released';
  UPDATE bom_versions SET state = 'Released', released_by = p_user, released_at = now(),
                          change_note = coalesce(change_note, 'Saved in the BOMTree page')
  WHERE id = p_version_id;
  PERFORM set_config('bomtree.direct', 'off', true);
  UPDATE boms SET current_version_id = p_version_id WHERE id = v_ver.bom_id;
END $$;

GRANT DELETE ON legacy_lines, placements, design_refs, design_nets, conversions, board_library, canvases,
                old_tools, alternates, alternate_searches, design_blocks, design_issues, blocks TO bomtree_app;
GRANT EXECUTE ON FUNCTION release_direct(bigint, bigint) TO bomtree_app;

COMMIT;
