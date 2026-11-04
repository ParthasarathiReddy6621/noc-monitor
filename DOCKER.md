# Running NOC Monitor inside the GNS3 lab (Docker)

The monitor runs as a container node in the GNS3 topology, connected to LAN1
(10.0.10.100/24, gateway 10.0.10.1), like a real NOC management server.

## Build (on the GNS3 VM)
    tar xzf noc-monitor-docker.tar.gz
    cd noc-monitor
    docker build -t noc-monitor .

## GNS3 template
- Preferences > Docker containers > New > Run on the GNS3 VM
- Image: `noc-monitor:latest`, adapters: 1
- Console type: `http`, HTTP port: `8000`, HTTP path: `/docs`
- Persistent directories: `/data`  (keeps the SQLite database)

## Node network config (eth0)
    auto eth0
    iface eth0 inet static
        address 10.0.10.100
        netmask 255.255.255.0
        gateway 10.0.10.1

## Use
- Double-click the node: opens the API (`/docs`, `/status`, `/uptime`) in your browser
- Auxiliary console: `python monitor.py --report` or `--interfaces`
- Edit targets in `hosts.yaml`, then rebuild the image
