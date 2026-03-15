#!/usr/bin/env python3
"""
TIMPAL Protocol v2.0 — Plan B for Humanity

Quantum-resistant. Worldwide. Instant transactions.
Distributed ledger. No banks. No servers. No control.

Install dependencies:
    pip3 install dilithium-py

Run:
    python3 timpal.py
"""

import socket
import threading
import json
import hashlib
import os
import time
import uuid
import random

# Quantum-resistant cryptography — Dilithium3 (NIST PQC Standard 2024)
try:
    from dilithium_py.dilithium import Dilithium3
    QUANTUM_RESISTANT = True
except ImportError:
    print("\n  [!] dilithium-py not installed.")
    print("  Run: pip3 install dilithium-py")
    print("  Then restart Timpal.\n")
    exit(1)

# ─────────────────────────────────────────────
# PROTOCOL CONSTANTS — NEVER CHANGE
# ─────────────────────────────────────────────
VERSION            = "2.0"
MIN_VERSION        = "2.0"   # Minimum version allowed to connect
BOOTSTRAP_SERVERS   = [
    ("5.78.187.91", 7777),   # Timpal foundation — always running
    # Community bootstrap servers can be added here
]
BOOTSTRAP_HOST      = "5.78.187.91"   # Primary (used for peer registration)
BOOTSTRAP_PORT      = 7777
BROADCAST_PORT     = 7778
DISCOVERY_INTERVAL = 5
WALLET_FILE        = __import__("os").path.join(__import__("os").path.expanduser("~"), ".timpal_wallet.json")
LEDGER_FILE        = __import__("os").path.join(__import__("os").path.expanduser("~"), ".timpal_ledger.json")

# Supply constants
TOTAL_SUPPLY       = 250_000_000.0   # 250 million TMPL total
REWARD_PER_ROUND   = 1.0575          # TMPL per 5-second round
REWARD_INTERVAL    = 5.0             # Seconds between reward rounds (increased from 3s for fair ticket collection)
TX_FEE             = 0.0             # Free for first 37.5 years
TX_FEE_ERA2        = 0.0005          # Fee after all coins distributed — split among active nodes
CHECKPOINT_INTERVAL = 241_920        # Slots between checkpoints (2 weeks)
CHECKPOINT_BUFFER   = 120            # Slots to wait before pruning (10 minutes)
PUSH_SECRET         = "b7e2f4a1c9d3e8f2a5b1c4d7e0f3a6b9c2d5e8f1a4b7c0d3e6f9a2b5c8d1e4f7"   # Explorer push authentication

def get_current_fee(total_minted: float) -> float:
    """Era 1 (0-250M minted): Free. Era 2 (250M+ minted): 0.0005 TMPL per tx."""
    if total_minted >= TOTAL_SUPPLY:
        return TX_FEE_ERA2
    return TX_FEE

def is_era2(total_minted: float) -> bool:
    return total_minted >= TOTAL_SUPPLY

def _ver(v: str) -> tuple:
    """Parse version string into comparable tuple. e.g. '2.1' -> (2, 1)"""
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0, 0)


def find_free_port(start=7779):
    for port in range(start, start + 20):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError("No free port found")


# ─────────────────────────────────────────────
# LEDGER — Shared truth of the network
# ─────────────────────────────────────────────

class Ledger:
    def __init__(self):
        self.transactions  = []   # All transactions ever
        self.rewards       = []   # All node rewards ever
        self.total_minted  = 0.0
        self.checkpoints   = []   # Checkpoint snapshots
        self._lock         = threading.RLock()
        self._load()

    def _load(self):
        if os.path.exists(LEDGER_FILE):
            try:
                with open(LEDGER_FILE, "r") as f:
                    data = json.load(f)
                self.transactions = data.get("transactions", [])
                self.rewards      = data.get("rewards", [])
                self.total_minted = data.get("total_minted", 0.0)
                self.checkpoints  = data.get("checkpoints", [])
            except Exception:
                pass

    def save(self):
        with open(LEDGER_FILE, "w") as f:
            json.dump({
                "version":      VERSION,
                "transactions": self.transactions,
                "rewards":      self.rewards,
                "total_minted": self.total_minted,
                "checkpoints":  self.checkpoints
            }, f, indent=2)

    def get_balance(self, device_id: str) -> float:
        """Calculate balance from checkpoint snapshot + post-checkpoint history.
        In Era 2, sender pays amount + fee. Fee rewards credited separately."""
        with self._lock:
            balance = 0.0
            if self.checkpoints:
                balance = self.checkpoints[-1]["balances"].get(device_id, 0.0)
            for tx in self.transactions:
                if tx["recipient_id"] == device_id:
                    balance += tx["amount"]
                if tx["sender_id"] == device_id:
                    balance -= tx["amount"]
                    balance -= tx.get("fee", 0.0)   # Era 2: deduct fee from sender
            for reward in self.rewards:
                if reward["winner_id"] == device_id:
                    balance += reward["amount"]
            return round(balance, 8)

    def has_transaction(self, tx_id: str) -> bool:
        if self.checkpoints:
            if tx_id in self.checkpoints[-1].get("spent_tx_ids", []):
                return True
        return any(tx["tx_id"] == tx_id for tx in self.transactions)

    def can_spend(self, device_id: str, amount: float) -> bool:
        return self.get_balance(device_id) >= amount

    def add_transaction(self, tx_dict: dict) -> bool:
        """Add a verified transaction to the ledger.
        In Era 2, sender must have enough balance for amount + fee."""
        with self._lock:
            if self.has_transaction(tx_dict["tx_id"]):
                return False
            fee   = tx_dict.get("fee", 0.0)
            total = tx_dict["amount"] + fee
            if not self.can_spend(tx_dict["sender_id"], total):
                return False
            self.transactions.append(tx_dict)
            self.save()
            return True

    def add_fee_reward(self, slot: int, node_id: str, amount: float) -> bool:
        """Add an Era 2 fee reward to a node for participating in a slot.
        Fee rewards are split equally among all nodes active in the slot."""
        with self._lock:
            reward_id = f"fee:{slot}:{node_id}"
            if any(r.get("reward_id") == reward_id for r in self.rewards):
                return False
            if amount <= 0:
                return False
            entry = {
                "reward_id":  reward_id,
                "winner_id":  node_id,
                "amount":     round(amount, 8),
                "timestamp":  time.time(),
                "time_slot":  slot,
                "type":       "fee_reward"
            }
            self.rewards.append(entry)
            self.save()
            return True

    def add_reward(self, reward_dict: dict) -> bool:
        """Add a node reward to the ledger.
        If two rewards claim the same slot, the one with the lowest VRF ticket wins.
        Slot comparison runs BEFORE reward_id dedup so competing rewards are evaluated.
        VRF ticket is cryptographically verified before acceptance."""
        with self._lock:
            # Cryptographically verify the VRF ticket before accepting
            public_key = reward_dict.get("vrf_public_key")
            seed = reward_dict.get("vrf_seed")
            sig = reward_dict.get("vrf_sig")
            ticket = reward_dict.get("vrf_ticket")
            if public_key and seed and sig and ticket:
                if not Node._verify_ticket(public_key, seed, sig, ticket):
                    return False
            slot = reward_dict.get("time_slot")
            new_ticket = reward_dict.get("vrf_ticket", "z")
            if slot:
                existing = next((r for r in self.rewards if r.get("time_slot") == slot), None)
                if existing:
                    # Exact same reward already stored
                    if existing.get("reward_id") == reward_dict["reward_id"] and existing.get("winner_id") == reward_dict.get("winner_id"):
                        return False
                    old_ticket = existing.get("vrf_ticket", "z")
                    if new_ticket < old_ticket:
                        # Incoming reward has lower ticket — it wins, replace
                        self.rewards = [r for r in self.rewards if r.get("time_slot") != slot]
                        self.total_minted -= existing["amount"]
                    else:
                        # Existing reward has lower or equal ticket — keep it
                        return False
            else:
                # No slot info — fall back to reward_id dedup
                if any(r["reward_id"] == reward_dict["reward_id"] for r in self.rewards):
                    return False
            if self.total_minted + reward_dict["amount"] > TOTAL_SUPPLY:
                return False
            self.rewards.append(reward_dict)
            self.total_minted += reward_dict["amount"]
            self.save()
            return True

    def recalculate_totals(self):
        self.total_minted = round(sum(r["amount"] for r in self.rewards), 8)

    def get_summary(self):
        return {
            "total_transactions": len(self.transactions),
            "total_rewards":      len(self.rewards),
            "total_minted":       self.total_minted,
            "remaining_supply":   TOTAL_SUPPLY - self.total_minted
        }

    def to_dict(self):
        return {
            "transactions": self.transactions,
            "rewards":      self.rewards,
            "total_minted": self.total_minted
        }

    def merge(self, other_ledger: dict):
        """Merge — one winner per time_slot, lowest VRF ticket wins conflict.
        All transactions and rewards are cryptographically verified before acceptance."""
        with self._lock:
            changed = False
            for tx in other_ledger.get("transactions", []):
                if not self.has_transaction(tx["tx_id"]):
                    total = tx["amount"] + tx.get("fee", 0.0)
                    if self.can_spend(tx["sender_id"], total):
                        # Verify signature before accepting
                        try:
                            t = Transaction.from_dict(tx)
                            if not t.verify():
                                continue
                        except Exception:
                            continue
                        self.transactions.append(tx)
                        changed = True
            existing_slots = {r.get("time_slot"): r for r in self.rewards}
            for reward in other_ledger.get("rewards", []):
                rid  = reward["reward_id"]
                slot = reward.get("time_slot")
                # Cryptographically verify VRF ticket before accepting via merge
                pub  = reward.get("vrf_public_key")
                seed = reward.get("vrf_seed")
                sig  = reward.get("vrf_sig")
                tick = reward.get("vrf_ticket")
                if pub and seed and sig and tick:
                    if not Node._verify_ticket(pub, seed, sig, tick):
                        continue
                if any(r["reward_id"] == rid and r.get("winner_id") == reward.get("winner_id") for r in self.rewards):
                    continue
                if slot and slot in existing_slots:
                    existing   = existing_slots[slot]
                    old_ticket = existing.get("vrf_ticket", "z")
                    new_ticket = reward.get("vrf_ticket", "z")
                    if new_ticket < old_ticket:
                        self.rewards = [r for r in self.rewards if r.get("time_slot") != slot]
                        del existing_slots[slot]
                    else:
                        continue
                if self.total_minted + reward["amount"] <= TOTAL_SUPPLY:
                    self.rewards.append(reward)
                    if slot:
                        existing_slots[slot] = reward
                    changed = True
            if changed:
                self.recalculate_totals()
                self.save()
            return changed


    @staticmethod
    def _compute_hash(entries: list) -> str:
        """SHA256 hash of a list of entries — cryptographic proof of pruned data."""
        serialized = json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(serialized).hexdigest()

    def create_checkpoint(self, checkpoint_slot: int) -> bool:
        """Create a checkpoint at checkpoint_slot.
        Calculates balances for all addresses, hashes pruned data,
        prunes old rewards and transactions, saves checkpoint to ledger.
        Runs in background thread — never blocks lottery or transactions."""
        prune_before = checkpoint_slot - CHECKPOINT_BUFFER
        with self._lock:
            if any(c["slot"] == checkpoint_slot for c in self.checkpoints):
                return False
            # Separate data to prune vs keep
            rewards_to_prune = [r for r in self.rewards
                               if r.get("time_slot", prune_before) < prune_before]
            rewards_to_keep  = [r for r in self.rewards
                               if r.get("time_slot", prune_before) >= prune_before]
            txs_to_prune = [t for t in self.transactions
                           if (t.get("slot") or 0) < prune_before]
            txs_to_keep  = [t for t in self.transactions
                           if (t.get("slot") or 0) >= prune_before]
            # Calculate balances from previous checkpoint + pruned data
            prev_balances = {}
            if self.checkpoints:
                prev_balances = dict(self.checkpoints[-1]["balances"])
            addresses = set(prev_balances.keys())
            for r in rewards_to_prune:
                addresses.add(r["winner_id"])
            for t in txs_to_prune:
                addresses.add(t["sender_id"])
                addresses.add(t["recipient_id"])
            balances = {}
            for addr in addresses:
                bal = prev_balances.get(addr, 0.0)
                for t in txs_to_prune:
                    if t["recipient_id"] == addr:
                        bal += t["amount"]
                    if t["sender_id"] == addr:
                        bal -= t["amount"]
                        bal -= t.get("fee", 0.0)
                for r in rewards_to_prune:
                    if r["winner_id"] == addr:
                        bal += r["amount"]
                balances[addr] = round(bal, 8)
            # Replay prevention — store pruned tx IDs
            prev_spent   = list(self.checkpoints[-1].get("spent_tx_ids", [])) if self.checkpoints else []
            new_spent    = [t["tx_id"] for t in txs_to_prune]
            spent_tx_ids = list(set(prev_spent + new_spent))
            # Cryptographic proof of pruned data
            rewards_hash = Ledger._compute_hash(
                sorted(rewards_to_prune, key=lambda r: r.get("time_slot", 0))
            )
            txs_hash = Ledger._compute_hash(
                sorted(txs_to_prune, key=lambda t: t.get("timestamp", 0))
            )
            checkpoint = {
                "slot":         checkpoint_slot,
                "prune_before": prune_before,
                "balances":     balances,
                "total_minted": self.total_minted,
                "rewards_hash": rewards_hash,
                "txs_hash":     txs_hash,
                "spent_tx_ids": spent_tx_ids,
                "timestamp":    time.time()
            }
            self.rewards      = rewards_to_keep
            self.transactions = txs_to_keep
            self.checkpoints.append(checkpoint)
            self.save()
            return True

    def apply_checkpoint(self, checkpoint: dict) -> bool:
        """Apply a checkpoint received from a peer.
        Only accepted if newer than our latest checkpoint.
        Verifies rewards_hash and txs_hash against local data before accepting.
        Prunes local data to match checkpoint."""
        with self._lock:
            if self.checkpoints:
                if checkpoint.get("slot", 0) <= self.checkpoints[-1]["slot"]:
                    return False
            if checkpoint.get("total_minted", 0) > TOTAL_SUPPLY:
                return False
            prune_before = checkpoint.get("prune_before", 0)
            # Verify checkpoint hashes against our local data.
            # If they don't match, the checkpoint is fake — reject it.
            rewards_to_verify = [r for r in self.rewards
                                 if r.get("time_slot", prune_before) < prune_before]
            if rewards_to_verify:
                computed = Ledger._compute_hash(
                    sorted(rewards_to_verify, key=lambda r: r.get("time_slot", 0))
                )
                if computed != checkpoint.get("rewards_hash", ""):
                    return False
            txs_to_verify = [t for t in self.transactions
                             if (t.get("slot") or 0) < prune_before]
            if txs_to_verify:
                computed = Ledger._compute_hash(
                    sorted(txs_to_verify, key=lambda t: t.get("timestamp", 0))
                )
                if computed != checkpoint.get("txs_hash", ""):
                    return False
            # Hashes verified (or no local data to check against)
            self.rewards      = [r for r in self.rewards
                                if r.get("time_slot", prune_before) >= prune_before]
            self.transactions = [t for t in self.transactions
                                if (t.get("slot") or 0) >= prune_before]
            self.checkpoints.append(checkpoint)
            self.total_minted = checkpoint["total_minted"]
            self.save()
            return True


# ─────────────────────────────────────────────
# WALLET — Quantum-resistant identity
# ─────────────────────────────────────────────

class Wallet:
    def __init__(self):
        self.public_key  = None   # bytes
        self.private_key = None   # bytes
        self.device_id   = None   # hex string derived from public key

    def create_new(self):
        self.public_key, self.private_key = Dilithium3.keygen()
        self.device_id = self._derive_device_id()
        print(f"\n  New quantum-resistant wallet created.")
        print(f"  Device ID: {self.device_id[:24]}...")
        print(f"")
        print(f"  WARNING - BACK UP YOUR WALLET FILE:")
        print(f"  {WALLET_FILE}")
        print(f"  This file contains your private key.")
        print(f"  If you delete it your TMPL is gone forever.")
        print(f"  Copy it somewhere safe.")

    def _derive_device_id(self):
        return hashlib.sha256(self.public_key).hexdigest()

    def save(self, path=WALLET_FILE):
        data = {
            "version":     VERSION,
            "device_id":   self.device_id,
            "public_key":  self.public_key.hex(),
            "private_key": self.private_key.hex(),
            "quantum":     True
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def load(self, path=WALLET_FILE):
        with open(path, "r") as f:
            data = json.load(f)
        self.public_key  = bytes.fromhex(data["public_key"])
        self.private_key = bytes.fromhex(data["private_key"])
        self.device_id   = data["device_id"]

    def get_public_key_hex(self):
        return self.public_key.hex()

    def sign(self, message: bytes) -> str:
        signature = Dilithium3.sign(self.private_key, message)
        return signature.hex()

    @staticmethod
    def verify_signature(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
        try:
            pub_bytes = bytes.fromhex(public_key_hex)
            sig_bytes = bytes.fromhex(signature_hex)
            return Dilithium3.verify(pub_bytes, message, sig_bytes)
        except Exception:
            return False


# ─────────────────────────────────────────────
# TRANSACTION
# ─────────────────────────────────────────────

class Transaction:
    def __init__(self, sender_id, recipient_id, sender_pubkey,
                 amount, timestamp=None, tx_id=None, fee=0.0, slot=None):
        self.tx_id         = tx_id or str(uuid.uuid4())
        self.sender_id     = sender_id
        self.recipient_id  = recipient_id
        self.sender_pubkey = sender_pubkey
        self.amount        = amount
        self.fee           = fee    # 0.0 in Era 1, 0.0005 in Era 2
        self.slot          = slot   # Time slot when tx was created
        self.timestamp     = timestamp or time.time()
        self.signature     = None

    def _payload(self) -> bytes:
        return f"{self.tx_id}:{self.sender_id}:{self.recipient_id}:{self.amount:.8f}:{self.fee:.8f}:{self.timestamp:.4f}:{self.slot}".encode()

    def sign(self, wallet: Wallet):
        self.signature = wallet.sign(self._payload())

    def verify(self) -> bool:
        if not self.signature:
            return False
        return Wallet.verify_signature(self.sender_pubkey, self._payload(), self.signature)

    def to_dict(self) -> dict:
        return {
            "tx_id":         self.tx_id,
            "sender_id":     self.sender_id,
            "recipient_id":  self.recipient_id,
            "sender_pubkey": self.sender_pubkey,
            "amount":        self.amount,
            "fee":           self.fee,
            "slot":          self.slot,
            "timestamp":     self.timestamp,
            "signature":     self.signature
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        tx = cls(
            sender_id     = d["sender_id"],
            recipient_id  = d["recipient_id"],
            sender_pubkey = d["sender_pubkey"],
            amount        = d["amount"],
            fee           = d.get("fee", 0.0),
            slot          = d.get("slot"),
            timestamp     = d["timestamp"],
            tx_id         = d["tx_id"]
        )
        tx.signature = d.get("signature")
        return tx


# ─────────────────────────────────────────────
# NETWORK — Worldwide peer discovery
# ─────────────────────────────────────────────

class Network:
    def __init__(self, wallet: Wallet, ledger: Ledger,
                 on_transaction, on_reward):
        self.wallet         = wallet
        self.ledger         = ledger
        self.on_transaction = on_transaction
        self.on_reward      = on_reward
        self.peers          = {}        # device_id -> {ip, port, last_seen}
        self.seen_ids       = set()
        self._running       = False
        self.local_ip       = self._get_local_ip()
        self.port           = find_free_port(7779)
        self._node_ref      = None

    def _get_local_ip(self):
        # Try to get public IP first (for server deployments)
        try:
            import urllib.request
            public_ip = urllib.request.urlopen(
                'https://api.ipify.org', timeout=3
            ).read().decode('utf-8').strip()
            if public_ip:
                return public_ip
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _save_peers(self):
        try:
            import os, json
            peers_file = os.path.join(os.path.expanduser("~"), ".timpal_peers.json")
            saveable = {
                pid: {"ip": p["ip"], "port": p["port"]}
                for pid, p in self.peers.items()
                if pid != self.wallet.device_id
            }
            with open(peers_file, "w") as f:
                json.dump(saveable, f)
        except Exception:
            pass

    def _load_peers(self):
        try:
            import os, json
            peers_file = os.path.join(os.path.expanduser("~"), ".timpal_peers.json")
            if os.path.exists(peers_file):
                with open(peers_file, "r") as f:
                    saved = json.load(f)
                for pid, p in saved.items():
                    if pid != self.wallet.device_id:
                        self.peers[pid] = {
                            "ip":        p["ip"],
                            "port":      p["port"],
                            "last_seen": __import__("time").time() - 25
                        }
        except Exception:
            pass

    def start(self):
        self._running = True
        self._load_peers()
        threading.Thread(target=self._listen_tcp,        daemon=True).start()
        threading.Thread(target=self._listen_discovery,  daemon=True).start()
        threading.Thread(target=self._broadcast_loop,    daemon=True).start()
        threading.Thread(target=self._bootstrap_connect, daemon=True).start()
        threading.Thread(target=self._periodic_sync, daemon=True).start()

    def stop(self):
        self._running = False

    # ── Bootstrap connection ────────────────────

    def _bootstrap_connect(self):
        """Connect to bootstrap server and get initial peer list."""
        time.sleep(2)
        while self._running:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10.0)
                sock.connect((BOOTSTRAP_HOST, BOOTSTRAP_PORT))
                msg = json.dumps({
                    "type":      "HELLO",
                    "device_id": self.wallet.device_id,
                    "port":      self.port,
                    "version":   VERSION
                }).encode()
                sock.sendall(msg)
                response = sock.recv(65536)
                sock.close()

                data = json.loads(response.decode())
                if data.get("type") == "VERSION_REJECTED":
                    print(f"\n  [!] {data.get('reason', 'Version rejected by bootstrap')}")
                    print(f"  > ", end="", flush=True)
                    return
                if data.get("type") == "PEERS":
                    new_peers = 0
                    for peer in data.get("peers", []):
                        pid = peer["device_id"]
                        if pid != self.wallet.device_id and pid not in self.peers:
                            self.peers[pid] = {
                                "ip":        peer["ip"],
                                "port":      peer["port"],
                                "last_seen": time.time()
                            }
                            new_peers += 1
                    if new_peers > 0:
                        self._save_peers()
                        print(f"\n  [+] Bootstrap: found {new_peers} peers worldwide")
                        print(f"  Network size: {data.get('network_size', 0)} nodes")
                        print(f"  > ", end="", flush=True)
                        threading.Thread(target=self._sync_ledger, daemon=True).start()

            except Exception:
                pass

            # Re-register with bootstrap every 2 minutes
            time.sleep(120)

    def _periodic_sync(self):
        """Run delta sync every 2 minutes to keep ledger fully up to date."""
        time.sleep(30)
        while self._running:
            try:
                if self.get_online_peers():
                    self._sync_ledger()
            except Exception:
                pass
            time.sleep(120)

    def _sync_ledger(self):
        """Delta sync — only request what we are missing from a peer.
        Sends our known slot numbers and tx IDs.
        Peer responds with only the missing pieces.
        Scales to millions of nodes — never transfers full ledger.
        """
        time.sleep(1)
        peers = self.get_online_peers()
        if not peers:
            return
        for peer_id in random.sample(list(peers.keys()), min(3, len(peers))):
            peer = peers[peer_id]
            try:
                with self.ledger._lock:
                    known_slots  = [r.get("time_slot") for r in self.ledger.rewards if r.get("time_slot")]
                    known_tx_ids = [t.get("tx_id") for t in self.ledger.transactions if t.get("tx_id")]

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(30.0)
                sock.connect((peer["ip"], peer["port"]))
                request = json.dumps({
                    "type":            "SYNC_REQUEST",
                    "known_slots":     known_slots,
                    "known_tx_ids":    known_tx_ids,
                    "checkpoint_slot": self.ledger.checkpoints[-1]["slot"] if self.ledger.checkpoints else 0
                }).encode()
                sock.sendall(request)
                sock.shutdown(socket.SHUT_WR)
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                sock.close()
                msg = json.loads(data.decode())
                if msg.get("type") == "SYNC_RESPONSE":
                    if msg.get("checkpoint"):
                        self.ledger.apply_checkpoint(msg["checkpoint"])
                    delta = {
                        "rewards":      msg.get("rewards", []),
                        "transactions": msg.get("txs", [])
                    }
                    missing_r = len(delta["rewards"])
                    missing_t = len(delta["transactions"])
                    if missing_r > 0 or missing_t > 0:
                        # Record known tx_ids before merge to detect truly new ones
                        known_tx_ids_before = set(t.get("tx_id") for t in self.ledger.transactions)
                        merged = self.ledger.merge(delta)
                        if merged:
                            print(f"\n  [+] Synced {missing_r} rewards, {missing_t} txs from network")
                            print(f"  > ", end="", flush=True)
                            # Notify only for transactions not seen before this sync
                            node = self._node_ref
                            if node:
                                for tx in delta["transactions"]:
                                    if tx.get("tx_id") in known_tx_ids_before:
                                        continue
                                    if tx.get("recipient_id") == node.wallet.device_id:
                                        balance = self.ledger.get_balance(node.wallet.device_id)
                                        print(f"\n  ╔══════════════════════════════════╗")
                                        print(f"  ║       TMPL RECEIVED              ║")
                                        print(f"  ╠══════════════════════════════════╣")
                                        print(f"  ║  Amount  : {tx['amount']:.8f} TMPL")
                                        print(f"  ║  From    : {tx['sender_id'][:20]}...")
                                        print(f"  ║  Balance : {balance:.8f} TMPL")
                                        print(f"  ╚══════════════════════════════════╝")
                                        print(f"  > ", end="", flush=True)

                    # Push back what the peer told us they need
                    we_need_slots  = set(msg.get("we_need_slots", []))
                    we_need_tx_ids = set(msg.get("we_need_tx_ids", []))
                    if we_need_slots or we_need_tx_ids:
                        with self.ledger._lock:
                            push_rewards = [
                                r for r in self.ledger.rewards
                                if r.get("time_slot") in we_need_slots
                            ]
                            push_txs = [
                                t for t in self.ledger.transactions
                                if t.get("tx_id") in we_need_tx_ids
                            ]
                        if push_rewards or push_txs:
                            try:
                                s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                s2.settimeout(30.0)
                                s2.connect((peer["ip"], peer["port"]))
                                s2.sendall(json.dumps({
                                    "type":    "SYNC_PUSH",
                                    "rewards": push_rewards,
                                    "txs":     push_txs
                                }).encode())
                                s2.shutdown(socket.SHUT_WR)
                                s2.close()
                            except Exception:
                                pass
                return
            except Exception:
                continue

    # ── Local discovery ─────────────────────────

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while self._running:
            msg = json.dumps({
                "type":      "HELLO",
                "device_id": self.wallet.device_id,
                "ip":        self.local_ip,
                "port":      self.port,
                "version":   VERSION
            }).encode()
            try:
                sock.sendto(msg, ("<broadcast>", BROADCAST_PORT))
            except Exception:
                pass
            time.sleep(DISCOVERY_INTERVAL)

    def _listen_discovery(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        sock.bind(("", BROADCAST_PORT))
        sock.settimeout(1.0)
        while self._running:
            try:
                data, addr = sock.recvfrom(1024)
                msg = json.loads(data.decode())
                if msg.get("type") == "HELLO":
                    peer_id      = msg["device_id"]
                    peer_ip      = msg["ip"]
                    peer_port    = msg.get("port", 7779)
                    peer_version = msg.get("version", "0.0")
                    if _ver(peer_version) < _ver(MIN_VERSION):
                        continue
                    if peer_id != self.wallet.device_id:
                        is_new = peer_id not in self.peers
                        self.peers[peer_id] = {
                            "ip":        peer_ip,
                            "port":      peer_port,
                            "last_seen": time.time()
                        }
                        if is_new:
                            print(f"\n  [+] Local peer: {peer_id[:20]}... at {peer_ip}")
                            print(f"  > ", end="", flush=True)
            except socket.timeout:
                continue
            except Exception:
                continue

    # ── TCP listener ────────────────────────────

    def _listen_tcp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.listen(50)
        sock.settimeout(1.0)
        while self._running:
            try:
                conn, addr = sock.accept()
                threading.Thread(
                    target=self._handle_incoming,
                    args=(conn, addr),
                    daemon=True
                ).start()
            except socket.timeout:
                continue
            except Exception:
                continue

    def _handle_incoming(self, conn, addr):
        try:
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > 10_000_000:
                    break

            msg = json.loads(data.decode())
            msg_type = msg.get("type")

            if msg_type == "HELLO":
                # Peer introducing itself directly
                peer_id      = msg.get("device_id")
                peer_version = msg.get("version", "0.0")
                # Version enforcement — reject incompatible nodes
                if _ver(peer_version) < _ver(MIN_VERSION):
                    conn.sendall(json.dumps({
                        "type":    "VERSION_REJECTED",
                        "reason":  f"Your version ({peer_version}) is below minimum ({MIN_VERSION}). Update from: https://github.com/EvokiTimpal/timpal"
                    }).encode())
                    return
                if peer_id and peer_id != self.wallet.device_id:
                    self.peers[peer_id] = {
                        "ip":        addr[0],
                        "port":      msg.get("port", 7779),
                        "last_seen": time.time()
                    }
                    # Share our full peer list so they can connect to everyone we know
                    peer_list = [
                        {"device_id": pid, "ip": p["ip"], "port": p["port"]}
                        for pid, p in self.peers.items()
                        if pid != peer_id
                    ]
                    response = json.dumps({
                        "type":      "HELLO_ACK",
                        "device_id": self.wallet.device_id,
                        "peers":     peer_list
                    }).encode()
                    conn.sendall(response)
                    threading.Thread(target=self._sync_ledger, daemon=True).start()

            elif msg_type == "TRANSACTION":
                tx = Transaction.from_dict(msg["transaction"])
                tx_gossip_id = msg.get("transaction", {}).get("tx_id", "")
                if tx_gossip_id and tx_gossip_id not in self.seen_ids:
                    self.on_transaction(tx)
                    threading.Thread(
                        target=self.broadcast,
                        args=(msg, None),
                        daemon=True
                    ).start()
                elif not tx_gossip_id:
                    self.on_transaction(tx)

            elif msg_type == "FEE_REWARDS":
                # Era 2: peer is gossiping fee reward distributions for a slot
                if is_era2(self.ledger.total_minted):
                    time_slot   = msg.get("time_slot")
                    fee_rewards = msg.get("fee_rewards", [])
                    for fr in fee_rewards:
                        self.ledger.add_fee_reward(
                            time_slot,
                            fr["winner_id"],
                            fr["amount"]
                        )

            elif msg_type in ("VRF_COMMIT", "VRF_REVEAL", "VRF_TICKET"):
                pass   # Lottery handled via bootstrap registry — not peer gossip

            elif msg_type == "REWARD":
                reward = msg.get("reward", {})
                # Include winner_id in gossip_id so competing rewards for same
                # slot are not deduplicated — lowest ticket must reach all nodes
                reward_gossip_id = reward.get("reward_id", "") + ":" + reward.get("winner_id", "")
                if reward_gossip_id and reward_gossip_id not in self.seen_ids:
                    self.seen_ids.add(reward_gossip_id)
                    self.on_reward(reward)
                    threading.Thread(
                        target=self.broadcast,
                        args=(msg, None),
                        daemon=True
                    ).start()
                elif not reward_gossip_id:
                    self.on_reward(msg["reward"])

            elif msg_type == "SYNC_PUSH":
                # Peer is pushing rewards/txs we asked for
                delta = {
                    "rewards":      msg.get("rewards", []),
                    "transactions": msg.get("txs", [])
                }
                if delta["rewards"] or delta["transactions"]:
                    self.ledger.merge(delta)

            elif msg_type == "CHECKPOINT":
                checkpoint = msg.get("checkpoint", {})
                if checkpoint:
                    gossip_id = f"checkpoint:{checkpoint.get('slot', '')}"
                    if gossip_id not in self.seen_ids:
                        self.seen_ids.add(gossip_id)
                        applied = self.ledger.apply_checkpoint(checkpoint)
                        if applied:
                            self.broadcast({"type": "CHECKPOINT", "checkpoint": checkpoint})

            elif msg_type == "GET_LEDGER":
                # Legacy full sync — still supported for compatibility
                response = json.dumps({
                    "type":   "LEDGER",
                    "ledger": self.ledger.to_dict()
                }).encode()
                conn.sendall(response)

            elif msg_type == "SYNC_REQUEST":
                # Delta sync — bidirectional
                # 1. Send peer what they are missing
                # 2. Tell peer what WE are missing so they can push it to us
                # 3. Send our checkpoint if peer is behind
                their_slots           = set(msg.get("known_slots", []))
                their_tx_ids          = set(msg.get("known_tx_ids", []))
                their_checkpoint_slot = msg.get("checkpoint_slot", 0)

                with self.ledger._lock:
                    our_slots = set(r.get("time_slot") for r in self.ledger.rewards if r.get("time_slot"))
                    our_tx_ids = set(t.get("tx_id") for t in self.ledger.transactions if t.get("tx_id"))

                    # What peer is missing (we have, they don't)
                    missing_rewards = [
                        r for r in self.ledger.rewards
                        if r.get("time_slot") not in their_slots
                    ]
                    missing_txs = [
                        t for t in self.ledger.transactions
                        if t.get("tx_id") not in their_tx_ids
                    ]

                    # What WE are missing (they have, we don't)
                    we_need_slots  = list(their_slots - our_slots)
                    we_need_tx_ids = list(their_tx_ids - our_tx_ids)

                    # Send checkpoint if peer is behind
                    our_checkpoint = None
                    if self.ledger.checkpoints:
                        latest = self.ledger.checkpoints[-1]
                        if latest["slot"] > their_checkpoint_slot:
                            our_checkpoint = latest

                response = json.dumps({
                    "type":             "SYNC_RESPONSE",
                    "rewards":          missing_rewards,
                    "txs":              missing_txs,
                    "total":            len(self.ledger.rewards),
                    "we_need_slots":    we_need_slots,
                    "we_need_tx_ids":   we_need_tx_ids,
                    "checkpoint":       our_checkpoint
                }).encode()
                conn.sendall(response)



        except Exception:
            pass
        finally:
            conn.close()

    # ── Broadcast to all peers ───────────────────

    def broadcast(self, message: dict, exclude_id: str = None):
        """Send a message to all known peers (gossip protocol).
        Uses all known peers, not just recently active ones."""
        msg_bytes = json.dumps(message).encode()
        for peer_id, peer in list(self.peers.items()):
            if peer_id == exclude_id:
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((peer["ip"], peer["port"]))
                sock.sendall(msg_bytes)
                sock.close()
                self.peers[peer_id]["last_seen"] = time.time()
            except Exception:
                continue

    def send_to_peer(self, peer_id: str, message: dict) -> bool:
        if peer_id not in self.peers:
            return False
        peer = self.peers[peer_id]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((peer["ip"], peer["port"]))
            sock.sendall(json.dumps(message).encode())
            sock.close()
            return True
        except Exception:
            return False

    def get_online_peers(self):
        cutoff = time.time() - 30
        return {
            pid: info for pid, info in self.peers.items()
            if info["last_seen"] > cutoff
        }


# ─────────────────────────────────────────────
# NODE — Complete Timpal device
# ─────────────────────────────────────────────

class Node:
    def __init__(self):
        self.wallet  = Wallet()
        self.ledger  = Ledger()
        self.network = None
        self._acquire_lock()
        self._load_or_create_wallet()
        self.network = Network(
            self.wallet, self.ledger,
            self._on_transaction_received,
            self._on_reward_received
        )
        self.network._node_ref = self
        self._sending      = False
        self._my_tickets   = {}   # slot -> (ticket, sig, seed)
        self._commits      = {}   # slot -> {device_id: commit_hash}
        self._reveals      = {}   # slot -> {device_id: {ticket,sig,seed,pubkey}}
        self._lottery_lock = threading.Lock()

    def _acquire_lock(self):
        import sys
        lock_path = __import__("os").path.join(__import__("os").path.expanduser("~"), ".timpal.lock")
        self._lock_file = open(lock_path, "w")
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("\n  TIMPAL IS ALREADY RUNNING. Only one node per device.\n")
            exit(0)

    def _load_or_create_wallet(self):
        if os.path.exists(WALLET_FILE):
            self.wallet.load()
            balance = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n  Wallet loaded.")
            print(f"  Device ID : {self.wallet.device_id[:24]}...")
            print(f"  Balance   : {balance:.8f} TMPL")
        else:
            self.wallet.create_new()
            self.wallet.save()

    _tx_rate = {}  # device_id -> [timestamps]

    def _on_transaction_received(self, tx: Transaction):
        if tx.tx_id in self.network.seen_ids:
            return
        self.network.seen_ids.add(tx.tx_id)

        if not tx.verify():
            return

        if tx.amount <= 0:
            return

        # Rate limit — max 60 transactions per minute per device
        now = time.time()
        sender = tx.sender_id
        times = Node._tx_rate.get(sender, [])
        times = [t for t in times if now - t < 60]
        if len(times) >= 60:
            return
        times.append(now)
        Node._tx_rate[sender] = times

        # Add to ledger — ledger checks balance automatically
        added = self.ledger.add_transaction(tx.to_dict())
        if not added:
            return

        # Broadcast to rest of network
        self.network.broadcast({
            "type":        "TRANSACTION",
            "transaction": tx.to_dict()
        })

        # Show notification if we are the recipient
        if tx.recipient_id == self.wallet.device_id:
            balance = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n")
            print(f"  ╔══════════════════════════════════╗")
            print(f"  ║       TMPL RECEIVED              ║")
            print(f"  ╠══════════════════════════════════╣")
            print(f"  ║  Amount  : {tx.amount:.8f} TMPL")
            print(f"  ║  From    : {tx.sender_id[:20]}...")
            print(f"  ║  Balance : {balance:.8f} TMPL")
            print(f"  ╚══════════════════════════════════╝")
            print(f"  > ", end="", flush=True)

    def _on_reward_received(self, reward: dict):
        added = self.ledger.add_reward(reward)
        if not added:
            return

        # Broadcast to rest of network
        self.network.broadcast({"type": "REWARD", "reward": reward})

        # Show notification if we won
        if reward.get("winner_id") == self.wallet.device_id:
            balance = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n")
            print(f"  ╔══════════════════════════════════╗")
            if not self._sending:
                print(f"  ║       REWARD WON! ★              ║")
            print(f"  ╠══════════════════════════════════╣")
            print(f"  ║  Amount  : {reward['amount']:.8f} TMPL")
            print(f"  ║  Balance : {balance:.8f} TMPL")
            print(f"  ╚══════════════════════════════════╝")
            print(f"  > ", end="", flush=True)

    def _vrf_ticket(self, time_slot: int) -> tuple:
        """VRF ticket using time_slot as shared seed.
        seed = time_slot — identical for every node on the planet, no sync needed.
        ticket = SHA256(sign(private_key, seed)) — unique per node, unpredictable.
        No ledger dependency. No forks ever possible."""
        seed = str(time_slot)
        msg = seed.encode()
        sig = Dilithium3.sign(self.wallet.private_key, msg)
        ticket = hashlib.sha256(sig).hexdigest()
        return ticket, sig.hex(), seed

    @staticmethod
    def _verify_ticket(public_key_hex: str, seed: str, sig_hex: str, ticket: str) -> bool:
        """Verify a ticket: signature matches public key and hashes to claimed ticket."""
        try:
            pub = bytes.fromhex(public_key_hex)
            sig = bytes.fromhex(sig_hex)
            msg = seed.encode()
            if not Dilithium3.verify(pub, msg, sig):
                return False
            return hashlib.sha256(sig).hexdigest() == ticket
        except Exception:
            return False

    # ── Commit-reveal lottery — decentralized, CGNAT-safe ──────────────────

    def _make_commit(self, time_slot: int, ticket: str) -> str:
        """Commitment = SHA256(ticket + device_id + slot).
        Binding to device_id prevents grinding attacks."""
        raw = f"{ticket}:{self.wallet.device_id}:{time_slot}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _bootstrap_request(self, msg: dict) -> dict:
        """Send a request to bootstrap and return the response.
        Tries all bootstrap servers, returns first success."""
        for host, port in BOOTSTRAP_SERVERS:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((host, port))
                sock.sendall(json.dumps(msg).encode())
                sock.shutdown(socket.SHUT_WR)
                resp = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    resp += chunk
                sock.close()
                return json.loads(resp.decode())
            except Exception:
                continue
        return {}

    def _pick_winner(self, time_slot, all_reveals):
        """Pick winner from all verified reveals for this slot.
        Lowest hex ticket wins — pure math, same answer on every node.
        Reveals only accepted if matching commit exists."""
        verified = {}
        with self._lottery_lock:
            known_commits = dict(self._commits.get(time_slot, {}))

        for device_id, r in all_reveals.items():
            # Must have a commit for this node
            commit = known_commits.get(device_id)
            if not commit:
                continue
            # Reveal must match commit
            expected = hashlib.sha256(
                f"{r['ticket']}:{device_id}:{time_slot}".encode()
            ).hexdigest()
            if expected != commit:
                continue
            # Ticket signature must be valid
            if not Node._verify_ticket(
                r["public_key"], r["seed"], r["sig"], r["ticket"]
            ):
                continue
            verified[device_id] = r

        if not verified:
            return None

        winner_id = min(verified, key=lambda d: verified[d]["ticket"])
        w = verified[winner_id]
        return {
            "winner_id":  winner_id,
            "ticket":     w["ticket"],
            "sig":        w["sig"],
            "seed":       w["seed"],
            "public_key": w["public_key"]
        }

    def _distribute_fees(self, time_slot: int, active_nodes: list):
        """Era 2 only: split all transaction fees for this slot equally
        among all nodes that submitted a VRF commit (were provably active).
        Called after slot winner is determined, only when Era 2 is active."""
        if not is_era2(self.ledger.total_minted):
            return
        if not active_nodes:
            return
        # Sum all fees from transactions in this slot
        slot_fees = sum(
            tx.get("fee", 0.0)
            for tx in self.ledger.transactions
            if tx.get("slot") == time_slot and tx.get("fee", 0.0) > 0
        )
        if slot_fees <= 0:
            return
        per_node = round(slot_fees / len(active_nodes), 8)
        if per_node <= 0:
            return
        fee_rewards = []
        for node_id in active_nodes:
            added = self.ledger.add_fee_reward(time_slot, node_id, per_node)
            if added:
                fee_rewards.append({
                    "reward_id": f"fee:{time_slot}:{node_id}",
                    "winner_id": node_id,
                    "amount":    per_node,
                    "timestamp": time.time(),
                    "time_slot": time_slot,
                    "type":      "fee_reward"
                })
        # Gossip fee rewards to peers
        if fee_rewards:
            self.network.broadcast({
                "type":        "FEE_REWARDS",
                "time_slot":   time_slot,
                "fee_rewards": fee_rewards
            })
            if self.wallet.device_id in active_nodes:
                print(f"\n  [Era 2] Fee share: +{per_node:.8f} TMPL "
                      f"({len(active_nodes)} nodes shared {slot_fees:.8f} TMPL)\n  > ",
                      end="", flush=True)

    def _claim_reward(self, winner, time_slot, active_nodes=None):
        """Verify winner cryptographically, add to ledger, gossip to peers.
        In Era 2, also distribute transaction fees among active nodes."""
        if not Node._verify_ticket(
            winner["public_key"], winner["seed"],
            winner["sig"], winner["ticket"]
        ):
            return
        with self.ledger._lock:
            if any(r.get("time_slot") == time_slot
                   and r.get("type") != "fee_reward"
                   for r in self.ledger.rewards):
                return
        reward_id = f"reward:{time_slot}"
        reward = {
            "reward_id":      reward_id,
            "winner_id":      winner["winner_id"],
            "amount":         REWARD_PER_ROUND,
            "timestamp":      time.time(),
            "time_slot":      time_slot,
            "vrf_ticket":     winner["ticket"],
            "vrf_seed":       winner["seed"],
            "vrf_sig":        winner["sig"],
            "vrf_public_key": winner["public_key"],
            "nodes":          len(active_nodes) if active_nodes else 1,
            "type":           "block_reward"
        }
        added = self.ledger.add_reward(reward)
        if not added:
            return
        gossip_id = reward_id + ":" + winner["winner_id"]
        self.network.seen_ids.add(gossip_id)
        self.network.broadcast({"type": "REWARD", "reward": reward})
        if winner["winner_id"] == self.wallet.device_id:
            balance = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n  ╔══════════════════════════════════╗")
            print(f"  ║       REWARD WON! ★              ║")
            print(f"  ╠══════════════════════════════════╣")
            print(f"  ║  Amount  : {REWARD_PER_ROUND:.8f} TMPL")
            print(f"  ║  Balance : {balance:.8f} TMPL")
            print(f"  ╚══════════════════════════════════╝")
            print(f"  > ", end="", flush=True)
        else:
            short = winner["winner_id"][:20]
            print(f"\n  [slot {time_slot}] Winner: {short}... +{REWARD_PER_ROUND} TMPL\n  > ", end="", flush=True)
        # Era 2: distribute fees among active nodes
        if active_nodes:
            self._distribute_fees(time_slot, active_nodes)

    def _cleanup_slot(self, time_slot):
        """Remove lottery data older than 10 slots.
        Also prune old reward gossip IDs from seen_ids to prevent unbounded growth."""
        with self._lottery_lock:
            for d in (self._commits, self._reveals):
                old = [s for s in d if s < time_slot - 10]
                for s in old:
                    del d[s]
        old = [s for s in self._my_tickets if s < time_slot - 10]
        for s in old:
            del self._my_tickets[s]
        # Prune old reward gossip IDs — format is "reward:{slot}:{winner_id}"
        # and checkpoint IDs — format is "checkpoint:{slot}"
        cutoff = time_slot - 100
        stale = [
            sid for sid in self.network.seen_ids
            if (sid.startswith("reward:") or sid.startswith("checkpoint:"))
            and int(sid.split(":")[1]) < cutoff
        ]
        for sid in stale:
            self.network.seen_ids.discard(sid)
        # Cap transaction IDs — keep most recent 10000 only
        tx_ids = [sid for sid in self.network.seen_ids
                  if not sid.startswith("reward:") and not sid.startswith("checkpoint:")]
        if len(tx_ids) > 10000:
            for sid in tx_ids[:-10000]:
                self.network.seen_ids.discard(sid)

    def _reward_lottery(self):
        """Commit-reveal VRF lottery — fully decentralized, CGNAT-safe.

        Bootstrap stores commits and reveals — nodes query it to get
        all participants regardless of NAT/latency. Bootstrap cannot
        cheat — every entry is cryptographically verified by nodes.

        Every 5-second slot:
          t=0.0  Compute ticket. Submit COMMIT to bootstrap.
          t=2.0  Query bootstrap for ALL commits this slot.
                 Store them locally for reveal verification.
          t=2.0  Submit REVEAL to bootstrap.
          t=4.0  Query bootstrap for ALL reveals this slot.
          t=4.5  Pick lowest verified ticket — same on every node.
          t=4.5  Add reward to ledger, gossip to peers.

        If bootstrap is down: slot skipped cleanly, next slot works.
        Multiple bootstrap servers: tries all, uses first that responds.
        """
        time.sleep(45)

        while self.network._running:
            # Align to next absolute slot boundary
            now            = time.time()
            next_slot_time = (int(now / REWARD_INTERVAL) + 1) * REWARD_INTERVAL
            time.sleep(max(0.05, next_slot_time - time.time()))

            if self.ledger.total_minted >= TOTAL_SUPPLY:
                continue

            time_slot  = int(time.time() / REWARD_INTERVAL)
            slot_start = time_slot * REWARD_INTERVAL

            # Skip if reward already arrived via gossip
            if any(r.get("time_slot") == time_slot for r in self.ledger.rewards):
                self._cleanup_slot(time_slot)
                continue

            # ── Phase 1: Submit commit to bootstrap (t=0.0) ──────────────
            ticket, sig_hex, seed = self._vrf_ticket(time_slot)
            self._my_tickets[time_slot] = (ticket, sig_hex, seed)
            commit = self._make_commit(time_slot, ticket)

            # Store our own commit locally
            with self._lottery_lock:
                if time_slot not in self._commits:
                    self._commits[time_slot] = {}
                self._commits[time_slot][self.wallet.device_id] = commit

            # Submit to bootstrap registry
            self._bootstrap_request({
                "type":      "SUBMIT_COMMIT",
                "device_id": self.wallet.device_id,
                "slot":      time_slot,
                "commit":    commit
            })

            # ── Wait until t=2.0 then fetch all commits ───────────────────
            wait_commits = slot_start + 2.0
            remaining    = wait_commits - time.time()
            if remaining > 0:
                time.sleep(remaining)

            if any(r.get("time_slot") == time_slot for r in self.ledger.rewards):
                self._cleanup_slot(time_slot)
                continue

            # Fetch all commits from bootstrap
            resp = self._bootstrap_request({
                "type": "GET_COMMITS",
                "slot": time_slot
            })
            if resp.get("type") == "COMMITS_RESPONSE":
                with self._lottery_lock:
                    for device_id, c in resp.get("commits", {}).items():
                        if device_id not in self._commits.get(time_slot, {}):
                            if time_slot not in self._commits:
                                self._commits[time_slot] = {}
                            self._commits[time_slot][device_id] = c

            # ── Phase 2: Submit reveal to bootstrap (t=2.0) ──────────────
            self._bootstrap_request({
                "type":       "SUBMIT_REVEAL",
                "device_id":  self.wallet.device_id,
                "slot":       time_slot,
                "ticket":     ticket,
                "sig":        sig_hex,
                "seed":       seed,
                "public_key": self.wallet.public_key.hex()
            })

            # ── Wait until t=4.0 then fetch all reveals ───────────────────
            wait_reveals = slot_start + 4.0
            remaining    = wait_reveals - time.time()
            if remaining > 0:
                time.sleep(remaining)

            if any(r.get("time_slot") == time_slot for r in self.ledger.rewards):
                self._cleanup_slot(time_slot)
                continue

            # Fetch all reveals from bootstrap
            resp = self._bootstrap_request({
                "type": "GET_REVEALS",
                "slot": time_slot
            })

            # ── Phase 3: Pick winner (t=4.0) ─────────────────────────────
            all_reveals = {}
            if resp.get("type") == "REVEALS_RESPONSE":
                all_reveals = resp.get("reveals", {})

            # Always include our own reveal
            all_reveals[self.wallet.device_id] = {
                "ticket":     ticket,
                "sig":        sig_hex,
                "seed":       seed,
                "public_key": self.wallet.public_key.hex()
            }

            # Wait until t=4.5 before claiming
            wait_claim = slot_start + 4.5
            remaining  = wait_claim - time.time()
            if remaining > 0:
                time.sleep(remaining)

            if any(r.get("time_slot") == time_slot for r in self.ledger.rewards):
                self._cleanup_slot(time_slot)
                continue

            # Build active node list from commits (for Era 2 fee distribution)
            active_nodes = list(self._commits.get(time_slot, {}).keys())

            winner = self._pick_winner(time_slot, all_reveals)
            if winner:
                self._claim_reward(winner, time_slot, active_nodes)

            self._cleanup_slot(time_slot)

    def send(self, peer_id: str, amount: float) -> bool:
        if amount <= 0:
            print("\n  Amount must be greater than zero.")
            return False

        my_balance = self.ledger.get_balance(self.wallet.device_id)
        if amount > my_balance:
            print(f"\n  Insufficient balance.")
            print(f"  Your balance : {my_balance:.8f} TMPL")
            print(f"  Requested    : {amount:.8f} TMPL")
            return False

        if peer_id not in self.network.get_online_peers():
            print(f"\n  Peer is not online.")
            return False

        # Era 2: include fee if all coins distributed
        fee        = get_current_fee(self.ledger.total_minted)
        total_cost = amount + fee
        current_slot = int(time.time() / REWARD_INTERVAL)

        if fee > 0:
            print(f"\n  Era 2 fee: {fee:.8f} TMPL (split among active nodes this slot)")

        my_balance = self.ledger.get_balance(self.wallet.device_id)
        if total_cost > my_balance:
            print(f"\n  Insufficient balance.")
            print(f"  Your balance : {my_balance:.8f} TMPL")
            print(f"  Amount + fee : {total_cost:.8f} TMPL")
            return False

        tx = Transaction(
            sender_id     = self.wallet.device_id,
            recipient_id  = peer_id,
            sender_pubkey = self.wallet.get_public_key_hex(),
            amount        = amount,
            fee           = fee,
            slot          = current_slot
        )
        tx.sign(self.wallet)

        # Add to our ledger first
        added = self.ledger.add_transaction(tx.to_dict())
        if not added:
            print(f"\n  Transaction rejected by ledger.")
            return False

        # Broadcast to all peers
        self.network.broadcast({
            "type":        "TRANSACTION",
            "transaction": tx.to_dict()
        })

        new_balance = self.ledger.get_balance(self.wallet.device_id)
        print(f"\n  ✓ Sent {amount:.8f} TMPL")
        if fee > 0:
            print(f"  Fee paid     : {fee:.8f} TMPL")
        print(f"  New balance: {new_balance:.8f} TMPL")
        return True

    def _control_server(self):
        """Local control socket — lets CLI commands talk to the running node."""
        import socket as _socket
        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", 7780))
            srv.listen(5)
            srv.settimeout(1.0)
        except Exception:
            return  # Port in use, skip
        while self.network._running:
            try:
                conn, _ = srv.accept()
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if data.endswith(b"\n"):
                        break
                try:
                    cmd = json.loads(data.decode().strip())
                    response = self._handle_control(cmd)
                except Exception as e:
                    response = {"ok": False, "error": str(e)}
                conn.sendall((json.dumps(response) + "\n").encode())
                conn.close()
            except Exception:
                continue
        srv.close()

    def _handle_control(self, cmd: dict) -> dict:
        """Handle a command from the CLI control socket."""
        action = cmd.get("action")

        if action == "balance":
            balance = self.ledger.get_balance(self.wallet.device_id)
            return {"ok": True, "balance": balance, "address": self.wallet.device_id}

        elif action == "send":
            peer_id = cmd.get("peer_id")
            amount  = float(cmd.get("amount", 0))
            peers   = self.network.get_online_peers()
            if peer_id not in peers:
                return {"ok": False, "error": "Peer not online"}
            ok = self.send(peer_id, amount)
            return {"ok": ok}

        elif action == "network":
            summary = self.ledger.get_summary()
            peers   = self.network.get_online_peers()
            return {
                "ok":           True,
                "peers":        len(peers),
                "transactions": summary.get("total_transactions", 0),
                "minted":       summary.get("total_minted", 0),
                "remaining":    250000000 - summary.get("total_minted", 0)
            }

        return {"ok": False, "error": "Unknown action"}

    def _push_to_explorer(self):
        """Push ledger updates to the explorer API every 5 seconds."""
        time.sleep(60)
        while self.network._running:
            try:
                import urllib.request, ssl
                with self.ledger._lock:
                    rewards = list(self.ledger.rewards[-50:])
                    txs     = list(self.ledger.transactions[-20:])
                payload = json.dumps({
                    "type":         "LEDGER_PUSH",
                    "push_secret":  PUSH_SECRET,
                    "rewards":      rewards,
                    "transactions": txs
                }).encode()
                req = urllib.request.Request(
                    "https://timpal.org/api",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print(f"  [push error] {e}")
            time.sleep(5)

    def _checkpoint_loop(self):
        """Background thread — checks every 30 seconds if a checkpoint is due.
        Fires at CHECKPOINT_INTERVAL slots, waits CHECKPOINT_BUFFER slots before pruning.
        Fully automatic, no human intervention, runs forever."""
        while self.network._running:
            try:
                current_slot = int(time.time() / REWARD_INTERVAL)
                # Determine next checkpoint slot
                if self.ledger.checkpoints:
                    last_slot        = self.ledger.checkpoints[-1]["slot"]
                    next_checkpoint  = last_slot + CHECKPOINT_INTERVAL
                else:
                    boundary        = (current_slot // CHECKPOINT_INTERVAL) * CHECKPOINT_INTERVAL
                    next_checkpoint = boundary if boundary > 0 else CHECKPOINT_INTERVAL
                # Fire only after buffer window has passed
                if current_slot >= next_checkpoint + CHECKPOINT_BUFFER:
                    created = self.ledger.create_checkpoint(next_checkpoint)
                    if created:
                        print(f"\n  ╔══════════════════════════════════╗")
                        print(f"  ║       CHECKPOINT CREATED         ║")
                        print(f"  ╠══════════════════════════════════╣")
                        print(f"  ║  Slot     : {next_checkpoint}")
                        print(f"  ║  Balances : saved")
                        print(f"  ║  Old data : pruned")
                        print(f"  ╚══════════════════════════════════╝")
                        print(f"  > ", end="", flush=True)
                        # Gossip checkpoint to all peers
                        if self.ledger.checkpoints:
                            latest        = self.ledger.checkpoints[-1]
                            gossip_id     = f"checkpoint:{latest['slot']}"
                            self.network.seen_ids.add(gossip_id)
                            self.network.broadcast({"type": "CHECKPOINT", "checkpoint": latest})
            except Exception:
                pass
            time.sleep(30)

    def _periodic_ledger_sync(self):
        """Re-sync ledger every 5 minutes to catch up on missed history."""
        time.sleep(60)
        while self.network._running:
            peers = self.network.get_online_peers()
            if peers:
                self.network._sync_ledger()
            time.sleep(300)

    def start(self):
        print("\n" + "═" * 52)
        print("  TIMPAL v2.0 — Plan B for Humanity")
        print("  Quantum-Resistant | Worldwide | Instant")
        print("═" * 52)
        self.network.start()
        balance = self.ledger.get_balance(self.wallet.device_id)
        summary = self.ledger.get_summary()
        print(f"  Device ID : {self.wallet.device_id[:24]}...")
        print(f"  Balance   : {balance:.8f} TMPL")
        print(f"  Network   : {self.network.local_ip}:{self.network.port}")
        print(f"  Minted    : {summary['total_minted']:.4f} / {TOTAL_SUPPLY:,.0f} TMPL")
        print("═" * 52)
        print("  Connecting to worldwide network...")
        print("  Commands: balance | peers | send | history | network | quit")
        print("═" * 52 + "\n")

        threading.Thread(target=self._reward_lottery, daemon=True).start()
        threading.Thread(target=self._control_server, daemon=True).start()
        threading.Thread(target=self._periodic_ledger_sync, daemon=True).start()
        threading.Thread(target=self._push_to_explorer, daemon=True).start()
        threading.Thread(target=self._checkpoint_loop, daemon=True).start()
        self._cli()

    def _cli(self):
        import sys
        if not sys.stdin.isatty():
            # Running as daemon — no terminal, just keep alive
            import signal
            def shutdown(sig, frame):
                self.network.stop()
            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)
            while self.network._running:
                time.sleep(1)
            return
        while True:
            try:
                raw = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Shutting down. Goodbye.\n")
                self.network.stop()
                break

            if not raw:
                continue

            elif raw == "balance":
                balance = self.ledger.get_balance(self.wallet.device_id)
                print(f"\n  Balance: {balance:.8f} TMPL")
                print(f"  Device : {self.wallet.device_id}\n")

            elif raw == "peers":
                peers = self.network.get_online_peers()
                if not peers:
                    print("\n  No peers found yet. Connecting...\n")
                else:
                    print(f"\n  Online peers ({len(peers)}):")
                    for i, (pid, info) in enumerate(peers.items()):
                        print(f"  [{i+1}] {pid[:24]}... — {info['ip']}:{info['port']}")
                    print()

            elif raw == "network":
                summary = self.ledger.get_summary()
                peers   = self.network.get_online_peers()
                print(f"\n  Network Status:")
                print(f"  Online peers      : {len(peers)}")
                print(f"  Total transactions: {summary['total_transactions']}")
                print(f"  Total minted      : {summary['total_minted']:.8f} TMPL")
                print(f"  Remaining supply  : {summary['remaining_supply']:.8f} TMPL")
                print(f"  Bootstrap         : {BOOTSTRAP_HOST}:{BOOTSTRAP_PORT}\n")

            elif raw == "send":
                self._sending = True
                peers = self.network.get_online_peers()
                if not peers:
                    print("\n  No peers online yet.\n")
                    continue
                peer_list = list(peers.items())
                print(f"\n  Online peers:")
                for i, (pid, info) in enumerate(peer_list):
                    print(f"  [{i+1}] {pid[:24]}... — {info['ip']}")
                try:
                    choice = input("\n  Select peer number: ").strip()
                    idx = int(choice) - 1
                    if idx < 0 or idx >= len(peer_list):
                        print("  Invalid selection.\n")
                        continue
                    peer_id = peer_list[idx][0]
                except (ValueError, IndexError):
                    print("  Invalid selection.\n")
                    continue
                balance = self.ledger.get_balance(self.wallet.device_id)
                try:
                    amount_str = input(f"  Amount to send (balance: {balance:.8f}): ").strip()
                    amount = float(amount_str)
                except ValueError:
                    print("  Invalid amount.\n")
                    continue
                self.send(peer_id, amount)
                self._sending = False

            elif raw == "history":
                my_id = self.wallet.device_id
                my_tx = [
                    tx for tx in self.ledger.transactions
                    if tx["sender_id"] == my_id or tx["recipient_id"] == my_id
                ]
                my_rewards = [r for r in self.ledger.rewards if r["winner_id"] == my_id]
                if not my_tx and not my_rewards:
                    print("\n  No transactions yet.\n")
                else:
                    print(f"\n  Your transaction history:")
                    for r in my_rewards[-5:]:
                        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["timestamp"]))
                        print(f"  ★ REWARD   +{r['amount']:.8f} TMPL  [{t}]")
                    for tx in my_tx[-10:]:
                        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tx["timestamp"]))
                        if tx["sender_id"] == my_id:
                            print(f"  ↑ SENT     {tx['amount']:.8f} TMPL  to   {tx['recipient_id'][:16]}...  [{t}]")
                        else:
                            print(f"  ↓ RECEIVED {tx['amount']:.8f} TMPL  from {tx['sender_id'][:16]}...  [{t}]")
                    print()

            elif raw in ("quit", "exit", "q"):
                print("\n  Shutting down. Goodbye.\n")
                self.network.stop()
                break

            else:
                print(f"\n  Unknown command. Try: balance | peers | send | history | network | quit\n")


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "send":
        if len(sys.argv) != 4:
            print("Usage: python3 timpal.py send <address> <amount>")
            sys.exit(1)
        recipient_id = sys.argv[2]
        try:
            amount = float(sys.argv[3])
        except ValueError:
            print("Invalid amount.")
            sys.exit(1)
        wallet = Wallet()
        ledger = Ledger()
        if not os.path.exists(WALLET_FILE):
            print("No wallet found. Run python3 timpal.py first.")
            sys.exit(1)
        wallet.load()
        balance = ledger.get_balance(wallet.device_id)
        if amount <= 0 or balance < amount:
            print(f"Insufficient balance. You have {balance:.8f} TMPL.")
            sys.exit(1)
        # Send via control socket to running node — one ledger, one source of truth
        import socket as _socket
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(("127.0.0.1", 7780))
            cmd = json.dumps({"action": "send", "peer_id": recipient_id, "amount": amount}) + "\n"
            sock.sendall(cmd.encode())
            resp = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk: break
                resp += chunk
                if resp.endswith(b"\n"): break
            sock.close()
            result = json.loads(resp.decode().strip())
            if result.get("ok"):
                ledger2 = Ledger()
                print(f"  > Sent {amount:.8f} TMPL to {recipient_id[:24]}...")
                print(f"New balance: {ledger2.get_balance(wallet.device_id):.8f} TMPL")
            else:
                print(f"  Transaction failed: {result.get('error', 'unknown error')}")
        except ConnectionRefusedError:
            print("  Node is not running. Start your node first with: python3 timpal.py")
        except Exception as e:
            print(f"  Error: {e}")
        sys.exit(0)

    elif len(sys.argv) >= 2 and sys.argv[1] == "balance":
        wallet = Wallet()
        ledger = Ledger()
        if not os.path.exists(WALLET_FILE):
            print("No wallet found. Run python3 timpal.py first.")
            sys.exit(1)
        wallet.load()
        balance = ledger.get_balance(wallet.device_id)
        print(f"Balance : {balance:.8f} TMPL")
        print(f"Address : {wallet.device_id}")
        sys.exit(0)

    else:
        node = Node()
        node.start()
