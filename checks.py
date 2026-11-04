"""
The actual checks. Every function returns the same shape:
    {"status": "UP" | "DOWN", "response_ms": float | None, "detail": str}
"""

import platform
import re
import socket
import subprocess
import time

import dns.exception
import dns.resolver
import requests


def _result(status, response_ms=None, detail=""):
    return {"status": status, "response_ms": response_ms, "detail": detail}


def _elapsed_ms(start):
    return round((time.perf_counter() - start) * 1000, 2)


# ---------------------------------------------------------------- ICMP
def ping_host(address, count=2, timeout=2):
    """Ping using the OS ping command (no admin rights needed)."""
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", str(count), "-w", str(timeout * 1000), address]
    elif system == "darwin":
        cmd = ["ping", "-c", str(count), "-W", str(timeout * 1000), address]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(timeout), address]

    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=count * timeout + 5).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return _result("DOWN", None, f"ping failed: {type(e).__name__}")

    loss_match = re.search(r"(\d+(?:\.\d+)?)% (?:packet )?loss", out)
    loss = float(loss_match.group(1)) if loss_match else 100.0
    times = [float(t) for t in re.findall(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", out)]
    latency = round(sum(times) / len(times), 2) if times else None

    if times and loss < 100:
        return _result("UP", latency, f"loss={loss}%")
    return _result("DOWN", None, f"loss={loss}%")


# ---------------------------------------------------------------- TCP
def check_port(address, port, timeout=2):
    """UP if a TCP 3-way handshake completes."""
    start = time.perf_counter()
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return _result("UP", _elapsed_ms(start), "open")
    except socket.timeout:
        return _result("DOWN", None, "timeout (filtered?)")
    except ConnectionRefusedError:
        return _result("DOWN", None, "connection refused (nothing listening)")
    except OSError as e:
        return _result("DOWN", None, str(e)[:120])


# ---------------------------------------------------------------- HTTP
def check_http(url, expected_status=200, keyword=None, timeout=5):
    """UP if the status code matches and (optionally) the keyword is on the page."""
    start = time.perf_counter()
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "noc-monitor/0.2"})
    except requests.exceptions.SSLError:
        return _result("DOWN", None, "SSL certificate error")
    except requests.exceptions.Timeout:
        return _result("DOWN", None, f"timeout after {timeout}s")
    except requests.exceptions.ConnectionError:
        return _result("DOWN", None, "connection error (DNS failure or host unreachable)")
    except requests.exceptions.RequestException as e:
        return _result("DOWN", None, str(e)[:120])

    ms = _elapsed_ms(start)
    if r.status_code != expected_status:
        return _result("DOWN", ms, f"HTTP {r.status_code} (expected {expected_status})")
    if keyword and keyword not in r.text:
        return _result("DOWN", ms, f"HTTP {r.status_code} but keyword '{keyword}' not found")
    return _result("UP", ms, f"HTTP {r.status_code}")


# ---------------------------------------------------------------- DNS
def check_dns(domain, record_type="A", server=None, expected=None, timeout=2):
    """UP if the record resolves (and contains 'expected', if given)."""
    if server:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [server]
    else:
        resolver = dns.resolver.Resolver()     # uses the system's DNS servers
    resolver.timeout = timeout
    resolver.lifetime = timeout

    start = time.perf_counter()
    try:
        answer = resolver.resolve(domain, record_type)
    except dns.resolver.NXDOMAIN:
        return _result("DOWN", _elapsed_ms(start), "NXDOMAIN (domain does not exist)")
    except dns.resolver.NoAnswer:
        return _result("DOWN", _elapsed_ms(start), f"no {record_type} record")
    except dns.resolver.NoNameservers:
        return _result("DOWN", None, "no nameserver answered (SERVFAIL/REFUSED)")
    except dns.exception.Timeout:
        return _result("DOWN", None, f"timeout after {timeout}s")
    except dns.exception.DNSException as e:
        return _result("DOWN", None, str(e)[:120])

    ms = _elapsed_ms(start)
    records = sorted(r.to_text() for r in answer)
    if expected and expected not in records:
        return _result("DOWN", ms, f"got {records}, expected {expected}")
    return _result("UP", ms, ", ".join(records)[:200])
