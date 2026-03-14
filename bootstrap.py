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

PORT    = 7777
VERSION = "2.1"

peers      = {}   # device_id -> {ip, port, last_seen}
commits    = {}   # slot -> {device_id: commit_hash}
reveals    = {}   # slot -> {device_id: {ticket,sig,seed,public_key}}
peers_lock   = threading.Lock()
lottery_lock = threading.Lock()


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

        # ── Peer registration ────────────────────────────────────────────
        if msg_type == "HELLO":
            device_id = msg.get("device_id", "")
            port      = msg.get("port", PORT)
            ip        = addr[0]
            with peers_lock:
                is_new = device_id not in peers
                peers[device_id] = {"ip": ip, "port": port, "last_seen": time.time()}
                peer_list = [
                    {"device_id": pid, "ip": p["ip"], "port": p["port"]}
                    for pid, p in peers.items()
                    if pid != device_id
                ]
            conn.sendall(json.dumps({
                "type":         "PEERS",
                "peers":        peer_list,
                "network_size": len(peers)
            }).encode())
            if is_new:
                print(f"  [+] New node: {device_id[:20]}... from {ip}:{port} | Total: {len(peers)}")

        # ── Commit submission ─────────────────────────────────────────────
        elif msg_type == "SUBMIT_COMMIT":
            device_id = msg.get("device_id", "")
            slot      = msg.get("slot")
            commit    = msg.get("commit", "")
            if not all([device_id, slot is not None, commit]):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing fields"}).encode())
                return
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
                    {"device_id": pid, "ip": p["ip"], "port": p["port"]}
                    for pid, p in peers.items()
                ]
            conn.sendall(json.dumps({"type": "PEERS", "peers": peer_list}).encode())

    except Exception:
        pass
    finally:
        conn.close()


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

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("", PORT))
    server.listen(100)
    print(f"  Ready. Waiting for nodes...\n")

    while True:
        try:
            conn, addr = server.accept()
            threading.Thread(
                target=handle_client,
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
