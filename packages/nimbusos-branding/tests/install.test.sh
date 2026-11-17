#!/bin/bash
# =============================================================================
# packages/nimbusos-branding/tests/install.test.sh
# =============================================================================
# What: installs the built nimbusos-branding package into the current
#       (throwaway) Debian system and checks it:
#         - every rendered file is there (wallpaper sizes, GRUB background,
#           logo, highlight and fonts, Plymouth images, global theme with its
#           splash logo, hicolor icon, kdeglobals with the colour groups,
#           the desktop-base theme directory);
#         - no @NIMBUS_ token was left unfilled;
#         - the three GRUB fonts carry exactly the names theme.txt uses;
#         - metadata.json files are valid JSON;
#         - the GRUB drop-in adds "splash" exactly once, even if sourced twice;
#         - desktop-base alternatives (docs/PLAN.md NOTE-56): NimbusOS is the
#           desktop-theme at priority 80 in auto mode; the background,
#           lockscreen, login, Plasma wallpaper, GRUB and debian-theme
#           Plymouth links all resolve to NimbusOS files; no alternative is
#           broken; an administrator's "update-alternatives --set" survives
#           a reinstall; a desktop-base reinstall keeps everything; removal
#           falls back to Debian's ceratopsian theme in auto mode;
#         - Plymouth switches to "nimbusos" on install and back to the
#           distribution default on removal.
#
# Why:  the theme spans GRUB, Plymouth, SDDM, Calamares and Plasma; a missing
#       file, a font name typo or a broken link would only show at boot.
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

# --- desktop-base alternatives helpers ---------------------------------------
DB=/usr/share/desktop-base
NT="$DB/nimbusos-theme"
DB_ALTERNATIVES="desktop-theme desktop-background desktop-background.xml desktop-lockscreen.xml desktop-login-background desktop-plasma5-wallpaper desktop-grub"

alt_field() {      # alt_field NAME FIELD -> value of "FIELD: value"
	update-alternatives --query "$1" | sed -n "s/^$2: //p"
}
alt_priority() {   # alt_priority NAME PATH -> priority of that entry
	update-alternatives --query "$1" | awk -v p="$2" '/^Alternative: /{a=substr($0,14)} /^Priority: /{if (a == p) print $2}'
}
outranks_all() {   # outranks_all NAME PATH: PATH's priority is strictly the highest
	update-alternatives --query "$1" | awk -v p="$2" '
		/^Alternative: /{a=substr($0,14)}
		/^Priority: /{ if (a == p) mine = $2 + 0; else if ($2 + 0 > other) other = $2 + 0 }
		END { exit !(mine > other) }'
}
resolves_to() {    # resolves_to LINK REGEX: the fully resolved LINK matches REGEX
	local target
	target="$(readlink -f "$1")" && [[ -e "$target" && "$target" =~ ^$2$ ]]
}
theme_is() {       # theme_is PATH MODE: desktop-theme points at PATH in MODE
	[[ "$(alt_field desktop-theme Value)" == "$1" && "$(alt_field desktop-theme Status)" == "$2" ]]
}
no_broken_alternatives() {   # every link and every registered path exists
	local name query path
	for name in $DB_ALTERNATIVES; do
		query="$(update-alternatives --query "$name")"
		path="$(sed -n 's/^Link: //p' <<<"$query")"
		[[ -e "$path" ]] || { echo "broken link: $path ($name)"; return 1; }
		while read -r path; do
			[[ -e "$path" ]] || { echo "missing target: $path ($name)"; return 1; }
		done < <(sed -n 's/^Alternative: //p' <<<"$query")
	done
	[[ -e "$DB/grub_background.sh" ]]
}
xml_images_exist() {   # every image a GNOME background XML names exists
	local xml="$1" img n=0
	while read -r img; do
		[[ -s "$img" ]] || return 1
		n=$((n + 1))
	done < <(sed -n 's|.*<size[^>]*>\([^<]*\)</size>.*|\1|p' "$xml")
	((n > 0))
}

deb="$(find "$DEBS" -maxdepth 1 -name 'nimbusos-branding_*.deb' -print -quit)"
[[ -n "$deb" ]] || { echo "branding install.test: no nimbusos-branding .deb in $DEBS" >&2; exit 2; }
apt-get update -q >/dev/null
# Plymouth and desktop-base first, to record Debian's look before NimbusOS
# is installed (desktop-base provides the ceratopsian Plymouth theme).
apt-get install -y -q --no-install-recommends python3 plymouth desktop-base >/tmp/branding-install.log 2>&1 \
	|| { tail -n 30 /tmp/branding-install.log; exit 1; }
theme_before="$(plymouth-set-default-theme)"
desktop_theme_before="$(alt_field desktop-theme Value)"
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
	/usr/share/wallpapers/NimbusSky/{metadata.json,gnome-background.xml,contents/screenshot.png,contents/images/1920x1080.svg,contents/images/1920x1080.png,contents/images/2560x1440.png,contents/images/3840x2160.png} \
	"$NT"/{grub/grub-16x9.png,grub/grub-4x3.png,grub/grub_background.sh,login/background.svg,lockscreen/gnome-background.xml,wallpaper/metadata.json,plymouth/nimbusos.plymouth} \
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
for xml in /usr/share/wallpapers/NimbusSky/gnome-background.xml "$NT/lockscreen/gnome-background.xml"; do
	check "every image in $xml exists" xml_images_exist "$xml"
done
check "SDDM theme is for the Qt 6 greeter" grep -qx 'QtVersion=6' /usr/share/sddm/themes/nimbus/metadata.desktop
check "GRUB drop-in adds splash exactly once, even if sourced twice" \
	sh -c 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"; . /etc/default/grub.d/10-nimbusos.cfg; . /etc/default/grub.d/10-nimbusos.cfg; test "$GRUB_CMDLINE_LINUX_DEFAULT" = "quiet splash" && test "$GRUB_DISTRIBUTOR" = NimbusOS'
check "the XDG env script puts /etc/xdg/nimbusos first, once" \
	sh -c 'XDG_CONFIG_DIRS=/etc/xdg; . /etc/xdg/plasma-workspace/env/10-nimbusos-xdg.sh; . /etc/xdg/plasma-workspace/env/10-nimbusos-xdg.sh; test "$XDG_CONFIG_DIRS" = /etc/xdg/nimbusos:/etc/xdg'

echo "desktop-base alternatives"
check "desktop-theme is NimbusOS, auto mode (before: '$desktop_theme_before')" theme_is "$NT" auto
check "NimbusOS desktop-theme priority is 80" test "$(alt_priority desktop-theme "$NT")" = 80
check "NimbusOS outranks every other desktop-theme" outranks_all desktop-theme "$NT"
check "desktop-background -> NimbusSky SVG" resolves_to /usr/share/images/desktop-base/desktop-background '/usr/share/wallpapers/NimbusSky/contents/images/1920x1080\.svg'
check "desktop-background.xml -> NimbusSky XML" resolves_to /usr/share/images/desktop-base/desktop-background.xml '/usr/share/wallpapers/NimbusSky/gnome-background\.xml'
check "desktop-lockscreen.xml -> NimbusOS XML" resolves_to /usr/share/images/desktop-base/desktop-lockscreen.xml "$NT/lockscreen/gnome-background\.xml"
check "login-background.svg (SDDM) -> NimbusOS" resolves_to /usr/share/images/desktop-base/login-background.svg "$NT/login/background\.svg"
check "DebianTheme (Plasma) -> NimbusSky" resolves_to /usr/share/wallpapers/DebianTheme '/usr/share/wallpapers/NimbusSky'
check "desktop-grub.png -> NimbusOS GRUB background" resolves_to /usr/share/images/desktop-base/desktop-grub.png "$NT/grub/grub-(16x9|4x3)\.png"
check "grub_background.sh -> NimbusOS" resolves_to "$DB/grub_background.sh" "$NT/grub/grub_background\.sh"
check "Plymouth debian-theme -> nimbusos theme" resolves_to /usr/share/plymouth/themes/debian-theme '/usr/share/plymouth/themes/nimbusos'
check "the paths SDDM, Plasma and the lock screen read exist" \
	sh -c 'b=$(sed -n "s/^background=//p" /usr/share/sddm/themes/nimbus/theme.conf); test -s "$b" && test -s /usr/share/wallpapers/DebianTheme/metadata.json'
check "no broken desktop-base alternative" no_broken_alternatives

echo "Administrator's choice and upgrades"
update-alternatives --set desktop-theme "$DB/emerald-theme" >/dev/null
apt-get install -y -q --reinstall "$deb" >/dev/null 2>&1
check "--set desktop-theme emerald survives a reinstall (manual mode)" theme_is "$DB/emerald-theme" manual
check "login background follows the administrator's theme" resolves_to /usr/share/images/desktop-base/login-background.svg "$DB/emerald-theme/login/background\.svg"
update-alternatives --auto desktop-theme >/dev/null
check "--auto desktop-theme returns to NimbusOS" theme_is "$NT" auto
apt-get install -y -q --reinstall desktop-base >/dev/null 2>&1
check "a desktop-base reinstall keeps NimbusOS active" theme_is "$NT" auto
check "  ... and re-registers its active-theme GRUB entry" test -n "$(alt_priority desktop-grub "$DB/active-theme/grub/grub-16x9.png")"
check "  ... and the GRUB background is still NimbusOS" resolves_to /usr/share/images/desktop-base/desktop-grub.png "$NT/grub/grub-(16x9|4x3)\.png"
check "  ... with no broken alternative" no_broken_alternatives

echo "Plymouth"
check "install selects the nimbusos theme (before: '$theme_before')" test "$(plymouth-set-default-theme)" = nimbusos
check "reinstall keeps it and doesn't touch an explicit choice" \
	sh -c "apt-get install -y -q --reinstall '$deb' >/dev/null 2>&1 && test \"\$(plymouth-set-default-theme)\" = nimbusos"

echo "Removal"
apt-get remove -y -q nimbusos-branding >/dev/null 2>&1
check "Plymouth gives back the theme in effect before ('$theme_before')" test "$(plymouth-set-default-theme)" = "$theme_before"
check "Plymouth: no Theme= line left behind" sh -c '! grep -Eq "^[[:space:]]*Theme[[:space:]]*=" /etc/plymouth/plymouthd.conf'
check "desktop-theme falls back to '$desktop_theme_before', auto mode" theme_is "$desktop_theme_before" auto
check "no NimbusOS entry left in desktop-theme" sh -c "! update-alternatives --query desktop-theme | grep -q nimbusos"
check "login background is Debian's again" resolves_to /usr/share/images/desktop-base/login-background.svg "$desktop_theme_before/login/background\.svg"
check "no broken desktop-base alternative after removal" no_broken_alternatives

echo
printf 'branding install.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
