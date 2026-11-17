"""One test per cell of the permission table in the Build spec (18 codes x 6 roles = 108), written out here
independently of rbac/roles.py so the two can be compared, plus the family scope and Blue's deny list.
Y = everywhere, F = only in the family the role was given for, - = never."""
import pytest

from app.rbac import can
from app.rbac.roles import BLUE_DENY, CODES

MATRIX = """
                   admin  family_owner  designer  reviewer  planner  viewer
bom.read_released  Y      Y             Y         Y         Y        Y
bom.read_draft     Y      F             F         F         Y        -
cost.read          Y      Y             Y         Y         Y        -
export.create      Y      Y             Y         Y         Y        -
comment.create     Y      Y             Y         Y         Y        -
cr.create          Y      Y             Y         Y         Y        -
item.create        Y      F             F         -         -        -
bom.edit           Y      F             F         -         -        -
import.run         Y      F             F         -         -        -
bom.submit         Y      F             F         -         -        -
version.restore    Y      F             F         -         -        -
version.approve    Y      F             -         F         -        -
version.release    Y      F             -         -         -        -
odoo.push          Y      F             -         -         -        -
cr.approve         Y      F             -         -         -        -
item.archive       Y      F             -         -         -        -
audit.read         Y      F             -         -         -        -
admin.manage       Y      -             -         -         -        -
"""
HEAD, *ROWS = [l.split() for l in MATRIX.strip().splitlines()]
CELLS = [(r[0], role, cell) for r in ROWS for role, cell in zip(HEAD, r[1:])]
MINE, OTHER = 1, 2


def test_table_covers_every_code():
    assert [r[0] for r in ROWS] == CODES and len(CELLS) == 108


@pytest.mark.parametrize("code,role,cell", CELLS, ids=[f"{c}-{r}" for c, r, _ in CELLS])
def test_cell(code, role, cell):
    roles = [(role, None)] if role == "admin" else [(role, MINE)]   # Admin is always given for all families
    assert can(roles, code, MINE) == (cell in "YF"), "in the role's own family"
    assert can(roles, code, OTHER) == (cell == "Y"), "in another family"


def test_role_for_all_families_applies_everywhere():
    assert can([("designer", None)], "bom.edit", OTHER)
    assert not can([("designer", MINE)], "bom.edit", OTHER)
    assert can([("designer", MINE), ("reviewer", OTHER)], "version.approve", OTHER)


def test_actions_without_a_family_need_an_all_families_role():
    assert not can([("family_owner", MINE)], "audit.read", None)
    assert can([("family_owner", None)], "audit.read", None)


@pytest.mark.parametrize("code", CODES)
def test_blue_deny_list(code):
    assert can([("admin", None)], code, MINE, blue=True) == (code not in BLUE_DENY)
    assert BLUE_DENY == {"version.approve", "version.release", "odoo.push", "cr.approve", "item.archive", "admin.manage"}
