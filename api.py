"""TIMPAL API v2.2 — serves live ledger data for timpal.org explorer.
Nodes push updates here — no NAT issues, fully decentralized data."""

import json
import os
import time
import threading
import urllib.parse
import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PUSH_SECRET = "b7e2f4a1c9d3e8f2a5b1c4d7e0f3a6b9c2d5e8f1a4b7c0d3e6f9a2b5c8d1e4f7"

# ── In-memory ledger state ─────────────────────────────────────────────────────
_ledger      = {"rewards": [], "transactions": [], "total_minted": 0.0}
_ledger_lock = threading.Lock()
_last_update = 0

# ── Cached computed stats ──────────────────────────────────────────────────────
_stats_cache      = None
_stats_cache_lock = threading.Lock()

# ── Per-IP POST rate limiting ──────────────────────────────────────────────────
_post_rate      = {}
_post_rate_lock = threading.Lock()
POST_RATE_LIMIT = 5    # Max pushes per IP per 10 seconds


def _is_valid_hex64(s) -> bool:
    """True if s is a 64-character lowercase hex string."""
    if not isinstance(s, str) or len(s) != 64:
        return False
    return all(c in "0123456789abcdef" for c in s)


def fmt_time(ts):
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _clean_post_rate():
    """Background thread: remove stale IP entries from _post_rate every 60 seconds."""
    while True:
        time.sleep(60)
        now = time.time()
        with _post_rate_lock:
            stale = [ip for ip, times in _post_rate.items()
                     if not [t for t in times if now - t < 10]]
            for ip in stale:
                del _post_rate[ip]


def _rebuild_stats_cache(rewards, txs, total_minted):
    block_rewards = [r for r in rewards if r.get("type") == "block_reward"]
    computed      = round(sum(r.get("amount", 0) for r in block_rewards), 8)
    total_minted  = max(computed, total_minted)

    node_counts = {}
    for r in block_rewards:
        wid = r.get("winner_id", "")
        if wid:
            node_counts[wid] = node_counts.get(wid, 0) + 1

    total_r    = sum(node_counts.values()) or 1
    node_stats = sorted([
        {
            "id":       nid,
            "id_short": nid[:16] + "...",
            "rewards":  cnt,
            "pct":      round(cnt / total_r * 100, 2)
        }
        for nid, cnt in node_counts.items()
    ], key=lambda x: x["rewards"], reverse=True)

    recent_rewards = sorted(rewards, key=lambda r: r.get("timestamp", 0), reverse=True)[:50]
    recent_txs     = sorted(txs,     key=lambda t: t.get("timestamp", 0), reverse=True)[:50]

    return {
        "total_minted":   total_minted,
        "remaining":      round(250_000_000 - total_minted, 8),
        "total_rewards":  len(block_rewards),
        "total_txs":      len(txs),
        "active_nodes":   len(node_counts),
        "node_stats":     node_stats,
        "recent_rewards": [
            {
                "id":     r.get("winner_id", ""),
                "amount": r.get("amount", 0),
                "time":   fmt_time(r.get("timestamp")),
                "slot":   r.get("time_slot", "")
            }
            for r in recent_rewards
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

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            parsed = urllib.parse.urlparse(self.path)
            path   = parsed.path.rstrip("/")
            params = urllib.parse.parse_qs(parsed.query)

            if path in ("", "/", "/api", "/api/"):
                with _stats_cache_lock:
                    cache = _stats_cache
                if cache is None:
                    self.wfile.write(json.dumps({
                        "total_minted":   0,
                        "remaining":      250_000_000,
                        "total_rewards":  0,
                        "total_txs":      0,
                        "active_nodes":   0,
                        "node_stats":     [],
                        "recent_rewards": [],
                        "recent_txs":     []
                    }).encode())
                else:
                    self.wfile.write(json.dumps(cache).encode())

            elif path == "/api/address":
                addr = params.get("id", [""])[0].strip()
                if not addr:
                    self.wfile.write(json.dumps({"error": "missing id"}).encode())
                    return
                # Validate — must be 64-char lowercase hex
                if not _is_valid_hex64(addr):
                    self.wfile.write(json.dumps({"error": "invalid address format"}).encode())
                    return
                with _ledger_lock:
                    rewards = list(_ledger["rewards"])
                    txs     = list(_ledger["transactions"])
                addr_rewards  = sorted(
                    [r for r in rewards if r.get("winner_id", "") == addr],
                    key=lambda r: r.get("timestamp", 0), reverse=True
                )
                addr_txs_sent = [t for t in txs if t.get("sender_id",    "") == addr]
                addr_txs_recv = [t for t in txs if t.get("recipient_id", "") == addr]
                addr_txs      = sorted(
                    addr_txs_sent + addr_txs_recv,
                    key=lambda t: t.get("timestamp", 0), reverse=True
                )
                self.wfile.write(json.dumps({
                    "address":        addr,
                    "total_rewards":  len(addr_rewards),
                    "total_earned":   round(sum(r.get("amount", 0) for r in addr_rewards), 8),
                    "total_sent":     round(sum(t.get("amount", 0) for t in addr_txs_sent), 8),
                    "total_received": round(sum(t.get("amount", 0) for t in addr_txs_recv), 8),
                    "rewards": [
                        {
                            "amount": r.get("amount", 0),
                            "time":   fmt_time(r.get("timestamp")),
                            "slot":   r.get("time_slot", "")
                        }
                        for r in addr_rewards[:100]
                    ],
                    "transactions": [
                        {
                            "tx_id":        t.get("tx_id", ""),
                            "direction":    "sent" if t.get("sender_id") == addr else "received",
                            "counterparty": (t.get("recipient_id", "")
                                            if t.get("sender_id") == addr
                                            else t.get("sender_id", "")),
                            "amount":    t.get("amount", 0),
                            "time":      fmt_time(t.get("timestamp")),
                            "timestamp": t.get("timestamp", 0)
                        }
                        for t in addr_txs
                    ]
                }).encode())

            elif path == "/api/tx":
                tx_id = params.get("id", [""])[0].strip()
                if not tx_id:
                    self.wfile.write(json.dumps({"error": "missing id"}).encode())
                    return
                # Basic format validation — UUID or hex string, max 64 chars
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
                times = _post_rate.get(ip, [])
                times = [t for t in times if now - t < 10]
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

            if data.get("push_secret") != PUSH_SECRET:
                self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
                return

            if data.get("type") != "LEDGER_PUSH":
                self.wfile.write(json.dumps({"error": "unknown type"}).encode())
                return

            rewards = data.get("rewards", [])
            txs     = data.get("transactions", [])

            rewards = [r for r in rewards
                       if isinstance(r, dict)
                       and isinstance(r.get("amount"), (int, float))
                       and isinstance(r.get("winner_id"), str)]
            txs     = [t for t in txs
                       if isinstance(t, dict)
                       and isinstance(t.get("amount"), (int, float))
                       and isinstance(t.get("tx_id"), str)]

            with _ledger_lock:
                existing_slots = {
                    r.get("time_slot"): r
                    for r in _ledger["rewards"]
                    if r.get("type") == "block_reward"
                }
                for r in rewards:
                    slot  = r.get("time_slot")
                    rtype = r.get("type", "block_reward")
                    if slot and rtype != "fee_reward":
                        existing = existing_slots.get(slot)
                        if not existing:
                            _ledger["rewards"].append(r)
                            existing_slots[slot] = r
                        elif r.get("vrf_ticket", "z") < existing.get("vrf_ticket", "z"):
                            _ledger["rewards"] = [
                                x for x in _ledger["rewards"]
                                if x.get("time_slot") != slot or x.get("type") == "fee_reward"
                            ]
                            _ledger["rewards"].append(r)
                            existing_slots[slot] = r
                    else:
                        if not any(x.get("reward_id") == r.get("reward_id")
                                   for x in _ledger["rewards"]):
                            _ledger["rewards"].append(r)

                existing_txids = {t.get("tx_id") for t in _ledger["transactions"]}
                for t in txs:
                    if t.get("tx_id") not in existing_txids:
                        _ledger["transactions"].append(t)
                        existing_txids.add(t.get("tx_id"))

                _ledger["rewards"]      = _ledger["rewards"][-10000:]
                _ledger["transactions"] = _ledger["transactions"][-5000:]

                if data.get("total_minted", 0.0) > _ledger["total_minted"]:
                    _ledger["total_minted"] = data["total_minted"]

                rewards_snapshot = list(_ledger["rewards"])
                txs_snapshot     = list(_ledger["transactions"])
                minted_snapshot  = _ledger["total_minted"]

            new_cache = _rebuild_stats_cache(rewards_snapshot, txs_snapshot, minted_snapshot)
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
    print("TIMPAL API v2.2 running on port 7781 — waiting for node pushes")
    server = ThreadingHTTPServer(("0.0.0.0", 7781), Handler)
    server.serve_forever()
