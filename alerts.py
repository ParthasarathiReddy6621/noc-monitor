"""
Alert engine: turns check results into incidents and notifications.

For every check + condition (down, latency, utilization, errors) there is a small
state machine with HYSTERESIS:

    OK  --(fail_threshold failures in a row)-->  ALERTING  (open incident, notify PROBLEM)
    ALERTING --(recover_threshold OKs in a row)-->  OK     (resolve incident, notify RESOLVED)

So one lost ping never pages anyone, and a link must be stable before "resolved".

FLAP SUPPRESSION: if a check changes state more than flap_max_changes times within
flap_window_minutes, it is marked FLAPPING. One FLAPPING message is sent, further
PROBLEM/RESOLVED messages are muted (incidents are still recorded), and when it has
been stable for a full window a single STABLE message reports the final state.
"""

import threading
import time
from collections import deque

import db
from notifier import Notifier

DEFAULTS = {
    "enabled": True,
    "fail_threshold": 3,          # consecutive failed checks before alerting
    "recover_threshold": 2,       # consecutive good checks before resolving
    "latency_warn_ms": 150,       # ping/http/dns/port slower than this = latency alert
    "utilization_warn_pct": 80,   # SNMP interface utilisation
    "errors_warn_per_poll": 10,   # new interface errors between two polls
    "flap_window_minutes": 10,
    "flap_max_changes": 4,
    "repeat_minutes": 30,         # remind about open CRITICAL incidents (0 = off)
}

DEFAULT_SEVERITY = {"ping": "major", "port": "minor", "http": "major",
                    "dns": "minor", "snmp": "major", "iface": "major"}


def _fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


class _State:
    """Runtime state for one (check, condition)."""
    def __init__(self):
        self.alerting = False
        self.fail = 0
        self.ok = 0
        self.changes = deque()        # timestamps of state changes (for flap detection)
        self.flapping = False
        self.incident_id = None
        self.since = time.time()      # when the current state began
        self.last_notified = 0.0


class AlertEngine:
    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.cfg = {**DEFAULTS, **{k: v for k, v in cfg.items() if k != "channels"}}
        self.notifier = Notifier(cfg.get("channels"))
        self.states = {}
        self.lock = threading.Lock()
        self._load_open_incidents()

    # ------------------------------------------------------------ startup
    def _load_open_incidents(self):
        """After a restart, keep tracking incidents that are still open."""
        try:
            for inc in db.list_incidents(status="active", limit=1000):
                st = self.states.setdefault((inc["check_id"], inc["condition"]), _State())
                st.alerting = True
                st.incident_id = inc["id"]
                st.flapping = bool(inc["flapping"])
        except Exception:
            pass   # table may not exist yet on first run

    # ------------------------------------------------------------ conditions
    def _conditions(self, check, result):
        """Yield (condition, is_failing, detail, severity) for one result."""
        severity = check.get("severity") or DEFAULT_SEVERITY.get(check["type"], "major")
        yield "down", result["status"] == "DOWN", result.get("detail", ""), severity

        if result["status"] == "UP" and result.get("response_ms") is not None \
                and check["type"] in ("ping", "port", "http", "dns"):
            limit = check.get("latency_warn_ms", self.cfg["latency_warn_ms"])
            yield ("latency", result["response_ms"] > limit,
                   f"response {result['response_ms']} ms (limit {limit:g} ms)", "minor")

        metrics = result.get("metrics") or {}
        if metrics.get("util_pct") is not None:
            limit = self.cfg["utilization_warn_pct"]
            yield ("utilization", metrics["util_pct"] > limit,
                   f"utilisation {metrics['util_pct']}% (limit {limit}%)", "major")
        if metrics.get("errors_delta") is not None:
            limit = self.cfg["errors_warn_per_poll"]
            yield ("errors", metrics["errors_delta"] > limit,
                   f"+{metrics['errors_delta']} interface errors since last poll (limit {limit})",
                   "minor")

    # ------------------------------------------------------------ main entry
    def process(self, check, result):
        if not self.cfg.get("enabled", True):
            return
        with self.lock:
            for condition, failing, detail, severity in self._conditions(check, result):
                self._step(check, condition, failing, detail, severity)

    def _step(self, check, condition, failing, detail, severity):
        key = (check["id"], condition)
        st = self.states.setdefault(key, _State())
        now = time.time()
        label = f"{check['name']} ({check['type']} {check['target']})"
        what = {"down": "is DOWN", "latency": "is SLOW", "utilization": "HIGH UTILISATION",
                "errors": "INTERFACE ERRORS"}[condition]

        if failing:
            st.fail, st.ok = st.fail + 1, 0
        else:
            st.ok, st.fail = st.ok + 1, 0

        if not st.alerting and st.fail >= self.cfg["fail_threshold"]:
            # ---- new problem
            st.alerting, st.since = True, now
            st.incident_id = db.open_incident(check, condition, severity, detail)
            self._state_changed(st, now, label, severity)
            self._send(st, "PROBLEM", severity, f"{label} {what}",
                       f"{detail}\nIncident #{st.incident_id} after "
                       f"{self.cfg['fail_threshold']} failed checks")

        elif st.alerting and st.ok >= self.cfg["recover_threshold"]:
            # ---- recovered
            duration = _fmt_duration(now - st.since)
            st.alerting, st.since = False, now
            if st.incident_id:
                db.resolve_incident(st.incident_id, detail)
            self._state_changed(st, now, label, severity)
            self._send(st, "RESOLVED", severity, f"{label} recovered",
                       f"Problem lasted {duration}. Incident #{st.incident_id} closed.")
            st.incident_id = None

        elif st.alerting and failing and st.incident_id:
            # ---- still broken: update incident, maybe remind
            db.touch_incident(st.incident_id, detail)
            repeat = self.cfg["repeat_minutes"]
            if repeat and severity == "critical" and now - st.last_notified > repeat * 60:
                self._send(st, "REMINDER", severity, f"{label} still {what}",
                           f"Down for {_fmt_duration(now - st.since)}. Incident #{st.incident_id}")

        # ---- end of flapping: stable for a whole window
        window = self.cfg["flap_window_minutes"] * 60
        if st.flapping and st.changes and now - st.changes[-1] >= window:
            st.flapping = False
            if st.incident_id:
                db.set_incident_flapping(st.incident_id, False)
            state = what if st.alerting else "OK"
            self.notifier.notify("STABLE", severity, f"{label} stopped flapping",
                                 f"No changes for {self.cfg['flap_window_minutes']} min. "
                                 f"Current state: {state}. Notifications resumed.")

    # ------------------------------------------------------------ flapping
    def _state_changed(self, st, now, label, severity):
        window = self.cfg["flap_window_minutes"] * 60
        st.changes.append(now)
        while st.changes and now - st.changes[0] > window:
            st.changes.popleft()
        if not st.flapping and len(st.changes) > self.cfg["flap_max_changes"]:
            st.flapping = True
            if st.incident_id:
                db.set_incident_flapping(st.incident_id, True)
            self.notifier.notify(
                "FLAPPING", severity, f"{label} is flapping",
                f"{len(st.changes)} state changes in {self.cfg['flap_window_minutes']} min. "
                f"Further alerts muted until it is stable.")

    def _send(self, st, event, severity, title, body):
        if st.flapping and event in ("PROBLEM", "RESOLVED", "REMINDER"):
            print(f"[alerts] suppressed {event} (flapping): {title}", flush=True)
            return
        st.last_notified = time.time()
        self.notifier.notify(event, severity, title, body)

    # ------------------------------------------------------------ test
    def send_test(self):
        self.notifier.notify("TEST", "critical", "NOC Monitor test alert",
                             "If you can read this, notifications are working.", force=True)
