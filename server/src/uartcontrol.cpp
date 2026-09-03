/*
 * BLE UART over the Nordic UART Service — session-bus control implementation.
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

#include "uartcontrol.h"

#include <QDBusConnection>
#include <QDebug>

#include "common.h"

UartControl::UartControl(UartSession *session, QObject *parent)
    : QObject(parent), m_session(session)
{
    QDBusConnection bus = QDBusConnection::sessionBus();
    if (!bus.registerObject(UART_CONTROL_PATH, this,
                            QDBusConnection::ExportAllSlots | QDBusConnection::ExportAllProperties))
        qWarning() << "Cannot register object" << UART_CONTROL_PATH << "on the session bus";
    if (!bus.registerService(UART_CONTROL_SERVICE_NAME))
        qWarning() << "Cannot register" << UART_CONTROL_SERVICE_NAME << "on the session bus";

    connect(m_session, &UartSession::stateChanged, this, &UartControl::stateChanged);
}

QString UartControl::getState() const
{
    switch (m_session->state()) {
    case UartSession::Armed:  return QStringLiteral("armed");
    case UartSession::Active: return QStringLiteral("active");
    default:                  return QStringLiteral("disabled");
    }
}

int UartControl::getSecondsRemaining() const
{
    return m_session->secondsRemaining();
}

void UartControl::Arm(int seconds)
{
    m_session->arm(seconds);
}

void UartControl::Disarm()
{
    m_session->disarm();
}
