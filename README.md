# BOMTree: switching from Google Sheets to PostgreSQL

This folder has everything needed to move BOMTree's data out of the Google Sheets and into a PostgreSQL database. Nothing is typed in by hand: a script copies the sheets, loads them in one go, and checks every BOM line against the sheets before anything is saved.

The sheets were loaded into the Railway database on 12 Nov 2026, with every BOM line matching the sheets exactly. Since then the BOMTree page reads and writes the database.

This repository is public, so it holds no internal Taraz data: the sheet IDs are Railway variables, and the BOMTree page (`api/app/static/bomtree.html`, a copy of the claude.ai page), the Taraz OS launcher (`tos-launcher.js`) and migration reports stay on the computer that deploys `bomtree-app` (they are git-ignored).

## What is in the folder

| File | What it is | Who runs it |
| --- | --- | --- |
| `1_schema.sql` | The database: 32 tables, their rules and 82 indexes | Once, on the new database |
| `2_rules_and_transactions.sql` | The safety rules and the "transactions" the app calls to change data | Once, right after file 1 |
| `3_security_and_seed.sql` | The 6 roles, 18 permissions, system users and database logins | Once, after file 2 (safe to run again) |
| `4_test_rules.sql` | 42 automatic tests of the rules (optional; leaves no trace) | After loading, to prove it works |
| `bomtree_migrate.py` | The script that copies the sheets and loads them (set `INDEX_SHEET_ID` and `BLUEPRINTS_SHEET_ID` first) | For every rehearsal and on cut-over day |
| `5_page_editing.sql` | What the page needs to edit the database (saving, removing) | Once, after file 3 (safe to run again) |
| `6_hippo.sql` | Hippo: shared people, access levels, change log; BOMTree becomes one part | Once, after file 5 (safe to run again) |
| `api/` | The BOMTree server: website, BOMTree and Hippo connectors, the page, the People page | Deployed as a Railway service |

**Important:** until cut-over the BOMTree page you use today still reads and writes the Google Sheets, and the sheets stay the master: the database is reloaded from them with `--replace` whenever it needs to catch up. Cut-over is when the page switches to the database (see below).

## BOMTree on the database (claude.ai page, connector and website)

The Postgres database is BOMTree's source of truth. The BOMTree page keeps its screens; only where it reads and writes changed.

| Who | Where | How they reach the data |
| --- | --- | --- |
| People in the organization | the BOMTree page in claude.ai | live, through the **BOMTree connector** |
| Anyone else the page is shared with | the same page | the **shared copy** the page keeps in its own claude.ai store |
| Any Claude chat with the connector | chat | the connector's tools (below) |
| Anyone added, from any domain | the BOMTree website on Railway | live, signing in with Google |

- **The shared copy.** A claude.ai page cannot reach Railway, and connectors give people outside the organization nothing, so the page keeps a copy of every tab in its own store. Whenever someone with the connector opens the page, it refreshes the copy (at most every 10 minutes, only tabs that changed).
- **Changes on the copy.** People the page's Share menu lets edit (outside people: invited by email as **Editor**, and only while the page is not also shared by public link) can change items, BOMs and lines on the copy. Their changes wait in the store and reach the database at the next refresh. A change to an item, BOM or block that changed in the database meanwhile is held, never forced in; people in the organization see it in a notice on the page. Other tabs (PNP, Design and the like) change only through the connector.
- **Saving.** A BOM saved in the page becomes a new released version at once (`release_direct`, file 5), recorded under the person who saved it; older versions are kept. Removing an item or BOM hides it and keeps it.
- **Connector tools.** `list_tabs`, `get_values`, `update_values`, `append_values` (the page's own calls, tab-shaped), `query` (read-only SQL, for people who may read drafts everywhere) and `run_sql` (admins). Table structure changes go through the Hippo connector (below).
- **Code.** `api/` is one Railway service: `app/routers/sheets.py` answers tabs from the tables, `app/sheetwrite.py` saves tab changes into them, `app/connector.py` is the connector (MCP at `/mcp`, OAuth with BOMTree's Google sign-in), `app/hippo.py` is the Hippo connector and the People page, `app/static/bomtree.html` is the page (not in this public repo; `bt-shim.js` stands in for claude.ai on the website). Tests: `cd api && DATABASE_URL=postgresql://postgres:x@localhost:55432/postgres pytest`.
- **Who gets in:** only people a Hippo admin added in the People page (`<PUBLIC_URL>/admin`). A company address must use the company's Google Workspace account; Microsoft sign-in only for the tenants in `MS_TENANT_ID`. Viewers see released BOMs only.

**Setting it up**

1. Run `5_page_editing.sql` after files 1–3 (safe to run again).
2. Railway service from `api/` (`railway up api --path-as-root --no-gitignore --service bomtree-app` from this folder; `--no-gitignore` so the page files, which are kept out of the public repo, are uploaded too), with a generated domain and these variables:

    ```
    BOMTREE_APP_PASSWORD=<the bomtree_app password, setup step 3>
    DATABASE_URL=postgresql://bomtree_app:${{BOMTREE_APP_PASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}
    ENVIRONMENT=production
    PORT=8000
    PUBLIC_URL=https://<the service's Railway domain>
    SECRET_KEY=<a long random string>
    GOOGLE_CLIENT_ID=...
    GOOGLE_CLIENT_SECRET=...      (OAuth client redirect URI: <PUBLIC_URL>/api/v1/auth/callback)
    INDEX_SHEET_ID=<the BOMTree Index sheet's ID>
    BLUEPRINTS_SHEET_ID=<the Blueprints sheet's ID>
    ```
3. In claude.ai, an organization owner adds a custom connector named exactly **BOMTree** with the URL `<PUBLIC_URL>/mcp`. Each person signs in to it once with Google.
4. Open the page once with the connector; that fills the shared copy. Then share it: Viewer to read, Editor (by email, no public link) to edit.
5. When the claude.ai page changes, save its HTML over `api/app/static/bomtree.html` too and redeploy, so the website matches.

## Hippo: Taraz's shared database

Hippo is the whole Postgres database. BOMTree is one part of it, the `bomtree` schema. What is shared lives in the `hippo` schema: people (`hippo.users`), each person's Hippo level (`hippo.access`) and the change log (`hippo.change_log`). Each new area (costs, stock, lead times ...) gets its own schema.

| Level | May do | Runs as |
| --- | --- | --- |
| read | read every table | `hippo_read` |
| write | also add, change and remove rows (not people, rights or the log) | `hippo_write` |
| admin | also change structure (schemas, tables, columns, constraints, indexes, views, functions, triggers, grants) and manage people | `hippo_admin`, which owns the tables |

- **Hippo connector** (`<PUBLIC_URL>/hippo/mcp`, same sign-in as BOMTree's). `query` reads. `run_sql` changes: the first call is a preview that is undone; `confirm: true` saves it. One statement per call, and Postgres itself refuses a second one. Each person's SQL runs as the login of their level, so Postgres, not the app, decides what they may do.
- **People page** (`<PUBLIC_URL>/admin`, Hippo admins only): add people, switch them off, set their Hippo level and BOMTree roles. Hippo always keeps one active admin. People are switched off, not deleted, because history points at them.
- **Change log.** Every change saved through the connector or the People page is in `hippo.change_log`, with the person, the reason and the Books note. Structure changes made any other way (psql, scripts) get there by an event trigger. No level can change or remove its rows.
- **Limits.** A call stops after 60 s. A structure change that waits more than 2 s for a busy table gives up, so the BOMTree page waits at most that long. Each level's login may hold 20 connections. Logins, superuser-only extensions and event triggers stay with `postgres`.
- **Safety.** Sign-in codes go only to Claude (claude.ai's connector callback, or a Claude app on the person's own computer). The BOMTree app's own login can only record sign-ins, so who someone is and what they may do change only in the People page. Hippo admins can drop anything, so backups matter (see Backups below).
- **BOMTree reloads.** Rows changed through Hippo are real work: `--replace` refuses to wipe them, as it does after app edits.
- **Maintenance on live** (scripts, reloads) runs as `hippo_admin`, which owns the tables, not as `postgres`: admins can rewrite BOMTree's trigger functions, and those run as whoever fires them. Run `4_test_rules.sql` only on a copy.
- **Emergency undo** (if the app must go back to a version from before Hippo): `ALTER TABLE hippo.users SET SCHEMA bomtree; GRANT INSERT, UPDATE ON bomtree.users TO bomtree_app;` The old app then works again and only Hippo stops; nothing is lost. Tested on a copy of live data, 17 Nov 2026.

**Where it runs** (live since 17 Nov 2026). Two Railway services run this code against the same Postgres:

- **`Hippo`** deploys from this GitHub repo (branch `main`, root directory `api/`) on every push. It serves the People page (`/admin`) and the Hippo connector (`/hippo/mcp`); it has no BOMTree page, so `/` opens the People page.
- **`bomtree-app`** serves the BOMTree page and connector. It is deployed by hand from a computer that has the page files (step 2 of "Setting it up" above), never from GitHub.

Backups taken just before going live (pg_dump, restore tested) are kept outside the repo.

**Setting it up** (once, after files 1–5)

1. Deploy the new code to `bomtree-app` first. It looks for people in `hippo` and in `bomtree`, so it works before and after step 2.
2. Run `6_hippo.sql` as one query, so its locks last a moment instead of one round trip per statement: `psql $env:BOMTREE_DB -X -c "$(Get-Content 6_hippo.sql -Raw)"`. It moves people to `hippo`, makes `hippo_admin` the owner of BOMTree's tables, and makes BOMTree's all-family admins the first Hippo admins.
3. Give the three logins a password (hex, so the URLs need no escaping): `ALTER ROLE hippo_read LOGIN PASSWORD '...'`, the same for `hippo_write` and `hippo_admin`. Send each on its own.
4. The `Hippo` service: a new service from this GitHub repo with root directory `api`, a generated domain, and these variables (the connector and People page stay off without the `HIPPO_*` ones):

    ```
    DATABASE_URL, BOMTREE_APP_PASSWORD, ENVIRONMENT, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
    INDEX_SHEET_ID, BLUEPRINTS_SHEET_ID                as for bomtree-app
    PUBLIC_URL=https://<the Hippo service's domain>
    SECRET_KEY=<its own long random string>
    HIPPO_READ_PASSWORD, HIPPO_WRITE_PASSWORD, HIPPO_ADMIN_PASSWORD
    HIPPO_READ_URL=postgresql://hippo_read:${{HIPPO_READ_PASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}
    HIPPO_WRITE_URL=postgresql://hippo_write:${{HIPPO_WRITE_PASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}
    HIPPO_ADMIN_URL=postgresql://hippo_admin:${{HIPPO_ADMIN_PASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}
    ```
5. In Google Cloud, add `<PUBLIC_URL>/api/v1/auth/callback` to the OAuth client's authorized redirect URIs, so people can sign in on the Hippo domain.
6. In claude.ai, an organization owner adds a custom connector named **Hippo** with the URL `<PUBLIC_URL>/hippo/mcp`.

## How it fits together

- **Tables.** Every sheet tab becomes a table (see "Where each sheet tab goes" below). Items, BOMs and lines are linked by hidden ID numbers, so renaming an item can never break a tree.
- **Versions.** Each BOM gets version 1, marked "migrated". From then on, a change makes a new version (Draft → In review → Approved → Released). A released version can never be edited, and older ones stay readable.
- **Safety rules inside the database.** These work even if the app has a bug:
  - qty must be above 0
  - a line is a part or a sub-assembly, never both
  - a BOM can never contain itself
  - one default BOM per item
  - the person who made a version cannot approve it
  - released versions are frozen
  - the audit log can only grow
- **Transactions.** The app never changes tables directly. It calls one function per job, for example "save this draft" or "release this version". Each function checks the person's rights, checks the rules, makes the change and writes the history row together. If any part fails, nothing is saved.
- **Rights (RBAC).** There are 6 roles: Admin, Family owner, Designer, Reviewer, Planner and Viewer. A role can be given for all families or for one family only. The full matrix is in file 3.

## One-time setup (Railway)

The database lives in the BOMTree project on Railway, the same project the full app will run in later. The migration runs from your own computer and needs no other server. The commands below are for PowerShell, run from this folder.

**1. Add PostgreSQL to the project.** In Railway, open the BOMTree project and click **Create → Database → PostgreSQL**. Railway adds a service called `Postgres`. Its **Variables** tab has two addresses:

- `DATABASE_PUBLIC_URL` can be reached from your computer. The migration uses this one.
- `DATABASE_URL` is private and can only be reached from inside the Railway project. The app will use it later, with its own login (see cut-over step 6).

Both log in as `postgres`, the database owner. The migration script must run as this owner.

**2. Create the tables**, rules and roles. This needs `psql`, the PostgreSQL client. If it is not installed, Docker Desktop can run it, as below. Client version 18 works with Railway's Postgres 16, 17 or 18.

```powershell
$env:BOMTREE_DB = "<DATABASE_PUBLIC_URL from Railway>"
function psql { docker run --rm -i -v "${PWD}:/w" -w /w postgres:18 psql @args }
psql $env:BOMTREE_DB -v ON_ERROR_STOP=1 -f 1_schema.sql
psql $env:BOMTREE_DB -v ON_ERROR_STOP=1 -f 2_rules_and_transactions.sql
psql $env:BOMTREE_DB -v ON_ERROR_STOP=1 -f 3_security_and_seed.sql
```

**3. Give the two app logins a password.** The app uses `bomtree_app`. Reports use `bomtree_readonly`.

```powershell
psql $env:BOMTREE_DB -c "ALTER ROLE bomtree_app LOGIN PASSWORD 'another-strong-password'"
psql $env:BOMTREE_DB -c "ALTER ROLE bomtree_readonly LOGIN PASSWORD 'a-third-password'"
```

`bomtree_app` can read and write, but it cannot delete items, BOMs or history, and it cannot touch the audit log.

**4. Install the script's libraries** (Python 3.10 or newer):

```
pip install "psycopg[binary]" google-auth requests
```

If `load` fails with *"An Application Control policy has blocked this file"*, Windows is blocking psycopg. `export` still works; run `load` and `check` in Docker instead, through this helper:

```powershell
function bomtree { docker run --rm -v "${PWD}:/w" -w /w -e BOMTREE_DB=$env:BOMTREE_DB python:3.13-slim sh -c "pip install -q --root-user-action=ignore 'psycopg[binary]' tzdata && python bomtree_migrate.py $args" }
bomtree load --snapshot snapshot
```

**5. Let the script read the sheets.**

1. In Google Cloud, create a **service account** and download its JSON key. Keep the key private and never put it in Git.
2. Share these sheets with the service account's e-mail address, as **Viewer**:
    - BOMTree – Index
    - every family sheet in the Index's Families tab. A family with no row in that tab is not exported.
    - the Blueprints sheet
3. Sharing the Drive folder that holds them all does the same in one step.

**6. Add the first admin** (only on a new, empty database; after that, people are added in the People page):

```
psql $env:BOMTREE_DB -c "SELECT bomtree.add_user('first.admin@example.com', 'First Admin', 'admin')"
```

## Every rehearsal (as often as you like, before cut-over)

```
python bomtree_migrate.py export --key service-account.json --out snapshot
python bomtree_migrate.py load   --snapshot snapshot
```

1. `export` saves an exact copy of every sheet in the `snapshot` folder. This copy is also your record of what was migrated.
2. `load` on its own is a **dry run**. It loads everything, checks every row, writes `snapshot/report-<date>.md`, and then **undoes it all**.
3. Read the report:
    - **Errors** must be fixed **in the sheets**. The report names the sheet, tab and row, e.g. `GDX › Lines row 4: Qty 'abc' is not a number`. Then export again.
    - **Warnings** do not stop the load, but are worth a look.
4. To keep a rehearsal in a test database, add `--commit`. To reload over an earlier rehearsal, add `--replace`. This wipes the migrated BOM data and loads it again; users, roles and families are kept. `--replace` refuses to run once anything has been done in the app (a new version, an edit to a migrated draft, an approval, a comment), so real work can never be wiped by mistake.

A load into Railway from your computer takes about 15 minutes, because each row is a round trip to Railway's public address.
5. Optional: run `psql $env:BOMTREE_DB -f 4_test_rules.sql`. The last line should say `42 passed, 0 failed`.

## Cut-over day

1. Tell everyone: no BOMTree edits for about an hour.
2. In Google Drive, set every BOMTree sheet to **read-only** (Index and every family sheet).
3. Export the final copy, then load it for real:

    ```
    python bomtree_migrate.py export --key service-account.json --out snapshot-final
    python bomtree_migrate.py load   --snapshot snapshot-final --commit
    ```

    The data is only saved if there are **0 errors**. Otherwise it prints `NOT SAVED` and nothing changes.
4. Run the check once more. It must say every line matches:

    ```
    python bomtree_migrate.py check --snapshot snapshot-final
    ```

5. Add the users and their roles in the People page (`<PUBLIC_URL>/admin`); a family limits a role to that family.
6. Switch on the new app. In the app service's Railway variables, point it at the private address with the `bomtree_app` login, never `postgres`:

    ```
    BOMTREE_APP_PASSWORD=<password from setup step 3>
    DATABASE_URL=postgresql://bomtree_app:${{BOMTREE_APP_PASSWORD}}@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{Postgres.PGDATABASE}}
    ```
7. Keep `snapshot-final` and its report. After 30 days, move the old sheets to an Archive folder in Drive.

**If something goes wrong on the day:** set the sheets back to editable and carry on with today's BOMTree page. Nothing was written to the sheets, so nothing is lost. Fix the problem, then repeat the cut-over another day.

## How the app changes data (for the developer)

Every change is one short transaction:

```
BEGIN;
SELECT bomtree.begin_action(<user id>, 'App');                                    -- who is acting
SELECT bomtree.save_draft_lines(<user id>, <version id>, <lock number>, '<lines as JSON>');
COMMIT;
```

| Function | What it does | Permission it checks |
| --- | --- | --- |
| `create_item` | New item in a family | item.create |
| `create_bom` | New BOM option; starts as an empty v1 draft | bom.edit |
| `start_draft` | Next draft of a released BOM, with its lines copied | bom.edit |
| `save_draft_lines` | Saves a draft's full line list. Refused if someone else saved first (lock number) | bom.edit |
| `submit_version` | Draft → In review | bom.submit |
| `review_version` | Approve, reject or ask changes; never by the maker | version.approve |
| `release_version` | Approved → Released; the old one becomes Superseded | version.release |
| `restore_version` | An old version comes back as a new draft | version.restore |
| `set_default_bom` | Makes a BOM the item's default | bom.edit |
| `archive_item` | Archives an item and its BOMs; refused while another BOM uses it | item.archive |
| `raise_change_request` / `decide_change_request` | Change request; approving opens the linked draft | cr.create / cr.approve |
| `add_user` / `grant_role` / `revoke_role` | People and their roles | admin.manage |
| `explode(item, qty)` / `parts_needed(item, qty)` | Full multi-level BOM, and total parts for a build | read only |

Errors come back with a code the app turns into a message:

- `BT403` no permission
- `BT404` not found
- `BT409` someone saved first, or the state does not allow it
- `BT422` a rule failed, with the line and the reason

## Where each sheet tab goes

| Sheet › tab | Table | Notes |
| --- | --- | --- |
| Index › Families | families | Sheet ID kept as `legacy_sheet_id` |
| Index › Items | items | "Archived" items get an archive date |
| Index › Blocks | blocks | |
| Index › Conversions | conversions | New item linked when it exists |
| Index › Log | audit_log | Kept as history, source = Migration |
| Index › Library, Canvas, Tools | board_library, canvases, old_tools | |
| Index › Alternates, Alternate searches | alternates, alternate_searches | |
| Index › Design blocks, Design issues | design_blocks, design_issues | |
| Index › Where used | *(not copied)* | Worked out live by the `v_where_used` view; the old tab is only compared |
| Family › BOMs | boms + bom_versions | Each BOM becomes version 1: Active → Released, Under review → In review, Archived and Not to be used → Archived, Needs info → an unfinished Draft (like a new BOM made in the app) |
| Family › Lines | bom_lines | A component that is an item becomes a sub-assembly link; otherwise a part, by its Odoo code |
| Family › Old lines | legacy_lines | |
| Family › PNP | placements | |
| Family › Design, Design nets | design_refs, design_nets | |
| Blueprints › Parts | parts.is_cmp | Marks our custom parts |

Columns are found by their **header name**, so moving a column in a sheet is fine. Renaming a header is not: the report will name the missing column.

## Backups

In Railway, open the Postgres service, then its volume's **Backups** tab, and turn on daily backups. Restoring one is done from the same tab. If the plan has no Backups tab, the copy below is the only backup, so run it every day.

Keep a copy outside Railway too: save a dump and put it in Google Drive.

```powershell
docker run --rm -v "${PWD}:/w" -w /w postgres:18 pg_dump $env:BOMTREE_DB --format=custom --file="bomtree-$(Get-Date -Format yyyy-MM-dd).dump"
```

Once a month, restore a dump into a throwaway local database, to be sure it works:

```powershell
docker run -d --name bomtree-restore-test -e POSTGRES_PASSWORD=test postgres:18
docker cp bomtree-<date>.dump bomtree-restore-test:/tmp/b.dump
docker exec bomtree-restore-test pg_restore -U postgres -d postgres --no-owner --no-privileges /tmp/b.dump
docker rm -f bomtree-restore-test
```
