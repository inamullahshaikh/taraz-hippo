"""The page reads and writes Postgres through the Sheets-shaped API; only invited people get in."""
import time
from pathlib import Path

import psycopg
import pytest

from app.auth import Denied, check_claims, clean_email, safe_next
from app.routers.sheets import cell, parse_range
from app.settings import S
from conftest import TEST_URL

# the page holds internal Taraz data, so it is only on the computer that deploys bomtree-app, not in the public repo
PAGE = Path(__file__).parent.parent / "app" / "static" / "bomtree.html"


def as_user(client, email):
    client.cookies.clear()
    return client.post("/api/v1/auth/dev-login", json={"email": email})


def get(client, rng, sid=None):
    return client.post("/api/v1/sheets/get_values", json={"spreadsheetId": sid or S.INDEX_SHEET_ID, "range": rng})


def test_only_invited_active_people_sign_in(client):
    assert as_user(client, "stranger@partner.org").status_code == 403
    assert as_user(client, "gone@partner.org").status_code == 403
    assert as_user(client, "migration@bomtree.local").status_code == 403     # system users never sign in
    assert as_user(client, "GUEST@partner.org").status_code == 200           # any domain, any case
    assert client.get("/api/v1/me").json()["roles"] == [{"role": "viewer", "family": None}]


def test_signed_out_gets_nothing(client):
    client.cookies.clear()
    assert get(client, "'Items'!A:L").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == ("/signin" if PAGE.exists() else "/admin")


def test_tabs_come_back_as_the_sheet_had_them(client):
    as_user(client, "admin@taraz.tech")
    items = get(client, "'Items'!A:L").json()["values"]
    assert items[0][:4] == ["Item", "Prefix", "Family", "Level"]
    assert items[1][:4] == ["USM-3IV", "FG", "USM", "FG"] and items[1][8] == "P016569"
    assert get(client, "'Items'!A:A").json()["values"] == [["Item"], ["USM-3IV"], ["PCBA-USM"], ["PackA-USM"]]
    assert get(client, "'Families'!A1:G1").json()["values"] == [["Family", "Status", "Old tools", "Notes", "Sheet ID", "Folder", "Owner"]]
    lines = get(client, "'Lines'!A:F", "sheet-usm").json()["values"]
    assert ["PCBA-USM", "R1K", "P000001", "2.5", "R1 R2"] in lines        # qty without trailing zeros
    assert ["USM-3IV", "PCBA-USM", "", "1"] in lines                       # sub-assembly line, no code, trailing cells cut
    boms = get(client, "'BOMs'!A:G", "sheet-usm").json()["values"]
    assert ["PackA-USM", "PackA-USM", "Standard", "Yes", "", "Needs info"] in boms
    assert get(client, "'Where used'!A:C").json()["values"][1:] == [["PCBA-USM", "", "USM"], ["R1K", "P000001", "USM"]]
    assert get(client, "'BOMs'!A:A", "no-such-sheet").status_code == 404


def test_viewers_see_released_boms_only(client):
    as_user(client, "guest@partner.org")
    keys = [r[0] for r in get(client, "'BOMs'!A:A", "sheet-usm").json()["values"]]
    assert keys == ["BOM", "USM-3IV", "PCBA-USM"]                          # the draft (Needs info) is hidden


@pytest.mark.skipif(not PAGE.exists(), reason="bomtree.html is not in the public repo")
def test_the_page_is_served_with_the_stand_in_first(client):
    as_user(client, "guest@partner.org")
    html = client.get("/").text
    assert html.index('src="/bt-shim.js"') < html.index("<title>BOMTree</title>")


def test_ranges_and_cells():
    assert parse_range("'Items'!A:L") == ("Items", 1, 12, 1, None)
    assert parse_range("'BOMs'!F12") == ("BOMs", 6, 6, 12, 12)
    assert parse_range("Lines!A2:F30") == ("Lines", 1, 6, 2, 30)
    assert parse_range("'Design nets'!A1:E1") == ("Design nets", 1, 5, 1, 1)
    from decimal import Decimal
    assert [cell(Decimal("2.0000")), cell(Decimal("0.5000")), cell(Decimal("1500")), cell(None)] == ["2", "0.5", "1500", ""]


def claims(**kw):
    base = {"aud": "test-client", "nonce": "n", "exp": time.time() + 300, "iat": time.time(),
            "iss": "https://accounts.google.com", "email_verified": True}
    return {**base, **kw}


def test_google_rules():
    assert check_claims("google", claims(email="someone@partner.org"), "n") == "someone@partner.org"
    assert check_claims("google", claims(email="a@taraz.tech", hd="taraz.tech"), "n") == "a@taraz.tech"
    for bad in (claims(email="a@taraz.tech"),                          # company address on a personal Google account
                claims(email="x@partner.org", email_verified=False),
                claims(email="x@partner.org", aud="other-app"),
                claims(email="x@partner.org", exp=time.time() - 600)):
        with pytest.raises(Denied):
            check_claims("google", bad, "n")
    with pytest.raises(Denied):
        check_claims("google", claims(email="x@partner.org"), "wrong-nonce")


def test_small_guards():
    assert clean_email(" A@B.org ") == "a@b.org"
    assert clean_email("a@b") is None and clean_email("a@@b.org") is None
    assert safe_next("//evil.com") == "/" and safe_next("https://evil.com") == "/" and safe_next("/x") == "/x"


# Editing: the page's Sheets-shaped saves land in Postgres ---------------------------------------------------------

def put(client, rng, values, sid=None, base=None, mode="update"):
    body = {"spreadsheetId": sid or S.INDEX_SHEET_ID, "range": rng, "values": values}
    if base is not None:
        body["base"] = base
    return client.post(f"/api/v1/sheets/{mode}_values", json=body)


def db(sql, *args):
    with psycopg.connect(TEST_URL) as c:
        return c.execute(sql, args).fetchall()


def versions(bom_key):
    return db("""SELECT v.version_no, v.state FROM bomtree.bom_versions v JOIN bomtree.boms b ON b.id = v.bom_id
                 WHERE b.legacy_key = %s ORDER BY 1""", bom_key)


def test_saving_a_bom_like_the_page_makes_a_new_released_version(client):
    as_user(client, "admin@taraz.tech")
    rows = get(client, "'Lines'!A:F", "sheet-usm").json()["values"][1:]
    rest = [r for r in rows if r[0] != "PCBA-USM"]
    mine = [["PCBA-USM", "R1K", "'P000001", 3, "R1 R2 R3", ""], ["PCBA-USM", "C100N", "", 2, "C1 C2", "new cap"]]
    out = rest + mine
    r = put(client, f"'Lines'!A2:F{len(out) + 1}", out, "sheet-usm")
    assert r.status_code == 200, r.text
    lines = get(client, "'Lines'!A:F", "sheet-usm").json()["values"]
    assert ["PCBA-USM", "R1K", "P000001", "3", "R1 R2 R3"] in lines and ["PCBA-USM", "C100N", "", "2", "C1 C2", "new cap"] in lines
    assert versions("PCBA-USM") == [(1, "Superseded"), (2, "Released")]     # history kept
    assert versions("USM-3IV") == [(1, "Released")]                          # untouched BOMs stay as they were


def test_status_cell_and_needs_info_draft_released_when_active(client):
    as_user(client, "admin@taraz.tech")
    keys = [r[0] for r in get(client, "'BOMs'!A:A", "sheet-usm").json()["values"]]
    r = put(client, f"'BOMs'!F{keys.index('PackA-USM') + 1}", [["Active"]], "sheet-usm")
    assert r.status_code == 200, r.text
    assert versions("PackA-USM") == [(1, "Released")]
    r = put(client, f"'BOMs'!F{keys.index('USM-3IV') + 1}", [["To review"]], "sheet-usm")
    assert db("SELECT status FROM bomtree.boms WHERE legacy_key = 'USM-3IV'") == [("Under review",)]


def test_new_item_bom_and_lines_appended_like_the_page(client):
    as_user(client, "admin@taraz.tech")
    assert put(client, "'Items'!A1", [["PCBA_SMD-USM", "PCBA_SMD", "USM", "SFG", "Proposed", "", "SMD board", "new", "", "", ""]],
               mode="append").status_code == 200
    assert put(client, "'BOMs'!A1", [["PCBA_SMD-USM", "PCBA_SMD-USM", "Standard", "Yes", "", "Active", ""]], "sheet-usm",
               mode="append").status_code == 200
    assert put(client, "'Lines'!A1", [["PCBA_SMD-USM", "R1K", "P000001", 10, "", ""]], "sheet-usm", mode="append").status_code == 200
    assert versions("PCBA_SMD-USM") == [(1, "Released")]
    as_user(client, "guest@partner.org")                                     # released: viewers see it too
    assert "PCBA_SMD-USM" in [r[0] for r in get(client, "'BOMs'!A:A", "sheet-usm").json()["values"]]


def test_removing_an_item_hides_it_but_keeps_it(client):
    as_user(client, "admin@taraz.tech")
    rows = get(client, "'Items'!A:L").json()["values"][1:]
    keep = [r for r in rows if r[0] != "PackA-USM"] + [[""] * 12]
    assert put(client, f"'Items'!A2:L{len(keep) + 1}", keep).status_code == 200
    assert "PackA-USM" not in [r[0] for r in get(client, "'Items'!A:A").json()["values"]]
    assert db("SELECT deleted_at IS NOT NULL FROM bomtree.items WHERE code = 'PackA-USM'") == [(True,)]


def test_log_rows_are_added_and_whole_tabs_replaced(client):
    as_user(client, "admin@taraz.tech")
    assert put(client, "'Log'!A1", [["'2026-11-12 10:00", "Admin", "BOM saved", "PCBA-USM", "2 lines"]], mode="append").status_code == 200
    assert get(client, "'Log'!A:E").json()["values"][-1][1:] == ["Admin", "BOM saved", "PCBA-USM", "2 lines"]
    pnp = [["Board", "Ref des", "Name", "Package", "X (mm)", "Y (mm)", "Side", "Rotation", "Value", "Source", "Loaded"],
           ["PCB-USM.1", "R1", "1k", "0603", "1.5", "2", "Top", "90", "RC0603", "test", "2026-11-12"]]
    assert put(client, "'PNP'!A1:K2", pnp, "sheet-usm").status_code == 200
    assert get(client, "'PNP'!A:K", "sheet-usm").json()["values"] == pnp


def test_viewers_cannot_change_anything(client):
    as_user(client, "guest@partner.org")
    assert put(client, "'BOMs'!F2", [["Archived"]], "sheet-usm").status_code == 403
    assert put(client, "'Log'!A1", [["x", "y", "z", "", ""]], mode="append").status_code == 403


def test_a_queued_change_never_overwrites_a_newer_one(client):
    as_user(client, "admin@taraz.tech")
    base = get(client, "'Lines'!A:F", "sheet-usm").json()["values"][1:]
    # meanwhile someone changes PCBA-USM live
    live = [r if r[0] != "PCBA-USM" or r[1] != "C100N" else ["PCBA-USM", "C100N", "", "5", "C1 C2", "new cap"] for r in base]
    assert put(client, f"'Lines'!A2:F{len(live) + 1}", live, "sheet-usm").status_code == 200
    # the queued change (made on the old copy) touches PCBA-USM and USM-3IV
    queued = [r if r[0] != "PCBA-USM" else r[:3] + ["9"] + r[4:] for r in base]
    queued = [r if r[0] != "USM-3IV" else r[:3] + ["2"] + r[4:] for r in queued]
    r = put(client, f"'Lines'!A2:F{len(queued) + 1}", queued, "sheet-usm", base=base)
    assert r.json()["conflicts"] == [{"tab": "Lines", "key": "PCBA-USM"}]
    lines = get(client, "'Lines'!A:F", "sheet-usm").json()["values"]
    assert ["PCBA-USM", "C100N", "", "5", "C1 C2", "new cap"] in lines      # the newer change stays
    assert ["USM-3IV", "PCBA-USM", "", "2"] in lines                         # the untouched-elsewhere change went in
