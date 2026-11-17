# =============================================================================
# control-center/migrations/0001_initial.py: state schema 1
# =============================================================================
# What: creates the three state files of schema 1 where they don't exist yet,
#       and leaves any that do exist as they are:
#         settings.json  {}                 Control Center settings; the
#                                           backend fills in defaults on read
#         shares.json    {"shares": []}     share definitions (privd renders
#                                           /etc/samba/nimbus-shares.conf)
#         stacks.json    {"stacks": {}}     installed app stacks by name, each
#                                           with the catalog app id and the
#                                           manifest template_version it was
#                                           created from (NOTE-60)
# Why:  every later step starts from a known layout (docs/PLAN.md NOTE-59).
#       nimbus-migrate runs this once, on the first start after installing.
# =============================================================================

DESCRIPTION = "create settings.json, shares.json and stacks.json"

INITIAL = {
    "settings.json": {},
    "shares.json": {"shares": []},
    "stacks.json": {"stacks": {}},
}


def migrate(ctx):
    for name, content in INITIAL.items():
        if not ctx.exists(name):
            ctx.write_json(name, content)
            ctx.log(f"created {name}")
