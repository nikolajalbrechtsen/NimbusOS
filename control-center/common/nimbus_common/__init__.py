# =============================================================================
# nimbus_common: code shared by the Control Center backend and nimbus-privd
# =============================================================================
# What: validators for every name, number and path that crosses a trust
#       boundary (validate.py) and the privd wire protocol (protocol.py).
# Why:  privd re-validates every argument with these same rules instead of
#       trusting the backend (docs/PLAN.md §4.3); one implementation keeps the
#       two sides from drifting apart.
# =============================================================================
