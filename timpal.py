#!/usr/bin/env python3
"""
TIMPAL Protocol v1.0
Plan B for Humanity

A simple peer-to-peer value transfer protocol that works on any
local network with zero external infrastructure.

Usage:
    python3 timpal.py

Two instances on the same WiFi will find each other automatically.
"""

import socket
import threading
import json
import hashlib
import os
import time
import uuid
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey
)
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption
)

# ─────────────────────────────────────────────
# PROTOCOL CONSTANTS — NEVER CHANGE
# ─────────────────────────────────────────────
VERSION         = "1.0"
PORT            = 7777          # All Timpal devices listen on this port
BROADCAST_PORT  = 7778          # Discovery broadcast port
DISCOVERY_INTERVAL = 5          # Seconds between discovery broadcasts
NEW_TMPL_PER_TX = 0.001         # New TMPL created per genuine transaction (split equally)
WALLET_FILE     = "wallet.json" # Local wallet storage


# ─────────────────────────────────────────────
# WALLET — Identity and balance storage
# Lives entirely on this device. Never leaves.
# ─────────────────────────────────────────────

class Wallet:
    def __init__(self):
        self.private_key = None
        self.public_key  = None
        self.device_id   = None
        self.balance     = 0.0
        self.transactions = []

    def create_new(self):
        """Generate a brand new cryptographic identity for this device."""
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key  = self.private_key.public_key()
        self.device_id   = self._derive_device_id()
        self.balance     = 0.0
        self.transactions = []
        print(f"\n  New wallet created.")
        print(f"  Device ID: {self.device_id[:24]}...")

    def _derive_device_id(self):
        """Derive a stable device ID from the public key."""
        pub_bytes = self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        return hashlib.sha256(pub_bytes).hexdigest()

    def save(self, path=WALLET_FILE):
        """Save wallet to disk."""
        priv_bytes = self.private_key.private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
        data = {
            "version":      VERSION,
            "device_id":    self.device_id,
            "private_key":  priv_bytes.hex(),
            "balance":      self.balance,
            "transactions": self.transactions
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def load(self, path=WALLET_FILE):
        """Load wallet from disk."""
        with open(path, "r") as f:
            data = json.load(f)
        priv_bytes       = bytes.fromhex(data["private_key"])
        self.private_key = Ed25519PrivateKey.from_private_bytes(priv_bytes)
        self.public_key  = self.private_key.public_key()
        self.device_id   = data["device_id"]
        self.balance     = data["balance"]
        self.transactions = data.get("transactions", [])

    def get_public_key_hex(self):
        pub_bytes = self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        return pub_bytes.hex()

    def sign(self, message: bytes) -> str:
        """Sign a message with this device's private key."""
        signature = self.private_key.sign(message)
        return signature.hex()

    @staticmethod
    def verify_signature(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
        """Verify a signature from any device."""
        try:
            pub_bytes = bytes.fromhex(public_key_hex)
            pub_key   = Ed25519PublicKey.from_public_bytes(pub_bytes)
            sig_bytes = bytes.fromhex(signature_hex)
            pub_key.verify(sig_bytes, message)
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────
# TRANSACTION — The atomic unit of value transfer
# ─────────────────────────────────────────────

class Transaction:
    def __init__(self, sender_id, recipient_id, sender_pubkey,
                 amount, timestamp=None, tx_id=None):
        self.tx_id        = tx_id or str(uuid.uuid4())
        self.sender_id    = sender_id
        self.recipient_id = recipient_id
        self.sender_pubkey = sender_pubkey
        self.amount       = amount
        self.timestamp    = timestamp or time.time()
        self.signature    = None
        # New TMPL minted by this transaction — shared equally
        self.minted       = NEW_TMPL_PER_TX

    def _payload(self) -> bytes:
        """The canonical bytes that get signed — order matters."""
        payload = f"{self.tx_id}:{self.sender_id}:{self.recipient_id}:{self.amount:.8f}:{self.timestamp:.4f}"
        return payload.encode()

    def sign(self, wallet: Wallet):
        """Sign this transaction with the sender's wallet."""
        self.signature = wallet.sign(self._payload())

    def verify(self) -> bool:
        """Verify the transaction signature is valid."""
        if not self.signature:
            return False
        return Wallet.verify_signature(
            self.sender_pubkey,
            self._payload(),
            self.signature
        )

    def to_dict(self) -> dict:
        return {
            "tx_id":        self.tx_id,
            "sender_id":    self.sender_id,
            "recipient_id": self.recipient_id,
            "sender_pubkey": self.sender_pubkey,
            "amount":       self.amount,
            "timestamp":    self.timestamp,
            "signature":    self.signature,
            "minted":       self.minted
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        tx = cls(
            sender_id    = d["sender_id"],
            recipient_id = d["recipient_id"],
            sender_pubkey = d["sender_pubkey"],
            amount       = d["amount"],
            timestamp    = d["timestamp"],
            tx_id        = d["tx_id"]
        )
        tx.signature = d.get("signature")
        tx.minted    = d.get("minted", NEW_TMPL_PER_TX)
        return tx


# ─────────────────────────────────────────────
# NETWORK — Peer discovery and messaging
# Uses UDP broadcast to find peers on the same
# network. Uses TCP to exchange transactions.
# No server. No internet. Just devices.
# ─────────────────────────────────────────────

class Network:
    def __init__(self, wallet: Wallet, on_transaction):
        self.wallet         = wallet
        self.on_transaction = on_transaction   # callback when tx received
        self.peers          = {}               # device_id -> (ip, last_seen)
        self.seen_tx_ids    = set()            # prevent double-processing
        self._running       = False
        self.local_ip       = self._get_local_ip()

    def _get_local_ip(self):
        """Get this machine's local network IP."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def start(self):
        """Start all network threads."""
        self._running = True
        threading.Thread(target=self._listen_tcp,       daemon=True).start()
        threading.Thread(target=self._listen_discovery, daemon=True).start()
        threading.Thread(target=self._broadcast_loop,   daemon=True).start()

    def stop(self):
        self._running = False

    # ── Discovery ──────────────────────────────

    def _broadcast_loop(self):
        """Periodically announce this device to the local network."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        msg = json.dumps({
            "type":      "HELLO",
            "device_id": self.wallet.device_id,
            "pubkey":    self.wallet.get_public_key_hex(),
            "ip":        self.local_ip,
            "balance":   self.wallet.balance
        }).encode()
        while self._running:
            try:
                sock.sendto(msg, ("<broadcast>", BROADCAST_PORT))
            except Exception:
                pass
            time.sleep(DISCOVERY_INTERVAL)

    def _listen_discovery(self):
        """Listen for other devices announcing themselves."""
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
                    peer_id = msg["device_id"]
                    peer_ip = msg["ip"]
                    # Don't add ourselves
                    if peer_id != self.wallet.device_id:
                        is_new = peer_id not in self.peers
                        self.peers[peer_id] = {
                            "ip":      peer_ip,
                            "pubkey":  msg.get("pubkey", ""),
                            "last_seen": time.time(),
                            "balance": msg.get("balance", 0)
                        }
                        if is_new:
                            print(f"\n  [+] Peer found: {peer_id[:20]}... at {peer_ip}")
                            print(f"  Type 'send' to transfer TMPL\n  > ", end="", flush=True)
            except socket.timeout:
                continue
            except Exception:
                continue

    # ── Transaction Exchange ────────────────────

    def _listen_tcp(self):
        """Listen for incoming transactions from peers."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", PORT))
        sock.listen(10)
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
        """Handle an incoming transaction from a peer."""
        try:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            msg = json.loads(data.decode())
            if msg.get("type") == "TRANSACTION":
                tx = Transaction.from_dict(msg["transaction"])
                self.on_transaction(tx)
        except Exception as e:
            pass
        finally:
            conn.close()

    def send_transaction(self, tx: Transaction, peer_id: str) -> bool:
        """Send a transaction directly to a specific peer."""
        if peer_id not in self.peers:
            print(f"\n  Peer not found: {peer_id[:20]}...")
            return False

        peer_ip = self.peers[peer_id]["ip"]
        msg = json.dumps({
            "type":        "TRANSACTION",
            "transaction": tx.to_dict()
        }).encode()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((peer_ip, PORT))
            sock.sendall(msg)
            sock.close()
            return True
        except Exception as e:
            print(f"\n  Could not reach peer: {e}")
            return False

    def get_online_peers(self):
        """Return peers seen in the last 15 seconds."""
        cutoff = time.time() - 15
        return {
            pid: info for pid, info in self.peers.items()
            if info["last_seen"] > cutoff
        }


# ─────────────────────────────────────────────
# NODE — The complete Timpal device
# Combines wallet + network + transaction logic
# ─────────────────────────────────────────────

class Node:
    def __init__(self):
        self.wallet  = Wallet()
        self.network = None
        self._load_or_create_wallet()
        self.network = Network(self.wallet, self._on_transaction_received)

    def _load_or_create_wallet(self):
        """Load existing wallet or create a new one."""
        if os.path.exists(WALLET_FILE):
            self.wallet.load()
            print(f"\n  Wallet loaded.")
            print(f"  Device ID : {self.wallet.device_id[:24]}...")
            print(f"  Balance   : {self.wallet.balance:.8f} TMPL")
        else:
            self.wallet.create_new()
            self.wallet.save()

    def _on_transaction_received(self, tx: Transaction):
        """Handle a transaction received from another device."""

        # Ignore transactions we've already processed
        if tx.tx_id in self.network.seen_tx_ids:
            return
        self.network.seen_tx_ids.add(tx.tx_id)

        # Only process transactions meant for us
        if tx.recipient_id != self.wallet.device_id:
            return

        # Verify the cryptographic signature
        if not tx.verify():
            print(f"\n  [!] Invalid transaction signature — rejected")
            return

        # Verify amount is positive
        if tx.amount <= 0:
            return

        # Apply the transaction
        self.wallet.balance += tx.amount
        # Each participant gets half the newly minted TMPL
        minted_share = tx.minted / 2
        self.wallet.balance += minted_share

        # Record it
        record = {
            "type":      "received",
            "tx_id":     tx.tx_id,
            "from":      tx.sender_id[:20] + "...",
            "amount":    tx.amount,
            "minted":    minted_share,
            "timestamp": tx.timestamp,
            "time_str":  time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tx.timestamp))
        }
        self.wallet.transactions.append(record)
        self.wallet.save()

        print(f"\n")
        print(f"  ╔══════════════════════════════════════╗")
        print(f"  ║      TMPL RECEIVED                   ║")
        print(f"  ╠══════════════════════════════════════╣")
        print(f"  ║  Amount   : {tx.amount:.8f} TMPL          ")
        print(f"  ║  Minted   : +{minted_share:.8f} TMPL (your share)")
        print(f"  ║  From     : {tx.sender_id[:20]}...")
        print(f"  ║  New balance: {self.wallet.balance:.8f} TMPL")
        print(f"  ╚══════════════════════════════════════╝")
        print(f"  > ", end="", flush=True)

    def send(self, peer_id: str, amount: float) -> bool:
        """Send TMPL to a peer."""

        # Validations
        if amount <= 0:
            print("\n  Amount must be greater than zero.")
            return False

        if amount > self.wallet.balance:
            print(f"\n  Insufficient balance.")
            print(f"  Your balance : {self.wallet.balance:.8f} TMPL")
            print(f"  Requested    : {amount:.8f} TMPL")
            return False

        if peer_id not in self.network.get_online_peers():
            print(f"\n  Peer is not online.")
            return False

        peer_info = self.network.peers[peer_id]

        # Build and sign the transaction
        tx = Transaction(
            sender_id    = self.wallet.device_id,
            recipient_id = peer_id,
            sender_pubkey = self.wallet.get_public_key_hex(),
            amount       = amount
        )
        tx.sign(self.wallet)

        # Send it
        success = self.network.send_transaction(tx, peer_id)

        if success:
            # Deduct from our balance
            self.wallet.balance -= amount
            # We also get our half of the newly minted TMPL
            minted_share = tx.minted / 2
            self.wallet.balance += minted_share

            # Record it
            record = {
                "type":      "sent",
                "tx_id":     tx.tx_id,
                "to":        peer_id[:20] + "...",
                "amount":    amount,
                "minted":    minted_share,
                "timestamp": tx.timestamp,
                "time_str":  time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tx.timestamp))
            }
            self.wallet.transactions.append(record)
            self.wallet.save()

            print(f"\n  ✓ Sent {amount:.8f} TMPL")
            print(f"  ✓ Minted +{minted_share:.8f} TMPL (your share)")
            print(f"  New balance: {self.wallet.balance:.8f} TMPL")
            return True
        else:
            print(f"\n  Transaction failed — peer unreachable.")
            return False

    def start(self):
        """Start the node and the interactive CLI."""
        print("\n" + "═" * 50)
        print("  TIMPAL v1.0 — Plan B for Humanity")
        print("═" * 50)
        print(f"  Device ID : {self.wallet.device_id[:24]}...")
        print(f"  Balance   : {self.wallet.balance:.8f} TMPL")
        print(f"  Network   : {self.network.local_ip}:{PORT}")
        print("═" * 50)
        print("  Searching for peers on your network...")
        print("  Commands: balance | peers | send | history | genesis | quit")
        print("═" * 50 + "\n")

        self.network.start()
        self._cli()

    def _cli(self):
        """Simple command line interface."""
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
                print(f"\n  Balance: {self.wallet.balance:.8f} TMPL")
                print(f"  Device : {self.wallet.device_id[:24]}...\n")

            elif raw == "peers":
                peers = self.network.get_online_peers()
                if not peers:
                    print("\n  No peers found yet. Searching...\n")
                else:
                    print(f"\n  Online peers ({len(peers)}):")
                    for i, (pid, info) in enumerate(peers.items()):
                        print(f"  [{i+1}] {pid[:24]}... — {info['ip']}")
                    print()

            elif raw == "send":
                peers = self.network.get_online_peers()
                if not peers:
                    print("\n  No peers online yet.\n")
                    continue

                # Show peers
                peer_list = list(peers.items())
                print(f"\n  Online peers:")
                for i, (pid, info) in enumerate(peer_list):
                    print(f"  [{i+1}] {pid[:24]}... — {info['ip']}")

                # Select peer
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

                # Enter amount
                try:
                    amount_str = input(f"  Amount to send (balance: {self.wallet.balance:.8f}): ").strip()
                    amount = float(amount_str)
                except ValueError:
                    print("  Invalid amount.\n")
                    continue

                self.send(peer_id, amount)

            elif raw == "history":
                if not self.wallet.transactions:
                    print("\n  No transactions yet.\n")
                else:
                    print(f"\n  Transaction history ({len(self.wallet.transactions)}):")
                    for tx in self.wallet.transactions[-10:]:  # Last 10
                        if tx["type"] == "sent":
                            print(f"  ↑ SENT     {tx['amount']:.8f} TMPL  to  {tx['to']}  [{tx['time_str']}]")
                        else:
                            print(f"  ↓ RECEIVED {tx['amount']:.8f} TMPL  from {tx['from']}  [{tx['time_str']}]")
                    print()

            elif raw == "genesis":
                if self.wallet.transactions:
                    print("\n  Genesis already used. This command only works once on a brand new wallet.\n")
                elif self.wallet.balance > 0:
                    print("\n  This wallet already has a balance.\n")
                else:
                    self.wallet.balance = 100.0
                    record = {
                        "type":      "genesis",
                        "tx_id":     "genesis",
                        "amount":    100.0,
                        "timestamp": time.time(),
                        "time_str":  time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    self.wallet.transactions.append(record)
                    self.wallet.save()
                    print(f"\n  ✓ Genesis block created.")
                    print(f"  100.00000000 TMPL added to start the network.")
                    print(f"  This can only happen once on this device.\n")

            elif raw in ("quit", "exit", "q"):
                print("\n  Shutting down. Goodbye.\n")
                self.network.stop()
                break

            else:
                print(f"\n  Unknown command. Try: balance | peers | send | history | quit\n")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    node = Node()
    node.start()
