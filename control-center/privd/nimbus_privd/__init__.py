# =============================================================================
# nimbus_privd: the Nimbus Control Center's privileged helper (runs as root)
# =============================================================================
# What: server.py (socket, caller check, dispatch), registry.py (the verb
#       allowlist and argument specs), runner.py (commands as argument
#       lists), audit.py (journal), and one module per area of verbs:
#       updates.py, system.py, stacks.py.
# Why:  docs/PLAN.md §4.1, §4.3, NOTE-15: the backend stays unprivileged;
#       everything that needs root goes through this small, checked surface.
# =============================================================================
