/*
 * BLE UART over the Nordic UART Service — session controller.
 *
 * Copyright (C) 2026 - Martin Monperrus <martin.monperrus@gnieh.org>
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program. If not, see <http://www.gnu.org/licenses/>.
 */

#ifndef UARTSESSION_H
#define UARTSESSION_H

#include <QObject>
#include <QTimer>
#include <QSocketNotifier>

/*
 * State machine (plan-uart.md):
 *
 *   disabled --Arm--> armed --first trusted RX--> active
 *       ^                |                            |
 *       +---Disarm / expiry----------------------------+
 *
 * A link loss ends the PTY and returns Active to Armed; the original arm
 * deadline remains in force so the trusted peer can reconnect.
 *
 * disabled is the only state at boot; nothing is persisted.
 */
class UartSession : public QObject
{
    Q_OBJECT
public:
    enum State { Disabled, Armed, Active };
    explicit UartSession(QObject *parent = nullptr);

    State state() const { return m_state; }
    int secondsRemaining() const;

    // Called by the on-watch D-Bus control interface.
    bool arm(int seconds);
    void disarm();
    void endPeer();

    // Called by the NUS characteristics.
    bool isArmed() const { return m_state != Disabled; }
    bool txSubscribed() const { return m_txSubscribed; }
    void setTxSubscribed(bool subscribed);

    // Gate for RX bytes: armed (or active with the same peer), TX subscribed,
    // peer paired+trusted. Returns true when the caller may accept the bytes.
    bool authorizePeer(const QString &devicePath);
    bool isActivePeer(const QString &devicePath) const { return m_state == Active && m_devicePath == devicePath; }

    // Only UartService calls this, after the PTY has actually been created.
    // Keeping the state Armed on a spawn failure means no input is ever sent
    // to a nonexistent or half-created shell.
    void activatePeer(const QString &devicePath);

signals:
    void stateChanged(const QString &state, int secondsRemaining);
    void armedPeerChanged(const QString &devicePath);

public slots:
    // PTY output -> TX characteristic
    void onPtyOutput(const QByteArray &bytes);

private slots:
    void onArmTimeout();
    void onIdleTimeout();

private:
    void setState(State state);
    State m_state = Disabled;
    bool m_txSubscribed = false;
    QString m_devicePath;
    QTimer m_armTimer;
    QTimer m_idleTimer;
    qint64 m_deadlineMs = 0;   // absolute arm deadline, monotonic
};

#endif // UARTSESSION_H
