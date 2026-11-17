# =============================================================================
# grub_background.sh for the NimbusOS desktop-base theme (nimbusos-branding)
# =============================================================================
# What: the desktop-grub.sh slave of desktop-base's "desktop-grub"
#       alternative. /etc/grub.d/05_debian_theme sources it for the menu
#       background and colours when no GRUB theme is in use.
# Why:  desktop-base registers its active-theme GRUB entries only if this
#       file exists in the active theme (docs/PLAN.md NOTE-56). With the
#       NimbusOS GRUB theme active (GRUB_THEME), the theme draws the menu.
# =============================================================================
WALLPAPER=/usr/share/images/desktop-base/desktop-grub.png
COLOR_NORMAL=light-gray/black
COLOR_HIGHLIGHT=black/light-cyan
