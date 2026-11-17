#!/bin/bash
# =============================================================================
# packages/nimbusos-branding/tests/install.test.sh
# =============================================================================
# What: installs the built nimbusos-branding package into the current
#       (throwaway) Debian system and checks it:
#         - every rendered file is there (wallpaper sizes, GRUB background,
#           logo, highlight and fonts, Plymouth images, global theme with its
#           splash logo, hicolor icon, kdeglobals with the colour groups);
#         - no @NIMBUS_ token was left unfilled;
#         - the three GRUB fonts carry exactly the names theme.txt uses;
#         - metadata.json files are valid JSON;
#         - the GRUB drop-in adds "splash" exactly once, even if sourced twice;
#         - Plymouth switches to "nimbusos" on install and back to the
#           distribution default on removal.
#
# Why:  the theme spans GRUB, Plymouth, SDDM, Calamares and Plasma; a missing
#       file or a font name typo would only show at boot.
#
# Usage: packages/nimbusos-branding/tests/install.test.sh DEBS_DIR
#        Throwaway container only (tools/dev-shell.sh): it installs packages.
# =============================================================================
set -Eeuo pipefail

DEBS="$(readlink -f "${1:?usage: install.test.sh DEBS_DIR}")"
[[ "$(id -u)" -eq 0 ]] || { echo "branding install.test: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] \
	|| { echo "branding install.test: refusing to run outside a container (it changes the system)" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

passed=0
failed=0
check() {   # check NAME COMMAND...
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}

deb="$(find "$DEBS" -maxdepth 1 -name 'nimbusos-branding_*.deb' -print -quit)"
[[ -n "$deb" ]] || { echo "branding install.test: no nimbusos-branding .deb in $DEBS" >&2; exit 2; }
apt-get update -q >/dev/null
# Plymouth first, to record the theme in effect before NimbusOS is installed
# (Debian's default theme comes from desktop-base; without it, "text").
apt-get install -y -q --no-install-recommends python3 plymouth >/tmp/branding-install.log 2>&1 \
	|| { tail -n 30 /tmp/branding-install.log; exit 1; }
theme_before="$(plymouth-set-default-theme)"
apt-get install -y -q --no-install-recommends "$deb" >>/tmp/branding-install.log 2>&1 \
	|| { tail -n 30 /tmp/branding-install.log; exit 1; }

echo "Files"
G=/usr/share/grub/themes/nimbusos
for f in "$G/theme.txt" "$G/background.png" "$G/logo.png" "$G/select_c.png" \
	"$G/inter-regular-16.pf2" "$G/inter-bold-16.pf2" "$G/inter-bold-28.pf2" \
	/usr/share/plymouth/themes/nimbusos/{nimbusos.plymouth,nimbusos.script,logo.png,progress-track.png,progress-bar.png,entry.png,bullet.png} \
	/usr/share/sddm/themes/nimbus/{Main.qml,metadata.desktop,theme.conf,logo.svg} \
	/usr/share/calamares/branding/nimbusos/{branding.desc,show.qml,logo.svg} \
	/usr/share/plasma/look-and-feel/org.nimbusos.desktop/{metadata.json,contents/defaults,contents/layouts/org.kde.plasma.desktop-layout.js,contents/splash/Splash.qml,contents/splash/images/logo.svg} \
	/usr/share/wallpapers/NimbusSky/{metadata.json,contents/screenshot.png,contents/images/1920x1080.png,contents/images/2560x1440.png,contents/images/3840x2160.png} \
	/usr/share/color-schemes/NimbusDark.colors /usr/share/icons/hicolor/scalable/apps/nimbusos.svg \
	/etc/xdg/{kdeglobals,kwinrc,plasmarc,ksplashrc,kscreenlockerrc} /etc/xdg/nimbusos/kcm-about-distrorc \
	/etc/xdg/plasma-workspace/env/10-nimbusos-xdg.sh /etc/sddm.conf.d/10-nimbusos.conf /etc/default/grub.d/10-nimbusos.cfg; do
	check "present: $f" test -s "$f"
done

echo "Content"
check "no unfilled @NIMBUS_ token installed" sh -c "! dpkg -L nimbusos-branding | xargs -d '\n' grep -lIs '@NIMBUS_'"
for font in "Inter Regular 16" "Inter Bold 16" "Inter Bold 28"; do
	check "theme.txt uses \"$font\" and a .pf2 is named exactly that" \
		sh -c "grep -q '\"$font\"' $G/theme.txt && grep -alF '$font' $G/*.pf2"
done
check "kdeglobals selects the global theme" grep -qx 'LookAndFeelPackage=org.nimbusos.desktop' /etc/xdg/kdeglobals
check "kdeglobals carries the Nimbus Dark colours" sh -c 'grep -qx "\[Colors:View\]" /etc/xdg/kdeglobals && grep -qx "DecorationFocus=108,196,255" /etc/xdg/kdeglobals'
for json in /usr/share/plasma/look-and-feel/org.nimbusos.desktop/metadata.json /usr/share/wallpapers/NimbusSky/metadata.json; do
	check "valid JSON: $json" python3 -m json.tool "$json"
done
check "SDDM theme is for the Qt 6 greeter" grep -qx 'QtVersion=6' /usr/share/sddm/themes/nimbus/metadata.desktop
check "GRUB drop-in adds splash exactly once, even if sourced twice" \
	sh -c 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"; . /etc/default/grub.d/10-nimbusos.cfg; . /etc/default/grub.d/10-nimbusos.cfg; test "$GRUB_CMDLINE_LINUX_DEFAULT" = "quiet splash" && test "$GRUB_DISTRIBUTOR" = NimbusOS'
check "the XDG env script puts /etc/xdg/nimbusos first, once" \
	sh -c 'XDG_CONFIG_DIRS=/etc/xdg; . /etc/xdg/plasma-workspace/env/10-nimbusos-xdg.sh; . /etc/xdg/plasma-workspace/env/10-nimbusos-xdg.sh; test "$XDG_CONFIG_DIRS" = /etc/xdg/nimbusos:/etc/xdg'

echo "Plymouth"
check "install selects the nimbusos theme (before: '$theme_before')" test "$(plymouth-set-default-theme)" = nimbusos
check "reinstall keeps it and doesn't touch an explicit choice" \
	sh -c "apt-get install -y -q --reinstall '$deb' >/dev/null 2>&1 && test \"\$(plymouth-set-default-theme)\" = nimbusos"
apt-get remove -y -q nimbusos-branding >/dev/null 2>&1
check "removal gives back the theme in effect before ('$theme_before')" test "$(plymouth-set-default-theme)" = "$theme_before"
check "removal leaves no Theme= line behind" sh -c '! grep -Eq "^[[:space:]]*Theme[[:space:]]*=" /etc/plymouth/plymouthd.conf'

echo
printf 'branding install.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
