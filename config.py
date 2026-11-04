"""Loads hosts.yaml and turns it into a flat list of check definitions."""

import os
import re
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
# NOC_CONFIG lets you pick another file, e.g.  NOC_CONFIG=hosts-alert-demo.yaml
CONFIG_FILE = Path(os.environ.get("NOC_CONFIG", "hosts.yaml"))
if not CONFIG_FILE.is_absolute():
    CONFIG_FILE = BASE_DIR / CONFIG_FILE

DEFAULT_SETTINGS = {"ping_count": 2, "timeout_seconds": 2, "interval_seconds": 60}


def load_dotenv(path=BASE_DIR / ".env"):
    """Read KEY=VALUE lines from .env into the environment (secrets stay out of hosts.yaml).
    Real environment variables win over the file."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _alert_opts(item):
    """Per-device alert settings copied onto each check (optional in hosts.yaml)."""
    opts = {}
    if item.get("severity"):
        opts["severity"] = str(item["severity"]).lower()
    if item.get("latency_warn_ms") is not None:
        opts["latency_warn_ms"] = float(item["latency_warn_ms"])
    return opts


def slug(text):
    """'Google DNS' -> 'google-dns' (safe to use in URLs)."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def load_config(path=CONFIG_FILE):
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}\n"
                         f"Put hosts.yaml in the same folder as the scripts.")
    with open(path, "r") as f:
        config = yaml.safe_load(f) or {}

    load_dotenv()
    settings = {**DEFAULT_SETTINGS, **(config.get("settings") or {})}
    settings["alerting"] = config.get("alerting") or {}
    default_interval = settings["interval_seconds"]
    timeout = settings["timeout_seconds"]
    checks = []

    # Ping + port checks for each host
    for h in config.get("hosts") or []:
        interval = h.get("interval_seconds", default_interval)
        checks.append({
            "id": f"ping-{slug(h['name'])}", "type": "ping",
            "name": h["name"], "target": h["address"], "interval": interval,
            "params": {"address": h["address"], "count": settings["ping_count"], "timeout": timeout},
            **_alert_opts(h),
        })
        for port in h.get("ports") or []:
            checks.append({
                "id": f"port-{slug(h['name'])}-{port}", "type": "port",
                "name": h["name"], "target": f"{h['address']}:{port}", "interval": interval,
                "params": {"address": h["address"], "port": port, "timeout": timeout},
                **_alert_opts(h),
            })

    # HTTP checks
    for c in config.get("http_checks") or []:
        checks.append({
            "id": f"http-{slug(c['name'])}", "type": "http",
            "name": c["name"], "target": c["url"],
            "interval": c.get("interval_seconds", default_interval),
            "params": {
                "url": c["url"],
                "expected_status": c.get("expected_status", 200),
                "keyword": c.get("keyword"),
                "timeout": c.get("timeout_seconds", 5),
            },
            **_alert_opts(c),
        })

    # DNS checks
    for c in config.get("dns_checks") or []:
        rtype = c.get("record_type", "A").upper()
        target = f"{c['domain']} {rtype}" + (f" @{c['server']}" if c.get("server") else "")
        checks.append({
            "id": f"dns-{slug(c['name'])}", "type": "dns",
            "name": c["name"], "target": target,
            "interval": c.get("interval_seconds", default_interval),
            "params": {
                "domain": c["domain"], "record_type": rtype,
                "server": c.get("server"), "expected": c.get("expected"),
                "timeout": timeout,
            },
            **_alert_opts(c),
        })

    # SNMP devices (interface status + bandwidth)
    for d in config.get("snmp_devices") or []:
        version = str(d.get("version", "2c"))
        port = int(d.get("port", 161))
        checks.append({
            "id": f"snmp-{slug(d['name'])}", "type": "snmp",
            "name": d["name"],
            "target": f"{d['address']}:{port} SNMPv{version}",
            "interval": d.get("interval_seconds", default_interval),
            "params": {
                "name": d["name"], "address": d["address"],
                "community": str(d.get("community", "public")),
                "version": version, "port": port,
                "timeout": d.get("timeout_seconds", timeout),
                "retries": d.get("retries", 1),
                "interfaces": [str(i) for i in (d.get("interfaces") or [])],
            },
            **_alert_opts(d),
        })

    # Every check needs a unique id (used in the database and API URLs)
    ids = [c["id"] for c in checks]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise SystemExit(f"Duplicate check names in hosts.yaml: {', '.join(sorted(duplicates))}")

    return settings, checks
