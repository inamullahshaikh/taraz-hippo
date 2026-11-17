"""Google Drive through the company service account: exports to Sheets, file uploads, folder lookup."""
import json
import uuid
from pathlib import Path

from ..settings import S

SCOPES = ["https://www.googleapis.com/auth/drive"]
API = "https://www.googleapis.com/drive/v3/files"


def _session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    if not S.GOOGLE_SA_FILE:
        raise RuntimeError("Google is not set up (GOOGLE_SA_FILE)")
    return AuthorizedSession(service_account.Credentials.from_service_account_file(S.GOOGLE_SA_FILE, scopes=SCOPES))


def create_sheet(title, folder_id, rows):
    import gspread
    gc = gspread.service_account(filename=S.GOOGLE_SA_FILE)
    sh = gc.create(title, folder_id=folder_id or S.DRIVE_ROOT_FOLDER_ID or None)
    sh.sheet1.update([[("" if c is None else c if isinstance(c, (int, float, str)) else str(c)) for c in r] for r in rows])
    sh.sheet1.freeze(rows=1)
    return sh.url


def upload(path, folder_id, name=None):
    path = Path(path)
    meta = {"name": name or path.name, "parents": [folder_id]}
    b = uuid.uuid4().hex.encode()            # random, so it can never appear inside the file
    body = (b"--" + b + b"\r\nContent-Type: application/json\r\n\r\n" + json.dumps(meta).encode()
            + b"\r\n--" + b + b"\r\nContent-Type: application/octet-stream\r\n\r\n" + path.read_bytes() + b"\r\n--" + b + b"--")
    r = _session().post("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true",
                        data=body, headers={"Content-Type": f"multipart/related; boundary={b.decode()}"}, timeout=600)
    r.raise_for_status()
    return r.json()["id"]


def list_files(folder_id, prefix=""):
    q = f"'{folder_id}' in parents and trashed = false" + (f" and name contains '{prefix}'" if prefix else "")
    r = _session().get(API, params={"q": q, "fields": "files(id,name,createdTime)", "pageSize": 1000,
                                    "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=60)
    r.raise_for_status()
    return r.json()["files"]


def delete(file_id):
    _session().delete(f"{API}/{file_id}", params={"supportsAllDrives": "true"}, timeout=60).raise_for_status()


def find_folders(name):
    q = f"mimeType = 'application/vnd.google-apps.folder' and trashed = false and name contains '{name.replace(chr(39), '')}'"
    r = _session().get(API, params={"q": q, "fields": "files(id,name)", "pageSize": 20,
                                    "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=60)
    r.raise_for_status()
    return r.json()["files"]


def ping():
    _session().get(API, params={"pageSize": 1}, timeout=20).raise_for_status()
    return True
