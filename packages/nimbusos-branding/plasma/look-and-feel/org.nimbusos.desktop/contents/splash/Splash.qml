/*
 * NimbusOS login splash (look-and-feel org.nimbusos.desktop), CC-BY-SA-4.0.
 *
 * What: shown by KSplash while Plasma starts: the Nimbus Dark background,
 *       the logo and a thin progress bar that follows KSplash's stages.
 * Why:  spec section 1 (Nimbus Dark theme), so the boot splash, login
 *       screen and desktop look like one product. KSplash sets "stage" from
 *       1 to 6 as the session starts.
 */
import QtQuick 2.15

Rectangle {
    id: root
    color: "#0b111b"
    property int stage

    Image {
        id: logo
        anchors.centerIn: parent
        anchors.verticalCenterOffset: -24
        source: "images/logo.svg"
        sourceSize.width: 112
        sourceSize.height: 112
    }

    Rectangle {
        id: track
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: logo.bottom
        anchors.topMargin: 40
        width: 200
        height: 4
        radius: 2
        color: "#1f2b3f"

        Rectangle {
            height: parent.height
            radius: parent.radius
            color: "#6cc4ff"
            width: parent.width * Math.min(root.stage, 6) / 6
            Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
        }
    }
}
