-- =====================================================================
-- BOMTree · PostgreSQL schema  (file 2 of 3)
-- Safety rules (triggers) and the transactions the app calls.
--
-- How the app uses this file, for every change a person makes:
--
--   BEGIN;
--   SELECT bomtree.begin_action(<user id>, 'App');      -- who is acting
--   SELECT bomtree.save_draft_lines(<user id>, <version id>, <lock>, '<lines json>');
--   COMMIT;
--
-- Each function below does one whole job (check rights, check rules,
-- change the rows, write the audit row). If anything fails, PostgreSQL
-- undoes the whole transaction, so a BOM is never left half-saved.
--
-- Error codes the functions raise (the app turns them into messages):
--   BT403  no permission          BT404  not found
--   BT409  conflict: someone else saved first, or the state does not allow it
--   BT422  a rule failed (qty, cycle, four eyes, unknown part ...)
-- =====================================================================

BEGIN;
SET search_path = bomtree, public;

-- ---------------------------------------------------------------------
-- 1. Who is acting (set once per transaction)
-- ---------------------------------------------------------------------

CREATE FUNCTION begin_action(p_user_id bigint, p_source text DEFAULT 'App')
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM users WHERE id = p_user_id AND active) THEN
    RAISE EXCEPTION 'User % does not exist or is switched off', p_user_id USING ERRCODE = 'BT403';
  END IF;
  PERFORM set_config('bomtree.user_id', p_user_id::text, true);   -- true = this transaction only
  PERFORM set_config('bomtree.source',  p_source,         true);
END $$;

CREATE FUNCTION current_actor() RETURNS bigint LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('bomtree.user_id', true), '')::bigint
$$;

CREATE FUNCTION current_source() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT coalesce(nullif(current_setting('bomtree.source', true), ''), 'Database')
$$;

-- ---------------------------------------------------------------------
-- 2. Permissions
-- ---------------------------------------------------------------------

-- true when the user holds the permission for this family
-- (or for all families, or the permission is a global one)
CREATE FUNCTION has_perm(p_user_id bigint, p_perm text, p_family_id bigint DEFAULT NULL)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1
    FROM users u
    JOIN user_roles ur       ON ur.user_id = u.id
    JOIN role_permissions rp ON rp.role_code = ur.role_code AND rp.perm_code = p_perm
    WHERE u.id = p_user_id AND u.active
      AND ( rp.scope = 'global'
            OR ur.family_id IS NULL
            OR ur.family_id = p_family_id )
  )
$$;

CREATE FUNCTION require_perm(p_user_id bigint, p_perm text, p_family_id bigint DEFAULT NULL)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF NOT has_perm(p_user_id, p_perm, p_family_id) THEN
    RAISE EXCEPTION 'No permission: % (family %)', p_perm, coalesce(p_family_id::text, 'any')
      USING ERRCODE = 'BT403';
  END IF;
END $$;

-- family of a BOM version, a BOM or an item (used by the rights checks)
CREATE FUNCTION family_of_version(p_version_id bigint) RETURNS bigint LANGUAGE sql STABLE AS $$
  SELECT i.family_id FROM bom_versions v JOIN boms b ON b.id = v.bom_id JOIN items i ON i.id = b.item_id
  WHERE v.id = p_version_id
$$;
CREATE FUNCTION family_of_bom(p_bom_id bigint) RETURNS bigint LANGUAGE sql STABLE AS $$
  SELECT i.family_id FROM boms b JOIN items i ON i.id = b.item_id WHERE b.id = p_bom_id
$$;

-- ---------------------------------------------------------------------
-- 3. Audit: every insert, update and delete is written to audit_log
-- ---------------------------------------------------------------------

CREATE FUNCTION trg_audit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_before jsonb; v_after jsonb; v_id bigint; v_label text; k text;
BEGIN
  -- the migration script writes one summary row instead of one per record
  IF current_setting('bomtree.migration', true) = 'on' THEN RETURN NULL; END IF;
  -- save_draft_lines writes one row for the whole line list
  IF TG_TABLE_NAME = 'bom_lines' AND current_setting('bomtree.lines_audited', true) = 'on' THEN RETURN NULL; END IF;

  IF TG_OP IN ('UPDATE','DELETE') THEN v_before := to_jsonb(OLD); END IF;
  IF TG_OP IN ('INSERT','UPDATE') THEN v_after  := to_jsonb(NEW); END IF;

  IF TG_OP = 'UPDATE' THEN                     -- keep only the columns that changed
    FOR k IN SELECT jsonb_object_keys(v_after) LOOP
      IF v_before -> k IS NOT DISTINCT FROM v_after -> k THEN
        v_before := v_before - k; v_after := v_after - k;
      END IF;
    END LOOP;
    v_before := v_before - 'updated_at'; v_after := v_after - 'updated_at';
    IF v_after = '{}'::jsonb THEN RETURN NULL; END IF;   -- nothing really changed
  END IF;

  v_id    := coalesce((v_after ->> 'id')::bigint, (to_jsonb(coalesce(NEW, OLD)) ->> 'id')::bigint);
  v_label := coalesce(to_jsonb(coalesce(NEW, OLD)) ->> 'code',
                      to_jsonb(coalesce(NEW, OLD)) ->> 'legacy_key',
                      to_jsonb(coalesce(NEW, OLD)) ->> 'email');

  INSERT INTO audit_log (user_id, source, action, entity_type, entity_id, entity_label, before, after)
  VALUES (current_actor(), current_source(), lower(TG_OP), TG_TABLE_NAME, v_id, v_label, v_before, v_after);
  RETURN NULL;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['users','user_roles','families','blocks','items','parts','boms',
                           'bom_versions','bom_lines','change_requests','approvals','conversions',
                           'alternates','board_library','canvases','old_tools','design_issues',
                           'design_blocks','attachments']
  LOOP
    EXECUTE format('CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON %I
                    FOR EACH ROW EXECUTE FUNCTION trg_audit()', t);
  END LOOP;
END $$;

-- The audit log can only grow: no one may change or remove a row
CREATE FUNCTION trg_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log rows can never be changed or removed' USING ERRCODE = 'BT403';
END $$;
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION trg_audit_append_only();
CREATE TRIGGER append_only_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION trg_audit_append_only();

-- ---------------------------------------------------------------------
-- 4. updated_at is set automatically
-- ---------------------------------------------------------------------

CREATE FUNCTION trg_touch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['users','families','items','parts','boms','bom_versions',
                           'change_requests','imports']
  LOOP
    EXECUTE format('CREATE TRIGGER touch BEFORE UPDATE ON %I
                    FOR EACH ROW EXECUTE FUNCTION trg_touch()', t);
  END LOOP;
END $$;

-- ---------------------------------------------------------------------
-- 5. BOM rules
-- ---------------------------------------------------------------------

-- 5a. Only a Draft version's lines can be added, changed or removed.
CREATE FUNCTION trg_lines_only_in_draft() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_state text;
BEGIN
  SELECT state INTO v_state FROM bom_versions
  WHERE id = coalesce(NEW.bom_version_id, OLD.bom_version_id);
  -- a draft that is being thrown away takes its lines with it
  -- (the version row is already gone; version_rules only lets drafts be deleted)
  IF TG_OP = 'DELETE' AND v_state IS NULL THEN RETURN OLD; END IF;
  IF v_state IS DISTINCT FROM 'Draft' THEN
    RAISE EXCEPTION 'Lines of a % version cannot be changed. Start a new draft instead.', v_state
      USING ERRCODE = 'BT409';
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.bom_version_id <> OLD.bom_version_id THEN
    RAISE EXCEPTION 'A line cannot move to another version' USING ERRCODE = 'BT422';
  END IF;
  RETURN coalesce(NEW, OLD);
END $$;
CREATE TRIGGER lines_only_in_draft BEFORE INSERT OR UPDATE OR DELETE ON bom_lines
  FOR EACH ROW EXECUTE FUNCTION trg_lines_only_in_draft();

-- 5b. Every item a given item contains, at any depth
--     (through the current version and any open draft of each of its BOMs).
CREATE FUNCTION item_descendants(p_item_id bigint)
RETURNS TABLE (item_id bigint) LANGUAGE sql STABLE AS $$
  WITH RECURSIVE tree(item_id) AS (
    SELECT l.child_item_id
    FROM boms b
    JOIN bom_versions v ON v.bom_id = b.id AND v.state <> 'Superseded'
    JOIN bom_lines l    ON l.bom_version_id = v.id AND l.child_item_id IS NOT NULL
    WHERE b.item_id = p_item_id
    UNION
    SELECT l.child_item_id
    FROM tree t
    JOIN boms b         ON b.item_id = t.item_id
    JOIN bom_versions v ON v.bom_id = b.id AND v.state <> 'Superseded'
    JOIN bom_lines l    ON l.bom_version_id = v.id AND l.child_item_id IS NOT NULL
  )
  SELECT item_id FROM tree
$$;

-- 5c. A BOM can never contain itself, directly or through a sub-assembly.
CREATE FUNCTION trg_no_cycles() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_parent bigint; v_parent_code text; v_child_code text;
BEGIN
  IF NEW.child_item_id IS NULL THEN RETURN NEW; END IF;
  SELECT b.item_id INTO v_parent FROM bom_versions v JOIN boms b ON b.id = v.bom_id
  WHERE v.id = NEW.bom_version_id;
  IF NEW.child_item_id = v_parent
     OR EXISTS (SELECT 1 FROM item_descendants(NEW.child_item_id) d WHERE d.item_id = v_parent) THEN
    SELECT code INTO v_parent_code FROM items WHERE id = v_parent;
    SELECT code INTO v_child_code  FROM items WHERE id = NEW.child_item_id;
    RAISE EXCEPTION '% cannot use % : % already contains %', v_parent_code, v_child_code, v_child_code, v_parent_code
      USING ERRCODE = 'BT422';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER no_cycles BEFORE INSERT OR UPDATE OF child_item_id ON bom_lines
  FOR EACH ROW EXECUTE FUNCTION trg_no_cycles();

-- 5d. Allowed version state changes. Released and Superseded versions are frozen.
CREATE FUNCTION trg_version_rules() RETURNS trigger LANGUAGE plpgsql AS $$
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
            OR (OLD.state = 'Draft'     AND NEW.state = 'Released' AND NEW.migrated) ) THEN
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
CREATE TRIGGER version_rules BEFORE UPDATE OR DELETE ON bom_versions
  FOR EACH ROW EXECUTE FUNCTION trg_version_rules();

-- ---------------------------------------------------------------------
-- 6. The transactions the app calls
-- ---------------------------------------------------------------------

-- 6a. New item
CREATE FUNCTION create_item(p_user bigint, p_code text, p_prefix text, p_family_code text,
                            p_level text, p_description text DEFAULT NULL,
                            p_built_by text DEFAULT NULL, p_odoo_code text DEFAULT NULL,
                            p_notes text DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_family bigint; v_id bigint;
BEGIN
  SELECT id INTO v_family FROM families WHERE code = p_family_code AND archived_at IS NULL;
  IF v_family IS NULL THEN RAISE EXCEPTION 'Family % not found', p_family_code USING ERRCODE = 'BT404'; END IF;
  PERFORM require_perm(p_user, 'item.create', v_family);
  IF EXISTS (SELECT 1 FROM items WHERE code = btrim(p_code)) THEN
    RAISE EXCEPTION 'Item % already exists', p_code USING ERRCODE = 'BT409';
  END IF;
  INSERT INTO items (code, prefix, family_id, level, description, built_by, odoo_code, notes)
  VALUES (btrim(p_code), p_prefix, v_family, p_level, p_description, p_built_by, nullif(btrim(p_odoo_code),''), p_notes)
  RETURNING id INTO v_id;
  RETURN v_id;
END $$;

-- 6b. New BOM option for an item; it starts as an empty v1 draft
CREATE FUNCTION create_bom(p_user bigint, p_item_id bigint, p_option text DEFAULT 'Standard',
                           p_is_default boolean DEFAULT NULL, p_when_to_use text DEFAULT NULL,
                           p_notes text DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_family bigint; v_bom bigint; v_ver bigint; v_default boolean;
BEGIN
  SELECT family_id INTO v_family FROM items WHERE id = p_item_id AND archived_at IS NULL;
  IF v_family IS NULL THEN RAISE EXCEPTION 'Item % not found', p_item_id USING ERRCODE = 'BT404'; END IF;
  PERFORM require_perm(p_user, 'bom.edit', v_family);
  -- the first BOM of an item is its default
  v_default := coalesce(p_is_default,
                        NOT EXISTS (SELECT 1 FROM boms WHERE item_id = p_item_id AND archived_at IS NULL));
  IF v_default THEN
    UPDATE boms SET is_default = false WHERE item_id = p_item_id AND is_default;
  END IF;
  INSERT INTO boms (item_id, option_code, is_default, when_to_use, notes, status)
  VALUES (p_item_id, p_option, v_default, p_when_to_use, p_notes, 'Under review')
  RETURNING id INTO v_bom;
  INSERT INTO bom_versions (bom_id, version_no, state, created_by)
  VALUES (v_bom, 1, 'Draft', p_user) RETURNING id INTO v_ver;
  UPDATE boms SET current_version_id = v_ver WHERE id = v_bom;
  RETURN v_bom;
END $$;

-- 6c. Open the next draft of a released BOM (copies the released lines)
--     open_draft does the work with no rights check; it is only called by the
--     functions below, after they have checked rights themselves.
CREATE FUNCTION open_draft(p_user bigint, p_bom_id bigint, p_change_request_id bigint DEFAULT NULL,
                           p_copy_from_version bigint DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_src bigint; v_no int; v_new bigint;
BEGIN
  PERFORM 1 FROM boms WHERE id = p_bom_id AND archived_at IS NULL FOR UPDATE;   -- one draft at a time per BOM
  IF NOT FOUND THEN RAISE EXCEPTION 'BOM % not found or archived', p_bom_id USING ERRCODE = 'BT404'; END IF;
  IF EXISTS (SELECT 1 FROM bom_versions WHERE bom_id = p_bom_id AND state IN ('Draft','In review','Approved')) THEN
    RAISE EXCEPTION 'This BOM already has an open draft' USING ERRCODE = 'BT409';
  END IF;
  v_src := coalesce(p_copy_from_version,
                    (SELECT id FROM bom_versions WHERE bom_id = p_bom_id AND state = 'Released'));
  SELECT coalesce(max(version_no), 0) + 1 INTO v_no FROM bom_versions WHERE bom_id = p_bom_id;
  INSERT INTO bom_versions (bom_id, version_no, state, created_by, change_request_id, restored_from_id)
  VALUES (p_bom_id, v_no, 'Draft', p_user, p_change_request_id,
          CASE WHEN p_copy_from_version IS NOT NULL THEN p_copy_from_version END)
  RETURNING id INTO v_new;
  IF v_src IS NOT NULL THEN
    PERFORM set_config('bomtree.lines_audited', 'on', true);
    INSERT INTO bom_lines (bom_version_id, seq, part_id, child_item_id, component_label, qty, uom, ref_des, note)
    SELECT v_new, seq, part_id, child_item_id, component_label, qty, uom, ref_des, note
    FROM bom_lines WHERE bom_version_id = v_src;
    PERFORM set_config('bomtree.lines_audited', 'off', true);
  END IF;
  RETURN v_new;
END $$;

CREATE FUNCTION start_draft(p_user bigint, p_bom_id bigint) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_family bigint;
BEGIN
  v_family := family_of_bom(p_bom_id);
  IF v_family IS NULL THEN RAISE EXCEPTION 'BOM % not found', p_bom_id USING ERRCODE = 'BT404'; END IF;
  PERFORM require_perm(p_user, 'bom.edit', v_family);
  RETURN open_draft(p_user, p_bom_id);
END $$;

-- 6d. Save the full line list of a draft.
--     p_lines is a JSON array; each line gives the component one of these ways:
--       {"part_code":"P015326", "qty":23, "ref_des":"C1 C2", "note":"..."}
--       {"part_id":123, ...}
--       {"item_code":"PCBA_SMD-USM", "qty":1}          (a sub-assembly)
--       {"child_item_id":45, ...}
--     p_expected_lock is the lock_version the person loaded. If someone else
--     saved in between, nothing is saved and BT409 is raised.
--     Returns the new lock_version.
CREATE FUNCTION save_draft_lines(p_user bigint, p_version_id bigint, p_expected_lock integer, p_lines jsonb)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
  v_ver bom_versions%ROWTYPE; v_family bigint; v_before jsonb; v_after jsonb;
  ln jsonb; v_seq int := 0; v_part bigint; v_child bigint; v_label text; v_qty numeric;
BEGIN
  SELECT * INTO v_ver FROM bom_versions WHERE id = p_version_id FOR UPDATE;   -- lock the version row
  IF NOT FOUND THEN RAISE EXCEPTION 'Version % not found', p_version_id USING ERRCODE = 'BT404'; END IF;
  v_family := family_of_version(p_version_id);
  PERFORM require_perm(p_user, 'bom.edit', v_family);
  IF v_ver.state <> 'Draft' THEN
    RAISE EXCEPTION 'Only a draft can be saved (this version is %)', v_ver.state USING ERRCODE = 'BT409';
  END IF;
  IF v_ver.lock_version <> p_expected_lock THEN
    RAISE EXCEPTION 'Someone else saved this BOM first. Reload it and make your change again.' USING ERRCODE = 'BT409';
  END IF;
  IF jsonb_typeof(p_lines) <> 'array' THEN
    RAISE EXCEPTION 'Lines must be a JSON array' USING ERRCODE = 'BT422';
  END IF;

  SELECT coalesce(jsonb_agg(to_jsonb(l) - 'id' - 'bom_version_id' ORDER BY seq), '[]'::jsonb)
    INTO v_before FROM bom_lines l WHERE bom_version_id = p_version_id;

  PERFORM set_config('bomtree.lines_audited', 'on', true);
  DELETE FROM bom_lines WHERE bom_version_id = p_version_id;

  FOR ln IN SELECT * FROM jsonb_array_elements(p_lines) LOOP
    v_seq := v_seq + 1; v_part := NULL; v_child := NULL;
    v_qty := (ln ->> 'qty')::numeric;
    IF v_qty IS NULL OR v_qty <= 0 THEN
      RAISE EXCEPTION 'Line %: qty must be above 0', v_seq USING ERRCODE = 'BT422';
    END IF;
    IF ln ? 'child_item_id' OR ln ? 'item_code' THEN
      SELECT id, code INTO v_child, v_label FROM items
      WHERE (id = (ln ->> 'child_item_id')::bigint OR code = ln ->> 'item_code') AND archived_at IS NULL;
      IF v_child IS NULL THEN
        RAISE EXCEPTION 'Line %: sub-assembly % not found', v_seq, coalesce(ln ->> 'item_code', ln ->> 'child_item_id')
          USING ERRCODE = 'BT422';
      END IF;
    ELSE
      SELECT id, mpn INTO v_part, v_label FROM parts
      WHERE id = (ln ->> 'part_id')::bigint OR code = ln ->> 'part_code';
      IF v_part IS NULL THEN
        RAISE EXCEPTION 'Line %: part % not found. Sync parts from Odoo first.', v_seq,
          coalesce(ln ->> 'part_code', ln ->> 'part_id', '(none given)') USING ERRCODE = 'BT422';
      END IF;
    END IF;
    INSERT INTO bom_lines (bom_version_id, seq, part_id, child_item_id, component_label, qty, uom, ref_des, note)
    VALUES (p_version_id, v_seq, v_part, v_child, coalesce(ln ->> 'component_label', v_label), v_qty,
            coalesce(ln ->> 'uom', 'Units'), nullif(btrim(ln ->> 'ref_des'), ''), nullif(btrim(ln ->> 'note'), ''));
  END LOOP;
  PERFORM set_config('bomtree.lines_audited', 'off', true);

  SELECT coalesce(jsonb_agg(to_jsonb(l) - 'id' - 'bom_version_id' ORDER BY seq), '[]'::jsonb)
    INTO v_after FROM bom_lines l WHERE bom_version_id = p_version_id;

  UPDATE bom_versions SET lock_version = lock_version + 1 WHERE id = p_version_id;

  INSERT INTO audit_log (user_id, source, action, entity_type, entity_id, entity_label, before, after, detail)
  SELECT p_user, current_source(), 'save lines', 'bom_versions', p_version_id,
         b.legacy_key, v_before, v_after, format('%s lines saved', v_seq)
  FROM boms b WHERE b.id = v_ver.bom_id;

  RETURN v_ver.lock_version + 1;
END $$;

-- 6e. Draft → In review
CREATE FUNCTION submit_version(p_user bigint, p_version_id bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_state text;
BEGIN
  PERFORM require_perm(p_user, 'bom.submit', family_of_version(p_version_id));
  SELECT state INTO v_state FROM bom_versions WHERE id = p_version_id FOR UPDATE;
  IF v_state IS DISTINCT FROM 'Draft' THEN
    RAISE EXCEPTION 'Only a draft can be sent for review' USING ERRCODE = 'BT409';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM bom_lines WHERE bom_version_id = p_version_id) THEN
    RAISE EXCEPTION 'An empty BOM cannot be sent for review' USING ERRCODE = 'BT422';
  END IF;
  UPDATE bom_versions SET state = 'In review', submitted_by = p_user, submitted_at = now()
  WHERE id = p_version_id;
END $$;

-- 6f. Review: Approve, Reject or Ask changes (four eyes: never the maker)
CREATE FUNCTION review_version(p_user bigint, p_version_id bigint, p_decision text, p_comment text DEFAULT NULL)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_ver bom_versions%ROWTYPE;
BEGIN
  PERFORM require_perm(p_user, 'version.approve', family_of_version(p_version_id));
  SELECT * INTO v_ver FROM bom_versions WHERE id = p_version_id FOR UPDATE;
  IF v_ver.state IS DISTINCT FROM 'In review' THEN
    RAISE EXCEPTION 'This version is not waiting for review' USING ERRCODE = 'BT409';
  END IF;
  IF p_user IN (v_ver.created_by, v_ver.submitted_by) THEN
    RAISE EXCEPTION 'The person who made a version cannot review it' USING ERRCODE = 'BT422';
  END IF;
  IF p_decision NOT IN ('Approve','Reject','Ask changes') THEN
    RAISE EXCEPTION 'Decision must be Approve, Reject or Ask changes' USING ERRCODE = 'BT422';
  END IF;
  INSERT INTO approvals (bom_version_id, reviewer_id, decision, comment) VALUES (p_version_id, p_user, p_decision, p_comment);
  IF p_decision = 'Approve' THEN
    UPDATE bom_versions SET state = 'Approved', approved_by = p_user, approved_at = now() WHERE id = p_version_id;
  ELSE
    UPDATE bom_versions SET state = 'Draft' WHERE id = p_version_id;
  END IF;
END $$;

-- 6g. Approved → Released. The old released version becomes Superseded.
CREATE FUNCTION release_version(p_user bigint, p_version_id bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_ver bom_versions%ROWTYPE;
BEGIN
  PERFORM require_perm(p_user, 'version.release', family_of_version(p_version_id));
  SELECT * INTO v_ver FROM bom_versions WHERE id = p_version_id FOR UPDATE;
  IF v_ver.state IS DISTINCT FROM 'Approved' THEN
    RAISE EXCEPTION 'Only an approved version can be released' USING ERRCODE = 'BT409';
  END IF;
  PERFORM 1 FROM boms WHERE id = v_ver.bom_id FOR UPDATE;
  UPDATE bom_versions SET state = 'Superseded' WHERE bom_id = v_ver.bom_id AND state = 'Released';
  UPDATE bom_versions SET state = 'Released', released_by = p_user, released_at = now() WHERE id = p_version_id;
  UPDATE boms SET current_version_id = p_version_id, status = 'Active' WHERE id = v_ver.bom_id;
  IF v_ver.change_request_id IS NOT NULL THEN
    UPDATE change_requests SET state = 'Done' WHERE id = v_ver.change_request_id AND state = 'Approved';
  END IF;
END $$;

-- 6h. Bring back an old version as a new draft
CREATE FUNCTION restore_version(p_user bigint, p_old_version_id bigint) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_bom bigint;
BEGIN
  SELECT bom_id INTO v_bom FROM bom_versions WHERE id = p_old_version_id;
  IF v_bom IS NULL THEN RAISE EXCEPTION 'Version % not found', p_old_version_id USING ERRCODE = 'BT404'; END IF;
  PERFORM require_perm(p_user, 'version.restore', family_of_bom(v_bom));
  RETURN open_draft(p_user, v_bom, NULL, p_old_version_id);
END $$;

-- 6i. Make a BOM the default for its item (the old default stops being default)
CREATE FUNCTION set_default_bom(p_user bigint, p_bom_id bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_item bigint;
BEGIN
  PERFORM require_perm(p_user, 'bom.edit', family_of_bom(p_bom_id));
  SELECT item_id INTO v_item FROM boms WHERE id = p_bom_id AND archived_at IS NULL FOR UPDATE;
  IF v_item IS NULL THEN RAISE EXCEPTION 'BOM % not found', p_bom_id USING ERRCODE = 'BT404'; END IF;
  UPDATE boms SET is_default = false WHERE item_id = v_item AND is_default AND id <> p_bom_id;
  UPDATE boms SET is_default = true  WHERE id = p_bom_id;
END $$;

-- 6j. Archive (never delete) an item with all its BOMs
CREATE FUNCTION archive_item(p_user bigint, p_item_id bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_family bigint; v_users text;
BEGIN
  SELECT family_id INTO v_family FROM items WHERE id = p_item_id AND archived_at IS NULL FOR UPDATE;
  IF v_family IS NULL THEN RAISE EXCEPTION 'Item % not found or already archived', p_item_id USING ERRCODE = 'BT404'; END IF;
  PERFORM require_perm(p_user, 'item.archive', v_family);
  SELECT string_agg(DISTINCT used_in_item, ', ') INTO v_users FROM v_where_used WHERE child_item_id = p_item_id;
  IF v_users IS NOT NULL THEN
    RAISE EXCEPTION 'Still used in: %. Remove it from those BOMs first.', v_users USING ERRCODE = 'BT409';
  END IF;
  UPDATE boms  SET status = 'Archived', archived_at = now(), is_default = false WHERE item_id = p_item_id AND archived_at IS NULL;
  UPDATE items SET status = 'Archived', archived_at = now() WHERE id = p_item_id;
END $$;

-- 6k. Change requests
CREATE FUNCTION raise_change_request(p_user bigint, p_bom_id bigint, p_reason text,
                                     p_impact text DEFAULT NULL, p_priority text DEFAULT 'Normal')
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_id bigint;
BEGIN
  PERFORM require_perm(p_user, 'cr.create', family_of_bom(p_bom_id));
  INSERT INTO change_requests (number, bom_id, reason, impact, priority, requested_by)
  VALUES ('CR-' || lpad(nextval('change_request_no_seq')::text, 4, '0'), p_bom_id, p_reason, p_impact, p_priority, p_user)
  RETURNING id INTO v_id;
  RETURN v_id;
END $$;

-- approve → opens the next draft linked to the request; returns that draft's id
CREATE FUNCTION decide_change_request(p_user bigint, p_cr_id bigint, p_approve boolean)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_cr change_requests%ROWTYPE; v_ver bigint;
BEGIN
  SELECT * INTO v_cr FROM change_requests WHERE id = p_cr_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Change request % not found', p_cr_id USING ERRCODE = 'BT404'; END IF;
  PERFORM require_perm(p_user, 'cr.approve', family_of_bom(v_cr.bom_id));
  IF v_cr.state <> 'Open' THEN RAISE EXCEPTION 'This request is already %', v_cr.state USING ERRCODE = 'BT409'; END IF;
  UPDATE change_requests SET state = CASE WHEN p_approve THEN 'Approved' ELSE 'Rejected' END,
         decided_by = p_user, decided_at = now() WHERE id = p_cr_id;
  IF p_approve THEN
    -- the draft is opened in the requester's name, so the approver can still review it later
    v_ver := open_draft(v_cr.requested_by, v_cr.bom_id, p_cr_id);
  END IF;
  RETURN v_ver;
END $$;

-- 6l. Users and roles
CREATE FUNCTION add_user(p_email text, p_name text, p_role text DEFAULT 'viewer', p_family_code text DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_id bigint; v_family bigint;
BEGIN
  INSERT INTO users (email, name) VALUES (p_email, p_name)
  ON CONFLICT (email) DO UPDATE SET name = excluded.name, active = true
  RETURNING id INTO v_id;
  IF p_family_code IS NOT NULL THEN
    SELECT id INTO v_family FROM families WHERE code = p_family_code;
    IF v_family IS NULL THEN RAISE EXCEPTION 'Family % not found', p_family_code USING ERRCODE = 'BT404'; END IF;
  END IF;
  INSERT INTO user_roles (user_id, role_code, family_id, granted_by)
  VALUES (v_id, p_role, v_family, current_actor()) ON CONFLICT DO NOTHING;
  RETURN v_id;
END $$;

CREATE FUNCTION grant_role(p_admin bigint, p_user bigint, p_role text, p_family_code text DEFAULT NULL)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_family bigint;
BEGIN
  PERFORM require_perm(p_admin, 'admin.manage');
  IF p_family_code IS NOT NULL THEN SELECT id INTO v_family FROM families WHERE code = p_family_code; END IF;
  INSERT INTO user_roles (user_id, role_code, family_id, granted_by)
  VALUES (p_user, p_role, v_family, p_admin) ON CONFLICT DO NOTHING;
END $$;

CREATE FUNCTION revoke_role(p_admin bigint, p_user bigint, p_role text, p_family_code text DEFAULT NULL)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  PERFORM require_perm(p_admin, 'admin.manage');
  DELETE FROM user_roles
  WHERE user_id = p_user AND role_code = p_role
    AND family_id IS NOT DISTINCT FROM (SELECT id FROM families WHERE code = p_family_code);
END $$;

-- ---------------------------------------------------------------------
-- 7. Reading helpers
-- ---------------------------------------------------------------------

-- Full multi-level BOM of an item (default BOM at every level), with
-- quantities multiplied out. Example:
--   SELECT * FROM bomtree.explode('USM-3IV', 10);
CREATE FUNCTION explode(p_item_code text, p_qty numeric DEFAULT 1, p_option text DEFAULT NULL)
RETURNS TABLE (level int, path text, component text, code text, is_subassembly boolean,
               qty_each numeric, qty_total numeric, ref_des text)
LANGUAGE sql STABLE AS $$
  WITH RECURSIVE x AS (
    SELECT 1 AS level, i.code::text AS path, l.child_item_id, l.part_id, l.qty AS qty_each,
           l.qty * p_qty AS qty_total, l.ref_des
    FROM items i
    JOIN boms b        ON b.item_id = i.id AND b.archived_at IS NULL
                      AND (b.option_code = p_option OR (p_option IS NULL AND b.is_default))
    JOIN bom_lines l   ON l.bom_version_id = b.current_version_id
    WHERE i.code = p_item_code
    UNION ALL
    SELECT x.level + 1, x.path || ' > ' || ci.code, l.child_item_id, l.part_id, l.qty,
           l.qty * x.qty_total, l.ref_des
    FROM x
    JOIN items ci      ON ci.id = x.child_item_id
    JOIN boms b        ON b.item_id = ci.id AND b.is_default AND b.archived_at IS NULL
    JOIN bom_lines l   ON l.bom_version_id = b.current_version_id
    WHERE x.level < 20
  )
  SELECT x.level, x.path, coalesce(ci.code, p.mpn), coalesce(p.code, ci.odoo_code),
         x.child_item_id IS NOT NULL, x.qty_each, x.qty_total, x.ref_des
  FROM x LEFT JOIN items ci ON ci.id = x.child_item_id LEFT JOIN parts p ON p.id = x.part_id
  ORDER BY x.path, x.level
$$;

-- Total of each bought part needed to build p_qty of an item
CREATE FUNCTION parts_needed(p_item_code text, p_qty numeric DEFAULT 1)
RETURNS TABLE (code text, component text, qty_total numeric)
LANGUAGE sql STABLE AS $$
  SELECT code, component, sum(qty_total) FROM explode(p_item_code, p_qty)
  WHERE NOT is_subassembly GROUP BY code, component ORDER BY code
$$;

-- ---------------------------------------------------------------------
-- 8. Every function always works inside the bomtree schema, whatever the
--    caller's search_path is (also stops anyone slipping in a look-alike table).
-- ---------------------------------------------------------------------
DO $$
DECLARE f regprocedure;
BEGIN
  FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'bomtree'
  LOOP
    EXECUTE format('ALTER FUNCTION %s SET search_path = bomtree, public', f);
  END LOOP;
END $$;

COMMIT;
