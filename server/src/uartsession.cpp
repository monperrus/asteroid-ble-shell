/*
 * BLE UART over the Nordic UART Service — session controller implementation.
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

#include "uartsession.h"

#include <QDBusConnection>
#include <QElapsedTimer>
#include <QDBusInterface>
#include <QDBusPendingCall>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>
#include <QDebug>

#include "common.h"

UartSession::UartSession(QObject *parent) : QObject(parent)
{
    m_armTimer.setSingleShot(true);
    m_idleTimer.setSingleShot(true);
    connect(&m_armTimer, &QTimer::timeout, this, &UartSession::onArmTimeout);
    connect(&m_idleTimer, &QTimer::timeout, this, &UartSession::onIdleTimeout);
}

namespace {
QElapsedTimer &monotonicClock()
{
    static QElapsedTimer clock;
    if (!clock.isValid())
        clock.start();
    return clock;
}
}

int UartSession::secondsRemaining() const
{
    if (m_state == Disabled)
        return 0;
    const qint64 left = m_deadlineMs - monotonicClock().elapsed();
    // Do not report zero while a positive fraction of the arm window remains.
    return left <= 0 ? 0 : int((left + 999) / 1000);
}

bool UartSession::arm(int seconds)
{
    // Upper bound so a fat-fingered argument can't hold the door open for a day.
    if (seconds <= 0 || seconds > 3600) {
        qWarning() << "BLE UART arm rejected:" << seconds << "seconds";
        return false;
    }

    m_deadlineMs = monotonicClock().elapsed() + qint64(seconds) * 1000;
    m_armTimer.start(seconds * 1000);

    if (m_state == Disabled)
        setState(Armed);

    qInfo() << "BLE UART armed for" << seconds << "seconds";
    return true;
}

void UartSession::disarm()
{
    if (m_state == Disabled)
        return;
    qInfo() << "BLE UART disarmed";
    setState(Disabled);
}

void UartSession::endPeer()
{
    if (m_state != Active)
        return;
    qInfo() << "BLE UART peer disconnected; keeping arm window open";
    m_idleTimer.stop();
    m_devicePath.clear();
    setState(Armed);
    emit armedPeerChanged(QString());
}

void UartSession::setTxSubscribed(bool subscribed)
{
    m_txSubscribed = subscribed;
    if (!subscribed && m_state == Active) {
        qInfo() << "BLE UART: TX notifications stopped, ending peer session";
        endPeer();
    }
}

bool UartSession::authorizePeer(const QString &devicePath)
{
    // An already active session is pinned to one device.
    if (m_state == Active)
        return m_devicePath == devicePath;

    if (m_state != Armed || !m_txSubscribed || devicePath.isEmpty())
        return false;

    // The peer must be paired and trusted per BlueZ.
    QDBusInterface device(BLUEZ_SERVICE_NAME, devicePath, DEVICE_MANAGER_IFACE,
                          QDBusConnection::systemBus());
    if (!device.isValid())
        return false;
    QVariant paired = device.property("Paired");
    QVariant trusted = device.property("Trusted");
    if (!paired.toBool() || !trusted.toBool()) {
        qWarning() << "BLE UART: rejecting peer" << devicePath
                   << "paired=" << paired.toBool() << "trusted=" << trusted.toBool();
        return false;
    }
    return true;
}

void UartSession::onPtyOutput(const QByteArray &)
{
    // The explicit arm deadline is the only lease boundary. In particular,
    // a quiet terminal remains available for the full owner-selected window.
}

void UartSession::onArmTimeout()
{
    qInfo() << "BLE UART arm window expired";
    setState(Disabled);
}

void UartSession::onIdleTimeout()
{
    // Retained as a defensive slot for existing signal wiring; no idle timer
    // is armed because the explicit arm deadline governs the lease.
}

void UartSession::setState(State state)
{
    if (state == m_state)
        return;

    State previous = m_state;
    m_state = state;

    if (state == Disabled) {
        m_armTimer.stop();
        m_idleTimer.stop();
        m_devicePath.clear();
        if (previous == Active)
            emit armedPeerChanged(QString());
    }

    static const char *names[] = { "disabled", "armed", "active" };
    emit stateChanged(QString::fromLatin1(names[state]), secondsRemaining());
}

void UartSession::activatePeer(const QString &devicePath)
{
    if (m_state != Armed || !m_txSubscribed || devicePath.isEmpty())
        return;
    m_devicePath = devicePath;
    setState(Active);
    emit armedPeerChanged(devicePath);
    qInfo() << "BLE UART session started for" << devicePath;
}
