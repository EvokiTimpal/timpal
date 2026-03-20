#!/usr/bin/env python3
"""
TIMPAL Bootstrap Server v2.2
------------------------------
Peer discovery + commit/reveal registry for decentralized lottery.

New in this version:
  - Eligibility-gated lottery: only ~10 nodes participate per slot
    regardless of network size. Sybil attacks become economically irrational.
  - Collective target: the winning number is unknown until all reveals are in.
    No node can predict or cherry-pick the outcome.
  - Reveal obligation: nodes that commit but do not reveal are banned for
    K=10 slots. Selective reveal advantage is eliminated.
  - Smoothed network size: rolling average prevents threshold jumping on
    single connect/disconnect events.

Cannot cheat — every commit/reveal is cryptographically verified by nodes.
Anyone can run this. The more servers, the better.

Run:
    python3 bootstrap.py
"""

import socket
import threading
import json
import time
import random
import hashlib

PORT        = 7777
VERSION     = "2.2"
MIN_VERSION = "2.2"

# ── GENESIS_TIME ──────────────────────────────────────────────────────────────
# Must match timpal.py exactly.
GENESIS_TIME = 0   # ← REPLACE with the same number as in timpal.py

REWARD_INTERVAL       = 5.0
TARGET_PARTICIPANTS   = 10   # Target eligible nodes per slot
BAN_DURATION          = 10   # Slots banned after reveal obligation failure
REVEAL_MISS_THRESHOLD = 2    # Missed reveals before ban (1 grace for network hiccups)
NETWORK_SIZE_SAMPLES  = 10   # Slots of history for rolling average peer count


def _check_genesis_time():
    if GENESIS_TIME == 0:
        print("\n  " + "=" * 50)
        print("  ERROR: GENESIS_TIME is not set.")
        print("  " + "=" * 50)
        print("  Set GENESIS_TIME in bootstrap.py to the")
        print("  same value as in timpal.py, then restart.")
        print("")
        print("  Run: python3 -c \"import time; print(int(time.time()))\"")
        print("  Paste the result into both files.")
        print("  " + "=" * 50 + "\n")
        exit(1)


def get_current_slot() -> int:
    return int((time.time() - GENESIS_TIME) / REWARD_INTERVAL)


def _ver(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0, 0)


# ── Eligibility ───────────────────────────────────────────────────────────────

def get_eligibility_threshold(network_size: int) -> float:
    """Returns T in [0.0, 1.0].
    A node is eligible for a slot if:
        int(sha256(f'{device_id}:{slot}'), 16) < T * 2**256
    Targets TARGET_PARTICIPANTS nodes per slot regardless of network size.
    Both bootstrap and timpal.py use the IDENTICAL formula and string format."""
    if network_size <= TARGET_PARTICIPANTS:
        return 1.0
    return TARGET_PARTICIPANTS / network_size


def is_eligible(device_id: str, slot: int, network_size: int) -> bool:
    """True if device_id is eligible to commit for this slot."""
    threshold = get_eligibility_threshold(network_size)
    if threshold >= 1.0:
        return True
    h = int(hashlib.sha256(f"{device_id}:{slot}".encode()).hexdigest(), 16)
    return h < int(threshold * (2 ** 256))


# ── Collective target ─────────────────────────────────────────────────────────

def compute_collective_target(slot_reveals: dict) -> str:
    """Compute the collective target from all reveals for a slot.
    Target = sha256 of all tickets sorted and joined with ':'.
    This value does not exist until the reveal window closes.
    No node can predict it in advance. Winner = closest ticket to target."""
    tickets = sorted(r["ticket"] for r in slot_reveals.values())
    return hashlib.sha256(":".join(tickets).encode()).hexdigest()


# ── State ─────────────────────────────────────────────────────────────────────

peers          = {}   # device_id -> {ip, port, last_seen}
commits        = {}   # slot -> {device_id -> commit_hash}
reveals        = {}   # slot -> {device_id -> {ticket, sig, seed, public_key}}
missed_reveals = {}   # device_id -> count of missed reveals
ban_until      = {}   # device_id -> slot until which banned

peers_lock   = threading.Lock()
lottery_lock = threading.Lock()
rate_lock    = threading.Lock()

# Rolling average of peer count for smooth eligibility threshold
_peer_count_history      = []
_peer_count_history_lock = threading.Lock()

commit_ip_rate = {}   # ip -> {slot -> count}
reveal_ip_rate = {}   # ip -> {slot -> count}
hello_ip_rate  = {}   # ip -> [timestamps]
bs_ip_rate     = {}   # ip -> [timestamps]

bootstrap_servers      = {}
bootstrap_servers_lock = threading.Lock()

COMMIT_RATE_LIMIT  = 3
REVEAL_RATE_LIMIT  = 3
HELLO_RATE_LIMIT   = 10
BS_RATE_LIMIT      = 5
BS_MAX_SERVERS     = 100
HELLO_PEERS_SAMPLE = 50


# ── Network size helpers ───────────────────────────────────────────────────────

def get_smoothed_network_size() -> int:
    """Rolling average of peer count over last NETWORK_SIZE_SAMPLES slots.
    Prevents eligibility threshold from jumping on single connect/disconnect."""
    with _peer_count_history_lock:
        if not _peer_count_history:
            with peers_lock:
                return max(1, len(peers))
        return max(1, int(sum(_peer_count_history) / len(_peer_count_history)))


def _record_network_size():
    """Sample peer count every slot for rolling average."""
    while True:
        time.sleep(REWARD_INTERVAL)
        with peers_lock:
            n = len(peers)
        with _peer_count_history_lock:
            _peer_count_history.append(n)
            if len(_peer_count_history) > NETWORK_SIZE_SAMPLES:
                _peer_count_history.pop(0)


# ── Reveal obligation enforcement ──────────────────────────────────────────────

def _check_missed_reveals():
    """After slot S+2, find nodes that committed but did not reveal for slot S.
    After REVEAL_MISS_THRESHOLD misses, ban from committing for BAN_DURATION slots.
    One grace slot is given for legitimate network hiccups."""
    last_checked_slot = -1
    while True:
        time.sleep(1.0)
        current_slot = get_current_slot()
        check_slot   = current_slot - 2
        if check_slot <= 0 or check_slot == last_checked_slot:
            continue
        last_checked_slot = check_slot
        with lottery_lock:
            if check_slot not in commits:
                continue
            slot_commits  = set(commits[check_slot].keys())
            slot_reveals  = set(reveals.get(check_slot, {}).keys())
            missed        = slot_commits - slot_reveals
            for device_id in missed:
                missed_reveals[device_id] = missed_reveals.get(device_id, 0) + 1
                count = missed_reveals[device_id]
                if count >= REVEAL_MISS_THRESHOLD:
                    ban_until[device_id] = current_slot + BAN_DURATION
                    missed_reveals[device_id] = 0   # reset count after ban
                    print(f"  [!] Reveal ban: {device_id[:20]}... "
                          f"until slot {ban_until[device_id]} "
                          f"(missed {count} consecutive reveals)")


# ── Cleanup ────────────────────────────────────────────────────────────────────

def clean_old_data():
    """Purge stale peers, old slot data, expired bans, rate limit entries."""
    while True:
        time.sleep(60)
        now          = time.time()
        cutoff       = now - 300
        current_slot = get_current_slot()

        with peers_lock:
            stale = [pid for pid, p in peers.items() if p["last_seen"] < cutoff]
            for pid in stale:
                del peers[pid]
            if stale:
                print(f"  Cleaned {len(stale)} stale peers. Active: {len(peers)}")

        with lottery_lock:
            # Old slot commit/reveal data
            for d in (commits, reveals):
                old = [s for s in list(d) if s < current_slot - 20]
                for s in old:
                    del d[s]
            # Expired bans and associated miss counts.
            # Prune as soon as the ban slot has passed — no extra delay.
            expired = [did for did, slot in list(ban_until.items())
                       if slot < current_slot]
            for did in expired:
                del ban_until[did]
                missed_reveals.pop(did, None)

        with rate_lock:
            for rate_dict in (commit_ip_rate, reveal_ip_rate):
                for ip_key in list(rate_dict.keys()):
                    old_slots = [s for s in list(rate_dict[ip_key].keys())
                                 if s < current_slot - 20]
                    for s in old_slots:
                        del rate_dict[ip_key][s]
                    if not rate_dict[ip_key]:
                        del rate_dict[ip_key]
            for ip_key in list(hello_ip_rate.keys()):
                hello_ip_rate[ip_key] = [t for t in hello_ip_rate[ip_key]
                                         if now - t < 60]
                if not hello_ip_rate[ip_key]:
                    del hello_ip_rate[ip_key]

        bs_cutoff = now - 86400
        with bootstrap_servers_lock:
            stale_bs = [k for k, v in bootstrap_servers.items()
                        if v["last_seen"] < bs_cutoff]
            for k in stale_bs:
                del bootstrap_servers[k]
            if stale_bs:
                print(f"  Cleaned {len(stale_bs)} stale bootstrap servers. "
                      f"Active: {len(bootstrap_servers)}")
            for ip_key in list(bs_ip_rate.keys()):
                bs_ip_rate[ip_key] = [t for t in bs_ip_rate[ip_key]
                                      if now - t < 3600]
                if not bs_ip_rate[ip_key]:
                    del bs_ip_rate[ip_key]


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

        msg      = json.loads(data.decode())
        msg_type = msg.get("type")
        ip       = addr[0]

        # ── Peer registration ─────────────────────────────────────────────────
        if msg_type == "HELLO":
            device_id    = msg.get("device_id", "")
            port         = msg.get("port", PORT)
            node_version = msg.get("version", "0.0")

            if _ver(node_version) < _ver(MIN_VERSION):
                conn.sendall(json.dumps({
                    "type":   "VERSION_REJECTED",
                    "reason": (f"Your version ({node_version}) is below minimum "
                               f"({MIN_VERSION}). "
                               f"Delete ~/.timpal_wallet.json and ~/.timpal_ledger.json, "
                               f"then update from: https://github.com/EvokiTimpal/timpal")
                }).encode())
                print(f"  [!] Rejected old node v{node_version}: "
                      f"{device_id[:20]}... from {ip}")
                return

            now_hello = time.time()
            with rate_lock:
                hello_ip_rate.setdefault(ip, [])
                hello_ip_rate[ip] = [t for t in hello_ip_rate[ip]
                                     if now_hello - t < 60]
                if len(hello_ip_rate[ip]) >= HELLO_RATE_LIMIT:
                    conn.sendall(json.dumps({
                        "type": "ERROR", "msg": "rate limit exceeded"
                    }).encode())
                    return
                hello_ip_rate[ip].append(now_hello)

            network_size = get_smoothed_network_size()

            with peers_lock:
                is_new = device_id not in peers
                if is_new and len(peers) >= 10000:
                    oldest = min(peers, key=lambda k: peers[k]["last_seen"])
                    del peers[oldest]
                peers[device_id] = {
                    "ip":        ip,
                    "port":      port,
                    "last_seen": time.time()
                }
                all_peers = [
                    {"device_id": pid, "ip": p["ip"], "port": p["port"]}
                    for pid, p in peers.items()
                    if pid != device_id
                ]
                peer_list = random.sample(
                    all_peers, min(HELLO_PEERS_SAMPLE, len(all_peers))
                )

            conn.sendall(json.dumps({
                "type":         "PEERS",
                "peers":        peer_list,
                "network_size": network_size
            }).encode())

            if is_new:
                print(f"  [+] New node v{node_version}: {device_id[:20]}... "
                      f"from {ip}:{port} | Total: {len(peers)} | "
                      f"Network size (smoothed): {network_size}")

        # ── Commit submission ─────────────────────────────────────────────────
        elif msg_type == "SUBMIT_COMMIT":
            device_id = msg.get("device_id", "")
            slot      = msg.get("slot")
            commit    = msg.get("commit", "")

            # Field validation
            if not all([device_id, slot is not None, commit]):
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "missing fields"
                }).encode())
                return
            if not isinstance(slot, int) or slot < 0:
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "invalid slot"
                }).encode())
                return
            if (len(device_id) != 64 or
                    not all(c in "0123456789abcdef" for c in device_id.lower())):
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "invalid device_id"
                }).encode())
                return
            if (len(commit) != 64 or
                    not all(c in "0123456789abcdef" for c in commit.lower())):
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "invalid commit"
                }).encode())
                return

            current_slot = get_current_slot()
            if abs(slot - current_slot) > 2:
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "stale slot"
                }).encode())
                return

            # Check reveal obligation ban BEFORE eligibility
            with lottery_lock:
                banned_until_slot = ban_until.get(device_id, 0)
            if banned_until_slot >= current_slot:
                conn.sendall(json.dumps({
                    "type":      "COMMIT_REJECTED",
                    "reason":    "reveal obligation ban",
                    "ban_until": banned_until_slot
                }).encode())
                return

            # Check eligibility for this slot
            network_size = get_smoothed_network_size()
            if not is_eligible(device_id, slot, network_size):
                conn.sendall(json.dumps({
                    "type":   "COMMIT_REJECTED",
                    "reason": "not eligible this slot"
                }).encode())
                return

            # Per-IP rate limit
            with rate_lock:
                commit_ip_rate.setdefault(ip, {})
                commit_ip_rate[ip].setdefault(slot, 0)
                if commit_ip_rate[ip][slot] >= COMMIT_RATE_LIMIT:
                    conn.sendall(json.dumps({
                        "type": "ERROR", "msg": "rate limit exceeded"
                    }).encode())
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
                    print(f"  [slot {slot}] Commit: {device_id[:20]}... "
                          f"({len(commits[slot])} committed) "
                          f"| net: {network_size}")

            conn.sendall(json.dumps({
                "type":         "COMMIT_ACK",
                "slot":         slot,
                "network_size": network_size
            }).encode())

        # ── Reveal submission ─────────────────────────────────────────────────
        elif msg_type == "SUBMIT_REVEAL":
            device_id  = msg.get("device_id", "")
            slot       = msg.get("slot")
            ticket     = msg.get("ticket", "")
            sig        = msg.get("sig", "")
            seed       = msg.get("seed", "")
            public_key = msg.get("public_key", "")

            # Field validation
            if not all([device_id, slot is not None, ticket, sig, seed, public_key]):
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "missing fields"
                }).encode())
                return
            if not isinstance(slot, int) or slot < 0:
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "invalid slot"
                }).encode())
                return
            if (len(device_id) != 64 or
                    not all(c in "0123456789abcdef" for c in device_id.lower())):
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "invalid device_id"
                }).encode())
                return
            if (len(ticket) != 64 or
                    not all(c in "0123456789abcdef" for c in ticket.lower())):
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "invalid ticket"
                }).encode())
                return
            # Seed must equal str(slot) — any other value is invalid
            if not isinstance(seed, str) or seed != str(slot):
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "invalid seed"
                }).encode())
                return
            if len(public_key) > 8192 or len(sig) > 8192:
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "payload too large"
                }).encode())
                return

            current_slot = get_current_slot()
            if abs(slot - current_slot) > 2:
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "stale slot"
                }).encode())
                return

            # Per-IP rate limit
            with rate_lock:
                reveal_ip_rate.setdefault(ip, {})
                reveal_ip_rate[ip].setdefault(slot, 0)
                if reveal_ip_rate[ip][slot] >= REVEAL_RATE_LIMIT:
                    conn.sendall(json.dumps({
                        "type": "ERROR", "msg": "rate limit exceeded"
                    }).encode())
                    return
                reveal_ip_rate[ip][slot] += 1

            with peers_lock:
                if device_id in peers:
                    peers[device_id]["last_seen"] = time.time()

            with lottery_lock:
                # Only accept reveal if a commit exists for this device_id and slot.
                # Reveal without a prior commit is silently ignored.
                #
                # Note: bootstrap does NOT verify the Dilithium3 VRF signature here.
                # Bootstrap is a relay, not a consensus participant. All nodes
                # independently verify every reveal in _pick_winner() before accepting
                # any winner. Verifying signatures here would require importing
                # dilithium-py on every bootstrap server — an operational burden that
                # adds no security because nodes verify anyway.
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
                        print(f"  [slot {slot}] Reveal:  {device_id[:20]}... "
                              f"({len(reveals[slot])} revealed)")

            conn.sendall(json.dumps({"type": "REVEAL_ACK", "slot": slot}).encode())

        # ── Commit query ──────────────────────────────────────────────────────
        elif msg_type == "GET_COMMITS":
            slot = msg.get("slot")
            if slot is None:
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "missing slot"
                }).encode())
                return
            with lottery_lock:
                slot_commits = dict(commits.get(slot, {}))
            conn.sendall(json.dumps({
                "type":    "COMMITS_RESPONSE",
                "slot":    slot,
                "commits": slot_commits
            }).encode())

        # ── Reveal query — includes collective target ──────────────────────────
        elif msg_type == "GET_REVEALS":
            slot = msg.get("slot")
            if slot is None:
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "missing slot"
                }).encode())
                return
            with lottery_lock:
                slot_reveals = dict(reveals.get(slot, {}))

            # Collective target: sha256 of all tickets sorted and joined.
            # Only meaningful once reveals exist.
            # Nodes independently verify this from the reveals they receive.
            collective_target = None
            if slot_reveals:
                collective_target = compute_collective_target(slot_reveals)

            conn.sendall(json.dumps({
                "type":              "REVEALS_RESPONSE",
                "slot":              slot,
                "reveals":           slot_reveals,
                "collective_target": collective_target
            }).encode())

        # ── Keepalive ─────────────────────────────────────────────────────────
        elif msg_type == "PING":
            device_id = msg.get("device_id", "")
            with peers_lock:
                if device_id in peers:
                    peers[device_id]["last_seen"] = time.time()
            conn.sendall(json.dumps({
                "type":         "PONG",
                "network_size": get_smoothed_network_size()
            }).encode())

        # ── Peer list query (device_ids only — no IPs exposed) ────────────────
        elif msg_type == "GET_PEERS":
            with peers_lock:
                peer_list = [{"device_id": pid} for pid in peers]
            conn.sendall(json.dumps({
                "type":  "PEERS",
                "peers": peer_list
            }).encode())

        # ── Bootstrap server registration ─────────────────────────────────────
        elif msg_type == "REGISTER_BOOTSTRAP":
            bs_host = msg.get("host", "").strip()
            bs_port = msg.get("port", 0)

            if (not bs_host or
                    not isinstance(bs_port, int) or
                    not (1024 <= bs_port <= 65535)):
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "invalid host or port"
                }).encode())
                return
            if len(bs_host) > 253:
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "invalid host"
                }).encode())
                return

            now = time.time()

            # Rate limit new registrations per IP (unless already a known server)
            known_bs_ips = set()
            with bootstrap_servers_lock:
                bs_hosts_list = [v["host"] for v in bootstrap_servers.values()]
            for host in bs_hosts_list:
                try:
                    known_bs_ips.add(socket.gethostbyname(host))
                except Exception:
                    known_bs_ips.add(host)

            if ip not in known_bs_ips:
                with bootstrap_servers_lock:
                    bs_ip_rate.setdefault(ip, [])
                    bs_ip_rate[ip] = [t for t in bs_ip_rate[ip] if now - t < 3600]
                    if len(bs_ip_rate[ip]) >= BS_RATE_LIMIT:
                        conn.sendall(json.dumps({
                            "type": "ERROR", "msg": "rate limit exceeded"
                        }).encode())
                        return
                    bs_ip_rate[ip].append(now)

            # Verify the server is actually reachable
            key = f"{bs_host}:{bs_port}"
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                probe.settimeout(3.0)
                probe.connect((bs_host, bs_port))
                probe.close()
            except Exception:
                conn.sendall(json.dumps({
                    "type": "ERROR", "msg": "server not reachable"
                }).encode())
                return

            with bootstrap_servers_lock:
                if key not in bootstrap_servers:
                    if len(bootstrap_servers) >= BS_MAX_SERVERS:
                        oldest = min(bootstrap_servers,
                                     key=lambda k: bootstrap_servers[k]["last_seen"])
                        del bootstrap_servers[oldest]
                    bootstrap_servers[key] = {
                        "host":      bs_host,
                        "port":      bs_port,
                        "last_seen": now
                    }
                    print(f"  [+] Bootstrap registered: {key} | "
                          f"Total: {len(bootstrap_servers)}")
                else:
                    bootstrap_servers[key]["last_seen"] = now

            conn.sendall(json.dumps({"type": "REGISTER_ACK", "key": key}).encode())

            def _gossip_new_bs(h, p):
                with bootstrap_servers_lock:
                    targets = [
                        (v["host"], v["port"])
                        for k, v in bootstrap_servers.items()
                        if k != f"{h}:{p}"
                    ]
                for t_host, t_port in targets:
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(3.0)
                        s.connect((t_host, t_port))
                        s.sendall(json.dumps({
                            "type": "REGISTER_BOOTSTRAP",
                            "host": h,
                            "port": p
                        }).encode())
                        s.close()
                    except Exception:
                        continue

            threading.Thread(
                target=_gossip_new_bs,
                args=(bs_host, bs_port),
                daemon=True
            ).start()

        # ── Bootstrap server list query ────────────────────────────────────────
        elif msg_type == "GET_BOOTSTRAP_SERVERS":
            now_bs = time.time()
            with rate_lock:
                hello_ip_rate.setdefault(ip, [])
                hello_ip_rate[ip] = [t for t in hello_ip_rate[ip] if now_bs - t < 60]
                if len(hello_ip_rate[ip]) >= HELLO_RATE_LIMIT:
                    conn.sendall(json.dumps({
                        "type": "ERROR", "msg": "rate limit exceeded"
                    }).encode())
                    return
                hello_ip_rate[ip].append(now_bs)
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


# ── Bootstrap server gossip ────────────────────────────────────────────────────

def _gossip_bootstrap_servers():
    time.sleep(30)
    while True:
        time.sleep(300)
        with bootstrap_servers_lock:
            targets  = list(bootstrap_servers.values())
            our_list = list(bootstrap_servers.values())
        for target in targets:
            for entry in our_list:
                if (entry["host"] == target["host"] and
                        entry["port"] == target["port"]):
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    _check_genesis_time()

    print("=" * 54)
    print("  TIMPAL Bootstrap Server v2.2")
    print("  Peer Discovery + Eligibility-Gated Lottery")
    print("=" * 54)
    print(f"  Port              : {PORT}")
    print(f"  Min node version  : {MIN_VERSION}")
    print(f"  Target per slot   : {TARGET_PARTICIPANTS} eligible nodes")
    print(f"  Reveal miss limit : {REVEAL_MISS_THRESHOLD} before ban")
    print(f"  Ban duration      : {BAN_DURATION} slots")
    print(f"  Network smoothing : {NETWORK_SIZE_SAMPLES}-slot rolling average")
    print(f"  Anyone can run this server.")
    print("=" * 54 + "\n")

    threading.Thread(target=clean_old_data,            daemon=True).start()
    threading.Thread(target=_gossip_bootstrap_servers, daemon=True).start()
    threading.Thread(target=_record_network_size,      daemon=True).start()
    threading.Thread(target=_check_missed_reveals,     daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("", PORT))
    server.listen(200)
    print(f"  Ready. Waiting for nodes...\n")

    _conn_sem = threading.Semaphore(200)

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
