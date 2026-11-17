/*
 * NimbusOS SDDM theme "nimbus" (nimbusos-branding), CC-BY-SA-4.0.
 *
 * What: the login screen in Nimbus Dark: the Nimbus Sky background, a clock,
 *       and a translucent card with the user name, password, session choice
 *       and Log in, plus Restart and Shut down.
 * Why:  spec section 1 (SDDM theme). Written for SDDM's Qt 6 greeter
 *       (metadata.desktop QtVersion=6) using only QtQuick, QtQuick.Controls
 *       and the context objects SDDM provides (sddm, userModel, sessionModel,
 *       config). The session list starts on SDDM's last-used session; for a
 *       new install that is Plasma (Wayland) (docs/PLAN.md NOTE-08).
 */
import QtQuick
import QtQuick.Controls

Rectangle {
    id: root
    width: 1920
    height: 1080
    color: "#0b111b"

    readonly property color accent: config.accent || "#6cc4ff"
    readonly property color fg: "#e7eef8"
    readonly property color muted: "#8d9db3"
    readonly property color bad: "#ff7070"
    readonly property string fontFamily: config.fontFamily || "Inter"

    Image {
        anchors.fill: parent
        source: config.background
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
    }

    Column {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.topMargin: parent.height * 0.12
        spacing: 4
        Text {
            id: clock
            anchors.horizontalCenter: parent.horizontalCenter
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: 64
            font.weight: Font.Light
            function refresh() { text = Qt.formatTime(new Date(), Qt.locale().timeFormat(Locale.ShortFormat)) }
            Component.onCompleted: refresh()
        }
        Text {
            id: date
            anchors.horizontalCenter: parent.horizontalCenter
            color: root.muted
            font.family: root.fontFamily
            font.pixelSize: 18
            function refresh() { text = Qt.formatDate(new Date(), Qt.locale().dateFormat(Locale.LongFormat)) }
            Component.onCompleted: refresh()
        }
        Timer {
            interval: 1000; running: true; repeat: true
            onTriggered: { clock.refresh(); date.refresh() }
        }
    }

    Rectangle {
        id: card
        width: 380
        height: form.implicitHeight + 56
        anchors.centerIn: parent
        anchors.verticalCenterOffset: 60
        radius: 16
        color: Qt.rgba(18 / 255, 26 / 255, 39 / 255, 0.82)
        border.color: Qt.rgba(150 / 255, 178 / 255, 214 / 255, 0.18)

        Column {
            id: form
            anchors.fill: parent
            anchors.margins: 28
            spacing: 14

            Image {
                anchors.horizontalCenter: parent.horizontalCenter
                source: config.logo
                sourceSize.width: 64
                sourceSize.height: 64
            }

            TextField {
                id: userField
                width: parent.width
                placeholderText: qsTr("User name")
                text: userModel.lastUser
                font.family: root.fontFamily
                font.pixelSize: 15
                color: root.fg
                placeholderTextColor: root.muted
                background: Rectangle { radius: 8; color: "#182233"; border.color: userField.activeFocus ? root.accent : "#26324a" }
                KeyNavigation.tab: passwordField
                onAccepted: passwordField.forceActiveFocus()
            }

            TextField {
                id: passwordField
                width: parent.width
                placeholderText: qsTr("Password")
                echoMode: TextInput.Password
                font.family: root.fontFamily
                font.pixelSize: 15
                color: root.fg
                placeholderTextColor: root.muted
                background: Rectangle { radius: 8; color: "#182233"; border.color: passwordField.activeFocus ? root.accent : "#26324a" }
                KeyNavigation.tab: sessionBox
                onAccepted: root.login()
                Component.onCompleted: if (userField.text !== "") forceActiveFocus(); else userField.forceActiveFocus()
            }

            ComboBox {
                id: sessionBox
                width: parent.width
                model: sessionModel
                textRole: "name"
                currentIndex: sessionModel.lastIndex
                font.family: root.fontFamily
                font.pixelSize: 14
                KeyNavigation.tab: loginButton
            }

            Text {
                id: message
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: root.bad
                font.family: root.fontFamily
                font.pixelSize: 13
                text: ""
                visible: text !== ""
            }

            Button {
                id: loginButton
                width: parent.width
                height: 40
                text: qsTr("Log in")
                font.family: root.fontFamily
                font.pixelSize: 15
                font.weight: Font.DemiBold
                contentItem: Text {
                    text: loginButton.text
                    font: loginButton.font
                    color: "#06182a"
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle { radius: 8; color: loginButton.down ? Qt.darker(root.accent, 1.15) : root.accent }
                onClicked: root.login()
            }
        }
    }

    Row {
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 32
        spacing: 12
        Button {
            text: qsTr("Restart")
            visible: sddm.canReboot
            font.family: root.fontFamily
            onClicked: sddm.reboot()
        }
        Button {
            text: qsTr("Shut down")
            visible: sddm.canPowerOff
            font.family: root.fontFamily
            onClicked: sddm.powerOff()
        }
    }

    function login() {
        message.text = ""
        sddm.login(userField.text, passwordField.text, sessionBox.currentIndex)
    }

    Connections {
        target: sddm
        function onLoginFailed() {
            message.text = qsTr("That password didn't work. Check the user name and try again.")
            passwordField.text = ""
            passwordField.forceActiveFocus()
        }
        function onLoginSucceeded() {
            message.text = ""
        }
    }
}
