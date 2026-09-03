/*
 * BLE UART over the Nordic UART Service — GATT service and PTY shell.
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

#ifndef UARTSERVICE_H
#define UARTSERVICE_H

#include <QObject>
#include <QQueue>
#include <QTimer>
#include <QSocketNotifier>

#include "service.h"
#include "uartsession.h"

class UartService;

/*
 * NUS RX: central -> watch, write / write-without-response.
 * Accepts an unframed byte stream: every WriteValue chunk is appended to
 * the PTY input. No line, record or UTF-8 assumption is made.
 */
class UartRxChrc : public Characteristic
{
    Q_OBJECT
public:
    UartRxChrc(QDBusConnection bus, int index, UartService *service);

public slots:
    void WriteValue(QByteArray value, QVariantMap options) override;

private:
    UartService *m_service;
};

/*
 * NUS TX: watch -> central, notify.
 * Output from the shell is queued and notified in bounded chunks paced by a
 * timer, so neither BlueZ nor the D-Bus daemon is flooded.
 */
class UartTxChrc : public Characteristic
{
    Q_OBJECT
public:
    UartTxChrc(QDBusConnection bus, int index, UartService *service);

public slots:
    void StartNotify() override;
    void StopNotify() override;
    void sendChunk();       // one paced chunk per tick

private:
    UartService *m_service;
};

/*
 * One PTY running /bin/sh -i as the daemon user, bridged to NUS.
 *
 * Security posture (plan-uart.md): the shell only exists while the session
 * is Active, which requires the feature to be armed from the watch, the peer
 * to be paired and trusted, TX notifications to be subscribed (a usable
 * return channel) and no other active terminal. Every teardown path kills
 * the whole process group and reaps the child.
 */
class UartService : public Service
{
    Q_OBJECT
public:
    explicit UartService(int index, QDBusConnection bus, QObject *parent = 0);
    ~UartService() override;

    UartSession *session() { return &m_session; }
    UartTxChrc *txCharacteristic() { return m_tx; }

    // RX side: returns true when the bytes were consumed.
    bool handleRx(const QByteArray &bytes, const QString &devicePath);

    // Called by the session on state transitions.
    void startShell(const QString &devicePath);
    void stopShell(const char *reason);

    // PTY master readable
    void onPtyReadable();

    qint64 rxDropCount() const { return m_rxDrops; }
    qint64 txByteCount() const { return m_txBytes; }
    int txQueueHighWater() const { return m_queueHighWater; }

private:
    void enqueueOutput(const QByteArray &bytes);
    void teardownPty();
    void reapShell();

    // UartTxChrc::sendChunk() drives the paced queue directly.
    friend class UartTxChrc;

    UartSession m_session;
    UartRxChrc *m_rx;
    UartTxChrc *m_tx;

    int m_masterFd = -1;
    pid_t m_childPid = -1;
    QSocketNotifier *m_ptyNotifier = nullptr;
    QTimer m_reapTimer;
    int m_reapStage = 0;

    QQueue<QByteArray> m_txQueue;
    int m_txQueuedBytes = 0;
    int m_queueHighWater = 0;
    qint64 m_txBytes = 0;
    qint64 m_rxDrops = 0;
    bool m_notifierPaused = false;
};

#endif // UARTSERVICE_H
