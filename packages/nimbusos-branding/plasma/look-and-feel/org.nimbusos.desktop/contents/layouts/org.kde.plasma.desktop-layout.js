/*
 * NimbusOS default desktop layout (look-and-feel org.nimbusos.desktop),
 * GPL-3.0-or-later.
 *
 * What: the panel and desktop a user gets at first login: one bottom panel
 *       (Plasma 6 panels float by default) with the application launcher,
 *       an icon task manager with Nimbus Control Center, Dolphin, Firefox and
 *       Konsole pinned, the system tray, the clock and "show desktop"; the
 *       Nimbus Sky wallpaper on every desktop.
 * Why:  spec section 1 ("a floating bottom panel with the Control Center
 *       pinned"). Plasma runs this script once per new user, so it never
 *       touches an existing user's panel (docs/PLAN.md NOTE-10). It uses
 *       the same scripting calls as Plasma's own default panel template.
 */

var panel = new Panel;
var panelScreen = panel.screen;
panel.location = "bottom";

// Same height rule as Plasma's default panel (an even number of pixels).
panel.height = 2 * Math.floor(gridUnit * 2.5 / 2);

// Keep the panel to the width of a 21:9 screen on very wide monitors.
var maximumAspectRatio = 21 / 9;
if (panel.formFactor === "horizontal") {
    var geo = screenGeometry(panelScreen);
    var maximumWidth = Math.ceil(geo.height * maximumAspectRatio);
    if (geo.width > maximumWidth) {
        panel.alignment = "center";
        panel.minimumLength = maximumWidth;
        panel.maximumLength = maximumWidth;
    }
}

panel.addWidget("org.kde.plasma.kickoff");

var tasks = panel.addWidget("org.kde.plasma.icontasks");
tasks.currentConfigGroup = ["General"];
tasks.writeConfig("launchers", [
    "applications:nimbus-control.desktop",
    "applications:org.kde.dolphin.desktop",
    "applications:firefox-esr.desktop",
    "applications:org.kde.konsole.desktop"
]);

panel.addWidget("org.kde.plasma.marginsseparator");
panel.addWidget("org.kde.plasma.systemtray");
panel.addWidget("org.kde.plasma.digitalclock");
panel.addWidget("org.kde.plasma.showdesktop");

var desktopsArray = desktopsForActivity(currentActivity());
for (var j = 0; j < desktopsArray.length; j++) {
    desktopsArray[j].wallpaperPlugin = "org.kde.image";
}
