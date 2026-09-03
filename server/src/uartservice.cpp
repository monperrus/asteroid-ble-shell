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

#include "uartservice.h"

#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <pwd.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

#include <QDebug>
#include <QDBusObjectPath>
#include <QSocketNotifier>

#include "characteristic.h"
#include "common.h"

/* Conservative ATT payload: 23-byte MTU - 3 header bytes. Do not raise this
 * until it has been verified against BlueZ and a real client. */
static const int TX_CHUNK_SIZE = 20;
/* Bound on TX queue: at high water we stop reading the PTY, at low water we
 * resume, instead of growing without limit or dropping terminal output. */
static const int TX_HIGH_WATER = 64 * 1024;
static const int TX_LOW_WATER  = 16 * 1024;
/* One notification chunk per tick; fast enough for an interactive shell,
 * slow enough not to flood BlueZ. */
static const int TX_PACE_MS = 20;

UartRxChrc::UartRxChrc(QDBusConnection bus, int index, UartService *service)
    : Characteristic(bus, index, NUS_RX_UUID, {"encrypt-authenticated-write", "write-without-response"}, service), m_service(service)
{
}

void UartRxChrc::WriteValue(QByteArray value, QVariantMap options)
{
    // NUS is a stream. Prepared/offset writes have record semantics that do
    // not map safely onto a terminal stream, so reject them rather than
    // guessing how to splice fragments. Normal write requests and commands
    // both arrive here with offset 0.
    if (options.value("offset", 0).toInt() != 0 ||
        options.value("prepare-authorize", false).toBool()) {
        qWarning() << "BLE UART: rejecting offset/prepared write";
        return;
    }

    // BlueZ specifies this option as an object path (`o`), which Qt stores as
    // QDBusObjectPath rather than a QString.  Do not silently turn it into an
    // empty peer identity: authorization is deliberately pinned to this path.
    const QVariant deviceOption = options.value("device");
    QString device = qvariant_cast<QDBusObjectPath>(deviceOption).path();
    if (device.isEmpty())
        device = deviceOption.toString();
    m_service->handleRx(value, device);
}

UartTxChrc::UartTxChrc(QDBusConnection bus, int index, UartService *service)
    : Characteristic(bus, index, NUS_TX_UUID, {"encrypt-authenticated-notify"}, service), m_service(service)
{
}

void UartTxChrc::StartNotify()
{
    Characteristic::StartNotify();
    m_service->session()->setTxSubscribed(true);
}

void UartTxChrc::StopNotify()
{
    Characteristic::StopNotify();
    m_service->session()->setTxSubscribed(false);
}

UartService::UartService(int index, QDBusConnection bus, QObject *parent)
    : Service(bus, index, NUS_UUID, parent)
{
    m_rx = new UartRxChrc(bus, 0, this);
    m_tx = new UartTxChrc(bus, 1, this);
    addCharacteristic(m_rx);
    addCharacteristic(m_tx);

    connect(&m_session, &UartSession::stateChanged, this, [this](const QString &, int) {
        if (m_session.state() == UartSession::Disabled)
            stopShell("session disabled");
    });
    connect(&m_session, &UartSession::armedPeerChanged, this, [this](const QString &peer) {
        if (peer.isEmpty())
            stopShell("peer disconnected");
    });
    m_reapTimer.setSingleShot(true);
    connect(&m_reapTimer, &QTimer::timeout, this, &UartService::reapShell);
}

UartService::~UartService()
{
    stopShell("service destroyed");
}

bool UartService::handleRx(const QByteArray &bytes, const QString &devicePath)
{
    if (m_session.state() == UartSession::Disabled) {
        // Rate-limited journal trace, never the payload itself.
        qWarning() << "BLE UART: input while disabled, dropped" << bytes.size() << "bytes";
        m_rxDrops++;
        return false;
    }

    if (!m_session.authorizePeer(devicePath)) {
        m_rxDrops += bytes.size();
        return false;
    }

    if (m_session.state() == UartSession::Armed)
        startShell(devicePath);

    if (m_childPid <= 0 || m_masterFd < 0)
        return false;

    if (m_session.state() == UartSession::Armed)
        m_session.activatePeer(devicePath);
    if (!m_session.isActivePeer(devicePath))
        return false;

    const char *data = bytes.constData();
    ssize_t total = 0;
    while (total < bytes.size()) {
        ssize_t n = write(m_masterFd, data + total, bytes.size() - total);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                qWarning() << "BLE UART: PTY input busy, dropping" << (bytes.size() - total) << "bytes";
                m_rxDrops += bytes.size() - total;
                return false;
            }
            qWarning() << "BLE UART: PTY write failed:" << strerror(errno);
            return false;
        }
        total += n;
    }
    // Input is activity too. The idle timeout is a safety limit, not a
    // requirement that a command happens to produce output.
    m_session.onPtyOutput(bytes);
    return true;
}

void UartService::startShell(const QString &devicePath)
{
    if (m_childPid > 0)
        return;

    // Fail closed: this daemon is a user service and the shell must never
    // run as root.
    uid_t uid = getuid();
    if (uid == 0) {
        qWarning() << "BLE UART: refusing to start a shell as root";
        m_session.disarm();
        return;
    }
    struct passwd *pw = getpwuid(uid);
    if (!pw || strcmp(pw->pw_name, "ceres") != 0) {
        qWarning() << "BLE UART: refusing shell for unexpected daemon user" << uid;
        m_session.disarm();
        return;
    }

    int master = posix_openpt(O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (master < 0) {
        qWarning() << "BLE UART: posix_openpt failed:" << strerror(errno);
        return;
    }
    if (grantpt(master) || unlockpt(master)) {
        qWarning() << "BLE UART: grantpt/unlockpt failed:" << strerror(errno);
        close(master);
        return;
    }
    char slaveName[64];
    if (ptsname_r(master, slaveName, sizeof(slaveName)) != 0) {
        qWarning() << "BLE UART: ptsname_r failed:" << strerror(errno);
        close(master);
        return;
    }

    pid_t pid = fork();
    if (pid < 0) {
        qWarning() << "BLE UART: fork failed:" << strerror(errno);
        close(master);
        return;
    }
    if (pid == 0) {
        // child: new session with the slave as controlling terminal
        setsid();
        int slave = open(slaveName, O_RDWR);
        if (slave < 0)
            _exit(127);
        ioctl(slave, TIOCSCTTY, 0);
        dup2(slave, 0);
        dup2(slave, 1);
        dup2(slave, 2);
        if (slave > 2)
            close(slave);
        close(master);

        struct winsize ws;
        memset(&ws, 0, sizeof(ws));
        ws.ws_col = 80;
        ws.ws_row = 24;
        ioctl(0, TIOCSWINSZ, &ws);

        // Small explicit environment; inherit nothing else from the daemon.
        clearenv();
        setenv("HOME", pw->pw_dir, 1);
        setenv("USER", pw->pw_name, 1);
        setenv("LOGNAME", pw->pw_name, 1);
        setenv("SHELL", pw->pw_shell ? pw->pw_shell : "/bin/sh", 1);
        setenv("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin", 1);
        setenv("TERM", "xterm-256color", 1);

        execl(pw->pw_shell ? pw->pw_shell : "/bin/sh",
              pw->pw_shell ? pw->pw_shell : "/bin/sh", "-i", (char *)nullptr);
        _exit(127);
    }

    m_masterFd = master;
    m_childPid = pid;

    m_ptyNotifier = new QSocketNotifier(m_masterFd, QSocketNotifier::Read, this);
    connect(m_ptyNotifier, &QSocketNotifier::activated, this, [this] { onPtyReadable(); });

    qInfo() << "BLE UART: shell started (pid" << pid << ") for" << devicePath;
}

void UartService::stopShell(const char *reason)
{
    delete m_ptyNotifier;
    m_ptyNotifier = nullptr;

    m_txQueue.clear();
    m_txQueuedBytes = 0;
    m_notifierPaused = false;

    if (m_childPid <= 0)
        return;

    qInfo() << "BLE UART: stopping shell (" << reason << ")";

    // Never sleep or block in btsyncd's event loop: it owns all of the
    // watch's GATT services. Reap/escalate asynchronously instead.
    kill(-m_childPid, SIGHUP);
    m_reapStage = 0;
    m_reapTimer.start(1000);
}

void UartService::reapShell()
{
    if (m_childPid <= 0)
        return;

    int status;
    const pid_t result = waitpid(m_childPid, &status, WNOHANG);
    if (result == 0) {
        if (m_reapStage == 0) {
            qWarning() << "BLE UART: shell ignored SIGHUP; sending SIGTERM";
            kill(-m_childPid, SIGTERM);
            m_reapStage = 1;
            m_reapTimer.start(500);
            return;
        }
        if (m_reapStage == 1) {
            qWarning() << "BLE UART: shell ignored SIGTERM; sending SIGKILL";
            kill(-m_childPid, SIGKILL);
            m_reapStage = 2;
        }
        m_reapTimer.start(100);
        return;
    }

    if (result < 0 && errno != ECHILD && errno != EINTR) {
        qWarning() << "BLE UART: waitpid failed:" << strerror(errno);
        m_reapTimer.start(100);
        return;
    }

    qInfo() << "BLE UART: shell reaped (pid" << m_childPid << ")";

    if (m_masterFd >= 0) {
        close(m_masterFd);
        m_masterFd = -1;
    }
    m_childPid = -1;
}

void UartService::onPtyReadable()
{
    if (m_masterFd < 0)
        return;

    char buf[4096];
    while (true) {
        ssize_t n = read(m_masterFd, buf, sizeof(buf));
        if (n < 0) {
            if (errno == EINTR)
                continue;
            if (errno == EAGAIN)
                break;
            qWarning() << "BLE UART: PTY read failed:" << strerror(errno);
            break;
        }
        if (n == 0) {
            // EOF ends only this PTY. Preserve the explicit arm deadline so
            // a trusted peer can open a fresh session after reconnecting.
            m_session.endPeer();
            return;
        }
        enqueueOutput(QByteArray(buf, int(n)));
        if (m_notifierPaused)
            break;
    }
}

void UartService::enqueueOutput(const QByteArray &bytes)
{
    if (bytes.isEmpty())
        return;

    m_session.onPtyOutput(bytes);

    // Backpressure: stop reading at high water, resume at low water.
    if (m_txQueuedBytes >= TX_HIGH_WATER && !m_notifierPaused) {
        m_notifierPaused = true;
        m_ptyNotifier->setEnabled(false);
        qWarning() << "BLE UART: TX queue at high water," << m_txQueuedBytes << "bytes, pausing PTY reads";
    }

    for (int i = 0; i < bytes.size(); i += TX_CHUNK_SIZE) {
        m_txQueue.enqueue(bytes.mid(i, TX_CHUNK_SIZE));
    }
    m_txQueuedBytes += bytes.size();
    if (m_txQueuedBytes > m_queueHighWater)
        m_queueHighWater = m_txQueuedBytes;

    if (!m_txBytes)
        m_tx->sendChunk(); // prime the pump
    QTimer::singleShot(TX_PACE_MS, m_tx, [this] { m_tx->sendChunk(); });
}

void UartTxChrc::sendChunk()
{
    if (!getNotifying())
        return;

    UartService *s = m_service;
    if (s->m_txQueue.isEmpty()) {
        if (s->m_notifierPaused && s->m_txQueuedBytes <= TX_LOW_WATER) {
            s->m_notifierPaused = false;
            s->m_ptyNotifier->setEnabled(true);
        }
        return;
    }

    QByteArray chunk = s->m_txQueue.dequeue();
    s->m_txQueuedBytes -= chunk.size();
    s->m_txBytes += chunk.size();

    updateValue(chunk);

    if (!s->m_txQueue.isEmpty())
        QTimer::singleShot(TX_PACE_MS, this, [this] { sendChunk(); });
    else if (s->m_notifierPaused && s->m_txQueuedBytes <= TX_LOW_WATER) {
        s->m_notifierPaused = false;
        s->m_ptyNotifier->setEnabled(true);
    }
}
