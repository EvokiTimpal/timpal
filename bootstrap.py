#!/usr/bin/env python3
"""
TIMPAL Bootstrap Server v2.0
------------------------------
Lottery coordinator + peer discovery.

- Collects VRF tickets from all nodes per 5-second slot
- Picks lowest-ticket winner 4 seconds into each slot
- Announces winner on GET_WINNER query (nodes poll — CGNAT-friendly)
- Sends TICKET_CONFIRMED receipt so nodes have proof of submission

Run:
    python3 bootstrap.py
"""

import socket
import threading
import json
import time

PORT            = 7777
VERSION         = "2.0"
REWARD_INTERVAL = 5.0   # Must match timpal.py
TICKET_WINDOW   = 3.5   # Seconds after slot start to accept tickets
WINNER_DELAY    = 4.0   # Seconds after slot start before picking winner

peers        = {}   # device_id -> {ip, port, last_seen}
slot_tickets = {}   # slot -> [ticket_entry, ...]
slot_winners = {}   # slot -> winner_dict  (None means no tickets that slot)
peers_lock   = threading.Lock()
tickets_lock = threading.Lock()


# --- Maintenance ---

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
        current_slot = int(time.time() / REWARD_INTERVAL)
        with tickets_lock:
            old = [s for s in list(slot_tickets) if s < current_slot - 20]
            for s in old:
                slot_tickets.pop(s, None)
            old = [s for s in list(slot_winners) if s < current_slot - 20]
            for s in old:
                slot_winners.pop(s, None)


# --- Lottery coordinator ---

def pick_winners():
    while True:
        time.sleep(0.5)
        now = time.time()
        with tickets_lock:
            for slot, tickets in list(slot_tickets.items()):
                if slot in slot_winners:
                    continue
                slot_start = slot * REWARD_INTERVAL
                if now < slot_start + WINNER_DELAY:
                    continue
                if not tickets:
                    slot_winners[slot] = None
                    print(f"  [slot {slot}] No tickets — skipped")
                    continue
                winner = min(tickets, key=lambda e: e["ticket"])
                slot_winners[slot] = winner
                print(f"  [slot {slot}] Winner: {winner['device_id'][:20]}... "
                      f"ticket={winner['ticket'][:16]}... "
                      f"({len(tickets)} node{'s' if len(tickets) != 1 else ''} competed)")


# --- Request handler ---

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

        elif msg_type == "VRF_TICKET":
            device_id  = msg.get("device_id", "")
            slot       = msg.get("slot")
            ticket     = msg.get("ticket", "")
            sig        = msg.get("sig", "")
            seed       = msg.get("seed", "")
            public_key = msg.get("public_key", "")
            port       = msg.get("port", 7779)
            ip         = addr[0]
            now        = time.time()
            if not all([device_id, slot is not None, ticket, sig, seed, public_key]):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing fields"}).encode())
                return
            with peers_lock:
                if device_id in peers:
                    peers[device_id]["last_seen"] = now
                else:
                    peers[device_id] = {"ip": ip, "port": port, "last_seen": now}
            slot_start = slot * REWARD_INTERVAL
            if now > slot_start + TICKET_WINDOW:
                conn.sendall(json.dumps({
                    "type": "TICKET_CONFIRMED", "slot": slot,
                    "ticket": ticket, "timestamp": now, "status": "late"
                }).encode())
                print(f"  [slot {slot}] Late ticket from {device_id[:20]}... (ignored)")
                return
            with tickets_lock:
                if slot not in slot_tickets:
                    slot_tickets[slot] = []
                already = any(e["device_id"] == device_id for e in slot_tickets[slot])
                if not already:
                    slot_tickets[slot].append({
                        "device_id":  device_id,
                        "winner_id":  device_id,
                        "ticket":     ticket,
                        "sig":        sig,
                        "seed":       seed,
                        "public_key": public_key,
                        "ip":         ip,
                        "port":       port,
                        "timestamp":  now
                    })
                    count = len(slot_tickets[slot])
                    print(f"  [slot {slot}] Ticket from {device_id[:20]}... ({count} total)")
            conn.sendall(json.dumps({
                "type": "TICKET_CONFIRMED", "slot": slot,
                "ticket": ticket, "timestamp": now, "status": "accepted"
            }).encode())

        elif msg_type == "GET_WINNER":
            slot = msg.get("slot")
            if slot is None:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing slot"}).encode())
                return
            with tickets_lock:
                winner  = slot_winners.get(slot)
                decided = slot in slot_winners
            if not decided:
                conn.sendall(json.dumps({"type": "WINNER_RESPONSE", "status": "pending", "slot": slot}).encode())
            elif winner is None:
                conn.sendall(json.dumps({"type": "WINNER_RESPONSE", "status": "no_tickets", "slot": slot}).encode())
            else:
                conn.sendall(json.dumps({
                    "type":       "WINNER_RESPONSE",
                    "status":     "winner",
                    "slot":       slot,
                    "winner_id":  winner["device_id"],
                    "ticket":     winner["ticket"],
                    "sig":        winner["sig"],
                    "seed":       winner["seed"],
                    "public_key": winner["public_key"]
                }).encode())

        elif msg_type == "PING":
            device_id = msg.get("device_id", "")
            with peers_lock:
                if device_id in peers:
                    peers[device_id]["last_seen"] = time.time()
            conn.sendall(json.dumps({"type": "PONG", "network_size": len(peers)}).encode())

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


# --- Main ---

def main():
    print("=" * 50)
    print("  TIMPAL Bootstrap Server v2.0")
    print("  Lottery Coordinator + Peer Discovery")
    print("=" * 50)
    print(f"  Listening on port {PORT}")
    print(f"  Ticket window  : first {TICKET_WINDOW}s of each slot")
    print(f"  Winner decided : {WINNER_DELAY}s after slot start")
    print("=" * 50 + "\n")
    threading.Thread(target=clean_old_peers, daemon=True).start()
    threading.Thread(target=pick_winners,    daemon=True).start()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("", PORT))
    server.listen(100)
    print(f"  Ready. Waiting for nodes...\n")
    while True:
        try:
            conn, addr = server.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except KeyboardInterrupt:
            print("\n  Bootstrap server shutting down.")
            break
        except Exception:
            continue

if __name__ == "__main__":
    main()
