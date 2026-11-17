/*
 * NimbusOS installer slideshow (nimbusos-branding), CC-BY-SA-4.0.
 *
 * What: three slides shown while Calamares installs: what NimbusOS is, what
 *       the Control Center does, and how updates work.
 * Why:  spec section 1 (Calamares branding). Uses Calamares' slideshow API 2
 *       (Presentation/Slide from calamares.slideshow), as Debian's own
 *       show.qml does; text only plus the logo, so it needs no images beyond
 *       logo.svg.
 */
import QtQuick 2.0
import calamares.slideshow 1.0

Presentation {
    id: presentation

    Timer {
        interval: 15000
        repeat: true
        running: true
        onTriggered: presentation.goToNextSlide()
    }

    Rectangle {
        anchors.fill: parent
        color: "#0b111b"
        z: -1
    }

    Slide {
        Column {
            anchors.centerIn: parent
            width: parent.width * 0.8
            spacing: 18
            Image {
                anchors.horizontalCenter: parent.horizontalCenter
                source: "logo.svg"
                sourceSize.width: 96
                sourceSize.height: 96
            }
            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                color: "#e7eef8"
                font.pixelSize: 26
                font.weight: Font.DemiBold
                text: qsTr("Welcome to NimbusOS")
            }
            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: "#8d9db3"
                font.pixelSize: 15
                text: qsTr("A Debian 13 desktop with KDE Plasma, Docker apps, virtual machines and network shares, all managed from one place. The installation finishes in a few minutes.")
            }
        }
    }

    Slide {
        Column {
            anchors.centerIn: parent
            width: parent.width * 0.8
            spacing: 18
            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                color: "#e7eef8"
                font.pixelSize: 24
                font.weight: Font.DemiBold
                text: qsTr("Nimbus Control Center")
            }
            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: "#8d9db3"
                font.pixelSize: 15
                text: qsTr("See what uses your machine, install apps from the catalog with one click, run virtual machines, and turn spare disks into shares for Windows, macOS and Linux. Find it in the panel after you log in.")
            }
        }
    }

    Slide {
        Column {
            anchors.centerIn: parent
            width: parent.width * 0.8
            spacing: 18
            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                color: "#e7eef8"
                font.pixelSize: 24
                font.weight: Font.DemiBold
                text: qsTr("Updates keep coming")
            }
            Text {
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: "#8d9db3"
                font.pixelSize: 15
                text: qsTr("Debian security updates install themselves every day. NimbusOS updates follow the policy you choose on the Updates page. Your computer never restarts on its own.")
            }
        }
    }
}
