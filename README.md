# NOC Monitor: Network Monitoring & Alerting Tool

A lightweight network monitoring and alerting tool for a **Network Operations Center (NOC)**, written in Python. It checks devices and services on a schedule, polls router interfaces over SNMP, raises alerts by severity, tracks incidents from open to resolved, and shows everything on a live dashboard.

Tested on a **GNS3 lab** with FRR routers running **OSPF** across a core and two branch LANs.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-REST%20API-009688)
![SNMP](https://img.shields.io/badge/SNMP-v2c-orange)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED)

---

## Screenshots

**Live NOC dashboard**: all 8 lab devices UP, with latency and 24h uptime for each

![NOC Dashboard](docs/dashboard.png)

**Incidents and daily report**: the same report is sent to Telegram automatically every day

![Incidents and daily report](docs/daily-report.png)

**GNS3 lab topology**: 3 FRR routers (OSPF), 2 switches, 4 PCs and the NOC Monitor container

![GNS3 topology](docs/topology.png)

---

## Features

**Monitoring**
- ICMP ping (latency and packet loss), TCP port, HTTP(S) and DNS checks
- SNMP polling of interface status (IF-MIB) and traffic counters
- Bandwidth from counter deltas, with 32-bit counter-wrap and device-reboot handling
- Scheduled checks (APScheduler), with every result stored in SQLite

**Alerting**
- Severity levels: `critical`, `major`, `minor`, set per device or check
- Hysteresis: 3 failed checks before alerting and 2 good checks before resolving, to avoid false alarms
- Conditions covered: device down, high latency, interface utilization, interface errors
- Flap detection that mutes noisy devices and reports when they are stable again
- Notifications to the console, **Telegram** and **email (SMTP)**, each with its own minimum severity
- Repeat reminders for open critical incidents

**Incident management**
- Incident lifecycle: `OPEN → ACKNOWLEDGED → RESOLVED`
- **MTTA** (mean time to acknowledge) and **MTTR** (mean time to resolve) tracking

**Dashboard & reporting**
- Live NOC dashboard that auto-refreshes every 15 s, with:
  - KPI tiles
  - device status cards
  - latency and uptime charts
  - an incident table with an *Acknowledge* button
- Automatic daily summary report sent to Telegram
- REST API (FastAPI) with Swagger docs at `/docs`
- Prometheus-compatible `/metrics` endpoint, ready for Grafana

---

## Architecture

```mermaid
flowchart LR
    Y[hosts.yaml] --> S[Scheduler<br/>APScheduler]
    S --> C[Checks<br/>ping / port / HTTP / DNS]
    S --> P[SNMP Poller<br/>IF-MIB counters]
    C --> DB[(SQLite)]
    P --> DB
    C --> A[Alert Engine<br/>thresholds, severity, flapping]
    P --> A
    A --> DB
    A --> N[Notifier]
    N --> T[Telegram]
    N --> E[Email]
    DB --> API[FastAPI REST API]
    API --> D[NOC Dashboard]
    API --> M[/metrics<br/>Prometheus]
```

---

## Lab Topology (GNS3)

```
  [Cloud1]                 [ CORE ]
  (internet)              /        \
     |             [ BRANCH1 ]    [ BRANCH2 ]
     |             LAN 10.0.10.0/24   LAN 10.0.20.0/24
     |                  |               |
  [NOC Monitor]----[Switch1]       [Switch2]
   10.0.10.100       /    \          /    \
                   PC1    PC2      PC3    PC4
```

- Routers: FRR 7.3.1 on Alpine Linux, running OSPF
- The NOC Monitor runs as a Docker container inside GNS3, with a second interface for internet access (Telegram alerts)

---

## Project Structure

| File | Purpose |
|---|---|
| `monitor.py` | Command-line entry point |
| `api.py` | FastAPI server, dashboard and REST endpoints |
| `checks.py` | Ping, port, HTTP and DNS checks |
| `snmp_poller.py` | SNMP interface polling |
| `bandwidth.py` | Bandwidth and utilization math from counters |
| `alerts.py` | Alert engine: thresholds, severity, flapping, incidents |
| `notifier.py` | Console, Telegram and email delivery with retries |
| `reports.py` | Daily summary report |
| `runner.py` | Scheduler and check runner |
| `db.py` | SQLite storage |
| `config.py` | YAML and `.env` configuration loader |
| `dashboard.html` | NOC dashboard (Chart.js) |
| `hosts.yaml` | Devices to monitor (GNS3 lab) |
| `hosts-alert-demo.yaml` | Demo config that works on any PC without a lab |
| `Dockerfile` | Container image |

---

## Quick Start

```bash
git clone https://github.com/<your-username>/noc-monitor.git
cd noc-monitor
pip install -r requirements.txt
```

**Try it without a lab.** The demo config includes one unreachable host, so you will see an alert within about a minute:

```bash
# Linux / macOS
NOC_CONFIG=hosts-alert-demo.yaml python monitor.py --schedule

# Windows PowerShell
$env:NOC_CONFIG="hosts-alert-demo.yaml"; python monitor.py --schedule
```

**Start the API and dashboard:**

```bash
python api.py
```

Then open <http://127.0.0.1:8000/dashboard> in a browser. The API docs are at `/docs`.

### Command-line options

```
python monitor.py --schedule      # run checks continuously
python monitor.py --report        # uptime report
python monitor.py --interfaces    # SNMP interface table
python monitor.py --incidents     # incidents with MTTA/MTTR
python monitor.py --test-alert    # send a test notification
```

### Alert setup (Telegram / email)

Copy `.env.example` to `.env` and fill in your values:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

`.env` is listed in `.gitignore`, so your secrets are never committed.

### Docker

```bash
docker build -t noc-monitor .
docker run -p 8000:8000 --env-file .env noc-monitor
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/dashboard` | NOC dashboard |
| GET | `/status` | Latest result for every check |
| GET | `/status/down` | Checks that are currently down |
| GET | `/uptime` | Uptime % per check |
| GET | `/interfaces` | SNMP interface status and bandwidth |
| GET | `/incidents` | Incident list (filter with `?status=active`) |
| GET | `/incidents/summary` | MTTA / MTTR summary |
| POST | `/incidents/{id}/ack` | Acknowledge an incident |
| GET | `/reports/daily` | Daily report preview |
| GET | `/metrics` | Prometheus metrics |

---

## Example Configuration

```yaml
hosts:
  - name: CORE
    address: 10.0.1.1
    severity: critical
  - name: BRANCH1
    address: 10.0.10.1
    severity: major

alerting:
  fail_threshold: 3
  recover_threshold: 2
  daily_report: "09:00"
  channels:
    console:  {min_severity: info}
    telegram: {min_severity: major}
```

---

## What I Learned

- How NOC tools reduce alert noise using thresholds, hysteresis and flap suppression
- SNMP IF-MIB counters and how to calculate bandwidth correctly, including counter wraps and reboots
- Incident workflows and NOC KPIs (MTTA, MTTR, availability)
- Building and troubleshooting a multi-router OSPF lab in GNS3: routing, IP forwarding, NAT and virtualization issues

## Future Improvements

- Dependency-aware alerting (suppress alerts for PCs when their branch router is down)
- SNMPv3 support
- Grafana dashboard using the `/metrics` endpoint
- Docker Compose setup

---

## Author

**PARTHU** · Aspiring NOC Engineer
[LinkedIn](https://www.linkedin.com/in/your-profile)
