-- =====================================================================
-- BOMTree · PostgreSQL schema  (file 1 of 3)
-- Tables, keys, checks, indexes and read views.
--
-- Run once on an empty database, as the database owner:
--   psql "$BOMTREE_DB" -v ON_ERROR_STOP=1 -f 1_schema.sql
--   psql "$BOMTREE_DB" -v ON_ERROR_STOP=1 -f 2_rules_and_transactions.sql
--   psql "$BOMTREE_DB" -v ON_ERROR_STOP=1 -f 3_security_and_seed.sql
--
-- Everything lives in the schema "bomtree". Needs PostgreSQL 15 or newer
-- (tested on 16).
-- =====================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS citext;    -- case-insensitive e-mail addresses
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fast "contains" search on part and item names

CREATE SCHEMA IF NOT EXISTS bomtree;
SET search_path = bomtree, public;

-- ---------------------------------------------------------------------
-- 1. People and rights (RBAC)
-- ---------------------------------------------------------------------

CREATE TABLE users (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email         citext NOT NULL UNIQUE,
  name          text   NOT NULL,
  active        boolean NOT NULL DEFAULT true,
  is_system     boolean NOT NULL DEFAULT false,      -- migration, Odoo sync, Blue
  last_login_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE roles (
  code        text PRIMARY KEY,                      -- admin, family_owner, designer, reviewer, planner, viewer
  name        text NOT NULL,
  description text
);

CREATE TABLE permissions (
  code        text PRIMARY KEY,                      -- bom.edit, version.release, ...
  description text NOT NULL
);

CREATE TABLE role_permissions (
  role_code   text NOT NULL REFERENCES roles ON DELETE CASCADE,
  perm_code   text NOT NULL REFERENCES permissions ON DELETE CASCADE,
  -- 'family' = applies only in the families the role was given for
  -- 'global' = applies in every family for anyone holding the role
  scope       text NOT NULL DEFAULT 'family' CHECK (scope IN ('family','global')),
  PRIMARY KEY (role_code, perm_code)
);

-- families must exist before user_roles can point at them
CREATE TABLE families (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code            text NOT NULL UNIQUE,              -- USM, SPM, PELab ...
  name            text NOT NULL,
  status          text NOT NULL DEFAULT 'Loaded',    -- Pilot, Loaded, Ready, Live ...
  old_tools       text,
  notes           text,
  drive_folder_url text,
  legacy_sheet_id text,                              -- the Google Sheet it came from
  owner_id        bigint REFERENCES users,
  odoo_target     text NOT NULL DEFAULT 'staging' CHECK (odoo_target IN ('staging','production')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  archived_at     timestamptz
);

CREATE TABLE user_roles (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id     bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  role_code   text   NOT NULL REFERENCES roles,
  family_id   bigint REFERENCES families,            -- NULL = all families
  granted_by  bigint REFERENCES users,
  granted_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (user_id, role_code, family_id)
);
CREATE INDEX user_roles_user_idx ON user_roles (user_id);

-- ---------------------------------------------------------------------
-- 2. Catalogue: blocks, items, parts
-- ---------------------------------------------------------------------

-- Index › Blocks: how items are grouped in the side panel
CREATE TABLE blocks (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name         text NOT NULL UNIQUE,                 -- Gate drivers, Stacks ...
  show_order   integer NOT NULL,
  level        text,                                 -- Product / SFG
  description  text,
  name_rule    text,                                 -- regular expression on the item code
  match_order  integer
);

-- Index › Items: every product (FG) and semi-finished good (SFG)
CREATE TABLE items (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code            text NOT NULL UNIQUE,              -- PCBA-USM-3IV
  prefix          text NOT NULL,                     -- PCBA, PCBA_SMD, ModA ...
  family_id       bigint NOT NULL REFERENCES families,
  level           text NOT NULL CHECK (level IN ('FG','SFG')),
  status          text NOT NULL DEFAULT 'Proposed',  -- Proposed, Approved, In Odoo, Archived ...
  built_by        text,
  description     text,
  odoo_today      text,                              -- what Odoo has today (free text)
  odoo_code       text CHECK (odoo_code IS NULL OR odoo_code ~ '^P[0-9]{6}$'),
  odoo_product_id integer,
  notes           text,
  brain_url       text,
  block_override_id bigint REFERENCES blocks,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  archived_at     timestamptz,
  CHECK (code = btrim(code) AND code <> '')
);
CREATE INDEX items_family_idx    ON items (family_id) WHERE archived_at IS NULL;
CREATE INDEX items_odoo_code_idx ON items (odoo_code) WHERE odoo_code IS NOT NULL;
CREATE INDEX items_code_trgm     ON items USING gin (code gin_trgm_ops);
CREATE INDEX items_desc_trgm     ON items USING gin (description gin_trgm_ops);

-- Bought or custom components (one row per Odoo product code)
CREATE TABLE parts (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code            text UNIQUE CHECK (code IS NULL OR code ~ '^P[0-9]{6}$'),  -- P0xxxxx; NULL until in Odoo
  mpn             text NOT NULL,                     -- the name used in the BOM (MPN or Odoo name)
  manufacturer    text,
  description     text,
  uom             text NOT NULL DEFAULT 'Units',
  is_cmp          boolean NOT NULL DEFAULT false,    -- our own custom mechanical part (Blueprints)
  image_url       text,
  buy_links       jsonb NOT NULL DEFAULT '[]'::jsonb,
  cost            numeric(14,4),
  cost_currency   text,
  on_hand         numeric(14,3),
  odoo_product_id integer UNIQUE,
  last_synced_at  timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
-- a part with no Odoo code yet is identified by its name
CREATE UNIQUE INDEX parts_mpn_no_code_uq ON parts (mpn) WHERE code IS NULL;
CREATE INDEX parts_mpn_idx  ON parts (mpn);
CREATE INDEX parts_mpn_trgm ON parts USING gin (mpn gin_trgm_ops);
CREATE INDEX parts_desc_trgm ON parts USING gin (description gin_trgm_ops);

-- ---------------------------------------------------------------------
-- 3. BOMs, versions and lines
-- ---------------------------------------------------------------------

-- Family sheet › BOMs: one row per BOM option of an item
CREATE TABLE boms (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  item_id            bigint NOT NULL REFERENCES items,
  option_code        text   NOT NULL DEFAULT 'Standard',   -- Standard, BD, BC, Make, Buy ...
  legacy_key         text UNIQUE,                    -- the "BOM" column in the sheet, e.g. "PCBA_SMD-USM · BD"
  is_default         boolean NOT NULL DEFAULT false,
  when_to_use        text,
  status             text NOT NULL DEFAULT 'Active' CHECK (status IN ('Active','Under review','Archived')),
  notes              text,
  current_version_id bigint,                         -- FK added below
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  archived_at        timestamptz,
  UNIQUE (item_id, option_code)
);
-- exactly one default BOM per item among the BOMs that are not archived
CREATE UNIQUE INDEX boms_one_default_uq ON boms (item_id) WHERE is_default AND archived_at IS NULL;
CREATE INDEX boms_item_idx ON boms (item_id);

CREATE TABLE change_requests (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  number       text NOT NULL UNIQUE,                 -- CR-0001
  bom_id       bigint NOT NULL REFERENCES boms,
  reason       text NOT NULL,
  impact       text,
  priority     text NOT NULL DEFAULT 'Normal' CHECK (priority IN ('Low','Normal','Urgent')),
  state        text NOT NULL DEFAULT 'Open' CHECK (state IN ('Open','Approved','Rejected','Done')),
  requested_by bigint NOT NULL REFERENCES users,
  decided_by   bigint REFERENCES users,
  decided_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX change_requests_bom_idx  ON change_requests (bom_id, state);
CREATE SEQUENCE change_request_no_seq;

CREATE TABLE bom_versions (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  bom_id            bigint  NOT NULL REFERENCES boms,
  version_no        integer NOT NULL CHECK (version_no > 0),
  state             text    NOT NULL DEFAULT 'Draft'
                    CHECK (state IN ('Draft','In review','Approved','Released','Superseded')),
  migrated          boolean NOT NULL DEFAULT false,  -- true = came from the Google Sheets
  created_by        bigint  NOT NULL REFERENCES users,
  submitted_by      bigint  REFERENCES users,
  submitted_at      timestamptz,
  approved_by       bigint  REFERENCES users,
  approved_at       timestamptz,
  released_by       bigint  REFERENCES users,
  released_at       timestamptz,
  change_request_id bigint  REFERENCES change_requests,
  restored_from_id  bigint  REFERENCES bom_versions,
  odoo_bom_id       integer,
  change_note       text,
  lock_version      integer NOT NULL DEFAULT 0,      -- optimistic lock: stops two people overwriting each other
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (bom_id, version_no),
  CHECK (approved_by IS NULL OR approved_by <> created_by)   -- four eyes: the maker never approves
);
-- at most one released version and at most one open (unreleased) version per BOM
CREATE UNIQUE INDEX bom_versions_one_released_uq ON bom_versions (bom_id) WHERE state = 'Released';
CREATE UNIQUE INDEX bom_versions_one_open_uq     ON bom_versions (bom_id) WHERE state IN ('Draft','In review','Approved');
CREATE INDEX bom_versions_state_idx ON bom_versions (state, bom_id);

ALTER TABLE boms ADD CONSTRAINT boms_current_version_fk
  FOREIGN KEY (current_version_id) REFERENCES bom_versions DEFERRABLE INITIALLY DEFERRED;

-- Family sheet › Lines
CREATE TABLE bom_lines (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  bom_version_id  bigint  NOT NULL REFERENCES bom_versions ON DELETE CASCADE,
  seq             integer NOT NULL,                  -- order of the line in the BOM
  part_id         bigint  REFERENCES parts,
  child_item_id   bigint  REFERENCES items,          -- a sub-assembly (another item)
  component_label text    NOT NULL,                  -- the component exactly as typed in the sheet
  qty             numeric(14,4) NOT NULL CHECK (qty > 0),
  uom             text    NOT NULL DEFAULT 'Units',
  ref_des         text,
  note            text,
  CHECK ((part_id IS NULL) <> (child_item_id IS NULL)),   -- a part OR a sub-assembly, never both
  UNIQUE (bom_version_id, seq)
);
CREATE INDEX bom_lines_part_idx  ON bom_lines (part_id)       WHERE part_id IS NOT NULL;
CREATE INDEX bom_lines_child_idx ON bom_lines (child_item_id) WHERE child_item_id IS NOT NULL;

CREATE TABLE approvals (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  bom_version_id bigint NOT NULL REFERENCES bom_versions,
  reviewer_id    bigint NOT NULL REFERENCES users,
  decision       text   NOT NULL CHECK (decision IN ('Approve','Reject','Ask changes')),
  comment        text,
  decided_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX approvals_version_idx ON approvals (bom_version_id);

-- ---------------------------------------------------------------------
-- 4. Old BOM Tool data (kept for comparison)
-- ---------------------------------------------------------------------

-- Index › Conversions
CREATE TABLE conversions (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  seq           integer NOT NULL,
  old_tool      text,
  old_item      text,
  old_code      text,
  change        text,                                -- Renamed, Rebuilt, Split, Merged, Removed ...
  new_item_id   bigint REFERENCES items,
  new_item_text text,                                -- as typed, kept even when no item matches
  note          text
);
CREATE INDEX conversions_old_item_idx ON conversions (old_item);
CREATE INDEX conversions_new_item_idx ON conversions (new_item_id);

-- Family sheet › Old lines
CREATE TABLE legacy_lines (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  family_id  bigint NOT NULL REFERENCES families,
  seq        integer NOT NULL,
  old_item   text NOT NULL,
  old_code   text,
  old_tool   text,
  component  text,
  part_code  text,
  qty        numeric(14,4),
  ref_des    text,
  note       text
);
CREATE INDEX legacy_lines_item_idx ON legacy_lines (old_item);
CREATE INDEX legacy_lines_code_idx ON legacy_lines (part_code);

-- Index › Tools: which old BOM Tools are retired
CREATE TABLE old_tools (
  tool        text PRIMARY KEY,                      -- BT013
  archived    boolean NOT NULL DEFAULT false,
  changed_by  text,
  changed_at  timestamptz,
  note        text
);

-- ---------------------------------------------------------------------
-- 5. Design and manufacturing data
-- ---------------------------------------------------------------------

-- Family sheet › PNP: pick-and-place positions per board
CREATE TABLE placements (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  family_id  bigint NOT NULL REFERENCES families,
  seq        integer NOT NULL,
  board      text NOT NULL,                          -- PCB name, e.g. PCB-USM-3IV.14
  ref_des    text NOT NULL,
  name       text,
  package    text,
  x_mm       numeric(10,4),
  y_mm       numeric(10,4),
  side       text CHECK (side IS NULL OR side IN ('Top','Bottom')),
  rotation   numeric(7,3),
  value      text,
  source     text,
  loaded_at  timestamptz
);
CREATE INDEX placements_board_idx ON placements (board, ref_des);
CREATE INDEX placements_family_idx ON placements (family_id);

-- Family sheet › Design: what each part on a board is for
CREATE TABLE design_refs (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  family_id    bigint NOT NULL REFERENCES families,
  seq          integer NOT NULL,
  board        text NOT NULL,
  ref_des      text NOT NULL,
  block_path   text,
  copy_label   text,
  level        text,                                 -- Critical, Sensitive, Normal
  role         text,
  why          text,
  design_mpn   text,
  design_code  text,
  confirmed_by text,
  confirmed_on date
);
CREATE INDEX design_refs_board_idx ON design_refs (board, ref_des);

-- Family sheet › Design nets
CREATE TABLE design_nets (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  family_id  bigint NOT NULL REFERENCES families,
  seq        integer NOT NULL,
  board      text NOT NULL,
  net        text NOT NULL,
  kind       text,                                   -- signal, power
  sheet      text,
  ends       text
);
CREATE INDEX design_nets_board_idx ON design_nets (board, net);

-- Index › Design blocks
CREATE TABLE design_blocks (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  seq          integer NOT NULL,
  block_type   text NOT NULL,
  function     text,
  products     text,
  boards       text,
  copies       integer,
  lines        integer,
  critical     integer,
  sensitive    integer,
  normal       integer,
  fingerprint  text,
  status       text,
  confirmed_by text,
  confirmed_on date
);

-- Index › Design issues
CREATE TABLE design_issues (
  id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  seq      integer NOT NULL,
  board    text NOT NULL,
  ref_des  text,
  issue    text NOT NULL,
  source   text,
  status   text NOT NULL DEFAULT 'Open'
);
CREATE INDEX design_issues_board_idx ON design_issues (board) WHERE status = 'Open';

-- Index › Library: board sizes, pictures and connector positions
CREATE TABLE board_library (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  item_id        bigint REFERENCES items,
  item_text      text NOT NULL,
  code           text,
  width_mm       numeric(10,3),
  height_mm      numeric(10,3),
  front_picture  text,
  back_picture   text,
  ports          jsonb NOT NULL DEFAULT '[]'::jsonb,
  source         text,
  updated_at     timestamptz,
  notes          text
);
CREATE UNIQUE INDEX board_library_item_uq ON board_library (item_text);

-- Index › Canvas: saved layout drawings
CREATE TABLE canvases (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  item_id     bigint REFERENCES items,
  item_text   text NOT NULL UNIQUE,
  pages       text,
  data        jsonb,
  updated_at  timestamptz,
  updated_by  text,
  notes       text
);

-- Index › Alternates: approved alternate parts
CREATE TABLE alternates (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  seq          integer NOT NULL,
  group_name   text NOT NULL,
  base_part    text,
  base_code    text,
  mfr_pn       text,
  mfr          text,
  digikey_pn   text,
  description  text,
  kind         text,
  status       text,
  odoo_code    text,
  search       text,
  approved_by  text,
  approved_on  date,
  note         text,
  url          text,
  product      text
);
CREATE INDEX alternates_base_code_idx ON alternates (base_code);
CREATE INDEX alternates_group_idx     ON alternates (group_name);

-- Index › Alternate searches
CREATE TABLE alternate_searches (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  seq          integer NOT NULL,
  search       text,
  group_name   text,
  base_part    text,
  base_code    text,
  kind         text,
  searched_on  date,
  searched_by  text,
  rows_in_csv  integer,
  approved     text,
  file         text,
  digikey_link text,
  note         text
);

-- ---------------------------------------------------------------------
-- 6. Work and history
-- ---------------------------------------------------------------------

CREATE TABLE comments (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_type text   NOT NULL,                       -- item, bom, version, line, change_request
  entity_id   bigint NOT NULL,
  user_id     bigint NOT NULL REFERENCES users,
  body        text   NOT NULL,
  resolved    boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX comments_entity_idx ON comments (entity_type, entity_id);

CREATE TABLE attachments (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_type   text   NOT NULL,
  entity_id     bigint NOT NULL,
  drive_file_id text   NOT NULL,
  name          text   NOT NULL,
  kind          text CHECK (kind IN ('Drawing','Datasheet','DipTrace BOM','Photo','Other')),
  added_by      bigint REFERENCES users,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX attachments_entity_idx ON attachments (entity_type, entity_id);

CREATE TABLE imports (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  file_name   text NOT NULL,
  family_id   bigint REFERENCES families,
  item_id     bigint REFERENCES items,
  status      text NOT NULL DEFAULT 'Matching' CHECK (status IN ('Matching','Waiting answers','Saved','Cancelled')),
  created_by  bigint NOT NULL REFERENCES users,
  result      jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE odoo_jobs (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  bom_version_id bigint REFERENCES bom_versions,
  kind           text NOT NULL CHECK (kind IN ('push_bom','sync_parts','sync_costs','sync_boms')),
  target         text NOT NULL CHECK (target IN ('staging','production')),
  status         text NOT NULL DEFAULT 'Queued' CHECK (status IN ('Queued','Running','Done','Failed')),
  attempts       integer NOT NULL DEFAULT 0,
  request        jsonb,
  response       jsonb,
  error          text,
  requested_by   bigint REFERENCES users,
  queued_at      timestamptz NOT NULL DEFAULT now(),
  started_at     timestamptz,
  finished_at    timestamptz
);
-- the worker picks jobs from this small index only
CREATE INDEX odoo_jobs_queue_idx ON odoo_jobs (queued_at) WHERE status IN ('Queued','Running');
CREATE INDEX odoo_jobs_version_idx ON odoo_jobs (bom_version_id);

-- Every change to every record. Rows are only ever added (see file 2 and 3).
CREATE TABLE audit_log (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at          timestamptz NOT NULL DEFAULT now(),
  user_id     bigint REFERENCES users,
  actor_label text,                                  -- free text, e.g. "Claude (owner said yes, CR-0023)" from the old Log
  source      text NOT NULL CHECK (source IN ('App','Blue','Import','Odoo sync','Admin','Migration','Database')),
  action      text NOT NULL,                         -- insert, update, archive, release, Seeded, Component swapped ...
  entity_type text NOT NULL,
  entity_id   bigint,
  entity_label text,                                 -- readable name, e.g. the item code
  before      jsonb,
  after       jsonb,
  detail      text
);
CREATE INDEX audit_log_at_idx     ON audit_log (at DESC);
CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id, at DESC);
CREATE INDEX audit_log_user_idx   ON audit_log (user_id, at DESC);

-- One row per run of the Sheets-to-Postgres migration script
CREATE TABLE migration_runs (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  snapshot     jsonb,                                -- which sheets, when they were read
  counts       jsonb,                                -- rows loaded per table
  errors       integer,
  warnings     integer,
  report       text
);

-- ---------------------------------------------------------------------
-- 7. Read views used by the app
-- ---------------------------------------------------------------------

-- The lines of every BOM's current version, ready to show
CREATE VIEW v_current_lines AS
SELECT b.id               AS bom_id,
       i.code             AS item_code,
       f.code             AS family,
       b.option_code,
       b.is_default,
       v.id               AS version_id,
       v.version_no,
       v.state            AS version_state,
       l.seq,
       COALESCE(ci.code, p.mpn) AS component,
       COALESCE(p.code, ci.odoo_code) AS code,
       (l.child_item_id IS NOT NULL) AS is_subassembly,
       l.qty, l.uom, l.ref_des, l.note,
       p.is_cmp
FROM boms b
JOIN items i         ON i.id = b.item_id
JOIN families f      ON f.id = i.family_id
JOIN bom_versions v  ON v.id = b.current_version_id
JOIN bom_lines l     ON l.bom_version_id = v.id
LEFT JOIN parts p    ON p.id = l.part_id
LEFT JOIN items ci   ON ci.id = l.child_item_id;

-- Where used: worked out live, never stored
CREATE VIEW v_where_used AS
SELECT COALESCE(p.code, ci.odoo_code) AS code,
       COALESCE(p.mpn, ci.code)       AS component,
       (l.child_item_id IS NOT NULL)  AS is_subassembly,
       l.part_id, l.child_item_id,
       i.code   AS used_in_item,
       b.option_code,
       f.code   AS family,
       l.qty,
       v.state  AS version_state
FROM bom_lines l
JOIN bom_versions v ON v.id = l.bom_version_id
JOIN boms b         ON b.id = v.bom_id AND b.current_version_id = v.id
JOIN items i        ON i.id = b.item_id
JOIN families f     ON f.id = i.family_id
LEFT JOIN parts p   ON p.id = l.part_id
LEFT JOIN items ci  ON ci.id = l.child_item_id
WHERE b.archived_at IS NULL;

COMMIT;
