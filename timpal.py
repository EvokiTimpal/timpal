#!/usr/bin/env python3
"""
TIMPAL Protocol v2.2 — Quantum-Resistant Money Without Masters
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

try:
    from dilithium_py.dilithium import Dilithium3
except ImportError:
    print("\n  [!] dilithium-py not installed. Run: pip3 install dilithium-py cryptography\n")
    exit(1)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.hazmat.backends import default_backend
except ImportError:
    print("\n  [!] cryptography not installed. Run: pip3 install dilithium-py cryptography\n")
    exit(1)

VERSION             = "2.2"
MIN_VERSION         = "2.2"
GENESIS_TIME        = 0        # ← SET BEFORE LAUNCH — same value in bootstrap.py
ERA2_SLOT           = 236_406_620
TARGET_PARTICIPANTS = 10       # Target eligible nodes per slot

BOOTSTRAP_SERVERS    = [("bootstrap.timpal.org", 7777)]
BOOTSTRAP_HOST       = "bootstrap.timpal.org"
BOOTSTRAP_PORT       = 7777
BOOTSTRAP_LIST_URL   = "https://raw.githubusercontent.com/EvokiTimpal/timpal/main/bootstrap_servers.txt"
BROADCAST_PORT       = 7778
DISCOVERY_INTERVAL   = 5
WALLET_FILE          = os.path.join(os.path.expanduser("~"), ".timpal_wallet.json")
LEDGER_FILE          = os.path.join(os.path.expanduser("~"), ".timpal_ledger.json")
BOOTSTRAP_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".timpal_bootstrap.json")

TOTAL_SUPPLY        = 250_000_000.0
REWARD_PER_ROUND    = 1.0575
REWARD_INTERVAL     = 5.0
TX_FEE              = 0.0
TX_FEE_ERA2         = 0.0005
CHECKPOINT_INTERVAL = 241_920
CHECKPOINT_BUFFER   = 120
MAX_PEERS           = 125
BROADCAST_FANOUT    = 8
REWARD_RATE_LIMIT   = 12   # max REWARD msgs per peer IP per slot
SYNC_RATE_WINDOW    = 30   # seconds between SYNC_REQUESTs per IP


def _check_genesis_time():
    if GENESIS_TIME == 0:
        print("\n  " + "═"*52)
        print("  ERROR: GENESIS_TIME is not set.")
        print("  Run: python3 -c \"import time; print(int(time.time()))\"")
        print("  Set the result in both timpal.py and bootstrap.py")
        print("  " + "═"*52 + "\n")
        exit(1)


def get_current_slot() -> int:
    return int((time.time() - GENESIS_TIME) / REWARD_INTERVAL)


def is_era2() -> bool:
    return get_current_slot() >= ERA2_SLOT


def get_current_fee() -> float:
    return TX_FEE_ERA2 if is_era2() else TX_FEE


def _ver(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0, 0)


def _is_valid_epoch_slot(slot) -> bool:
    if slot is None or not isinstance(slot, int):
        return False
    if slot < 0:
        return False
    if slot > get_current_slot() + 10:
        return False
    return True


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


class Ledger:
    def __init__(self):
        self.transactions     = []
        self.rewards          = []
        self.total_minted     = 0.0
        self.checkpoints      = []
        self._lock            = threading.RLock()
        # O(1) duplicate detection for pruned tx_ids.
        # Maintained in sync with checkpoints[-1]["spent_tx_ids"].
        self._spent_tx_ids_set: set = set()
        self._load()

    def _load(self):
        if os.path.exists(LEDGER_FILE):
            try:
                with open(LEDGER_FILE, "r") as f:
                    data = json.load(f)
                self.transactions = data.get("transactions", [])
                self.checkpoints  = data.get("checkpoints", [])
                raw = data.get("rewards", [])
                clean = []
                for r in raw:
                    if r.get("type", "block_reward") == "block_reward":
                        if not _is_valid_epoch_slot(r.get("time_slot")):
                            continue
                    clean.append(r)
                self.rewards = clean
                self.recalculate_totals()
                if self.checkpoints:
                    self._spent_tx_ids_set = set(self.checkpoints[-1].get("spent_tx_ids", []))
            except Exception:
                pass

    def save(self):
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
            for r in self.rewards:
                if r["winner_id"] == device_id:
                    balance += r["amount"]
            return round(balance, 8)

    def has_transaction(self, tx_id: str) -> bool:
        # O(1) check against the set of all pruned tx_ids across checkpoints
        if tx_id in self._spent_tx_ids_set:
            return True
        return any(tx["tx_id"] == tx_id for tx in self.transactions)

    def can_spend(self, device_id: str, amount: float) -> bool:
        return self.get_balance(device_id) >= amount

    def add_transaction(self, tx_dict: dict) -> bool:
        if tx_dict.get("amount", 0) <= 0:
            return False
        try:
            t = Transaction.from_dict(tx_dict)
            if not t.verify():
                return False
        except Exception:
            return False
        with self._lock:
            if self.has_transaction(tx_dict["tx_id"]):
                return False
            total = tx_dict["amount"] + tx_dict.get("fee", 0.0)
            if not self.can_spend(tx_dict["sender_id"], total):
                return False
            self.transactions.append(tx_dict)
            self.save()
            return True

    def add_fee_reward(self, slot: int, node_id: str, amount: float) -> bool:
        with self._lock:
            reward_id = f"fee:{slot}:{node_id}"
            if any(r.get("reward_id") == reward_id for r in self.rewards):
                return False
            if amount <= 0:
                return False
            self.rewards.append({
                "reward_id": reward_id,
                "winner_id": node_id,
                "amount":    round(amount, 8),
                "timestamp": time.time(),
                "time_slot": slot,
                "type":      "fee_reward"
            })
            self.save()
            return True

    def add_reward(self, reward_dict: dict) -> bool:
        with self._lock:
            reward_id = reward_dict.get("reward_id", "")
            if not reward_id:
                return False
            rtype = reward_dict.get("type", "block_reward")
            # block_reward MUST carry all four VRF fields.
            # Accepting a block_reward without them would let any peer mint TMPL
            # to any address without a valid proof — a full inflation attack.
            if rtype == "block_reward":
                pub  = reward_dict.get("vrf_public_key", "")
                seed = reward_dict.get("vrf_seed", "")
                sig  = reward_dict.get("vrf_sig", "")
                tick = reward_dict.get("vrf_ticket", "")
                if not (pub and seed and sig and tick):
                    return False
                if not Node._verify_ticket(pub, seed, sig, tick):
                    return False
            slot = reward_dict.get("time_slot")
            if reward_dict.get("type", "block_reward") == "block_reward":
                if not _is_valid_epoch_slot(slot):
                    return False
            if slot is not None:
                # First writer wins — once a slot has a winner it is final.
                # All honest nodes independently compute the same winner via the
                # collective target, so the first valid REWARD for any slot is
                # authoritative. Replacing with a "lower ticket" is meaningless
                # under the new lottery and would allow a malicious node to
                # displace the legitimate winner by ticket manipulation.
                if any(r.get("time_slot") == slot and r.get("type") != "fee_reward"
                       for r in self.rewards):
                    return False
            else:
                if any(r.get("reward_id") == reward_id for r in self.rewards):
                    return False
            if round(self.total_minted + reward_dict["amount"], 8) > TOTAL_SUPPLY:
                return False
            self.rewards.append(reward_dict)
            self.total_minted = round(self.total_minted + reward_dict["amount"], 8)
            self.save()
            return True

    def recalculate_totals(self):
        if self.checkpoints:
            cp          = self.checkpoints[-1]
            pruned_base = cp["total_minted"] - cp.get("kept_minted", 0)
            self.total_minted = round(
                pruned_base + sum(
                    r["amount"] for r in self.rewards if r.get("type") == "block_reward"
                ), 8)
        else:
            self.total_minted = round(
                sum(r["amount"] for r in self.rewards if r.get("type") == "block_reward"), 8)

    def get_summary(self):
        return {
            "total_transactions": len(self.transactions),
            "total_rewards":      len([r for r in self.rewards if r.get("type") == "block_reward"]),
            "total_minted":       self.total_minted,
            "remaining_supply":   round(TOTAL_SUPPLY - self.total_minted, 8)
        }

    def to_dict(self):
        return {"transactions": self.transactions, "rewards": self.rewards,
                "total_minted": self.total_minted}

    def merge(self, other: dict):
        verified_txs = []
        for tx in other.get("transactions", []):
            try:
                t = Transaction.from_dict(tx)
                if t.verify():
                    verified_txs.append(tx)
            except Exception:
                continue
        verified_rewards = []
        for r in other.get("rewards", []):
            if not r.get("reward_id", ""):
                continue
            rtype = r.get("type", "block_reward")
            # block_reward MUST carry all four VRF fields.
            # If any are absent, a peer can mint TMPL to any address without proof.
            if rtype == "block_reward":
                pub  = r.get("vrf_public_key", "")
                seed = r.get("vrf_seed", "")
                sig  = r.get("vrf_sig", "")
                tick = r.get("vrf_ticket", "")
                if not (pub and seed and sig and tick):
                    continue
                if not Node._verify_ticket(pub, seed, sig, tick):
                    continue
            # fee_rewards only exist in Era 2.  Accepting them in Era 1 lets a
            # malicious peer inflate any balance by sending fake fee_reward dicts
            # (they bypass the supply cap and carry no VRF proof).
            elif rtype == "fee_reward":
                if not is_era2():
                    continue
            if r.get("amount", 0) <= 0:
                continue
            verified_rewards.append(r)

        with self._lock:
            changed = False
            for tx in verified_txs:
                if not self.has_transaction(tx["tx_id"]):
                    total = tx["amount"] + tx.get("fee", 0.0)
                    if self.can_spend(tx["sender_id"], total):
                        self.transactions.append(tx)
                        changed = True
            existing_slots = {
                r.get("time_slot"): r
                for r in self.rewards if r.get("type") == "block_reward"
            }
            running = self.total_minted
            for r in verified_rewards:
                rid   = r.get("reward_id", "")
                slot  = r.get("time_slot")
                rtype = r.get("type", "block_reward")
                # Whitelist known types — reject any unknown reward type
                if rtype not in ("block_reward", "fee_reward"):
                    continue
                if rtype == "block_reward" and not _is_valid_epoch_slot(slot):
                    continue
                if any(x.get("reward_id") == rid and x.get("winner_id") == r.get("winner_id")
                       for x in self.rewards):
                    continue
                if slot is not None and rtype != "fee_reward" and slot in existing_slots:
                    # First writer wins — slot already has a winner, reject all replacements.
                    # Ticket comparison is meaningless under the collective-target lottery.
                    continue
                if rtype == "fee_reward":
                    self.rewards.append(r)
                    changed = True
                elif round(running + r["amount"], 8) <= TOTAL_SUPPLY:
                    self.rewards.append(r)
                    running = round(running + r["amount"], 8)
                    if slot is not None:
                        existing_slots[slot] = r
                    changed = True
            if changed:
                self.recalculate_totals()
                self.save()
            return changed

    @staticmethod
    def _compute_hash(entries: list) -> str:
        return hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()

    def create_checkpoint(self, checkpoint_slot: int) -> bool:
        prune_before = checkpoint_slot - CHECKPOINT_BUFFER
        with self._lock:
            if any(c["slot"] == checkpoint_slot for c in self.checkpoints):
                return False
            r_prune = [r for r in self.rewards if r.get("time_slot", prune_before) < prune_before]
            r_keep  = [r for r in self.rewards if r.get("time_slot", prune_before) >= prune_before]
            t_prune = [t for t in self.transactions if (t.get("slot") or 0) < prune_before]
            t_keep  = [t for t in self.transactions if (t.get("slot") or 0) >= prune_before]
            prev_bal = dict(self.checkpoints[-1]["balances"]) if self.checkpoints else {}
            addrs = set(prev_bal.keys())
            for r in r_prune:
                wid = r.get("winner_id", "")
                if wid: addrs.add(wid)
            for t in t_prune:
                sid = t.get("sender_id", "")
                rid = t.get("recipient_id", "")
                if sid: addrs.add(sid)
                if rid: addrs.add(rid)
            balances = {}
            for addr in addrs:
                bal = prev_bal.get(addr, 0.0)
                for t in t_prune:
                    if t.get("recipient_id") == addr: bal += t.get("amount", 0.0)
                    if t.get("sender_id")    == addr: bal -= t.get("amount", 0.0) + t.get("fee", 0.0)
                for r in r_prune:
                    if r.get("winner_id") == addr: bal += r.get("amount", 0.0)
                balances[addr] = round(bal, 8)
            prev_spent   = list(self.checkpoints[-1].get("spent_tx_ids", [])) if self.checkpoints else []
            spent_tx_ids = list(set(prev_spent + [t["tx_id"] for t in t_prune]))
            kept_minted  = round(sum(r["amount"] for r in r_keep if r.get("type") == "block_reward"), 8)
            cp = {
                "slot":         checkpoint_slot,
                "prune_before": prune_before,
                "balances":     balances,
                "total_minted": self.total_minted,
                "kept_minted":  kept_minted,
                "rewards_hash": Ledger._compute_hash(sorted(r_prune, key=lambda r: r.get("time_slot", 0))),
                "txs_hash":     Ledger._compute_hash(sorted(t_prune, key=lambda t: t.get("timestamp", 0))),
                "spent_tx_ids": spent_tx_ids,
                "timestamp":    time.time()
            }
            self.rewards      = r_keep
            self.transactions = t_keep
            self.checkpoints.append(cp)
            self._spent_tx_ids_set = set(spent_tx_ids)
            self.save()
            return True

    def apply_checkpoint(self, checkpoint: dict) -> bool:
        with self._lock:
            if self.checkpoints:
                if checkpoint.get("slot", 0) <= self.checkpoints[-1]["slot"]:
                    return False
            if checkpoint.get("total_minted", 0) > TOTAL_SUPPLY:
                return False
            prune_before = checkpoint.get("prune_before", 0)
            r_verify = [r for r in self.rewards if r.get("time_slot", prune_before) < prune_before]
            if r_verify:
                if Ledger._compute_hash(sorted(r_verify, key=lambda r: r.get("time_slot", 0))) != checkpoint.get("rewards_hash", ""):
                    return False
            t_verify = [t for t in self.transactions if (t.get("slot") or 0) < prune_before]
            if t_verify:
                if Ledger._compute_hash(sorted(t_verify, key=lambda t: t.get("timestamp", 0))) != checkpoint.get("txs_hash", ""):
                    return False
            self.rewards      = [r for r in self.rewards if r.get("time_slot", prune_before) >= prune_before]
            self.transactions = [t for t in self.transactions if (t.get("slot") or 0) >= prune_before]
            self.checkpoints.append(checkpoint)
            self._spent_tx_ids_set = set(checkpoint.get("spent_tx_ids", []))
            self.total_minted = checkpoint.get("total_minted", 0.0)
            self.save()
            return True


class Wallet:
    def __init__(self):
        self.public_key  = None
        self.private_key = None
        self.device_id   = None

    def create_new(self):
        self.public_key, self.private_key = Dilithium3.keygen()
        self.device_id = hashlib.sha256(self.public_key).hexdigest()
        print(f"\n  New quantum-resistant wallet created.")
        print(f"  Device ID: {self.device_id[:24]}...")
        print(f"\n  WARNING — BACK UP YOUR WALLET FILE: {WALLET_FILE}")
        print(f"  If you delete it your TMPL is gone forever.")

    @staticmethod
    def _derive_key(password: str, salt: bytes) -> bytes:
        return Scrypt(salt=salt, length=32, n=131072, r=8, p=1,
                      backend=default_backend()).derive(password.encode())

    def save(self, path=WALLET_FILE, password=None):
        if password:
            salt  = os.urandom(32)
            key   = Wallet._derive_key(password, salt)
            nonce = os.urandom(12)
            ct    = AESGCM(key).encrypt(nonce, self.private_key, None)
            data  = {
                "version": VERSION, "device_id": self.device_id,
                "public_key": self.public_key.hex(), "encrypted": True,
                "kdf": "scrypt", "scrypt_n": 131072, "scrypt_r": 8, "scrypt_p": 1,
                "salt": salt.hex(), "nonce": nonce.hex(), "private_key_enc": ct.hex()
            }
        else:
            data = {
                "version": VERSION, "device_id": self.device_id,
                "public_key": self.public_key.hex(),
                "private_key": self.private_key.hex(), "quantum": True
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
            salt = bytes.fromhex(data["salt"])
            nonce= bytes.fromhex(data["nonce"])
            ct   = bytes.fromhex(data["private_key_enc"])
            key  = Wallet._derive_key(password, salt)
            try:
                self.private_key = AESGCM(key).decrypt(nonce, ct, None)
            except Exception:
                raise ValueError("wrong password")
        else:
            self.private_key = bytes.fromhex(data["private_key"])

    def get_public_key_hex(self):
        return self.public_key.hex()

    def sign(self, message: bytes) -> str:
        return Dilithium3.sign(self.private_key, message).hex()

    @staticmethod
    def verify_signature(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
        try:
            return Dilithium3.verify(bytes.fromhex(public_key_hex), message, bytes.fromhex(signature_hex))
        except Exception:
            return False


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
        return (f"{self.tx_id}:{self.sender_id}:{self.recipient_id}:"
                f"{self.amount:.8f}:{self.fee:.8f}:{self.timestamp:.6f}:"
                f"{self.slot if self.slot is not None else 0}").encode()

    def sign(self, wallet: Wallet):
        self.signature = wallet.sign(self._payload())

    def verify(self) -> bool:
        if not self.signature:
            return False
        try:
            if hashlib.sha256(bytes.fromhex(self.sender_pubkey)).hexdigest() != self.sender_id:
                return False
        except Exception:
            return False
        return Wallet.verify_signature(self.sender_pubkey, self._payload(), self.signature)

    def to_dict(self) -> dict:
        return {
            "tx_id": self.tx_id, "sender_id": self.sender_id,
            "recipient_id": self.recipient_id, "sender_pubkey": self.sender_pubkey,
            "amount": self.amount, "fee": self.fee, "slot": self.slot,
            "timestamp": self.timestamp, "signature": self.signature
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        amount = d["amount"]
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
            raise ValueError(f"invalid amount: {amount!r}")
        fee = d.get("fee", 0.0)
        if not isinstance(fee, (int, float)) or isinstance(fee, bool) or fee < 0:
            raise ValueError(f"invalid fee: {fee!r}")
        for field in ("sender_id", "recipient_id"):
            val = d.get(field, "")
            if not isinstance(val, str) or len(val) != 64 or not all(c in "0123456789abcdef" for c in val):
                raise ValueError(f"invalid {field}")
        pubkey = d.get("sender_pubkey", "")
        if not isinstance(pubkey, str) or not pubkey:
            raise ValueError("invalid sender_pubkey")
        bytes.fromhex(pubkey)
        tx = cls(sender_id=d["sender_id"], recipient_id=d["recipient_id"],
                 sender_pubkey=d["sender_pubkey"], amount=amount, fee=fee,
                 slot=d.get("slot"), timestamp=d["timestamp"], tx_id=d["tx_id"])
        tx.signature = d.get("signature")
        return tx


def _load_bootstrap_servers() -> list:
    servers = list(BOOTSTRAP_SERVERS)
    try:
        if os.path.exists(BOOTSTRAP_CACHE_FILE):
            with open(BOOTSTRAP_CACHE_FILE, "r") as f:
                for e in json.load(f):
                    pair = (e["host"], e["port"])
                    if pair not in servers:
                        servers.append(pair)
    except Exception:
        pass
    return servers


def _fetch_bootstrap_list() -> list:
    servers = []
    try:
        import urllib.request
        raw = urllib.request.urlopen(BOOTSTRAP_LIST_URL, timeout=5).read().decode()
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) == 2:
                try:
                    port = int(parts[1].strip())
                    if parts[0].strip() and 1024 <= port <= 65535:
                        servers.append((parts[0].strip(), port))
                except ValueError:
                    continue
    except Exception:
        pass
    return servers


def _save_bootstrap_servers(servers: list):
    try:
        with open(BOOTSTRAP_CACHE_FILE, "w") as f:
            json.dump([{"host": h, "port": p} for h, p in servers], f)
    except Exception:
        pass


class Network:
    def __init__(self, wallet, ledger, on_transaction, on_reward):
        self.wallet            = wallet
        self.ledger            = ledger
        self.on_transaction    = on_transaction
        self.on_reward         = on_reward
        self.peers             = {}
        self._peers_lock       = threading.Lock()
        self.seen_ids          = set()
        self._seen_tx_order    = []
        self._seen_lock        = threading.Lock()
        self._running          = False
        self._bootstrap_servers= _load_bootstrap_servers()
        self.local_ip          = self._get_local_ip()
        self.port              = find_free_port(7779)
        self._node_ref         = None
        self._udp_rate         = {}
        self._udp_rate_lock    = threading.Lock()
        self._network_size     = 1
        self._sync_rate        = {}
        self._sync_rate_lock   = threading.Lock()
        self._reward_rate      = {}
        self._reward_rate_lock = threading.Lock()

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
            pf = os.path.join(os.path.expanduser("~"), ".timpal_peers.json")
            with self._peers_lock:
                saveable = {pid: {"ip": p["ip"], "port": p["port"]}
                            for pid, p in self.peers.items()
                            if pid != self.wallet.device_id}
            with open(pf, "w") as f:
                json.dump(saveable, f)
        except Exception:
            pass

    def _load_peers(self):
        try:
            pf = os.path.join(os.path.expanduser("~"), ".timpal_peers.json")
            if os.path.exists(pf):
                with open(pf, "r") as f:
                    saved = json.load(f)
                with self._peers_lock:
                    for pid, p in saved.items():
                        if pid != self.wallet.device_id:
                            self.peers[pid] = {"ip": p["ip"], "port": p["port"],
                                               "last_seen": time.time() - 25}
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

    def stop(self):
        self._running = False

    def _clean_peers(self):
        while self._running:
            time.sleep(60)
            cutoff = time.time() - 1200
            with self._peers_lock:
                stale = [pid for pid, p in list(self.peers.items()) if p["last_seen"] < cutoff]
                for pid in stale:
                    del self.peers[pid]
            if stale:
                self._save_peers()
            sync_cutoff = time.time() - SYNC_RATE_WINDOW * 2
            with self._sync_rate_lock:
                for ip in [ip for ip, t in self._sync_rate.items() if t < sync_cutoff]:
                    del self._sync_rate[ip]
            current_slot = get_current_slot()
            with self._reward_rate_lock:
                for ip in [ip for ip, (s, _) in self._reward_rate.items() if s < current_slot - 5]:
                    del self._reward_rate[ip]

    def _bootstrap_connect(self):
        time.sleep(2)
        for pair in _fetch_bootstrap_list():
            if pair not in self._bootstrap_servers:
                self._bootstrap_servers.append(pair)
        _save_bootstrap_servers(self._bootstrap_servers)

        while self._running:
            new_peers = 0
            for host, port in list(self._bootstrap_servers):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(10.0)
                    sock.connect((host, port))
                    sock.sendall(json.dumps({"type": "HELLO", "device_id": self.wallet.device_id,
                                             "port": self.port, "version": VERSION}).encode())
                    sock.shutdown(socket.SHUT_WR)
                    resp = b""
                    while True:
                        chunk = sock.recv(65536)
                        if not chunk: break
                        resp += chunk
                        if len(resp) > 1_000_000: break
                    sock.close()
                    data = json.loads(resp.decode())
                    if data.get("type") == "VERSION_REJECTED":
                        print(f"\n  ╔══════════════════════════════════════════════════════╗")
                        print(f"  ║  TIMPAL UPDATE REQUIRED                              ║")
                        print(f"  ║  Your version is no longer supported.                ║")
                        print(f"  ║  Delete ~/.timpal_wallet.json + ~/.timpal_ledger.json ║")
                        print(f"  ║  Then: curl -O https://raw.githubusercontent.com/    ║")
                        print(f"  ║  EvokiTimpal/timpal/main/timpal.py                   ║")
                        print(f"  ╚══════════════════════════════════════════════════════╝\n")
                        os._exit(1)
                    if data.get("type") == "PEERS":
                        ns = data.get("network_size", 0)
                        if ns > 0:
                            self._network_size = ns
                        for peer in data.get("peers", []):
                            pid = peer["device_id"]
                            with self._peers_lock:
                                if pid != self.wallet.device_id and pid not in self.peers:
                                    if len(self.peers) >= MAX_PEERS:
                                        oldest = min(self.peers, key=lambda k: self.peers[k]["last_seen"])
                                        del self.peers[oldest]
                                    self.peers[pid] = {"ip": peer["ip"], "port": peer["port"],
                                                       "last_seen": time.time()}
                                    new_peers += 1
                except Exception:
                    continue
                try:
                    sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock2.settimeout(5.0)
                    sock2.connect((host, port))
                    sock2.sendall(json.dumps({"type": "GET_BOOTSTRAP_SERVERS"}).encode())
                    sock2.shutdown(socket.SHUT_WR)
                    resp = b""
                    while True:
                        chunk = sock2.recv(65536)
                        if not chunk: break
                        resp += chunk
                        if len(resp) > 65536: break
                    sock2.close()
                    bs = json.loads(resp.decode())
                    if bs.get("type") == "BOOTSTRAP_SERVERS_RESPONSE":
                        changed = False
                        for e in bs.get("servers", []):
                            pair = (e["host"], e["port"])
                            if pair not in self._bootstrap_servers:
                                self._bootstrap_servers.append(pair)
                                changed = True
                        if changed:
                            _save_bootstrap_servers(self._bootstrap_servers)
                except Exception:
                    pass
            if new_peers > 0:
                self._save_peers()
                print(f"\n  [+] Bootstrap: found {new_peers} peers worldwide\n  > ", end="", flush=True)
                threading.Thread(target=self._sync_ledger, daemon=True).start()
            time.sleep(120)

    def _periodic_sync(self):
        time.sleep(30)
        while self._running:
            if self.get_online_peers():
                self._sync_ledger()
            time.sleep(120)

    def _confirm_checkpoint_with_peers(self, checkpoint, exclude_ip=None):
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
                sock.sendall(json.dumps({"type": "SYNC_REQUEST", "known_slots": [],
                                         "known_tx_ids": [], "checkpoint_slot": 0}).encode())
                sock.shutdown(socket.SHUT_WR)
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk: break
                    data += chunk
                    if len(data) > 1_000_000: break
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
        time.sleep(1)
        peers = self.get_online_peers()
        if not peers:
            return
        for peer_id in random.sample(list(peers.keys()), min(3, len(peers))):
            peer = peers[peer_id]
            try:
                with self.ledger._lock:
                    known_slots  = [r.get("time_slot") for r in self.ledger.rewards
                                    if r.get("time_slot") is not None and r.get("type") == "block_reward"][-10000:]
                    known_tx_ids = [t.get("tx_id") for t in self.ledger.transactions if t.get("tx_id")][-10000:]
                    checkpoint_slot = self.ledger.checkpoints[-1]["slot"] if self.ledger.checkpoints else 0
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(30.0)
                sock.connect((peer["ip"], peer["port"]))
                sock.sendall(json.dumps({
                    "type": "SYNC_REQUEST", "known_slots": known_slots,
                    "known_tx_ids": known_tx_ids,
                    "checkpoint_slot": checkpoint_slot
                }).encode())
                sock.shutdown(socket.SHUT_WR)
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk: break
                    data += chunk
                    if len(data) > 10_000_000: break
                sock.close()
                msg = json.loads(data.decode())
                if msg.get("type") == "SYNC_RESPONSE":
                    if msg.get("checkpoint"):
                        cp = msg["checkpoint"]
                        prune_before = cp.get("prune_before", 0)
                        with self.ledger._lock:
                            can_verify = bool(
                                [r for r in self.ledger.rewards if r.get("time_slot", prune_before) < prune_before] or
                                [t for t in self.ledger.transactions if (t.get("slot") or 0) < prune_before])
                        if can_verify:
                            self.ledger.apply_checkpoint(cp)
                        elif self._confirm_checkpoint_with_peers(cp, exclude_ip=peer["ip"]):
                            self.ledger.apply_checkpoint(cp)
                    delta = {"rewards": msg.get("rewards", []), "transactions": msg.get("txs", [])}
                    if delta["rewards"] or delta["transactions"]:
                        known_before = set(t.get("tx_id") for t in self.ledger.transactions)
                        merged = self.ledger.merge(delta)
                        if merged:
                            print(f"\n  [+] Synced {len(delta['rewards'])} rewards, "
                                  f"{len(delta['transactions'])} txs from network\n  > ", end="", flush=True)
                            node = self._node_ref
                            if node:
                                for tx in delta["transactions"]:
                                    if tx.get("tx_id") in known_before:
                                        continue
                                    if tx.get("recipient_id") == node.wallet.device_id:
                                        bal = self.ledger.get_balance(node.wallet.device_id)
                                        print(f"\n  ╔══════════════════════════════════════╗")
                                        print(f"  ║       TMPL RECEIVED                  ║")
                                        print(f"  ╠══════════════════════════════════════╣")
                                        print(f"  ║  Amount  : {tx['amount']:.8f} TMPL")
                                        print(f"  ║  From    : {tx['sender_id'][:20]}...")
                                        print(f"  ║  Balance : {bal:.8f} TMPL")
                                        print(f"  ╚══════════════════════════════════════╝\n  > ", end="", flush=True)
                    we_need_slots  = set(msg.get("we_need_slots", []))
                    we_need_tx_ids = set(msg.get("we_need_tx_ids", []))
                    if we_need_slots or we_need_tx_ids:
                        with self.ledger._lock:
                            push_r = [r for r in self.ledger.rewards if r.get("time_slot") in we_need_slots]
                            push_t = [t for t in self.ledger.transactions if t.get("tx_id") in we_need_tx_ids]
                        if push_r or push_t:
                            try:
                                s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                s2.settimeout(30.0)
                                s2.connect((peer["ip"], peer["port"]))
                                s2.sendall(json.dumps({"type": "SYNC_PUSH", "rewards": push_r, "txs": push_t}).encode())
                                s2.shutdown(socket.SHUT_WR)
                                s2.close()
                            except Exception:
                                pass
                return
            except Exception:
                continue

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while self._running:
            try:
                sock.sendto(json.dumps({"type": "HELLO", "device_id": self.wallet.device_id,
                                        "ip": self.local_ip, "port": self.port,
                                        "version": VERSION}).encode(), ("<broadcast>", BROADCAST_PORT))
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
                ip  = addr[0]
                now = time.time()
                with self._udp_rate_lock:
                    if now - self._udp_rate.get(ip, 0) < 5.0:
                        continue
                    self._udp_rate[ip] = now
                msg = json.loads(data.decode())
                if msg.get("type") == "HELLO":
                    pid = msg["device_id"]
                    if _ver(msg.get("version", "0.0")) < _ver(MIN_VERSION):
                        continue
                    if pid != self.wallet.device_id:
                        with self._peers_lock:
                            is_new = pid not in self.peers
                            self.peers[pid] = {"ip": msg["ip"], "port": msg.get("port", 7779),
                                               "last_seen": time.time()}
                        if is_new:
                            print(f"\n  [+] Local peer: {pid[:20]}... at {msg['ip']}\n  > ", end="", flush=True)
            except socket.timeout:
                continue
            except Exception:
                continue

    def _listen_tcp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        sock.listen(100)
        sock.settimeout(1.0)
        sem = threading.Semaphore(200)
        def _handle(conn, addr):
            try: self._handle_incoming(conn, addr)
            finally: sem.release()
        while self._running:
            try:
                conn, addr = sock.accept()
                if not sem.acquire(blocking=False):
                    conn.close()
                    continue
                threading.Thread(target=_handle, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                continue

    def _handle_incoming(self, conn, addr):
        try:
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk: break
                data += chunk
                if len(data) > 10_000_000: break
            msg       = json.loads(data.decode())
            msg_type  = msg.get("type")
            sender_ip = addr[0]

            if msg_type == "HELLO":
                pid = msg.get("device_id")
                if _ver(msg.get("version", "0.0")) < _ver(MIN_VERSION):
                    conn.sendall(json.dumps({"type": "VERSION_REJECTED",
                                             "reason": f"Update from https://github.com/EvokiTimpal/timpal"}).encode())
                    return
                if pid and pid != self.wallet.device_id:
                    with self._peers_lock:
                        if pid not in self.peers and len(self.peers) >= MAX_PEERS:
                            oldest = min(self.peers, key=lambda k: self.peers[k]["last_seen"])
                            del self.peers[oldest]
                        self.peers[pid] = {"ip": sender_ip, "port": msg.get("port", 7779),
                                           "last_seen": time.time()}
                    conn.sendall(json.dumps({"type": "HELLO_ACK", "device_id": self.wallet.device_id}).encode())
                    threading.Thread(target=self._sync_ledger, daemon=True).start()

            elif msg_type == "TRANSACTION":
                tx_dict      = msg.get("transaction", {})
                tx_gossip_id = tx_dict.get("tx_id", "")
                if not tx_gossip_id:
                    return
                try:
                    tx = Transaction.from_dict(tx_dict)
                except Exception:
                    return
                # Atomic check-and-add (fixes TOCTOU — single lock acquisition)
                with self._seen_lock:
                    if tx_gossip_id in self.seen_ids:
                        return
                    self.seen_ids.add(tx_gossip_id)
                    self._seen_tx_order.append(tx_gossip_id)
                self.on_transaction(tx)
                threading.Thread(target=self.broadcast, args=(msg, None), daemon=True).start()

            elif msg_type == "FEE_REWARDS":
                if is_era2():
                    ts  = msg.get("time_slot")
                    frs = msg.get("fee_rewards", [])[:1000]
                    frs = [fr for fr in frs if isinstance(fr.get("winner_id"), str)
                           and len(fr["winner_id"]) == 64
                           and all(c in "0123456789abcdef" for c in fr["winner_id"].lower())]
                    if frs and ts is not None:
                        with self.ledger._lock:
                            actual_fees = sum(t.get("fee", 0.0) for t in self.ledger.transactions
                                              if t.get("slot") == ts and t.get("fee", 0.0) > 0)
                        claimed = sum(fr.get("amount", 0.0) for fr in frs)
                        if claimed <= actual_fees + 0.000001:
                            expected = round(actual_fees / len(frs), 8)
                            if all(abs(fr.get("amount", 0.0) - expected) < 0.000001 for fr in frs):
                                for fr in frs:
                                    self.ledger.add_fee_reward(ts, fr["winner_id"], fr["amount"])

            elif msg_type == "REWARD":
                # REWARD flood rate limit: max REWARD_RATE_LIMIT per IP per slot
                current_slot = get_current_slot()
                with self._reward_rate_lock:
                    s, cnt = self._reward_rate.get(sender_ip, (current_slot, 0))
                    if s != current_slot:
                        cnt = 0
                    if cnt >= REWARD_RATE_LIMIT:
                        return
                    self._reward_rate[sender_ip] = (current_slot, cnt + 1)
                reward = msg.get("reward", {})
                gid    = reward.get("reward_id", "") + ":" + reward.get("winner_id", "")
                with self._seen_lock:
                    if gid and gid not in self.seen_ids:
                        self.seen_ids.add(gid)
                    else:
                        gid = None
                if gid:
                    self.on_reward(reward)
                    threading.Thread(target=self.broadcast, args=(msg, None), daemon=True).start()

            elif msg_type == "SYNC_PUSH":
                with self._peers_lock:
                    known_ips = {p["ip"] for p in self.peers.values()}
                if sender_ip not in known_ips:
                    return
                self.ledger.merge({"rewards": msg.get("rewards", [])[:5000],
                                   "transactions": msg.get("txs", [])[:2000]})

            elif msg_type == "CHECKPOINT":
                cp = msg.get("checkpoint", {})
                if cp:
                    gid = f"checkpoint:{cp.get('slot', '')}"
                    with self._seen_lock:
                        if gid in self.seen_ids:
                            gid = None
                        else:
                            self.seen_ids.add(gid)
                    if gid:
                        pb = cp.get("prune_before", 0)
                        with self.ledger._lock:
                            can_verify = bool(
                                [r for r in self.ledger.rewards if r.get("time_slot", pb) < pb] or
                                [t for t in self.ledger.transactions if (t.get("slot") or 0) < pb])
                        if can_verify:
                            applied = self.ledger.apply_checkpoint(cp)
                        elif self._confirm_checkpoint_with_peers(cp, exclude_ip=sender_ip):
                            applied = self.ledger.apply_checkpoint(cp)
                        else:
                            applied = False
                        if applied:
                            self.broadcast({"type": "CHECKPOINT", "checkpoint": cp})

            elif msg_type == "SYNC_REQUEST":
                # Rate limit: max 1 per IP per SYNC_RATE_WINDOW seconds
                now = time.time()
                with self._sync_rate_lock:
                    if now - self._sync_rate.get(sender_ip, 0) < SYNC_RATE_WINDOW:
                        return
                    self._sync_rate[sender_ip] = now
                their_slots  = set(msg.get("known_slots", [])[:10000])
                their_tx_ids = set(msg.get("known_tx_ids", [])[:10000])
                their_cp     = msg.get("checkpoint_slot", 0)
                with self.ledger._lock:
                    our_slots  = set(r.get("time_slot") for r in self.ledger.rewards
                                     if r.get("time_slot") is not None and r.get("type") == "block_reward")
                    our_tx_ids = set(t.get("tx_id") for t in self.ledger.transactions if t.get("tx_id"))
                    missing_r  = [r for r in self.ledger.rewards
                                  if r.get("type") == "fee_reward" or r.get("time_slot") not in their_slots][:5000]
                    missing_t  = [t for t in self.ledger.transactions if t.get("tx_id") not in their_tx_ids][:2000]
                    our_cp     = None
                    if self.ledger.checkpoints:
                        latest = self.ledger.checkpoints[-1]
                        if latest["slot"] > their_cp:
                            our_cp = latest
                conn.sendall(json.dumps({
                    "type": "SYNC_RESPONSE", "rewards": missing_r, "txs": missing_t,
                    "total": len(self.ledger.rewards),
                    "we_need_slots": list(their_slots - our_slots),
                    "we_need_tx_ids": list(their_tx_ids - our_tx_ids),
                    "checkpoint": our_cp
                }).encode())
        except Exception:
            pass
        finally:
            conn.close()

    def broadcast(self, message: dict, exclude_id: str = None):
        msg_bytes = json.dumps(message).encode()
        online    = self.get_online_peers()
        peers     = list(online.items())
        random.shuffle(peers)
        for peer_id, peer in peers[:BROADCAST_FANOUT]:
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

    def get_online_peers(self):
        cutoff = time.time() - 120
        with self._peers_lock:
            return {pid: info for pid, info in self.peers.items() if info["last_seen"] > cutoff}


class Node:
    def __init__(self):
        self.wallet  = Wallet()
        self.ledger  = Ledger()
        self.network = None
        self._acquire_lock()
        self._load_or_create_wallet()
        self.network = Network(self.wallet, self.ledger,
                               self._on_transaction_received, self._on_reward_received)
        self.network._node_ref = self
        self._sending      = False
        self._my_tickets   = {}
        self._commits      = {}
        self._lottery_lock = threading.Lock()

    def _acquire_lock(self):
        import sys
        lock_path       = os.path.join(os.path.expanduser("~"), ".timpal.lock")
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
            try:
                with open(WALLET_FILE, "r") as f:
                    wd = json.load(f)
                if _ver(wd.get("version", "0.0")) < _ver(VERSION):
                    print("\n  " + "═"*52)
                    print("  TIMPAL v2.2 — ACTION REQUIRED")
                    print("  " + "═"*52)
                    print(f"  Wallet version {wd.get('version','?')} is too old.")
                    print(f"  Delete old wallet and ledger, then restart:")
                    print(f"  rm ~/.timpal_wallet.json ~/.timpal_ledger.json")
                    print("  " + "═"*52 + "\n")
                    exit(1)
            except Exception:
                pass
            with open(WALLET_FILE, "r") as f:
                wd = json.load(f)
            if wd.get("encrypted"):
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
                print("\n  ⚠️  Your wallet is not encrypted.")
                if input("  Encrypt now? (yes/no): ").strip().lower() == "yes":
                    while True:
                        pw  = getpass.getpass("  Password (min 8 chars): ")
                        pw2 = getpass.getpass("  Confirm: ")
                        if pw != pw2:
                            print("  Passwords do not match."); continue
                        if len(pw) < 8:
                            print("  Too short."); continue
                        self.wallet.save(password=pw)
                        print("  Wallet encrypted.")
                        break
            balance = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n  Wallet loaded.")
            print(f"  Device ID : {self.wallet.device_id[:24]}...")
            print(f"  Balance   : {balance:.8f} TMPL")
        else:
            self.wallet.create_new()
            print("\n  Set a password to encrypt your wallet.")
            while True:
                pw  = getpass.getpass("  Password (min 8 chars): ")
                pw2 = getpass.getpass("  Confirm: ")
                if pw != pw2:
                    print("  Passwords do not match."); continue
                if len(pw) < 8:
                    print("  Too short."); continue
                self.wallet.save(password=pw)
                print("  Wallet encrypted and saved.")
                break

    _tx_rate      = {}
    _tx_rate_lock = threading.Lock()

    def _on_transaction_received(self, tx: Transaction):
        if not tx.verify() or tx.amount <= 0:
            return
        now = time.time()
        with Node._tx_rate_lock:
            times = [t for t in Node._tx_rate.get(tx.sender_id, []) if now - t < 60]
            if len(times) >= 60:
                return
            times.append(now)
            Node._tx_rate[tx.sender_id] = times
        if not self.ledger.add_transaction(tx.to_dict()):
            return
        if tx.recipient_id == self.wallet.device_id:
            bal = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n  ╔══════════════════════════════════════╗")
            print(f"  ║       TMPL RECEIVED                  ║")
            print(f"  ╠══════════════════════════════════════╣")
            print(f"  ║  Amount  : {tx.amount:.8f} TMPL")
            print(f"  ║  From    : {tx.sender_id[:20]}...")
            print(f"  ║  Balance : {bal:.8f} TMPL")
            print(f"  ╚══════════════════════════════════════╝\n  > ", end="", flush=True)

    def _on_reward_received(self, reward: dict):
        if not self.ledger.add_reward(reward):
            return
        if reward.get("winner_id") == self.wallet.device_id and not self._sending:
            bal = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n  ╔══════════════════════════════════════╗")
            print(f"  ║       REWARD WON! ★                  ║")
            print(f"  ╠══════════════════════════════════════╣")
            print(f"  ║  Amount  : {reward['amount']:.8f} TMPL")
            print(f"  ║  Balance : {bal:.8f} TMPL")
            print(f"  ╚══════════════════════════════════════╝\n  > ", end="", flush=True)
        else:
            print(f"\n  [slot {reward.get('time_slot','?')}] "
                  f"Winner: {reward.get('winner_id','')[:20]}... "
                  f"+{reward['amount']} TMPL\n  > ", end="", flush=True)

    def _vrf_ticket(self, time_slot: int) -> tuple:
        seed   = str(time_slot)
        sig    = Dilithium3.sign(self.wallet.private_key, seed.encode())
        ticket = hashlib.sha256(sig).hexdigest()
        return ticket, sig.hex(), seed

    @staticmethod
    def _verify_ticket(public_key_hex: str, seed: str, sig_hex: str, ticket: str) -> bool:
        try:
            pub = bytes.fromhex(public_key_hex)
            sig = bytes.fromhex(sig_hex)
            if not Dilithium3.verify(pub, seed.encode(), sig):
                return False
            return hashlib.sha256(sig).hexdigest() == ticket
        except Exception:
            return False

    def _make_commit(self, time_slot: int, ticket: str) -> str:
        return hashlib.sha256(f"{ticket}:{self.wallet.device_id}:{time_slot}".encode()).hexdigest()

    def _is_eligible_this_slot(self, time_slot: int, network_size: int) -> bool:
        """Identical formula to bootstrap.py — must stay in sync."""
        if network_size <= TARGET_PARTICIPANTS:
            return True
        threshold = TARGET_PARTICIPANTS / network_size
        h = int(hashlib.sha256(f"{self.wallet.device_id}:{time_slot}".encode()).hexdigest(), 16)
        return h < int(threshold * (2 ** 256))

    def _bootstrap_submit(self, msg: dict):
        """Fire-and-forget — used for reveals."""
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

    def _bootstrap_submit_commit(self, msg: dict) -> str:
        """Submit commit and read response synchronously.
        Returns 'COMMIT_ACK' or 'COMMIT_REJECTED'.
        If all bootstrap servers unreachable, returns 'COMMIT_ACK'
        (commit was never registered so no reveal obligation exists)."""
        results   = []
        lock      = threading.Lock()
        done      = threading.Event()
        servers   = list(self.network._bootstrap_servers)
        remaining = [len(servers)]

        def _send(host, port):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((host, port))
                sock.sendall(json.dumps(msg).encode())
                sock.shutdown(socket.SHUT_WR)
                resp = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk: break
                    resp += chunk
                    if len(resp) > 65536: break
                sock.close()
                data = json.loads(resp.decode())
                ns = data.get("network_size", 0)
                if ns > 0:
                    self.network._network_size = ns
                with lock:
                    results.append(data.get("type", "ERROR"))
            except Exception:
                with lock:
                    results.append("ERROR")
            finally:
                with lock:
                    remaining[0] -= 1
                    if remaining[0] <= 0:
                        done.set()

        if not servers:
            return "COMMIT_ACK"
        for host, port in servers:
            threading.Thread(target=_send, args=(host, port), daemon=True).start()
        done.wait(timeout=4.0)
        with lock:
            if "COMMIT_REJECTED" in results:
                return "COMMIT_REJECTED"
            return "COMMIT_ACK"

    def _bootstrap_query_commits(self, slot: int) -> dict:
        results = {}
        lock = threading.Lock()
        done = threading.Event()
        servers = list(self.network._bootstrap_servers)
        remaining = [len(servers)]
        def _query(host, port):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((host, port))
                sock.sendall(json.dumps({"type": "GET_COMMITS", "slot": slot}).encode())
                sock.shutdown(socket.SHUT_WR)
                resp = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk: break
                    resp += chunk
                    if len(resp) > 1_000_000: break
                sock.close()
                data = json.loads(resp.decode())
                with lock:
                    for k, v in data.get("commits", {}).items():
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

    def _bootstrap_query_reveals(self, slot: int) -> tuple:
        """Returns (reveals_dict, collective_target_or_None)."""
        results   = {}
        bs_target = [None]
        lock      = threading.Lock()
        done      = threading.Event()
        servers   = list(self.network._bootstrap_servers)
        remaining = [len(servers)]
        def _query(host, port):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((host, port))
                sock.sendall(json.dumps({"type": "GET_REVEALS", "slot": slot}).encode())
                sock.shutdown(socket.SHUT_WR)
                resp = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk: break
                    resp += chunk
                    if len(resp) > 1_000_000: break
                sock.close()
                data = json.loads(resp.decode())
                with lock:
                    for k, v in data.get("reveals", {}).items():
                        if k not in results:
                            results[k] = v
                    if data.get("collective_target") and bs_target[0] is None:
                        bs_target[0] = data["collective_target"]
            except Exception:
                pass
            finally:
                with lock:
                    remaining[0] -= 1
                    if remaining[0] <= 0:
                        done.set()
        if not servers:
            return {}, None
        for host, port in servers:
            threading.Thread(target=_query, args=(host, port), daemon=True).start()
        done.wait(timeout=4.0)
        return results, bs_target[0]

    def _pick_winner(self, time_slot: int, all_reveals: dict):
        """Winner = node with valid ticket closest to collective target.
        Collective target = sha256(all tickets sorted and joined with ':').
        This value is unknown until all reveals are in — cannot be predicted.
        Tiebreaker: device_id (deterministic across all nodes)."""
        verified = {}
        with self._lottery_lock:
            known_commits = dict(self._commits.get(time_slot, {}))
        for device_id, r in all_reveals.items():
            # Use .get() on all fields — data comes from untrusted bootstrap.
            # Missing fields raise KeyError which would silently kill winner
            # selection for the entire slot.
            ticket     = r.get("ticket", "")
            public_key = r.get("public_key", "")
            seed       = r.get("seed", "")
            sig        = r.get("sig", "")
            if not (ticket and public_key and seed and sig):
                continue
            commit = known_commits.get(device_id)
            if not commit:
                continue
            expected = hashlib.sha256(f"{ticket}:{device_id}:{time_slot}".encode()).hexdigest()
            if expected != commit:
                continue
            if not Node._verify_ticket(public_key, seed, sig, ticket):
                continue
            verified[device_id] = r
        if not verified:
            return None
        # Collective target: sha256 of ALL reveals (not just verified) sorted and joined.
        # Cannot be predicted before the reveal window closes.
        # Only include reveals that have a ticket field (malformed reveals are excluded).
        tickets    = sorted(r["ticket"] for r in all_reveals.values() if r.get("ticket"))
        target     = hashlib.sha256(":".join(tickets).encode()).hexdigest()
        target_int = int(target, 16)
        winner_id  = min(verified, key=lambda d: (
            abs(int(verified[d]["ticket"], 16) - target_int), d))
        w = verified[winner_id]
        return {"winner_id": winner_id, "ticket": w["ticket"],
                "sig": w["sig"], "seed": w["seed"], "public_key": w["public_key"]}

    def _distribute_fees(self, time_slot: int, active_nodes: list):
        if not is_era2() or not active_nodes:
            return
        with self.ledger._lock:
            slot_fees = sum(t.get("fee", 0.0) for t in self.ledger.transactions
                            if t.get("slot") == time_slot and t.get("fee", 0.0) > 0)
        if slot_fees <= 0:
            return
        per_node    = round(slot_fees / len(active_nodes), 8)
        fee_rewards = []
        for node_id in active_nodes:
            if self.ledger.add_fee_reward(time_slot, node_id, per_node):
                fee_rewards.append({"reward_id": f"fee:{time_slot}:{node_id}",
                                    "winner_id": node_id, "amount": per_node,
                                    "timestamp": time.time(), "time_slot": time_slot,
                                    "type": "fee_reward"})
        if fee_rewards:
            self.network.broadcast({"type": "FEE_REWARDS", "time_slot": time_slot,
                                    "fee_rewards": fee_rewards})

    def _claim_reward(self, winner: dict, time_slot: int, active_nodes=None):
        if not Node._verify_ticket(winner["public_key"], winner["seed"],
                                   winner["sig"], winner["ticket"]):
            return
        with self.ledger._lock:
            if any(r.get("time_slot") == time_slot and r.get("type") != "fee_reward"
                   for r in self.ledger.rewards):
                return
        reward_id = f"reward:{time_slot}"
        reward = {
            "reward_id": reward_id, "winner_id": winner["winner_id"],
            "amount": REWARD_PER_ROUND, "timestamp": time.time(),
            "time_slot": time_slot, "vrf_ticket": winner["ticket"],
            "vrf_seed": winner["seed"], "vrf_sig": winner["sig"],
            "vrf_public_key": winner["public_key"],
            "nodes": len(active_nodes) if active_nodes else 1,
            "type": "block_reward"
        }
        if not self.ledger.add_reward(reward):
            return
        gid = reward_id + ":" + winner["winner_id"]
        with self.network._seen_lock:
            self.network.seen_ids.add(gid)
        self.network.broadcast({"type": "REWARD", "reward": reward})
        if winner["winner_id"] == self.wallet.device_id:
            bal = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n  ╔══════════════════════════════════════╗")
            print(f"  ║       REWARD WON! ★                  ║")
            print(f"  ╠══════════════════════════════════════╣")
            print(f"  ║  Amount  : {REWARD_PER_ROUND:.8f} TMPL")
            print(f"  ║  Balance : {bal:.8f} TMPL")
            print(f"  ╚══════════════════════════════════════╝\n  > ", end="", flush=True)
        else:
            print(f"\n  [slot {time_slot}] Winner: {winner['winner_id'][:20]}... "
                  f"+{REWARD_PER_ROUND} TMPL\n  > ", end="", flush=True)
        if active_nodes:
            self._distribute_fees(time_slot, active_nodes)

    def _cleanup_slot(self, time_slot: int):
        with self._lottery_lock:
            for s in [s for s in self._commits if s < time_slot - 10]:
                del self._commits[s]
        for s in [s for s in self._my_tickets if s < time_slot - 10]:
            del self._my_tickets[s]
        cutoff = time_slot - 100
        with self.network._seen_lock:
            def _stale(sid):
                try:
                    if sid.startswith(("reward:", "checkpoint:", "fee:")):
                        return int(sid.split(":")[1]) < cutoff
                except Exception:
                    pass
                return False
            for sid in [s for s in self.network.seen_ids if _stale(s)]:
                self.network.seen_ids.discard(sid)
            if len(self.network._seen_tx_order) > 10000:
                to_remove = self.network._seen_tx_order[:-10000]
                for sid in to_remove:
                    self.network.seen_ids.discard(sid)
                self.network._seen_tx_order = self.network._seen_tx_order[-10000:]

    def _reward_lottery(self):
        """Eligibility-gated commit-reveal VRF lottery.

        t=0.0  Eligibility check. If not eligible: skip slot silently.
               Generate ticket. Submit COMMIT — read response synchronously.
               COMMIT_REJECTED → skip slot. COMMIT_ACK → record and proceed.
        t=2.0  Fetch commits. Submit REVEAL (fire-and-forget).
        t=4.0  Fetch reveals + collective target.
        t=4.5  Pick winner (closest ticket to collective target). Claim. Gossip.
        """
        time.sleep(45)
        while self.network._running:
            now            = time.time()
            elapsed        = now - GENESIS_TIME
            next_slot_time = GENESIS_TIME + (int(elapsed / REWARD_INTERVAL) + 1) * REWARD_INTERVAL
            time.sleep(max(0.05, next_slot_time - time.time()))

            if is_era2():
                continue

            time_slot  = get_current_slot()
            if time_slot < 0:
                continue
            slot_start = GENESIS_TIME + time_slot * REWARD_INTERVAL

            with self.ledger._lock:
                already_won = any(r.get("time_slot") == time_slot for r in self.ledger.rewards)
            if already_won:
                self._cleanup_slot(time_slot)
                continue

            # Eligibility self-check
            if not self._is_eligible_this_slot(time_slot, self.network._network_size):
                continue

            # Generate ticket and submit commit — read response
            ticket, sig_hex, seed = self._vrf_ticket(time_slot)
            self._my_tickets[time_slot] = (ticket, sig_hex, seed)
            commit = self._make_commit(time_slot, ticket)

            result = self._bootstrap_submit_commit({
                "type": "SUBMIT_COMMIT", "device_id": self.wallet.device_id,
                "slot": time_slot, "commit": commit
            })

            if result == "COMMIT_REJECTED":
                print(f"\n  [slot {time_slot}] Commit rejected (ban active). Skipping.\n  > ",
                      end="", flush=True)
                self._cleanup_slot(time_slot)
                continue

            # Record commit locally ONLY after bootstrap ACK
            with self._lottery_lock:
                self._commits.setdefault(time_slot, {})[self.wallet.device_id] = commit

            # Wait until t=2.0
            remaining = slot_start + 2.0 - time.time()
            if remaining > 0:
                time.sleep(remaining)
            with self.ledger._lock:
                already_won = any(r.get("time_slot") == time_slot for r in self.ledger.rewards)
            if already_won:
                self._cleanup_slot(time_slot); continue

            commits_merged = self._bootstrap_query_commits(time_slot)
            with self._lottery_lock:
                for did, c in commits_merged.items():
                    self._commits.setdefault(time_slot, {}).setdefault(did, c)

            # Submit reveal — fire-and-forget
            self._bootstrap_submit({
                "type": "SUBMIT_REVEAL", "device_id": self.wallet.device_id,
                "slot": time_slot, "ticket": ticket, "sig": sig_hex,
                "seed": seed, "public_key": self.wallet.public_key.hex()
            })

            # Wait until t=4.0
            remaining = slot_start + 4.0 - time.time()
            if remaining > 0:
                time.sleep(remaining)
            with self.ledger._lock:
                already_won = any(r.get("time_slot") == time_slot for r in self.ledger.rewards)
            if already_won:
                self._cleanup_slot(time_slot); continue

            all_reveals, _ = self._bootstrap_query_reveals(time_slot)

            # Add self to reveals — commit was ACKed so we are eligible
            all_reveals[self.wallet.device_id] = {
                "ticket": ticket, "sig": sig_hex,
                "seed": seed, "public_key": self.wallet.public_key.hex()
            }

            # Wait until t=4.5
            remaining = slot_start + 4.5 - time.time()
            if remaining > 0:
                time.sleep(remaining)
            with self.ledger._lock:
                already_won = any(r.get("time_slot") == time_slot for r in self.ledger.rewards)
            if already_won:
                self._cleanup_slot(time_slot); continue

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
        peer_id = peer_id.lower().strip()
        if not (len(peer_id) == 64 and all(c in "0123456789abcdef" for c in peer_id)):
            print("\n  Invalid address. Must be 64-character hex.")
            return False
        if peer_id == self.wallet.device_id:
            print("\n  Cannot send to yourself.")
            return False
        my_balance = self.ledger.get_balance(self.wallet.device_id)
        fee        = get_current_fee()
        total_cost = round(amount + fee, 8)
        if total_cost > my_balance:
            print(f"\n  Insufficient balance. Have {my_balance:.8f}, need {total_cost:.8f}.")
            return False
        tx = Transaction(sender_id=self.wallet.device_id, recipient_id=peer_id,
                         sender_pubkey=self.wallet.get_public_key_hex(),
                         amount=amount, fee=fee, slot=get_current_slot())
        tx.sign(self.wallet)
        if not self.ledger.add_transaction(tx.to_dict()):
            print("\n  Transaction rejected by ledger.")
            return False
        with self.network._seen_lock:
            self.network.seen_ids.add(tx.tx_id)
            self.network._seen_tx_order.append(tx.tx_id)
        self.network.broadcast({"type": "TRANSACTION", "transaction": tx.to_dict()})
        print(f"\n  ✓ Sent {amount:.8f} TMPL to {peer_id[:24]}...")
        if fee > 0:
            print(f"  Fee paid   : {fee:.8f} TMPL")
        print(f"  New balance: {self.ledger.get_balance(self.wallet.device_id):.8f} TMPL")
        return True

    def _control_server(self):
        token      = os.urandom(32).hex()
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
                    if not chunk: break
                    data += chunk
                    if len(data) > 65536 or data.endswith(b"\n"): break
                try:
                    cmd = json.loads(data.decode().strip())
                    if cmd.get("token") != token:
                        conn.sendall((json.dumps({"ok": False, "error": "unauthorized"}) + "\n").encode())
                        conn.close(); continue
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
            return {"ok": True, "balance": self.ledger.get_balance(self.wallet.device_id),
                    "address": self.wallet.device_id}
        elif action == "send":
            return {"ok": self.send(cmd.get("peer_id"), float(cmd.get("amount", 0)))}
        elif action == "network":
            s = self.ledger.get_summary()
            return {"ok": True, "peers": len(self.network.get_online_peers()),
                    "transactions": s["total_transactions"],
                    "total_rewards": s["total_rewards"],
                    "minted": s["total_minted"], "remaining": s["remaining_supply"]}
        return {"ok": False, "error": "Unknown action"}

    def _push_to_explorer(self):
        """Push signed ledger updates to explorer API.
        No shared secret — pushes are signed with node's Dilithium3 private key.
        API verifies signature cryptographically."""
        time.sleep(60)
        ssl_ctx = ssl.create_default_context()
        while self.network._running:
            try:
                import urllib.request
                with self.ledger._lock:
                    valid_rewards  = [r for r in self.ledger.rewards
                                      if r.get("type") != "block_reward"
                                      or _is_valid_epoch_slot(r.get("time_slot"))]
                    my_rewards     = [r for r in valid_rewards
                                      if r.get("winner_id") == self.wallet.device_id][-200:]
                    recent_rewards = list(valid_rewards[-50:])
                    seen_rids      = set()
                    rewards        = []
                    for r in my_rewards + recent_rewards:
                        rid = r.get("reward_id", "")
                        if rid not in seen_rids:
                            seen_rids.add(rid)
                            rewards.append({k: v for k, v in r.items()
                                            if k not in ("vrf_sig", "vrf_public_key")})
                    txs          = list(self.ledger.transactions[-20:])
                    total_minted = self.ledger.total_minted  # captured inside lock

                payload_data = {
                    "type":         "LEDGER_PUSH",
                    "device_id":    self.wallet.device_id,
                    "public_key":   self.wallet.get_public_key_hex(),
                    "rewards":      rewards,
                    "transactions": txs,
                    "total_minted": total_minted,
                    "timestamp":    int(time.time())
                }
                payload_bytes = json.dumps(payload_data, sort_keys=True, separators=(',', ':')).encode()
                payload_data["signature"] = self.wallet.sign(payload_bytes)
                req = urllib.request.Request(
                    "https://timpal.org/api",
                    data    = json.dumps(payload_data).encode(),
                    headers = {"Content-Type": "application/json"},
                    method  = "POST"
                )
                urllib.request.urlopen(req, timeout=5, context=ssl_ctx)
            except Exception:
                pass
            time.sleep(5)

    def _checkpoint_loop(self):
        while self.network._running:
            try:
                current_slot = get_current_slot()
                if self.ledger.checkpoints:
                    next_cp = self.ledger.checkpoints[-1]["slot"] + CHECKPOINT_INTERVAL
                else:
                    boundary = (current_slot // CHECKPOINT_INTERVAL) * CHECKPOINT_INTERVAL
                    next_cp  = boundary if boundary > 0 else CHECKPOINT_INTERVAL
                if current_slot >= next_cp + CHECKPOINT_BUFFER:
                    if self.ledger.create_checkpoint(next_cp):
                        print(f"\n  ╔══════════════════════════════════════╗")
                        print(f"  ║       CHECKPOINT CREATED             ║")
                        print(f"  ╠══════════════════════════════════════╣")
                        print(f"  ║  Slot : {next_cp}")
                        print(f"  ╚══════════════════════════════════════╝\n  > ", end="", flush=True)
                        if self.ledger.checkpoints:
                            latest = self.ledger.checkpoints[-1]
                            gid    = f"checkpoint:{latest['slot']}"
                            with self.network._seen_lock:
                                self.network.seen_ids.add(gid)
                            self.network.broadcast({"type": "CHECKPOINT", "checkpoint": latest})
            except Exception:
                pass
            time.sleep(30)

    def start(self):
        print("\n" + "═"*54)
        print("  TIMPAL v2.2 — Quantum-Resistant Money Without Masters")
        print("  Quantum-Resistant | Worldwide | Instant")
        print("═"*54)
        self.network.start()
        balance = self.ledger.get_balance(self.wallet.device_id)
        summary = self.ledger.get_summary()
        print(f"  Device ID : {self.wallet.device_id[:24]}...")
        print(f"  Balance   : {balance:.8f} TMPL")
        print(f"  Network   : {self.network.local_ip}:{self.network.port}")
        print(f"  Minted    : {summary['total_minted']:.4f} / {TOTAL_SUPPLY:,.0f} TMPL")
        print("═"*54)
        print("  Commands: balance | peers | send | history | network | quit")
        print("═"*54 + "\n")
        threading.Thread(target=self._reward_lottery, daemon=True).start()
        threading.Thread(target=self._control_server, daemon=True).start()
        threading.Thread(target=self._push_to_explorer, daemon=True).start()
        threading.Thread(target=self._checkpoint_loop, daemon=True).start()
        self._cli()

    def _cli(self):
        import sys
        if not sys.stdin.isatty():
            import signal
            def shutdown(sig, frame): self.network.stop()
            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)
            while self.network._running: time.sleep(1)
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
                bal = self.ledger.get_balance(self.wallet.device_id)
                print(f"\n  Balance: {bal:.8f} TMPL\n  Device : {self.wallet.device_id}\n")
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
                s     = self.ledger.get_summary()
                peers = self.network.get_online_peers()
                print(f"\n  Network Status:")
                print(f"  Online peers      : {len(peers)}")
                print(f"  Total transactions: {s['total_transactions']}")
                print(f"  Total rewards     : {s['total_rewards']}")
                print(f"  Total minted      : {s['total_minted']:.8f} TMPL")
                print(f"  Remaining supply  : {s['remaining_supply']:.8f} TMPL")
                print(f"  Network size est. : {self.network._network_size}")
                print(f"  Bootstrap         : {BOOTSTRAP_HOST}:{BOOTSTRAP_PORT}\n")
            elif raw == "send":
                self._sending = True
                try:
                    peer_list = list(self.network.get_online_peers().items())
                    if peer_list:
                        print(f"\n  Online peers:")
                        for i, (pid, info) in enumerate(peer_list):
                            print(f"  [{i+1}] {pid[:24]}... — {info['ip']}")
                        print(f"\n  Enter peer number or full address:")
                    else:
                        print(f"\n  No peers online. Enter recipient address:")
                    try:
                        choice = input("  > ").strip()
                        if peer_list and choice.isdigit():
                            idx = int(choice) - 1
                            if idx < 0 or idx >= len(peer_list):
                                print("  Invalid selection.\n"); continue
                            peer_id = peer_list[idx][0]
                        else:
                            peer_id = choice.lower().strip()
                            if not (len(peer_id) == 64 and all(c in "0123456789abcdef" for c in peer_id)):
                                print("  Invalid address.\n"); continue
                    except (ValueError, IndexError):
                        print("  Invalid selection.\n"); continue
                    balance = self.ledger.get_balance(self.wallet.device_id)
                    try:
                        amount = float(input(f"  Amount (balance: {balance:.8f}): ").strip())
                    except ValueError:
                        print("  Invalid amount.\n"); continue
                    self.send(peer_id, amount)
                finally:
                    self._sending = False
            elif raw == "history":
                my_id = self.wallet.device_id
                with self.ledger._lock:
                    my_tx      = [t for t in self.ledger.transactions
                                  if t["sender_id"] == my_id or t["recipient_id"] == my_id]
                    my_rewards = [r for r in self.ledger.rewards if r["winner_id"] == my_id]
                if not my_tx and not my_rewards:
                    print("\n  No transactions yet.\n")
                else:
                    print(f"\n  Your history:")
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
    _check_genesis_time()

    if len(sys.argv) >= 2 and sys.argv[1] == "send":
        if len(sys.argv) != 4:
            print("Usage: python3 timpal.py send <address> <amount>")
            sys.exit(1)
        recipient_id = sys.argv[2].lower().strip()
        try:
            amount = float(sys.argv[3])
        except ValueError:
            print("Invalid amount."); sys.exit(1)
        if not (len(recipient_id) == 64 and all(c in "0123456789abcdef" for c in recipient_id)):
            print("Invalid address."); sys.exit(1)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(("127.0.0.1", 7780))
            token = ""
            try:
                with open(os.path.join(os.path.expanduser("~"), ".timpal_control.token")) as f:
                    token = f.read().strip()
            except Exception:
                pass
            sock.sendall((json.dumps({"action": "send", "peer_id": recipient_id,
                                      "amount": amount, "token": token}) + "\n").encode())
            resp = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk: break
                resp += chunk
                if resp.endswith(b"\n"): break
            sock.close()
            result = json.loads(resp.decode().strip())
            if result.get("ok"):
                print(f"Sent {amount:.8f} TMPL to {recipient_id[:24]}...")
            else:
                print(f"Failed: {result.get('error', 'unknown')}")
        except ConnectionRefusedError:
            print("Node not running. Start with: python3 timpal.py")
        sys.exit(0)

    elif len(sys.argv) >= 2 and sys.argv[1] == "balance":
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(("127.0.0.1", 7780))
            token = ""
            try:
                with open(os.path.join(os.path.expanduser("~"), ".timpal_control.token")) as f:
                    token = f.read().strip()
            except Exception:
                pass
            sock.sendall((json.dumps({"action": "balance", "token": token}) + "\n").encode())
            resp = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk: break
                resp += chunk
                if resp.endswith(b"\n"): break
            sock.close()
            result = json.loads(resp.decode().strip())
            if result.get("ok"):
                print(f"Balance : {result['balance']:.8f} TMPL")
                print(f"Address : {result['address']}")
                sys.exit(0)
        except Exception:
            pass
        sys.exit(0)

    else:
        node = Node()
        node.start()
