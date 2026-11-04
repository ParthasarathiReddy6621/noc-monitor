# NOC Monitor - runs inside the GNS3 lab as a "monitoring server" node.
# Build on the GNS3 VM:   docker build -t noc-monitor .
FROM python:3.11-slim

# ping for ICMP checks, tzdata for local timestamps, iproute2 for troubleshooting
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping iproute2 tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV TZ=Asia/Kolkata \
    NOC_DB=/data/monitor.db \
    NOC_API_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data
EXPOSE 8000

# Starts the scheduler (all checks) and the web API on port 8000
CMD ["python", "api.py"]
