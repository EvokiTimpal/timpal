#!/usr/bin/env python3
"""
TIMPAL Bootstrap Server
-----------------------
This is the door to the Timpal network.
It does NOT store value. It does NOT control anything.
It introduces new nodes to existing nodes AND relays VRF tickets
so nodes behind CGNAT can participate in the lottery fairly.

Run this on your Hetzner server:
    python3 bootstrap.py
"""

import socket
import threading
import json
import time

PORT    = 7777
VERSION = "1.0"

peers = {}  # device_id -> {ip, port, last_seen}
lock  = threading.Lock()

def clean_old_peers():
    """Remove peers not seen in the last 5 minutes."""
    while True:
        time.sleep(60)
        cutoff = time.time() - 300
        with lock:
            before = len(peers)
            stale  = [pid for pid, p in peers.items() if p["last_seen"] < cutoff]
            for pid in stale:
                del peers[pid]
            after = len(peers)
        if before != after:
            print(f"  Cleaned {before - after} stale peers. Active: {after}")

def relay_to_all(msg: dict, exclude_id: str = None):
    """Relay a message to all known peers except the sender."""
    msg_bytes = json.dumps(msg).encode()
    with lock:
        peer_list = [(pid, p["ip"], p["port"]) for pid, p in peers.items() if pid != exclude_id]
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
        data = b""
        while True:
            chunk = conn.recv(8192)
            if not chunk:
                break
            data += chunk
            if len(data) > 65536:
                break

        msg = json.loads(data.decode())
        msg_type = msg.get("type")

        if msg_type == "HELLO":
            device_id = msg.get("device_id", "")
            port      = msg.get("port", PORT)
            ip        = addr[0]

            with lock:
                is_new = device_id not in peers
                peers[device_id] = {
                    "ip":        ip,
                    "port":      port,
                    "last_seen": time.time()
                }
                peer_list = [
                    {"device_id": pid, "ip": p["ip"], "port": p["port"]}
                    for pid, p in peers.items()
                    if pid != device_id
                ]

            response = json.dumps({
                "type":         "PEERS",
                "peers":        peer_list,
                "network_size": len(peers)
            }).encode()
            conn.sendall(response)

            if is_new:
                print(f"  [+] New node: {device_id[:20]}... from {ip}:{port} | Total: {len(peers)}")

        elif msg_type == "VRF_TICKET":
            device_id = msg.get("device_id", "")
            with lock:
                if device_id in peers:
                    peers[device_id]["last_seen"] = time.time()
            threading.Thread(
                target=relay_to_all,
                args=(msg, device_id),
                daemon=True
            ).start()

        elif msg_type == "PING":
            device_id = msg.get("device_id", "")
            with lock:
                if device_id in peers:
                    peers[device_id]["last_seen"] = time.time()
            conn.sendall(json.dumps({"type": "PONG", "network_size": len(peers)}).encode())

        elif msg_type == "GET_PEERS":
            with lock:
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
    print("═" * 50)
    print("  TIMPAL Bootstrap Server v1.1")
    print("  Plan B for Humanity")
    print("═" * 50)
    print(f"  Listening on port {PORT}")
    print(f"  VRF ticket relay enabled for CGNAT nodes")
    print("═" * 50 + "\n")

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
