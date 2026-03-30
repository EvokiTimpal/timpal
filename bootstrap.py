#!/usr/bin/env python3
"""
TIMPAL Bootstrap Server v3.2

v3.2 fixes over v3.1
────────────────────────────────────────────────────────────────────
  M8  GET_CHECKPOINT_TIP now rate-limited to 10 requests/min/IP.
      Previously the only handler with no rate limit — trivial DoS vector.

  M9  GET_CHECKPOINT_TIP majority_hash tiebreak is now deterministic.
      Old: max(tally, key=lambda h: tally[h])
        → ties broken by dict insertion order (varies per server)
        → two bootstrap servers could return different majority hashes
      Fixed: max(tally, key=lambda h: (tally[h], h))
        → secondary sort on hash string: identical across all servers

  C1  compute_collective_target now uses only commit-verified reveals.
      Bootstrap doesn't run Dilithium3.verify — "verified" here means
      the reveal has a matching commit entry for the same slot.
      (timpal.py v3.2 already ignores the bootstrap collective_target
      after the M7 fix, but this keeps bootstrap internally consistent.)
"""

import socket
import threading
import json
import time
import random
import hashlib

PORT        = 7777
VERSION     = "3.3"
MIN_VERSION = "3.3"

GENESIS_TIME = 1774706400   # ← same value as in timpal.py

REWARD_INTERVAL        = 5.0
TARGET_PARTICIPANTS    = 10
BAN_DURATION           = 10
CHECKPOINT_INTERVAL    = 1000
REVEAL_MISS_THRESHOLD  = 1   # 1 missed reveal triggers ban — closes selective reveal rotation attack
NETWORK_SIZE_SAMPLES   = 10
GENESIS_PREV_HASH      = "0" * 64
MIN_IDENTITY_AGE       = 200  # slots — must match timpal.py (defense-in-depth gate at bootstrap)

# ── Rate limits ────────────────────────────────────────────────────────────────
COMMIT_RATE_LIMIT         = 3    # per slot
REVEAL_RATE_LIMIT         = 3    # per slot
TIP_RATE_LIMIT            = 3    # per slot
HELLO_RATE_LIMIT          = 10   # per 60 seconds
BS_RATE_LIMIT             = 5    # per hour
CHECKPOINT_TIP_RATE_LIMIT = 10   # M8 FIX: per 60 seconds

BS_MAX_SERVERS     = 100
HELLO_PEERS_SAMPLE = 50


def _check_genesis_time():
    if GENESIS_TIME == 0:
        print("\n  " + "=" * 50)
        print("  ERROR: GENESIS_TIME is not set.")
        print("  Run: python3 -c \"import time; print(int(time.time()))\"")
        print("  Paste the result into both files.")
        print("  " + "=" * 50 + "\n")
        exit(1)


def get_current_slot():
    return int((time.time() - GENESIS_TIME) / REWARD_INTERVAL)


def _ver(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0, 0)


def get_eligibility_threshold(n):
    return 1.0 if n <= TARGET_PARTICIPANTS else TARGET_PARTICIPANTS / n


def is_eligible(device_id, slot, n):
    t = get_eligibility_threshold(n)
    if t >= 1.0:
        return True
    return int(hashlib.sha256(f"{device_id}:{slot}".encode()).hexdigest(), 16) < int(t * (2 ** 256))


def compute_collective_target(verified_reveals: dict) -> str:
    """Compute collective target from commit-verified reveals only."""
    tickets = sorted(r["ticket"] for r in verified_reveals.values())
    if not tickets:
        return hashlib.sha256(b"").hexdigest()
    return hashlib.sha256(":".join(tickets).encode()).hexdigest()


# ── Shared state ───────────────────────────────────────────────────────────────

peers          = {}
commits        = {}
reveals        = {}
missed_reveals = {}
ban_until      = {}

peers_lock   = threading.Lock()
lottery_lock = threading.Lock()
rate_lock    = threading.Lock()

_peer_count_history      = []
_peer_count_history_lock = threading.Lock()

_chain_tip      = {"hash": GENESIS_PREV_HASH, "slot": -1, "device_id": ""}
_chain_tip_lock = threading.Lock()

_checkpoint_tips    = {}   # {cp_slot: {device_id: tip_hash}} — LMD: latest tip per node
_checkpoint_winners = {}   # {cp_slot: {tip_hash: first_device_id}}
_checkpoint_tips_lock = threading.Lock()

# Chain-anchored wallet creation (Sybil resistance).
_recent_block_hashes      = []
_recent_block_hashes_lock = threading.Lock()

# Rate limit: max 1 new wallet per block hash per IP.
_wallet_hash_ip_rate      = {}
_wallet_hash_ip_rate_lock = threading.Lock()

# Identity maturation tracking (defense-in-depth).
# Records the first slot at which each device_id was seen via HELLO or REGISTER.
# Used in SUBMIT_COMMIT to reject identities younger than MIN_IDENTITY_AGE.
# Never pruned — must persist for the lifetime of the bootstrap process.
# NOTE: This is a soft gate only. Consensus enforcement lives in timpal.py.
_identity_first_seen = {}   # {device_id: first_seen_slot}
_identity_lock       = threading.Lock()

commit_ip_rate         = {}
reveal_ip_rate         = {}
hello_ip_rate          = {}
bs_ip_rate             = {}
tip_ip_rate            = {}
checkpoint_tip_ip_rate = {}   # M8 FIX

bootstrap_servers      = {}
bootstrap_servers_lock = threading.Lock()


# ── Background tasks ───────────────────────────────────────────────────────────

def get_smoothed_network_size():
    with _peer_count_history_lock:
        if not _peer_count_history:
            with peers_lock:
                return max(1, len(peers))
        return max(1, int(sum(_peer_count_history) / len(_peer_count_history)))


def _record_network_size():
    while True:
        time.sleep(REWARD_INTERVAL)
        with peers_lock:
            n = len(peers)
        with _peer_count_history_lock:
            _peer_count_history.append(n)
            if len(_peer_count_history) > NETWORK_SIZE_SAMPLES:
                _peer_count_history.pop(0)


def _check_missed_reveals():
    last = -1
    while True:
        time.sleep(1.0)
        cs  = get_current_slot()
        chk = cs - 2
        if chk <= 0 or chk == last:
            continue
        last = chk
        with lottery_lock:
            if chk not in commits:
                continue
            missed = set(commits[chk].keys()) - set(reveals.get(chk, {}).keys())
            for did in missed:
                missed_reveals[did] = missed_reveals.get(did, 0) + 1
                cnt = missed_reveals[did]
                if cnt >= REVEAL_MISS_THRESHOLD:
                    ban_until[did]      = cs + BAN_DURATION
                    missed_reveals[did] = 0
                    print(f"  [!] Reveal ban: {did[:20]}... until slot {ban_until[did]}")


def clean_old_data():
    while True:
        time.sleep(60)
        now    = time.time()
        cutoff = now - 300
        cs     = get_current_slot()

        with peers_lock:
            stale = [p for p, d in peers.items() if d["last_seen"] < cutoff]
            for p in stale:
                del peers[p]
            if stale:
                print(f"  Cleaned {len(stale)} stale peers. Active: {len(peers)}")

        with lottery_lock:
            for d in (commits, reveals):
                for s in [s for s in list(d) if s < cs - 20]:
                    del d[s]
            for did in [d for d, s in list(ban_until.items()) if s < cs]:
                del ban_until[did]
                missed_reveals.pop(did, None)

        with rate_lock:
            for rd in (commit_ip_rate, reveal_ip_rate, tip_ip_rate):
                for ip in list(rd.keys()):
                    for s in [s for s in list(rd[ip].keys()) if s < cs - 20]:
                        del rd[ip][s]
                    if not rd[ip]:
                        del rd[ip]
            # M8 FIX: prune checkpoint_tip_ip_rate alongside hello_ip_rate
            for rd in (hello_ip_rate, checkpoint_tip_ip_rate):
                for ip in list(rd.keys()):
                    rd[ip] = [t for t in rd[ip] if now - t < 60]
                    if not rd[ip]:
                        del rd[ip]

        bs_cut = now - 86400
        with bootstrap_servers_lock:
            stale_bs = [k for k, v in bootstrap_servers.items() if v["last_seen"] < bs_cut]
            for k in stale_bs:
                del bootstrap_servers[k]
            for ip in list(bs_ip_rate.keys()):
                bs_ip_rate[ip] = [t for t in bs_ip_rate[ip] if now - t < 3600]
                if not bs_ip_rate[ip]:
                    del bs_ip_rate[ip]

        with _checkpoint_tips_lock:
            keep = sorted(_checkpoint_tips.keys())[-5:]
            for s in [s for s in list(_checkpoint_tips) if s not in keep]:
                del _checkpoint_tips[s]
                _checkpoint_winners.pop(s, None)

        # Prune wallet hash rate entries for expired block hashes.
        with _recent_block_hashes_lock:
            current_valid = set(_recent_block_hashes)
        with _wallet_hash_ip_rate_lock:
            for pip in list(_wallet_hash_ip_rate.keys()):
                for bh in list(_wallet_hash_ip_rate[pip].keys()):
                    if bh not in current_valid:
                        del _wallet_hash_ip_rate[pip][bh]
                if not _wallet_hash_ip_rate[pip]:
                    del _wallet_hash_ip_rate[pip]

        # NOTE: _identity_first_seen is intentionally NOT pruned.
        # Identity records must persist for the bootstrap process lifetime
        # so maturation checks remain valid across reconnects.


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

        if mt == "HELLO":
            did  = msg.get("device_id", "")
            port = msg.get("port", PORT)
            ver  = msg.get("version", "0.0")
            if _ver(ver) < _ver(MIN_VERSION):
                conn.sendall(json.dumps({
                    "type":   "VERSION_REJECTED",
                    "reason": f"Update required (min {MIN_VERSION}) — delete wallet+ledger then re-download from GitHub"
                }).encode())
                return
            now = time.time()
            with rate_lock:
                hello_ip_rate.setdefault(ip, [])
                hello_ip_rate[ip] = [t for t in hello_ip_rate[ip] if now - t < 60]
                if len(hello_ip_rate[ip]) >= HELLO_RATE_LIMIT:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit"}).encode())
                    return
                hello_ip_rate[ip].append(now)
            ns = get_smoothed_network_size()
            with _chain_tip_lock:
                current_tip_slot = _chain_tip["slot"]
            genesis_phase = current_tip_slot < 1000
            if not genesis_phase:
                gbh = msg.get("genesis_block_hash", "")
                with peers_lock:
                    already_known = did in peers
                if not already_known:
                    with _recent_block_hashes_lock:
                        valid_hashes = list(_recent_block_hashes)
                    if not gbh or gbh not in valid_hashes:
                        conn.sendall(json.dumps({
                            "type":   "VERSION_REJECTED",
                            "reason": "Wallet must be created with a live block hash. "
                                      "Delete your wallet and restart to create a valid one."
                        }).encode())
                        return
                    with _wallet_hash_ip_rate_lock:
                        ip_entry = _wallet_hash_ip_rate.setdefault(ip, {})
                        count    = ip_entry.get(gbh, 0)
                        if count >= 1:
                            conn.sendall(json.dumps({
                                "type":   "VERSION_REJECTED",
                                "reason": "Only 1 wallet can be created per IP per block. "
                                          "Wait for a new block (~5 seconds) and try again. "
                                          "Note: only 1 wallet is allowed per IP per 83-minute window."
                            }).encode())
                            return
                        ip_entry[gbh] = 1
            with peers_lock:
                is_new = did not in peers
                if is_new and len(peers) >= 10000:
                    del peers[min(peers, key=lambda k: peers[k]["last_seen"])]
                peers[did] = {"ip": ip, "port": port, "last_seen": time.time()}
                ap = [{"device_id": p, "ip": d["ip"], "port": d["port"]}
                      for p, d in peers.items() if p != did]
                pl = random.sample(ap, min(HELLO_PEERS_SAMPLE, len(ap)))
            with _chain_tip_lock:
                th = _chain_tip["hash"]
                ts = _chain_tip["slot"]
            # Record identity first_seen_slot for maturation tracking.
            # Only records if not already present — earliest slot always wins.
            cs = get_current_slot()
            with _identity_lock:
                if did and did not in _identity_first_seen:
                    _identity_first_seen[did] = cs
            conn.sendall(json.dumps({
                "type":           "PEERS",
                "peers":          pl,
                "network_size":   ns,
                "chain_tip_hash": th,
                "chain_tip_slot": ts
            }).encode())
            if is_new:
                print(f"  [+] Node v{ver}: {did[:20]}... from {ip}:{port} | total={len(peers)} ns={ns}")

        elif mt == "REGISTER":
            # Identity registration message from a node.
            # Bootstrap records first_seen_slot for maturation tracking.
            # This is defense-in-depth only — consensus enforcement is in timpal.py.
            did     = msg.get("device_id", "")
            pub_hex = msg.get("public_key", "")
            if not (isinstance(did, str) and len(did) == 64
                    and all(c in "0123456789abcdef" for c in did)):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid"}).encode())
                return
            if not isinstance(pub_hex, str) or not pub_hex:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid"}).encode())
                return
            # Verify device_id derivation
            try:
                pub_bytes = bytes.fromhex(pub_hex)
                gbh = msg.get("genesis_block_hash", "")
                if gbh and len(gbh) == 64 and all(c in "0123456789abcdef" for c in gbh):
                    expected_id = hashlib.sha256(pub_bytes + bytes.fromhex(gbh)).hexdigest()
                else:
                    expected_id = hashlib.sha256(pub_bytes).hexdigest()
                if expected_id != did:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid id"}).encode())
                    return
            except Exception:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid"}).encode())
                return
            cs = get_current_slot()
            with _identity_lock:
                if did not in _identity_first_seen:
                    _identity_first_seen[did] = cs
                    print(f"  [register] Identity: {did[:20]}... slot={cs}")
            conn.sendall(json.dumps({"type": "REGISTER_ACK"}).encode())

        elif mt == "SUBMIT_TIP":
            did  = msg.get("device_id", "")
            slot = msg.get("slot")
            th   = msg.get("tip_hash", "")
            if not all([did, slot is not None, th]):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing"}).encode()); return
            if not isinstance(slot, int) or slot < 0:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad slot"}).encode()); return
            if len(did) != 64 or not all(c in "0123456789abcdef" for c in did.lower()):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad id"}).encode()); return
            if len(th) != 64 or not all(c in "0123456789abcdef" for c in th.lower()):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad hash"}).encode()); return
            cs = get_current_slot()
            if abs(slot - cs) > 10:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "stale"}).encode()); return
            with rate_lock:
                tip_ip_rate.setdefault(ip, {})
                tip_ip_rate[ip].setdefault(slot, 0)
                if tip_ip_rate[ip][slot] >= TIP_RATE_LIMIT:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit"}).encode()); return
                tip_ip_rate[ip][slot] += 1
            with _chain_tip_lock:
                if slot > _chain_tip["slot"]:
                    _chain_tip.update({"hash": th, "slot": slot, "device_id": did})
                    print(f"  [chain] Tip: slot {slot} by {did[:20]}...")
                    with _recent_block_hashes_lock:
                        if th not in _recent_block_hashes:
                            _recent_block_hashes.append(th)
                            if len(_recent_block_hashes) > 100:
                                _recent_block_hashes.pop(0)
            cp_slot = msg.get("cp_slot")
            if not isinstance(cp_slot, int) or cp_slot % CHECKPOINT_INTERVAL != 0:
                cp_slot = (slot // CHECKPOINT_INTERVAL) * CHECKPOINT_INTERVAL
            if cp_slot > 0:
                with _checkpoint_tips_lock:
                    _checkpoint_tips.setdefault(cp_slot, {})[did] = th
                    _checkpoint_winners.setdefault(cp_slot, {})
                    if th not in _checkpoint_winners[cp_slot]:
                        _checkpoint_winners[cp_slot][th] = did
            conn.sendall(json.dumps({"type": "TIP_ACK", "slot": slot}).encode())

        elif mt == "GET_CHAIN_TIP":
            with _chain_tip_lock:
                th = _chain_tip["hash"]
                ts = _chain_tip["slot"]
            conn.sendall(json.dumps({
                "type":           "CHAIN_TIP_RESPONSE",
                "chain_tip_hash": th,
                "chain_tip_slot": ts
            }).encode())

        elif mt == "GET_WALLET_BLOCK_HASH":
            with _chain_tip_lock:
                current_tip_slot = _chain_tip["slot"]
                current_tip_hash = _chain_tip["hash"]
            genesis_phase = current_tip_slot < 1000
            with _recent_block_hashes_lock:
                latest_hash = _recent_block_hashes[-1] if _recent_block_hashes else ""
            conn.sendall(json.dumps({
                "type":          "WALLET_BLOCK_HASH_RESPONSE",
                "block_hash":    latest_hash,
                "genesis_phase": genesis_phase,
                "tip_slot":      current_tip_slot
            }).encode())
            if not genesis_phase:
                print(f"  [wallet] Block hash issued to {ip} (slot {current_tip_slot})")

        elif mt == "GET_CHECKPOINT_TIP":
            # M8 FIX: rate limit
            now = time.time()
            with rate_lock:
                checkpoint_tip_ip_rate.setdefault(ip, [])
                checkpoint_tip_ip_rate[ip] = [
                    t for t in checkpoint_tip_ip_rate[ip] if now - t < 60
                ]
                if len(checkpoint_tip_ip_rate[ip]) >= CHECKPOINT_TIP_RATE_LIMIT:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit"}).encode())
                    return
                checkpoint_tip_ip_rate[ip].append(now)

            cp_slot = msg.get("cp_slot")
            if not isinstance(cp_slot, int) or cp_slot <= 0 or cp_slot % CHECKPOINT_INTERVAL != 0:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad cp_slot"}).encode()); return
            with _checkpoint_tips_lock:
                node_tips = dict(_checkpoint_tips.get(cp_slot, {}))
                winners   = dict(_checkpoint_winners.get(cp_slot, {}))
            if not node_tips:
                conn.sendall(json.dumps({
                    "type":          "CHECKPOINT_TIP_RESPONSE",
                    "cp_slot":       cp_slot,
                    "majority_hash": None,
                    "count":         0,
                    "peer_id":       None
                }).encode())
                return
            tally = {}
            for tip_hash in node_tips.values():
                tally[tip_hash] = tally.get(tip_hash, 0) + 1
            majority_hash = max(tally, key=lambda h: (tally[h], h))
            conn.sendall(json.dumps({
                "type":          "CHECKPOINT_TIP_RESPONSE",
                "cp_slot":       cp_slot,
                "majority_hash": majority_hash,
                "count":         tally[majority_hash],
                "peer_id":       winners.get(majority_hash)
            }).encode())

        elif mt == "SUBMIT_COMMIT":
            did    = msg.get("device_id", "")
            slot   = msg.get("slot")
            commit = msg.get("commit", "")
            if not all([did, slot is not None, commit]):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing"}).encode()); return
            if not isinstance(slot, int) or slot < 0:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad slot"}).encode()); return
            if len(did) != 64 or not all(c in "0123456789abcdef" for c in did.lower()):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad id"}).encode()); return
            if len(commit) != 64 or not all(c in "0123456789abcdef" for c in commit.lower()):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad commit"}).encode()); return
            cs = get_current_slot()
            if abs(slot - cs) > 2:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "stale"}).encode()); return
            with lottery_lock:
                ban = ban_until.get(did, 0)
            if ban >= cs:
                conn.sendall(json.dumps({
                    "type": "COMMIT_REJECTED", "reason": "ban", "ban_until": ban
                }).encode()); return
            # Identity maturation gate (defense-in-depth).
            # Post-genesis phase: reject commits from identities younger than MIN_IDENTITY_AGE.
            # Primary enforcement is at consensus level in timpal.py _add_block_locked().
            if cs >= 1000:
                with _identity_lock:
                    first_seen = _identity_first_seen.get(did)
                if first_seen is None or cs - first_seen < MIN_IDENTITY_AGE:
                    conn.sendall(json.dumps({
                        "type":   "COMMIT_REJECTED",
                        "reason": "identity too young — wait for maturation"
                    }).encode()); return
            ns = get_smoothed_network_size()
            if not is_eligible(did, slot, ns):
                conn.sendall(json.dumps({
                    "type": "COMMIT_REJECTED", "reason": "not eligible"
                }).encode()); return
            with rate_lock:
                commit_ip_rate.setdefault(ip, {})
                commit_ip_rate[ip].setdefault(slot, 0)
                if commit_ip_rate[ip][slot] >= COMMIT_RATE_LIMIT:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit"}).encode()); return
                commit_ip_rate[ip][slot] += 1
            with peers_lock:
                if did in peers:
                    peers[did]["last_seen"] = time.time()
            with lottery_lock:
                commits.setdefault(slot, {})
                if did not in commits[slot]:
                    commits[slot][did] = commit
                    print(f"  [slot {slot}] Commit: {did[:20]}... ({len(commits[slot])} total) ns={ns}")
            conn.sendall(json.dumps({
                "type": "COMMIT_ACK", "slot": slot, "network_size": ns
            }).encode())

        elif mt == "SUBMIT_REVEAL":
            did    = msg.get("device_id", "")
            slot   = msg.get("slot")
            ticket = msg.get("ticket", "")
            sig    = msg.get("sig", "")
            seed   = msg.get("seed", "")
            pk     = msg.get("public_key", "")
            if not all([did, slot is not None, ticket, sig, seed, pk]):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing"}).encode()); return
            if not isinstance(slot, int) or slot < 0:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad slot"}).encode()); return
            if len(did) != 64 or not all(c in "0123456789abcdef" for c in did.lower()):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad id"}).encode()); return
            if len(ticket) != 64 or not all(c in "0123456789abcdef" for c in ticket.lower()):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad ticket"}).encode()); return
            if not isinstance(seed, str) or seed != str(slot):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "bad seed"}).encode()); return
            if len(pk) > 8192 or len(sig) > 8192:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "too large"}).encode()); return
            cs = get_current_slot()
            if abs(slot - cs) > 2:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "stale"}).encode()); return
            with rate_lock:
                reveal_ip_rate.setdefault(ip, {})
                reveal_ip_rate[ip].setdefault(slot, 0)
                if reveal_ip_rate[ip][slot] >= REVEAL_RATE_LIMIT:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit"}).encode()); return
                reveal_ip_rate[ip][slot] += 1
            with peers_lock:
                if did in peers:
                    peers[did]["last_seen"] = time.time()
            with lottery_lock:
                if slot in commits and did in commits[slot]:
                    reveals.setdefault(slot, {})
                    if did not in reveals[slot]:
                        reveals[slot][did] = {
                            "ticket": ticket, "sig": sig,
                            "seed": seed, "public_key": pk
                        }
                        print(f"  [slot {slot}] Reveal: {did[:20]}... ({len(reveals[slot])} total)")
            conn.sendall(json.dumps({"type": "REVEAL_ACK", "slot": slot}).encode())

        elif mt == "GET_COMMITS":
            slot = msg.get("slot")
            if slot is None:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing slot"}).encode()); return
            with lottery_lock:
                sc = dict(commits.get(slot, {}))
            conn.sendall(json.dumps({
                "type": "COMMITS_RESPONSE", "slot": slot, "commits": sc
            }).encode())

        elif mt == "GET_REVEALS":
            slot = msg.get("slot")
            if slot is None:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "missing slot"}).encode()); return
            with lottery_lock:
                sr = dict(reveals.get(slot, {}))
                verified_reveals = {
                    did: r for did, r in sr.items()
                    if did in commits.get(slot, {})
                }
            ct = compute_collective_target(verified_reveals) if verified_reveals else None
            conn.sendall(json.dumps({
                "type": "REVEALS_RESPONSE", "slot": slot,
                "reveals": sr, "collective_target": ct
            }).encode())

        elif mt == "PING":
            did = msg.get("device_id", "")
            with peers_lock:
                if did in peers:
                    peers[did]["last_seen"] = time.time()
            conn.sendall(json.dumps({
                "type": "PONG", "network_size": get_smoothed_network_size()
            }).encode())

        elif mt == "GET_PEERS":
            with peers_lock:
                pl = [{"device_id": p} for p in peers]
            conn.sendall(json.dumps({"type": "PEERS", "peers": pl}).encode())

        elif mt == "REGISTER_BOOTSTRAP":
            h = msg.get("host", "").strip()
            p = msg.get("port", 0)
            if not h or not isinstance(p, int) or not (1024 <= p <= 65535):
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid"}).encode()); return
            if len(h) > 253:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "invalid host"}).encode()); return
            now   = time.time()
            known = set()
            with bootstrap_servers_lock:
                hl = [v["host"] for v in bootstrap_servers.values()]
            for host in hl:
                try:
                    known.add(socket.gethostbyname(host))
                except Exception:
                    known.add(host)
            if ip not in known:
                with bootstrap_servers_lock:
                    bs_ip_rate.setdefault(ip, [])
                    bs_ip_rate[ip] = [t for t in bs_ip_rate[ip] if now - t < 3600]
                    if len(bs_ip_rate[ip]) >= BS_RATE_LIMIT:
                        conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit"}).encode()); return
                    bs_ip_rate[ip].append(now)
            key = f"{h}:{p}"
            try:
                pr = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                pr.settimeout(3.0)
                pr.connect((h, p))
                pr.close()
            except Exception:
                conn.sendall(json.dumps({"type": "ERROR", "msg": "not reachable"}).encode()); return
            with bootstrap_servers_lock:
                if key not in bootstrap_servers:
                    if len(bootstrap_servers) >= BS_MAX_SERVERS:
                        del bootstrap_servers[min(bootstrap_servers,
                                                  key=lambda k: bootstrap_servers[k]["last_seen"])]
                    bootstrap_servers[key] = {"host": h, "port": p, "last_seen": now}
                    print(f"  [+] Bootstrap: {key} | total={len(bootstrap_servers)}")
                else:
                    bootstrap_servers[key]["last_seen"] = now
            conn.sendall(json.dumps({"type": "REGISTER_ACK", "key": key}).encode())

        elif mt == "GET_BOOTSTRAP_SERVERS":
            now = time.time()
            with rate_lock:
                hello_ip_rate.setdefault(ip, [])
                hello_ip_rate[ip] = [t for t in hello_ip_rate[ip] if now - t < 60]
                if len(hello_ip_rate[ip]) >= HELLO_RATE_LIMIT:
                    conn.sendall(json.dumps({"type": "ERROR", "msg": "rate limit"}).encode()); return
                hello_ip_rate[ip].append(now)
            with bootstrap_servers_lock:
                bl = [{"host": v["host"], "port": v["port"]} for v in bootstrap_servers.values()]
            conn.sendall(json.dumps({
                "type": "BOOTSTRAP_SERVERS_RESPONSE", "servers": bl
            }).encode())

    except Exception:
        pass
    finally:
        conn.close()


def _gossip_bootstrap_servers():
    time.sleep(30)
    while True:
        time.sleep(300)
        with bootstrap_servers_lock:
            targets = list(bootstrap_servers.values())
            ol      = list(bootstrap_servers.values())
        for t in targets:
            for e in ol:
                if e["host"] == t["host"] and e["port"] == t["port"]:
                    continue
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(3.0)
                    s.connect((t["host"], t["port"]))
                    s.sendall(json.dumps({
                        "type": "REGISTER_BOOTSTRAP",
                        "host": e["host"],
                        "port": e["port"]
                    }).encode())
                    s.close()
                except Exception:
                    continue


def main():
    _check_genesis_time()
    print("=" * 54)
    print("  TIMPAL Bootstrap Server v3.3")
    print("  Peer Discovery + Eligibility-Gated Lottery + Chain Tip")
    print("=" * 54)
    print(f"  Port: {PORT} | Min version: {MIN_VERSION} | Target: {TARGET_PARTICIPANTS}/slot")
    print("=" * 54 + "\n")
    for fn in (clean_old_data, _gossip_bootstrap_servers,
               _record_network_size, _check_missed_reveals):
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
