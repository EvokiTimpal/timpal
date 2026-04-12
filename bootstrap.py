#!/usr/bin/env python3
"""
TIMPAL Bootstrap Server v4.0

Peer directory only. Zero consensus authority. Zero lottery role.
Handles exactly five message types: HELLO, PING, GET_PEERS,
REGISTER_BOOTSTRAP, GET_BOOTSTRAP_SERVERS.

Everything else from v3.3 is permanently deleted:
SUBMIT_COMMIT, SUBMIT_REVEAL, GET_COMMITS, GET_REVEALS,
SUBMIT_TIP, GET_CHECKPOINT_TIP, identity maturation tracking,
network size reporting, lottery state, chain tip authority.
"""

import socket
import threading
import json
import time
import random

PORT        = 7777
VERSION     = "4.0"
MIN_VERSION = "4.0"

GENESIS_TIME = 1776020400   # 12:00 PM PDT / 19:00 UTC April 12 2026 — must match timpal.py

# Rate limits (DoS protection only — no consensus gatekeeping)
HELLO_RATE_LIMIT = 10   # per 60 seconds per IP
BS_RATE_LIMIT    = 5    # bootstrap registrations per hour per IP
BS_MAX_SERVERS   = 100
HELLO_PEERS_SAMPLE = 50
PEER_STALE_SECONDS = 300    # 5 minutes


def _check_genesis_time():
    if GENESIS_TIME == 0:
        print("\n  ERROR: GENESIS_TIME not set. Must match timpal.py.\n")
        exit(1)


def _ver(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0, 0)


# ── Shared state ───────────────────────────────────────────────────────────────

peers             = {}    # {device_id: {ip, port, last_seen}}
peers_lock        = threading.Lock()

bootstrap_servers = {}    # {key: {host, port, last_seen}}
bs_lock           = threading.Lock()

hello_ip_rate     = {}    # {ip: [timestamps]}
bs_ip_rate        = {}    # {ip: [timestamps]}
rate_lock         = threading.Lock()


# ── Background cleanup ─────────────────────────────────────────────────────────

def _clean_old_data():
    while True:
        time.sleep(60)
        now    = time.time()
        cutoff = now - PEER_STALE_SECONDS

        with peers_lock:
            stale = [p for p, d in peers.items() if d["last_seen"] < cutoff]
            for p in stale:
                del peers[p]
            if stale:
                print(f"  Cleaned {len(stale)} stale peers. Active: {len(peers)}")

        bs_cut = now - 86400
        with bs_lock:
            stale_bs = [k for k, v in bootstrap_servers.items() if v["last_seen"] < bs_cut]
            for k in stale_bs:
                del bootstrap_servers[k]

        with rate_lock:
            for rd in (hello_ip_rate,):
                for ip in list(rd.keys()):
                    rd[ip] = [t for t in rd[ip] if now - t < 60]
                    if not rd[ip]:
                        del rd[ip]
            for ip in list(bs_ip_rate.keys()):
                bs_ip_rate[ip] = [t for t in bs_ip_rate[ip] if now - t < 3600]
                if not bs_ip_rate[ip]:
                    del bs_ip_rate[ip]


def _gossip_bootstrap_servers():
    """Propagate known bootstrap servers to each other every 5 minutes."""
    time.sleep(30)
    while True:
        time.sleep(300)
        with bs_lock:
            targets = list(bootstrap_servers.values())
            all_bs  = list(bootstrap_servers.values())
        for target in targets:
            for entry in all_bs:
                if entry["host"] == target["host"] and entry["port"] == target["port"]:
                    continue
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(3.0)
                    s.connect((target["host"], target["port"]))
                    s.sendall(json.dumps({
                        "type": "REGISTER_BOOTSTRAP",
                        "host": entry["host"],
                        "port": entry["port"]
                    }).encode())
                    s.close()
                except Exception:
                    continue


# ── Request handler ────────────────────────────────────────────────────────────

def handle_client(conn, addr):
    try:
        conn.settimeout(10.0)
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > 131072:
                break
        msg = json.loads(data.decode())
        mt  = msg.get("type")
        ip  = addr[0]
        now = time.time()

        # ── HELLO ──────────────────────────────────────────────────────────────
        if mt == "HELLO":
            did  = msg.get("device_id", "")
            port = msg.get("port", PORT)
            ver  = msg.get("version", "0.0")

            if _ver(ver) < _ver(MIN_VERSION):
                conn.sendall(json.dumps({
                    "type":   "VERSION_REJECTED",
                    "reason": f"Minimum version {MIN_VERSION} required. "
                              "Re-download from github.com/EvokiTimpal/timpal"
                }).encode())
                return

            with rate_lock:
                hello_ip_rate.setdefault(ip, [])
                hello_ip_rate[ip] = [t for t in hello_ip_rate[ip] if now - t < 60]
                if len(hello_ip_rate[ip]) >= HELLO_RATE_LIMIT:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit"}).encode())
                    return
                hello_ip_rate[ip].append(now)

            with peers_lock:
                is_new = did not in peers
                if is_new and len(peers) >= 10000:
                    oldest = min(peers, key=lambda k: peers[k]["last_seen"])
                    del peers[oldest]
                if did:
                    peers[did] = {"ip": ip, "port": port, "last_seen": now}
                # Sample peers to return (exclude requester)
                all_peers = [
                    {"device_id": p, "ip": d["ip"], "port": d["port"]}
                    for p, d in peers.items()
                    if p != did
                ]
                sample = random.sample(all_peers, min(HELLO_PEERS_SAMPLE, len(all_peers)))

            conn.sendall(json.dumps({
                "type":  "PEERS",
                "peers": sample
            }).encode())

            if is_new and did:
                print(f"  [+] v{ver} {did[:20]}... from {ip}:{port} | peers={len(peers)}")

        # ── PING ───────────────────────────────────────────────────────────────
        elif mt == "PING":
            did = msg.get("device_id", "")
            with peers_lock:
                if did and did in peers:
                    peers[did]["last_seen"] = now
                count = len(peers)
            conn.sendall(json.dumps({
                "type":       "PONG",
                "peer_count": count
            }).encode())

        # ── GET_PEERS ──────────────────────────────────────────────────────────
        elif mt == "GET_PEERS":
            with peers_lock:
                all_peers = [
                    {"device_id": p, "ip": d["ip"], "port": d["port"]}
                    for p, d in peers.items()
                ]
                sample = random.sample(all_peers, min(HELLO_PEERS_SAMPLE, len(all_peers)))
            conn.sendall(json.dumps({
                "type":  "PEERS",
                "peers": sample
            }).encode())

        # ── REGISTER_BOOTSTRAP ─────────────────────────────────────────────────
        elif mt == "REGISTER_BOOTSTRAP":
            h = msg.get("host", "").strip()
            p = msg.get("port", 0)
            if not h or not isinstance(p, int) or not (1024 <= p <= 65535):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid"}).encode())
                return
            if len(h) > 253:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid host"}).encode())
                return

            # FIX 6: Resolve known server hostnames OUTSIDE bs_lock to avoid
            # blocking DNS calls while holding the lock (could stall all requests
            # for seconds when DNS is slow or BS_MAX_SERVERS entries are present).
            with bs_lock:
                known_hosts = [v["host"] for v in bootstrap_servers.values()]
            known_ips = set()
            for host in known_hosts:
                try:
                    known_ips.add(socket.gethostbyname(host))
                except Exception:
                    known_ips.add(host)

            # Rate limit (skip if already registered from same IP)
            if ip not in known_ips:
                with rate_lock:
                    bs_ip_rate.setdefault(ip, [])
                    bs_ip_rate[ip] = [t for t in bs_ip_rate[ip] if now - t < 3600]
                    if len(bs_ip_rate[ip]) >= BS_RATE_LIMIT:
                        conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit"}).encode())
                        return
                    bs_ip_rate[ip].append(now)

            # Verify reachability
            key = f"{h}:{p}"
            try:
                pr = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                pr.settimeout(3.0)
                pr.connect((h, p))
                pr.close()
            except Exception:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "not reachable"}).encode())
                return

            with bs_lock:
                if key not in bootstrap_servers:
                    if len(bootstrap_servers) >= BS_MAX_SERVERS:
                        oldest = min(bootstrap_servers, key=lambda k: bootstrap_servers[k]["last_seen"])
                        del bootstrap_servers[oldest]
                    bootstrap_servers[key] = {"host": h, "port": p, "last_seen": now}
                    print(f"  [+] Bootstrap registered: {key} | total={len(bootstrap_servers)}")
                else:
                    bootstrap_servers[key]["last_seen"] = now
            conn.sendall(json.dumps({"type": "REGISTER_ACK", "key": key}).encode())

        # ── GET_BOOTSTRAP_SERVERS ──────────────────────────────────────────────
        elif mt == "GET_BOOTSTRAP_SERVERS":
            with bs_lock:
                bl = [{"host": v["host"], "port": v["port"]}
                      for v in bootstrap_servers.values()]
            conn.sendall(json.dumps({
                "type":    "BOOTSTRAP_SERVERS_RESPONSE",
                "servers": bl
            }).encode())

        # ── Unknown ────────────────────────────────────────────────────────────
        else:
            # Silently ignore all deleted message types
            # (SUBMIT_COMMIT, SUBMIT_REVEAL, GET_COMMITS, GET_REVEALS,
            #  SUBMIT_TIP, GET_CHECKPOINT_TIP, etc.)
            pass

    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    _check_genesis_time()
    print("=" * 54)
    print("  TIMPAL Bootstrap Server v4.0 — Peer Discovery Only")
    print("=" * 54)
    print(f"  Port: {PORT} | Min version: {MIN_VERSION}")
    print(f"  Role: Peer directory. Zero consensus authority.")
    print("=" * 54 + "\n")

    for fn in (_clean_old_data, _gossip_bootstrap_servers):
        threading.Thread(target=fn, daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", PORT))
    srv.listen(200)
    print("  Ready. Waiting for nodes...\n")

    sem = threading.Semaphore(200)
    def _wrap(conn, addr):
        try:
            handle_client(conn, addr)
        finally:
            sem.release()

    while True:
        try:
            conn, addr = srv.accept()
            if not sem.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=_wrap, args=(conn, addr), daemon=True).start()
        except KeyboardInterrupt:
            print("\n  Shutting down.")
            break
        except Exception:
            continue


if __name__ == "__main__":
    main()
