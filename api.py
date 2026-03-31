"""TIMPAL API v3.2 — SQLite-backed explorer for timpal.org.

v3.3 changes (this version):
  - Replaced in-memory _ledger dict with SQLite persistent storage.
  - Address and block queries read from the full SQLite index.
  - timpal.py now sends checkpoint_balances in LEDGER_PUSH.
  - chain_height display uses total_minted // REWARD_PER_ROUND.
  - Payload size limit raised to 2MB.

v3.2 fixes (carried forward):
  - total_minted from node push directly.
  - chain_tip_hash correct canonical hash of full tip block.
  - Dilithium3 push signature verification.
  - Full tip field validation on LEDGER_PUSH.
  - _compute_block_hash removed.
"""

import json
import os
import sqlite3
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

import hashlib

UNIT                = 100_000_000
TOTAL_SUPPLY_TMPL   = 250_000_000.0
REWARD_PER_ROUND    = 105_750_000
CHECKPOINT_BUFFER   = 120
RECENT_WINDOW       = 50
CONFIRMATION_DEPTH  = 6
GENESIS_TIME        = 1774706400
REWARD_INTERVAL     = 5.0
TIMESTAMP_TOLERANCE = 30

DB_PATH  = os.path.expanduser("~/.timpal_explorer.db")
_db_lock = threading.Lock()

_tip_lock = threading.Lock()
_tip = {
    "chain_tip_slot": -1,
    "chain_tip_hash": "0" * 64,
    "total_minted":   0,
    "chain_height":   0,
}

_stats_cache      = None
_stats_cache_lock = threading.Lock()
_last_update      = 0

_post_rate      = {}
_post_rate_lock = threading.Lock()
POST_RATE_LIMIT = 5


def _init_db():
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (
                slot           INTEGER PRIMARY KEY,
                reward_id      TEXT,
                winner_id      TEXT NOT NULL,
                amount         INTEGER NOT NULL,
                timestamp      INTEGER,
                type           TEXT DEFAULT 'block_reward',
                prev_hash      TEXT,
                vrf_ticket     TEXT,
                canonical_hash TEXT,
                nodes          INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_blocks_winner ON blocks(winner_id);
            CREATE INDEX IF NOT EXISTS idx_blocks_type   ON blocks(type);

            CREATE TABLE IF NOT EXISTS transactions (
                tx_id        TEXT PRIMARY KEY,
                sender_id    TEXT,
                recipient_id TEXT,
                amount       INTEGER,
                fee          INTEGER,
                timestamp    REAL,
                slot         INTEGER,
                signature    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_txs_sender    ON transactions(sender_id);
            CREATE INDEX IF NOT EXISTS idx_txs_recipient ON transactions(recipient_id);

            CREATE TABLE IF NOT EXISTS fee_rewards (
                reward_id  TEXT PRIMARY KEY,
                winner_id  TEXT,
                amount     INTEGER,
                timestamp  INTEGER,
                time_slot  INTEGER
            );

            CREATE TABLE IF NOT EXISTS checkpoint_balances (
                address         TEXT PRIMARY KEY,
                balance         INTEGER NOT NULL,
                checkpoint_slot INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        try:
            conn.execute(
                "ALTER TABLE checkpoint_balances "
                "ADD COLUMN pre_checkpoint_blocks INTEGER DEFAULT 0"
            )
        except Exception:
            pass
        conn.commit()
        conn.close()


def _load_state():
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        rows = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta").fetchall()}
        conn.close()
    with _tip_lock:
        _tip["chain_tip_slot"] = int(rows.get("chain_tip_slot", -1))
        _tip["chain_tip_hash"] = rows.get("chain_tip_hash", "0" * 64)
        _tip["total_minted"]   = int(rows.get("total_minted",   0))
        _tip["chain_height"]   = int(rows.get("chain_height",   0))


def _migrate_apply_checkpoint_prune():
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = {r[0]: r[1] for r in conn.execute(
                "SELECT key, value FROM meta"
            ).fetchall()}

            checkpoint_slot = int(rows.get("checkpoint_slot", 0))
            if checkpoint_slot <= 0:
                return

            if "prune_before" in rows:
                return

            new_prune_before = max(0, checkpoint_slot - CHECKPOINT_BUFFER)

            overlap_rows = conn.execute(
                "SELECT winner_id, COUNT(*) FROM blocks "
                "WHERE type='block_reward' "
                "AND slot >= ? AND slot < ? "
                "GROUP BY winner_id",
                (new_prune_before, checkpoint_slot)
            ).fetchall()
            overlap = {r[0]: r[1] for r in overlap_rows}

            cp_rows = conn.execute(
                "SELECT address, balance FROM checkpoint_balances"
            ).fetchall()

            updates = []
            for addr, bal in cp_rows:
                total_blocks = bal // REWARD_PER_ROUND
                adj = total_blocks - overlap.get(addr, 0)
                if adj < 0:
                    adj = 0
                updates.append((adj, addr))

            conn.executemany(
                "UPDATE checkpoint_balances "
                "SET pre_checkpoint_blocks=? WHERE address=?",
                updates
            )

            conn.execute(
                "DELETE FROM blocks WHERE slot < ?",
                (new_prune_before,)
            )

            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('prune_before', ?)",
                (str(new_prune_before),)
            )

            conn.commit()
        finally:
            conn.close()


def _to_tmpl(units) -> float:
    try:
        return units / UNIT
    except Exception:
        return 0.0


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
    # BUG 3 FIX: removed sha256(pub_bytes) == device_id check.
    # That check was correct only for genesis-phase wallets where
    # device_id = sha256(public_key). Post-genesis chain-anchored wallets
    # use device_id = sha256(public_key + genesis_block_hash), which never
    # equals sha256(public_key), so every post-genesis node was permanently
    # rejected. The Dilithium3 signature over the full payload (which includes
    # device_id) already proves the sender owns the key — the hash check
    # provided no additional security and actively broke all new nodes.
    try:
        pub_bytes = bytes.fromhex(public_key)
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


def _rebuild_stats_cache() -> dict:
    with _tip_lock:
        total_minted_units = _tip["total_minted"]
        chain_tip_slot     = _tip["chain_tip_slot"]
        chain_tip_hash     = _tip["chain_tip_hash"]

    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='prune_before'"
            ).fetchone()
            prune_before = int(row[0]) if row else 0

            pre_rows = conn.execute(
                "SELECT address, pre_checkpoint_blocks "
                "FROM checkpoint_balances WHERE pre_checkpoint_blocks > 0"
            ).fetchall()
            pre_cp = {r[0]: r[1] for r in pre_rows}

            live_rows = conn.execute(
                "SELECT winner_id, COUNT(*) FROM blocks "
                "WHERE type='block_reward' AND slot >= ? "
                "GROUP BY winner_id",
                (prune_before,)
            ).fetchall()
            live = {r[0]: r[1] for r in live_rows}

            node_counts = {
                nid: pre_cp.get(nid, 0) + live.get(nid, 0)
                for nid in set(pre_cp) | set(live)
            }

            block_rows = conn.execute(
                "SELECT slot, winner_id, amount, timestamp, prev_hash "
                "FROM blocks ORDER BY slot DESC LIMIT 50"
            ).fetchall()

            tx_rows = conn.execute(
                "SELECT tx_id, sender_id, recipient_id, amount, timestamp "
                "FROM transactions ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()

            fr_rows = conn.execute(
                "SELECT winner_id, amount, time_slot, timestamp "
                "FROM fee_rewards ORDER BY time_slot DESC LIMIT 20"
            ).fetchall()

            tx_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        finally:
            conn.close()

    total_minted_tmpl = _to_tmpl(total_minted_units)
    total_r = sum(node_counts.values()) or 1
    vrf_rounds = total_minted_units // REWARD_PER_ROUND

    node_stats = sorted([
        {
            "id":       nid,
            "id_short": nid[:16] + "...",
            "rewards":  cnt,
            "pct":      round(cnt / total_r * 100, 2)
        }
        for nid, cnt in node_counts.items()
    ], key=lambda x: x["rewards"], reverse=True)

    return {
        "total_minted":   round(total_minted_tmpl, 8),
        "remaining":      round(TOTAL_SUPPLY_TMPL - total_minted_tmpl, 8),
        "total_rewards":  vrf_rounds,
        "total_txs":      tx_count,
        "active_nodes":   len(node_counts),
        "chain_height":   vrf_rounds,
        "chain_tip_slot": chain_tip_slot,
        "chain_tip_hash": chain_tip_hash,
        "node_stats":     node_stats,
        "recent_fee_rewards": [
            {
                "winner_id": r[0],
                "amount":    round(_to_tmpl(r[1]), 8),
                "time_slot": r[2],
                "time":      fmt_time(r[3])
            }
            for r in fr_rows
        ],
        "recent_blocks": [
            {
                "id":        r[1],
                "amount":    round(_to_tmpl(r[2]), 8),
                "time":      fmt_time(r[3]),
                "slot":      r[0],
                "prev_hash": (r[4] or "")[:16] + "..." if r[4] else "",
                "confirmed": _is_confirmed(r[0], chain_tip_slot)
            }
            for r in block_rows
        ],
        "recent_txs": [
            {
                "tx_id":     r[0],
                "id":        (r[0] or "")[:16] + "...",
                "sender":    r[1],
                "recipient": r[2],
                "amount":    round(_to_tmpl(r[3]), 8),
                "time":      fmt_time(r[4]),
                "timestamp": r[4]
            }
            for r in tx_rows
        ],
    }


class Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _send_json(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path   = parsed.path.rstrip("/")
            params = urllib.parse.parse_qs(parsed.query)

            if path in ("", "/", "/api", "/api/"):
                with _stats_cache_lock:
                    cache = _stats_cache
                if cache is None:
                    self._send_json(200, {
                        "total_minted": 0, "remaining": TOTAL_SUPPLY_TMPL,
                        "total_rewards": 0, "total_txs": 0, "active_nodes": 0,
                        "chain_height": 0, "chain_tip_slot": -1,
                        "chain_tip_hash": "0" * 64,
                        "node_stats": [], "recent_blocks": [], "recent_txs": [],
                        "tip_slot": -1
                    })
                else:
                    self._send_json(200, cache)

            elif path == "/api/address":
                addr = params.get("id", [""])[0].strip()
                if not addr:
                    self._send_json(400, {"error": "missing id"})
                    return
                if not _is_valid_hex64(addr):
                    self._send_json(400, {"error": "invalid address format"})
                    return

                with _tip_lock:
                    current_slot = _tip["chain_tip_slot"]

                with _db_lock:
                    conn = sqlite3.connect(DB_PATH)
                    try:
                        block_count = conn.execute(
                            "SELECT COUNT(*) FROM blocks "
                            "WHERE winner_id=? AND type='block_reward'",
                            (addr,)
                        ).fetchone()[0]

                        pre_cp_row = conn.execute(
                            "SELECT pre_checkpoint_blocks FROM checkpoint_balances WHERE address=?",
                            (addr,)
                        ).fetchone()
                        pre_cp_blocks = pre_cp_row[0] if pre_cp_row else 0

                        block_rows = conn.execute(
                            "SELECT slot, amount, timestamp, prev_hash FROM blocks "
                            "WHERE winner_id=? AND type='block_reward' "
                            "ORDER BY slot DESC LIMIT 100",
                            (addr,)
                        ).fetchall()

                        sent_rows = conn.execute(
                            "SELECT amount, fee, timestamp, recipient_id, tx_id "
                            "FROM transactions WHERE sender_id=? "
                            "ORDER BY timestamp DESC",
                            (addr,)
                        ).fetchall()

                        recv_rows = conn.execute(
                            "SELECT amount, timestamp, sender_id, tx_id "
                            "FROM transactions WHERE recipient_id=? "
                            "ORDER BY timestamp DESC",
                            (addr,)
                        ).fetchall()
                    finally:
                        conn.close()

                total_rewards = block_count + pre_cp_blocks
                total_earned  = round(total_rewards * _to_tmpl(REWARD_PER_ROUND), 8)
                total_sent    = round(sum(_to_tmpl(r[0]) for r in sent_rows), 8)
                total_recv    = round(sum(_to_tmpl(r[0]) for r in recv_rows), 8)

                all_txs = []
                for r in sent_rows:
                    all_txs.append({
                        "tx_id":        r[4],
                        "direction":    "sent",
                        "counterparty": r[3],
                        "amount":       round(_to_tmpl(r[0]), 8),
                        "time":         fmt_time(r[2]),
                        "timestamp":    r[2]
                    })
                for r in recv_rows:
                    all_txs.append({
                        "tx_id":        r[3],
                        "direction":    "received",
                        "counterparty": r[2],
                        "amount":       round(_to_tmpl(r[0]), 8),
                        "time":         fmt_time(r[1]),
                        "timestamp":    r[1]
                    })
                all_txs.sort(key=lambda t: t.get("timestamp", 0), reverse=True)

                self._send_json(200, {
                    "address":        addr,
                    "total_rewards":  total_rewards,
                    "total_earned":   total_earned,
                    "total_sent":     total_sent,
                    "total_received": total_recv,
                    "blocks": [
                        {
                            "amount":    round(_to_tmpl(r[1]), 8),
                            "time":      fmt_time(r[2]),
                            "slot":      r[0],
                            "prev_hash": r[3] or "",
                            "confirmed": _is_confirmed(r[0], current_slot)
                        }
                        for r in block_rows
                    ],
                    "transactions": all_txs
                })

            elif path == "/api/tx":
                tx_id = params.get("id", [""])[0].strip()
                if not tx_id:
                    self._send_json(400, {"error": "missing id"})
                    return
                if not isinstance(tx_id, str) or len(tx_id) > 64 or not tx_id.replace("-", "").isalnum():
                    self._send_json(400, {"error": "invalid tx_id format"})
                    return
                with _db_lock:
                    conn = sqlite3.connect(DB_PATH)
                    row = conn.execute(
                        "SELECT tx_id, sender_id, recipient_id, amount, fee, timestamp, signature "
                        "FROM transactions WHERE tx_id=?", (tx_id,)
                    ).fetchone()
                    conn.close()
                if not row:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(200, {
                    "tx_id":     row[0],
                    "sender":    row[1],
                    "recipient": row[2],
                    "amount":    round(_to_tmpl(row[3]), 8),
                    "fee":       round(_to_tmpl(row[4] or 0), 8),
                    "timestamp": row[5],
                    "time":      fmt_time(row[5]),
                    "signature": row[6] or "",
                    "confirmed": True
                })

            elif path == "/api/block":
                slot_str = params.get("slot", [""])[0].strip()
                if not slot_str:
                    self._send_json(400, {"error": "missing slot"})
                    return
                try:
                    slot = int(slot_str)
                except ValueError:
                    self._send_json(400, {"error": "invalid slot"})
                    return
                with _tip_lock:
                    current_slot = _tip["chain_tip_slot"]
                with _db_lock:
                    conn = sqlite3.connect(DB_PATH)
                    row = conn.execute(
                        "SELECT slot, winner_id, amount, prev_hash, timestamp, nodes "
                        "FROM blocks WHERE slot=?", (slot,)
                    ).fetchone()
                    conn.close()
                if not row:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(200, {
                    "slot":      row[0],
                    "winner":    row[1],
                    "amount":    round(_to_tmpl(row[2]), 8),
                    "prev_hash": row[3] or "",
                    "time":      fmt_time(row[4]),
                    "timestamp": row[4],
                    "confirmed": _is_confirmed(slot, current_slot),
                    "nodes":     row[5] or 1
                })

            else:
                self._send_json(404, {"error": "not found"})

        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        global _last_update, _stats_cache
        try:
            ip  = self.client_address[0]
            now = time.time()

            with _post_rate_lock:
                times = [t for t in _post_rate.get(ip, []) if now - t < 10]
                if len(times) >= POST_RATE_LIMIT:
                    self._send_json(429, {"error": "rate limit exceeded"})
                    return
                times.append(now)
                _post_rate[ip] = times

            length = int(self.headers.get("Content-Length", 0))
            if length > 2_000_000:
                self._send_json(400, {"error": "payload too large"})
                return

            body = self.rfile.read(length)
            data = json.loads(body.decode())

            if data.get("type") != "LEDGER_PUSH":
                self._send_json(400, {"error": "unknown type"})
                return

            if not _verify_push_signature(data):
                self._send_json(401, {"error": "invalid signature"})
                return

            incoming_blocks = data.get("blocks", [])
            txs             = data.get("transactions", [])

            incoming_blocks = [b for b in incoming_blocks
                               if isinstance(b, dict)
                               and isinstance(b.get("amount"), int)
                               and not isinstance(b.get("amount"), bool)
                               and _is_valid_hex64(b.get("winner_id", ""))
                               and isinstance(b.get("slot"), int)]
            txs = [t for t in txs
                   if isinstance(t, dict)
                   and isinstance(t.get("amount"), int)
                   and not isinstance(t.get("amount"), bool)
                   and isinstance(t.get("tx_id"), str)]

            incoming_tip_hash = data.get("chain_tip_hash")
            incoming_tip_slot = data.get("chain_tip_slot")

            if incoming_tip_hash is not None and not _is_valid_hex64(incoming_tip_hash):
                self._send_json(400, {"error": "invalid chain_tip_hash"})
                return

            if incoming_tip_slot is not None:
                if not isinstance(incoming_tip_slot, int) or incoming_tip_slot < -1:
                    self._send_json(400, {"error": "invalid chain_tip_slot"})
                    return
                payload_ts = data.get("timestamp", 0)
                if isinstance(payload_ts, (int, float)):
                    if payload_ts > time.time() + TIMESTAMP_TOLERANCE:
                        self._send_json(400, {"error": "payload timestamp too far in future"})
                        return
                    if payload_ts > GENESIS_TIME:
                        max_expected = int((payload_ts - GENESIS_TIME) / REWARD_INTERVAL) + 10
                        if incoming_tip_slot > max_expected:
                            self._send_json(400, {"error": "chain_tip_slot exceeds expected range"})
                            return

            if incoming_tip_slot is not None and incoming_blocks:
                br_slots = [b.get("slot") for b in incoming_blocks
                             if b.get("type", "block_reward") == "block_reward"
                             and isinstance(b.get("slot"), int)]
                if br_slots and max(br_slots) != incoming_tip_slot:
                    self._send_json(400, {"error": "chain_tip_slot mismatch with pushed blocks"})
                    return

            if incoming_tip_hash is not None and incoming_blocks:
                br_blocks = [b for b in incoming_blocks
                              if b.get("type", "block_reward") == "block_reward"
                              and isinstance(b.get("slot"), int)]
                if br_blocks:
                    tip_block      = max(br_blocks, key=lambda b: b.get("slot", -1))
                    tip_canon_hash = tip_block.get("canonical_hash", "")
                    if not tip_canon_hash:
                        self._send_json(400, {"error": "missing canonical_hash in tip block"})
                        return
                    if incoming_tip_hash != tip_canon_hash:
                        self._send_json(400, {"error": "chain_tip_hash mismatch with tip block"})
                        return

            incoming_minted = data.get("total_minted", 0)
            incoming_height = data.get("chain_height", 0)

            incoming_cp_balances = data.get("checkpoint_balances", {})
            incoming_cp_slot     = data.get("checkpoint_slot", 0)
            valid_cp = (isinstance(incoming_cp_balances, dict)
                        and isinstance(incoming_cp_slot, int)
                        and incoming_cp_slot > 0)

            with _tip_lock:
                if incoming_minted > _tip["total_minted"]:
                    _tip["total_minted"] = incoming_minted
                if incoming_height > _tip["chain_height"]:
                    _tip["chain_height"] = incoming_height
                if (incoming_tip_hash is not None
                        and incoming_tip_slot is not None
                        and incoming_tip_slot > _tip["chain_tip_slot"]):
                    _tip["chain_tip_slot"] = incoming_tip_slot
                    _tip["chain_tip_hash"] = incoming_tip_hash
                tip_snapshot = dict(_tip)

            with _db_lock:
                conn = sqlite3.connect(DB_PATH)
                try:
                    row = conn.execute(
                        "SELECT value FROM meta WHERE key='prune_before'"
                    ).fetchone()
                    prune_before = int(row[0]) if row else 0

                    for b in incoming_blocks:
                        rtype = b.get("type", "block_reward")
                        if rtype == "block_reward" and not _is_valid_hex64(b.get("vrf_ticket", "")):
                            continue
                        slot = b.get("slot")
                        if slot is None:
                            continue

                        if slot < prune_before:
                            continue

                        if incoming_tip_slot is not None and slot > incoming_tip_slot - RECENT_WINDOW:
                            conn.execute(
                                "INSERT OR REPLACE INTO blocks "
                                "(slot, reward_id, winner_id, amount, timestamp, type, "
                                " prev_hash, vrf_ticket, canonical_hash, nodes) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (slot, b.get("reward_id"), b.get("winner_id"),
                                 int(b.get("amount", 0)), b.get("timestamp"),
                                 rtype, b.get("prev_hash"), b.get("vrf_ticket"),
                                 b.get("canonical_hash"), b.get("nodes", 1))
                            )
                        else:
                            conn.execute(
                                "INSERT OR IGNORE INTO blocks "
                                "(slot, reward_id, winner_id, amount, timestamp, type, "
                                " prev_hash, vrf_ticket, canonical_hash, nodes) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (slot, b.get("reward_id"), b.get("winner_id"),
                                 int(b.get("amount", 0)), b.get("timestamp"),
                                 rtype, b.get("prev_hash"), b.get("vrf_ticket"),
                                 b.get("canonical_hash"), b.get("nodes", 1))
                            )

                    for t in txs:
                        conn.execute(
                            "INSERT OR IGNORE INTO transactions "
                            "(tx_id, sender_id, recipient_id, amount, fee, timestamp, slot, signature) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (t.get("tx_id"), t.get("sender_id"), t.get("recipient_id"),
                             int(t.get("amount", 0)), int(t.get("fee", 0)),
                             t.get("timestamp"), t.get("slot"), t.get("signature"))
                        )

                    for fr in data.get("fee_rewards", []):
                        if (isinstance(fr, dict)
                                and fr.get("reward_id")
                                and isinstance(fr.get("amount"), int)
                                and fr.get("amount", 0) > 0
                                and _is_valid_hex64(fr.get("winner_id", ""))):
                            conn.execute(
                                "INSERT OR IGNORE INTO fee_rewards "
                                "(reward_id, winner_id, amount, timestamp, time_slot) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (fr["reward_id"], fr["winner_id"], fr["amount"],
                                 fr.get("timestamp"), fr.get("time_slot"))
                            )

                    if valid_cp:
                        cur = conn.execute(
                            "SELECT value FROM meta WHERE key='checkpoint_slot'"
                        ).fetchone()
                        cur_cp_slot = int(cur[0]) if cur else 0
                        if incoming_cp_slot <= cur_cp_slot:
                            self._send_json(400, {"error": "stale checkpoint"})
                            return
                        if incoming_cp_slot <= prune_before:
                            self._send_json(400, {"error": "checkpoint does not advance pruning"})
                            return
                        if incoming_cp_slot > cur_cp_slot:
                            new_prune_before = max(0, incoming_cp_slot - CHECKPOINT_BUFFER)
                            old_pre_cp = {r[0]: r[1] for r in conn.execute(
                                "SELECT address, pre_checkpoint_blocks FROM checkpoint_balances"
                            ).fetchall()}
                            # BUG 1 FIX: use prune_before (current DB value) as lower bound,
                            # not cur_cp_slot. Ensures the CHECKPOINT_BUFFER overlap window
                            # [prune_before, new_prune_before) is fully accumulated before deletion.
                            new_block_counts = {r[0]: r[1] for r in conn.execute(
                                "SELECT winner_id, COUNT(*) FROM blocks "
                                "WHERE type='block_reward' AND slot >= ? AND slot < ? GROUP BY winner_id",
                                (prune_before, new_prune_before)
                            ).fetchall()}
                            # Checkpoint integrity: sum of balances must not exceed total_minted.
                            # Catches corrupted or malformed checkpoint payloads before they
                            # are written to DB and history is irreversibly pruned.
                            cp_balance_sum = sum(
                                v for v in incoming_cp_balances.values()
                                if isinstance(v, int) and not isinstance(v, bool)
                            )
                            if cp_balance_sum > incoming_minted:
                                self._send_json(400, {"error": "checkpoint balances exceed total_minted"})
                                return
                            records = []
                            for addr, bal in incoming_cp_balances.items():
                                if not (isinstance(addr, str) and len(addr) == 64):
                                    continue
                                if not (isinstance(bal, int) and bal >= 0):
                                    continue
                                # Per-address sanity: no single address can hold more than total_minted.
                                if bal > incoming_minted:
                                    continue
                                pre_cp = old_pre_cp.get(addr, 0) + new_block_counts.get(addr, 0)
                                records.append((addr, int(bal), incoming_cp_slot, pre_cp))
                            conn.executemany(
                                "INSERT INTO checkpoint_balances "
                                "(address, balance, checkpoint_slot, pre_checkpoint_blocks) "
                                "VALUES (?, ?, ?, ?) "
                                "ON CONFLICT(address) DO UPDATE SET "
                                "balance=excluded.balance, "
                                "checkpoint_slot=excluded.checkpoint_slot, "
                                "pre_checkpoint_blocks=excluded.pre_checkpoint_blocks",
                                [(addr, bal, cp, pre) for addr, bal, cp, pre in records]
                            )
                            conn.execute("DELETE FROM blocks WHERE slot < ?", (new_prune_before,))
                            conn.execute(
                                "INSERT OR REPLACE INTO meta VALUES ('prune_before', ?)",
                                (str(new_prune_before),)
                            )
                            conn.execute(
                                "INSERT OR REPLACE INTO meta VALUES ('checkpoint_slot', ?)",
                                (str(incoming_cp_slot),)
                            )
                            prune_before = new_prune_before

                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('total_minted',   ?)",
                                 (str(tip_snapshot["total_minted"]),))
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('chain_height',   ?)",
                                 (str(tip_snapshot["chain_height"]),))
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('chain_tip_slot', ?)",
                                 (str(tip_snapshot["chain_tip_slot"]),))
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('chain_tip_hash', ?)",
                                 (tip_snapshot["chain_tip_hash"],))
                    conn.commit()
                finally:
                    conn.close()

            new_cache = _rebuild_stats_cache()
            with _stats_cache_lock:
                _stats_cache = new_cache

            _last_update = time.time()
            self._send_json(200, {"ok": True})

        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    _init_db()
    _load_state()
    _migrate_apply_checkpoint_prune()
    startup_cache = _rebuild_stats_cache()
    with _stats_cache_lock:
        _stats_cache = startup_cache
    threading.Thread(target=_clean_post_rate, daemon=True).start()
    print("TIMPAL API v3.3 (SQLite) running on port 7781")
    print(f"Database : {DB_PATH}")
    print("Auth     : Dilithium3 push signature")
    print("Storage  : Persistent — data survives restarts")
    server = ThreadingHTTPServer(("0.0.0.0", 7781), Handler)
    server.serve_forever()
