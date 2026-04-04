#!/usr/bin/env python3
"""
TIMPAL Explorer API v4.0 — SQLite-backed explorer for timpal.org

v4.0 changes from v3.3:
  - Updated for v4.0 block structure: compete_sig, compete_proof,
    fees_collected, block_sig fields
  - GET /api/status endpoint: live network health + registration freeze state
  - /api/address returns finalized field on transactions
  - registration_freeze status field from LEDGER_PUSH
  - Explorer homepage freeze banner driven by status endpoint
  - LEDGER_PUSH now carries compete_sig, compete_proof, fees_collected,
    registration_freeze
  - _verify_push_signature: sha256(pubkey)==device_id check PERMANENTLY ABSENT
    (carried from Session 19 BUG 3 fix — never re-add this check)
  - Updated constants: TOTAL_SUPPLY = 12,500,000,000,000,000 (125M TMPL)
  - Tiered fee model: calculate_fee() for display, not flat MIN_TX_FEE
  - CONFIRMATION_DEPTH = 3 (30 second finality)
  - snapshots table records freeze_active per slot
  - LEDGER_PUSH rejected if node version < MIN_VERSION (blocks old nodes)
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

# ── Constants (must match timpal.py) ──────────────────────────────────────────
UNIT               = 100_000_000
TOTAL_SUPPLY       = 12_500_000_000_000_000   # 125,000,000 TMPL
TOTAL_SUPPLY_TMPL  = 125_000_000.0
REWARD_PER_ROUND   = 105_750_000              # 1.0575 TMPL
CHECKPOINT_BUFFER  = 120
CONFIRMATION_DEPTH = 3
GENESIS_TIME       = 0    # ← must match timpal.py
REWARD_INTERVAL    = 10.0
TX_FEE_RATE        = 0.001
TX_FEE_MIN         = 10_000
TX_FEE_MAX         = 1_000_000
TIMESTAMP_TOLERANCE= 30
MIN_VERSION        = "4.0"   # pushes from older nodes are rejected outright

DB_PATH  = os.path.expanduser("~/.timpal_explorer.db")
_db_lock = threading.Lock()

_tip_lock = threading.Lock()
_tip = {
    "chain_tip_slot":    -1,
    "chain_tip_hash":    "0" * 64,
    "total_minted":      0,
    "chain_height":      0,
    "freeze_active":     False,
    "freeze_status":     {},
}

_stats_cache      = None
_stats_cache_lock = threading.Lock()
_last_update      = 0

_post_rate      = {}
_post_rate_lock = threading.Lock()
POST_RATE_LIMIT = 5


def _ver(v: str) -> tuple:
    """Parse version string into comparable tuple. Returns (0,0) on error."""
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0, 0)


def _calculate_fee(amount: int) -> int:
    fee = int(amount * TX_FEE_RATE)
    return max(TX_FEE_MIN, min(TX_FEE_MAX, fee))


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
                fees_collected INTEGER DEFAULT 0,
                timestamp      INTEGER,
                type           TEXT DEFAULT 'block_reward',
                prev_hash      TEXT,
                compete_proof  TEXT,
                canonical_hash TEXT,
                nodes          INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_blocks_winner ON blocks(winner_id);

            CREATE TABLE IF NOT EXISTS transactions (
                tx_id        TEXT PRIMARY KEY,
                sender_id    TEXT,
                recipient_id TEXT,
                amount       INTEGER,
                fee          INTEGER,
                memo         TEXT DEFAULT '',
                timestamp    REAL,
                slot         INTEGER,
                signature    TEXT,
                finalized    INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_txs_sender    ON transactions(sender_id);
            CREATE INDEX IF NOT EXISTS idx_txs_recipient ON transactions(recipient_id);

            CREATE TABLE IF NOT EXISTS checkpoint_balances (
                address         TEXT PRIMARY KEY,
                balance         INTEGER NOT NULL,
                checkpoint_slot INTEGER NOT NULL,
                pre_checkpoint_blocks INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                slot            INTEGER PRIMARY KEY,
                total_minted    INTEGER,
                total_nodes     INTEGER,
                freeze_active   INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
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
        _tip["total_minted"]   = int(rows.get("total_minted", 0))
        _tip["chain_height"]   = int(rows.get("chain_height", 0))


def _to_tmpl(units) -> float:
    try:
        return units / UNIT
    except Exception:
        return 0.0


def _is_valid_hex64(s) -> bool:
    return isinstance(s, str) and len(s) == 64 and all(c in "0123456789abcdef" for c in s)


def _verify_push_signature(data: dict) -> bool:
    """Verify Dilithium3 signature on LEDGER_PUSH.

    CRITICAL: does NOT check sha256(pub_bytes) == device_id.
    That check breaks all post-genesis chain-anchored wallets.
    The Dilithium3 signature over the full payload is sufficient proof.
    This check is PERMANENTLY ABSENT — never re-add it.
    """
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
    except Exception:
        return False
    payload_data  = {k: v for k, v in data.items() if k != "signature"}
    try:
        payload_bytes = json.dumps(
            payload_data, sort_keys=True, separators=(",", ":")
        ).encode()
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
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


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
        freeze_active      = _tip.get("freeze_active", False)
        freeze_status      = _tip.get("freeze_status", {})

    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='prune_before'").fetchone()
            prune_before = int(row[0]) if row else 0

            pre_rows = conn.execute(
                "SELECT address, pre_checkpoint_blocks FROM checkpoint_balances "
                "WHERE pre_checkpoint_blocks > 0"
            ).fetchall()
            pre_cp = {r[0]: r[1] for r in pre_rows}

            live_rows = conn.execute(
                "SELECT winner_id, COUNT(*) FROM blocks "
                "WHERE type='block_reward' AND slot >= ? GROUP BY winner_id",
                (prune_before,)
            ).fetchall()
            live = {r[0]: r[1] for r in live_rows}

            node_counts = {
                nid: pre_cp.get(nid, 0) + live.get(nid, 0)
                for nid in set(pre_cp) | set(live)
            }

            block_rows = conn.execute(
                "SELECT slot, winner_id, amount, fees_collected, timestamp, prev_hash "
                "FROM blocks ORDER BY slot DESC LIMIT 50"
            ).fetchall()

            tx_rows = conn.execute(
                "SELECT tx_id, sender_id, recipient_id, amount, timestamp, memo "
                "FROM transactions ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()

            tx_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

            # TPS stats
            now_slot = int((time.time() - GENESIS_TIME) / REWARD_INTERVAL) if GENESIS_TIME > 0 else 0
            recent_tx_count = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE slot >= ?",
                (max(0, now_slot - 10),)
            ).fetchone()[0]
            tps_current  = round(recent_tx_count / max(1, 10 * REWARD_INTERVAL), 2)
            tps_capacity = round(500 / REWARD_INTERVAL, 2)

        finally:
            conn.close()

    total_minted_tmpl = _to_tmpl(total_minted_units)
    vrf_rounds        = total_minted_units // REWARD_PER_ROUND if total_minted_units else 0
    total_r           = sum(node_counts.values()) or 1

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
        "total_minted":       round(total_minted_tmpl, 8),
        "remaining":          round(TOTAL_SUPPLY_TMPL - total_minted_tmpl, 8),
        "total_rewards":      vrf_rounds,
        "total_txs":          tx_count,
        "active_nodes":       len(node_counts),
        "chain_height":       vrf_rounds,
        "chain_tip_slot":     chain_tip_slot,
        "chain_tip_hash":     chain_tip_hash,
        "registration_freeze":freeze_status,
        "node_stats":         node_stats,
        "recent_blocks": [
            {
                "id":             r[1],
                "amount":         round(_to_tmpl(r[2]), 8),
                "fees_collected": round(_to_tmpl(r[3] or 0), 8),
                "time":           fmt_time(r[4]),
                "slot":           r[0],
                "prev_hash":      (r[5] or "")[:16] + "..." if r[5] else "",
                "confirmed":      _is_confirmed(r[0], chain_tip_slot)
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
                "timestamp": r[4],
                "memo":      r[5] or ""
            }
            for r in tx_rows
        ],
        "tps_current":  tps_current,
        "tps_capacity": tps_capacity,
        "era": 2 if total_minted_units >= 12_500_000_000_000_000 else 1,
        "era_progress": f"{round(total_minted_tmpl / TOTAL_SUPPLY_TMPL * 100, 4)}% of Era 1 supply distributed"
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

            # GET /api — network statistics
            if path in ("", "/", "/api", "/api/"):
                with _stats_cache_lock:
                    cache = _stats_cache
                if cache is None:
                    self._send_json(200, {
                        "total_minted": 0, "remaining": TOTAL_SUPPLY_TMPL,
                        "total_rewards": 0, "total_txs": 0, "active_nodes": 0,
                        "chain_height": 0, "chain_tip_slot": -1,
                        "chain_tip_hash": "0" * 64,
                        "registration_freeze": {"active": False},
                        "node_stats": [], "recent_blocks": [], "recent_txs": [],
                        "tps_current": 0, "tps_capacity": 50.0, "era": 1,
                        "era_progress": "0% of Era 1 supply distributed"
                    })
                else:
                    self._send_json(200, cache)

            # GET /api/status — live network health + freeze status
            elif path == "/api/status":
                with _tip_lock:
                    total_minted   = _tip["total_minted"]
                    tip_slot       = _tip["chain_tip_slot"]
                    freeze_active  = _tip.get("freeze_active", False)
                    freeze_status  = _tip.get("freeze_status", {})

                era2 = total_minted >= TOTAL_SUPPLY
                self._send_json(200, {
                    "network_healthy":       not freeze_active,
                    "registration_freeze":   freeze_status,
                    "total_nodes":           0,
                    "chain_tip_slot":        tip_slot,
                    "finalized_slot":        max(-1, tip_slot - CONFIRMATION_DEPTH),
                    "era":                   2 if era2 else 1,
                    "era_progress": f"{round(_to_tmpl(total_minted) / TOTAL_SUPPLY_TMPL * 100, 4)}% of Era 1 supply distributed",
                    "tps_current":  0,
                    "tps_capacity": 50.0
                })

            # GET /api/address?id=<device_id>
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
                    finalized_slot = max(-1, current_slot - CONFIRMATION_DEPTH)

                with _db_lock:
                    conn = sqlite3.connect(DB_PATH)
                    try:
                        block_count = conn.execute(
                            "SELECT COUNT(*) FROM blocks WHERE winner_id=? AND type='block_reward'",
                            (addr,)
                        ).fetchone()[0]

                        pre_cp_row = conn.execute(
                            "SELECT pre_checkpoint_blocks FROM checkpoint_balances WHERE address=?",
                            (addr,)
                        ).fetchone()
                        pre_cp_blocks = pre_cp_row[0] if pre_cp_row else 0

                        block_rows = conn.execute(
                            "SELECT slot, amount, fees_collected, timestamp "
                            "FROM blocks WHERE winner_id=? AND type='block_reward' "
                            "ORDER BY slot DESC LIMIT 100",
                            (addr,)
                        ).fetchall()

                        sent_rows = conn.execute(
                            "SELECT amount, fee, memo, timestamp, recipient_id, tx_id, slot "
                            "FROM transactions WHERE sender_id=? ORDER BY timestamp DESC",
                            (addr,)
                        ).fetchall()

                        recv_rows = conn.execute(
                            "SELECT amount, memo, timestamp, sender_id, tx_id, slot "
                            "FROM transactions WHERE recipient_id=? ORDER BY timestamp DESC",
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
                        "tx_id":        r[5],
                        "direction":    "sent",
                        "counterparty": r[4],
                        "amount":       round(_to_tmpl(r[0]), 8),
                        "fee":          round(_to_tmpl(r[1] or 0), 8),
                        "memo":         r[2] or "",
                        "time":         fmt_time(r[3]),
                        "timestamp":    r[3],
                        "slot":         r[6],
                        "confirmed":    True,
                        "finalized":    (r[6] or 0) <= finalized_slot
                    })
                for r in recv_rows:
                    all_txs.append({
                        "tx_id":        r[4],
                        "direction":    "received",
                        "counterparty": r[3],
                        "amount":       round(_to_tmpl(r[0]), 8),
                        "memo":         r[1] or "",
                        "time":         fmt_time(r[2]),
                        "timestamp":    r[2],
                        "slot":         r[5],
                        "confirmed":    True,
                        "finalized":    (r[5] or 0) <= finalized_slot
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
                            "slot":           r[0],
                            "amount":         round(_to_tmpl(r[1]), 8),
                            "fees_collected": round(_to_tmpl(r[2] or 0), 8),
                            "time":           fmt_time(r[3]),
                            "confirmed":      _is_confirmed(r[0], current_slot),
                            "finalized":      r[0] <= finalized_slot
                        }
                        for r in block_rows
                    ],
                    "transactions": all_txs
                })

            # GET /api/block?slot=<slot>
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
                    current_slot  = _tip["chain_tip_slot"]
                    finalized_slot= max(-1, current_slot - CONFIRMATION_DEPTH)
                with _db_lock:
                    conn = sqlite3.connect(DB_PATH)
                    row = conn.execute(
                        "SELECT slot, winner_id, amount, fees_collected, prev_hash, "
                        "timestamp, nodes, compete_proof "
                        "FROM blocks WHERE slot=?", (slot,)
                    ).fetchone()
                    conn.close()
                if not row:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(200, {
                    "slot":           row[0],
                    "winner":         row[1],
                    "amount":         round(_to_tmpl(row[2]), 8),
                    "fees_collected": round(_to_tmpl(row[3] or 0), 8),
                    "prev_hash":      row[4] or "",
                    "time":           fmt_time(row[5]),
                    "timestamp":      row[5],
                    "nodes":          row[6] or 1,
                    "compete_proof":  row[7] or "",
                    "confirmed":      _is_confirmed(slot, current_slot),
                    "finalized":      slot <= finalized_slot
                })

            # GET /api/tx?id=<tx_id>
            elif path == "/api/tx":
                tx_id = params.get("id", [""])[0].strip()
                if not tx_id:
                    self._send_json(400, {"error": "missing id"})
                    return
                if not isinstance(tx_id, str) or len(tx_id) > 64:
                    self._send_json(400, {"error": "invalid tx_id"})
                    return
                with _tip_lock:
                    current_slot  = _tip["chain_tip_slot"]
                    finalized_slot= max(-1, current_slot - CONFIRMATION_DEPTH)
                with _db_lock:
                    conn = sqlite3.connect(DB_PATH)
                    row = conn.execute(
                        "SELECT tx_id, sender_id, recipient_id, amount, fee, "
                        "memo, timestamp, slot, signature "
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
                    "memo":      row[5] or "",
                    "timestamp": row[6],
                    "time":      fmt_time(row[6]),
                    "slot":      row[7],
                    "signature": row[8] or "",
                    "confirmed": True,
                    "finalized": (row[7] or 0) <= finalized_slot
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

            # Version check — reject pushes from nodes below MIN_VERSION.
            push_version = data.get("version", "0.0")
            if _ver(push_version) < _ver(MIN_VERSION):
                self._send_json(400, {
                    "error": f"node version {push_version} below minimum {MIN_VERSION} — update required"
                })
                return

            if not _verify_push_signature(data):
                self._send_json(401, {"error": "invalid signature"})
                return

            # Validate and extract fields
            incoming_blocks = [
                b for b in data.get("blocks", [])
                if isinstance(b, dict)
                and isinstance(b.get("amount"), int)
                and not isinstance(b.get("amount"), bool)
                and _is_valid_hex64(b.get("winner_id", ""))
                and isinstance(b.get("slot"), int)
            ]
            txs = [
                t for t in data.get("transactions", [])
                if isinstance(t, dict)
                and isinstance(t.get("amount"), int)
                and not isinstance(t.get("amount"), bool)
                and isinstance(t.get("tx_id"), str)
            ]

            incoming_tip_hash   = data.get("chain_tip_hash")
            incoming_tip_slot   = data.get("chain_tip_slot")
            incoming_minted     = data.get("total_minted", 0)
            incoming_height     = data.get("chain_height", 0)
            incoming_cp_balances= data.get("checkpoint_balances", {})
            incoming_cp_slot    = data.get("checkpoint_slot", 0)
            freeze_status       = data.get("registration_freeze", {})

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
                        self._send_json(400, {"error": "timestamp too far in future"})
                        return

            # Update tip
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
                if freeze_status:
                    _tip["freeze_active"] = freeze_status.get("active", False)
                    _tip["freeze_status"] = freeze_status
                tip_snapshot = dict(_tip)

            with _db_lock:
                conn = sqlite3.connect(DB_PATH)
                try:
                    row = conn.execute("SELECT value FROM meta WHERE key='prune_before'").fetchone()
                    prune_before = int(row[0]) if row else 0

                    RECENT_WINDOW = 50
                    for b in incoming_blocks:
                        slot  = b.get("slot")
                        rtype = b.get("type", "block_reward")
                        if slot is None or slot < prune_before:
                            continue
                        if (incoming_tip_slot is not None
                                and slot > incoming_tip_slot - RECENT_WINDOW):
                            conn.execute(
                                "INSERT OR REPLACE INTO blocks "
                                "(slot, reward_id, winner_id, amount, fees_collected, "
                                " timestamp, type, prev_hash, compete_proof, canonical_hash, nodes) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (slot, b.get("reward_id"), b.get("winner_id"),
                                 int(b.get("amount", 0)),
                                 int(b.get("fees_collected", 0)),
                                 b.get("timestamp"), rtype,
                                 b.get("prev_hash"), b.get("compete_proof"),
                                 b.get("canonical_hash"), b.get("nodes", 1))
                            )
                        else:
                            conn.execute(
                                "INSERT OR IGNORE INTO blocks "
                                "(slot, reward_id, winner_id, amount, fees_collected, "
                                " timestamp, type, prev_hash, compete_proof, canonical_hash, nodes) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (slot, b.get("reward_id"), b.get("winner_id"),
                                 int(b.get("amount", 0)),
                                 int(b.get("fees_collected", 0)),
                                 b.get("timestamp"), rtype,
                                 b.get("prev_hash"), b.get("compete_proof"),
                                 b.get("canonical_hash"), b.get("nodes", 1))
                            )

                    for t in txs:
                        conn.execute(
                            "INSERT OR IGNORE INTO transactions "
                            "(tx_id, sender_id, recipient_id, amount, fee, memo, "
                            " timestamp, slot, signature) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (t.get("tx_id"), t.get("sender_id"), t.get("recipient_id"),
                             int(t.get("amount", 0)), int(t.get("fee", 0)),
                             t.get("memo", ""), t.get("timestamp"),
                             t.get("slot"), t.get("signature"))
                        )

                    # Snapshot
                    if incoming_tip_slot and incoming_tip_slot > 0:
                        conn.execute(
                            "INSERT OR IGNORE INTO snapshots (slot, total_minted, total_nodes, freeze_active) "
                            "VALUES (?, ?, ?, ?)",
                            (incoming_tip_slot, incoming_minted,
                             len(incoming_cp_balances) if incoming_cp_balances else 0,
                             1 if freeze_status.get("active") else 0)
                        )

                    # Checkpoint balances
                    valid_cp = (isinstance(incoming_cp_balances, dict)
                                and isinstance(incoming_cp_slot, int)
                                and incoming_cp_slot > 0)
                    if valid_cp:
                        cur = conn.execute(
                            "SELECT value FROM meta WHERE key='checkpoint_slot'"
                        ).fetchone()
                        cur_cp_slot = int(cur[0]) if cur else 0
                        if incoming_cp_slot > cur_cp_slot:
                            new_prune_before = max(0, incoming_cp_slot - CHECKPOINT_BUFFER)

                            # FIX 7: verify checkpoint balances are self-consistent.
                            # (1) Total sum must not exceed total_minted.
                            # (2) No single address balance may exceed total_minted —
                            #     this catches per-address inflation that is masked by
                            #     an equal deflation elsewhere (sum-neutral attack).
                            cp_balance_sum = sum(
                                v for v in incoming_cp_balances.values()
                                if isinstance(v, int) and not isinstance(v, bool)
                            )
                            if cp_balance_sum > incoming_minted:
                                self._send_json(400, {"error": "checkpoint balances exceed total_minted"})
                                return
                            if any(
                                isinstance(v, int) and not isinstance(v, bool) and v > incoming_minted
                                for v in incoming_cp_balances.values()
                            ):
                                self._send_json(400, {"error": "single balance exceeds total_minted"})
                                return

                            new_block_counts = {r[0]: r[1] for r in conn.execute(
                                "SELECT winner_id, COUNT(*) FROM blocks "
                                "WHERE type='block_reward' AND slot >= ? AND slot < ? "
                                "GROUP BY winner_id",
                                (prune_before, new_prune_before)
                            ).fetchall()}
                            old_pre_cp = {r[0]: r[1] for r in conn.execute(
                                "SELECT address, pre_checkpoint_blocks FROM checkpoint_balances"
                            ).fetchall()}

                            records = []
                            for addr, bal in incoming_cp_balances.items():
                                if not (isinstance(addr, str) and len(addr) == 64):
                                    continue
                                if not (isinstance(bal, int) and bal >= 0):
                                    continue
                                if bal > incoming_minted:
                                    continue
                                pre = old_pre_cp.get(addr, 0) + new_block_counts.get(addr, 0)
                                records.append((addr, int(bal), incoming_cp_slot, pre))
                            conn.executemany(
                                "INSERT INTO checkpoint_balances "
                                "(address, balance, checkpoint_slot, pre_checkpoint_blocks) "
                                "VALUES (?, ?, ?, ?) "
                                "ON CONFLICT(address) DO UPDATE SET "
                                "balance=excluded.balance, "
                                "checkpoint_slot=excluded.checkpoint_slot, "
                                "pre_checkpoint_blocks=excluded.pre_checkpoint_blocks",
                                records
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

                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('total_minted', ?)",
                                 (str(tip_snapshot["total_minted"]),))
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('chain_height', ?)",
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
    startup_cache = _rebuild_stats_cache()
    with _stats_cache_lock:
        _stats_cache = startup_cache
    threading.Thread(target=_clean_post_rate, daemon=True).start()
    print("TIMPAL Explorer API v4.0 running on port 7781")
    print(f"Database: {DB_PATH}")
    print(f"Auth    : Dilithium3 push signature | Min version: {MIN_VERSION}")
    print("Endpoints: /api /api/address /api/block /api/tx /api/status")
    server = ThreadingHTTPServer(("0.0.0.0", 7781), Handler)
    server.serve_forever()
