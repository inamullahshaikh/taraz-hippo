-- =====================================================================
-- BOMTree · PostgreSQL schema  (file 3 of 3)
-- App roles and permissions (RBAC), database logins, grants, system users.
-- Safe to run again: it only adds what is missing and re-applies grants.
-- =====================================================================

BEGIN;
SET search_path = bomtree, hippo, public;

-- ---------------------------------------------------------------------
-- 1. App roles, permissions and who holds what
-- ---------------------------------------------------------------------

INSERT INTO roles (code, name, description) VALUES
  ('admin',        'Admin',        'Runs the app: users, roles, families, settings, Odoo link'),
  ('family_owner', 'Family owner', 'Approves and releases BOMs, sends them to Odoo'),
  ('designer',     'Designer',     'Creates and edits drafts, imports DipTrace, raises change requests'),
  ('reviewer',     'Reviewer',     'Checks drafts and approves, rejects or asks for changes'),
  ('planner',      'Planner',      'Reads everything, sees cost and shortage, exports, comments'),
  ('viewer',       'Viewer',       'Reads released BOMs only')
ON CONFLICT (code) DO UPDATE SET name = excluded.name, description = excluded.description;

INSERT INTO permissions (code, description) VALUES
  ('bom.read_released', 'See released BOMs, tree, map, where used'),
  ('bom.read_draft',    'See drafts and versions under review'),
  ('cost.read',         'See cost and shortage'),
  ('export.create',     'Export to Excel or Google Sheet'),
  ('comment.create',    'Comment'),
  ('cr.create',         'Raise a change request'),
  ('item.create',       'Create an item'),
  ('bom.edit',          'Create or edit a draft BOM'),
  ('import.run',        'Import a DipTrace BOM'),
  ('bom.submit',        'Send a draft for review'),
  ('version.restore',   'Restore an old version as a new draft'),
  ('version.approve',   'Approve or reject a version'),
  ('version.release',   'Release a version'),
  ('odoo.push',         'Push a released version to Odoo'),
  ('cr.approve',        'Approve a change request'),
  ('item.archive',      'Archive an item or BOM'),
  ('audit.read',        'Read the change history'),
  ('admin.manage',      'Manage users, roles, families, settings')
ON CONFLICT (code) DO UPDATE SET description = excluded.description;

-- The permission matrix. scope 'global' = applies in every family.
DELETE FROM role_permissions;
INSERT INTO role_permissions (role_code, perm_code, scope)
SELECT r, p, CASE WHEN p IN ('bom.read_released','cost.read','export.create','comment.create','cr.create')
                    OR (r = 'planner' AND p = 'bom.read_draft')
                  THEN 'global' ELSE 'family' END
FROM (VALUES
  ('admin','bom.read_released'),('admin','bom.read_draft'),('admin','cost.read'),('admin','export.create'),
  ('admin','comment.create'),('admin','cr.create'),('admin','item.create'),('admin','bom.edit'),
  ('admin','import.run'),('admin','bom.submit'),('admin','version.restore'),('admin','version.approve'),
  ('admin','version.release'),('admin','odoo.push'),('admin','cr.approve'),('admin','item.archive'),
  ('admin','audit.read'),('admin','admin.manage'),

  ('family_owner','bom.read_released'),('family_owner','bom.read_draft'),('family_owner','cost.read'),
  ('family_owner','export.create'),('family_owner','comment.create'),('family_owner','cr.create'),
  ('family_owner','item.create'),('family_owner','bom.edit'),('family_owner','import.run'),
  ('family_owner','bom.submit'),('family_owner','version.restore'),('family_owner','version.approve'),
  ('family_owner','version.release'),('family_owner','odoo.push'),('family_owner','cr.approve'),
  ('family_owner','item.archive'),('family_owner','audit.read'),

  ('designer','bom.read_released'),('designer','bom.read_draft'),('designer','cost.read'),
  ('designer','export.create'),('designer','comment.create'),('designer','cr.create'),
  ('designer','item.create'),('designer','bom.edit'),('designer','import.run'),('designer','bom.submit'),
  ('designer','version.restore'),

  ('reviewer','bom.read_released'),('reviewer','bom.read_draft'),('reviewer','cost.read'),
  ('reviewer','export.create'),('reviewer','comment.create'),('reviewer','cr.create'),
  ('reviewer','version.approve'),

  ('planner','bom.read_released'),('planner','bom.read_draft'),('planner','cost.read'),
  ('planner','export.create'),('planner','comment.create'),('planner','cr.create'),

  ('viewer','bom.read_released')
) AS m(r, p);

-- System users: the migration, the Odoo sync and Blue act under these names
INSERT INTO users (email, name, is_system) VALUES
  ('migration@bomtree.local', 'Sheets migration', true),
  ('odoo-sync@bomtree.local', 'Odoo sync',        true),
  ('blue@bomtree.local',      'Blue assistant',   true)
ON CONFLICT (email) DO NOTHING;

-- ---------------------------------------------------------------------
-- 2. Database logins and what each one may do
--    The roles are created without a password. Give them one with:
--      ALTER ROLE bomtree_app      LOGIN PASSWORD '...';
--      ALTER ROLE bomtree_readonly LOGIN PASSWORD '...';
-- ---------------------------------------------------------------------

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bomtree_app')      THEN CREATE ROLE bomtree_app NOLOGIN;      END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bomtree_readonly') THEN CREATE ROLE bomtree_readonly NOLOGIN; END IF;
END $$;

REVOKE ALL ON SCHEMA bomtree FROM PUBLIC;
GRANT USAGE ON SCHEMA bomtree TO bomtree_app, bomtree_readonly;

-- read-only login (reports, Metabase, backups checks)
GRANT SELECT ON ALL TABLES IN SCHEMA bomtree TO bomtree_readonly;
GRANT EXECUTE ON FUNCTION has_perm(bigint, text, bigint), explode(text, numeric, text),
                          parts_needed(text, numeric), item_descendants(bigint) TO bomtree_readonly;

-- the app's login: read and write, but never delete history
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA bomtree TO bomtree_app;
GRANT DELETE ON bom_lines, user_roles, comments, attachments TO bomtree_app;   -- rules in file 2 still apply
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log, migration_runs FROM bomtree_app;
REVOKE INSERT, UPDATE ON roles, permissions, role_permissions FROM bomtree_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bomtree TO bomtree_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA bomtree TO bomtree_app;

COMMIT;
