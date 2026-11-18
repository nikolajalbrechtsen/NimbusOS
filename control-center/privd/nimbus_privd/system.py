# =============================================================================
# nimbus_privd/system.py: system verbs
# =============================================================================
# What: system.reboot restarts the computer (systemctl reboot). Only ever on
#       the user's request: the Updates page's "Restart now".
# Why:  spec section 4.4: the system never reboots by itself.
# =============================================================================

from . import runner
from .registry import REGISTRY

SYSTEMCTL = "/usr/bin/systemctl"


@REGISTRY.verb("system.reboot", lock="power")
def reboot(ctx, args):
    runner.run([SYSTEMCTL, "reboot"], timeout=30)
    return {"rebooting": True}
