"""Who hears about what. Recipients are worked out inside the request; sending happens after commit."""
from ..db import setting
from ..integrations import mail
from ..rbac.roles import ROLES


def _people(ctx, code, family_id, exclude=None):
    """Active users holding `code` in the family (or everywhere)."""
    roles = [r for r, codes in ROLES.items() if code in codes and r != "admin"]
    rows = ctx.all("""SELECT DISTINCT u.email FROM users u JOIN user_roles ur ON ur.user_id = u.id JOIN roles r ON r.id = ur.role_id
                      WHERE u.active AND r.code = ANY(%s) AND (ur.family_id IS NULL OR ur.family_id = %s) AND u.id IS DISTINCT FROM %s""",
                   roles, family_id, exclude)
    return [r["email"] for r in rows]


def _send(ctx, to, subject, body):
    if to and setting(ctx.conn, "notifications_enabled"):
        ctx.after.append(lambda: mail.send(to, subject, body))


def _link(v):
    return f"/item/{v['item_code']}/bom/{v['option_code']}/v/{v['version_no']}"


def reviewers(ctx, v):
    _send(ctx, _people(ctx, "version.approve", v["family_id"], exclude=v["created_by"]),
          f"Review: {v['item_code']} {v['option_code']} v{v['version_no']}",
          f"{ctx.user['name']} sent {v['item_code']} {v['option_code']} v{v['version_no']} for review.\n{_link(v)}")


def maker(ctx, v, decision, comment):
    row = ctx.one("SELECT email FROM users WHERE id = %s AND active", v["created_by"])
    _send(ctx, [row["email"]] if row else [], f"{decision}: {v['item_code']} {v['option_code']} v{v['version_no']}",
          f"{ctx.user['name']}: {decision}.\n{comment}\n{_link(v)}")


def released(ctx, v, stale):
    to = _people(ctx, "bom.edit", v["family_id"])
    _send(ctx, to, f"Released: {v['item_code']} {v['option_code']} v{v['version_no']}",
          f"{ctx.user['name']} released {v['item_code']} {v['option_code']} v{v['version_no']}.\n{_link(v)}")
    if stale:
        owners = [r["email"] for r in ctx.all("SELECT email FROM users WHERE id = ANY(%s) AND active",
                                              [s["owner_id"] for s in stale if s["owner_id"]])]
        _send(ctx, owners or to, f"Uses an older version of {v['item_code']}",
              "These BOMs were released before the new version of " + v["item_code"] + ":\n"
              + "\n".join(f"- {s['item_code']} {s['option_code']} v{s['version_no']}" for s in stale))


def change_request(ctx, cr, family_id, decided=None):
    if decided:
        row = ctx.one("SELECT email FROM users WHERE id = %s AND active", cr["requested_by"])
        _send(ctx, [row["email"]] if row else [], f"{cr['number']} {decided}", f"{ctx.user['name']} {decided.lower()} {cr['number']}.")
    else:
        _send(ctx, _people(ctx, "cr.approve", family_id), f"New change request {cr['number']}",
              f"{ctx.user['name']} raised {cr['number']}: {cr['reason']}\n/change-requests")
