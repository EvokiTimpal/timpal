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
BOOTSTRAP_HOST      = "5.78.187.91"   # Hetzner bootstrap node
BOOTSTRAP_PORT      = 7777
BOOTSTRAP_NODE_PORT = 7779            # Server node — always online
BROADCAST_PORT     = 7778
DISCOVERY_INTERVAL = 5
WALLET_FILE        = __import__("os").path.join(__import__("os").path.expanduser("~"), ".timpal_wallet.json")
LEDGER_FILE        = __import__("os").path.join(__import__("os").path.expanduser("~"), ".timpal_ledger.json")

# Supply constants
TOTAL_SUPPLY       = 250_000_000.0   # 250 million TMPL total
REWARD_PER_ROUND   = 0.6345          # TMPL per 3-second round
REWARD_INTERVAL    = 3.0             # Seconds between reward rounds
TX_FEE             = 0.0             # Free for first 37.5 years
TX_FEE_ERA2        = 0.0005          # Fee after all coins distributed

def get_current_fee(total_minted: float) -> float:
    """Era 1 (0-250M minted): Free. Era 2 (250M minted): 0.0005 TMPL to broadcaster."""
    if total_minted >= TOTAL_SUPPLY:
        return TX_FEE_ERA2
    return TX_FEE


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
        self._lock         = threading.Lock()
        self._load()

    def _load(self):
        if os.path.exists(LEDGER_FILE):
            try:
                with open(LEDGER_FILE, "r") as f:
                    data = json.load(f)
                self.transactions = data.get("transactions", [])
                self.rewards      = data.get("rewards", [])
                self.total_minted = data.get("total_minted", 0.0)
            except Exception:
                pass

    def save(self):
        with open(LEDGER_FILE, "w") as f:
            json.dump({
                "version":      VERSION,
                "transactions": self.transactions,
                "rewards":      self.rewards,
                "total_minted": self.total_minted
            }, f, indent=2)

    def get_balance(self, device_id: str) -> float:
        """Calculate balance from complete ledger history."""
        balance = 0.0
        for tx in self.transactions:
            if tx["recipient_id"] == device_id:
                balance += tx["amount"]
            if tx["sender_id"] == device_id:
                balance -= tx["amount"]
        for reward in self.rewards:
            if reward["winner_id"] == device_id:
                balance += reward["amount"]
        return round(balance, 8)

    def has_transaction(self, tx_id: str) -> bool:
        return any(tx["tx_id"] == tx_id for tx in self.transactions)

    def can_spend(self, device_id: str, amount: float) -> bool:
        return self.get_balance(device_id) >= amount

    def add_transaction(self, tx_dict: dict) -> bool:
        """Add a verified transaction to the ledger."""
        with self._lock:
            if self.has_transaction(tx_dict["tx_id"]):
                return False
            if not self.can_spend(tx_dict["sender_id"], tx_dict["amount"]):
                return False
            self.transactions.append(tx_dict)
            self.save()
            return True

    def add_reward(self, reward_dict: dict) -> bool:
        """Add a node reward to the ledger."""
        with self._lock:
            if any(r["reward_id"] == reward_dict["reward_id"] for r in self.rewards):
                return False
            slot = reward_dict.get("time_slot")
            if slot and any(r.get("time_slot") == slot for r in self.rewards):
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
        """Merge — one winner per time_slot, earliest timestamp wins conflict."""
        with self._lock:
            changed = False
            for tx in other_ledger.get("transactions", []):
                if not self.has_transaction(tx["tx_id"]):
                    if self.can_spend(tx["sender_id"], tx["amount"]):
                        self.transactions.append(tx)
                        changed = True
            existing_slots = {r.get("time_slot"): r for r in self.rewards}
            for reward in other_ledger.get("rewards", []):
                rid  = reward["reward_id"]
                slot = reward.get("time_slot")
                if any(r["reward_id"] == rid for r in self.rewards):
                    continue
                if slot and slot in existing_slots:
                    existing = existing_slots[slot]
                    if reward.get("timestamp", 0) < existing.get("timestamp", 0):
                        self.rewards = [r for r in self.rewards if r.get("time_slot") != slot]
                        self.rewards.append(reward)
                        existing_slots[slot] = reward
                        changed = True
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
                 amount, timestamp=None, tx_id=None):
        self.tx_id         = tx_id or str(uuid.uuid4())
        self.sender_id     = sender_id
        self.recipient_id  = recipient_id
        self.sender_pubkey = sender_pubkey
        self.amount        = amount
        self.timestamp     = timestamp or time.time()
        self.signature     = None

    def _payload(self) -> bytes:
        return f"{self.tx_id}:{self.sender_id}:{self.recipient_id}:{self.amount:.8f}:{self.timestamp:.4f}".encode()

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

    def start(self):
        self._running = True
        threading.Thread(target=self._listen_tcp,        daemon=True).start()
        threading.Thread(target=self._listen_discovery,  daemon=True).start()
        threading.Thread(target=self._broadcast_loop,    daemon=True).start()
        threading.Thread(target=self._bootstrap_connect, daemon=True).start()

    def stop(self):
        self._running = False

    # ── Bootstrap connection ────────────────────

    def _connect_to_server_node(self):
        """Directly connect to the always-on server node as a peer.
        This bypasses NAT issues — clients connect TO the server, not vice versa."""
        time.sleep(3)
        server_id = None
        while self._running:
            try:
                # Don't connect to ourselves
                if self.local_ip == BOOTSTRAP_HOST:
                    return
                # Check if already connected
                if any(p.get("ip") == BOOTSTRAP_HOST for p in self.peers.values()):
                    time.sleep(30)
                    continue
                # Connect to server node
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((BOOTSTRAP_HOST, BOOTSTRAP_NODE_PORT))
                sock.sendall(json.dumps({"type": "HELLO", "device_id": self.wallet.device_id}).encode())
                sock.shutdown(socket.SHUT_WR)  # Signal end of message
                resp = sock.recv(4096)
                sock.close()
                data = json.loads(resp.decode())
                if data.get("type") == "HELLO_ACK":
                    server_id = data.get("device_id")
                    if server_id and server_id != self.wallet.device_id:
                        self.peers[server_id] = {
                            "ip":        BOOTSTRAP_HOST,
                            "port":      BOOTSTRAP_NODE_PORT,
                            "last_seen": time.time()
                        }
                        print(f"\n  [+] Connected to server node\n  > ", end="", flush=True)
                        threading.Thread(target=self._sync_ledger, daemon=True).start()
            except Exception:
                pass
            time.sleep(30)

    def _bootstrap_connect(self):
        """Connect to bootstrap server and get initial peer list."""
        time.sleep(2)
        # Also connect directly to the bootstrap node as a peer
        # This ensures server and clients are always connected
        threading.Thread(target=self._connect_to_server_node, daemon=True).start()
        while self._running:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10.0)
                sock.connect((BOOTSTRAP_HOST, BOOTSTRAP_PORT))
                msg = json.dumps({
                    "type":      "HELLO",
                    "device_id": self.wallet.device_id,
                    "port":      self.port
                }).encode()
                sock.sendall(msg)
                response = sock.recv(65536)
                sock.close()

                data = json.loads(response.decode())
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
                        print(f"\n  [+] Bootstrap: found {new_peers} peers worldwide")
                        print(f"  Network size: {data.get('network_size', 0)} nodes")
                        print(f"  > ", end="", flush=True)
                        # Sync ledger with a peer
                        threading.Thread(target=self._sync_ledger, daemon=True).start()

            except Exception:
                pass

            # Re-register with bootstrap every 2 minutes
            time.sleep(120)

    def _sync_ledger(self):
        """Request full ledger from a random peer."""
        time.sleep(1)
        peers = self.get_online_peers()
        if not peers:
            return
        peer_id = random.choice(list(peers.keys()))
        peer    = peers[peer_id]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((peer["ip"], peer["port"]))
            sock.sendall(json.dumps({"type": "GET_LEDGER"}).encode())
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            sock.close()
            msg = json.loads(data.decode())
            if msg.get("type") == "LEDGER":
                merged = self.ledger.merge(msg["ledger"])
                if merged:
                    print(f"\n  [+] Ledger synchronized with network")
                    print(f"  > ", end="", flush=True)
        except Exception:
            pass

    # ── Local discovery ─────────────────────────

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while self._running:
            msg = json.dumps({
                "type":      "HELLO",
                "device_id": self.wallet.device_id,
                "ip":        self.local_ip,
                "port":      self.port
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
                    peer_id   = msg["device_id"]
                    peer_ip   = msg["ip"]
                    peer_port = msg.get("port", 7779)
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
                peer_id = msg.get("device_id")
                if peer_id and peer_id != self.wallet.device_id:
                    self.peers[peer_id] = {
                        "ip":        addr[0],
                        "port":      msg.get("port", 7779),
                        "last_seen": time.time()
                    }
                    response = json.dumps({
                        "type":      "HELLO_ACK",
                        "device_id": self.wallet.device_id
                    }).encode()
                    conn.sendall(response)
                    threading.Thread(target=self._sync_ledger, daemon=True).start()

            elif msg_type == "TRANSACTION":
                tx = Transaction.from_dict(msg["transaction"])
                self.on_transaction(tx)

            elif msg_type == "VRF_TICKET":
                device_id = msg.get("device_id")
                time_slot = msg.get("time_slot")
                ticket    = msg.get("ticket")
                if device_id and time_slot and ticket:
                    node = self._node_ref
                    if node and hasattr(node, "_vrf_tickets"):
                        if Node._verify_ticket(device_id, time_slot, ticket):
                            with node._vrf_lock:
                                if time_slot not in node._vrf_tickets:
                                    node._vrf_tickets[time_slot] = {}
                                node._vrf_tickets[time_slot][device_id] = ticket

            elif msg_type == "VRF_TICKET":
                device_id = msg.get("device_id")
                time_slot = msg.get("time_slot")
                ticket    = msg.get("ticket")
                if device_id and time_slot and ticket:
                    node = self._node_ref
                    if node and hasattr(node, "_vrf_tickets"):
                        if Node._verify_ticket(device_id, time_slot, ticket):
                            with node._vrf_lock:
                                if time_slot not in node._vrf_tickets:
                                    node._vrf_tickets[time_slot] = {}
                                node._vrf_tickets[time_slot][device_id] = ticket

            elif msg_type == "REWARD":
                self.on_reward(msg["reward"])

            elif msg_type == "GET_LEDGER":
                response = json.dumps({
                    "type":   "LEDGER",
                    "ledger": self.ledger.to_dict()
                }).encode()
                conn.sendall(response)



        except Exception:
            pass
        finally:
            conn.close()

    # ── Broadcast to all peers ───────────────────

    def broadcast(self, message: dict):
        """Send a message to all known online peers."""
        msg_bytes = json.dumps(message).encode()
        peers = self.get_online_peers()
        for peer_id, peer in peers.items():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((peer["ip"], peer["port"]))
                sock.sendall(msg_bytes)
                sock.close()
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
        self.network._node_ref = self

    def _acquire_lock(self):
        import fcntl
        lock_path = __import__("os").path.join(__import__("os").path.expanduser("~"), ".timpal.lock")
        self._lock_file = open(lock_path, "w")
        try:
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

    def _on_transaction_received(self, tx: Transaction):
        if tx.tx_id in self.network.seen_ids:
            return
        self.network.seen_ids.add(tx.tx_id)

        if not tx.verify():
            return

        if tx.amount <= 0:
            return

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
        reward_id = reward.get("reward_id", "")
        if reward_id in self.network.seen_ids:
            return
        self.network.seen_ids.add(reward_id)

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
            print(f"  ║       REWARD WON! ★              ║")
            print(f"  ╠══════════════════════════════════╣")
            print(f"  ║  Amount  : {reward['amount']:.8f} TMPL")
            print(f"  ║  Balance : {balance:.8f} TMPL")
            print(f"  ╚══════════════════════════════════╝")
            print(f"  > ", end="", flush=True)

    def _vrf_ticket(self, time_slot: int) -> str:
        """Each node computes a unique verifiable ticket for each time slot.
        Ticket = SHA256(device_id + time_slot)
        The node with the LOWEST ticket value wins. Scales to millions of nodes."""
        data = f"{self.wallet.device_id}:{time_slot}".encode()
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _verify_ticket(device_id: str, time_slot: int, ticket: str) -> bool:
        expected = hashlib.sha256(f"{device_id}:{time_slot}".encode()).hexdigest()
        return ticket == expected

    def _reward_lottery(self):
        """VRF-based lottery. Scales to millions of nodes.
        No peer list needed. Every node computes its own ticket.
        Lowest ticket wins. Winner is verifiable by anyone."""
        self._vrf_tickets = {}
        self._vrf_lock = threading.Lock()

        while self.network._running:
            time.sleep(REWARD_INTERVAL)

            if self.ledger.total_minted >= TOTAL_SUPPLY:
                continue

            time_slot = int(time.time() / REWARD_INTERVAL)
            my_ticket = self._vrf_ticket(time_slot)

            with self._vrf_lock:
                if time_slot not in self._vrf_tickets:
                    self._vrf_tickets[time_slot] = {}
                self._vrf_tickets[time_slot][self.wallet.device_id] = my_ticket

            self.network.broadcast({
                "type":      "VRF_TICKET",
                "device_id": self.wallet.device_id,
                "time_slot": time_slot,
                "ticket":    my_ticket
            })

            time.sleep(REWARD_INTERVAL * 0.4)

            with self._vrf_lock:
                slot_tickets = dict(self._vrf_tickets.get(time_slot, {}))
                slot_tickets[self.wallet.device_id] = my_ticket

            if not slot_tickets:
                continue

            winner_id = min(slot_tickets, key=lambda d: slot_tickets[d])

            if winner_id == self.wallet.device_id:
                reward_id = f"reward:{time_slot}"
                if any(r["reward_id"] == reward_id for r in self.ledger.rewards):
                    continue
                reward = {
                    "reward_id":  reward_id,
                    "winner_id":  self.wallet.device_id,
                    "amount":     REWARD_PER_ROUND,
                    "timestamp":  time.time(),
                    "time_slot":  time_slot,
                    "vrf_ticket": my_ticket,
                    "nodes":      len(slot_tickets)
                }
                added = self.ledger.add_reward(reward)
                if added:
                    self.network.broadcast({"type": "REWARD", "reward": reward})
                    balance = self.ledger.get_balance(self.wallet.device_id)
                    print(f"\n  ★ Reward won! +{REWARD_PER_ROUND} TMPL | Balance: {balance:.8f}\n  > ", end="", flush=True)

            with self._vrf_lock:
                old_slots = [s for s in self._vrf_tickets if s < time_slot - 5]
                for s in old_slots:
                    del self._vrf_tickets[s]

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

        tx = Transaction(
            sender_id     = self.wallet.device_id,
            recipient_id  = peer_id,
            sender_pubkey = self.wallet.get_public_key_hex(),
            amount        = amount
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
