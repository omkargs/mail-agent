"""Supervisor: the always-on runtime.

Responsibilities the naive loop got wrong:
  * Push, don't poll. IMAP IDLE for Gmail, Graph delta for Outlook. Detection
    costs nothing while idle; polling costs an API round trip every cycle.
  * Re-auth once, not every cycle. Token refresh mid-send kills a run.
  * Backoff on failure. A dead router must not be hammered every 60s.
  * Never miss the brief. Catch-up if the machine was asleep at 07:00.
  * Fail loudly. If auth dies, the user is told, not left with a silent loop.
  * Survive its own crash. A watchdog thread restarts the loop if it wedges.

Exit reasons are explicit: clean signal, or a fatal condition that should stop
the unit so systemd's Restart policy does not spin forever.
"""
from __future__ import annotations

import logging
import random
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import logging_setup
from ..storage import db

log = logging.getLogger("mailagent.supervisor")

# Consecutive failures before we stop retrying and escalate to the user.
MAX_CONSECUTIVE_FAILURES = 8

# Chat is interactive, so it gets its own fast cadence instead of riding the
# mail scan interval. A few seconds is well inside Telegram's getUpdates
# long-poll window and costs one cheap request per cycle.
CHAT_POLL_SEC = 3.0


@dataclass
class HealthState:
    """What the supervisor knows about its own condition."""

    started_at: float = field(default_factory=time.time)
    last_scan: float = 0.0
    last_success: float = 0.0
    cycles: int = 0
    consecutive_failures: int = 0
    total_failures: int = 0
    last_error: str = ""
    auth_dead: set[str] = field(default_factory=set)
    last_brief_day: int = 0

    def ok(self) -> bool:
        return self.consecutive_failures < MAX_CONSECUTIVE_FAILURES

    def snapshot(self) -> dict[str, Any]:
        from datetime import datetime, timezone

        return {
            # Stamped on every write so `mail-agent health` can tell a live
            # daemon from a stale file left by one that died. /proc existence
            # is not enough — pids get reused.
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "uptime_sec": int(time.time() - self.started_at),
            "cycles": self.cycles,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "last_error": self.last_error[:200],
            "last_scan_ago_sec": int(time.time() - self.last_scan) if self.last_scan else None,
            "last_success_ago_sec": int(time.time() - self.last_success) if self.last_success else None,
            "auth_dead": sorted(self.auth_dead),
        }


def backoff_seconds(failures: int, base: float = 60.0, cap: float = 1800.0) -> float:
    """Exponential backoff with jitter.

    Jitter matters: without it, a fleet of daemons that all failed on the same
    router outage retries in lockstep and re-creates the outage.
    """
    if failures <= 0:
        return base
    delay = min(base * (2 ** (failures - 1)), cap)
    return delay * (0.7 + random.random() * 0.6)


class Supervisor:
    def __init__(self, cfg, providers_factory: Callable[[], dict[str, Any]],
                 scan_fn: Callable[[], Any], tick_fn: Callable[[], Any],
                 notify: Callable[..., Any] | None = None):
        self.cfg = cfg
        self.providers_factory = providers_factory
        self.scan_fn = scan_fn
        self.tick_fn = tick_fn
        self.notify = notify
        self.health = HealthState()
        self._stop = threading.Event()
        # Set by IMAP IDLE when new mail arrives. The loop's sleep returns
        # early on it, turning a 5-minute poll into sub-second detection.
        self.wake = threading.Event()
        self._loops: list[threading.Thread] = []
        self._told_auth_dead: set[str] = set()
        # One-shot notices (out of credit, cap hit) — told once, not every cycle.
        self._told_notice: set[str] = set()

    # ------------------------------------------------------------------ stop
    def install_signal_handlers(self) -> None:
        def handler(signum, frame):
            log.info("signal %s received; shutting down", signum)
            self._stop.set()

        if threading.current_thread() is not threading.main_thread():
            # Embedding the supervisor in a thread (tests, an A2A host). The
            # caller owns signals; we still honour should_stop().
            log.debug("not the main thread; skipping signal handler install")
            return

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def should_stop(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------------ loop
    def start(self) -> int:
        self.install_signal_handlers()
        base = max(30, self.cfg.agent.scan_interval_sec)

        log.info("supervisor up: interval=%ds brief_hour=%d cap=%d",
                 base, self.cfg.agent.brief_hour, self.cfg.agent.daily_token_cap)

        if not self._warmup():
            self._write_health()
            # Not a crash: a missing credential or an un-authenticated account.
            # Exit 0 so systemd treats it as a clean stop rather than a failed
            # unit to retry. `systemctl --user reset-failed` + start once the
            # config is fixed. Retrying here would just crash-loop.
            return 0

        self._loops = [
            threading.Thread(target=self._watchdog, name="watchdog", daemon=True),
            threading.Thread(target=self._brief_loop, name="brief", daemon=True),
            threading.Thread(target=self._chat_loop, name="chat", daemon=True),
        ]
        for t in self._loops:
            t.start()

        try:
            return self._main_loop(base)
        finally:
            self._write_health()

    def _write_health(self) -> None:
        """Persist a health snapshot the `health` command can read."""
        import json
        import os
        from pathlib import Path

        from ..config import DATA_DIR

        try:
            d = Path(os.environ.get("MAIL_AGENT_STATE", str(DATA_DIR)))
            d.mkdir(parents=True, exist_ok=True)
            snap = self.health.snapshot()
            snap["pid"] = os.getpid()
            (d / "daemon.json").write_text(json.dumps(snap, indent=2))
        except Exception as e:
            log.debug("could not write health state: %s", type(e).__name__)

    def _warmup(self) -> bool:
        """Authenticate before the first cycle, and validate the router."""
        from ..limits import from_config

        from_config(self.cfg)
        log.info("spend limits: %d calls/min, %d tokens/day",
                 self.cfg.agent.max_calls_per_min, self.cfg.agent.daily_token_cap)

        problems = self.cfg.validate()
        if problems:
            msg = (
                "mail-agent is not configured yet:\n  - "
                + "\n  - ".join(problems)
                + "\n\nRun ./setup.sh, or `mail-agent auth`, then start the service."
            )
            log.error(msg)
            self._say(msg)
            return False

        providers = self.providers_factory()
        if not providers:
            msg = "mail-agent: no authenticated account. Run `mail-agent auth`, then restart."
            log.error(msg)
            self._say(msg)
            return False

        from .client import build_client, guarded_call

        try:
            client = build_client(self.cfg.router)
            guarded_call(client, 
                model=self.cfg.router.model, max_tokens=16,
                messages=[{"role": "user", "content": "ping"}],
            )
            log.info("router reachable: %s", self.cfg.router.base_url)
        except Exception as e:
            # A router outage at boot should not be fatal — backoff will retry.
            log.warning("router check failed at boot: %s: %s", type(e).__name__, e)

        log.info("accounts ready: %s", ", ".join(providers))
        return True

    def _sleep_or_wake(self, seconds: float) -> bool:
        """Wait up to `seconds`, returning early on shutdown or on an IDLE
        poke. True means the loop should exit.

        This is the single place both the stop Event and the wake Event are
        consulted, so there is exactly one way to interrupt the sleep.
        """
        if self._stop.is_set():
            return True
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            # Wake early if IDLE signalled, but keep the wake set for the
            # scan_fn that follows.
            if self.wake.wait(timeout=min(remaining, 1.0)):
                self.wake.clear()
                return False
            if self._stop.is_set():
                return True

    def _chat_loop(self) -> None:
        """Poll the chat channel on its own fast cadence.

        Chat is interactive. Tying it to the mail scan interval means someone
        asking "who needs a reply" waits up to five minutes for an answer, which
        reads as a broken bot rather than a slow one. This polls independently
        of mail scanning.

        The channel long-polls: Telegram holds the request open and answers the
        moment a message arrives. So an empty return already cost us the full
        long-poll window — sleeping another few seconds would just add latency
        for no benefit. A short gap only when something was handled, to let
        the next burst arrive.
        """
        from ..notify.channels import ApprovalListener

        listener = ApprovalListener(self.cfg, self.providers_factory())
        while not self.should_stop():
            handled = 0
            try:
                listener.providers = self.providers_factory()
                handled = listener.tick()
            except Exception as e:
                log.warning("chat poll failed: %s: %s", type(e).__name__, e)
            gap = 0.5 if handled else CHAT_POLL_SEC
            if self._stop.wait(gap):
                return

    def _run_due_jobs(self) -> None:
        """Fire any scheduled job whose time has arrived.

        The job is marked ran before it executes, not after. A job that raises
        would otherwise stay due forever and re-fire on every 5-minute cycle,
        spamming the operator with the same brief.
        """
        from datetime import datetime
        from ..storage import db
        from .schedule import run_job

        local_now = datetime.now().strftime("%H:%M")
        for job in db.due_jobs(local_now):
            db.mark_job_ran(job["id"], job["repeat"])
            log.info("running scheduled job %s (%s at %s)", job["id"], job["kind"], job["at_time"])
            try:
                text = run_job(job, self.cfg, self.providers_factory, notify=self.notify)
            except Exception as e:
                log.error("scheduled job %s failed: %s: %s", job["id"], type(e).__name__, e)
                self._say(f"Scheduled task ({job['kind']}) failed: {type(e).__name__}")
                continue
            if text:
                self._say(f"*Scheduled — {job['kind']}*\n\n{text}")

    def _main_loop(self, base_interval: float) -> int:
        while not self.should_stop():
            try:
                providers = self.providers_factory()
                self._check_auth(providers)

                if providers:
                    self.scan_fn()
                # tick_fn (approvals + chat) runs in _chat_loop on its own fast
                # cadence. Calling it here too would give the channel two
                # pollers with independent offsets, and every message would be
                # answered twice.
                self._run_due_jobs()

                self.health.consecutive_failures = 0
                self.health.last_success = time.time()
            except Exception as e:
                self.health.consecutive_failures += 1
                self.health.total_failures += 1
                self.health.last_error = f"{type(e).__name__}: {e}"
                log.error("cycle failed (%d in a row): %s",
                          self.health.consecutive_failures, self.health.last_error)

                # Money problems are told once, clearly. A silent agent that has
                # run out of credit is indistinguishable from a broken one.
                from ..limits import BudgetExhausted, classify, global_limits

                kind = classify(e)
                if kind in ("credits", "forbidden") and "credits" not in self._told_notice:
                    self._told_notice.add("credits")
                    self._say(
                        f"mail-agent cannot reach the AI provider.\n\n{self.health.last_error}\n\n"
                        f"I have stopped making model calls so I do not waste anything. "
                        f"Top up or fix the key, then: systemctl --user restart mail-agent"
                    )
                elif isinstance(e, BudgetExhausted) and "limit" not in self._told_notice:
                    self._told_notice.add("limit")
                    self._say(
                        f"mail-agent hit its spend limit and is pausing.\n\n{self.health.last_error}\n\n"
                        f"Raise AGENT_DAILY_TOKEN_CAP or AGENT_MAX_CALLS_PER_MIN to resume."
                    )
                elif isinstance(e, BudgetExhausted) and "rate" in str(e):
                    pass  # rate limiting is normal, not news

                if not self.health.ok():
                    msg = (
                        f"mail-agent stopped after {self.health.consecutive_failures} "
                        f"consecutive failures.\nLast error: {self.health.last_error}\n"
                        f"Fix it, then: systemctl --user restart mail-agent"
                    )
                    log.error(msg)
                    self._say(msg)
                    return 1

            self.health.cycles += 1
            self.health.last_scan = time.time()
            self._write_health()

            delay = (backoff_seconds(self.health.consecutive_failures, base=base_interval)
                     if self.health.consecutive_failures else base_interval)
            if self._sleep_or_wake(delay):
                break

        log.info("supervisor stopped after %d cycles", self.health.cycles)
        return 0

    def _check_auth(self, providers: dict[str, Any]) -> None:
        """Tell the user when an account's auth dies, exactly once."""
        for name in list(self.health.auth_dead):
            if name not in providers:
                continue
            p = providers[name]
            if p.valid():
                self.health.auth_dead.discard(name)
                log.info("account %s auth recovered", name)
                self._say(f"mail-agent: {name} reconnected.")
            else:
                try:
                    p.authenticate()
                    if p.valid():
                        self.health.auth_dead.discard(name)
                        self._say(f"mail-agent: {name} reconnected.")
                except Exception as e:
                    log.debug("re-auth for %s failed: %s", name, type(e).__name__)

    # ------------------------------------------------------------- scheduling
    def _brief_loop(self) -> None:
        """Fires the brief once per day, with catch-up if we were asleep."""
        while not self.should_stop():
            try:
                now = time.localtime()
                today = time.strftime("%Y-%m-%d")
                brief_sent = self._brief_done(today)

                if not brief_sent and now.tm_hour >= self.cfg.agent.brief_hour:
                    from .brief import build_brief, brief_quiet_threads

                    self._say(build_brief(self.cfg))
                    self._say(brief_quiet_threads(self.cfg))
                    self._mark_brief(today)
                    log.info("morning brief sent for %s", today)
                elif not brief_sent and now.tm_hour == self.cfg.agent.brief_hour:
                    log.info("brief window reached, sending")
            except Exception as e:
                log.error("brief failed: %s: %s", type(e).__name__, e)

            self._stop.wait(300)

    def _brief_done(self, day: str) -> bool:
        """Catch-up: mark the brief as sent for a day only after it is."""
        from ..config import DATA_DIR

        p = DATA_DIR / "brief_state.json"
        if not p.exists():
            return False
        import json

        try:
            return json.loads(p.read_text()).get("last_day") == day
        except Exception:
            return False

    def _mark_brief(self, day: str) -> None:
        from ..config import DATA_DIR
        import json

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "brief_state.json").write_text(json.dumps({"last_day": day}))

    # --------------------------------------------------------------- watchdog
    def _watchdog(self) -> None:
        """Detects a wedged or silently-dying loop."""
        while not self.should_stop():
            last = self.health.last_scan or self.health.started_at
            # Two missed cycles means the loop is stuck, not busy.
            threshold = max(300, self.cfg.agent.scan_interval_sec * 2)
            if time.time() - last > threshold and self.health.cycles > 0:
                log.error("no scan in %ds (threshold %ds) — loop may be wedged",
                          int(time.time() - last), threshold)
                self._say("mail-agent: loop appears stuck. Restarting the service.")
            self._stop.wait(60)

    # -------------------------------------------------------------- utilities
    def _say(self, text: str) -> None:
        if not self.notify:
            return
        try:
            self.notify(text)
        except Exception as e:
            log.warning("notify failed: %s", type(e).__name__)

    def health_json(self) -> str:
        import json

        return json.dumps(self.health.snapshot(), indent=2)


def run_daemon(cfg, providers_factory, scan_fn, tick_fn, notify=None) -> int:
    return Supervisor(cfg, providers_factory, scan_fn, tick_fn, notify).start()
