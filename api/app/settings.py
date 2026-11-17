"""All settings come from the environment (Railway service variables; a local .env never goes in Git)."""
import os

e = os.environ.get


class S:
    DATABASE_URL = e("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/railway")
    ENVIRONMENT = e("ENVIRONMENT", "local")                 # local, production
    PUBLIC_URL = e("PUBLIC_URL", "http://localhost:8000").rstrip("/")
    SECRET_KEY = e("SECRET_KEY", "dev-only-not-secret")
    SESSION_HOURS = float(e("SESSION_HOURS", "12"))
    DEV_LOGIN = e("DEV_LOGIN", "0") == "1"                  # sign in by email, no provider: local and tests only
    # Anyone an admin added (users table) may sign in, from any domain. An address at one of these company domains
    # must come from the company's own Google Workspace (hd claim): a personal Google account can't use it.
    COMPANY_DOMAINS = [d.strip().lower() for d in e("COMPANY_DOMAINS", "taraztechnologies.com,taraz.tech").split(",") if d.strip()]

    # Microsoft 365 (Entra ID): only the tenants listed here. Other tenants' admins can put any address on an account,
    # so outside people sign in with Google.
    MS_TENANT_IDS = [t.strip() for t in e("MS_TENANT_ID", "").split(",") if t.strip()]
    MS_CLIENT_ID = e("MS_CLIENT_ID", "")
    MS_CLIENT_SECRET = e("MS_CLIENT_SECRET", "")
    GOOGLE_CLIENT_ID = e("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = e("GOOGLE_CLIENT_SECRET", "")

    # Hippo (file 6): one database login per level. The Hippo connector and People page are off until these are set.
    HIPPO_READ_URL = e("HIPPO_READ_URL", "")
    HIPPO_WRITE_URL = e("HIPPO_WRITE_URL", "")
    HIPPO_ADMIN_URL = e("HIPPO_ADMIN_URL", "")

    # The Google Sheets the page was written for; the server answers their tabs from Postgres.
    # Their IDs are set in Railway, not here: the code is in a public repo.
    INDEX_SHEET_ID = e("INDEX_SHEET_ID", "")
    BLUEPRINTS_SHEET_ID = e("BLUEPRINTS_SHEET_ID", "")
    SHEET_TZ = e("SHEET_TZ", "Europe/Istanbul")            # dates are shown as the sheets showed them


def startup_problems():
    """Settings that must never reach production."""
    if S.ENVIRONMENT == "local":
        return []
    out = []
    if S.DEV_LOGIN:
        out.append("DEV_LOGIN=1 lets anyone sign in by typing an email: local only")
    if S.SECRET_KEY in ("", "dev-only-not-secret") or len(S.SECRET_KEY) < 32:
        out.append("SECRET_KEY must be a long random string")
    if not (S.MS_CLIENT_ID or S.GOOGLE_CLIENT_ID):
        out.append("no sign-in provider: set GOOGLE_CLIENT_ID or MS_CLIENT_ID")
    if S.MS_CLIENT_ID and not S.MS_TENANT_IDS:
        out.append("MS_TENANT_ID is required with MS_CLIENT_ID")
    return out
