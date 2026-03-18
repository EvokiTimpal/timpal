#!/usr/bin/env python3
"""
TIMPAL Protocol v2.1 — Quantum-Resistant Money Without Masters

Quantum-resistant. Worldwide. Instant transactions.
Distributed ledger. No banks. No servers. No control.

Install dependencies:
    pip3 install dilithium-py cryptography

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
import ssl

# Quantum-resistant cryptography — Dilithium3 (NIST PQC Standard 2024)
try:
    from dilithium_py.dilithium import Dilithium3
    QUANTUM_RESISTANT = True
except ImportError:
    print("\n  [!] dilithium-py not installed.")
    print("  Run: pip3 install dilithium-py cryptography")
    print("  Then restart Timpal.\n")
    exit(1)

# Wallet encryption — AES-256-GCM with scrypt key derivation
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.hazmat.backends import default_backend
    ENCRYPTION_AVAILABLE = True
except ImportError:
    print("\n  [!] cryptography not installed.")
    print("  Run: pip3 install dilithium-py cryptography")
    print("  Then restart Timpal.\n")
    exit(1)

# ─────────────────────────────────────────────
# PROTOCOL CONSTANTS — NEVER CHANGE
# ─────────────────────────────────────────────
VERSION            = "2.1"
MIN_VERSION        = "2.1"   # Minimum version allowed to connect

# ── GENESIS_TIME ─────────────────────────────────────────────────────────────
# Set this ONCE before the final network launch — never change it after.
# Run this command and paste the result below:
#     python3 -c "import time; print(int(time.time()))"
# Set the SAME value in bootstrap.py
GENESIS_TIME       = 0        # ← REPLACE 0 with the number from the command above

# ── ERA2_SLOT ─────────────────────────────────────────────────────────────────
# Slot at which Era 2 begins (all 250M coins distributed).
# 250,000,000 / 1.0575 = 236,406,620 slots from genesis.
# Every node reaches Era 2 at the EXACT same moment. No disagreement ever.
ERA2_SLOT          = 236_406_620

BOOTSTRAP_SERVERS   = [
    ("bootstrap.timpal.org", 7777),   # Timpal foundation — always running
    # Community bootstrap servers can be added here
]
BOOTSTRAP_HOST      = "bootstrap.timpal.org"   # Primary (used for peer registration)
BOOTSTRAP_PORT      = 7777
BOOTSTRAP_LIST_URL  = "https://raw.githubusercontent.com/EvokiTimpal/timpal/main/bootstrap_servers.txt"
BROADCAST_PORT      = 7778
DISCOVERY_INTERVAL  = 5
WALLET_FILE         = os.path.join(os.path.expanduser("~"), ".timpal_wallet.json")
LEDGER_FILE         = os.path.join(os.path.expanduser("~"), ".timpal_ledger.json")
BOOTSTRAP_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".timpal_bootstrap.json")

# Supply constants
TOTAL_SUPPLY        = 250_000_000.0  # 250 million TMPL total
REWARD_PER_ROUND    = 1.0575         # TMPL per 5-second round
REWARD_INTERVAL     = 5.0            # Seconds between reward rounds
TX_FEE              = 0.0            # Free for first 37.5 years (Era 1)
TX_FEE_ERA2         = 0.0005         # Fee after all coins distributed — split among active nodes
CHECKPOINT_INTERVAL = 241_920        # Slots between checkpoints (~2 weeks)
CHECKPOINT_BUFFER   = 120            # Slots to wait before pruning (~10 minutes)
MAX_PEERS           = 125            # Max peers stored in node peers dict
BROADCAST_FANOUT    = 8              # Max peers to broadcast to per message
PUSH_SECRET         = "b7e2f4a1c9d3e8f2a5b1c4d7e0f3a6b9c2d5e8f1a4b7c0d3e6f9a2b5c8d1e4f7"


def _check_genesis_time():
    """Refuse to start if GENESIS_TIME has not been set."""
    if GENESIS_TIME == 0:
        print("\n  " + "═" * 52)
        print("  ERROR: GENESIS_TIME is not set.")
        print("  " + "═" * 52)
        print("  Run this command and copy the number:")
        print("  python3 -c \"import time; print(int(time.time()))\"")
        print("")
        print("  Then open timpal.py and replace:")
        print("  GENESIS_TIME = 0")
        print("  with:")
        print("  GENESIS_TIME = <the number you copied>")
        print("")
        print("  Do the same in bootstrap.py.")
        print("  " + "═" * 52 + "\n")
        exit(1)


def get_current_slot() -> int:
    """Return the current slot number relative to GENESIS_TIME.
    Same answer on every node at the same instant — no disagreement ever."""
    return int((time.time() - GENESIS_TIME) / REWARD_INTERVAL)


def is_era2() -> bool:
    """Era 2 begins at ERA2_SLOT — determined by time, not local minted amount.
    Every node transitions at the exact same slot. No disagreement ever."""
    return get_current_slot() >= ERA2_SLOT


def get_current_fee() -> float:
    """Era 1 (slots 0 to ERA2_SLOT-1): free. Era 2: 0.0005 TMPL per tx."""
    return TX_FEE_ERA2 if is_era2() else TX_FEE


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
        self.rewards       = []   # All node rewards ever (block_reward + fee_reward)
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
        """Atomic write — temp file + os.replace prevents corruption on crash."""
        tmp = LEDGER_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "version":      VERSION,
                "transactions": self.transactions,
                "rewards":      self.rewards,
                "total_minted": self.total_minted,
                "checkpoints":  self.checkpoints
            }, f, indent=2)
        os.replace(tmp, LEDGER_FILE)

    def get_balance(self, device_id: str) -> float:
        """Calculate balance from latest checkpoint + post-checkpoint history.
        Includes both block_rewards and fee_rewards — both are real spendable coins."""
        with self._lock:
            balance = 0.0
            if self.checkpoints:
                balance = self.checkpoints[-1].get("balances", {}).get(device_id, 0.0)
            for tx in self.transactions:
                if tx["recipient_id"] == device_id:
                    balance += tx["amount"]
                if tx["sender_id"] == device_id:
                    balance -= tx["amount"]
                    balance -= tx.get("fee", 0.0)
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
        Signature verified before acquiring lock — Dilithium3 takes ~1ms."""
        if tx_dict.get("amount", 0) <= 0:
            return False
        # Validate and verify outside the lock
        try:
            t = Transaction.from_dict(tx_dict)
            if not t.verify():
                return False
        except Exception:
            return False
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
        """Add an Era 2 fee reward. Fee rewards are split equally among
        all nodes that submitted a VRF commit for the slot."""
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
        """Add a node block reward. If two rewards claim the same slot,
        lowest VRF ticket wins. Verified cryptographically before acceptance."""
        with self._lock:
            # Cryptographically verify the VRF ticket before accepting
            public_key = reward_dict.get("vrf_public_key")
            seed       = reward_dict.get("vrf_seed")
            sig        = reward_dict.get("vrf_sig")
            ticket     = reward_dict.get("vrf_ticket")
            if public_key and seed and sig and ticket:
                if not Node._verify_ticket(public_key, seed, sig, ticket):
                    return False
            slot       = reward_dict.get("time_slot")
            new_ticket = reward_dict.get("vrf_ticket", "z")
            if slot is not None:
                existing = next(
                    (r for r in self.rewards
                     if r.get("time_slot") == slot and r.get("type") != "fee_reward"),
                    None
                )
                if existing:
                    if (existing.get("reward_id") == reward_dict["reward_id"] and
                            existing.get("winner_id") == reward_dict.get("winner_id")):
                        return False   # Exact same reward already stored
                    old_ticket = existing.get("vrf_ticket", "z")
                    if new_ticket < old_ticket:
                        # Incoming reward wins — replace existing
                        self.rewards = [r for r in self.rewards
                                        if r.get("time_slot") != slot or r.get("type") == "fee_reward"]
                        self.total_minted -= existing["amount"]
                    else:
                        return False   # Existing reward has lower or equal ticket — keep it
            else:
                if any(r["reward_id"] == reward_dict["reward_id"] for r in self.rewards):
                    return False
            # Use round() to avoid IEEE 754 drift near the supply cap
            if round(self.total_minted + reward_dict["amount"], 8) > TOTAL_SUPPLY:
                return False
            self.rewards.append(reward_dict)
            self.total_minted = round(self.total_minted + reward_dict["amount"], 8)
            self.save()
            return True

    def recalculate_totals(self):
        """Recompute total_minted from checkpoint base + remaining block rewards."""
        if self.checkpoints:
            cp          = self.checkpoints[-1]
            pruned_base = cp["total_minted"] - cp.get("kept_minted", 0)
            self.total_minted = round(
                pruned_base + sum(r["amount"] for r in self.rewards if r.get("type") == "block_reward"),
                8
            )
        else:
            self.total_minted = round(
                sum(r["amount"] for r in self.rewards if r.get("type") == "block_reward"),
                8
            )

    def get_summary(self):
        return {
            "total_transactions": len(self.transactions),
            "total_rewards":      len([r for r in self.rewards if r.get("type") == "block_reward"]),
            "total_minted":       self.total_minted,
            "remaining_supply":   round(TOTAL_SUPPLY - self.total_minted, 8)
        }

    def to_dict(self):
        return {
            "transactions": self.transactions,
            "rewards":      self.rewards,
            "total_minted": self.total_minted
        }

    def merge(self, other_ledger: dict):
        """Merge incoming ledger data — one winner per slot, lowest VRF ticket wins.
        Crypto verification happens outside the lock. All supply checks use a
        running counter so near-cap rewards are never incorrectly rejected."""
        # Pre-verify all transactions outside the lock
        verified_txs = []
        for tx in other_ledger.get("transactions", []):
            try:
                t = Transaction.from_dict(tx)
                if t.verify():
                    verified_txs.append(tx)
            except Exception:
                continue

        # Pre-verify all rewards outside the lock
        verified_rewards = []
        for reward in other_ledger.get("rewards", []):
            pub  = reward.get("vrf_public_key")
            seed = reward.get("vrf_seed")
            sig  = reward.get("vrf_sig")
            tick = reward.get("vrf_ticket")
            if pub and seed and sig and tick:
                if not Node._verify_ticket(pub, seed, sig, tick):
                    continue
            if reward.get("amount", 0) <= 0:
                continue
            verified_rewards.append(reward)

        with self._lock:
            changed = False

            # Merge transactions
            for tx in verified_txs:
                if not self.has_transaction(tx["tx_id"]):
                    total = tx["amount"] + tx.get("fee", 0.0)
                    if self.can_spend(tx["sender_id"], total):
                        self.transactions.append(tx)
                        changed = True

            # Merge rewards — track running total to keep supply cap accurate
            existing_slots = {
                r.get("time_slot"): r
                for r in self.rewards if r.get("type") == "block_reward"
            }
            running_minted = self.total_minted

            for reward in verified_rewards:
                rid    = reward["reward_id"]
                slot   = reward.get("time_slot")
                rtype  = reward.get("type", "block_reward")

                # Skip exact duplicates
                if any(r["reward_id"] == rid and r.get("winner_id") == reward.get("winner_id")
                       for r in self.rewards):
                    continue

                if slot is not None and rtype != "fee_reward" and slot in existing_slots:
                    existing   = existing_slots[slot]
                    old_ticket = existing.get("vrf_ticket", "z")
                    new_ticket = reward.get("vrf_ticket", "z")
                    if new_ticket < old_ticket:
                        # Incoming reward wins — remove old, adjust running total
                        self.rewards = [r for r in self.rewards
                                        if r.get("time_slot") != slot or r.get("type") == "fee_reward"]
                        running_minted = round(running_minted - existing["amount"], 8)
                        del existing_slots[slot]
                    else:
                        continue   # Existing wins

                # Check supply cap using running total (fee rewards bypass the cap)
                if rtype == "fee_reward":
                    self.rewards.append(reward)
                    changed = True
                elif round(running_minted + reward["amount"], 8) <= TOTAL_SUPPLY:
                    self.rewards.append(reward)
                    running_minted = round(running_minted + reward["amount"], 8)
                    if slot is not None:
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
        """Create a checkpoint at checkpoint_slot. Calculates balances,
        hashes pruned data, prunes old rewards and transactions."""
        prune_before = checkpoint_slot - CHECKPOINT_BUFFER
        with self._lock:
            if any(c["slot"] == checkpoint_slot for c in self.checkpoints):
                return False
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

            prev_spent   = list(self.checkpoints[-1].get("spent_tx_ids", [])) if self.checkpoints else []
            new_spent    = [t["tx_id"] for t in txs_to_prune]
            spent_tx_ids = list(set(prev_spent + new_spent))

            rewards_hash = Ledger._compute_hash(
                sorted(rewards_to_prune, key=lambda r: r.get("time_slot", 0))
            )
            txs_hash = Ledger._compute_hash(
                sorted(txs_to_prune, key=lambda t: t.get("timestamp", 0))
            )
            kept_minted = round(
                sum(r["amount"] for r in rewards_to_keep if r.get("type") == "block_reward"),
                8
            )
            checkpoint = {
                "slot":         checkpoint_slot,
                "prune_before": prune_before,
                "balances":     balances,
                "total_minted": self.total_minted,
                "kept_minted":  kept_minted,
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
        Only accepted if newer than our latest. Hashes verified against local data."""
        with self._lock:
            if self.checkpoints:
                if checkpoint.get("slot", 0) <= self.checkpoints[-1]["slot"]:
                    return False
            if checkpoint.get("total_minted", 0) > TOTAL_SUPPLY:
                return False
            prune_before = checkpoint.get("prune_before", 0)

            # Verify checkpoint hashes against local data before accepting
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

            # Hashes verified — prune local data and store checkpoint
            self.rewards      = [r for r in self.rewards
                                 if r.get("time_slot", prune_before) >= prune_before]
            self.transactions = [t for t in self.transactions
                                 if (t.get("slot") or 0) >= prune_before]
            self.checkpoints.append(checkpoint)
            # Trust the checkpoint's total_minted — it is the network-agreed value
            self.total_minted = checkpoint.get("total_minted", 0.0)
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

    @staticmethod
    def _derive_key(password: str, salt: bytes) -> bytes:
        kdf = Scrypt(salt=salt, length=32, n=131072, r=8, p=1, backend=default_backend())
        return kdf.derive(password.encode())

    def save(self, path=WALLET_FILE, password=None):
        """Atomic write — temp file + os.replace prevents corruption on crash."""
        if password:
            salt       = os.urandom(32)
            key        = Wallet._derive_key(password, salt)
            nonce      = os.urandom(12)
            ciphertext = AESGCM(key).encrypt(nonce, self.private_key, None)
            data = {
                "version":         VERSION,
                "device_id":       self.device_id,
                "public_key":      self.public_key.hex(),
                "encrypted":       True,
                "kdf":             "scrypt",
                "scrypt_n":        131072,
                "scrypt_r":        8,
                "scrypt_p":        1,
                "salt":            salt.hex(),
                "nonce":           nonce.hex(),
                "private_key_enc": ciphertext.hex()
            }
        else:
            data = {
                "version":     VERSION,
                "device_id":   self.device_id,
                "public_key":  self.public_key.hex(),
                "private_key": self.private_key.hex(),
                "quantum":     True
            }
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def load(self, path=WALLET_FILE, password=None):
        with open(path, "r") as f:
            data = json.load(f)
        self.public_key = bytes.fromhex(data["public_key"])
        self.device_id  = data["device_id"]
        if data.get("encrypted"):
            if password is None:
                raise ValueError("wallet is encrypted — password required")
            salt       = bytes.fromhex(data["salt"])
            nonce      = bytes.fromhex(data["nonce"])
            ciphertext = bytes.fromhex(data["private_key_enc"])
            key        = Wallet._derive_key(password, salt)
            try:
                self.private_key = AESGCM(key).decrypt(nonce, ciphertext, None)
            except Exception:
                raise ValueError("wrong password")
        else:
            self.private_key = bytes.fromhex(data["private_key"])

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
        self.fee           = fee
        self.slot          = slot
        self.timestamp     = timestamp or time.time()
        self.signature     = None

    def _payload(self) -> bytes:
        return (
            f"{self.tx_id}:{self.sender_id}:{self.recipient_id}:"
            f"{self.amount:.8f}:{self.fee:.8f}:{self.timestamp:.6f}:"
            f"{self.slot if self.slot is not None else 0}"
        ).encode()

    def sign(self, wallet: Wallet):
        self.signature = wallet.sign(self._payload())

    def verify(self) -> bool:
        if not self.signature:
            return False
        try:
            expected_id = hashlib.sha256(bytes.fromhex(self.sender_pubkey)).hexdigest()
            if expected_id != self.sender_id:
                return False
        except Exception:
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
        # Validate amount — must be numeric and positive
        amount = d["amount"]
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            raise ValueError(f"invalid amount: {amount!r}")
        if amount <= 0:
            raise ValueError(f"amount must be positive: {amount}")

        # Validate fee — must be numeric and non-negative
        fee = d.get("fee", 0.0)
        if not isinstance(fee, (int, float)) or isinstance(fee, bool):
            raise ValueError(f"invalid fee: {fee!r}")
        if fee < 0:
            raise ValueError(f"fee must be non-negative: {fee}")

        # Validate sender_id and recipient_id — must be 64-char lowercase hex
        for field in ("sender_id", "recipient_id"):
            val = d.get(field, "")
            if not isinstance(val, str) or len(val) != 64 or not all(c in "0123456789abcdef" for c in val):
                raise ValueError(f"invalid {field}: {val!r}")

        # Validate sender_pubkey — must be non-empty hex string
        pubkey = d.get("sender_pubkey", "")
        if not isinstance(pubkey, str) or not pubkey:
            raise ValueError("invalid sender_pubkey")
        try:
            bytes.fromhex(pubkey)
        except Exception:
            raise ValueError("sender_pubkey is not valid hex")

        tx = cls(
            sender_id     = d["sender_id"],
            recipient_id  = d["recipient_id"],
            sender_pubkey = d["sender_pubkey"],
            amount        = amount,
            fee           = fee,
            slot          = d.get("slot"),
            timestamp     = d["timestamp"],
            tx_id         = d["tx_id"]
        )
        tx.signature = d.get("signature")
        return tx


# ─────────────────────────────────────────────
# BOOTSTRAP HELPERS — Multi-server resilience
# ─────────────────────────────────────────────

def _load_bootstrap_servers() -> list:
    """Load cached bootstrap servers and merge with hardcoded list."""
    servers = list(BOOTSTRAP_SERVERS)
    try:
        if os.path.exists(BOOTSTRAP_CACHE_FILE):
            with open(BOOTSTRAP_CACHE_FILE, "r") as f:
                cached = json.load(f)
            for entry in cached:
                pair = (entry["host"], entry["port"])
                if pair not in servers:
                    servers.append(pair)
    except Exception:
        pass
    return servers

def _fetch_bootstrap_list() -> list:
    """Fetch bootstrap_servers.txt from GitHub.
    Returns empty list silently if GitHub is unreachable."""
    servers = []
    try:
        import urllib.request
        raw = urllib.request.urlopen(BOOTSTRAP_LIST_URL, timeout=5).read().decode("utf-8")
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) == 2:
                host = parts[0].strip()
                try:
                    port = int(parts[1].strip())
                    if host and 1024 <= port <= 65535:
                        servers.append((host, port))
                except ValueError:
                    continue
    except Exception:
        pass
    return servers

def _save_bootstrap_servers(servers: list):
    try:
        data = [{"host": h, "port": p} for h, p in servers]
        with open(BOOTSTRAP_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


# ─────────────────────────────────────────────
# NETWORK — Worldwide peer discovery
# ─────────────────────────────────────────────

class Network:
    def __init__(self, wallet: Wallet, ledger: Ledger,
                 on_transaction, on_reward):
        self.wallet             = wallet
        self.ledger             = ledger
        self.on_transaction     = on_transaction
        self.on_reward          = on_reward
        self.peers              = {}   # device_id -> {ip, port, last_seen}
        self._peers_lock        = threading.Lock()
        self.seen_ids           = set()
        self._seen_tx_order     = []
        self._seen_lock         = threading.Lock()
        self._running           = False
        self._bootstrap_servers = _load_bootstrap_servers()
        self.local_ip           = self._get_local_ip()
        self.port               = find_free_port(7779)
        self._node_ref          = None

    def _get_local_ip(self):
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
            peers_file = os.path.join(os.path.expanduser("~"), ".timpal_peers.json")
            with self._peers_lock:
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
            peers_file = os.path.join(os.path.expanduser("~"), ".timpal_peers.json")
            if os.path.exists(peers_file):
                with open(peers_file, "r") as f:
                    saved = json.load(f)
                with self._peers_lock:
                    for pid, p in saved.items():
                        if pid != self.wallet.device_id:
                            self.peers[pid] = {
                                "ip":        p["ip"],
                                "port":      p["port"],
                                "last_seen": time.time() - 25
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
        threading.Thread(target=self._periodic_sync,     daemon=True).start()
        threading.Thread(target=self._clean_peers,       daemon=True).start()

    def _clean_peers(self):
        """Remove peers not seen for 20 minutes. Runs every 60 seconds."""
        while self._running:
            time.sleep(60)
            cutoff = time.time() - 1200
            with self._peers_lock:
                stale = [pid for pid, p in list(self.peers.items())
                         if p["last_seen"] < cutoff]
                for pid in stale:
                    del self.peers[pid]
            if stale:
                self._save_peers()

    def stop(self):
        self._running = False

    # ── Bootstrap connection ────────────────────

    def _bootstrap_connect(self):
        """Connect to all known bootstrap servers. Re-registers every 2 minutes."""
        time.sleep(2)
        # Fetch GitHub bootstrap list once on startup
        github_servers = _fetch_bootstrap_list()
        for pair in github_servers:
            if pair not in self._bootstrap_servers:
                self._bootstrap_servers.append(pair)
        if github_servers:
            _save_bootstrap_servers(self._bootstrap_servers)

        while self._running:
            new_peers = 0
            for host, port in list(self._bootstrap_servers):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(10.0)
                    sock.connect((host, port))
                    sock.sendall(json.dumps({
                        "type":      "HELLO",
                        "device_id": self.wallet.device_id,
                        "port":      self.port,
                        "version":   VERSION
                    }).encode())
                    sock.shutdown(socket.SHUT_WR)
                    response = b""
                    while True:
                        chunk = sock.recv(65536)
                        if not chunk:
                            break
                        response += chunk
                        if len(response) > 1_000_000:
                            break
                    sock.close()
                    data = json.loads(response.decode())
                    if data.get("type") == "VERSION_REJECTED":
                        print(f"\n  \u2554" + "\u2550" * 50 + "\u2557")
                        print(f"  \u2551  TIMPAL UPDATE REQUIRED                           \u2551")
                        print(f"  \u2560" + "\u2550" * 50 + "\u2563")
                        print(f"  \u2551  Your version is no longer supported.            \u2551")
                        print(f"  \u2551  You must update before joining the network.     \u2551")
                        print(f"  \u2560" + "\u2550" * 50 + "\u2563")
                        print(f"  \u2551  Step 1: Install dependencies:                   \u2551")
                        print(f"  \u2551    pip3 install dilithium-py cryptography        \u2551")
                        print(f"  \u2551  Step 2: Delete your old ledger:                 \u2551")
                        print(f"  \u2551    rm ~/.timpal_ledger.json                       \u2551")
                        print(f"  \u2551  Step 3: Download the new version:               \u2551")
                        print(f"  \u2551    curl -O https://raw.githubusercontent.com/     \u2551")
                        print(f"  \u2551    EvokiTimpal/timpal/main/timpal.py              \u2551")
                        print(f"  \u2551  Step 4: Restart:                                \u2551")
                        print(f"  \u2551    python3 timpal.py                             \u2551")
                        print(f"  \u255a" + "\u2550" * 50 + "\u255d")
                        return
                    if data.get("type") == "PEERS":
                        for peer in data.get("peers", []):
                            pid = peer["device_id"]
                            with self._peers_lock:
                                if pid != self.wallet.device_id and pid not in self.peers:
                                    if len(self.peers) >= MAX_PEERS:
                                        oldest = min(self.peers,
                                                     key=lambda k: self.peers[k]["last_seen"])
                                        del self.peers[oldest]
                                    self.peers[pid] = {
                                        "ip":        peer["ip"],
                                        "port":      peer["port"],
                                        "last_seen": time.time()
                                    }
                                    new_peers += 1
                except Exception:
                    continue

                # Ask each bootstrap server for its known bootstrap servers
                try:
                    sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock2.settimeout(5.0)
                    sock2.connect((host, port))
                    sock2.sendall(json.dumps({"type": "GET_BOOTSTRAP_SERVERS"}).encode())
                    sock2.shutdown(socket.SHUT_WR)
                    resp = b""
                    while True:
                        chunk = sock2.recv(65536)
                        if not chunk:
                            break
                        resp += chunk
                        if len(resp) > 65536:
                            break
                    sock2.close()
                    bs_data = json.loads(resp.decode())
                    if bs_data.get("type") == "BOOTSTRAP_SERVERS_RESPONSE":
                        changed = False
                        for entry in bs_data.get("servers", []):
                            pair = (entry["host"], entry["port"])
                            if pair not in self._bootstrap_servers:
                                self._bootstrap_servers.append(pair)
                                changed = True
                        if changed:
                            _save_bootstrap_servers(self._bootstrap_servers)
                except Exception:
                    pass

            if new_peers > 0:
                self._save_peers()
                print(f"\n  [+] Bootstrap: found {new_peers} peers worldwide")
                print(f"  > ", end="", flush=True)
                threading.Thread(target=self._sync_ledger, daemon=True).start()

            time.sleep(120)   # Re-register every 2 minutes

    def _periodic_sync(self):
        """Delta sync every 2 minutes to keep ledger fully up to date."""
        time.sleep(30)
        while self._running:
            try:
                if self.get_online_peers():
                    self._sync_ledger()
            except Exception:
                pass
            time.sleep(120)

    def _confirm_checkpoint_with_peers(self, checkpoint, exclude_ip=None):
        """Ask other peers if they have the same checkpoint.
        Queries peers IN PARALLEL and returns True when enough confirm."""
        peers    = self.get_online_peers()
        eligible = {pid: p for pid, p in peers.items() if p["ip"] != exclude_ip}
        required = min(3, len(eligible))
        if required == 0:
            return False

        confirmations = [0]
        conf_lock     = threading.Lock()
        confirmed     = threading.Event()

        def _ask(peer):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10.0)
                sock.connect((peer["ip"], peer["port"]))
                sock.sendall(json.dumps({
                    "type":            "SYNC_REQUEST",
                    "known_slots":     [],
                    "known_tx_ids":    [],
                    "checkpoint_slot": 0
                }).encode())
                sock.shutdown(socket.SHUT_WR)
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > 1_000_000:
                        break
                sock.close()
                msg = json.loads(data.decode())
                if msg.get("type") == "SYNC_RESPONSE":
                    peer_cp = msg.get("checkpoint")
                    if (peer_cp and
                            peer_cp.get("slot")         == checkpoint.get("slot") and
                            peer_cp.get("rewards_hash") == checkpoint.get("rewards_hash") and
                            peer_cp.get("txs_hash")     == checkpoint.get("txs_hash") and
                            peer_cp.get("total_minted") == checkpoint.get("total_minted")):
                        with conf_lock:
                            confirmations[0] += 1
                            if confirmations[0] >= required:
                                confirmed.set()
            except Exception:
                pass

        for peer in list(eligible.values()):
            threading.Thread(target=_ask, args=(peer,), daemon=True).start()

        confirmed.wait(timeout=12.0)
        return confirmed.is_set()

    def _sync_ledger(self):
        """Delta sync — only request what we are missing from a peer.
        Never transfers full ledger — scales to millions of nodes."""
        time.sleep(1)
        peers = self.get_online_peers()
        if not peers:
            return
        for peer_id in random.sample(list(peers.keys()), min(3, len(peers))):
            peer = peers[peer_id]
            try:
                with self.ledger._lock:
                    known_slots = [
                        r.get("time_slot") for r in self.ledger.rewards
                        if r.get("time_slot") and r.get("type") == "block_reward"
                    ][-10000:]
                    known_tx_ids = [
                        t.get("tx_id") for t in self.ledger.transactions
                        if t.get("tx_id")
                    ][-10000:]

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
                    if len(data) > 10_000_000:
                        break
                sock.close()
                msg = json.loads(data.decode())
                if msg.get("type") == "SYNC_RESPONSE":
                    if msg.get("checkpoint"):
                        cp           = msg["checkpoint"]
                        prune_before = cp.get("prune_before", 0)
                        with self.ledger._lock:
                            can_verify = bool(
                                [r for r in self.ledger.rewards
                                 if r.get("time_slot", prune_before) < prune_before] or
                                [t for t in self.ledger.transactions
                                 if (t.get("slot") or 0) < prune_before]
                            )
                        if can_verify:
                            self.ledger.apply_checkpoint(cp)
                        elif self._confirm_checkpoint_with_peers(cp, exclude_ip=peer["ip"]):
                            self.ledger.apply_checkpoint(cp)

                    delta = {
                        "rewards":      msg.get("rewards", []),
                        "transactions": msg.get("txs", [])
                    }
                    missing_r = len(delta["rewards"])
                    missing_t = len(delta["transactions"])
                    if missing_r > 0 or missing_t > 0:
                        known_tx_ids_before = set(t.get("tx_id") for t in self.ledger.transactions)
                        merged = self.ledger.merge(delta)
                        if merged:
                            print(f"\n  [+] Synced {missing_r} rewards, {missing_t} txs from network")
                            print(f"  > ", end="", flush=True)
                            node = self._node_ref
                            if node:
                                for tx in delta["transactions"]:
                                    if tx.get("tx_id") in known_tx_ids_before:
                                        continue
                                    if tx.get("recipient_id") == node.wallet.device_id:
                                        balance = self.ledger.get_balance(node.wallet.device_id)
                                        print(f"\n  \u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557")
                                        print(f"  \u2551       TMPL RECEIVED              \u2551")
                                        print(f"  \u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563")
                                        print(f"  \u2551  Amount  : {tx['amount']:.8f} TMPL")
                                        print(f"  \u2551  From    : {tx['sender_id'][:20]}...")
                                        print(f"  \u2551  Balance : {balance:.8f} TMPL")
                                        print(f"  \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d")
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
                        with self._peers_lock:
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
        sock.listen(100)
        sock.settimeout(1.0)
        _conn_sem = threading.Semaphore(200)   # Allow 200 concurrent connections

        def _handle_with_sem(conn, addr):
            try:
                self._handle_incoming(conn, addr)
            finally:
                _conn_sem.release()

        while self._running:
            try:
                conn, addr = sock.accept()
                if not _conn_sem.acquire(blocking=False):
                    conn.close()
                    continue
                threading.Thread(
                    target=_handle_with_sem,
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

            msg      = json.loads(data.decode())
            msg_type = msg.get("type")

            if msg_type == "HELLO":
                peer_id      = msg.get("device_id")
                peer_version = msg.get("version", "0.0")
                if _ver(peer_version) < _ver(MIN_VERSION):
                    conn.sendall(json.dumps({
                        "type":   "VERSION_REJECTED",
                        "reason": (f"Your version ({peer_version}) is below minimum "
                                   f"({MIN_VERSION}). Update from: "
                                   f"https://github.com/EvokiTimpal/timpal")
                    }).encode())
                    return
                if peer_id and peer_id != self.wallet.device_id:
                    with self._peers_lock:
                        if peer_id not in self.peers and len(self.peers) >= MAX_PEERS:
                            oldest = min(self.peers, key=lambda k: self.peers[k]["last_seen"])
                            del self.peers[oldest]
                        self.peers[peer_id] = {
                            "ip":        addr[0],
                            "port":      msg.get("port", 7779),
                            "last_seen": time.time()
                        }
                        peer_list = [
                            {"device_id": pid, "ip": p["ip"], "port": p["port"]}
                            for pid, p in self.peers.items()
                            if pid != peer_id
                        ]
                    conn.sendall(json.dumps({
                        "type":      "HELLO_ACK",
                        "device_id": self.wallet.device_id,
                        "peers":     peer_list
                    }).encode())
                    threading.Thread(target=self._sync_ledger, daemon=True).start()

            elif msg_type == "TRANSACTION":
                tx_gossip_id = msg.get("transaction", {}).get("tx_id", "")
                with self._seen_lock:
                    if not tx_gossip_id or tx_gossip_id in self.seen_ids:
                        tx_gossip_id = None
                if tx_gossip_id:
                    try:
                        tx = Transaction.from_dict(msg["transaction"])
                    except Exception:
                        tx_gossip_id = None
                if tx_gossip_id:
                    with self._seen_lock:
                        self.seen_ids.add(tx_gossip_id)
                        self._seen_tx_order.append(tx_gossip_id)
                    self.on_transaction(tx)
                    threading.Thread(
                        target=self.broadcast,
                        args=(msg, None),
                        daemon=True
                    ).start()

            elif msg_type == "FEE_REWARDS":
                if is_era2():
                    time_slot   = msg.get("time_slot")
                    fee_rewards = msg.get("fee_rewards", [])
                    if fee_rewards and time_slot is not None:
                        fee_rewards = fee_rewards[:1000]
                        fee_rewards = [fr for fr in fee_rewards
                                       if isinstance(fr.get("winner_id"), str)
                                       and len(fr["winner_id"]) == 64
                                       and all(c in "0123456789abcdef"
                                               for c in fr["winner_id"].lower())]
                        if fee_rewards:
                            with self.ledger._lock:
                                actual_fees = sum(
                                    tx.get("fee", 0.0)
                                    for tx in self.ledger.transactions
                                    if tx.get("slot") == time_slot and tx.get("fee", 0.0) > 0
                                )
                            claimed_total = sum(fr.get("amount", 0.0) for fr in fee_rewards)
                            if claimed_total <= actual_fees + 0.000001:
                                amounts  = [fr.get("amount", 0.0) for fr in fee_rewards]
                                expected = round(actual_fees / len(fee_rewards), 8)
                                if all(abs(a - expected) < 0.000001 for a in amounts):
                                    for fr in fee_rewards:
                                        self.ledger.add_fee_reward(
                                            time_slot,
                                            fr["winner_id"],
                                            fr["amount"]
                                        )

            elif msg_type in ("VRF_COMMIT", "VRF_REVEAL", "VRF_TICKET"):
                pass   # Lottery handled via bootstrap — not peer gossip

            elif msg_type == "REWARD":
                reward          = msg.get("reward", {})
                reward_gossip_id = reward.get("reward_id", "") + ":" + reward.get("winner_id", "")
                with self._seen_lock:
                    if reward_gossip_id and reward_gossip_id not in self.seen_ids:
                        self.seen_ids.add(reward_gossip_id)
                    else:
                        reward_gossip_id = None
                if reward_gossip_id:
                    self.on_reward(reward)
                    threading.Thread(
                        target=self.broadcast,
                        args=(msg, None),
                        daemon=True
                    ).start()

            elif msg_type == "SYNC_PUSH":
                delta = {
                    "rewards":      msg.get("rewards", [])[:5000],
                    "transactions": msg.get("txs", [])[:2000]
                }
                if delta["rewards"] or delta["transactions"]:
                    self.ledger.merge(delta)

            elif msg_type == "CHECKPOINT":
                checkpoint = msg.get("checkpoint", {})
                if checkpoint:
                    gossip_id = f"checkpoint:{checkpoint.get('slot', '')}"
                    with self._seen_lock:
                        if gossip_id in self.seen_ids:
                            gossip_id = None
                        else:
                            self.seen_ids.add(gossip_id)
                    if gossip_id:
                        prune_before = checkpoint.get("prune_before", 0)
                        with self.ledger._lock:
                            can_verify = bool(
                                [r for r in self.ledger.rewards
                                 if r.get("time_slot", prune_before) < prune_before] or
                                [t for t in self.ledger.transactions
                                 if (t.get("slot") or 0) < prune_before]
                            )
                        if can_verify:
                            applied = self.ledger.apply_checkpoint(checkpoint)
                        elif self._confirm_checkpoint_with_peers(checkpoint, exclude_ip=addr[0]):
                            applied = self.ledger.apply_checkpoint(checkpoint)
                        else:
                            applied = False
                        if applied:
                            self.broadcast({"type": "CHECKPOINT", "checkpoint": checkpoint})

            elif msg_type == "GET_LEDGER":
                conn.sendall(json.dumps({
                    "type": "ERROR",
                    "msg":  "GET_LEDGER removed — use SYNC_REQUEST"
                }).encode())

            elif msg_type == "SYNC_REQUEST":
                their_slots           = set(msg.get("known_slots", [])[:10000])
                their_tx_ids          = set(msg.get("known_tx_ids", [])[:10000])
                their_checkpoint_slot = msg.get("checkpoint_slot", 0)

                with self.ledger._lock:
                    our_slots = set(
                        r.get("time_slot") for r in self.ledger.rewards
                        if r.get("time_slot") and r.get("type") == "block_reward"
                    )
                    our_tx_ids = set(
                        t.get("tx_id") for t in self.ledger.transactions
                        if t.get("tx_id")
                    )
                    missing_rewards = [
                        r for r in self.ledger.rewards
                        if r.get("type") == "fee_reward" or r.get("time_slot") not in their_slots
                    ][:5000]
                    missing_txs = [
                        t for t in self.ledger.transactions
                        if t.get("tx_id") not in their_tx_ids
                    ][:2000]
                    we_need_slots  = list(their_slots  - our_slots)
                    we_need_tx_ids = list(their_tx_ids - our_tx_ids)
                    our_checkpoint = None
                    if self.ledger.checkpoints:
                        latest = self.ledger.checkpoints[-1]
                        if latest["slot"] > their_checkpoint_slot:
                            our_checkpoint = latest

                conn.sendall(json.dumps({
                    "type":           "SYNC_RESPONSE",
                    "rewards":        missing_rewards,
                    "txs":            missing_txs,
                    "total":          len(self.ledger.rewards),
                    "we_need_slots":  we_need_slots,
                    "we_need_tx_ids": we_need_tx_ids,
                    "checkpoint":     our_checkpoint
                }).encode())

        except Exception:
            pass
        finally:
            conn.close()

    # ── Broadcast to peers ───────────────────────

    def broadcast(self, message: dict, exclude_id: str = None):
        """Gossip a message to recently active peers only.
        Uses get_online_peers() so stale nodes don't consume fanout slots."""
        msg_bytes   = json.dumps(message).encode()
        online      = self.get_online_peers()
        all_peers   = list(online.items())
        random.shuffle(all_peers)
        peers_snapshot = all_peers[:BROADCAST_FANOUT]
        for peer_id, peer in peers_snapshot:
            if peer_id == exclude_id:
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((peer["ip"], peer["port"]))
                sock.sendall(msg_bytes)
                sock.close()
                with self._peers_lock:
                    if peer_id in self.peers:
                        self.peers[peer_id]["last_seen"] = time.time()
            except Exception:
                continue

    def send_to_peer(self, peer_id: str, message: dict) -> bool:
        with self._peers_lock:
            if peer_id not in self.peers:
                return False
            peer = dict(self.peers[peer_id])
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
        """Return peers seen within the last 120 seconds."""
        cutoff = time.time() - 120
        with self._peers_lock:
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
        lock_path = os.path.join(os.path.expanduser("~"), ".timpal.lock")
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
        import getpass
        if os.path.exists(WALLET_FILE):
            with open(WALLET_FILE, "r") as f:
                wallet_data = json.load(f)
            if wallet_data.get("encrypted"):
                while True:
                    try:
                        pw = getpass.getpass("  Wallet password: ")
                        self.wallet.load(password=pw)
                        break
                    except ValueError as e:
                        if "wrong password" in str(e):
                            print("  Wrong password. Try again.")
                        else:
                            raise
            else:
                self.wallet.load()
                print("\n  \u26a0\ufe0f  Your wallet is not encrypted.")
                print("  Anyone with access to this device can steal your TMPL.")
                ans = input("  Encrypt your wallet now? (yes/no): ").strip().lower()
                if ans == "yes":
                    while True:
                        pw  = getpass.getpass("  Set password (min 8 characters): ")
                        pw2 = getpass.getpass("  Confirm password: ")
                        if pw != pw2:
                            print("  Passwords do not match. Try again.")
                            continue
                        if len(pw) < 8:
                            print("  Password must be at least 8 characters.")
                            continue
                        self.wallet.save(password=pw)
                        print("  Wallet encrypted successfully.")
                        break
                else:
                    print("  Wallet left unencrypted.")
            balance = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n  Wallet loaded.")
            print(f"  Device ID : {self.wallet.device_id[:24]}...")
            print(f"  Balance   : {balance:.8f} TMPL")
        else:
            self.wallet.create_new()
            print("\n  Set a password to encrypt your wallet.")
            print("  If you forget it your TMPL is gone forever.")
            while True:
                pw  = getpass.getpass("  Set password (min 8 characters): ")
                pw2 = getpass.getpass("  Confirm password: ")
                if pw != pw2:
                    print("  Passwords do not match. Try again.")
                    continue
                if len(pw) < 8:
                    print("  Password must be at least 8 characters.")
                    continue
                self.wallet.save(password=pw)
                print("  Wallet encrypted and saved.")
                break

    _tx_rate      = {}
    _tx_rate_lock = threading.Lock()

    def _on_transaction_received(self, tx: Transaction):
        if not tx.verify():
            return
        if tx.amount <= 0:
            return
        now    = time.time()
        sender = tx.sender_id
        with Node._tx_rate_lock:
            times = Node._tx_rate.get(sender, [])
            times = [t for t in times if now - t < 60]
            if len(times) >= 60:
                return
            times.append(now)
            Node._tx_rate[sender] = times
        added = self.ledger.add_transaction(tx.to_dict())
        if not added:
            return
        if tx.recipient_id == self.wallet.device_id:
            balance = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n")
            print(f"  \u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557")
            print(f"  \u2551       TMPL RECEIVED              \u2551")
            print(f"  \u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563")
            print(f"  \u2551  Amount  : {tx.amount:.8f} TMPL")
            print(f"  \u2551  From    : {tx.sender_id[:20]}...")
            print(f"  \u2551  Balance : {balance:.8f} TMPL")
            print(f"  \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d")
            print(f"  > ", end="", flush=True)

    def _on_reward_received(self, reward: dict):
        added = self.ledger.add_reward(reward)
        if not added:
            return
        if reward.get("winner_id") == self.wallet.device_id:
            if not self._sending:
                balance = self.ledger.get_balance(self.wallet.device_id)
                print(f"\n")
                print(f"  \u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557")
                print(f"  \u2551       REWARD WON! \u2605              \u2551")
                print(f"  \u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563")
                print(f"  \u2551  Amount  : {reward['amount']:.8f} TMPL")
                print(f"  \u2551  Balance : {balance:.8f} TMPL")
                print(f"  \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d")
                print(f"  > ", end="", flush=True)
        else:
            short = reward.get("winner_id", "")[:20]
            slot  = reward.get("time_slot", "?")
            print(f"\n  [slot {slot}] Winner: {short}... +{reward['amount']} TMPL\n  > ",
                  end="", flush=True)

    def _vrf_ticket(self, time_slot: int) -> tuple:
        """VRF ticket: seed = time_slot, ticket = SHA256(sign(private_key, seed)).
        Unique per node, unpredictable before reveal, verifiable by anyone."""
        seed = str(time_slot)
        msg  = seed.encode()
        sig  = Dilithium3.sign(self.wallet.private_key, msg)
        ticket = hashlib.sha256(sig).hexdigest()
        return ticket, sig.hex(), seed

    @staticmethod
    def _verify_ticket(public_key_hex: str, seed: str, sig_hex: str, ticket: str) -> bool:
        """Verify: signature is valid for the seed under this public key,
        and hashes to the claimed ticket value."""
        try:
            pub = bytes.fromhex(public_key_hex)
            sig = bytes.fromhex(sig_hex)
            msg = seed.encode()
            if not Dilithium3.verify(pub, msg, sig):
                return False
            return hashlib.sha256(sig).hexdigest() == ticket
        except Exception:
            return False

    # ── Commit-reveal lottery ──────────────────────────────────────────────────

    def _make_commit(self, time_slot: int, ticket: str) -> str:
        """Commitment = SHA256(ticket + device_id + slot). Binding to device_id
        prevents grinding attacks."""
        raw = f"{ticket}:{self.wallet.device_id}:{time_slot}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _bootstrap_submit(self, msg: dict):
        """Submit to ALL known bootstrap servers simultaneously (fire and forget)."""
        def _send(host, port):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((host, port))
                sock.sendall(json.dumps(msg).encode())
                sock.shutdown(socket.SHUT_WR)
                sock.close()
            except Exception:
                pass
        for host, port in list(self.network._bootstrap_servers):
            threading.Thread(target=_send, args=(host, port), daemon=True).start()

    def _bootstrap_query(self, msg_type: str, slot: int) -> dict:
        """Query ALL known bootstrap servers simultaneously and merge results."""
        results   = {}
        lock      = threading.Lock()
        done      = threading.Event()
        servers   = list(self.network._bootstrap_servers)
        remaining = [len(servers)]

        def _query(host, port):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((host, port))
                sock.sendall(json.dumps({"type": msg_type, "slot": slot}).encode())
                sock.shutdown(socket.SHUT_WR)
                resp = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    resp += chunk
                    if len(resp) > 1_000_000:
                        break
                sock.close()
                data = json.loads(resp.decode())
                key  = "commits" if msg_type == "GET_COMMITS" else "reveals"
                with lock:
                    for k, v in data.get(key, {}).items():
                        if k not in results:
                            results[k] = v
            except Exception:
                pass
            finally:
                with lock:
                    remaining[0] -= 1
                    if remaining[0] <= 0:
                        done.set()

        if not servers:
            return {}
        for host, port in servers:
            threading.Thread(target=_query, args=(host, port), daemon=True).start()
        done.wait(timeout=4.0)
        return results

    def _pick_winner(self, time_slot, all_reveals):
        """Pick winner from all verified reveals. Lowest hex ticket wins.
        Only accepts reveals that have a matching commit."""
        verified = {}
        with self._lottery_lock:
            known_commits = dict(self._commits.get(time_slot, {}))
        for device_id, r in all_reveals.items():
            commit = known_commits.get(device_id)
            if not commit:
                continue
            expected = hashlib.sha256(
                f"{r['ticket']}:{device_id}:{time_slot}".encode()
            ).hexdigest()
            if expected != commit:
                continue
            if not Node._verify_ticket(r["public_key"], r["seed"], r["sig"], r["ticket"]):
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
        """Era 2 only: split all tx fees for this slot equally among active nodes."""
        if not is_era2():
            return
        if not active_nodes:
            return
        with self.ledger._lock:
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
        """Verify winner cryptographically, add to ledger, gossip to peers."""
        if not Node._verify_ticket(
            winner["public_key"], winner["seed"],
            winner["sig"], winner["ticket"]
        ):
            return
        with self.ledger._lock:
            if any(r.get("time_slot") == time_slot and r.get("type") != "fee_reward"
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
        with self.network._seen_lock:
            self.network.seen_ids.add(gossip_id)
        self.network.broadcast({"type": "REWARD", "reward": reward})
        if winner["winner_id"] == self.wallet.device_id:
            balance = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n  \u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557")
            print(f"  \u2551       REWARD WON! \u2605              \u2551")
            print(f"  \u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563")
            print(f"  \u2551  Amount  : {REWARD_PER_ROUND:.8f} TMPL")
            print(f"  \u2551  Balance : {balance:.8f} TMPL")
            print(f"  \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d")
            print(f"  > ", end="", flush=True)
        else:
            short = winner["winner_id"][:20]
            print(f"\n  [slot {time_slot}] Winner: {short}... +{REWARD_PER_ROUND} TMPL\n  > ",
                  end="", flush=True)
        if active_nodes:
            self._distribute_fees(time_slot, active_nodes)

    def _cleanup_slot(self, time_slot):
        """Remove lottery data older than 10 slots and prune stale seen_ids."""
        with self._lottery_lock:
            for d in (self._commits, self._reveals):
                old = [s for s in d if s < time_slot - 10]
                for s in old:
                    del d[s]
        old = [s for s in self._my_tickets if s < time_slot - 10]
        for s in old:
            del self._my_tickets[s]
        cutoff = time_slot - 100
        with self.network._seen_lock:
            def _is_stale(sid):
                try:
                    return ((sid.startswith("reward:") or sid.startswith("checkpoint:"))
                            and int(sid.split(":")[1]) < cutoff)
                except Exception:
                    return False
            stale = [sid for sid in self.network.seen_ids if _is_stale(sid)]
            for sid in stale:
                self.network.seen_ids.discard(sid)
            if len(self.network._seen_tx_order) > 10000:
                to_remove = self.network._seen_tx_order[:-10000]
                for sid in to_remove:
                    self.network.seen_ids.discard(sid)
                self.network._seen_tx_order = self.network._seen_tx_order[-10000:]

    def _reward_lottery(self):
        """Commit-reveal VRF lottery — decentralized, CGNAT-safe.

        Bootstrap stores commits/reveals — nodes query it regardless of NAT.
        Bootstrap cannot cheat — every entry is cryptographically verified by nodes.

        Every 5-second slot:
          t=0.0  Compute ticket. Submit COMMIT to bootstrap.
          t=2.0  Fetch all commits. Submit REVEAL to bootstrap.
          t=4.0  Fetch all reveals.
          t=4.5  Pick lowest verified ticket — same answer on every node.
                 Add reward to ledger, gossip to peers.
        """
        time.sleep(45)

        while self.network._running:
            # Align to next absolute slot boundary
            now            = time.time()
            elapsed        = now - GENESIS_TIME
            next_slot_time = GENESIS_TIME + (int(elapsed / REWARD_INTERVAL) + 1) * REWARD_INTERVAL
            time.sleep(max(0.05, next_slot_time - time.time()))

            # Era 2: no more block rewards
            if is_era2():
                continue

            time_slot  = get_current_slot()
            slot_start = GENESIS_TIME + time_slot * REWARD_INTERVAL

            # Skip if reward already arrived via gossip
            with self.ledger._lock:
                already_won = any(r.get("time_slot") == time_slot for r in self.ledger.rewards)
            if already_won:
                self._cleanup_slot(time_slot)
                continue

            # ── Phase 1: Submit commit (t=0.0) ─────────────────────────────
            ticket, sig_hex, seed = self._vrf_ticket(time_slot)
            self._my_tickets[time_slot] = (ticket, sig_hex, seed)
            commit = self._make_commit(time_slot, ticket)
            with self._lottery_lock:
                if time_slot not in self._commits:
                    self._commits[time_slot] = {}
                self._commits[time_slot][self.wallet.device_id] = commit
            self._bootstrap_submit({
                "type":      "SUBMIT_COMMIT",
                "device_id": self.wallet.device_id,
                "slot":      time_slot,
                "commit":    commit
            })

            # ── Wait until t=2.0 then fetch all commits ─────────────────────
            wait_commits = slot_start + 2.0
            remaining    = wait_commits - time.time()
            if remaining > 0:
                time.sleep(remaining)
            with self.ledger._lock:
                already_won = any(r.get("time_slot") == time_slot for r in self.ledger.rewards)
            if already_won:
                self._cleanup_slot(time_slot)
                continue
            commits_merged = self._bootstrap_query("GET_COMMITS", time_slot)
            with self._lottery_lock:
                for device_id, c in commits_merged.items():
                    if device_id not in self._commits.get(time_slot, {}):
                        if time_slot not in self._commits:
                            self._commits[time_slot] = {}
                        self._commits[time_slot][device_id] = c

            # ── Phase 2: Submit reveal (t=2.0) ──────────────────────────────
            self._bootstrap_submit({
                "type":       "SUBMIT_REVEAL",
                "device_id":  self.wallet.device_id,
                "slot":       time_slot,
                "ticket":     ticket,
                "sig":        sig_hex,
                "seed":       seed,
                "public_key": self.wallet.public_key.hex()
            })

            # ── Wait until t=4.0 then fetch all reveals ─────────────────────
            wait_reveals = slot_start + 4.0
            remaining    = wait_reveals - time.time()
            if remaining > 0:
                time.sleep(remaining)
            with self.ledger._lock:
                already_won = any(r.get("time_slot") == time_slot for r in self.ledger.rewards)
            if already_won:
                self._cleanup_slot(time_slot)
                continue
            all_reveals = self._bootstrap_query("GET_REVEALS", time_slot)

            # Always include our own reveal
            all_reveals[self.wallet.device_id] = {
                "ticket":     ticket,
                "sig":        sig_hex,
                "seed":       seed,
                "public_key": self.wallet.public_key.hex()
            }

            # ── Wait until t=4.5 before claiming ────────────────────────────
            wait_claim = slot_start + 4.5
            remaining  = wait_claim - time.time()
            if remaining > 0:
                time.sleep(remaining)
            with self.ledger._lock:
                already_won = any(r.get("time_slot") == time_slot for r in self.ledger.rewards)
            if already_won:
                self._cleanup_slot(time_slot)
                continue

            with self._lottery_lock:
                active_nodes = list(self._commits.get(time_slot, {}).keys())

            winner = self._pick_winner(time_slot, all_reveals)
            if winner:
                self._claim_reward(winner, time_slot, active_nodes)

            self._cleanup_slot(time_slot)

    def send(self, peer_id: str, amount: float) -> bool:
        if amount <= 0:
            print("\n  Amount must be greater than zero.")
            return False

        # Normalize and validate recipient address
        peer_id = peer_id.lower().strip()
        if not (len(peer_id) == 64 and all(c in "0123456789abcdef" for c in peer_id)):
            print(f"\n  Invalid address. Must be a 64-character hex string.")
            return False

        if peer_id == self.wallet.device_id:
            print(f"\n  Cannot send to yourself.")
            return False

        my_balance = self.ledger.get_balance(self.wallet.device_id)
        if amount > my_balance:
            print(f"\n  Insufficient balance.")
            print(f"  Your balance : {my_balance:.8f} TMPL")
            print(f"  Requested    : {amount:.8f} TMPL")
            return False

        fee          = get_current_fee()
        total_cost   = round(amount + fee, 8)
        current_slot = get_current_slot()

        if fee > 0:
            print(f"\n  Era 2 fee: {fee:.8f} TMPL (split among active nodes this slot)")

        if total_cost > my_balance:
            print(f"\n  Insufficient balance (amount + fee).")
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

        added = self.ledger.add_transaction(tx.to_dict())
        if not added:
            print(f"\n  Transaction rejected by ledger.")
            return False

        with self.network._seen_lock:
            self.network.seen_ids.add(tx.tx_id)
            self.network._seen_tx_order.append(tx.tx_id)
        self.network.broadcast({
            "type":        "TRANSACTION",
            "transaction": tx.to_dict()
        })

        new_balance = self.ledger.get_balance(self.wallet.device_id)
        print(f"\n  \u2713 Sent {amount:.8f} TMPL")
        if fee > 0:
            print(f"  Fee paid   : {fee:.8f} TMPL")
        print(f"  New balance: {new_balance:.8f} TMPL")
        return True

    def _control_server(self):
        """Local control socket — lets CLI subcommands talk to the running node."""
        token = os.urandom(32).hex()
        token_file = os.path.join(os.path.expanduser("~"), ".timpal_control.token")
        try:
            with open(token_file, "w") as f:
                f.write(token)
            os.chmod(token_file, 0o600)
        except Exception:
            pass
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", 7780))
            srv.listen(5)
            srv.settimeout(1.0)
        except Exception:
            return
        while self.network._running:
            try:
                conn, _ = srv.accept()
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > 65536:
                        break
                    if data.endswith(b"\n"):
                        break
                try:
                    cmd = json.loads(data.decode().strip())
                    if cmd.get("token") != token:
                        conn.sendall((json.dumps({"ok": False, "error": "unauthorized"}) + "\n").encode())
                        conn.close()
                        continue
                    response = self._handle_control(cmd)
                except Exception as e:
                    response = {"ok": False, "error": str(e)}
                conn.sendall((json.dumps(response) + "\n").encode())
                conn.close()
            except Exception:
                continue
        srv.close()

    def _handle_control(self, cmd: dict) -> dict:
        action = cmd.get("action")

        if action == "balance":
            balance = self.ledger.get_balance(self.wallet.device_id)
            return {"ok": True, "balance": balance, "address": self.wallet.device_id}

        elif action == "send":
            peer_id = cmd.get("peer_id")
            amount  = float(cmd.get("amount", 0))
            ok = self.send(peer_id, amount)
            return {"ok": ok}

        elif action == "network":
            summary = self.ledger.get_summary()
            peers   = self.network.get_online_peers()
            return {
                "ok":           True,
                "peers":        len(peers),
                "transactions": summary.get("total_transactions", 0),
                "total_rewards": summary.get("total_rewards", 0),
                "minted":       summary.get("total_minted", 0),
                "remaining":    summary.get("remaining_supply", 0)
            }

        return {"ok": False, "error": "Unknown action"}

    def _push_to_explorer(self):
        """Push ledger updates to the explorer API every 5 seconds."""
        time.sleep(60)
        # Use Python's built-in CA bundle — no certifi dependency needed
        ssl_ctx = ssl.create_default_context()

        while self.network._running:
            try:
                import urllib.request
                with self.ledger._lock:
                    my_rewards     = [r for r in self.ledger.rewards
                                      if r.get("winner_id") == self.wallet.device_id][-200:]
                    recent_rewards = list(self.ledger.rewards[-50:])
                    seen_ids = set()
                    rewards  = []
                    for r in my_rewards + recent_rewards:
                        rid = r.get("reward_id", "")
                        if rid not in seen_ids:
                            seen_ids.add(rid)
                            slim = {k: v for k, v in r.items()
                                    if k not in ("vrf_sig", "vrf_public_key")}
                            rewards.append(slim)
                    txs = list(self.ledger.transactions[-20:])

                payload = json.dumps({
                    "type":         "LEDGER_PUSH",
                    "push_secret":  PUSH_SECRET,
                    "rewards":      rewards,
                    "transactions": txs,
                    "total_minted": self.ledger.total_minted
                }).encode()
                req = urllib.request.Request(
                    "https://timpal.org/api",
                    data    = payload,
                    headers = {"Content-Type": "application/json"},
                    method  = "POST"
                )
                urllib.request.urlopen(req, timeout=5, context=ssl_ctx)
            except Exception:
                pass   # Never crash the node over an explorer push failure
            time.sleep(5)

    def _checkpoint_loop(self):
        """Check every 30 seconds if a checkpoint is due.
        Checkpoint slots are deterministic from GENESIS_TIME — all nodes agree."""
        while self.network._running:
            try:
                current_slot = get_current_slot()
                if self.ledger.checkpoints:
                    last_slot       = self.ledger.checkpoints[-1]["slot"]
                    next_checkpoint = last_slot + CHECKPOINT_INTERVAL
                else:
                    # Align to the checkpoint grid from genesis
                    boundary        = (current_slot // CHECKPOINT_INTERVAL) * CHECKPOINT_INTERVAL
                    next_checkpoint = boundary if boundary > 0 else CHECKPOINT_INTERVAL
                if current_slot >= next_checkpoint + CHECKPOINT_BUFFER:
                    created = self.ledger.create_checkpoint(next_checkpoint)
                    if created:
                        print(f"\n  \u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557")
                        print(f"  \u2551       CHECKPOINT CREATED         \u2551")
                        print(f"  \u2560\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2563")
                        print(f"  \u2551  Slot     : {next_checkpoint}")
                        print(f"  \u2551  Balances : saved")
                        print(f"  \u2551  Old data : pruned")
                        print(f"  \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d")
                        print(f"  > ", end="", flush=True)
                        if self.ledger.checkpoints:
                            latest    = self.ledger.checkpoints[-1]
                            gossip_id = f"checkpoint:{latest['slot']}"
                            with self.network._seen_lock:
                                self.network.seen_ids.add(gossip_id)
                            self.network.broadcast({"type": "CHECKPOINT", "checkpoint": latest})
            except Exception:
                pass
            time.sleep(30)

    def start(self):
        print("\n" + "\u2550" * 52)
        print("  TIMPAL v2.1 \u2014 Quantum-Resistant Money Without Masters")
        print("  Quantum-Resistant | Worldwide | Instant")
        print("\u2550" * 52)
        self.network.start()
        balance = self.ledger.get_balance(self.wallet.device_id)
        summary = self.ledger.get_summary()
        print(f"  Device ID : {self.wallet.device_id[:24]}...")
        print(f"  Balance   : {balance:.8f} TMPL")
        print(f"  Network   : {self.network.local_ip}:{self.network.port}")
        print(f"  Minted    : {summary['total_minted']:.4f} / {TOTAL_SUPPLY:,.0f} TMPL")
        print("\u2550" * 52)
        print("  Connecting to worldwide network...")
        print("  Commands: balance | peers | send | history | network | quit")
        print("\u2550" * 52 + "\n")

        threading.Thread(target=self._reward_lottery, daemon=True).start()
        threading.Thread(target=self._control_server, daemon=True).start()
        threading.Thread(target=self._push_to_explorer, daemon=True).start()
        threading.Thread(target=self._checkpoint_loop, daemon=True).start()
        self._cli()

    def _cli(self):
        import sys
        if not sys.stdin.isatty():
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
                        print(f"  [{i+1}] {pid[:24]}... \u2014 {info['ip']}:{info['port']}")
                    print()

            elif raw == "network":
                summary = self.ledger.get_summary()
                peers   = self.network.get_online_peers()
                print(f"\n  Network Status:")
                print(f"  Online peers      : {len(peers)}")
                print(f"  Total transactions: {summary['total_transactions']}")
                print(f"  Total rewards     : {summary['total_rewards']}")
                print(f"  Total minted      : {summary['total_minted']:.8f} TMPL")
                print(f"  Remaining supply  : {summary['remaining_supply']:.8f} TMPL")
                print(f"  Bootstrap         : {BOOTSTRAP_HOST}:{BOOTSTRAP_PORT}\n")

            elif raw == "send":
                self._sending = True
                try:
                    peers     = self.network.get_online_peers()
                    peer_list = list(peers.items())
                    if peer_list:
                        print(f"\n  Online peers:")
                    for i, (pid, info) in enumerate(peer_list):
                        print(f"  [{i+1}] {pid[:24]}... \u2014 {info['ip']}")
                    if peer_list:
                        print(f"\n  Enter peer number or full address:")
                    else:
                        print(f"\n  No peers online. Enter recipient address:")
                    try:
                        choice = input("  > ").strip()
                        if peer_list and choice.isdigit():
                            idx = int(choice) - 1
                            if idx < 0 or idx >= len(peer_list):
                                print("  Invalid selection.\n")
                                continue
                            peer_id = peer_list[idx][0]
                        else:
                            peer_id = choice.lower().strip()
                            if not (len(peer_id) == 64 and
                                    all(c in "0123456789abcdef" for c in peer_id)):
                                print("  Invalid address. Must be a 64-character hex string.\n")
                                continue
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
                finally:
                    self._sending = False

            elif raw == "history":
                my_id      = self.wallet.device_id
                my_tx      = [tx for tx in self.ledger.transactions
                              if tx["sender_id"] == my_id or tx["recipient_id"] == my_id]
                my_rewards = [r for r in self.ledger.rewards if r["winner_id"] == my_id]
                if not my_tx and not my_rewards:
                    print("\n  No transactions yet.\n")
                else:
                    print(f"\n  Your transaction history:")
                    for r in my_rewards[-5:]:
                        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["timestamp"]))
                        print(f"  \u2605 REWARD   +{r['amount']:.8f} TMPL  [{t}]")
                    for tx in my_tx[-10:]:
                        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tx["timestamp"]))
                        if tx["sender_id"] == my_id:
                            print(f"  \u2191 SENT     {tx['amount']:.8f} TMPL  "
                                  f"to   {tx['recipient_id'][:16]}...  [{t}]")
                        else:
                            print(f"  \u2193 RECEIVED {tx['amount']:.8f} TMPL  "
                                  f"from {tx['sender_id'][:16]}...  [{t}]")
                    print()

            elif raw in ("quit", "exit", "q"):
                print("\n  Shutting down. Goodbye.\n")
                self.network.stop()
                break

            else:
                print(f"\n  Unknown command. "
                      f"Try: balance | peers | send | history | network | quit\n")


if __name__ == "__main__":
    import sys

    _check_genesis_time()   # Refuse to start if GENESIS_TIME is not set

    if len(sys.argv) >= 2 and sys.argv[1] == "send":
        if len(sys.argv) != 4:
            print("Usage: python3 timpal.py send <address> <amount>")
            sys.exit(1)
        recipient_id = sys.argv[2].lower().strip()
        try:
            amount = float(sys.argv[3])
        except ValueError:
            print("Invalid amount.")
            sys.exit(1)
        if not os.path.exists(WALLET_FILE):
            print("No wallet found. Run python3 timpal.py first.")
            sys.exit(1)
        if amount <= 0:
            print("Amount must be greater than zero.")
            sys.exit(1)
        if not (len(recipient_id) == 64 and
                all(c in "0123456789abcdef" for c in recipient_id)):
            print("Invalid address. Must be a 64-character hex string.")
            sys.exit(1)
        # Send via control socket — running node is the single source of truth
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(("127.0.0.1", 7780))
            token = ""
            try:
                token_file = os.path.join(os.path.expanduser("~"), ".timpal_control.token")
                with open(token_file, "r") as tf:
                    token = tf.read().strip()
            except Exception:
                pass
            cmd = json.dumps({
                "action":  "send",
                "peer_id": recipient_id,
                "amount":  amount,
                "token":   token
            }) + "\n"
            sock.sendall(cmd.encode())
            resp = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk
                if resp.endswith(b"\n"):
                    break
            sock.close()
            result = json.loads(resp.decode().strip())
            if result.get("ok"):
                print(f"Sent {amount:.8f} TMPL to {recipient_id[:24]}...")
            else:
                print(f"Transaction failed: {result.get('error', 'unknown error')}")
        except ConnectionRefusedError:
            print("Node is not running. Start your node first with: python3 timpal.py")
        except Exception as e:
            print(f"Error: {e}")
        sys.exit(0)

    elif len(sys.argv) >= 2 and sys.argv[1] == "balance":
        if not os.path.exists(WALLET_FILE):
            print("No wallet found. Run python3 timpal.py first.")
            sys.exit(1)
        # Read balance via control socket if node is running
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(("127.0.0.1", 7780))
            token = ""
            try:
                token_file = os.path.join(os.path.expanduser("~"), ".timpal_control.token")
                with open(token_file, "r") as tf:
                    token = tf.read().strip()
            except Exception:
                pass
            cmd = json.dumps({"action": "balance", "token": token}) + "\n"
            sock.sendall(cmd.encode())
            resp = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk
                if resp.endswith(b"\n"):
                    break
            sock.close()
            result = json.loads(resp.decode().strip())
            if result.get("ok"):
                print(f"Balance : {result['balance']:.8f} TMPL")
                print(f"Address : {result['address']}")
                sys.exit(0)
        except Exception:
            pass
        # Fallback: read directly from ledger file (node not running)
        ledger = Ledger()
        wallet = Wallet()
        with open(WALLET_FILE, "r") as f:
            wallet_data = json.load(f)
        wallet.public_key = bytes.fromhex(wallet_data["public_key"])
        wallet.device_id  = wallet_data["device_id"]
        balance = ledger.get_balance(wallet.device_id)
        print(f"Balance : {balance:.8f} TMPL")
        print(f"Address : {wallet.device_id}")
        sys.exit(0)

    else:
        node = Node()
        node.start()
