from fastapi import HTTPException

from .roles import BLUE_DENY, CODES, EVERY_FAMILY, EVERY_FAMILY_FOR, ROLES


def can(roles, code, family_id=None, blue=False):
    """roles: [(role_code, family_id or None)]. family_id None means the action has no family (admin pages)."""
    if blue and code in BLUE_DENY:
        return False
    return any(code in ROLES[role] and (fam is None or fam == family_id or code in EVERY_FAMILY
                                        or (role, code) in EVERY_FAMILY_FOR)
               for role, fam in roles)


def forbid(code, why=None):
    raise HTTPException(403, {"message": why or f"You do not have the right '{code}' here", "permission": code})


def permissions(roles, family_ids, blue=False):
    """Per family: the codes the person holds there. '*' = codes held everywhere."""
    out = {"*": [c for c in CODES if all_families(roles, c, blue)]}
    for f in family_ids:
        out[str(f)] = [c for c in CODES if can(roles, c, f, blue)]
    return out


def all_families(roles, code, blue=False):
    if blue and code in BLUE_DENY:
        return False
    return any(code in ROLES[r] and (f is None or code in EVERY_FAMILY or (r, code) in EVERY_FAMILY_FOR)
               for r, f in roles)
