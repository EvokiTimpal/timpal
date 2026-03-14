#!/usr/bin/env python3
"""
TIMPAL Bootstrap Server v2.1
------------------------------
Peer discovery + message relay for CGNAT nodes.
No lottery coordination — fully decentralized.
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

peers      = {}  # device_id -> {ip, port, last_seen}
peers_lock = threading.Lock()


def clean_old_peers():
    while True:
        time.sleep(60)
        cutoff = time.time() - 300
        with peers_lock:
            stale = [pid for pid, p in peers.items() if p["last_seen"] < cutoff]
            for pid in stale:
                del peers[pid]
            if stale:
                print(f"  Cleaned {len(stale)} stale peers. Active: {len(peers)}")


def relay_to_all(msg: dict, exclude_id: str = None):
    """Relay a message to all known peers except sender.
    Used for VRF_COMMIT and VRF_REVEAL so CGNAT nodes participate."""
    msg_bytes = json.dumps(msg).encode()
    with peers_lock:
        peer_list = [
            (pid, p["ip"], p["port"])
            for pid, p in peers.items()
            if pid != exclude_id
        ]
    for pid, ip, port in peer_list:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((ip, port))
            s.sendall(msg_bytes)
            s.close()
        except Exception:
            continue


def handle_client(conn, addr):
    try:
        conn.settimeout(10.0)
        data = b""
        while True:
            chunk = conn.recv(8192)
            if not chunk:
                break
            data += chunk
            if len(data) > 65536:
                break

        msg      = json.loads(data.decode())
        msg_type = msg.get("type")

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

        elif msg_type in ("VRF_COMMIT", "VRF_REVEAL"):
            # Relay to all peers for CGNAT support
            device_id = msg.get("device_id", "")
            slot      = msg.get("slot")
            with peers_lock:
                if device_id in peers:
                    peers[device_id]["last_seen"] = time.time()
            threading.Thread(
                target=relay_to_all,
                args=(msg, device_id),
                daemon=True
            ).start()

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
    print("  Peer Discovery + Message Relay")
    print("  No lottery coordination — fully decentralized")
    print("=" * 50)
    print(f"  Listening on port {PORT}")
    print(f"  Anyone can run this server.")
    print("=" * 50 + "\n")

    threading.Thread(target=clean_old_peers, daemon=True).start()

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
