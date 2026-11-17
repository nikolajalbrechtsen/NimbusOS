# =============================================================================
# Makefile for nimbusos-branding
# =============================================================================
# What: renders everything that must be raster or compiled from the SVG and
#       text sources into build/, which debian/install ships:
#         - Nimbus Sky wallpaper PNGs (1920x1080, 2560x1440, 3840x2160)
#         - GRUB theme: background, logo, menu highlight, three .pf2 fonts
#           made from Inter with grub-mkfont (names set with -n so theme.txt
#           can refer to them exactly)
#         - Plymouth theme PNGs
#         - the global theme with its splash logo, the wallpaper package,
#           the hicolor "nimbusos" icon
#         - /etc/xdg/kdeglobals = xdg/kdeglobals.head + the colour groups of
#           color-schemes/NimbusDark.colors
# Why:  the repository holds no binary files (docs/PLAN.md NOTE-12).
#       Called by debian/rules (dh_auto_build).
# =============================================================================

RSVG          ?= rsvg-convert
GRUB_MKFONT   ?= grub-mkfont
INTER_REGULAR ?= /usr/share/fonts/opentype/inter/Inter-Regular.otf
INTER_BOLD    ?= /usr/share/fonts/opentype/inter/Inter-Bold.otf
B             := build
WALLPAPER_SIZES := 1920x1080 2560x1440 3840x2160
LNF           := org.nimbusos.desktop

width  = $(word 1,$(subst x, ,$1))
height = $(word 2,$(subst x, ,$1))

.PHONY: all clean wallpaper grub plymouth plasma icons xdg

all: wallpaper grub plymouth plasma icons xdg

# --- Wallpaper package ----------------------------------------------------------
WALLPAPER_PNGS := $(foreach s,$(WALLPAPER_SIZES),$(B)/wallpapers/NimbusSky/contents/images/$(s).png)

wallpaper: $(WALLPAPER_PNGS) $(B)/wallpapers/NimbusSky/metadata.json $(B)/wallpapers/NimbusSky/contents/screenshot.png

$(B)/wallpapers/NimbusSky/contents/images/%.png: art/wallpaper.svg
	mkdir -p $(@D)
	$(RSVG) --format png --width $(call width,$*) --height $(call height,$*) --output $@ $<

$(B)/wallpapers/NimbusSky/contents/screenshot.png: art/wallpaper.svg
	mkdir -p $(@D)
	$(RSVG) --format png --width 400 --height 225 --output $@ $<

$(B)/wallpapers/NimbusSky/metadata.json: plasma/wallpapers/NimbusSky/metadata.json
	mkdir -p $(@D)
	cp $< $@

# --- GRUB theme -------------------------------------------------------------------
grub: $(B)/grub/background.png $(B)/grub/logo.png $(B)/grub/select_c.png \
      $(B)/grub/inter-regular-16.pf2 $(B)/grub/inter-bold-16.pf2 $(B)/grub/inter-bold-28.pf2 \
      $(B)/grub/theme.txt

$(B)/grub/background.png: art/wallpaper.svg
	mkdir -p $(@D)
	$(RSVG) --format png --width 1920 --height 1080 --output $@ $<

$(B)/grub/logo.png: art/logo.svg
	mkdir -p $(@D)
	$(RSVG) --format png --width 80 --height 80 --output $@ $<

$(B)/grub/select_c.png: art/select.svg
	mkdir -p $(@D)
	$(RSVG) --format png --output $@ $<

$(B)/grub/inter-regular-16.pf2: $(INTER_REGULAR)
	mkdir -p $(@D)
	$(GRUB_MKFONT) --name="Inter Regular 16" --size=16 --output=$@ $<

$(B)/grub/inter-bold-16.pf2: $(INTER_BOLD)
	mkdir -p $(@D)
	$(GRUB_MKFONT) --name="Inter Bold 16" --size=16 --output=$@ $<

$(B)/grub/inter-bold-28.pf2: $(INTER_BOLD)
	mkdir -p $(@D)
	$(GRUB_MKFONT) --name="Inter Bold 28" --size=28 --output=$@ $<

$(B)/grub/theme.txt: grub/theme.txt
	mkdir -p $(@D)
	cp $< $@

# --- Plymouth theme -------------------------------------------------------------
PLYMOUTH_PNGS := $(B)/plymouth/logo.png $(B)/plymouth/progress-track.png $(B)/plymouth/progress-bar.png \
                 $(B)/plymouth/entry.png $(B)/plymouth/bullet.png

plymouth: $(PLYMOUTH_PNGS)

$(B)/plymouth/logo.png: art/logo.svg
	mkdir -p $(@D)
	$(RSVG) --format png --width 128 --height 128 --output $@ $<

$(B)/plymouth/%.png: art/%.svg
	mkdir -p $(@D)
	$(RSVG) --format png --output $@ $<

# --- Global theme (look-and-feel) -------------------------------------------------
plasma: $(B)/look-and-feel/$(LNF)/metadata.json

$(B)/look-and-feel/$(LNF)/metadata.json: $(shell find plasma/look-and-feel/$(LNF) -type f) art/logo.svg
	rm -rf $(B)/look-and-feel/$(LNF)
	mkdir -p $(B)/look-and-feel
	cp -R plasma/look-and-feel/$(LNF) $(B)/look-and-feel/$(LNF)
	mkdir -p $(B)/look-and-feel/$(LNF)/contents/splash/images
	cp art/logo.svg $(B)/look-and-feel/$(LNF)/contents/splash/images/logo.svg

# --- Icons -------------------------------------------------------------------------
icons: $(B)/icons/nimbusos.svg

$(B)/icons/nimbusos.svg: art/logo.svg
	mkdir -p $(@D)
	cp $< $@

# --- /etc/xdg/kdeglobals -----------------------------------------------------------
xdg: $(B)/xdg/kdeglobals

$(B)/xdg/kdeglobals: xdg/kdeglobals.head color-schemes/NimbusDark.colors
	mkdir -p $(@D)
	cp xdg/kdeglobals.head $@
	awk '/^\[/ { keep = ($$0 ~ /^\[(ColorEffects|Colors):/) } keep' color-schemes/NimbusDark.colors >> $@

clean:
	rm -rf $(B)
