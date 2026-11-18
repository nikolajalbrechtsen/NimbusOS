#!/usr/bin/python3
# =============================================================================
# nimbus-privd.py: entry point of nimbus-privd.service (root, socket-activated)
# =============================================================================
# What: puts the Control Center's Python packages on the module path and
#       starts the privileged helper's server (nimbus_privd.server).
# Why:  docs/PLAN.md §4.1: the unit runs this file; the code lives in
#       /usr/lib/nimbus-control/python/ next to the backend's.
# =============================================================================
import sys

sys.path.insert(0, "/usr/lib/nimbus-control/python")

from nimbus_privd.server import main  # noqa: E402

sys.exit(main())
