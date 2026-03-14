"""TIMPAL API — serves live ledger data for timpal.org explorer.
Nodes push updates to this API — no NAT issues, fully decentralized data."""

import json, os, time, threading, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

_ledger      = {"rewards": [], "transactions": []}
_ledger_lock = threading.Lock()
_last_update = 0

def fmt_time(ts):
    if not ts: return ""
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

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

            with _ledger_lock:
                rewards = list(_ledger["rewards"])
                txs     = list(_ledger["transactions"])

            if path in ("", "/", "/api", "/api/"):
                total_minted   = round(sum(r.get("amount", 0) for r in rewards), 8)
                recent_rewards = sorted(rewards, key=lambda r: r.get("timestamp", 0), reverse=True)[:50]
                recent_txs     = sorted(txs,     key=lambda t: t.get("timestamp", 0), reverse=True)[:50]
                node_counts = {}
                for r in rewards:
                    wid = r.get("winner_id", "")
                    if wid:
                        node_counts[wid] = node_counts.get(wid, 0) + 1
                total_r    = len(rewards) or 1
                node_stats = sorted([
                    {"id": nid, "id_short": nid[:16]+"...", "rewards": cnt,
                     "pct": round(cnt/total_r*100, 2)}
                    for nid, cnt in node_counts.items()
                ], key=lambda x: x["rewards"], reverse=True)
                data = {
                    "total_minted":   total_minted,
                    "remaining":      round(250000000 - total_minted, 8),
                    "total_rewards":  len(rewards),
                    "total_txs":      len(txs),
                    "active_nodes":   len(node_counts),
                    "node_stats":     node_stats,
                    "recent_rewards": [
                        {"id": r.get("winner_id",""), "amount": r.get("amount",0),
                         "time": fmt_time(r.get("timestamp")), "slot": r.get("time_slot","")}
                        for r in recent_rewards
                    ],
                    "recent_txs": [
                        {"tx_id": t.get("tx_id",""), "id": t.get("tx_id","")[:16]+"...",
                         "sender": t.get("sender_id",""), "recipient": t.get("recipient_id",""),
                         "amount": t.get("amount",0), "time": fmt_time(t.get("timestamp")),
                         "timestamp": t.get("timestamp",0)}
                        for t in recent_txs
                    ]
                }
                self.wfile.write(json.dumps(data).encode())

            elif path == "/api/address":
                addr = params.get("id", [""])[0].strip()
                if not addr:
                    self.wfile.write(json.dumps({"error": "missing id"}).encode()); return
                addr_rewards  = sorted([r for r in rewards if r.get("winner_id","") == addr],
                                       key=lambda r: r.get("timestamp",0), reverse=True)
                addr_txs_sent = [t for t in txs if t.get("sender_id","") == addr]
                addr_txs_recv = [t for t in txs if t.get("recipient_id","") == addr]
                addr_txs      = sorted(addr_txs_sent + addr_txs_recv,
                                       key=lambda t: t.get("timestamp",0), reverse=True)
                data = {
                    "address":        addr,
                    "total_rewards":  len(addr_rewards),
                    "total_earned":   round(sum(r.get("amount",0) for r in addr_rewards), 8),
                    "total_sent":     round(sum(t.get("amount",0) for t in addr_txs_sent), 8),
                    "total_received": round(sum(t.get("amount",0) for t in addr_txs_recv), 8),
                    "rewards": [
                        {"amount": r.get("amount",0), "time": fmt_time(r.get("timestamp")),
                         "slot": r.get("time_slot","")}
                        for r in addr_rewards[:100]
                    ],
                    "transactions": [
                        {"tx_id": t.get("tx_id",""),
                         "direction": "sent" if t.get("sender_id") == addr else "received",
                         "counterparty": t.get("recipient_id","") if t.get("sender_id") == addr else t.get("sender_id",""),
                         "amount": t.get("amount",0), "time": fmt_time(t.get("timestamp")),
                         "timestamp": t.get("timestamp",0)}
                        for t in addr_txs
                    ]
                }
                self.wfile.write(json.dumps(data).encode())

            elif path == "/api/tx":
                tx_id = params.get("id", [""])[0].strip()
                if not tx_id:
                    self.wfile.write(json.dumps({"error": "missing id"}).encode()); return
                tx = next((t for t in txs if t.get("tx_id","") == tx_id), None)
                if not tx:
                    self.wfile.write(json.dumps({"error": "not found"}).encode()); return
                self.wfile.write(json.dumps({
                    "tx_id": tx.get("tx_id",""), "sender": tx.get("sender_id",""),
                    "recipient": tx.get("recipient_id",""), "amount": tx.get("amount",0),
                    "timestamp": tx.get("timestamp",0), "time": fmt_time(tx.get("timestamp")),
                    "signature": tx.get("signature",""), "confirmed": True
                }).encode())

            else:
                self.wfile.write(json.dumps({"error": "not found"}).encode())

        except Exception as e:
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_POST(self):
        """Nodes push ledger updates here."""
        global _last_update
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            data   = json.loads(body.decode())

            if data.get("type") == "LEDGER_PUSH":
                rewards = data.get("rewards", [])
                txs     = data.get("transactions", [])
                with _ledger_lock:
                    # Merge rewards — one per slot, lowest ticket wins
                    existing_slots = {r.get("time_slot"): r for r in _ledger["rewards"]}
                    for r in rewards:
                        slot = r.get("time_slot")
                        if slot:
                            existing = existing_slots.get(slot)
                            if not existing:
                                _ledger["rewards"].append(r)
                                existing_slots[slot] = r
                            elif r.get("vrf_ticket","z") < existing.get("vrf_ticket","z"):
                                _ledger["rewards"] = [x for x in _ledger["rewards"] if x.get("time_slot") != slot]
                                _ledger["rewards"].append(r)
                                existing_slots[slot] = r
                        else:
                            if not any(x.get("reward_id") == r.get("reward_id") for x in _ledger["rewards"]):
                                _ledger["rewards"].append(r)
                    # Merge transactions — dedup by tx_id
                    existing_txids = {t.get("tx_id") for t in _ledger["transactions"]}
                    for t in txs:
                        if t.get("tx_id") not in existing_txids:
                            _ledger["transactions"].append(t)
                            existing_txids.add(t.get("tx_id"))
                _last_update = time.time()
                self.wfile.write(json.dumps({"ok": True}).encode())
            else:
                self.wfile.write(json.dumps({"error": "unknown type"}).encode())
        except Exception as e:
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    print("TIMPAL API running on port 7781 — waiting for node pushes")
    server = HTTPServer(("0.0.0.0", 7781), Handler)
    server.serve_forever()
