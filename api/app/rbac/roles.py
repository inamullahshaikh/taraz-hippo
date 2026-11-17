"""The 18 permission codes and what each role holds. Compare line by line with the Build spec table."""

CODES = [
    "bom.read_released", "bom.read_draft", "cost.read", "export.create", "comment.create", "cr.create",
    "item.create", "bom.edit", "import.run", "bom.submit", "version.restore",
    "version.approve", "version.release", "odoo.push", "cr.approve", "item.archive", "audit.read",
    "admin.manage",
]

READ = {"bom.read_released", "bom.read_draft", "cost.read", "export.create", "comment.create", "cr.create"}
MAKE = {"item.create", "bom.edit", "import.run", "bom.submit", "version.restore"}

ROLES = {
    "admin":        set(CODES),
    "family_owner": set(CODES) - {"admin.manage"},
    "designer":     READ | MAKE,
    "reviewer":     READ | {"version.approve"},
    "planner":      READ,
    "viewer":       {"bom.read_released"},
}

# These apply in every family for any role that holds them; everything else only in the role's family.
EVERY_FAMILY = {"bom.read_released", "cost.read", "export.create", "comment.create", "cr.create"}
EVERY_FAMILY_FOR = {("planner", "bom.read_draft")}

# Blue can never do these, whatever the person chatting holds.
BLUE_DENY = {"version.approve", "version.release", "odoo.push", "cr.approve", "item.archive", "admin.manage"}
