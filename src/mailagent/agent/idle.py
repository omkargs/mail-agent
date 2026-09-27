"""IMAP IDLE — push-based mail detection for Gmail.

Polling every 5 minutes costs an API round trip and still leaves up to 5
minutes of latency. IDLE parks a connection and the server pushes the moment
mail arrives: zero cost while waiting, sub-second detection.

Runs in its own thread. If IDLE drops (which Gmail does roughly every 29 days,
and networks do constantly), it reconnects with backoff. Detection degrades to
the poll interval, never below it.
"""
from __future__ import annotations

import imaplib
import logging
import socket
import ssl
import threading
import time
from typing import Callable

from ..config import GoogleConfig

log = logging.getLogger("mailagent.idle")

IDLE_REFRESH_SEC = 2400    # Gmail drops IDLE ~every 29 days; refresh well before
IDLE_POLL_FALLBACK = 300   # if IDLE cannot connect, fall back to this cadence
IDLE_ACK_SEC = 10.0        # how long to wait for the server's "+ idling"


def _idle_ack(im: imaplib.IMAP4_SSL) -> float:
    """Block until the server acknowledges IDLE, or the timeout expires.

    Returns the seconds spent. A timeout is not fatal — some servers send
    nothing at all — but waiting for the real continuation is strictly better
    than assuming it arrived.

    Catches TimeoutError, not socket.timeout: they were separate classes
    before Python 3.10 and socket.timeout is now an alias, but code that only
    catches the old name still works while code that raises the new one must
    catch both or the connection is torn down on every poll.
    """
    deadline = time.time() + IDLE_ACK_SEC
    while time.time() < deadline:
        try:
            im.socket().settimeout(max(0.5, deadline - time.time()))
            line = im.readline()
        except (TimeoutError, socket.timeout, OSError):
            break
        if not line:
            break
        if b"+" in line[:1] or b"idling" in line.lower():
            return IDLE_ACK_SEC - max(0.0, deadline - time.time())
    return IDLE_ACK_SEC


class IdleListener(threading.Thread):
    """Watches INBOX over IMAP IDLE and calls `on_new` when mail arrives."""

    def __init__(self, cfg: GoogleConfig, on_new: Callable[[], None], password: str):
        super().__init__(name="imap-idle", daemon=True)
        self.cfg = cfg
        self.on_new = on_new
        self.password = password
        self._stop = threading.Event()
        self.connected = False
        self.last_event = 0.0

    def stop(self) -> None:
        self._stop.set()

    def _connect(self) -> imaplib.IMAP4_SSL | None:
        try:
            ctx = ssl.create_default_context()
            im = imaplib.IMAP4_SSL(self.cfg.imap_host, self.cfg.imap_port, ssl_context=ctx)
            im.login(self.cfg.account, self.password)
            im.select("INBOX")
            return im
        except Exception as e:
            log.warning("idle connect failed: %s: %s", type(e).__name__, e)
            return None

    def run(self) -> None:
        """Reconnect forever. Detection degrades to polling, never stops."""
        backoff = 30.0
        while not self._stop.is_set():
            im = self._connect()
            if im is None:
                if self._stop.wait(IDLE_POLL_FALLBACK):
                    return
                continue

            self.connected = True
            backoff = 30.0
            try:
                tag = im._new_tag()
                im.send(f"{tag} IDLE\r\n".encode())

                # RFC 3501: the server answers "+ idling" and only then does
                # the idle period begin. A blind sleep raced the server — on a
                # slow ack the EXISTS for the first new message landed before
                # we were listening, and that mail went unseen until the
                # refresh cycle. Read the continuation instead of guessing.
                self._stop.wait(_idle_ack(im))

                deadline = time.time() + IDLE_REFRESH_SEC
                while not self._stop.is_set() and time.time() < deadline:
                    try:
                        # Read timeout spans toward the refresh boundary rather
                        # than one second. A 1s timeout fires repeatedly and
                        # leaves the socket unusable ("cannot read from timed
                        # out object") within seconds, so the connection is
                        # torn down and rebuilt in a loop — push that costs a
                        # reconnect every few seconds. IDLE only speaks when
                        # mail arrives, so there is nothing to poll for.
                        im.socket().settimeout(min(300.0, max(1.0, deadline - time.time())))
                        line = im.readline()
                        if not line:
                            continue
                        if b"EXISTS" in line or b"RECENT" in line:
                            log.info("new mail via IDLE: %s",
                                     line.decode(errors="ignore").strip()[:80])
                            self.last_event = time.time()
                            try:
                                self.on_new()
                            except Exception as e:
                                log.error("on_new failed: %s", type(e).__name__)
                    except (TimeoutError, socket.timeout):
                        # Expected — the server is idle and said nothing. Set a
                        # fresh timeout and go back to waiting on the push.
                        continue
                    except OSError as e:
                        # A socket whose timeout already fired cannot be read
                        # again. That connection is spent; reconnect cleanly.
                        log.info("idle socket spent (%s); reconnecting", str(e)[:60])
                        break
                    except (imaplib.IMAP4.abort, ssl.SSLError):
                        raise

                # End the IDLE cleanly. Closing mid-IDLE leaves an unread
                # tagged response on the socket, and the next refresh starts
                # reading a desynchronised stream.
                try:
                    im.send(f"DONE\r\n".encode())
                except Exception:
                    pass
            except Exception as e:
                log.warning("idle dropped (%s: %s); reconnecting", type(e).__name__, e, exc_info=True)
            finally:
                self.connected = False
                try:
                    im.close()
                    im.logout()
                except Exception:
                    pass
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 300)


def latest_uid(im: imaplib.IMAP4_SSL) -> int:
    """Highest message UID in INBOX, for the polling fallback."""
    _, data = im.search(None, "ALL")
    if not data or not data[0]:
        return 0
    return int(data[0].split()[-1])
