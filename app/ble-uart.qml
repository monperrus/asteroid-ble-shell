/* BLE SHELL control — deliberately explicit local arming for the debug shell. */
import QtQuick
import org.asteroid.controls
import Nemo.DBus 2.0
import Nemo.Ngf

Application {
    id: app
    centerColor: uartState === "active" ? "#5B1717" : (uartState === "armed" ? "#173F5C" : "#26323A")
    outerColor: uartState === "active" ? "#220909" : (uartState === "armed" ? "#071D2B" : "#11181D")
    Behavior on centerColor { ColorAnimation { duration: 180 } }
    Behavior on outerColor { ColorAnimation { duration: 180 } }

    // libnemodbus on this image invokes methods correctly but does not mirror
    // these capitalized D-Bus properties into QML. Track the state after the
    // daemon acknowledges Arm/Disarm instead of leaving a working button
    // visually inert.
    property string uiState: "disabled"
    readonly property string uartState: serviceAvailable ? uiState : "unavailable"
    property int secondsRemaining: 0
    property bool wasActive: false
    property string actionError: ""
    readonly property bool serviceAvailable: uart.status === DBusInterface.Available

    function updateState() {
        if (!serviceAvailable) {
            uiState = "unavailable"
            secondsRemaining = 0
        } else if (uiState === "unavailable") {
            uiState = "disabled"
        }
        if (uartState === "active" && !wasActive)
            activeFeedback.play()
        wasActive = uartState === "active"
    }

    function arm() {
        // 15 minutes is intentionally fixed in the on-watch UI.
        uart.typedCall("Arm", { "type": "i", "value": 900 }, function() {
            app.uiState = "armed"
            app.secondsRemaining = 900
            app.actionError = ""
            refreshTimer.restart()
        }, function(error, message) {
            app.actionError = message || qsTr("Unable to enable BLE shell")
        })
    }

    function disarm() {
        uart.call("Disarm", undefined, function() {
            app.uiState = "disabled"
            app.secondsRemaining = 0
            app.actionError = ""
            refreshTimer.restart()
        }, function(error, message) {
            app.actionError = message || qsTr("Unable to disable BLE shell")
        })
    }

    DBusInterface {
        id: uart
        service: "org.asteroidos.btsyncd"
        path: "/org/asteroidos/btsyncd/uart"
        iface: "org.asteroidos.BleUart1"
        bus: DBus.SessionBus
        watchServiceStatus: true
        propertiesEnabled: true
        onStatusChanged: app.updateState()
    }

    Timer {
        id: refreshTimer
        interval: 1000
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: {
            if (app.uartState === "armed" || app.uartState === "active")
                app.secondsRemaining = Math.max(0, app.secondsRemaining - 1)
            else
                app.secondsRemaining = 0
            if (app.secondsRemaining === 0 && app.uiState === "armed")
                app.uiState = "disabled"
        }
    }

    onUartStateChanged: updateState()

    NonGraphicalFeedback {
        id: activeFeedback
        event: "notification"
    }

    Item {
        id: dial
        width: Dims.w(72)
        height: width
        anchors { top: parent.top; topMargin: Dims.h(10); horizontalCenter: parent.horizontalCenter }

        SegmentedArc {
            anchors.fill: parent
            segmentAmount: 40
            gap: 2
            start: -90
            endFromStart: 360
            inputValue: uartState === "armed" || uartState === "active" ? secondsRemaining / 9 : 0
            fgColor: uartState === "active" ? "#FF5A52" : "#48B7F0"
            bgColor: "#0A0A0A"
            arcStrokeWidth: .014
        }

        Column {
            anchors.centerIn: parent
            width: parent.width * .82
            spacing: Dims.h(1)

            Label {
                anchors.horizontalCenter: parent.horizontalCenter
                horizontalAlignment: Text.AlignHCenter
                width: parent.width
                font.pixelSize: Dims.l(7)
                text: uartState === "active" ? qsTr("SHELL ACTIVE")
                      : uartState === "armed" ? qsTr("SHELL ARMED")
                      : uartState === "disabled" ? qsTr("SHELL OFF")
                      : qsTr("SERVICE OFFLINE")
                color: uartState === "active" ? "#FF8A84" : "white"
            }
            Label {
                anchors.horizontalCenter: parent.horizontalCenter
                horizontalAlignment: Text.AlignHCenter
                width: parent.width
                font.pixelSize: uartState === "armed" || uartState === "active" ? Dims.l(17) : Dims.l(7)
                font.styleName: "Medium"
                text: uartState === "armed" || uartState === "active"
                      ? Math.ceil(secondsRemaining / 60) + qsTr(" min")
                      : serviceAvailable ? qsTr("TAP TO ENABLE") : qsTr("UNAVAILABLE")
                color: serviceAvailable ? "#5EC8FF" : "#999999"
            }
            Label {
                anchors.horizontalCenter: parent.horizontalCenter
                horizontalAlignment: Text.AlignHCenter
                width: parent.width
                wrapMode: Text.Wrap
                font.pixelSize: Dims.l(4.5)
                color: "#C5CDD2"
                text: uartState === "active"
                      ? qsTr("A trusted Bluetooth terminal is connected")
                      : actionError.length > 0 ? actionError
                      : qsTr("Enables a privileged debug shell for 15 minutes")
            }
        }

        // The primary action must be usable without finding the small bottom
        // icon on a round display. Tapping the status dial arms or revokes.
        MouseArea {
            anchors.fill: parent
            enabled: app.serviceAvailable
            onClicked: {
                if (app.uartState === "armed" || app.uartState === "active")
                    app.disarm()
                else
                    app.arm()
            }
        }
    }

    Row {
        anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: Dims.iconButtonMargin }
        spacing: Dims.w(18)

        IconButton {
            iconName: "ios-power"
            iconColor: "#FF6E61"
            visible: uartState === "armed" || uartState === "active"
            onClicked: app.disarm()
        }
        IconButton {
            iconName: "ios-unlock-outline"
            iconColor: "#5EC8FF"
            visible: uartState !== "armed" && uartState !== "active"
            enabled: app.serviceAvailable
            onClicked: app.arm()
        }
    }
}
