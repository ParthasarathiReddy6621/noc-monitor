"""SQLite storage: one row per check result."""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from config import BASE_DIR

# NOC_DB lets the Docker container keep its database in a persistent folder (/data)
DB_FILE = os.environ.get("NOC_DB", str(BASE_DIR / "monitor.db"))
_write_lock = threading.Lock()   # checks run in parallel threads; serialize writes


@contextmanager
def connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS check_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                check_id    TEXT NOT NULL,
                check_type  TEXT NOT NULL,     -- ping / port / http / dns
                name        TEXT NOT NULL,
                target      TEXT NOT NULL,
                status      TEXT NOT NULL,     -- UP / DOWN
                response_ms REAL,
                detail      TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_check_time "
                     "ON check_results (check_id, timestamp)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS interface_stats (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp        TEXT NOT NULL,
                epoch            REAL NOT NULL,     -- exact poll time (for rate maths)
                device           TEXT NOT NULL,
                if_index         INTEGER NOT NULL,
                if_name          TEXT,
                if_alias         TEXT,
                admin_status     INTEGER,           -- 1=up 2=down
                oper_status      INTEGER,           -- 1=up 2=down 7=lowerLayerDown ...
                speed_bps        INTEGER,
                counter_bits     INTEGER,           -- 32 or 64
                in_octets        TEXT,              -- TEXT: 64-bit counters can exceed SQLite's INTEGER
                out_octets       TEXT,
                in_errors        INTEGER,
                out_errors       INTEGER,
                sys_uptime       INTEGER,           -- hundredths of a second
                in_bps           REAL,
                out_bps          REAL,
                in_util_pct      REAL,
                out_util_pct     REAL,
                in_errors_delta  INTEGER,
                out_errors_delta INTEGER,
                note             TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_iface "
                     "ON interface_stats (device, if_index, id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                check_id        TEXT NOT NULL,
                check_type      TEXT NOT NULL,
                name            TEXT NOT NULL,
                target          TEXT NOT NULL,
                condition       TEXT NOT NULL,      -- down / latency / utilization / errors
                severity        TEXT NOT NULL,      -- critical / major / minor
                status          TEXT NOT NULL,      -- OPEN / ACKNOWLEDGED / RESOLVED
                opened_at       TEXT NOT NULL,
                acknowledged_at TEXT,
                acknowledged_by TEXT,
                resolved_at     TEXT,
                last_seen       TEXT NOT NULL,
                detail          TEXT,
                flapping        INTEGER NOT NULL DEFAULT 0,
                occurrences     INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_incident_status ON incidents (status, id)")


def save_result(check, result, timestamp):
    with _write_lock, connect() as conn:
        conn.execute(
            "INSERT INTO check_results "
            "(timestamp, check_id, check_type, name, target, status, response_ms, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (timestamp, check["id"], check["type"], check["name"], check["target"],
             result["status"], result["response_ms"], result["detail"]),
        )


def get_latest():
    """Most recent result for every check."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT r.* FROM check_results r
            JOIN (SELECT check_id, MAX(id) AS max_id
                  FROM check_results GROUP BY check_id) latest
              ON r.id = latest.max_id
            ORDER BY r.check_type, r.name, r.target
        """).fetchall()
    return [dict(r) for r in rows]


def get_history(check_id, limit=50):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM check_results WHERE check_id = ? ORDER BY id DESC LIMIT ?",
            (check_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def check_id_exists(check_id):
    with connect() as conn:
        return conn.execute("SELECT 1 FROM check_results WHERE check_id = ? LIMIT 1",
                            (check_id,)).fetchone() is not None


# ---------------------------------------------------------------- interfaces (SNMP)
IFACE_COLUMNS = ["timestamp", "epoch", "device", "if_index", "if_name", "if_alias",
                 "admin_status", "oper_status", "speed_bps", "counter_bits",
                 "in_octets", "out_octets", "in_errors", "out_errors", "sys_uptime",
                 "in_bps", "out_bps", "in_util_pct", "out_util_pct",
                 "in_errors_delta", "out_errors_delta", "note"]


def _iface_row(row):
    d = dict(row)
    d["in_octets"] = int(d["in_octets"])
    d["out_octets"] = int(d["out_octets"])
    return d


def save_interface_sample(sample):
    values = [str(sample[c]) if c in ("in_octets", "out_octets") else sample.get(c)
              for c in IFACE_COLUMNS]
    with _write_lock, connect() as conn:
        conn.execute(f"INSERT INTO interface_stats ({', '.join(IFACE_COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(IFACE_COLUMNS))})", values)


def get_last_interface_sample(device, if_index):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM interface_stats WHERE device = ? AND if_index = ? "
            "ORDER BY id DESC LIMIT 1", (device, if_index)).fetchone()
    return _iface_row(row) if row else None


def get_latest_interfaces(device=None):
    sql = """
        SELECT s.* FROM interface_stats s
        JOIN (SELECT device, if_index, MAX(id) AS max_id
              FROM interface_stats GROUP BY device, if_index) latest
          ON s.id = latest.max_id
    """
    params = ()
    if device:
        sql += " WHERE s.device = ?"
        params = (device,)
    sql += " ORDER BY s.device, s.if_index"
    with connect() as conn:
        return [_iface_row(r) for r in conn.execute(sql, params).fetchall()]


def get_interface_history(device, if_index, limit=60):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM interface_stats WHERE device = ? AND if_index = ? "
            "ORDER BY id DESC LIMIT ?", (device, if_index, limit)).fetchall()
    return [_iface_row(r) for r in rows]


def get_uptime(hours=24):
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute("""
            SELECT check_id, check_type, name, target,
                   COUNT(*)                                          AS checks,
                   ROUND(100.0 * SUM(status = 'UP') / COUNT(*), 2)  AS uptime_pct,
                   ROUND(AVG(response_ms), 2)                       AS avg_response_ms
            FROM check_results
            WHERE timestamp >= ?
            GROUP BY check_id
            ORDER BY uptime_pct, check_type, name
        """, (since,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- incidents (alerting)
def _now():
    return datetime.now().isoformat(timespec="seconds")


def open_incident(check, condition, severity, detail):
    ts = _now()
    with _write_lock, connect() as conn:
        cur = conn.execute(
            "INSERT INTO incidents (check_id, check_type, name, target, condition, severity, "
            "status, opened_at, last_seen, detail) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)",
            (check["id"], check["type"], check["name"], check["target"], condition,
             severity, ts, ts, detail))
        return cur.lastrowid


def touch_incident(incident_id, detail):
    with _write_lock, connect() as conn:
        conn.execute("UPDATE incidents SET last_seen = ?, detail = ?, occurrences = occurrences + 1 "
                     "WHERE id = ?", (_now(), detail, incident_id))


def resolve_incident(incident_id, detail):
    with _write_lock, connect() as conn:
        conn.execute("UPDATE incidents SET status = 'RESOLVED', resolved_at = ?, last_seen = ?, "
                     "detail = ? WHERE id = ?", (_now(), _now(), detail, incident_id))


def set_incident_flapping(incident_id, flapping):
    with _write_lock, connect() as conn:
        conn.execute("UPDATE incidents SET flapping = ? WHERE id = ?", (1 if flapping else 0, incident_id))


def acknowledge_incident(incident_id, who="noc-engineer"):
    with _write_lock, connect() as conn:
        cur = conn.execute(
            "UPDATE incidents SET status = 'ACKNOWLEDGED', acknowledged_at = ?, acknowledged_by = ? "
            "WHERE id = ? AND status = 'OPEN'", (_now(), who, incident_id))
        return cur.rowcount > 0


def get_incident(incident_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    return dict(row) if row else None


def list_incidents(status=None, limit=100):
    sql, params = "SELECT * FROM incidents", []
    if status == "active":
        sql += " WHERE status IN ('OPEN', 'ACKNOWLEDGED')"
    elif status:
        sql += " WHERE status = ?"
        params.append(status.upper())
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def incident_summary():
    """Counts by status/severity, plus MTTA and MTTR in minutes (resolved incidents)."""
    with connect() as conn:
        by_sev = conn.execute(
            "SELECT severity, COUNT(*) AS n FROM incidents "
            "WHERE status IN ('OPEN','ACKNOWLEDGED') GROUP BY severity").fetchall()
        times = conn.execute("""
            SELECT
              ROUND(AVG((julianday(acknowledged_at) - julianday(opened_at)) * 1440), 1) AS mtta,
              ROUND(AVG((julianday(resolved_at)     - julianday(opened_at)) * 1440), 1) AS mttr,
              COUNT(*) AS total
            FROM incidents
        """).fetchone()
    return {"active_by_severity": {r["severity"]: r["n"] for r in by_sev},
            "mtta_minutes": times["mtta"], "mttr_minutes": times["mttr"],
            "total_incidents": times["total"]}


# ---------------------------------------------------------------- dashboard / reports (Week 5)
def get_timeseries(hours=6, bucket_minutes=5, check_types=("ping", "http", "port", "dns")):
    """Average response time and availability per check, in time buckets (for charts)."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    size = int(bucket_minutes) * 60
    marks = ", ".join("?" * len(check_types))
    with connect() as conn:
        rows = conn.execute(f"""
            SELECT check_id, check_type, name,
                   datetime((CAST(strftime('%s', timestamp) AS INTEGER) / {size}) * {size},
                            'unixepoch') AS bucket,
                   ROUND(AVG(response_ms), 2)                       AS avg_ms,
                   ROUND(100.0 * SUM(status = 'UP') / COUNT(*), 1)  AS up_pct
            FROM check_results
            WHERE timestamp >= ? AND check_type IN ({marks})
            GROUP BY check_id, bucket
            ORDER BY bucket
        """, (since, *check_types)).fetchall()
    return [dict(r) for r in rows]


def incident_stats_since(hours=24):
    """Numbers for the daily report."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with connect() as conn:
        r = conn.execute("""
            SELECT COUNT(*) AS opened,
                   SUM(status = 'RESOLVED') AS resolved,
                   SUM(severity = 'critical') AS critical,
                   SUM(flapping) AS flapping,
                   ROUND(AVG(CASE WHEN resolved_at IS NOT NULL
                         THEN (julianday(resolved_at) - julianday(opened_at)) * 1440 END), 1) AS mttr
            FROM incidents WHERE opened_at >= ?
        """, (since,)).fetchone()
        worst = conn.execute("""
            SELECT name, COUNT(*) AS n FROM incidents WHERE opened_at >= ?
            GROUP BY name ORDER BY n DESC LIMIT 3
        """, (since,)).fetchall()
        avail = conn.execute("""
            SELECT ROUND(100.0 * SUM(status = 'UP') / COUNT(*), 2) AS pct
            FROM check_results WHERE timestamp >= ?
        """, (since,)).fetchone()
    return {"opened": r["opened"] or 0, "resolved": r["resolved"] or 0,
            "critical": r["critical"] or 0, "flapping": r["flapping"] or 0,
            "mttr_minutes": r["mttr"], "availability_pct": avail["pct"],
            "most_affected": [(w["name"], w["n"]) for w in worst]}
