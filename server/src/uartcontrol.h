/*
 * BLE UART over the Nordic UART Service — session-bus control interface.
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

#ifndef UARTCONTROL_H
#define UARTCONTROL_H

#include <QObject>

#include "common.h"
#include "uartsession.h"

/*
 * org.asteroidos.BleUart1 on the session bus, so the on-watch app and
 * gdbus/busctl can arm and revoke the BLE shell. The NUS GATT objects
 * themselves live on the system bus and are registered unconditionally:
 * the arm state gates sessions, not GATT registration.
 */
class UartControl : public QObject
{
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", UART_CONTROL_IFACE)
    Q_PROPERTY(QString State READ getState NOTIFY stateChanged)
    Q_PROPERTY(int SecondsRemaining READ getSecondsRemaining NOTIFY stateChanged)

public:
    explicit UartControl(UartSession *session, QObject *parent = nullptr);

    QString getState() const;
    int getSecondsRemaining() const;

public slots:
    void Arm(int seconds);
    void Disarm();

signals:
    void stateChanged();

private:
    UartSession *m_session;
};

#endif // UARTCONTROL_H
