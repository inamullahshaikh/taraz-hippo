-- =====================================================================
-- Hippo · Taraz's shared database  (file 6, after files 1-5; safe to run again)
--
-- BOMTree becomes one part of Hippo. People move to the shared schema "hippo",
-- next to who may do what in Hippo and the change log. Run as the database owner
-- (a superuser: the change log uses an event trigger):
--   psql "$HIPPO_DB" -v ON_ERROR_STOP=1 -f 6_hippo.sql
--
-- Three database logins, one per level. The Hippo connector runs each person's SQL
-- as the login of their level, so Postgres itself enforces what they may do:
--   hippo_read   reads every table
--   hippo_write  also adds, changes and removes rows (not people, rights or the log)
--   hippo_admin  owns the tables, so it may also change structure: tables, columns,
--                constraints, indexes, views, functions, triggers, schemas, grants
-- They are created without a password. Give them one with:
--   ALTER ROLE hippo_read  LOGIN PASSWORD '...';
--   ALTER ROLE hippo_write LOGIN PASSWORD '...';
--   ALTER ROLE hippo_admin LOGIN PASSWORD '...';
-- =====================================================================

BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hippo_read')  THEN CREATE ROLE hippo_read  NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hippo_write') THEN CREATE ROLE hippo_write NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hippo_admin') THEN CREATE ROLE hippo_admin NOLOGIN; END IF;
END $$;
-- the connector opens one connection per call: a runaway caller can't use up the server's connections
ALTER ROLE hippo_read  CONNECTION LIMIT 20;
ALTER ROLE hippo_write CONNECTION LIMIT 20;
ALTER ROLE hippo_admin CONNECTION LIMIT 20;

CREATE SCHEMA IF NOT EXISTS hippo;   -- owned by the database owner, so not even an admin can drop the change log

-- ---------------------------------------------------------------------
-- 1. People are Hippo's, not only BOMTree's
-- ---------------------------------------------------------------------

DO $$
BEGIN
  IF to_regclass('bomtree.users') IS NOT NULL THEN
    ALTER TABLE bomtree.users SET SCHEMA hippo;        -- keys, triggers and grants come along
  END IF;
END $$;

-- BOMTree's functions find people in hippo now (and keep ignoring the caller's search_path)
DO $$
DECLARE f regprocedure;
BEGIN
  FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'bomtree'
  LOOP
    EXECUTE format('ALTER FUNCTION %s SET search_path = bomtree, hippo, public', f);
  END LOOP;
END $$;

-- Each person's Hippo level. No row = no Hippo access (BOMTree roles are separate, in bomtree.user_roles).
CREATE TABLE IF NOT EXISTS hippo.access (
  user_id    bigint PRIMARY KEY REFERENCES hippo.users ON DELETE CASCADE,
  level      text NOT NULL CHECK (level IN ('read', 'write', 'admin')),
  granted_by bigint REFERENCES hippo.users,
  granted_at timestamptz NOT NULL DEFAULT now()
);

-- Every change made through Hippo's connector or People page, and every structure change however it was made.
-- Rows can only be added: no level may change or remove them. db_login is always the real database login.
CREATE TABLE IF NOT EXISTS hippo.change_log (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at         timestamptz NOT NULL DEFAULT now(),
  who        text NOT NULL,                       -- the person's email, or the database login
  db_login   text NOT NULL DEFAULT session_user,
  command    text NOT NULL,                       -- UPDATE 3, CREATE TABLE ..., or 'people'
  objects    text,                                -- what a structure change touched, when Postgres says
  statement  text NOT NULL,
  reason     text,
  books_note text                                 -- the Books note that records the decision
);
CREATE INDEX IF NOT EXISTS change_log_at_idx ON hippo.change_log (at DESC);

-- ---------------------------------------------------------------------
-- 2. hippo_admin owns BOMTree and the people tables, so it may change their structure
-- ---------------------------------------------------------------------

DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.oid::regclass AS obj,
                  CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'S' THEN 'SEQUENCE'
                                 ELSE 'TABLE' END AS kind
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE (n.nspname = 'bomtree' OR c.oid IN ('hippo.users'::regclass, 'hippo.access'::regclass))
             AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
             -- sequences of identity columns follow their table
             AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                                                         AND d.deptype IN ('a', 'i'))
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO hippo_admin', r.kind, r.obj);
  END LOOP;
  FOR r IN SELECT p.oid::regprocedure AS f FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'bomtree'
  LOOP
    EXECUTE format('ALTER ROUTINE %s OWNER TO hippo_admin', r.f);
  END LOOP;
  EXECUTE format('GRANT CREATE ON DATABASE %I TO hippo_admin', current_database());   -- new schemas: costs, stock ...
END $$;
ALTER SCHEMA bomtree OWNER TO hippo_admin;

-- ---------------------------------------------------------------------
-- 3. What each level may do
-- ---------------------------------------------------------------------

GRANT USAGE ON SCHEMA hippo TO hippo_read, hippo_write, bomtree_app, bomtree_readonly;
GRANT USAGE, CREATE ON SCHEMA hippo TO hippo_admin;
GRANT USAGE ON SCHEMA bomtree TO hippo_read, hippo_write;

GRANT SELECT ON ALL TABLES IN SCHEMA bomtree, hippo TO hippo_read, hippo_write, hippo_admin;
GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA bomtree TO hippo_write;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bomtree TO hippo_write;
-- people, their rights and the history are changed by admins (People page) or by the app's own rules
REVOKE INSERT, UPDATE, DELETE ON bomtree.user_roles, bomtree.roles, bomtree.permissions, bomtree.role_permissions
  FROM hippo_write;
REVOKE UPDATE, DELETE ON bomtree.audit_log, bomtree.migration_runs FROM hippo_write;
-- the connector logs each change as the person's level; when and which login are always filled in by Postgres
REVOKE INSERT ON hippo.change_log FROM hippo_write, hippo_admin;
GRANT INSERT (who, command, objects, statement, reason, books_note) ON hippo.change_log TO hippo_write, hippo_admin;

-- the BOMTree app only reads Hippo levels, and of people it only records sign-ins: who someone is (email, name,
-- switched on or off) changes only in the People page. Otherwise a BOMTree admin's SQL could take over an account.
GRANT SELECT ON hippo.access TO bomtree_app, bomtree_readonly;
GRANT SELECT ON hippo.users TO bomtree_readonly;
REVOKE INSERT, UPDATE ON hippo.users FROM bomtree_app;
GRANT UPDATE (last_login_at) ON hippo.users TO bomtree_app;

-- whatever admins create later is readable by read and writable by write
ALTER DEFAULT PRIVILEGES FOR ROLE hippo_admin GRANT USAGE ON SCHEMAS TO hippo_read, hippo_write;
ALTER DEFAULT PRIVILEGES FOR ROLE hippo_admin GRANT SELECT ON TABLES TO hippo_read, hippo_write;
ALTER DEFAULT PRIVILEGES FOR ROLE hippo_admin GRANT INSERT, UPDATE, DELETE ON TABLES TO hippo_write;
ALTER DEFAULT PRIVILEGES FOR ROLE hippo_admin GRANT USAGE, SELECT ON SEQUENCES TO hippo_write;
-- and a table an admin adds to BOMTree is usable by the BOMTree app, like the ones from file 3
ALTER DEFAULT PRIVILEGES FOR ROLE hippo_admin IN SCHEMA bomtree GRANT SELECT, INSERT, UPDATE ON TABLES TO bomtree_app;
ALTER DEFAULT PRIVILEGES FOR ROLE hippo_admin IN SCHEMA bomtree GRANT USAGE, SELECT ON SEQUENCES TO bomtree_app;
ALTER DEFAULT PRIVILEGES FOR ROLE hippo_admin IN SCHEMA bomtree GRANT SELECT ON TABLES TO bomtree_readonly;

-- the first Hippo admins: BOMTree's admins for all families. Everyone else is added in the People page (/admin).
INSERT INTO hippo.access (user_id, level)
SELECT DISTINCT ur.user_id, 'admin' FROM bomtree.user_roles ur JOIN hippo.users u ON u.id = ur.user_id
WHERE ur.role_code = 'admin' AND ur.family_id IS NULL AND u.active AND NOT u.is_system
ON CONFLICT (user_id) DO NOTHING;

-- ---------------------------------------------------------------------
-- 4. Every structure change is logged, however it was made (connector, psql, scripts).
--    The connector adds its own row too, with the reason and the Books note.
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION hippo.log_ddl() RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, hippo AS $$
BEGIN
  INSERT INTO hippo.change_log (who, command, objects, statement)
  SELECT coalesce(nullif(current_setting('hippo.who', true), ''), session_user), tg_tag,
         string_agg(object_identity, ', '), left(current_query(), 20000)
  FROM pg_event_trigger_ddl_commands();
END $$;
REVOKE ALL ON FUNCTION hippo.log_ddl() FROM PUBLIC;

DROP EVENT TRIGGER IF EXISTS hippo_log_ddl;
CREATE EVENT TRIGGER hippo_log_ddl ON ddl_command_end EXECUTE FUNCTION hippo.log_ddl();

COMMIT;
