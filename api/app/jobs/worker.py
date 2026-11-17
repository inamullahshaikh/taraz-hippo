"""Background worker: runs queued Odoo jobs and the nightly schedule. Jobs are rows in odoo_jobs, so they survive a restart.

  python -m app.jobs.worker

Nightly (server local time, set TZ): sync_parts 01:00, sync_costs 01:30, sync_boms 02:00, backup upload 03:30
(the backup container dumps at 03:00), migration_run 04:00 until cut-over, restore-test check on the 1st at 06:00.
"""
import io
import json
import logging
import time
import traceback
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

from ..db import Jsonb, pool, setting, tx
from ..integrations import drive, mail, odoo
from ..settings import S

log = logging.getLogger("bomtree.worker")
STATE = Path(S.DATA_DIR, "worker_state.json")

SCHEDULE = [  # (name, HH:MM, only on day of month)
    ("sync_parts", "01:00", None), ("sync_costs", "01:30", None), ("sync_boms", "02:00", None),
    ("backup_upload", "03:30", None), ("migration_run", "04:00", None), ("restore_check", "06:00", 1),
]


def claim():
    with tx(source="Odoo sync") as conn:
        return conn.execute("""UPDATE odoo_jobs SET status = 'Running', started_at = now()
                               WHERE id = (SELECT id FROM odoo_jobs WHERE status = 'Queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
                               RETURNING *""").fetchone()


def run_job(job):
    request = response = None
    try:
        client = odoo.connect(job["target"])
        with tx(job["requested_by"], "Odoo sync") as conn:
            if not setting(conn, "odoo_enabled"):
                raise odoo.OdooError("Odoo is switched off on the Admin page")
            if job["kind"] == "push_bom":
                request, response = odoo.push_bom(conn, client, job["bom_version_id"])
            else:
                response = getattr(odoo, job["kind"])(conn, client)
            conn.execute("UPDATE odoo_jobs SET status = 'Done', finished_at = now(), request = coalesce(%s, request), response = %s "
                         "WHERE id = %s", (Jsonb(request) if request else None, Jsonb(response), job["id"]))
        log.info("job %s %s done", job["id"], job["kind"])
    except Exception as e:  # noqa: BLE001 - every failure is recorded on the job and alerted
        missing = getattr(e, "missing", [])
        with tx(source="Odoo sync") as conn:
            conn.execute("UPDATE odoo_jobs SET status = 'Failed', finished_at = now(), error = %s, response = %s WHERE id = %s",
                         (str(e) or type(e).__name__, Jsonb({"missing": missing}) if missing else None, job["id"]))
            to = [r["email"] for r in conn.execute("SELECT email FROM users WHERE id = %s AND active", (job["requested_by"],))]
        body = f"{job['kind']} to Odoo {job['target']} failed: {e}\n" + "\n".join(f"- {m}" for m in missing[:50]) + "\n/odoo"
        mail.send(to + S.ALERT_EMAILS, f"Odoo job {job['id']} failed", body)
        log.warning("job %s failed: %s", job["id"], e)


# Scheduled work ----------------------------------------------------------------

def queue_sync(kind):
    with tx(source="Odoo sync") as conn:
        if setting(conn, "odoo_enabled"):
            conn.execute("INSERT INTO odoo_jobs (kind, target) VALUES (%s, %s)", (kind, S.ODOO_READ_TARGET))


def status_file(name):
    p = Path(S.BACKUP_DIR, "status", name)
    return json.loads(p.read_text()) if p.exists() else None


def backup_upload():
    """Copy the newest dump to Drive and prune old copies (30 daily, 12 monthly). Alerts if last night's dump failed."""
    st = status_file("backup.json")
    if not st or not st.get("ok") or datetime.fromisoformat(st["at"]).replace(tzinfo=None) < datetime.now() - timedelta(hours=26):
        mail.alert("Backup failed", f"Last night's database backup did not complete.\n{st}")
        return
    if not (S.BACKUP_DRIVE_FOLDER_ID and S.GOOGLE_SA_FILE):
        return
    name = Path(st["file"]).name
    files = drive.list_files(S.BACKUP_DRIVE_FOLDER_ID, "bomtree-")
    if name not in {f["name"] for f in files}:
        drive.upload(st["file"], S.BACKUP_DRIVE_FOLDER_ID, name)
    files.sort(key=lambda f: f["name"], reverse=True)          # bomtree-YYYY-MM-DD-HHMM.dump
    monthly = [f for f in files if f["name"][16:18] == "01"]
    daily = [f for f in files if f["name"][16:18] != "01"]
    keep = {f["id"] for f in daily[:30] + monthly[:12]}
    for f in files:
        if f["id"] not in keep and f["name"] != name:
            drive.delete(f["id"])


def migration_run():
    if not S.MIGRATION_NIGHTLY:
        return
    from ..migration import sheets_to_pg
    out = io.StringIO()
    with redirect_stdout(out):
        code = sheets_to_pg.main([])
    mail.send(S.ALERT_EMAILS, f"Migration check report: {'0 differences' if code == 0 else 'differences found'}", out.getvalue())


def restore_check():
    st = status_file("restore_test.json")
    if not st or not st.get("ok"):
        mail.alert("Monthly restore test failed", f"The backup restore test did not pass.\n{st}")


TASKS = {"sync_parts": lambda: queue_sync("sync_parts"), "sync_costs": lambda: queue_sync("sync_costs"),
         "sync_boms": lambda: queue_sync("sync_boms"), "backup_upload": backup_upload, "migration_run": migration_run,
         "restore_check": restore_check}


def due(state, now):
    today = now.strftime("%Y-%m-%d")
    for name, at, day in SCHEDULE:
        if now.strftime("%H:%M") >= at and state.get(name) != today and (day is None or now.day == day):
            yield name, today


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    pool.open()
    Path(S.DATA_DIR).mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if not state:  # first start: don't run tonight's jobs retroactively for the past hours
        state = {name: datetime.now().strftime("%Y-%m-%d") for name, *_ in SCHEDULE}
    log.info("worker started")
    while True:
        Path(S.DATA_DIR, "worker.heartbeat").touch()
        for name, today in due(state, datetime.now()):
            state[name] = today
            STATE.write_text(json.dumps(state))
            try:
                TASKS[name]()
            except Exception:  # noqa: BLE001
                log.exception("scheduled %s failed", name)
                mail.alert(f"Scheduled job {name} failed", traceback.format_exc()[-3000:])
        try:
            job = claim()
        except Exception as e:  # noqa: BLE001 - database restarting or not migrated yet: wait, don't crash
            log.warning("waiting for the database: %s", str(e).splitlines()[0])
            time.sleep(10)
            continue
        if job:
            run_job(job)
        else:
            time.sleep(5)


if __name__ == "__main__":
    main()
