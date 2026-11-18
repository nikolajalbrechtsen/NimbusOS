# =============================================================================
# nimbus_control: the Nimbus Control Center backend (FastAPI)
# =============================================================================
# What: app.py builds the application; auth.py (token, launch codes, Host/
#       Origin checks, security headers), privd_client.py, fonts.py and one
#       *_service.py module per area of the API.
# Why:  spec section 3; runs as the unprivileged nimbus-control user on
#       127.0.0.1:7777 (docs/PLAN.md §4.1).
# =============================================================================
