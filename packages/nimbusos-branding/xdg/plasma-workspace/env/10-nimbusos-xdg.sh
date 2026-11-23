# shellcheck shell=sh
# /etc/xdg/plasma-workspace/env/10-nimbusos-xdg.sh (nimbusos-branding)
#
# Sourced by the Plasma session at login. It puts /etc/xdg/nimbusos first in
# XDG_CONFIG_DIRS, so NimbusOS defaults for files another package already
# owns in /etc/xdg (kcm-about-distrorc belongs to desktop-base) take
# precedence. A user's own ~/.config still wins over both.
case ":${XDG_CONFIG_DIRS:-/etc/xdg}:" in
	*:/etc/xdg/nimbusos:*) ;;
	*) XDG_CONFIG_DIRS="/etc/xdg/nimbusos:${XDG_CONFIG_DIRS:-/etc/xdg}"; export XDG_CONFIG_DIRS ;;
esac
