#!/usr/bin/env python3
"""
TIMPAL Bootstrap Server v2.1
------------------------------
Peer discovery + commit/reveal registry for decentralized lottery.
Cannot cheat — every commit/reveal is cryptographically verified by nodes.
Anyone can run this. The more servers, the better.

Run:
    python3 bootstrap.py
"""

import socket
import threading
import json
import time

PORT        = 7777
VERSION     = "2.1"
MIN_VERSION = "2.1"   # Minimum node version allowed to connect

def _ver(v: str) -> tuple:
    """Parse version string into comparable tuple. e.g. '2.1' -> (2, 1)"""
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0, 0)

peers      = {}   # device_id -> {ip, port, last_seen}
commits    = {}   # slot -> {device_id: commit_hash}
reveals    = {}   # slot -> {device_id: {ticket,sig,seed,public_key}}
peers_lock   = threading.Lock()
lottery_lock = threading.Lock()
rate_lock    = threading.Lock()
commit_ip_rate = {}   # ip -> {slot -> count} — commit rate limiting per IP per slot
reveal_ip_rate = {}   # ip -> {slot -> count} — reveal rate limiting per IP per slot
bootstrap_servers      = {}   # "host:port" -> {host, port, last_seen}
bootstrap_servers_lock = threading.Lock()
bs_ip_rate             = {}   # ip -> last_register_times list — rate limiting per IP
BS_RATE_LIMIT          = 5    # Max REGISTER_BOOTSTRAP per IP per hour
BS_MAX_SERVERS         = 100  # Max bootstrap servers stored
COMMIT_RATE_LIMIT = 3   # Max commits per IP per slot (3 nodes per household)
REVEAL_RATE_LIMIT = 3   # Max reveals per IP per slot (3 nodes per household)
hello_ip_rate  = {}   # ip -> [timestamps] — HELLO rate limiting per IP per minute
HELLO_RATE_LIMIT = 10  # Max HELLO registrations per IP per minute
HELLO_PEERS_SAMPLE = 50  # Max peers returned in HELLO response


def clean_old_data():
    while True:
        time.sleep(60)
        now = time.time()
        # Clean stale peers
        cutoff = now - 300
        with peers_lock:
            stale = [pid for pid, p in peers.items() if p["last_seen"] < cutoff]
            for pid in stale:
                del peers[pid]
            if stale:
                print(f"  Cleaned {len(stale)} stale peers. Active: {len(peers)}")
        # Clean old slot data (keep last 20 slots)
        current_slot = int(now / 5.0)
        with lottery_lock:
            for d in (commits, reveals):
                old = [s for s in list(d) if s < current_slot - 20]
                for s in old:
                    del d[s]
        # Clean old rate limit data
        with rate_lock:
            for rate_dict in (commit_ip_rate, reveal_ip_rate):
                for ip_key in list(rate_dict.keys()):
                    old_slots = [s for s in list(rate_dict[ip_key].keys()) if s < current_slot - 20]
                    for s in old_slots:
                        del rate_dict[ip_key][s]
                    if not rate_dict[ip_key]:
                        del rate_dict[ip_key]
        # Clean stale bootstrap servers — prune if not seen for 24 hours
        bs_cutoff = now - 86400
        with bootstrap_servers_lock:
            stale_bs = [k for k, v in bootstrap_servers.items() if v["last_seen"] < bs_cutoff]
            for k in stale_bs:
                del bootstrap_servers[k]
            if stale_bs:
                print(f"  Cleaned {len(stale_bs)} stale bootstrap servers. Active: {len(bootstrap_servers)}")
        # Clean stale hello_ip_rate entries
        now_clean = time.time()
        with rate_lock:
            for ip_key in list(hello_ip_rate.keys()):
                hello_ip_rate[ip_key] = [t for t in hello_ip_rate[ip_key] if now_clean - t < 60]
                if not hello_ip_rate[ip_key]:
                    del hello_ip_rate[ip_key]
        # Clean stale bs_ip_rate entries
        with bootstrap_servers_lock:
            for ip_key in list(bs_ip_rate.keys()):
                bs_ip_rate[ip_key] = [t for t in bs_ip_rate[ip_key] if now - t < 3600]
                if not bs_ip_rate[ip_key]:
                    del bs_ip_rate[ip_key]


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

        msg      = json.loads(data.decode())
        msg_type = msg.get("type")
        ip       = addr[0]

        # ── Peer registration ────────────────────────────────────────────
        if msg_type == "HELLO":
            device_id   = msg.get("device_id", "")
            port        = msg.get("port", PORT)
            node_version = msg.get("version", "0.0")
            if _ver(node_version) < _ver(MIN_VERSION):
                conn.sendall(json.dumps({
                    "type":   "VERSION_REJECTED",
                    "reason": f"Your version ({node_version}) is below minimum ({MIN_VERSION}). Update from: https://github.com/EvokiTimpal/timpal"
                }).encode())
                print(f"  [!] Rejected old node v{node_version}: {device_id[:20]}... from {ip}")
                return
            # Rate limit — max HELLO_RATE_LIMIT registrations per IP per minute
            now_hello = time.time()
            with rate_lock:
                hello_ip_rate.setdefault(ip, [])
                hello_ip_rate[ip] = [t for t in hello_ip_rate[ip] if now_hello - t < 60]
                if len(hello_ip_rate[ip]) >= HELLO_RATE_LIMIT:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit exceeded"}).encode())
                    return
                hello_ip_rate[ip].append(now_hello)
            with peers_lock:
                is_new = device_id not in peers
                if is_new and len(peers) >= 10000:
                    oldest = min(peers, key=lambda k: peers[k]["last_seen"])
                    del peers[oldest]
                peers[device_id] = {"ip": ip, "port": port, "last_seen": time.time()}
                all_peers = [
                    {"device_id": pid, "ip": p["ip"], "port": p["port"]}
                    for pid, p in peers.items()
                    if pid != device_id
                ]
                import random as _random
                peer_list = _random.sample(all_peers, min(HELLO_PEERS_SAMPLE, len(all_peers)))
            conn.sendall(json.dumps({
                "type":         "PEERS",
                "peers":        peer_list,
                "network_size": len(peers)
            }).encode())
            if is_new:
                print(f"  [+] New node v{node_version}: {device_id[:20]}... from {ip}:{port} | Total: {len(peers)}")

        # ── Commit submission ─────────────────────────────────────────────
        elif msg_type == "SUBMIT_COMMIT":
            device_id = msg.get("device_id", "")
            slot      = msg.get("slot")
            commit    = msg.get("commit", "")
            if not all([device_id, slot is not None, commit]):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing fields"}).encode())
                return
            # Validate formats — device_id and commit must be 64-char hex, slot a positive integer
            if not isinstance(slot, int) or slot < 0:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid slot"}).encode())
                return
            if len(device_id) != 64 or not all(c in "0123456789abcdef" for c in device_id.lower()):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid device_id"}).encode())
                return
            if len(commit) != 64 or not all(c in "0123456789abcdef" for c in commit.lower()):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid commit"}).encode())
                return
            # Reject stale or future slots — must be within 2 slots of current
            current_slot = int(time.time() / 5.0)
            if abs(slot - current_slot) > 2:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "stale slot"}).encode())
                return
            # Rate limit — max COMMIT_RATE_LIMIT commits per IP per slot
            with rate_lock:
                commit_ip_rate.setdefault(ip, {})
                commit_ip_rate[ip].setdefault(slot, 0)
                if commit_ip_rate[ip][slot] >= COMMIT_RATE_LIMIT:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit exceeded"}).encode())
                    return
                commit_ip_rate[ip][slot] += 1
            with peers_lock:
                if device_id in peers:
                    peers[device_id]["last_seen"] = time.time()
            with lottery_lock:
                if slot not in commits:
                    commits[slot] = {}
                if device_id not in commits[slot]:
                    commits[slot][device_id] = commit
                    print(f"  [slot {slot}] Commit from {device_id[:20]}... ({len(commits[slot])} total)")
            conn.sendall(json.dumps({"type": "COMMIT_ACK", "slot": slot}).encode())

        # ── Reveal submission ─────────────────────────────────────────────
        elif msg_type == "SUBMIT_REVEAL":
            device_id  = msg.get("device_id", "")
            slot       = msg.get("slot")
            ticket     = msg.get("ticket", "")
            sig        = msg.get("sig", "")
            seed       = msg.get("seed", "")
            public_key = msg.get("public_key", "")
            if not all([device_id, slot is not None, ticket, sig, seed, public_key]):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing fields"}).encode())
                return
            # Validate formats
            if not isinstance(slot, int) or slot < 0:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid slot"}).encode())
                return
            if len(device_id) != 64 or not all(c in "0123456789abcdef" for c in device_id.lower()):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid device_id"}).encode())
                return
            if len(ticket) != 64 or not all(c in "0123456789abcdef" for c in ticket.lower()):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid ticket"}).encode())
                return
            if not isinstance(seed, str) or len(seed) > 64:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid seed"}).encode())
                return
            if len(public_key) > 8192:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid public_key"}).encode())
                return
            if len(sig) > 8192:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid sig"}).encode())
                return
            # Reject stale or future slots — must be within 2 slots of current
            current_slot = int(time.time() / 5.0)
            if abs(slot - current_slot) > 2:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "stale slot"}).encode())
                return
            # Rate limit — max REVEAL_RATE_LIMIT reveals per IP per slot
            with rate_lock:
                reveal_ip_rate.setdefault(ip, {})
                reveal_ip_rate[ip].setdefault(slot, 0)
                if reveal_ip_rate[ip][slot] >= REVEAL_RATE_LIMIT:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit exceeded"}).encode())
                    return
                reveal_ip_rate[ip][slot] += 1
            with peers_lock:
                if device_id in peers:
                    peers[device_id]["last_seen"] = time.time()
            with lottery_lock:
                # Only store reveal if commit exists for this node
                if slot in commits and device_id in commits[slot]:
                    if slot not in reveals:
                        reveals[slot] = {}
                    if device_id not in reveals[slot]:
                        reveals[slot][device_id] = {
                            "ticket":     ticket,
                            "sig":        sig,
                            "seed":       seed,
                            "public_key": public_key
                        }
                        print(f"  [slot {slot}] Reveal from {device_id[:20]}... ({len(reveals[slot])} total)")
            conn.sendall(json.dumps({"type": "REVEAL_ACK", "slot": slot}).encode())

        # ── Commit query ──────────────────────────────────────────────────
        elif msg_type == "GET_COMMITS":
            slot = msg.get("slot")
            if slot is None:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing slot"}).encode())
                return
            with lottery_lock:
                slot_commits = dict(commits.get(slot, {}))
            conn.sendall(json.dumps({
                "type":    "COMMITS_RESPONSE",
                "slot":    slot,
                "commits": slot_commits
            }).encode())

        # ── Reveal query ──────────────────────────────────────────────────
        elif msg_type == "GET_REVEALS":
            slot = msg.get("slot")
            if slot is None:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing slot"}).encode())
                return
            with lottery_lock:
                slot_reveals = dict(reveals.get(slot, {}))
            conn.sendall(json.dumps({
                "type":    "REVEALS_RESPONSE",
                "slot":    slot,
                "reveals": slot_reveals
            }).encode())

        # ── Keepalive ─────────────────────────────────────────────────────
        elif msg_type == "PING":
            device_id = msg.get("device_id", "")
            with peers_lock:
                if device_id in peers:
                    peers[device_id]["last_seen"] = time.time()
            conn.sendall(json.dumps({
                "type":         "PONG",
                "network_size": len(peers)
            }).encode())

        elif msg_type == "GET_PEERS":
            with peers_lock:
                peer_list = [
                    {"device_id": pid}
                    for pid, p in peers.items()
                ]
            conn.sendall(json.dumps({"type": "PEERS", "peers": peer_list}).encode())

        # ── Bootstrap server registration ─────────────────────────────────
        elif msg_type == "REGISTER_BOOTSTRAP":
            bs_host = msg.get("host", "").strip()
            bs_port = msg.get("port", 0)
            # Validate input
            if not bs_host or not isinstance(bs_port, int) or not (1024 <= bs_port <= 65535):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid host or port"}).encode())
                return
            if len(bs_host) > 253:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid host"}).encode())
                return
            # Rate limit — max BS_RATE_LIMIT registrations per IP per hour
            # Skip rate limit for known bootstrap servers gossiping to each other
            now = time.time()
            # Build known_bs_ips without holding the lock — DNS can block for seconds
            known_bs_ips = set()
            with bootstrap_servers_lock:
                bs_hosts = [v["host"] for v in bootstrap_servers.values()]
            for host in bs_hosts:
                try:
                    known_bs_ips.add(socket.gethostbyname(host))
                except Exception:
                    known_bs_ips.add(host)
            if ip not in known_bs_ips:
                with bootstrap_servers_lock:
                    bs_ip_rate.setdefault(ip, [])
                    bs_ip_rate[ip] = [t for t in bs_ip_rate[ip] if now - t < 3600]
                    if len(bs_ip_rate[ip]) >= BS_RATE_LIMIT:
                        conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit exceeded"}).encode())
                        return
                    bs_ip_rate[ip].append(now)
            # Verify server is actually reachable before storing
            key = f"{bs_host}:{bs_port}"
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                probe.settimeout(3.0)
                probe.connect((bs_host, bs_port))
                probe.close()
            except Exception:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "server not reachable"}).encode())
                return
            # Store — cap at BS_MAX_SERVERS, evict oldest if full
            with bootstrap_servers_lock:
                if key not in bootstrap_servers:
                    if len(bootstrap_servers) >= BS_MAX_SERVERS:
                        oldest = min(bootstrap_servers, key=lambda k: bootstrap_servers[k]["last_seen"])
                        del bootstrap_servers[oldest]
                    bootstrap_servers[key] = {"host": bs_host, "port": bs_port, "last_seen": now}
                    print(f"  [+] Bootstrap server registered: {key} | Total: {len(bootstrap_servers)}")
                else:
                    bootstrap_servers[key]["last_seen"] = now
            conn.sendall(json.dumps({"type": "REGISTER_ACK", "key": key}).encode())
            # Gossip to other known bootstrap servers in background
            def _gossip_new_bs(h, p):
                with bootstrap_servers_lock:
                    targets = [(v["host"], v["port"]) for k, v in bootstrap_servers.items() if k != f"{h}:{p}"]
                for t_host, t_port in targets:
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(3.0)
                        s.connect((t_host, t_port))
                        s.sendall(json.dumps({"type": "REGISTER_BOOTSTRAP", "host": h, "port": p}).encode())
                        s.close()
                    except Exception:
                        continue
            threading.Thread(target=_gossip_new_bs, args=(bs_host, bs_port), daemon=True).start()

        # ── Bootstrap server list query ───────────────────────────────────
        elif msg_type == "GET_BOOTSTRAP_SERVERS":
            with bootstrap_servers_lock:
                bs_list = [
                    {"host": v["host"], "port": v["port"]}
                    for v in bootstrap_servers.values()
                ]
            conn.sendall(json.dumps({
                "type":    "BOOTSTRAP_SERVERS_RESPONSE",
                "servers": bs_list
            }).encode())

    except Exception:
        pass
    finally:
        conn.close()


def _gossip_bootstrap_servers():
    """Every 5 minutes, sync bootstrap server list with all known bootstrap servers.
    Sends our full list to every peer bootstrap server so all stay in sync."""
    time.sleep(30)
    while True:
        time.sleep(300)
        with bootstrap_servers_lock:
            targets = list(bootstrap_servers.values())
        for target in targets:
            with bootstrap_servers_lock:
                our_list = list(bootstrap_servers.values())
            for entry in our_list:
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


def main():
    print("=" * 50)
    print("  TIMPAL Bootstrap Server v2.1")
    print("  Peer Discovery + Commit/Reveal Registry")
    print("  Cannot cheat — nodes verify everything")
    print("=" * 50)
    print(f"  Listening on port {PORT}")
    print(f"  Anyone can run this server.")
    print("=" * 50 + "\n")

    threading.Thread(target=clean_old_data, daemon=True).start()
    threading.Thread(target=_gossip_bootstrap_servers, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("", PORT))
    server.listen(100)
    print(f"  Ready. Waiting for nodes...\n")

    _conn_sem = threading.Semaphore(50)
    def _handle_with_sem(conn, addr):
        try:
            handle_client(conn, addr)
        finally:
            _conn_sem.release()

    while True:
        try:
            conn, addr = server.accept()
            if not _conn_sem.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(
                target=_handle_with_sem,
                args=(conn, addr),
                daemon=True
            ).start()
        except KeyboardInterrupt:
            print("\n  Bootstrap server shutting down.")
            break
        except Exception:
            continue


if __name__ == "__main__":
    main()
