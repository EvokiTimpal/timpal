"""TIMPAL API v3.0 — serves live ledger data for timpal.org explorer.

v3.0 changes:
  - Nodes push "blocks" (chain blocks) instead of flat "rewards" list
  - Explorer shows chain height, slot, prev_hash linkage, confirmed status
  - All existing endpoints unchanged — block structure is a superset of reward

Push authentication unchanged: nodes sign with Dilithium3 private key.
No shared secret exists anywhere.
"""

import json
import os
import time
import threading
import urllib.parse
import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

try:
    from dilithium_py.dilithium import Dilithium3
    _DILITHIUM_AVAILABLE = True
except ImportError:
    _DILITHIUM_AVAILABLE = False
    print("[!] dilithium-py not installed — push signature verification disabled")
    print("    Run: pip3 install dilithium-py cryptography")

import hashlib

CONFIRMATION_DEPTH = 6   # must match timpal.py

# ── In-memory ledger state ─────────────────────────────────────────────────────
_ledger = {
    "blocks":       [],   # v3.0: was "rewards" — now chain blocks
    "transactions": [],
    "total_minted": 0.0,
    "chain_height": 0,
    "chain_tip_slot": -1,
    "chain_tip_hash": "0" * 64
}
_ledger_lock = threading.Lock()
_last_update = 0

# ── Cached computed stats ──────────────────────────────────────────────────────
_stats_cache      = None
_stats_cache_lock = threading.Lock()

# ── Per-IP POST rate limiting ──────────────────────────────────────────────────
_post_rate      = {}
_post_rate_lock = threading.Lock()
POST_RATE_LIMIT = 5


def _is_valid_hex64(s) -> bool:
    if not isinstance(s, str) or len(s) != 64:
        return False
    return all(c in "0123456789abcdef" for c in s)


def _verify_push_signature(data: dict) -> bool:
    if not _DILITHIUM_AVAILABLE:
        print("[!] WARNING: push accepted without signature verification")
        return True

    device_id  = data.get("device_id", "")
    public_key = data.get("public_key", "")
    signature  = data.get("signature", "")

    if not device_id or not public_key or not signature:
        return False

    try:
        pub_bytes = bytes.fromhex(public_key)
        if hashlib.sha256(pub_bytes).hexdigest() != device_id:
            return False
    except Exception:
        return False

    payload_data = {k: v for k, v in data.items() if k != "signature"}
    try:
        payload_bytes = json.dumps(payload_data, sort_keys=True, separators=(',', ':')).encode()
    except Exception:
        return False

    try:
        sig_bytes = bytes.fromhex(signature)
        return Dilithium3.verify(pub_bytes, payload_bytes, sig_bytes)
    except Exception:
        return False


def fmt_time(ts):
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _is_confirmed(block_slot, current_slot):
    return current_slot - block_slot >= CONFIRMATION_DEPTH


def _clean_post_rate():
    while True:
        time.sleep(60)
        now = time.time()
        with _post_rate_lock:
            stale = [ip for ip, times in _post_rate.items()
                     if not [t for t in times if now - t < 10]]
            for ip in stale:
                del _post_rate[ip]


def _rebuild_stats_cache(blocks, txs, total_minted, chain_height, chain_tip_slot, chain_tip_hash):
    current_slot  = chain_tip_slot
    block_rewards = [b for b in blocks if b.get("type") == "block_reward"]
    computed      = round(sum(b.get("amount", 0) for b in block_rewards), 8)
    total_minted  = max(computed, total_minted)

    node_counts = {}
    for b in block_rewards:
        wid = b.get("winner_id", "")
        if wid:
            node_counts[wid] = node_counts.get(wid, 0) + 1

    total_r    = sum(node_counts.values()) or 1
    node_stats = sorted([
        {"id": nid, "id_short": nid[:16] + "...",
         "rewards": cnt, "pct": round(cnt / total_r * 100, 2)}
        for nid, cnt in node_counts.items()
    ], key=lambda x: x["rewards"], reverse=True)

    recent_blocks = sorted(blocks, key=lambda b: b.get("slot", 0), reverse=True)[:50]
    recent_txs    = sorted(txs,    key=lambda t: t.get("timestamp", 0), reverse=True)[:50]

    return {
        "total_minted":    total_minted,
        "remaining":       round(250_000_000 - total_minted, 8),
        "total_rewards":   len(block_rewards),
        "total_txs":       len(txs),
        "active_nodes":    len(node_counts),
        "chain_height":    chain_height,
        "chain_tip_slot":  chain_tip_slot,
        "chain_tip_hash":  chain_tip_hash,
        "node_stats":      node_stats,
        "recent_blocks": [
            {
                "id":        b.get("winner_id", ""),
                "amount":    b.get("amount", 0),
                "time":      fmt_time(b.get("timestamp")),
                "slot":      b.get("slot", ""),
                "prev_hash": b.get("prev_hash", "")[:16] + "..." if b.get("prev_hash") else "",
                "confirmed": _is_confirmed(b.get("slot", 0), current_slot)
            }
            for b in recent_blocks
        ],
        "recent_txs": [
            {
                "tx_id":     t.get("tx_id", ""),
                "id":        (t.get("tx_id", "") or "")[:16] + "...",
                "sender":    t.get("sender_id", ""),
                "recipient": t.get("recipient_id", ""),
                "amount":    t.get("amount", 0),
                "time":      fmt_time(t.get("timestamp")),
                "timestamp": t.get("timestamp", 0)
            }
            for t in recent_txs
        ]
    }


class Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            parsed = urllib.parse.urlparse(self.path)
            path   = parsed.path.rstrip("/")
            params = urllib.parse.parse_qs(parsed.query)

            # ── GET /api — main stats ─────────────────────────────────────────
            if path in ("", "/", "/api", "/api/"):
                with _stats_cache_lock:
                    cache = _stats_cache
                if cache is None:
                    self.wfile.write(json.dumps({
                        "total_minted": 0, "remaining": 250_000_000,
                        "total_rewards": 0, "total_txs": 0, "active_nodes": 0,
                        "chain_height": 0, "chain_tip_slot": -1,
                        "chain_tip_hash": "0" * 64,
                        "node_stats": [], "recent_blocks": [], "recent_txs": []
                    }).encode())
                else:
                    self.wfile.write(json.dumps(cache).encode())

            # ── GET /api/address?id=<hex64> ───────────────────────────────────
            elif path == "/api/address":
                addr = params.get("id", [""])[0].strip()
                if not addr:
                    self.wfile.write(json.dumps({"error": "missing id"}).encode())
                    return
                if not _is_valid_hex64(addr):
                    self.wfile.write(json.dumps({"error": "invalid address format"}).encode())
                    return
                with _ledger_lock:
                    blocks = list(_ledger["blocks"])
                    txs    = list(_ledger["transactions"])
                    current_slot = _ledger["chain_tip_slot"]
                addr_blocks   = sorted([b for b in blocks if b.get("winner_id", "") == addr],
                                       key=lambda b: b.get("slot", 0), reverse=True)
                addr_txs_sent = [t for t in txs if t.get("sender_id",    "") == addr]
                addr_txs_recv = [t for t in txs if t.get("recipient_id", "") == addr]
                addr_txs      = sorted(addr_txs_sent + addr_txs_recv,
                                       key=lambda t: t.get("timestamp", 0), reverse=True)
                self.wfile.write(json.dumps({
                    "address":       addr,
                    "total_rewards": len(addr_blocks),
                    "total_earned":  round(sum(b.get("amount", 0) for b in addr_blocks), 8),
                    "total_sent":    round(sum(t.get("amount", 0) for t in addr_txs_sent), 8),
                    "total_received":round(sum(t.get("amount", 0) for t in addr_txs_recv), 8),
                    "blocks": [
                        {
                            "amount":    b.get("amount", 0),
                            "time":      fmt_time(b.get("timestamp")),
                            "slot":      b.get("slot", ""),
                            "prev_hash": b.get("prev_hash", ""),
                            "confirmed": _is_confirmed(b.get("slot", 0), current_slot)
                        }
                        for b in addr_blocks[:100]
                    ],
                    "transactions": [
                        {
                            "tx_id":       t.get("tx_id", ""),
                            "direction":   "sent" if t.get("sender_id") == addr else "received",
                            "counterparty":(t.get("recipient_id", "")
                                            if t.get("sender_id") == addr
                                            else t.get("sender_id", "")),
                            "amount":      t.get("amount", 0),
                            "time":        fmt_time(t.get("timestamp")),
                            "timestamp":   t.get("timestamp", 0)
                        }
                        for t in addr_txs
                    ]
                }).encode())

            # ── GET /api/tx?id=<tx_id> ────────────────────────────────────────
            elif path == "/api/tx":
                tx_id = params.get("id", [""])[0].strip()
                if not tx_id:
                    self.wfile.write(json.dumps({"error": "missing id"}).encode())
                    return
                if not isinstance(tx_id, str) or len(tx_id) > 64 or not tx_id.replace("-", "").isalnum():
                    self.wfile.write(json.dumps({"error": "invalid tx_id format"}).encode())
                    return
                with _ledger_lock:
                    tx = next((t for t in _ledger["transactions"]
                               if t.get("tx_id", "") == tx_id), None)
                if not tx:
                    self.wfile.write(json.dumps({"error": "not found"}).encode())
                    return
                self.wfile.write(json.dumps({
                    "tx_id":     tx.get("tx_id", ""),
                    "sender":    tx.get("sender_id", ""),
                    "recipient": tx.get("recipient_id", ""),
                    "amount":    tx.get("amount", 0),
                    "timestamp": tx.get("timestamp", 0),
                    "time":      fmt_time(tx.get("timestamp")),
                    "signature": tx.get("signature", ""),
                    "confirmed": True
                }).encode())

            # ── GET /api/block?slot=<int> ─────────────────────────────────────
            elif path == "/api/block":
                slot_str = params.get("slot", [""])[0].strip()
                if not slot_str:
                    self.wfile.write(json.dumps({"error": "missing slot"}).encode())
                    return
                try:
                    slot = int(slot_str)
                except ValueError:
                    self.wfile.write(json.dumps({"error": "invalid slot"}).encode())
                    return
                with _ledger_lock:
                    block = next((b for b in _ledger["blocks"]
                                  if b.get("slot") == slot), None)
                    current_slot = _ledger["chain_tip_slot"]
                if not block:
                    self.wfile.write(json.dumps({"error": "not found"}).encode())
                    return
                self.wfile.write(json.dumps({
                    "slot":       block.get("slot"),
                    "winner":     block.get("winner_id", ""),
                    "amount":     block.get("amount", 0),
                    "prev_hash":  block.get("prev_hash", ""),
                    "time":       fmt_time(block.get("timestamp")),
                    "timestamp":  block.get("timestamp", 0),
                    "confirmed":  _is_confirmed(slot, current_slot),
                    "nodes":      block.get("nodes", 1)
                }).encode())

            else:
                self.wfile.write(json.dumps({"error": "not found"}).encode())

        except Exception as e:
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_POST(self):
        global _last_update, _stats_cache
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            ip  = self.client_address[0]
            now = time.time()

            with _post_rate_lock:
                times = [t for t in _post_rate.get(ip, []) if now - t < 10]
                if len(times) >= POST_RATE_LIMIT:
                    self.wfile.write(json.dumps({"error": "rate limit exceeded"}).encode())
                    return
                times.append(now)
                _post_rate[ip] = times

            length = int(self.headers.get("Content-Length", 0))
            if length > 1_000_000:
                self.wfile.write(json.dumps({"error": "payload too large"}).encode())
                return

            body = self.rfile.read(length)
            data = json.loads(body.decode())

            if data.get("type") != "LEDGER_PUSH":
                self.wfile.write(json.dumps({"error": "unknown type"}).encode())
                return

            if not _verify_push_signature(data):
                self.wfile.write(json.dumps({"error": "invalid signature"}).encode())
                return

            # v3.0: nodes push "blocks" (chain blocks) instead of flat rewards
            incoming_blocks = data.get("blocks", [])
            txs             = data.get("transactions", [])

            # Structural validation
            incoming_blocks = [b for b in incoming_blocks
                               if isinstance(b, dict)
                               and isinstance(b.get("amount"), (int, float))
                               and _is_valid_hex64(b.get("winner_id", ""))
                               and isinstance(b.get("slot"), int)]
            txs = [t for t in txs
                   if isinstance(t, dict)
                   and isinstance(t.get("amount"), (int, float))
                   and isinstance(t.get("tx_id"), str)]

            with _ledger_lock:
                existing_slots = {
                    b.get("slot"): b
                    for b in _ledger["blocks"] if b.get("type") == "block_reward"
                }
                for b in incoming_blocks:
                    slot  = b.get("slot")
                    rtype = b.get("type", "block_reward")

                    if rtype == "block_reward":
                        if not _is_valid_hex64(b.get("vrf_ticket", "")):
                            continue

                    if slot is not None and rtype != "fee_reward":
                        if slot not in existing_slots:
                            # First writer wins — first valid block per slot is accepted.
                            # Consistent with node behavior.
                            _ledger["blocks"].append(b)
                            existing_slots[slot] = b
                    else:
                        if not any(x.get("reward_id") == b.get("reward_id")
                                   for x in _ledger["blocks"]):
                            _ledger["blocks"].append(b)

                existing_txids = {t.get("tx_id") for t in _ledger["transactions"]}
                for t in txs:
                    if t.get("tx_id") not in existing_txids:
                        _ledger["transactions"].append(t)
                        existing_txids.add(t.get("tx_id"))

                _ledger["blocks"]       = _ledger["blocks"][-10000:]
                _ledger["transactions"] = _ledger["transactions"][-5000:]

                if data.get("total_minted", 0.0) > _ledger["total_minted"]:
                    _ledger["total_minted"] = data["total_minted"]

                # Update chain height and tip from incoming data
                block_rewards = [b for b in _ledger["blocks"] if b.get("type") == "block_reward"]
                if block_rewards:
                    tip_block = max(block_rewards, key=lambda b: b.get("slot", -1))
                    _ledger["chain_height"]   = len(block_rewards)
                    _ledger["chain_tip_slot"] = tip_block.get("slot", -1)
                    _ledger["chain_tip_hash"] = tip_block.get("prev_hash", "0" * 64)

                blocks_snap      = list(_ledger["blocks"])
                txs_snap         = list(_ledger["transactions"])
                minted_snap      = _ledger["total_minted"]
                height_snap      = _ledger["chain_height"]
                tip_slot_snap    = _ledger["chain_tip_slot"]
                tip_hash_snap    = _ledger["chain_tip_hash"]

            new_cache = _rebuild_stats_cache(
                blocks_snap, txs_snap, minted_snap,
                height_snap, tip_slot_snap, tip_hash_snap
            )
            with _stats_cache_lock:
                _stats_cache = new_cache

            _last_update = time.time()
            self.wfile.write(json.dumps({"ok": True}).encode())

        except Exception as e:
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=_clean_post_rate, daemon=True).start()
    print("TIMPAL API v3.0 running on port 7781")
    print("Push authentication: Dilithium3 signature (no shared secret)")
    print("v3.0: chain blocks, height, tip tracking, /api/block endpoint")
    server = ThreadingHTTPServer(("0.0.0.0", 7781), Handler)
    server.serve_forever()
