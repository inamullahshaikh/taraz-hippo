"""The API end to end: the phase 3 gate (designer -> reviewer -> family owner), rights refused on the server,
lock_version, change requests, import, exports, read-only mode."""
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import app

TEST_URL = os.environ["DATABASE_URL"]
DOMAIN = "taraztechnologies.com"


def sql(q, *args):
    with psycopg.connect(TEST_URL) as c:
        cur = c.execute(q, args)
        return cur.fetchone() if cur.description else None


def person(name, *roles):
    """A signed-in client. roles: (role, family_id or None)."""
    c = TestClient(app)
    r = c.post("/api/v1/auth/dev-login", json={"email": f"{name}@{DOMAIN}", "name": name.title()})
    assert r.status_code == 200, r.text
    uid = sql("SELECT id FROM users WHERE email = %s", f"{name}@{DOMAIN}")[0]
    for role, fam in roles:
        sql("INSERT INTO user_roles (user_id, role_id, family_id) SELECT %s, id, %s FROM roles WHERE code = %s ON CONFLICT DO NOTHING",
            uid, fam, role)
    c.uid = uid
    return c


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


@pytest.fixture(scope="module")
def w():
    fam = sql("INSERT INTO families (code, name) VALUES ('TST', 'Test') RETURNING id")[0]
    other = sql("INSERT INTO families (code, name) VALUES ('OTH', 'Other') RETURNING id")[0]
    p1 = sql("INSERT INTO parts (code, mpn, cost, on_hand) VALUES ('P900001', 'RES-10K', 0.5, 100) RETURNING id")[0]
    p2 = sql("INSERT INTO parts (code, mpn, cost, on_hand) VALUES ('P900002', 'CAP-100N', 0.25, 2) RETURNING id")[0]
    return {
        "fam": fam, "other": other, "p1": p1, "p2": p2,
        "admin": person("admin1", ("admin", None)),
        "designer": person("designer1", ("designer", fam)),
        "reviewer": person("reviewer1", ("reviewer", fam)),
        "far_reviewer": person("reviewer2", ("reviewer", other)),
        "owner": person("owner1", ("family_owner", fam)),
        "planner": person("planner1", ("planner", None)),
        "viewer": person("viewer1"),                      # new sign-ins start as Viewer
    }


def test_sign_in_rules(w):
    me = ok(w["viewer"].get("/api/v1/me"))
    assert me["roles"] == [{"role": "viewer", "family_id": None}]
    assert "bom.read_draft" not in me["permissions"]["*"]
    c = TestClient(app)
    assert c.post("/api/v1/auth/dev-login", json={"email": "someone@gmail.com"}).status_code == 403
    assert c.get("/api/v1/me").status_code == 401
    assert sql("SELECT count(*) FROM audit_log WHERE action = 'sign in failed'")[0] >= 1


def test_draft_to_released(w):
    d, r, o, v = w["designer"], w["reviewer"], w["owner"], w["viewer"]
    ok(d.post("/api/v1/items", json={"code": "PCBA-TST-1", "family_id": w["fam"], "level": "SFG"}))
    assert d.post("/api/v1/items", json={"code": "X-1", "family_id": w["other"], "level": "SFG"}).status_code == 403
    bom = ok(d.post("/api/v1/items/PCBA-TST-1/boms", json={"option_code": "BD"}))
    vid = bom["version"]["id"]
    assert bom["is_default"]

    lines = [{"part_id": w["p1"], "qty": 4, "ref_des": "R1 R2 R3 R4"}, {"part_id": w["p2"], "qty": 2, "ref_des": "C1  C2"}]
    assert ok(d.put(f"/api/v1/versions/{vid}/lines", json={"lock_version": 0, "lines": lines}))["lock_version"] == 1
    stale = d.put(f"/api/v1/versions/{vid}/lines", json={"lock_version": 0, "lines": lines})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale"
    bad = d.put(f"/api/v1/versions/{vid}/lines", json={"lock_version": 1, "lines": [{"part_id": w["p1"], "qty": 0}]})
    assert bad.status_code == 422 and "qty must be above 0" in bad.text

    # a viewer can't see the draft or change it, even by calling the API directly
    assert v.get(f"/api/v1/versions/{vid}").status_code == 403
    assert v.put(f"/api/v1/versions/{vid}/lines", json={"lock_version": 1, "lines": lines}).status_code == 403
    assert ok(v.get("/api/v1/items/PCBA-TST-1"))["boms"][0]["versions"] == []
    state = lambda c: next(i["default_state"] for i in ok(c.get("/api/v1/items?family=TST")) if i["code"] == "PCBA-TST-1")
    assert state(d) == "Draft" and state(v) is None          # the side panel doesn't even say a draft exists

    ok(d.post(f"/api/v1/versions/{vid}/submit", json={"note": "first"}))
    assert d.post(f"/api/v1/versions/{vid}/review", json={"decision": "Approve"}).status_code == 403   # not a reviewer
    assert w["far_reviewer"].post(f"/api/v1/versions/{vid}/review", json={"decision": "Approve"}).status_code == 403
    assert o.post(f"/api/v1/versions/{vid}/release").status_code == 409                                # not approved yet
    ok(r.post(f"/api/v1/versions/{vid}/review", json={"decision": "Approve", "comment": "fine"}))
    assert ok(o.post(f"/api/v1/versions/{vid}/release"))["state"] == "Released"

    got = ok(v.get(f"/api/v1/versions/{vid}"))
    assert got["version"]["state"] == "Released" and [l["ref_des"] for l in got["lines"]] == ["R1 R2 R3 R4", "C1 C2"]
    assert d.put(f"/api/v1/versions/{vid}/lines", json={"lock_version": 1, "lines": lines}).status_code == 409
    w["v1"], w["bom"] = vid, bom["id"]


def test_four_eyes_even_for_family_owner(w):
    o = w["owner"]
    bom = ok(o.post("/api/v1/items/PCBA-TST-1/boms", json={"option_code": "BC"}))
    vid = bom["version"]["id"]
    ok(o.put(f"/api/v1/versions/{vid}/lines", json={"lock_version": 0, "lines": [{"part_id": w["p1"], "qty": 1}]}))
    ok(o.post(f"/api/v1/versions/{vid}/submit"))
    r = o.post(f"/api/v1/versions/{vid}/review", json={"decision": "Approve"})
    assert r.status_code == 403 and "Four eyes" in r.text


def test_change_request_opens_next_draft_and_release_supersedes(w):
    cr = ok(w["planner"].post("/api/v1/change-requests", json={"bom_id": w["bom"], "reason": "R4 not fitted"}))
    assert cr["number"].startswith("ECO-")
    assert w["designer"].post(f"/api/v1/change-requests/{cr['id']}/decide", json={"decision": "approve"}).status_code == 403
    dec = ok(w["owner"].post(f"/api/v1/change-requests/{cr['id']}/decide", json={"decision": "approve"}))
    v2 = dec["draft"]["id"]
    d = w["designer"]
    cur = ok(d.get(f"/api/v1/versions/{v2}"))
    assert cur["version"]["change_request_id"] == cr["id"] and len(cur["lines"]) == 2
    ids = [l["id"] for l in cur["lines"] if l["part_id"] == w["p1"]]
    ok(d.post(f"/api/v1/versions/{v2}/lines/bulk", json={"lock_version": 0, "line_ids": ids, "op": "set_qty", "value": "3"}))
    diff = ok(d.get(f"/api/v1/versions/{w['v1']}/diff/{v2}"))
    assert [c["component"] for c in diff["changed"]] == ["P900001"] and not diff["added"] and not diff["removed"]
    ok(d.post(f"/api/v1/versions/{v2}/submit"))
    ok(w["reviewer"].post(f"/api/v1/versions/{v2}/review", json={"decision": "Approve"}))
    ok(w["owner"].post(f"/api/v1/versions/{v2}/release"))
    assert sql("SELECT state FROM bom_versions WHERE id = %s", w["v1"])[0] == "Superseded"
    assert sql("SELECT state FROM change_requests WHERE id = %s", cr["id"])[0] == "Done"
    assert sql("SELECT current_version_id FROM boms WHERE id = %s", w["bom"])[0] == v2
    w["v2"] = v2


def test_restore_old_version_as_draft(w):
    r = ok(w["designer"].post(f"/api/v1/versions/{w['v1']}/restore"))
    assert r["version_no"] == 3
    assert w["designer"].post(f"/api/v1/versions/{w['v1']}/restore").status_code == 409   # one open draft at a time
    assert w["planner"].post(f"/api/v1/versions/{w['v1']}/restore").status_code == 403


def test_cost_shortage_where_used(w):
    c = ok(w["planner"].get(f"/api/v1/versions/{w['v2']}/cost?qty=10"))
    assert float(c["total"]) == pytest.approx(10 * (3 * 0.5 + 2 * 0.25))
    s = ok(w["planner"].get(f"/api/v1/versions/{w['v2']}/shortage?qty=10"))
    assert [p["code"] for p in s["short"]] == ["P900002"]
    assert w["viewer"].get(f"/api/v1/versions/{w['v2']}/cost").status_code == 403
    used = ok(w["viewer"].get("/api/v1/where-used?part=P900001"))
    assert {r["item_code"] for r in used["direct"]} == {"PCBA-TST-1"}
    assert all(r["state"] == "Released" for r in used["direct"])   # the viewer sees no drafts here either


def test_exports(w):
    x = ok(w["planner"].post("/api/v1/exports", json={"version": w["v2"], "format": "xlsx"}))
    assert w["planner"].get(x["url"]).status_code == 200
    for fmt in ("pdf", "jlc", "pcbway"):
        assert ok(w["planner"].post("/api/v1/exports", json={"version": w["v2"], "format": fmt}))["url"]
    assert ok(w["viewer"].post("/api/v1/exports", json={"version": w["v2"], "format": "pdf"}))["url"]
    assert w["viewer"].post("/api/v1/exports", json={"version": w["v2"], "format": "xlsx"}).status_code == 403


def test_diptrace_import(w):
    d = w["designer"]
    csv = b"RefDes,Value,Name,Pattern,Quantity\nR1 R2,10k,RES-10K,0603,2\nC1,100n,P900002 CAP,0603,1\nU1,,MYSTERY-IC,SOIC8,1\n"
    imp = ok(d.post("/api/v1/imports", files={"file": ("board.csv", csv)}, data={"item": "PCBA-TST-1", "option_code": "AC"}))
    assert imp["status"] == "Waiting answers" and imp["open_questions"] == 1
    assert d.post(f"/api/v1/imports/{imp['id']}/save").status_code == 422
    imp = ok(d.post(f"/api/v1/imports/{imp['id']}/answers", json={"answers": {"2": w["p1"]}}))
    assert imp["open_questions"] == 0 and len(imp["preview"]["added"]) == 2
    saved = ok(d.post(f"/api/v1/imports/{imp['id']}/save"))
    lines = ok(d.get(f"/api/v1/versions/{saved['version_id']}"))["lines"]
    assert sorted(l["ref_des"] for l in lines) == ["C1", "R1 R2", "U1"]
    assert sql("SELECT count(*) FROM audit_log WHERE source = 'Import' AND entity_type = 'bom_lines'")[0] == 3


def test_audit_scope(w):
    rows = ok(w["owner"].get("/api/v1/audit?entity=PCBA-TST-1"))
    assert rows and all(r["family_id"] == w["fam"] for r in rows)
    assert w["designer"].get("/api/v1/audit").status_code == 403
    assert ok(w["admin"].get("/api/v1/audit?source=Import"))


def test_odoo_push_queues_a_job(w):
    assert w["designer"].post(f"/api/v1/versions/{w['v2']}/odoo-push").status_code == 403
    job = ok(w["owner"].post(f"/api/v1/versions/{w['v2']}/odoo-push"))
    assert job["status"] == "Queued" and job["target"] == "staging"
    assert w["owner"].post(f"/api/v1/versions/{w['v2']}/odoo-push").status_code == 409


def test_admin_roles_and_read_only(w):
    a = w["admin"]
    users = ok(a.get("/api/v1/admin/users"))
    vid = next(u["id"] for u in users if u["email"].startswith("viewer1"))
    ok(a.put(f"/api/v1/admin/users/{vid}/roles", json=[{"role": "viewer"}, {"role": "designer", "family_id": w["fam"]}]))
    assert sql("SELECT count(*) FROM audit_log WHERE entity_type = 'user_roles' AND after->>'user_id' = %s", str(vid))[0] >= 1
    assert a.put(f"/api/v1/admin/users/{a.uid}/roles", json=[]).status_code == 409    # never leave no Admin
    assert w["owner"].get("/api/v1/admin/users").status_code == 403

    ok(a.put("/api/v1/admin/settings", json={"read_only": True}))
    r = w["designer"].post("/api/v1/items", json={"code": "RO-1", "family_id": w["fam"], "level": "SFG"})
    assert r.status_code == 409 and "read-only" in r.text
    ok(a.put("/api/v1/admin/settings", json={"read_only": False}))

    ok(a.patch(f"/api/v1/admin/users/{vid}", json={"active": False}))   # switched off: logged out at once
    assert w["viewer"].get("/api/v1/me").status_code == 401

    h = ok(a.get("/api/v1/admin/health"))
    assert h["database"]["ok"]


def test_home_and_blue_off(w):
    home = ok(w["owner"].get("/api/v1/home"))
    assert any(f["kind"] for f in home["failed_pushes"]) or home["failed_pushes"] == []
    assert w["designer"].post("/api/v1/blue/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 409
