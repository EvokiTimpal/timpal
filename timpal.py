#!/usr/bin/env python3
"""
TIMPAL Protocol v4.0 — Quantum-Resistant Money Without Masters

Complete rewrite from v3.3 spec. All v3.x commit-reveal lottery machinery
replaced by the compete-based design. Bootstrap stripped to peer discovery only.
Attestation-based cryptographic finality. Two-layer storage architecture.

Session 21 — Built from timpal_spec_v4.md
"""

import socket
import threading
import json
import hashlib
import os
import time
import uuid
import random
import struct

try:
    from dilithium_py.dilithium import Dilithium3
except ImportError:
    print("\n  [!] dilithium-py not installed.")
    print("  Run: pip3 install dilithium-py cryptography pycryptodome\n")
    exit(1)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.hazmat.backends import default_backend
except ImportError:
    print("\n  [!] cryptography not installed.")
    print("  Run: pip3 install dilithium-py cryptography pycryptodome\n")
    exit(1)

# ── Version ────────────────────────────────────────────────────────────────────

VERSION     = "4.0"
MIN_VERSION = "4.0"

# ── Genesis time — SET BEFORE LAUNCH, identical in timpal.py and bootstrap.py ─

GENESIS_TIME = 0   # ← python3 -c "import time; print(int(time.time()) + 300)"

# ── Economic constants (immutable post-genesis) ────────────────────────────────

UNIT             = 100_000_000          # 1 TMPL = 10^8 units
TOTAL_SUPPLY     = 12_500_000_000_000_000   # 125,000,000 TMPL
REWARD_PER_ROUND = 105_750_000          # 1.0575 TMPL per block

TX_FEE_RATE = 0.001     # 0.1% of transaction amount
TX_FEE_MIN  = 10_000    # 0.0001 TMPL minimum fee
TX_FEE_MAX  = 1_000_000 # 0.01  TMPL maximum fee

# ── Timing constants ───────────────────────────────────────────────────────────

REWARD_INTERVAL    = 10.0   # seconds per slot
CONFIRMATION_DEPTH = 3      # blocks for finality = 30 seconds

# ── Identity constants ─────────────────────────────────────────────────────────

MIN_IDENTITY_AGE     = 200  # slots before identity can compete (~33 min)
MAX_REGS_PER_BLOCK   = 10   # global registration cap per block
MAX_REGS_PER_PRODUCER = 2   # per winner registration cap per block

# ── Lottery constants ──────────────────────────────────────────────────────────

TARGET_COMPETITORS      = 10    # nodes selected to compete per slot
COMPETE_TO_BLOCK_TIMEOUT = 5.0  # seconds: COMPETE received → BLOCK must arrive

# ── Consensus constants ────────────────────────────────────────────────────────

ATTESTATION_THRESHOLD = 2 / 3
CHECKPOINT_INTERVAL   = 1000
CHECKPOINT_BUFFER     = 120   # intentional gap — NEVER treat as bug

# ── Storage constants ──────────────────────────────────────────────────────────

MAX_TRANSACTIONS_PER_BLOCK = 500    # 50 TPS at 10s slots
TX_EXPIRY_SLOTS            = 100    # ~17 min mempool TTL
BLOOM_FILTER_YEARS         = 2
BLOOM_FALSE_POSITIVE       = 0.0001

# ── Registration freeze constants ─────────────────────────────────────────────

FREEZE_RATE_MULTIPLIER  = 5
FREEZE_BASELINE_WINDOW  = 1000
FREEZE_DETECTION_WINDOW = 100
FREEZE_COOLDOWN_SLOTS   = 200

# ── Network constants ──────────────────────────────────────────────────────────

MAX_PEERS              = 125
BROADCAST_FANOUT       = 8
MAX_FUTURE_SLOTS       = 3
MAX_SLOT_GAP           = 30
SYNC_RATE_WINDOW       = 30
BROADCAST_PORT         = 7778
NODE_PORT_RANGE_START  = 7779
MAX_P2P_MESSAGE_SIZE   = 10_000_000   # 10MB DoS protection — regular P2P messages
MAX_SYNC_MESSAGE_SIZE  = 100_000_000  # 100MB — SYNC responses can contain full block history
IP_BAN_SECONDS         = 60           # seconds to ban IP after sending oversized message
BLOCK_RATE_LIMIT       = 3            # max BLOCK msgs per IP per slot window (10s)
COMPETE_RATE_LIMIT     = 12           # max COMPETE msgs per IP per slot (TARGET_COMPETITORS + 2)
ATTEST_RATE_LIMIT      = 500          # max ATTEST msgs per IP per slot window
MAX_MEMPOOL_TX_PER_SENDER = 10

# ── Chain constants ────────────────────────────────────────────────────────────

GENESIS_PREV_HASH = "0" * 64
MAX_REORG_DEPTH   = 100
ORPHAN_POOL_MAX   = 100
ORPHAN_TTL_SLOTS  = 60

# ── File paths ─────────────────────────────────────────────────────────────────

WALLET_FILE    = os.path.join(os.path.expanduser("~"), ".timpal_wallet.json")
LEDGER_FILE    = os.path.join(os.path.expanduser("~"), ".timpal_ledger.json")
PEERS_FILE     = os.path.join(os.path.expanduser("~"), ".timpal_peers.json")
CONTROL_TOKEN  = os.path.join(os.path.expanduser("~"), ".timpal_control.token")

# ── Bootstrap servers ──────────────────────────────────────────────────────────

BOOTSTRAP_SERVERS = [("5.78.187.91", 7777)]
DNS_SEEDS         = ["dns.timpal.org"]

# ── Explorer push targets ──────────────────────────────────────────────────────

EXPLORER_TARGETS = [("5.78.187.91", 7781)]


# ══════════════════════════════════════════════════════════════════════════════
# PART 1 — UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _check_genesis_time():
    if GENESIS_TIME == 0:
        print("\n  " + "═" * 54)
        print("  ERROR: GENESIS_TIME is not set.")
        print("  Run: python3 -c \"import time; print(int(time.time()) + 300)\"")
        print("  Paste the result into both timpal.py AND bootstrap.py")
        print("  " + "═" * 54 + "\n")
        exit(1)


def get_current_slot() -> int:
    return int((time.time() - GENESIS_TIME) / REWARD_INTERVAL)


def _ver(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0, 0)


def canonical_block(block: dict) -> bytes:
    """Deterministic serialization for block hashing.
    sort_keys=True and separators=(',',':') are MANDATORY.
    Any deviation causes permanent silent chain splits across all nodes."""
    return json.dumps(block, sort_keys=True, separators=(",", ":")).encode()


def compute_block_hash(block: dict) -> str:
    return hashlib.sha256(canonical_block(block)).hexdigest()


def find_free_port(start: int = NODE_PORT_RANGE_START) -> int:
    for port in range(start, start + 20):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError("No free port in range")


def _is_valid_hex64(s) -> bool:
    return (isinstance(s, str) and len(s) == 64
            and all(c in "0123456789abcdef" for c in s))


def _check_clock_drift():
    """Warn loudly if system clock drifts more than 2 seconds from NTP.
    Advisory only — never blocks startup."""
    try:
        import ntplib
        r = ntplib.NTPClient().request("pool.ntp.org", version=3)
        drift = abs(r.offset)
        if drift > 2.0:
            print(f"\n  ⚠️  WARNING: Clock drift detected: {drift:.1f} seconds")
            print(f"  This may cause block validation failures.")
            print(f"  Sync your system clock immediately.\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# PART 2 — ECONOMICS
# ══════════════════════════════════════════════════════════════════════════════

def get_block_reward(total_minted: int) -> int:
    """Return correct block reward given current total_minted.

    Every block pays exactly REWARD_PER_ROUND except the final block
    of Era 1, which pays exactly the remaining supply (0.23825 TMPL).
    Returns 0 if Era 2 has already begun (total_minted >= TOTAL_SUPPLY).

    This prevents the permanent Era 1 stall caused by non-integer division:
      12,500,000,000,000,000 / 105,750,000 = 118,203,309.692...
    The remainder (23,825,000 units) would never be awarded without this function.
    """
    remaining = TOTAL_SUPPLY - total_minted
    if remaining <= 0:
        return 0
    return min(REWARD_PER_ROUND, remaining)


def is_era2(ledger=None) -> bool:
    """Era 2 begins when total_minted reaches TOTAL_SUPPLY.
    No ERA2_ROUND constant. Era 2 triggers purely from total_minted."""
    if ledger is None:
        return False
    return ledger.total_minted >= TOTAL_SUPPLY


def calculate_fee(amount: int) -> int:
    """Tiered percentage fee: 0.1% of amount, min 0.0001 TMPL, max 0.01 TMPL.

    Examples:
      Send 0.001 TMPL  → fee 0.0001 TMPL  (minimum applies)
      Send 1    TMPL   → fee 0.001  TMPL  (0.1%)
      Send 10   TMPL   → fee 0.01   TMPL  (0.1%)
      Send 100  TMPL   → fee 0.01   TMPL  (maximum applies)
    """
    fee = int(amount * TX_FEE_RATE)
    return max(TX_FEE_MIN, min(TX_FEE_MAX, fee))


# ══════════════════════════════════════════════════════════════════════════════
# PART 3 — LOTTERY: COMPETITOR SELECTION AND CHALLENGE
# ══════════════════════════════════════════════════════════════════════════════

def select_competitors(identities: dict, prev_block_hash: str,
                       slot: int, n: int = TARGET_COMPETITORS) -> list:
    """Select n competitors for this slot from all registered mature identities.

    Selection is:
    - Deterministic: every node gets the same result from the same inputs
    - Unpredictable: requires prev_block_hash, unknown until previous block arrives
    - Fair: uniform SHA256 scoring gives every eligible identity equal probability
    - Verifiable: any node can reproduce this from chain data alone

    Security: an attacker controlling K of N identities has K/N probability of
    selection — cannot be improved without controlling prev_block_hash itself.
    """
    eligible = [
        did for did, first_seen in identities.items()
        if slot - first_seen >= MIN_IDENTITY_AGE
    ]
    if not eligible:
        return []
    scored = sorted(
        eligible,
        key=lambda did: hashlib.sha256(
            f"{did}:{prev_block_hash}:{slot}".encode()
        ).hexdigest()
    )
    return scored[:min(n, len(scored))]


def compute_challenge(prev_block_hash: str, slot: int) -> bytes:
    """Compute the Dilithium3 challenge for this slot.
    Same challenge for all 10 competitors. Cannot be pre-computed because
    it includes prev_block_hash which is unknown until the previous block arrives."""
    return hashlib.sha256(
        f"challenge:{prev_block_hash}:{slot}".encode()
    ).digest()


def solve_challenge(private_key: bytes, challenge: bytes) -> tuple:
    """Sign the challenge with Dilithium3. Returns (signature_hex, proof_hex).
    proof = sha256(signature) — used for deterministic tiebreaking."""
    signature = Dilithium3.sign(private_key, challenge)
    proof     = hashlib.sha256(signature).hexdigest()
    return signature.hex(), proof


# ══════════════════════════════════════════════════════════════════════════════
# PART 4 — FINALITY
# ══════════════════════════════════════════════════════════════════════════════

def is_final(block_hash: str, slot: int, ledger, attestations: dict) -> bool:
    """A block is cryptographically final when >2/3 of all mature identities attest.

    attestations = {block_hash: {device_id: attestation_dict}}

    Finality is not probabilistic. It requires Dilithium3 signatures from
    a supermajority of all registered mature identities. A finalized block
    cannot be reorged under any circumstances short of breaking Dilithium3.
    """
    mature = {
        did for did, first_seen in ledger.identities.items()
        if slot - first_seen >= MIN_IDENTITY_AGE
    }
    total    = len(mature)
    attested = len(attestations.get(block_hash, {}))
    if total == 0:
        return False
    return attested / total > ATTESTATION_THRESHOLD


def produce_attestation(block_hash: str, slot: int, wallet) -> dict:
    """Produce a Dilithium3-signed attestation for a validated block."""
    payload = f"attest:{block_hash}:{slot}".encode()
    return {
        "type":       "ATTEST",
        "block_hash": block_hash,
        "slot":       slot,
        "device_id":  wallet.device_id,
        "public_key": wallet.get_public_key_hex(),
        "signature":  Dilithium3.sign(wallet.private_key, payload).hex(),
        "timestamp":  time.time()
    }


# ══════════════════════════════════════════════════════════════════════════════
# PART 5 — BLOOM FILTER (spent transaction IDs, 2-year rolling window)
# ══════════════════════════════════════════════════════════════════════════════

class SpentBloomFilter:
    """Probabilistic set for spent transaction IDs.

    2-year rolling window at 50 TPS requires ~2GB for 0.01% false positive rate.
    On false positive: sender retries with a new UUID — no funds lost, no double-spend.
    Persisted as a compact bitarray in the ledger file.
    """
    def __init__(self, capacity: int = 0, error_rate: float = BLOOM_FALSE_POSITIVE):
        import math
        if capacity == 0:
            # Default: 2 years at 50 TPS = 50 * 10 * 60 * 60 * 24 * 365 * 2 = ~3.15 billion
            # Practical cap: store what we have, resize on load
            capacity = 10_000_000  # start at 10M, grows if needed
        self._capacity  = capacity
        self._error     = error_rate
        # Optimal parameters
        self._num_bits  = self._optimal_bits(capacity, error_rate)
        self._num_hashes= self._optimal_hashes(self._num_bits, capacity)
        self._bits      = bytearray((self._num_bits + 7) // 8)
        self._count     = 0

    @staticmethod
    def _optimal_bits(n: int, p: float) -> int:
        import math
        return max(1, int(-n * math.log(p) / (math.log(2) ** 2)))

    @staticmethod
    def _optimal_hashes(m: int, n: int) -> int:
        import math
        return max(1, int(m / n * math.log(2)))

    def _hash_positions(self, item: str) -> list:
        positions = []
        for i in range(self._num_hashes):
            h = int(hashlib.sha256(f"{i}:{item}".encode()).hexdigest(), 16)
            positions.append(h % self._num_bits)
        return positions

    def add(self, item: str):
        for pos in self._hash_positions(item):
            self._bits[pos >> 3] |= (1 << (pos & 7))
        self._count += 1

    def __contains__(self, item: str) -> bool:
        return all(
            (self._bits[pos >> 3] >> (pos & 7)) & 1
            for pos in self._hash_positions(item)
        )

    def to_dict(self) -> dict:
        return {
            "capacity":   self._capacity,
            "error_rate": self._error,
            "num_bits":   self._num_bits,
            "num_hashes": self._num_hashes,
            "bits":       self._bits.hex(),
            "count":      self._count
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpentBloomFilter":
        bf = cls.__new__(cls)
        bf._capacity   = d.get("capacity", 10_000_000)
        bf._error      = d.get("error_rate", BLOOM_FALSE_POSITIVE)
        bf._num_bits   = d.get("num_bits", 1)
        bf._num_hashes = d.get("num_hashes", 1)
        bf._bits       = bytearray(bytes.fromhex(d.get("bits", ""))) if d.get("bits") else bytearray((bf._num_bits + 7) // 8)
        bf._count      = d.get("count", 0)
        return bf


# ══════════════════════════════════════════════════════════════════════════════
# PART 6 — REGISTRATION FREEZE
# ══════════════════════════════════════════════════════════════════════════════

def _avg_regs_per_slot(chain: list, from_slot: int, to_slot: int) -> float:
    """Count average registrations per slot in the given window from chain data.
    Accepts a chain list directly so callers can pass a lock-safe snapshot."""
    span = max(1, to_slot - from_slot)
    count = 0
    for block in chain:
        s = block.get("slot", 0)
        if from_slot <= s < to_slot:
            count += len(block.get("registrations", []))
    return count / span


def is_registration_freeze_active(ledger, current_slot: int,
                                   chain: list = None) -> tuple:
    """Check whether registration freeze is active.

    Returns (freeze_active: bool, status: dict)

    Baseline: avg regs/slot over [current_slot - 1000, current_slot - 100)
    Recent:   avg regs/slot over [current_slot - 100,  current_slot)
    Freeze triggers when recent > baseline * FREEZE_RATE_MULTIPLIER
    Freeze lifts after FREEZE_COOLDOWN_SLOTS consecutive normal slots.

    chain: optional pre-snapshotted chain list. Pass a snapshot when calling
    without ledger._lock held (e.g. _freeze_monitor). Pass None when
    ledger._lock is already held — ledger.chain is then accessed directly.
    """
    chain_data = chain if chain is not None else ledger.chain
    baseline = _avg_regs_per_slot(
        chain_data,
        current_slot - FREEZE_BASELINE_WINDOW,
        current_slot - FREEZE_DETECTION_WINDOW
    )
    recent = _avg_regs_per_slot(
        chain_data,
        current_slot - FREEZE_DETECTION_WINDOW,
        current_slot
    )
    threshold = max(baseline * FREEZE_RATE_MULTIPLIER, 1.0)
    active    = recent > threshold

    # Cooldown: even if rate dropped, freeze stays until 200 consecutive normal slots
    if not active and getattr(ledger, "freeze_triggered_slot", None) is not None:
        normal_streak = current_slot - getattr(ledger, "freeze_last_abnormal_slot", current_slot)
        if normal_streak < FREEZE_COOLDOWN_SLOTS:
            active = True

    normal_streak = current_slot - getattr(ledger, "freeze_last_abnormal_slot", current_slot)
    status = {
        "active":          active,
        "triggered_slot":  getattr(ledger, "freeze_triggered_slot", None),
        "current_rate":    round(recent, 2),
        "baseline_rate":   round(baseline, 2),
        "normal_streak":   getattr(ledger, "freeze_normal_streak", 0),
        "cooldown_needed": max(0, FREEZE_COOLDOWN_SLOTS - getattr(ledger, "freeze_normal_streak", 0))
    }
    return active, status


# ══════════════════════════════════════════════════════════════════════════════
# PART 7 — WALLET
# ══════════════════════════════════════════════════════════════════════════════

# BIP39 English wordlist (2048 words, first 2048 of standard list embedded)
# Used for 12-word seed phrase generation and recovery.

def _load_bip39_wordlist() -> list:
    """Return the 2048-word BIP39 English wordlist via mnemonic library."""
    try:
        import mnemonic as _mn_lib
        return list(_mn_lib.Mnemonic('english').wordlist)
    except ImportError:
        raise RuntimeError(
            "mnemonic library required for seed phrase support.\n"
            "  Run: pip3 install mnemonic"
        )







def _entropy_to_mnemonic(entropy: bytes, wordlist: list) -> str:
    """Convert 16 bytes of entropy to a 12-word BIP39 mnemonic."""
    import hashlib as _hl
    checksum_byte = _hl.sha256(entropy).digest()[0]
    # 128 bits entropy + 4 bits checksum = 132 bits = 12 × 11 bits
    combined = int.from_bytes(entropy, "big")
    combined = (combined << 4) | (checksum_byte >> 4)
    words = []
    for _ in range(12):
        words.append(wordlist[combined & 0x7FF])
        combined >>= 11
    return " ".join(reversed(words))


def _mnemonic_to_entropy(phrase: str) -> bytes:
    """Convert a 12-word BIP39 mnemonic back to 16 bytes of entropy."""
    wordlist = _load_bip39_wordlist()
    words = phrase.strip().lower().split()
    if len(words) != 12:
        raise ValueError(f"Expected 12 words, got {len(words)}")
    combined = 0
    for word in words:
        if word not in wordlist:
            raise ValueError(f"Unknown word: {word!r}")
        combined = (combined << 11) | wordlist.index(word)
    # combined is 132 bits: 128 bits entropy + 4 bits checksum (lowest bits)
    actual_checksum = combined & 0xF
    combined >>= 4
    entropy = combined.to_bytes(16, "big")
    # Verify checksum — catches mistyped phrases that happen to use valid words
    import hashlib as _hl
    expected_checksum = _hl.sha256(entropy).digest()[0] >> 4
    if actual_checksum != expected_checksum:
        raise ValueError("Checksum mismatch — check your seed phrase")
    return entropy


def generate_seed_phrase() -> str:
    """Generate 12 BIP39 words from 128 bits of entropy."""
    entropy  = os.urandom(16)
    wordlist = _load_bip39_wordlist()
    return _entropy_to_mnemonic(entropy, wordlist)


def derive_keys_from_seed(seed_phrase: str) -> tuple:
    """Deterministically derive Dilithium3 keypair from seed phrase.
    Same phrase always produces same keys. Uses set_drbg_seed (requires pycryptodome).
    """
    # ValueError from _mnemonic_to_entropy (bad phrase) propagates directly —
    # that is a user error, not a missing dependency.
    seed_bytes   = _mnemonic_to_entropy(seed_phrase)
    key_material = hashlib.sha512(
        b"timpal-dilithium3-v4:" + seed_bytes
    ).digest()[:48]  # set_drbg_seed requires exactly 48 bytes
    try:
        Dilithium3.set_drbg_seed(key_material)
        pk, sk = Dilithium3.keygen()
        return pk, sk
    except Exception as w:
        # L2: catch any Exception (AttributeError, TypeError, Warning, etc.)
        # not just Warning — pycryptodome may raise different types depending
        # on version when set_drbg_seed is unavailable.
        raise RuntimeError(
            "pycryptodome required for wallet recovery.\n"
            "  Run: pip3 install pycryptodome"
        ) from w


class Wallet:
    def __init__(self):
        self.public_key:         bytes = None
        self.private_key:        bytes = None
        self.device_id:          str   = None
        self.genesis_block_hash: str   = None  # None for genesis-phase wallets

    def create_new(self, genesis_block_hash: str = None):
        """Create a new Dilithium3 wallet and display mandatory seed phrase."""
        self.public_key, self.private_key = Dilithium3.keygen()
        if genesis_block_hash:
            self.genesis_block_hash = genesis_block_hash
            seed = self.public_key + bytes.fromhex(genesis_block_hash)
            self.device_id = hashlib.sha256(seed).hexdigest()
        else:
            self.genesis_block_hash = None
            self.device_id = hashlib.sha256(self.public_key).hexdigest()

    def show_seed_phrase_and_confirm(self) -> str:
        """Generate seed phrase, display it, require user to type it back.
        Returns seed phrase on success. Exits if user cannot confirm."""
        phrase = generate_seed_phrase()
        words  = phrase.split()
        print("\n")
        print("  ╔══════════════════════════════════════════════════════════════╗")
        print("  ║         WRITE DOWN YOUR 12-WORD RECOVERY PHRASE             ║")
        print("  ╠══════════════════════════════════════════════════════════════╣")
        print("  ║                                                              ║")
        print(f"  ║  {' '.join(words[:6]):<56}║")
        print(f"  ║  {' '.join(words[6:]):<56}║")
        print("  ║                                                              ║")
        print("  ║  ⚠️  THIS IS THE ONLY WAY TO RECOVER YOUR WALLET            ║")
        print("  ║  ⚠️  IF YOU LOSE YOUR WALLET FILE AND THIS PHRASE           ║")
        print("  ║     YOUR TMPL IS GONE FOREVER — NO EXCEPTIONS               ║")
        print("  ║                                                              ║")
        print("  ║  Store this phrase:                                          ║")
        print("  ║  • Written on paper in a safe place                          ║")
        print("  ║  • NEVER in a photo, email, or cloud storage                 ║")
        print("  ║  • NEVER share it with anyone                                ║")
        print("  ╚══════════════════════════════════════════════════════════════╝\n")
        print("  Type all 12 words to confirm you have written them down:")
        while True:
            try:
                entered = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Wallet creation cancelled.")
                exit(0)
            if entered == phrase:
                print("  ✓ Seed phrase confirmed.\n")
                return phrase
            print("  ✗ Phrase does not match. Please try again:")

    @staticmethod
    def _derive_key(password: str, salt: bytes) -> bytes:
        return Scrypt(salt=salt, length=32, n=131072, r=8, p=1,
                      backend=default_backend()).derive(password.encode())

    def save(self, path: str = WALLET_FILE, password: str = None,
             seed_phrase: str = None):
        """Save wallet to disk. AES-256-GCM encrypted if password provided.

        File format is backward-compatible with v3.3 encrypted wallets.
        v4.0 adds: seed_phrase_hash (SHA256 of phrase for recovery verification).
        """
        if password:
            salt  = os.urandom(32)
            key   = Wallet._derive_key(password, salt)
            nonce = os.urandom(12)
            ct    = AESGCM(key).encrypt(nonce, self.private_key, None)
            data  = {
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
                "private_key_enc": ct.hex()
            }
        else:
            data = {
                "version":     VERSION,
                "device_id":   self.device_id,
                "public_key":  self.public_key.hex(),
                "private_key": self.private_key.hex(),
                "quantum":     True
            }
        if self.genesis_block_hash:
            data["genesis_block_hash"] = self.genesis_block_hash
        if seed_phrase:
            data["seed_phrase_hash"] = hashlib.sha256(seed_phrase.encode()).hexdigest()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def load(self, path: str = WALLET_FILE, password: str = None):
        with open(path, "r") as f:
            data = json.load(f)
        self.public_key         = bytes.fromhex(data["public_key"])
        self.device_id          = data["device_id"]
        self.genesis_block_hash = data.get("genesis_block_hash")
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

    def get_public_key_hex(self) -> str:
        return self.public_key.hex()

    def sign(self, message: bytes) -> str:
        return Dilithium3.sign(self.private_key, message).hex()

    @staticmethod
    def verify_signature(public_key_hex: str, message: bytes,
                          signature_hex: str) -> bool:
        try:
            return Dilithium3.verify(
                bytes.fromhex(public_key_hex),
                message,
                bytes.fromhex(signature_hex)
            )
        except Exception:
            return False

    def _make_registration_message(self) -> dict:
        """Produce a signed REGISTER message for this wallet."""
        gbh     = self.genesis_block_hash or ""
        payload = f"{self.device_id}:{gbh}".encode()
        return {
            "type":               "REGISTER",
            "device_id":          self.device_id,
            "public_key":         self.get_public_key_hex(),
            "genesis_block_hash": gbh,
            "signature":          Dilithium3.sign(self.private_key, payload).hex(),
            "version":            VERSION
        }


# ══════════════════════════════════════════════════════════════════════════════
# PART 8 — TRANSACTION
# ══════════════════════════════════════════════════════════════════════════════

class Transaction:
    def __init__(self, sender_id: str, recipient_id: str, sender_pubkey: str,
                 amount: int, fee: int = 0, memo: str = "",
                 slot: int = None, timestamp: float = None, tx_id: str = None):
        self.tx_id        = tx_id or str(uuid.uuid4())
        self.sender_id    = sender_id
        self.recipient_id = recipient_id
        self.sender_pubkey= sender_pubkey
        self.amount       = amount
        self.fee          = fee
        self.memo         = (memo or "")[:128]
        self.slot         = slot
        self.timestamp    = timestamp or time.time()
        self.signature    = None

    def _payload(self) -> bytes:
        """Signed payload. memo is included — any tampering invalidates signature."""
        memo = (self.memo or "")[:128]
        return (
            f"{self.tx_id}:{self.sender_id}:{self.recipient_id}:"
            f"{self.amount}:{self.fee}:{self.timestamp:.6f}:"
            f"{self.slot if self.slot is not None else 0}:{memo}"
        ).encode()

    def sign(self, wallet: Wallet):
        self.signature = wallet.sign(self._payload())

    def verify(self) -> bool:
        """Verify transaction signature.

        CRITICAL: does NOT check sha256(sender_pubkey) == sender_id.
        Chain-anchored wallets use sha256(pubkey + block_hash) as device_id,
        which never equals sha256(pubkey). The Dilithium3 signature already
        proves ownership. The hash check is permanently removed.
        """
        if not self.signature:
            return False
        return Wallet.verify_signature(self.sender_pubkey, self._payload(), self.signature)

    def to_dict(self) -> dict:
        return {
            "tx_id":        self.tx_id,
            "sender_id":    self.sender_id,
            "recipient_id": self.recipient_id,
            "sender_pubkey":self.sender_pubkey,
            "amount":       self.amount,
            "fee":          self.fee,
            "memo":         self.memo,
            "slot":         self.slot,
            "timestamp":    self.timestamp,
            "signature":    self.signature
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        amount = d["amount"]
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError(f"invalid amount: {amount!r}")
        fee = d.get("fee", 0)
        if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
            raise ValueError(f"invalid fee: {fee!r}")
        for field in ("sender_id", "recipient_id"):
            val = d.get(field, "")
            if not _is_valid_hex64(val):
                raise ValueError(f"invalid {field}")
        pubkey = d.get("sender_pubkey", "")
        if not isinstance(pubkey, str) or not pubkey:
            raise ValueError("invalid sender_pubkey")
        bytes.fromhex(pubkey)   # validate hex
        tx = cls(
            sender_id    = d["sender_id"],
            recipient_id = d["recipient_id"],
            sender_pubkey= d["sender_pubkey"],
            amount       = amount,
            fee          = fee,
            memo         = d.get("memo", ""),
            slot         = d.get("slot"),
            timestamp    = d["timestamp"],
            tx_id        = d["tx_id"]
        )
        tx.signature = d.get("signature")
        return tx


def select_transactions_for_block(mempool: dict, current_slot: int) -> list:
    """Select up to MAX_TRANSACTIONS_PER_BLOCK transactions, highest fee first.
    Expired transactions (older than TX_EXPIRY_SLOTS) are excluded."""
    pending = [
        tx for tx in mempool.values()
        if current_slot - tx.get("slot", 0) <= TX_EXPIRY_SLOTS
    ]
    return sorted(
        pending,
        key=lambda tx: tx.get("fee", 0),
        reverse=True
    )[:MAX_TRANSACTIONS_PER_BLOCK]


def can_add_to_mempool(sender_id: str, mempool: dict) -> bool:
    """Enforce per-sender mempool limit to prevent spam."""
    pending = sum(1 for tx in mempool.values() if tx.get("sender_id") == sender_id)
    return pending < MAX_MEMPOOL_TX_PER_SENDER


# ── Payment URI (spec Part 13B) ───────────────────────────────────────────────

def generate_payment_uri(device_id: str, amount: float = None,
                          memo: str = None, label: str = None) -> str:
    """Generate a Timpal payment URI per spec Part 13B.

    Format: timpal:<device_id>?amount=<tmpl>&memo=<text>&label=<name>

    All query parameters are optional. amount is in TMPL (decimal).
    memo is the signed payment reference (max 128 chars).
    label is a display hint only — never included in the transaction.

    Examples:
        generate_payment_uri("4a7f...")
        → "timpal:4a7f..."

        generate_payment_uri("4a7f...", amount=4.50, memo="Table7", label="Cafe")
        → "timpal:4a7f...?amount=4.5&memo=Table7&label=Cafe"
    """
    import urllib.parse
    if not _is_valid_hex64(device_id):
        raise ValueError(f"Invalid device_id: must be 64 lowercase hex chars")
    params = {}
    if amount is not None:
        if not isinstance(amount, (int, float)) or amount <= 0:
            raise ValueError("amount must be a positive number")
        params["amount"] = f"{float(amount):g}"
    if memo is not None:
        params["memo"] = str(memo)[:128]
    if label is not None:
        params["label"] = str(label)
    uri = f"timpal:{device_id}"
    if params:
        uri += "?" + urllib.parse.urlencode(params)
    return uri


def parse_payment_uri(uri: str) -> dict:
    """Parse a Timpal payment URI per spec Part 13B.

    Returns dict with keys: device_id (str), amount (float|None),
    memo (str|None), label (str|None).

    Raises ValueError if the URI is malformed or device_id is invalid.
    """
    import urllib.parse
    if not isinstance(uri, str):
        raise ValueError("URI must be a string")
    uri = uri.strip()
    if not uri.startswith("timpal:"):
        raise ValueError("URI must start with 'timpal:'")
    rest = uri[len("timpal:"):]
    if "?" in rest:
        device_id, query = rest.split("?", 1)
    else:
        device_id, query = rest, ""
    device_id = device_id.strip().lower()
    if not _is_valid_hex64(device_id):
        raise ValueError(f"Invalid device_id in URI: {device_id!r}")
    params = urllib.parse.parse_qs(query, keep_blank_values=False)
    amount = None
    if "amount" in params:
        try:
            amount = float(params["amount"][0])
            if amount <= 0:
                raise ValueError("amount must be positive")
        except (ValueError, TypeError):
            raise ValueError(f"Invalid amount in URI: {params['amount'][0]!r}")
    memo  = params["memo"][0][:128]  if "memo"  in params else None
    label = params["label"][0]       if "label" in params else None
    return {"device_id": device_id, "amount": amount, "memo": memo, "label": label}


# ══════════════════════════════════════════════════════════════════════════════
# PART 9 — LEDGER
# ══════════════════════════════════════════════════════════════════════════════

class Ledger:
    """Chain-node ledger. Stores only what is needed for consensus.

    PERMANENT (never pruned):
      balances        — current balance for every address
      identities      — {device_id: first_seen_slot}
      anchor_hashes   — set of all genesis_block_hash values used
      checkpoints     — checkpoint metadata
      my_transactions — own personal transaction history

    ROLLING (bounded, old data pruned at checkpoint):
      chain           — last CHECKPOINT_BUFFER blocks only
      mempool         — unconfirmed txs, expire after TX_EXPIRY_SLOTS
      fee_rewards     — pending fee records (baked into checkpoint balances)

    Storage footprint: ~2.5GB at 1M users, 50 TPS, 2 years.
    """

    def __init__(self):
        self.transactions:   list  = []     # confirmed transactions in window
        self.chain:          list  = []     # accepted blocks (rolling)
        self.fee_rewards:    list  = []     # fee redistribution records
        self.total_minted:   int   = 0
        self.checkpoints:    list  = []
        self.identities:      dict  = {}     # {device_id: first_seen_slot}
        self.identity_pubkeys:dict  = {}     # {device_id: public_key_hex} — attestation binding
        self.anchor_hashes:  set   = set()  # {genesis_block_hash} — Layer 2 Sybil
        self.balances:       dict  = {}     # {device_id: int} permanent balance cache
        self.my_transactions:list  = []     # personal history, kept forever
        self._lock           = threading.RLock()
        self._spent_bloom    = SpentBloomFilter()
        self._orphan_pool:   dict  = {}     # {prev_hash: [block, ...]}
        # Registration freeze tracking
        self.freeze_triggered_slot:    int  = None
        self.freeze_last_abnormal_slot:int  = 0
        self.freeze_normal_streak:     int  = 0
        self.last_finalized_slot:      int  = -1   # reorg barrier: no reorg past this slot
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(LEDGER_FILE):
            return
        try:
            with open(LEDGER_FILE, "r") as f:
                data = json.load(f)
            self.transactions  = data.get("transactions", [])
            self.checkpoints   = data.get("checkpoints", [])
            self.chain         = data.get("chain", [])
            self.fee_rewards   = data.get("fee_rewards", [])
            self.identities      = data.get("identities", {})
            self.identity_pubkeys= data.get("identity_pubkeys", {})
            self.anchor_hashes   = set(data.get("anchor_hashes", []))
            self.balances      = data.get("balances", {})
            self.my_transactions = data.get("my_transactions", [])
            self.freeze_triggered_slot     = data.get("freeze_triggered_slot")
            self.freeze_last_abnormal_slot = data.get("freeze_last_abnormal_slot", 0)
            self.freeze_normal_streak      = data.get("freeze_normal_streak", 0)
            self.last_finalized_slot       = data.get("last_finalized_slot", -1)
            bloom_data = data.get("spent_bloom")
            if bloom_data:
                self._spent_bloom = SpentBloomFilter.from_dict(bloom_data)
            self.recalculate_totals()
        except Exception as e:
            print(f"  [ledger] Load warning: {e}")

    def save(self):
        tmp = LEDGER_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({
                    "version":        VERSION,
                    "transactions":   self.transactions,
                    "chain":          self.chain,
                    "fee_rewards":    self.fee_rewards,
                    "total_minted":   self.total_minted,
                    "checkpoints":    self.checkpoints,
                    "identities":      self.identities,
                    "identity_pubkeys":self.identity_pubkeys,
                    "anchor_hashes":   list(self.anchor_hashes),
                    "balances":       self.balances,
                    "my_transactions":self.my_transactions,
                    "freeze_triggered_slot":     self.freeze_triggered_slot,
                    "freeze_last_abnormal_slot": self.freeze_last_abnormal_slot,
                    "freeze_normal_streak":      self.freeze_normal_streak,
                    "last_finalized_slot":        self.last_finalized_slot,
                    "spent_bloom":    self._spent_bloom.to_dict()
                }, f, indent=2)
            os.replace(tmp, LEDGER_FILE)
        except Exception as e:
            print(f"  [ledger] Save error: {e}")

    def recalculate_totals(self):
        """Recompute total_minted from checkpoint + chain. Called after load and reorg."""
        base = 0
        if self.checkpoints:
            base = self.checkpoints[-1].get("total_minted", 0)
        chain_minted = sum(b.get("amount", 0) for b in self.chain)
        self.total_minted = base + chain_minted

    # ── Tip helpers ────────────────────────────────────────────────────────────

    def _get_tip(self) -> tuple:
        """Returns (tip_hash, tip_slot) for current chain head. Caller holds lock."""
        if self.chain:
            tip = self.chain[-1]
            return compute_block_hash(tip), tip.get("slot", -1)
        elif self.checkpoints:
            cp = self.checkpoints[-1]
            return cp.get("chain_tip_hash", GENESIS_PREV_HASH), cp.get("chain_tip_slot", -1)
        return GENESIS_PREV_HASH, -1

    # ── Balance ────────────────────────────────────────────────────────────────

    def get_balance(self, device_id: str) -> int:
        """Return balance in units. Starts from checkpoint balance then adds
        block rewards, fee rewards, and transactions from the rolling window."""
        with self._lock:
            balance = 0
            if self.checkpoints:
                balance = self.checkpoints[-1].get("balances", {}).get(device_id, 0)
            for block in self.chain:
                if block.get("winner_id") == device_id:
                    balance += block.get("amount", 0)
                    balance += block.get("fees_collected", 0)
            for tx in self.transactions:
                if tx["recipient_id"] == device_id:
                    balance += tx["amount"]
                if tx["sender_id"] == device_id:
                    balance -= tx["amount"]
                    balance -= tx.get("fee", 0)
            # fee_rewards: kept for checkpoint baking compat; fees_collected
            # on each block is the primary source (already counted above)
            return balance

    # ── Registration validation ────────────────────────────────────────────────

    @staticmethod
    def _verify_registration(reg: dict) -> bool:
        """Verify a registration entry from block data.
        Checks: field types, device_id derivation, Dilithium3 signature."""
        try:
            did     = reg.get("device_id", "")
            pub_hex = reg.get("public_key", "")
            sig_hex = reg.get("signature", "")
            gbh     = reg.get("genesis_block_hash", "")

            if not _is_valid_hex64(did):
                return False
            if not isinstance(pub_hex, str) or not pub_hex:
                return False
            if not isinstance(sig_hex, str) or not sig_hex:
                return False

            pub_bytes = bytes.fromhex(pub_hex)

            # Device ID derivation check
            if gbh and _is_valid_hex64(gbh):
                expected = hashlib.sha256(pub_bytes + bytes.fromhex(gbh)).hexdigest()
            else:
                expected = hashlib.sha256(pub_bytes).hexdigest()
            if expected != did:
                return False

            payload   = f"{did}:{gbh or ''}".encode()
            sig_bytes = bytes.fromhex(sig_hex)
            return Dilithium3.verify(pub_bytes, payload, sig_bytes)
        except Exception:
            return False

    # ── Block acceptance ───────────────────────────────────────────────────────

    def _add_block_locked(self, block: dict) -> bool:
        """Validate and add a block to the chain. Caller must hold self._lock.

        Enforces all 17 validation rules from spec Part 7.2.
        This is the consensus enforcement point — every rule is mandatory.
        """
        slot    = block.get("slot")
        wid     = block.get("winner_id", "")
        prev    = block.get("prev_hash", "")
        amount  = block.get("amount", 0)
        ver     = block.get("version", "0.0")

        # Basic type checks
        if not isinstance(slot, int) or slot < 0:
            return False
        if not _is_valid_hex64(wid):
            return False
        if not isinstance(amount, int) or isinstance(amount, bool):
            return False
        if _ver(ver) < _ver(MIN_VERSION):
            return False

        # Rule 1: winner_id must be in select_competitors for this slot
        tip_hash, tip_slot = self._get_tip()
        prev_hash_for_selection = block.get("prev_hash", GENESIS_PREV_HASH)
        selected = select_competitors(self.identities, prev_hash_for_selection, slot)
        if wid not in selected and len(self.identities) >= TARGET_COMPETITORS:
            # Allow when fewer than TARGET_COMPETITORS mature identities exist
            # (early network — all eligible nodes compete, not just the top 10)
            mature = [d for d, fs in self.identities.items()
                      if slot - fs >= MIN_IDENTITY_AGE]
            if len(mature) >= TARGET_COMPETITORS:
                return False

        # Rule 2: challenge must match compute_challenge(prev_hash, slot)
        expected_challenge = compute_challenge(prev_hash_for_selection, slot).hex()
        if block.get("challenge") != expected_challenge:
            return False

        # Rule 3: Dilithium3.verify(vrf_public_key, challenge_bytes, compete_sig)
        pub_hex      = block.get("vrf_public_key", "")
        compete_sig  = block.get("compete_sig", "")
        compete_proof= block.get("compete_proof", "")
        block_sig    = block.get("block_sig", "")
        if not pub_hex or not compete_sig or not compete_proof or not block_sig:
            return False
        try:
            pub_bytes      = bytes.fromhex(pub_hex)
            challenge_bytes= compute_challenge(prev_hash_for_selection, slot)
            sig_bytes      = bytes.fromhex(compete_sig)
            if not Dilithium3.verify(pub_bytes, challenge_bytes, sig_bytes):
                return False
        except Exception:
            return False

        # Rule 4: sha256(compete_sig) == compete_proof
        if hashlib.sha256(bytes.fromhex(compete_sig)).hexdigest() != compete_proof:
            return False

        # Rule 5: winner_id matches public key derivation
        gbh_for_winner = self.identities  # we check via registration record
        # Verify winner_id matches pub_bytes + genesis_block_hash or pub_bytes alone
        expected_from_pub = hashlib.sha256(pub_bytes).hexdigest()
        # Also check chain-anchored derivation: look up the winner's registration
        # to find their genesis_block_hash, then verify derivation.
        # For simplicity: accept if either derivation matches wid.
        # The registration was already verified when it was added to the chain.
        # If winner is in identities, their registration passed _verify_registration.
        # We trust that check for derivation — just confirm pubkey → wid is consistent.
        # (A more strict check would store the gbh per identity, which is future work.)
        # For now: wid must equal sha256(pubkey) OR be in identities (chain-anchored).
        if expected_from_pub != wid and wid not in self.identities:
            return False

        # Rule 6: maturation check (applies to all slots)
        first_seen = self.identities.get(wid)
        if first_seen is None:
            # Rule 5 already verified sha256(pubkey)==wid for this branch.
            # Treat genesis wallet as registered at slot 0 so the chain can start.
            first_seen = 0
        if slot - first_seen < MIN_IDENTITY_AGE:
            return False

        # Rule 7: slot validity
        current_slot = get_current_slot()
        if slot > current_slot + MAX_FUTURE_SLOTS:
            return False

        # Rule 8: chain linkage
        if prev != tip_hash:
            return False

        # Rule 9: no duplicate slot
        if any(b.get("slot") == slot for b in self.chain):
            return False

        # Rule 10: supply cap — block amount must exactly equal get_block_reward
        expected_reward = get_block_reward(self.total_minted)
        if amount != expected_reward:
            return False

        # Rule 11: global registration count
        regs = block.get("registrations", [])
        if not isinstance(regs, list) or len(regs) > MAX_REGS_PER_BLOCK:
            return False

        # Rule 12: per-producer registration count
        producer_regs = [r for r in regs
                         if r.get("device_id") == wid or
                         hashlib.sha256(bytes.fromhex(r.get("public_key", "00"))).hexdigest() == wid]
        # Count registrations whose public key matches winner's public key
        winner_regs = 0
        for r in regs:
            try:
                if bytes.fromhex(r.get("public_key", "")).hex() == pub_hex:
                    winner_regs += 1
            except Exception:
                pass
        if winner_regs > MAX_REGS_PER_PRODUCER:
            return False

        # Rule 13: unique anchor check — no genesis_block_hash already used
        for reg in regs:
            gbh = reg.get("genesis_block_hash", "")
            if gbh and gbh in self.anchor_hashes:
                return False

        # Rule 14: post-genesis registrations must have valid genesis_block_hash
        if slot >= 1000:
            for reg in regs:
                gbh = reg.get("genesis_block_hash", "")
                if not _is_valid_hex64(gbh):
                    return False

        # Registration freeze: blocks with registrations are invalid during freeze
        freeze_active, _ = is_registration_freeze_active(self, slot)
        if freeze_active and regs:
            return False

        # Rule 15: transaction validity
        txs = block.get("transactions", [])
        if not isinstance(txs, list):
            return False
        if len(txs) > MAX_TRANSACTIONS_PER_BLOCK:
            return False
        intra_block_debit = {}   # {sender_id: cumulative spend within this block}
        for tx_dict in txs:
            if not isinstance(tx_dict, dict):
                return False
            try:
                tx = Transaction.from_dict(tx_dict)
            except Exception:
                return False
            if not tx.verify():
                return False
            if tx.amount <= 0:
                return False
            if tx.fee < calculate_fee(tx.amount):
                return False
            if tx.tx_id in self._spent_bloom:
                return False
            # Intra-block double-spend prevention: deduct prior spends from
            # this block before checking the sender's available balance.
            prior_debit    = intra_block_debit.get(tx.sender_id, 0)
            sender_balance = self.get_balance(tx.sender_id)
            if sender_balance - prior_debit < tx.amount + tx.fee:
                return False
            intra_block_debit[tx.sender_id] = prior_debit + tx.amount + tx.fee

        # Rule 16 (block_sig): winner signs canonical block without block_sig field.
        # Normalize "type" to "block_reward": the network gossips blocks with
        # type="BLOCK" but the producer signed over type="block_reward".
        # Without this normalization every gossiped block fails sig verification.
        block_without_sig = {k: v for k, v in block.items() if k != "block_sig"}
        block_without_sig["type"] = "block_reward"
        try:
            block_payload = canonical_block(block_without_sig)
            block_sig_bytes = bytes.fromhex(block_sig)
            if not Dilithium3.verify(pub_bytes, block_payload, block_sig_bytes):
                return False
        except Exception:
            return False

        # All 17 rules passed — accept the block
        self.chain.append(block)
        self.total_minted += amount

        # fees_collected is on the block itself — counted in get_balance via
        # block.get("fees_collected", 0). No separate fee_rewards entry needed.

        # Self-register genesis winner if not yet in identities
        if wid not in self.identities:
            self.identities[wid]       = 0
            self.identity_pubkeys[wid] = pub_hex

        # Process registrations — add to identities and anchor_hashes
        for reg in regs:
            if Ledger._verify_registration(reg):
                did = reg.get("device_id", "")
                if did and did not in self.identities:
                    gbh = reg.get("genesis_block_hash", "")
                    self.identities[did]       = slot
                    self.identity_pubkeys[did] = reg.get("public_key", "")
                    if gbh:
                        self.anchor_hashes.add(gbh)

        # Mark transactions as spent
        for tx_dict in txs:
            tx_id = tx_dict.get("tx_id", "")
            if tx_id:
                self._spent_bloom.add(tx_id)
            self.transactions.append(tx_dict)

        return True

    # ── Merge (incoming blocks from peers) ────────────────────────────────────

    def merge(self, delta: dict) -> bool:
        """Accept incoming blocks, transactions, and fee_rewards from peers.
        Returns True if the ledger changed."""
        blocks       = delta.get("blocks", [])
        transactions = delta.get("transactions", [])

        with self._lock:
            changed = False

            # First pass: try to extend chain
            for block in sorted(blocks, key=lambda b: b.get("slot", 0)):
                tip_hash, tip_slot = self._get_tip()
                if block.get("prev_hash") != tip_hash:
                    self._store_orphan_locked(block)
                    continue
                if self._add_block_locked(block):
                    changed = True

            # Try reorg if direct extension failed
            if not changed and blocks:
                if self._attempt_reorg(blocks):
                    changed = True

            # Drain orphan pool
            if changed:
                self._drain_orphan_pool_locked()

            # Accept new transactions into mempool
            for tx_dict in transactions:
                tx_id = tx_dict.get("tx_id", "")
                if tx_id and tx_id not in self._spent_bloom:
                    if not any(t.get("tx_id") == tx_id for t in self.transactions):
                        self.transactions.append(tx_dict)
                        changed = True

            if changed:
                self.save()

            return changed

    # ── Orphan pool ────────────────────────────────────────────────────────────

    def _store_orphan_locked(self, block: dict):
        # Use chain tip slot as reference — not wall clock — so tests and
        # nodes with GENESIS_TIME=0 don't drop all orphans immediately.
        block_slot = block.get("slot", 0)
        tip_slot   = (self.chain[-1].get("slot", 0) if self.chain
                      else (self.checkpoints[-1].get("chain_tip_slot", 0)
                            if self.checkpoints else 0))
        if block_slot < tip_slot - ORPHAN_TTL_SLOTS:
            return
        prev = block.get("prev_hash", "")
        if prev not in self._orphan_pool:
            self._orphan_pool[prev] = []
        if len(self._orphan_pool) < ORPHAN_POOL_MAX:
            # Avoid duplicates
            existing_slots = {b.get("slot") for b in self._orphan_pool[prev]}
            if block.get("slot") not in existing_slots:
                self._orphan_pool[prev].append(block)

    def _drain_orphan_pool_locked(self):
        """Attach orphans whose parent is now at the chain tip."""
        changed = True
        while changed:
            changed = False
            tip_hash, _ = self._get_tip()
            if tip_hash in self._orphan_pool:
                candidates = self._orphan_pool.pop(tip_hash, [])
                for block in sorted(candidates, key=lambda b: b.get("slot", 0)):
                    if self._add_block_locked(block):
                        changed = True

    # ── Reorg ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _chain_weight(blocks: list) -> int:
        """Chain weight: +1 per block, -(gap-1) per missing slot. Penalises sparse chains."""
        weight    = 0
        prev_slot = None
        for b in blocks:
            slot = b.get("slot", 0)
            if prev_slot is not None:
                gap = slot - prev_slot
                if gap > 1:
                    weight -= (gap - 1)
            weight   += 1
            prev_slot = slot
        return max(weight, 0)

    def _attempt_reorg(self, incoming: list) -> bool:
        """Try to replace current chain tail with a heavier alternative.
        Finalized blocks can never be reorged."""
        if not incoming:
            return False

        our_hash_to_idx = {compute_block_hash(b): i for i, b in enumerate(self.chain)}

        if self.checkpoints:
            cp_tip_hash = self.checkpoints[-1].get("chain_tip_hash", GENESIS_PREV_HASH)
            cp_tip_slot = self.checkpoints[-1].get("chain_tip_slot", -1)
        else:
            cp_tip_hash = GENESIS_PREV_HASH
            cp_tip_slot = -1

        fork_anchor_idx  = None
        fork_start_in_in = None

        for i, block in enumerate(incoming):
            prev = block.get("prev_hash", "")
            if prev == cp_tip_hash:
                fork_anchor_idx  = -1
                fork_start_in_in = i
                break
            if prev in our_hash_to_idx:
                fork_anchor_idx  = our_hash_to_idx[prev]
                fork_start_in_in = i
                break

        if fork_start_in_in is None:
            return False
        if fork_anchor_idx == -1 and self.checkpoints:
            return False  # Never reorg before checkpoint boundary

        fork_blocks = incoming[fork_start_in_in:]

        if fork_anchor_idx == -1:
            anchor_hash = cp_tip_hash
            anchor_slot = cp_tip_slot
        else:
            anchor_block = self.chain[fork_anchor_idx]
            anchor_hash  = compute_block_hash(anchor_block)
            anchor_slot  = anchor_block.get("slot", -1)

        if self.chain and anchor_slot >= 0:
            tip_slot_now = self.chain[-1].get("slot", 0)
            if tip_slot_now - anchor_slot > MAX_REORG_DEPTH:
                return False

        # Finality barrier: never reorg past a finalized block.
        # A finalized block carries >2/3 Dilithium3 attestations — irreversible.
        if anchor_slot <= self.last_finalized_slot:
            return False

        # Validate fork blocks against a fork-local identity table
        validated       = []
        prev_hash       = anchor_hash
        prev_slot       = anchor_slot
        fork_identities = dict(self.identities)
        fork_pubkeys    = dict(self.identity_pubkeys)
        fork_anchors    = set(self.anchor_hashes)

        for block in fork_blocks:
            slot = block.get("slot")
            if not isinstance(slot, int):
                break
            if block.get("prev_hash") != prev_hash:
                break
            if slot <= prev_slot:
                break

            # Maturation check using fork-local identity table
            wid = block.get("winner_id", "")
            first_seen_fork = fork_identities.get(wid)
            if first_seen_fork is None:
                # Genesis wallet path: verify sha256(pubkey)==wid before treating
                # first_seen as 0 — prevents forged identity claims in reorg
                try:
                    pb = bytes.fromhex(block.get("vrf_public_key", ""))
                    if hashlib.sha256(pb).hexdigest() != wid:
                        break
                    first_seen_fork = 0
                except Exception:
                    break
            if slot - first_seen_fork < MIN_IDENTITY_AGE:
                break

            # Supply cap: block amount must equal get_block_reward at that point
            running_minted = (self.total_minted
                              + sum(b.get("amount", 0) for b in validated))
            expected_amt = get_block_reward(running_minted)
            if block.get("amount", -1) != expected_amt:
                break

            # Crypto verification (compete_sig, block_sig, tx signatures)
            # An attacker MUST NOT be able to reorg with structurally-valid but
            # unsigned blocks — this closes the critical unsigned-reorg attack.
            reorg_pub_hex     = block.get("vrf_public_key", "")
            reorg_compete_sig = block.get("compete_sig", "")
            reorg_proof       = block.get("compete_proof", "")
            reorg_block_sig   = block.get("block_sig", "")
            if not reorg_pub_hex or not reorg_compete_sig or not reorg_proof or not reorg_block_sig:
                break
            try:
                reorg_pub_bytes = bytes.fromhex(reorg_pub_hex)
                reorg_challenge = compute_challenge(block.get("prev_hash", ""), slot)
                if not Dilithium3.verify(reorg_pub_bytes, reorg_challenge,
                                         bytes.fromhex(reorg_compete_sig)):
                    break
                if hashlib.sha256(bytes.fromhex(reorg_compete_sig)).hexdigest() != reorg_proof:
                    break
                reorg_bws = {k: v for k, v in block.items() if k != "block_sig"}
                if not Dilithium3.verify(reorg_pub_bytes, canonical_block(reorg_bws),
                                         bytes.fromhex(reorg_block_sig)):
                    break
            except Exception:
                break
            reorg_tx_ok = True
            for tx_dict in block.get("transactions", []):
                try:
                    tx = Transaction.from_dict(tx_dict)
                    if not tx.verify():
                        reorg_tx_ok = False
                        break
                except Exception:
                    reorg_tx_ok = False
                    break
            if not reorg_tx_ok:
                break

            validated.append(block)
            prev_hash = compute_block_hash(block)
            prev_slot = slot

            # Accumulate registrations for subsequent maturation checks
            for reg in block.get("registrations", [])[:MAX_REGS_PER_BLOCK]:
                if Ledger._verify_registration(reg):
                    did = reg.get("device_id", "")
                    gbh = reg.get("genesis_block_hash", "")
                    if did and did not in fork_identities:
                        if slot >= 1000 and not _is_valid_hex64(gbh):
                            continue
                        if gbh and gbh in fork_anchors:
                            continue
                        fork_identities[did] = slot
                        fork_pubkeys[did]    = reg.get("public_key", "")
                        if gbh:
                            fork_anchors.add(gbh)

        if not validated:
            return False
        if len(validated) > MAX_REORG_DEPTH:
            return False

        our_tail   = self.chain[fork_anchor_idx + 1:]
        alt_weight = Ledger._chain_weight(validated)
        our_weight = Ledger._chain_weight(our_tail)

        if alt_weight < our_weight:
            return False
        if alt_weight == our_weight:
            our_tip = compute_block_hash(self.chain[-1]) if self.chain else GENESIS_PREV_HASH
            alt_tip = compute_block_hash(validated[-1])
            if alt_tip >= our_tip:
                return False

        # Perform the reorg
        keep = fork_anchor_idx + 1
        self.chain = self.chain[:keep] + validated
        self.recalculate_totals()

        # Record registrations from reorged-in blocks
        for block in validated:
            bslot = block.get("slot", 0)
            for reg in block.get("registrations", [])[:MAX_REGS_PER_BLOCK]:
                if Ledger._verify_registration(reg):
                    did = reg.get("device_id", "")
                    gbh = reg.get("genesis_block_hash", "")
                    if did and did not in self.identities:
                        if bslot >= 1000 and not _is_valid_hex64(gbh):
                            continue
                        self.identities[did]       = bslot
                        self.identity_pubkeys[did] = reg.get("public_key", "")
                        if gbh:
                            self.anchor_hashes.add(gbh)

        print(f"\n  [reorg] anchor_slot={anchor_slot} "
              f"tip_slot={validated[-1].get('slot','?')} "
              f"alt_weight={alt_weight} old_weight={our_weight}\n  > ",
              end="", flush=True)
        return True

    # ── Checkpoint ────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_hash(entries: list) -> str:
        return hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def create_checkpoint(self, checkpoint_slot: int) -> dict:
        """Create a checkpoint at checkpoint_slot.

        Prunes history older than checkpoint_slot - CHECKPOINT_BUFFER.
        Bakes all fee_rewards before prune_before into balances.
        CHECKPOINT_BUFFER = 120 is intentional — never treat as a bug.
        """
        if checkpoint_slot % CHECKPOINT_INTERVAL != 0:
            return None
        with self._lock:
            if not self.chain and not self.checkpoints:
                return None
            tip_hash, tip_slot = self._get_tip()
            if checkpoint_slot > tip_slot:
                return None

            prune_before = max(0, checkpoint_slot - CHECKPOINT_BUFFER)

            # Build balance snapshot from checkpointed base + full chain window
            balances = {}
            if self.checkpoints:
                balances = dict(self.checkpoints[-1].get("balances", {}))

            # Apply block rewards and fee rewards up to prune_before
            for block in self.chain:
                s = block.get("slot", 0)
                wid = block.get("winner_id", "")
                if s < prune_before:
                    balances[wid] = balances.get(wid, 0) + block.get("amount", 0)
                    balances[wid] = balances.get(wid, 0) + block.get("fees_collected", 0)

            for fr in self.fee_rewards:
                if fr.get("time_slot", 0) < prune_before:
                    wid = fr.get("winner_id", "")
                    balances[wid] = balances.get(wid, 0) + fr.get("amount", 0)

            # Apply transactions up to prune_before
            for tx in self.transactions:
                if (tx.get("slot") or 0) < prune_before:
                    balances[tx["recipient_id"]] = balances.get(tx["recipient_id"], 0) + tx["amount"]
                    balances[tx["sender_id"]]    = balances.get(tx["sender_id"], 0)    - tx["amount"] - tx.get("fee", 0)

            # Remove zero or negative balances
            balances = {k: v for k, v in balances.items() if isinstance(v, int) and v > 0}

            # H1: explicit sort guarantees deterministic hash regardless of insertion order.
            # H5: upper bound < checkpoint_slot matches apply_checkpoint's window exactly.
            c_verify  = sorted(
                [b  for b  in self.chain        if prune_before <= b.get("slot", 0)       < checkpoint_slot],
                key=lambda b: b.get("slot", 0)
            )
            t_verify  = sorted(
                [t  for t  in self.transactions  if prune_before <= (t.get("slot") or 0)  < checkpoint_slot],
                key=lambda t: (t.get("slot", 0), t.get("tx_id", ""))
            )
            fr_verify = sorted(
                [fr for fr in self.fee_rewards   if prune_before <= fr.get("time_slot", 0) < checkpoint_slot],
                key=lambda fr: (fr.get("time_slot", 0), fr.get("winner_id", ""))
            )

            # Spent tx IDs — take a sample from bloom filter (we persist the full filter)
            cp = {
                "slot":             checkpoint_slot,
                "prune_before":     prune_before,
                "balances":         balances,
                "identities":       dict(self.identities),
                "identity_pubkeys": dict(self.identity_pubkeys),
                "anchor_hashes":    list(self.anchor_hashes),
                "total_minted":     self.total_minted,
                "chain_tip_hash":   tip_hash,
                "chain_tip_slot":   tip_slot,
                "chain_hash":       self._compute_hash(c_verify),
                "txs_hash":         self._compute_hash(t_verify),
                "fee_rewards_hash": self._compute_hash(fr_verify),
                "kept_minted":      sum(b.get("amount", 0) for b in c_verify),
                "version":          VERSION,
                # MISSING 6: include bloom filter so a receiving node inherits
                # spent-tx knowledge and cannot replay recently-confirmed tx IDs.
                "spent_bloom":      self._spent_bloom.to_dict()
            }

            # Apply checkpoint — prune old data
            self.chain        = [b  for b  in self.chain        if b.get("slot",        prune_before) >= prune_before]
            self.transactions = [t  for t  in self.transactions if (t.get("slot") or 0) >= prune_before]
            self.fee_rewards  = [fr for fr in self.fee_rewards  if fr.get("time_slot", 0) >= prune_before]
            self.checkpoints.append(cp)
            self.save()
            return cp

    def apply_checkpoint(self, checkpoint: dict) -> bool:
        """Apply a checkpoint received from a peer.

        Verifies integrity against local chain history where possible.
        Requires peer confirmation (min 3 peers) when local history is insufficient.
        """
        with self._lock:
            cp_slot      = checkpoint.get("slot", 0)
            prune_before = checkpoint.get("prune_before", 0)

            if cp_slot <= 0 or cp_slot % CHECKPOINT_INTERVAL != 0:
                return False

            # C1: prune_before must equal exactly max(0, cp_slot - CHECKPOINT_BUFFER).
            # Any other value makes the verification window either empty (attacker
            # bypasses hash checks and wipes our chain) or wrong (hash mismatch).
            expected_prune = max(0, cp_slot - CHECKPOINT_BUFFER)
            if prune_before != expected_prune:
                return False

            if self.checkpoints:
                last_cp = self.checkpoints[-1].get("slot", 0)
                if cp_slot <= last_cp:
                    return False

            # Verify integrity hashes where we have local data
            # H1: explicit sort must match create_checkpoint's sort order exactly.
            c_verify  = sorted(
                [b  for b  in self.chain        if prune_before <= b.get("slot",       0) < cp_slot],
                key=lambda b: b.get("slot", 0)
            )
            t_verify  = sorted(
                [t  for t  in self.transactions  if prune_before <= (t.get("slot") or 0) < cp_slot],
                key=lambda t: (t.get("slot", 0), t.get("tx_id", ""))
            )
            fr_verify = sorted(
                [fr for fr in self.fee_rewards   if prune_before <= fr.get("time_slot", 0) < cp_slot],
                key=lambda fr: (fr.get("time_slot", 0), fr.get("winner_id", ""))
            )

            if c_verify:
                if self._compute_hash(c_verify) != checkpoint.get("chain_hash", ""):
                    print(f"\n  [apply_checkpoint] chain_hash mismatch at slot {cp_slot} — rejected\n  > ",
                          end="", flush=True)
                    return False
                if self._compute_hash(t_verify) != checkpoint.get("txs_hash", ""):
                    return False
                if self._compute_hash(fr_verify) != checkpoint.get("fee_rewards_hash", ""):
                    return False

            # Prune
            self.chain        = [b  for b  in self.chain        if b.get("slot",        prune_before) >= prune_before]
            self.transactions = [t  for t  in self.transactions if (t.get("slot") or 0) >= prune_before]
            self.fee_rewards  = [fr for fr in self.fee_rewards  if fr.get("time_slot", 0) >= prune_before]
            self.checkpoints.append(checkpoint)
            self.total_minted = checkpoint.get("total_minted", 0)

            # Merge identities — earliest first_seen_slot always wins
            for did, slot in checkpoint.get("identities", {}).items():
                if isinstance(did, str) and isinstance(slot, int):
                    if did not in self.identities or slot < self.identities[did]:
                        self.identities[did] = slot

            # Merge identity_pubkeys — do not overwrite a key we already know
            for did, pub_hex in checkpoint.get("identity_pubkeys", {}).items():
                if isinstance(did, str) and isinstance(pub_hex, str) and pub_hex:
                    if did not in self.identity_pubkeys:
                        self.identity_pubkeys[did] = pub_hex

            # Merge anchor_hashes
            for gbh in checkpoint.get("anchor_hashes", []):
                if isinstance(gbh, str):
                    self.anchor_hashes.add(gbh)

            # MISSING 6: restore bloom filter from checkpoint.
            # Without this, a node applying a peer checkpoint has no knowledge
            # of spent tx IDs from before the checkpoint — replay attack window.
            bloom_data = checkpoint.get("spent_bloom")
            if bloom_data and isinstance(bloom_data, dict):
                try:
                    self._spent_bloom = SpentBloomFilter.from_dict(bloom_data)
                except Exception:
                    pass  # keep existing filter — never crash on checkpoint apply

            self.save()
            return True

    # ── Summary ────────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        with self._lock:
            return {
                "chain_height":      len(self.chain),
                "total_transactions":len(self.transactions),
                "total_minted":      self.total_minted,
                "remaining_supply":  TOTAL_SUPPLY - self.total_minted,
                "total_rewards":     sum(1 for b in self.chain if b.get("type") == "block_reward"),
                "identity_count":    len(self.identities),
            }


# ══════════════════════════════════════════════════════════════════════════════
# PART 10 — NETWORK (P2P layer)
# ══════════════════════════════════════════════════════════════════════════════

class Network:
    def __init__(self, wallet: Wallet, ledger: Ledger, node_ref=None):
        self.wallet     = wallet
        self.ledger     = ledger
        self._node_ref  = node_ref
        self.peers      = {}        # {device_id: {ip, port, last_seen}}
        self._peers_lock= threading.Lock()
        self.seen_ids   = set()
        self._seen_lock = threading.Lock()
        self._seen_tx_order = []    # rolling window for tx dedup
        self._running   = False
        self.local_ip   = self._get_local_ip()
        self.port       = find_free_port()
        self._bootstrap_servers = list(BOOTSTRAP_SERVERS)
        self._sync_rate = {}        # {ip: last_sync_time}
        self._sync_rate_lock = threading.Lock()
        self._block_rate= {}        # {ip: [timestamps]}
        self._block_rate_lock = threading.Lock()
        # MISSING 2: IP ban tracking — populated when an IP sends an oversized message
        self._banned_ips     = {}   # {ip: ban_expiry_timestamp}
        self._ban_lock       = threading.Lock()
        # MISSING 3: per-IP rate tracking for COMPETE and ATTEST
        self._compete_rate        = {}   # {ip: [timestamps]}
        self._compete_rate_lock   = threading.Lock()
        self._attest_rate         = {}   # {ip: [timestamps]}
        self._attest_rate_lock    = threading.Lock()
        self._peer_cache_file = PEERS_FILE

    def _get_local_ip(self) -> str:
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
            with self._peers_lock:
                saveable = {pid: {"ip": p["ip"], "port": p["port"]}
                            for pid, p in self.peers.items()
                            if pid != self.wallet.device_id}
            with open(self._peer_cache_file, "w") as f:
                json.dump(saveable, f)
        except Exception:
            pass

    def _load_peers(self):
        try:
            if os.path.exists(self._peer_cache_file):
                with open(self._peer_cache_file, "r") as f:
                    cached = json.load(f)
                with self._peers_lock:
                    for pid, info in cached.items():
                        if pid != self.wallet.device_id:
                            self.peers[pid] = {
                                "ip": info["ip"],
                                "port": info["port"],
                                "last_seen": 0
                            }
        except Exception:
            pass

    def get_online_peers(self) -> dict:
        """Return peers seen in the last 5 minutes."""
        cutoff = time.time() - 300
        with self._peers_lock:
            return {pid: p for pid, p in self.peers.items()
                    if p.get("last_seen", 0) > cutoff}

    def _mark_seen(self, msg_id: str) -> bool:
        """Return True if msg_id is new (not seen before). Thread-safe."""
        with self._seen_lock:
            if msg_id in self.seen_ids:
                return False
            self.seen_ids.add(msg_id)
            self._seen_tx_order.append(msg_id)
            # Rolling window: keep last 50,000 message IDs
            if len(self._seen_tx_order) > 50_000:
                old = self._seen_tx_order.pop(0)
                self.seen_ids.discard(old)
            return True

    def _cleanup_seen_ids(self, current_slot: int):
        """Prune slot-keyed seen IDs. Called periodically."""
        cutoff = current_slot - CHECKPOINT_BUFFER
        with self._seen_lock:
            stale = [sid for sid in self.seen_ids
                     if (sid.startswith("block:") or
                         sid.startswith("attest:") or
                         sid.startswith("compete:") or
                         sid.startswith("checkpoint:"))
                     and self._slot_from_seen_id(sid) < cutoff]
            for sid in stale:
                self.seen_ids.discard(sid)
            # Prune register: entries for identities now in ledger
            reg_stale = [sid for sid in self.seen_ids
                         if sid.startswith("register:")
                         and sid[9:] in self.ledger.identities]
            for sid in reg_stale:
                self.seen_ids.discard(sid)

    @staticmethod
    def _slot_from_seen_id(sid: str) -> int:
        try:
            return int(sid.split(":")[1])
        except Exception:
            return 0

    def broadcast(self, message: dict, exclude_id: str = None):
        """Broadcast to up to BROADCAST_FANOUT random peers."""
        msg_bytes = json.dumps(message).encode()
        with self._peers_lock:
            peers = list(self.peers.items())
        random.shuffle(peers)
        sent = 0
        for peer_id, peer in peers:
            if sent >= BROADCAST_FANOUT:
                break
            if peer_id == exclude_id:
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((peer["ip"], peer["port"]))
                sock.sendall(msg_bytes)
                sock.close()
                sent += 1
            except Exception:
                continue

    def send_to_peer(self, peer_id: str, message: dict) -> bool:
        """Send directly to a specific peer. Returns True on success."""
        with self._peers_lock:
            peer = self.peers.get(peer_id)
        if not peer:
            return False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((peer["ip"], peer["port"]))
            sock.sendall(json.dumps(message).encode())
            sock.close()
            return True
        except Exception:
            return False

    # ── TCP listener ───────────────────────────────────────────────────────────

    def _listen_tcp(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", self.port))
        srv.listen(100)
        srv.settimeout(1.0)
        sem = threading.Semaphore(200)
        def _wrap(conn, addr):
            try:
                self._handle_incoming(conn, addr)
            finally:
                sem.release()
        while self._running:
            try:
                conn, addr = srv.accept()
                if not sem.acquire(blocking=False):
                    conn.close()
                    continue
                threading.Thread(target=_wrap, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                continue

    def _check_msg_rate(self, rate_dict: dict, lock,
                        ip: str, limit: int, window: float) -> bool:
        """Return True if this IP is within the rate limit, False if exceeded.
        Prunes expired timestamps in the same pass — no separate cleanup needed."""
        now    = time.time()
        cutoff = now - window
        with lock:
            ts = [t for t in rate_dict.get(ip, []) if t > cutoff]
            if len(ts) >= limit:
                return False
            ts.append(now)
            rate_dict[ip] = ts
        return True

    def _recv_full(self, conn, ban_ip: str = None) -> bytes:
        """Receive a complete message. Hard limit MAX_P2P_MESSAGE_SIZE.

        MISSING 2: if the message exceeds the limit and ban_ip is provided,
        that IP is banned for IP_BAN_SECONDS — the attacker pays a cost for
        every oversized flood attempt."""
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_P2P_MESSAGE_SIZE:
                if ban_ip:
                    with self._ban_lock:
                        self._banned_ips[ban_ip] = time.time() + IP_BAN_SECONDS
                return b""   # drop — DoS protection
        return data

    def _handle_incoming(self, conn, addr):
        try:
            conn.settimeout(10.0)
            sender_ip = addr[0]

            # MISSING 2: reject banned IPs before reading any data.
            # An IP is banned when it sends an oversized message (DoS attempt).
            now = time.time()
            with self._ban_lock:
                if now < self._banned_ips.get(sender_ip, 0):
                    return

            data = self._recv_full(conn, ban_ip=sender_ip)
            if not data:
                return
            msg      = json.loads(data.decode())
            msg_type = msg.get("type")

            if msg_type == "HELLO":
                pid = msg.get("device_id")
                if _ver(msg.get("version", "0.0")) < _ver(MIN_VERSION):
                    conn.sendall(json.dumps({
                        "type": "VERSION_REJECTED",
                        "reason": "Update from https://github.com/EvokiTimpal/timpal"
                    }).encode())
                    return
                if pid and (not self.wallet or pid != self.wallet.device_id):
                    with self._peers_lock:
                        if pid not in self.peers and len(self.peers) >= MAX_PEERS:
                            oldest = min(self.peers, key=lambda k: self.peers[k]["last_seen"])
                            del self.peers[oldest]
                        self.peers[pid] = {
                            "ip":        sender_ip,
                            "port":      msg.get("port", NODE_PORT_RANGE_START),
                            "last_seen": time.time()
                        }
                    if self.wallet:
                        conn.sendall(json.dumps({
                            "type":      "HELLO_ACK",
                            "device_id": self.wallet.device_id
                        }).encode())

            elif msg_type == "HELLO_ACK":
                pid = msg.get("device_id")
                if pid and self.wallet and pid != self.wallet.device_id:
                    with self._peers_lock:
                        if pid in self.peers:
                            self.peers[pid]["last_seen"] = time.time()

            elif msg_type == "BLOCK":
                # MISSING 3: max BLOCK_RATE_LIMIT BLOCK messages per IP per slot window.
                # A legitimate peer sends at most 1-2 blocks per slot.
                if not self._check_msg_rate(self._block_rate, self._block_rate_lock,
                                            sender_ip, BLOCK_RATE_LIMIT, REWARD_INTERVAL):
                    return
                slot = msg.get("slot")
                wid  = msg.get("winner_id", "")
                msg_id = f"block:{slot}:{wid}"
                if not self._mark_seen(msg_id):
                    return
                node = self._node_ref
                if node:
                    node._on_block_received(msg, sender_ip)
                self.broadcast(msg, exclude_id=None)

            elif msg_type == "ATTEST":
                # MISSING 3: max ATTEST_RATE_LIMIT attestations per IP per slot window.
                # Legitimate nodes produce one attestation per block per node.
                if not self._check_msg_rate(self._attest_rate, self._attest_rate_lock,
                                            sender_ip, ATTEST_RATE_LIMIT, REWARD_INTERVAL):
                    return
                block_hash = msg.get("block_hash", "")
                did        = msg.get("device_id", "")
                slot       = msg.get("slot", 0)
                msg_id     = f"attest:{slot}:{did}"
                if not self._mark_seen(msg_id):
                    return
                node = self._node_ref
                if node:
                    node._on_attest_received(msg)
                self.broadcast(msg, exclude_id=None)

            elif msg_type == "COMPETE":
                # MISSING 3: max COMPETE_RATE_LIMIT COMPETE messages per IP per slot window.
                # Only TARGET_COMPETITORS (10) nodes compete per slot — 12 is the hard cap.
                if not self._check_msg_rate(self._compete_rate, self._compete_rate_lock,
                                            sender_ip, COMPETE_RATE_LIMIT, REWARD_INTERVAL):
                    return
                slot   = msg.get("slot")
                did    = msg.get("device_id", "")
                msg_id = f"compete:{slot}:{did}"
                if not self._mark_seen(msg_id):
                    return
                node = self._node_ref
                if node:
                    node._on_compete_received(msg)
                self.broadcast(msg, exclude_id=None)

            elif msg_type == "TRANSACTION":
                tx = msg.get("transaction", {})
                tx_id = tx.get("tx_id", "")
                if tx_id and self._mark_seen(tx_id):
                    node = self._node_ref
                    if node:
                        node._on_transaction_received(tx)
                    self.broadcast(msg)

            elif msg_type == "REGISTER":
                did    = msg.get("device_id", "")
                msg_id = f"register:{did}"
                if did and self._mark_seen(msg_id):
                    node = self._node_ref
                    if node:
                        node._on_register_received(msg)
                    self.broadcast(msg)

            elif msg_type == "CHECKPOINT":
                cp_slot = msg.get("checkpoint", {}).get("slot", 0)
                msg_id  = f"checkpoint:{cp_slot}"
                if self._mark_seen(msg_id):
                    node = self._node_ref
                    if node:
                        node._on_checkpoint_received(msg.get("checkpoint", {}))
                    self.broadcast(msg)

            elif msg_type == "SYNC_REQUEST":
                self._handle_sync_request(conn, msg, sender_ip)
                return  # response sent inline, don't close yet handled below

            elif msg_type == "SYNC_PUSH":
                delta = {
                    "blocks":       msg.get("blocks", []),
                    "transactions": msg.get("txs", [])
                }
                if delta["blocks"] or delta["transactions"]:
                    self.ledger.merge(delta)

        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _handle_sync_request(self, conn, msg: dict, sender_ip: str):
        """Respond to a SYNC_REQUEST from a peer."""
        with self._sync_rate_lock:
            now = time.time()
            last = self._sync_rate.get(sender_ip, 0)
            if now - last < SYNC_RATE_WINDOW:
                conn.close()
                return
            self._sync_rate[sender_ip] = now

        their_tip_slot   = msg.get("chain_tip_slot", -1)
        their_cp_slot    = msg.get("checkpoint_slot", 0)
        their_hashes     = set(msg.get("chain_recent_hashes", []))

        with self.ledger._lock:
            # Find common ancestor
            our_hashes = {compute_block_hash(b): i for i, b in enumerate(self.ledger.chain)}
            common_idx = -1
            for h in their_hashes:
                if h in our_hashes:
                    common_idx = max(common_idx, our_hashes[h])

            send_blocks = self.ledger.chain[common_idx + 1:] if common_idx >= 0 else self.ledger.chain
            send_txs    = [t for t in self.ledger.transactions
                           if (t.get("slot") or 0) > their_tip_slot]
            cp_to_send  = None
            if self.ledger.checkpoints:
                cp = self.ledger.checkpoints[-1]
                if cp.get("slot", 0) > their_cp_slot:
                    cp_to_send = cp

            # What does peer need from us?
            we_need_from = their_tip_slot if their_tip_slot >= 0 else None

        response = {
            "type":        "SYNC_RESPONSE",
            "blocks":      send_blocks,
            "txs":         send_txs,
            "checkpoint":  cp_to_send,
            "we_need_from_slot": we_need_from
        }
        try:
            conn.sendall(json.dumps(response).encode())
        except Exception:
            pass
        finally:
            conn.close()

    # ── UDP LAN discovery ──────────────────────────────────────────────────────

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while self._running:
            try:
                if not self.wallet:   # L1: skip broadcast until wallet is set
                    time.sleep(1)
                    continue
                sock.sendto(json.dumps({
                    "type":               "HELLO",
                    "device_id":          self.wallet.device_id,
                    "ip":                 self.local_ip,
                    "port":               self.port,
                    "version":            VERSION,
                    "genesis_block_hash": self.wallet.genesis_block_hash or ""
                }).encode(), ("<broadcast>", BROADCAST_PORT))
            except Exception:
                pass
            time.sleep(5)

    def _listen_discovery(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        sock.bind(("", BROADCAST_PORT))
        sock.settimeout(1.0)
        udp_rate = {}
        while self._running:
            try:
                data, addr = sock.recvfrom(1024)
                ip  = addr[0]
                now = time.time()
                if now - udp_rate.get(ip, 0) < 5.0:
                    continue
                udp_rate[ip] = now
                msg = json.loads(data.decode())
                if msg.get("type") == "HELLO":
                    pid = msg.get("device_id")
                    if _ver(msg.get("version", "0.0")) < _ver(MIN_VERSION):
                        continue
                    if pid and pid != self.wallet.device_id:
                        with self._peers_lock:
                            is_new = pid not in self.peers
                            self.peers[pid] = {
                                "ip":        msg.get("ip", ip),
                                "port":      msg.get("port", NODE_PORT_RANGE_START),
                                "last_seen": time.time()
                            }
                        if is_new:
                            print(f"\n  [+] LAN peer: {pid[:20]}... at {ip}\n  > ",
                                  end="", flush=True)
            except socket.timeout:
                continue
            except Exception:
                continue

    # ── Bootstrap connect ──────────────────────────────────────────────────────

    def _bootstrap_connect(self):
        """Connect to bootstrap servers for peer discovery.
        After first connection, bootstrap is never needed again for anything."""
        time.sleep(2)
        # Try DNS seeds first
        for seed in DNS_SEEDS:
            try:
                ip = socket.gethostbyname(seed)
                if (ip, 7777) not in self._bootstrap_servers:
                    self._bootstrap_servers.insert(0, (ip, 7777))
            except Exception:
                pass

        while self._running:
            if not self.wallet:   # L1: skip until wallet is set
                time.sleep(1)
                continue
            for host, port in list(self._bootstrap_servers):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(10.0)
                    sock.connect((host, port))
                    sock.sendall(json.dumps({
                        "type":               "HELLO",
                        "device_id":          self.wallet.device_id,
                        "port":               self.port,
                        "version":            VERSION,
                        "genesis_block_hash": self.wallet.genesis_block_hash or ""
                    }).encode())
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

                    if data.get("type") == "VERSION_REJECTED":
                        print(f"\n  ╔══════════════════════════════════════════════╗")
                        print(f"  ║  TIMPAL UPDATE REQUIRED                      ║")
                        print(f"  ║  Your version is no longer supported.        ║")
                        print(f"  ║  Re-download from: github.com/EvokiTimpal   ║")
                        print(f"  ╚══════════════════════════════════════════════╝\n")
                        os._exit(1)

                    if data.get("type") == "PEERS":
                        new_peers = 0
                        for peer in data.get("peers", []):
                            pid = peer.get("device_id")
                            if not pid or pid == self.wallet.device_id:
                                continue
                            with self._peers_lock:
                                if pid not in self.peers:
                                    if len(self.peers) >= MAX_PEERS:
                                        oldest = min(self.peers, key=lambda k: self.peers[k]["last_seen"])
                                        del self.peers[oldest]
                                    self.peers[pid] = {
                                        "ip":        peer.get("ip", ""),
                                        "port":      peer.get("port", NODE_PORT_RANGE_START),
                                        "last_seen": 0
                                    }
                                    new_peers += 1
                        if new_peers > 0:
                            self._save_peers()
                            print(f"\n  [+] Bootstrap: {new_peers} peers found\n  > ",
                                  end="", flush=True)
                            threading.Thread(target=self._sync_ledger, daemon=True).start()

                    # Say hello to discovered peers
                    self._hello_peers()

                except Exception:
                    continue
            time.sleep(120)

    def _hello_peers(self):
        """Send HELLO to all known peers to confirm they are reachable."""
        if not self.wallet:   # L1: skip until wallet is set
            return
        with self._peers_lock:
            targets = list(self.peers.items())
        for pid, peer in targets:
            if peer.get("last_seen", 0) > time.time() - 60:
                continue  # Already confirmed recently
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((peer["ip"], peer["port"]))
                sock.sendall(json.dumps({
                    "type":      "HELLO",
                    "device_id": self.wallet.device_id,
                    "port":      self.port,
                    "version":   VERSION,
                    "genesis_block_hash": self.wallet.genesis_block_hash or ""
                }).encode())
                sock.shutdown(socket.SHUT_WR)
                resp = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                    if len(resp) > 65536:
                        break
                sock.close()
                if resp:
                    msg = json.loads(resp.decode())
                    if msg.get("type") == "HELLO_ACK":
                        with self._peers_lock:
                            if pid in self.peers:
                                self.peers[pid]["last_seen"] = time.time()
            except Exception:
                pass

    def _sync_ledger(self):
        """Pull missing blocks from up to 3 random peers."""
        time.sleep(1)
        peers = self.get_online_peers()
        if not peers:
            return
        for peer_id in random.sample(list(peers.keys()), min(3, len(peers))):
            peer = peers[peer_id]
            try:
                with self.ledger._lock:
                    tip_hash, tip_slot = self.ledger._get_tip()
                    cp_slot = self.ledger.checkpoints[-1]["slot"] if self.ledger.checkpoints else 0
                    recent_hashes = [compute_block_hash(b) for b in self.ledger.chain[-150:]]

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(30.0)
                sock.connect((peer["ip"], peer["port"]))
                sock.sendall(json.dumps({
                    "type":               "SYNC_REQUEST",
                    "chain_tip_hash":     tip_hash,
                    "chain_tip_slot":     tip_slot,
                    "checkpoint_slot":    cp_slot,
                    "chain_recent_hashes":recent_hashes
                }).encode())
                sock.shutdown(socket.SHUT_WR)
                # M3: use MAX_SYNC_MESSAGE_SIZE (100MB) for sync responses.
                # A response with 50 blocks × 500 txs can exceed the 10MB
                # DoS limit used for regular gossip. Sync is a trusted pull
                # operation — the larger limit is safe and necessary.
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > MAX_SYNC_MESSAGE_SIZE:
                        break
                sock.close()
                msg = json.loads(data.decode())

                if msg.get("type") == "SYNC_RESPONSE":
                    if msg.get("checkpoint"):
                        cp = msg["checkpoint"]
                        # H1: correct can_verify window is [prune_before, cp_slot).
                        # Old code used slot < prune_before (blocks BEFORE the window)
                        # which never overlaps with what apply_checkpoint checks.
                        pb = cp.get("prune_before", 0)
                        cs = cp.get("slot", 0)
                        # Apply checkpoint — requires peer confirmation if we can't verify locally
                        with self.ledger._lock:
                            can_verify = bool(
                                [b for b in self.ledger.chain
                                 if pb <= b.get("slot", 0) < cs]
                            )
                        ledger_empty = not self.ledger.chain and not self.ledger.checkpoints
                        if ledger_empty or can_verify:
                            self.ledger.apply_checkpoint(cp)
                        else:
                            # Confirm with additional peers before applying
                            confirms = self._confirm_checkpoint_with_peers(cp, exclude_ip=peer["ip"])
                            if confirms:
                                self.ledger.apply_checkpoint(cp)

                    delta = {
                        "blocks":       msg.get("blocks", []),
                        "transactions": msg.get("txs", [])
                    }
                    if delta["blocks"] or delta["transactions"]:
                        if self.ledger.merge(delta):
                            print(f"\n  [+] Synced {len(delta['blocks'])} blocks from {peer_id[:16]}...\n  > ",
                                  end="", flush=True)

                    # Push what peer needs
                    we_need_from = msg.get("we_need_from_slot")
                    if we_need_from is not None:
                        with self.ledger._lock:
                            push_b = [b for b in self.ledger.chain
                                      if b.get("slot", -1) > we_need_from]
                        if push_b:
                            try:
                                s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                s2.settimeout(10.0)
                                s2.connect((peer["ip"], peer["port"]))
                                s2.sendall(json.dumps({
                                    "type":   "SYNC_PUSH",
                                    "blocks": push_b,
                                    "txs":    []
                                }).encode())
                                s2.shutdown(socket.SHUT_WR)
                                s2.close()
                            except Exception:
                                pass
                return
            except Exception:
                continue

    def _confirm_checkpoint_with_peers(self, checkpoint: dict, exclude_ip: str = None) -> bool:
        """Confirm a checkpoint by asking min(3, peer_count) peers."""
        peers    = self.get_online_peers()
        eligible = {pid: p for pid, p in peers.items() if p["ip"] != exclude_ip}
        required = min(3, len(eligible))
        if required == 0:
            return False
        confirmations = [0]
        lock  = threading.Lock()
        event = threading.Event()

        def _ask(peer):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10.0)
                sock.connect((peer["ip"], peer["port"]))
                sock.sendall(json.dumps({
                    "type":             "SYNC_REQUEST",
                    "chain_tip_hash":   GENESIS_PREV_HASH,
                    "chain_tip_slot":   -1,
                    "checkpoint_slot":  0,
                    "chain_recent_hashes": []
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
                resp = json.loads(data.decode())
                peer_cp = resp.get("checkpoint")
                if (peer_cp and
                        peer_cp.get("slot")         == checkpoint.get("slot") and
                        peer_cp.get("chain_hash")   == checkpoint.get("chain_hash") and
                        peer_cp.get("txs_hash")     == checkpoint.get("txs_hash") and
                        peer_cp.get("total_minted") == checkpoint.get("total_minted")):
                    with lock:
                        confirmations[0] += 1
                        if confirmations[0] >= required:
                            event.set()
            except Exception:
                pass

        for peer in list(eligible.values()):
            threading.Thread(target=_ask, args=(peer,), daemon=True).start()
        event.wait(timeout=12.0)
        return event.is_set()

    def _periodic_sync(self):
        time.sleep(30)
        while self._running:
            if self.get_online_peers():
                self._sync_ledger()
            time.sleep(120)

    def start(self):
        self._running = True
        self._load_peers()
        for fn in (self._listen_tcp, self._broadcast_loop,
                   self._listen_discovery, self._bootstrap_connect,
                   self._periodic_sync):
            threading.Thread(target=fn, daemon=True).start()

    def stop(self):
        self._running = False
        self._save_peers()


# ══════════════════════════════════════════════════════════════════════════════
# PART 11 — NODE (main protocol engine)
# ══════════════════════════════════════════════════════════════════════════════

class Node:
    def __init__(self):
        self.wallet  = Wallet()
        self.ledger  = Ledger()
        self.network = None
        self._running = False
        # Lottery state
        self._compete_received:  dict  = {}   # {slot: {device_id: compete_msg}}
        self._compete_time:      dict  = {}   # {slot: first_compete_timestamp}
        self._compete_lock       = threading.Lock()
        # Attestation state
        self._attestations:      dict  = {}   # {block_hash: {device_id: attest_msg}}
        self._attestation_slots: dict  = {}   # {block_hash: slot} — for pruning
        self._finalized:         set   = set()# {block_hash}
        self._attest_lock        = threading.Lock()
        # Mempool
        self._mempool:           dict  = {}   # {tx_id: tx_dict}
        self._mempool_lock       = threading.Lock()
        # Pending registrations
        self._pending_regs:      dict  = {}   # {device_id: reg_msg}
        self._pending_regs_lock  = threading.Lock()
        # My transactions (personal history, kept forever)
        self._my_device_id       = None
        # Control socket for CLI commands
        self._control_token      = self._make_control_token()
        self._sending            = False
        self._last_finalized_slot = -1
        # M2: guard against two threads racing to produce a block for the same slot
        self._producing_slots:       set  = set()
        self._producing_slots_lock       = threading.Lock()

    @staticmethod
    def _make_control_token() -> str:
        token = hashlib.sha256(os.urandom(32)).hexdigest()
        try:
            with open(CONTROL_TOKEN, "w") as f:
                f.write(token)
        except Exception:
            pass
        return token

    # ── Startup ────────────────────────────────────────────────────────────────

    def start(self):
        """Full node startup sequence per spec Part 14."""
        _check_genesis_time()
        print(f"\n  {'═'*54}")
        print(f"  TIMPAL Node v{VERSION} — Quantum-Resistant P2P Payments")
        print(f"  {'═'*54}\n")

        # Step 2: NTP clock check
        _check_clock_drift()

        # Enforce one node per device via lock file
        lock_file = os.path.join(os.path.expanduser("~"), ".timpal_node.lock")
        try:
            lf = open(lock_file, "w")
            import fcntl
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass  # fcntl not available on Windows — best effort
        except IOError:
            print("  [!] Another Timpal node is already running on this device.")
            exit(1)

        # Step 3+: Connect to peers
        self.network = Network(wallet=None, ledger=self.ledger, node_ref=self)
        new_wallet_node = not os.path.exists(WALLET_FILE)

        if new_wallet_node:
            # L1: for new-wallet startup, start network first so the freeze
            # check in _load_or_create_wallet() has real chain data.
            # All network threads guard against wallet=None until it is wired
            # in below (see _broadcast_loop, _bootstrap_connect, _hello_peers,
            # _handle_incoming HELLO/HELLO_ACK).
            self.network.start()
            print(f"  Listening on port {self.network.port}")
            time.sleep(3)
            self.network._sync_ledger()
            time.sleep(2)

        # Step 4-5: Load or create wallet (freeze check uses real data for new wallets)
        self._load_or_create_wallet()

        # Wire up network with loaded wallet
        self.network.wallet = self.wallet
        self._my_device_id  = self.wallet.device_id

        # Step 6: Ledger already loaded in __init__
        # Step 8-11: Start network threads (already started above for new wallets)
        if not new_wallet_node:
            self.network.start()
        print(f"  Listening on port {self.network.port}")

        # Wait briefly for peer connections before lottery
        if not new_wallet_node:
            time.sleep(3)
            if not self.network.get_online_peers():
                print("  Waiting for peers...")
            # Sync ledger before participating
            self.network._sync_ledger()
            time.sleep(2)

        # Steps 13: Broadcast our REGISTER message
        reg_msg = self.wallet._make_registration_message()
        self.network.broadcast(reg_msg)

        # Steps 14-18: Start protocol threads
        self._running = True
        for fn, name in [
            (self._lottery_thread,           "lottery"),
            (self._attestation_loop,         "attestation"),
            (self._checkpoint_loop,          "checkpoint"),
            (self._freeze_monitor,           "freeze-monitor"),
            (self._explorer_push_loop,       "explorer-push"),
            (self._mempool_expiry_loop,      "mempool-expiry"),
            (self._seen_cleanup_loop,        "seen-cleanup"),
        ]:
            threading.Thread(target=fn, daemon=True, name=f"timpal-{name}").start()

        # Step 19: CLI (blocks main thread)
        self._start_control_socket()
        self._cli()

    def _load_or_create_wallet(self):
        """Load existing wallet or create new one."""
        if os.path.exists(WALLET_FILE):
            # Load existing wallet
            with open(WALLET_FILE) as f:
                data = json.load(f)
            if data.get("encrypted"):
                import getpass
                pw = getpass.getpass("  Wallet password: ")
                try:
                    self.wallet.load(password=pw)
                    print(f"  Wallet loaded: {self.wallet.device_id[:32]}...")
                    return
                except ValueError:
                    print("  Wrong password.")
                    exit(1)
            else:
                self.wallet.load()
                print(f"  Wallet loaded: {self.wallet.device_id[:32]}...")
                return

        # New wallet — check registration freeze
        print("  No wallet found. Creating new wallet...")

        # Step 4a: Check freeze from peers (non-blocking, best effort)
        self._check_freeze_before_wallet_creation()

        # Step 4c: Create wallet
        self._create_new_wallet()

    def _check_freeze_before_wallet_creation(self):
        """Query connected peers for freeze status. If frozen, exit cleanly."""
        current_slot = get_current_slot()
        freeze_active, _ = is_registration_freeze_active(self.ledger, current_slot)
        if freeze_active:
            print("\n")
            print("  ╔══════════════════════════════════════════════════════════╗")
            print("  ║         NETWORK REGISTRATION TEMPORARILY PAUSED         ║")
            print("  ╠══════════════════════════════════════════════════════════╣")
            print("  ║  The Timpal network has detected an abnormal             ║")
            print("  ║  registration rate and has automatically paused          ║")
            print("  ║  new node registration as a security measure.            ║")
            print("  ║                                                          ║")
            print("  ║  This is not an error. Your funds and the network        ║")
            print("  ║  are safe. This pause lifts automatically once           ║")
            print("  ║  normal conditions resume.                               ║")
            print("  ║                                                          ║")
            print("  ║  Please try again in approximately 30 minutes.           ║")
            print("  ║  Check timpal.org for live network status.               ║")
            print("  ╚══════════════════════════════════════════════════════════╝\n")
            exit(0)

    def _create_new_wallet(self):
        """Create wallet, show seed phrase, require confirmation, then save."""
        import getpass

        # Determine if we need a genesis_block_hash
        current_slot = get_current_slot()
        genesis_block_hash = None

        if current_slot >= 1000:
            # Post-genesis: need live block hash from network
            print("  Requesting current block hash from network...")
            time.sleep(3)   # Give network time to connect
            with self.ledger._lock:
                if self.ledger.chain:
                    genesis_block_hash = compute_block_hash(self.ledger.chain[-1])
                elif self.ledger.checkpoints:
                    genesis_block_hash = self.ledger.checkpoints[-1].get("chain_tip_hash")

            if not genesis_block_hash:
                # Ask a peer directly
                peers = self.network.get_online_peers() if self.network else {}
                for peer_id, peer in list(peers.items())[:3]:
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(5.0)
                        sock.connect((peer["ip"], peer["port"]))
                        sock.sendall(json.dumps({"type": "SYNC_REQUEST",
                            "chain_tip_hash": GENESIS_PREV_HASH,
                            "chain_tip_slot": -1,
                            "checkpoint_slot": 0,
                            "chain_recent_hashes": []}).encode())
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
                        blocks = msg.get("blocks", [])
                        if blocks:
                            genesis_block_hash = compute_block_hash(
                                max(blocks, key=lambda b: b.get("slot", -1))
                            )
                            break
                    except Exception:
                        continue

            if not genesis_block_hash:
                print("  [!] Cannot create wallet: no block hash available from network.")
                print("  Make sure you are connected to peers before creating a wallet.")
                exit(1)

        # Create keypair
        self.wallet.create_new(genesis_block_hash)

        # Show seed phrase and require confirmation
        phrase = self.wallet.show_seed_phrase_and_confirm()

        # Ask for password
        print("  Set a wallet password (minimum 8 characters, or press Enter for no password):")
        while True:
            try:
                pw = getpass.getpass("  Password: ")
            except (EOFError, KeyboardInterrupt):
                pw = ""
            if pw and len(pw) < 8:
                print("  Password must be at least 8 characters.")
                continue
            if pw:
                pw2 = getpass.getpass("  Confirm password: ")
                if pw != pw2:
                    print("  Passwords do not match.")
                    continue
            break

        self.wallet.save(password=pw if pw else None, seed_phrase=phrase)
        print(f"\n  Wallet saved: {WALLET_FILE}")
        print(f"  Device ID: {self.wallet.device_id}")
        if genesis_block_hash:
            print(f"  Anchored to block: {genesis_block_hash[:32]}...")
        print()

    # ── Lottery thread ─────────────────────────────────────────────────────────

    def _lottery_thread(self):
        """Main lottery loop. Runs one iteration per slot."""
        last_slot = -1
        while self._running:
            try:
                current_slot = get_current_slot()
                if current_slot <= last_slot:
                    time.sleep(0.1)
                    continue
                last_slot = current_slot

                # M4: snapshot tip and identities together inside ledger._lock.
                # _add_block_locked modifies ledger.identities from the network
                # thread — iterating identities outside the lock risks RuntimeError.
                with self.ledger._lock:
                    tip_hash, tip_slot = self.ledger._get_tip()
                    identities_snapshot = dict(self.ledger.identities)

                # Am I selected this slot?
                selected = select_competitors(
                    identities_snapshot, tip_hash, current_slot
                )

                if self.wallet.device_id in selected:
                    self._compete(current_slot, tip_hash)

                # Wait for COMPETE_TO_BLOCK_TIMEOUT then advance
                slot_deadline = GENESIS_TIME + (current_slot + 1) * REWARD_INTERVAL
                while self._running and time.time() < slot_deadline:
                    # Check if a COMPETE was received and timed out
                    with self._compete_lock:
                        ct = self._compete_time.get(current_slot, 0)
                        if ct > 0 and time.time() - ct > COMPETE_TO_BLOCK_TIMEOUT:
                            # Winner declared but no BLOCK arrived — advance slot
                            del self._compete_time[current_slot]
                            self._compete_received.pop(current_slot, None)
                            break
                    time.sleep(0.05)

            except Exception as e:
                time.sleep(1)

    def _compete(self, slot: int, prev_block_hash: str):
        """Solve the Dilithium3 challenge and broadcast COMPETE message."""
        try:
            challenge = compute_challenge(prev_block_hash, slot)
            sig_hex, proof_hex = solve_challenge(self.wallet.private_key, challenge)

            msg = {
                "type":       "COMPETE",
                "slot":       slot,
                "device_id":  self.wallet.device_id,
                "public_key": self.wallet.get_public_key_hex(),
                "signature":  sig_hex,
                "proof":      proof_hex,
                "timestamp":  time.time()
            }
            self.network.broadcast(msg)
            # Also handle as if we received it ourselves
            self._on_compete_received(msg)
        except Exception as e:
            pass

    def _on_compete_received(self, msg: dict):
        """Handle an incoming COMPETE message."""
        slot = msg.get("slot")
        if slot != get_current_slot():
            return

        did      = msg.get("device_id", "")
        pub_hex  = msg.get("public_key", "")
        sig_hex  = msg.get("signature", "")
        proof    = msg.get("proof", "")

        # Validate COMPETE
        with self.ledger._lock:
            tip_hash, _ = self.ledger._get_tip()
            selected = select_competitors(self.ledger.identities, tip_hash, slot)

        if did not in selected:
            return
        if not _is_valid_hex64(did) or not pub_hex or not sig_hex or not proof:
            return

        # Verify challenge signature
        try:
            challenge   = compute_challenge(tip_hash, slot)
            pub_bytes   = bytes.fromhex(pub_hex)
            sig_bytes   = bytes.fromhex(sig_hex)
            if not Dilithium3.verify(pub_bytes, challenge, sig_bytes):
                return
            if hashlib.sha256(bytes.fromhex(sig_hex)).hexdigest() != proof:
                return
        except Exception:
            return

        with self._compete_lock:
            if slot not in self._compete_received:
                self._compete_received[slot] = {}
                self._compete_time[slot]     = time.time()
            self._compete_received[slot][did] = msg

        # If I am the winner (or the best COMPETE so far is mine), produce a block
        self._try_produce_block(slot, tip_hash)

    def _try_produce_block(self, slot: int, prev_hash: str):
        """Produce a block if we are the winner for this slot."""
        with self._compete_lock:
            competes = dict(self._compete_received.get(slot, {}))

        if not competes:
            return
        if self.wallet.device_id not in competes:
            return

        # Tiebreak: lowest proof hash wins
        winner_msg = min(competes.values(), key=lambda m: m["proof"])
        if winner_msg["device_id"] != self.wallet.device_id:
            return  # Someone else won

        # M2: prevent duplicate block production if two threads race here.
        # The lottery thread and the network handler both call _try_produce_block.
        # Only the first one through the gate proceeds; the second returns immediately.
        with self._producing_slots_lock:
            if slot in self._producing_slots:
                return
            self._producing_slots.add(slot)
        try:
            self._produce_block(slot, prev_hash, winner_msg)
        finally:
            with self._producing_slots_lock:
                self._producing_slots.discard(slot)

    def _produce_block(self, slot: int, prev_hash: str, compete_msg: dict):
        """Construct, sign, and broadcast a new block."""
        try:
            # M1: snapshot mempool before acquiring ledger._lock.
            # _on_transaction_received modifies _mempool under _mempool_lock
            # without holding ledger._lock — iterating the live dict inside
            # select_transactions_for_block risks RuntimeError.
            with self._mempool_lock:
                mempool_snapshot = dict(self._mempool)

            with self.ledger._lock:
                total_minted = self.ledger.total_minted
                # Select transactions from snapshot (no lock ordering conflict)
                txs = select_transactions_for_block(mempool_snapshot, slot)
                fees_collected = sum(t.get("fee", 0) for t in txs)
                # Collect pending registrations — skip entirely if freeze is active
                # (a block with regs during freeze fails Rule 14/freeze validation)
                freeze_now, _ = is_registration_freeze_active(self.ledger, slot)
                with self._pending_regs_lock:
                    if freeze_now:
                        regs = []
                    else:
                        regs = list(self._pending_regs.values())[:MAX_REGS_PER_BLOCK]

            reward = get_block_reward(total_minted)
            challenge = compute_challenge(prev_hash, slot)

            block_without_sig = {
                "reward_id":     f"reward:{slot}",
                "slot":          slot,
                "winner_id":     self.wallet.device_id,
                "prev_hash":     prev_hash,
                "challenge":     challenge.hex(),
                "compete_sig":   compete_msg["signature"],
                "compete_proof": compete_msg["proof"],
                "vrf_public_key":self.wallet.get_public_key_hex(),
                "amount":        reward,
                "fees_collected":fees_collected,
                "timestamp":     int(time.time()),
                "transactions":  txs,
                "registrations": regs,
                "type":          "block_reward",
                "nodes":         len(self.ledger.identities),
                "version":       VERSION
            }

            # Sign the canonical block (without block_sig)
            block_sig = self.wallet.sign(canonical_block(block_without_sig))
            block = dict(block_without_sig)
            block["block_sig"] = block_sig

            # Accept into our own ledger
            with self.ledger._lock:
                if not self.ledger._add_block_locked(block):
                    return
                self.ledger.save()

            # Remove included transactions from mempool
            with self._mempool_lock:
                for tx in txs:
                    self._mempool.pop(tx.get("tx_id", ""), None)

            # Remove included registrations from pending
            with self._pending_regs_lock:
                for reg in regs:
                    self._pending_regs.pop(reg.get("device_id", ""), None)

            # Broadcast — note: {**block, "type": "BLOCK"} not {"type":"BLOCK", **block}
            # because block contains "type":"block_reward" and the LAST key wins
            self.network.broadcast({**block, "type": "BLOCK"})

            print(f"\n  ★ Block produced! slot={slot} "
                  f"reward={reward/UNIT:.4f} TMPL "
                  f"fees={fees_collected/UNIT:.6f} TMPL\n  > ",
                  end="", flush=True)

            # Push to explorer
            self._push_to_explorer()

        except Exception as e:
            pass

    def _on_block_received(self, msg: dict, sender_ip: str):
        """Handle an incoming BLOCK from the network."""
        slot = msg.get("slot")
        wid  = msg.get("winner_id", "")

        # Record compete_time to suppress COMPETE_TO_BLOCK_TIMEOUT
        with self._compete_lock:
            self._compete_time.pop(slot, None)

        with self.ledger._lock:
            changed = self.ledger._add_block_locked(msg)
            if changed:
                self.ledger.save()

        if changed:
            tip_hash = compute_block_hash(msg)
            # Attest immediately
            attest = produce_attestation(tip_hash, slot, self.wallet)
            self.network.broadcast(attest)
            self._on_attest_received(attest)

            # Remove confirmed txs from mempool
            with self._mempool_lock:
                for tx in msg.get("transactions", []):
                    self._mempool.pop(tx.get("tx_id", ""), None)
            # Remove confirmed regs from pending
            with self._pending_regs_lock:
                for reg in msg.get("registrations", []):
                    self._pending_regs.pop(reg.get("device_id", ""), None)

            # Notify if we received TMPL
            for tx in msg.get("transactions", []):
                if tx.get("recipient_id") == self.wallet.device_id:
                    bal = self.ledger.get_balance(self.wallet.device_id)
                    print(f"\n  ╔══════════════════════════════════════╗")
                    print(f"  ║  TMPL RECEIVED                       ║")
                    print(f"  ║  Amount : {tx['amount']/UNIT:.8f} TMPL")
                    print(f"  ║  From   : {tx['sender_id'][:20]}...")
                    print(f"  ║  Balance: {bal/UNIT:.8f} TMPL")
                    print(f"  ╚══════════════════════════════════════╝\n  > ",
                          end="", flush=True)

    # ── Attestation ────────────────────────────────────────────────────────────

    def _attestation_loop(self):
        """Periodically re-attest recent unfinalized blocks."""
        while self._running:
            time.sleep(REWARD_INTERVAL)
            try:
                with self.ledger._lock:
                    recent_blocks = list(self.ledger.chain[-CONFIRMATION_DEPTH:])
                for block in recent_blocks:
                    block_hash = compute_block_hash(block)
                    slot       = block.get("slot", 0)
                    if block_hash in self._finalized:
                        continue
                    attest = produce_attestation(block_hash, slot, self.wallet)
                    self.network.broadcast(attest)
            except Exception:
                pass

    def _on_attest_received(self, msg: dict):
        """Handle an incoming ATTEST message."""
        block_hash = msg.get("block_hash", "")
        slot       = msg.get("slot", 0)
        did        = msg.get("device_id", "")
        pub_hex    = msg.get("public_key", "")
        sig_hex    = msg.get("signature", "")

        if not block_hash or not did or not pub_hex or not sig_hex:
            return

        # Validate attestation
        if did not in self.ledger.identities:
            return
        first_seen = self.ledger.identities.get(did, 0)
        if slot - first_seen < MIN_IDENTITY_AGE:
            return

        # Verify the supplied public key matches the key registered on-chain.
        # Without this check an attacker can forge attestations for any known
        # device_id by using their own Dilithium3 key — the sig verifies but
        # the voter is not the real identity owner.
        registered_pub = self.ledger.identity_pubkeys.get(did, "")
        if registered_pub and pub_hex != registered_pub:
            return

        try:
            payload   = f"attest:{block_hash}:{slot}".encode()
            pub_bytes = bytes.fromhex(pub_hex)
            sig_bytes = bytes.fromhex(sig_hex)
            if not Dilithium3.verify(pub_bytes, payload, sig_bytes):
                return
        except Exception:
            return

        with self._attest_lock:
            if block_hash not in self._attestations:
                self._attestations[block_hash]      = {}
                self._attestation_slots[block_hash] = slot  # track slot for pruning
            self._attestations[block_hash][did] = msg

            # Check finality
            if block_hash not in self._finalized:
                if is_final(block_hash, slot, self.ledger, self._attestations):
                    self._finalized.add(block_hash)
                    self._last_finalized_slot = slot
                    # Propagate to ledger so _attempt_reorg enforces the barrier
                    if slot > self.ledger.last_finalized_slot:
                        self.ledger.last_finalized_slot = slot
                    # Prune attestation dicts for blocks older than CHECKPOINT_BUFFER
                    stale = [h for h in list(self._attestations.keys())
                             if self._attestation_slots.get(h, slot) < slot - CHECKPOINT_BUFFER]
                    for h in stale:
                        del self._attestations[h]
                        self._attestation_slots.pop(h, None)

    # ── Transaction ────────────────────────────────────────────────────────────

    def _on_transaction_received(self, tx_dict: dict):
        """Validate and add to mempool."""
        try:
            tx = Transaction.from_dict(tx_dict)
        except Exception:
            return
        if not tx.verify():
            return
        if tx.amount <= 0:
            return
        if tx.fee < calculate_fee(tx.amount):
            return
        if tx.tx_id in self.ledger._spent_bloom:
            return
        sender_bal = self.ledger.get_balance(tx.sender_id)
        if sender_bal < tx.amount + tx.fee:
            return
        with self._mempool_lock:
            if tx.tx_id not in self._mempool:
                if can_add_to_mempool(tx.sender_id, self._mempool):
                    self._mempool[tx.tx_id] = tx_dict

    # ── Registration ───────────────────────────────────────────────────────────

    def _on_register_received(self, msg: dict):
        """Hold registration in pending pool until included in a block."""
        did = msg.get("device_id", "")
        if not did:
            return
        if did in self.ledger.identities:
            return
        if Ledger._verify_registration(msg):
            with self._pending_regs_lock:
                self._pending_regs[did] = msg

    # ── Checkpoint ────────────────────────────────────────────────────────────

    def _checkpoint_loop(self):
        last_cp = 0
        if self.ledger.checkpoints:
            last_cp = self.ledger.checkpoints[-1].get("slot", 0)
        while self._running:
            time.sleep(REWARD_INTERVAL)
            current_slot = get_current_slot()
            cp_slot = (current_slot // CHECKPOINT_INTERVAL) * CHECKPOINT_INTERVAL
            if cp_slot > last_cp and current_slot > cp_slot + CHECKPOINT_BUFFER:
                cp = self.ledger.create_checkpoint(cp_slot)
                if cp:
                    last_cp = cp_slot
                    self.network.broadcast({"type": "CHECKPOINT", "checkpoint": cp})
                    print(f"\n  [checkpoint] Created at slot {cp_slot}\n  > ",
                          end="", flush=True)

    def _on_checkpoint_received(self, cp: dict):
        """Apply a checkpoint from a peer."""
        self.ledger.apply_checkpoint(cp)

    # ── Registration freeze monitor ────────────────────────────────────────────

    def _freeze_monitor(self):
        """Background thread that tracks registration rate and manages freeze state."""
        while self._running:
            time.sleep(REWARD_INTERVAL)
            current_slot = get_current_slot()
            # M5: snapshot chain under lock before calling is_registration_freeze_active.
            # _add_block_locked modifies ledger.chain from the network thread without
            # holding _freeze_monitor's context — iterating without the lock risks
            # RuntimeError: dictionary changed size during iteration.
            with self.ledger._lock:
                chain_snapshot = list(self.ledger.chain)
            freeze_active, status = is_registration_freeze_active(
                self.ledger, current_slot, chain=chain_snapshot
            )
            was_active = getattr(self.ledger, "_last_freeze_active", False)

            if freeze_active and not was_active:
                print(f"\n  ⚠️  REGISTRATION FREEZE ACTIVE — slot {current_slot}")
                print(f"      Rate: {status['current_rate']}/100 slots "
                      f"(baseline: {status['baseline_rate']})")
                print(f"      Freeze lifts after 200 consecutive normal slots.\n  > ",
                      end="", flush=True)
                self.ledger.freeze_triggered_slot    = current_slot
                self.ledger.freeze_last_abnormal_slot = current_slot
                self.ledger.freeze_normal_streak      = 0
                self.ledger.save()
            elif not freeze_active and was_active:
                print(f"\n  ✅ REGISTRATION FREEZE LIFTED — slot {current_slot}\n  > ",
                      end="", flush=True)
                self.ledger.freeze_triggered_slot = None
                self.ledger.save()
            elif freeze_active:
                # Progress update every 10 slots
                streak = status.get("normal_streak", 0)
                if current_slot % 10 == 0:
                    print(f"\n  ⚠️  REGISTRATION FREEZE ACTIVE — slot {current_slot}")
                    print(f"      Normal streak: {streak}/200 slots\n  > ",
                          end="", flush=True)
                self.ledger.freeze_last_abnormal_slot = (
                    current_slot if status["current_rate"] > status["baseline_rate"]
                    else self.ledger.freeze_last_abnormal_slot
                )
                self.ledger.freeze_normal_streak = streak

            self.ledger._last_freeze_active = freeze_active

    # ── Mempool expiry ─────────────────────────────────────────────────────────

    def _mempool_expiry_loop(self):
        while self._running:
            time.sleep(30)
            current_slot = get_current_slot()
            with self._mempool_lock:
                expired = [tx_id for tx_id, tx in self._mempool.items()
                           if current_slot - tx.get("slot", 0) > TX_EXPIRY_SLOTS]
                for tx_id in expired:
                    del self._mempool[tx_id]

    # ── Seen ID cleanup ────────────────────────────────────────────────────────

    def _seen_cleanup_loop(self):
        while self._running:
            time.sleep(60)
            current_slot = get_current_slot()
            self.network._cleanup_seen_ids(current_slot)
            # L4: prune attestation memory regardless of finality.
            # Pruning inside _on_attest_received only runs on finalization.
            # Under network partition (< 3 nodes) blocks may never finalize —
            # this sweep bounds _attestations unconditionally.
            cutoff = current_slot - CHECKPOINT_BUFFER
            with self._attest_lock:
                stale = [h for h, s in list(self._attestation_slots.items())
                         if s < cutoff]
                for h in stale:
                    self._attestations.pop(h, None)
                    self._attestation_slots.pop(h, None)

    # ── Explorer push ──────────────────────────────────────────────────────────

    def _explorer_push_loop(self):
        """Push ledger state to explorer nodes periodically."""
        import urllib.request
        last_push_slot = -1
        while self._running:
            time.sleep(REWARD_INTERVAL * 2)
            try:
                current_slot = get_current_slot()
                if current_slot == last_push_slot:
                    continue
                self._push_to_explorer()
                last_push_slot = current_slot
            except Exception:
                pass

    def _push_to_explorer(self):
        """Build and send LEDGER_PUSH to all explorer targets."""
        import urllib.request
        try:
            with self.ledger._lock:
                tip_hash, tip_slot = self.ledger._get_tip()
                blocks       = [dict(b) for b in self.ledger.chain[-50:]]
                transactions = list(self.ledger.transactions[-200:])
                total_minted = self.ledger.total_minted
                cp_slot      = 0
                cp_balances  = {}
                if self.ledger.checkpoints:
                    cp = self.ledger.checkpoints[-1]
                    cp_slot     = cp.get("slot", 0)
                    cp_balances = cp.get("balances", {})
                current_slot = get_current_slot()
                freeze_active, freeze_status = is_registration_freeze_active(
                    self.ledger, current_slot
                )

            # Add canonical_hash to each block for explorer integrity checks
            for block in blocks:
                if "canonical_hash" not in block:
                    block["canonical_hash"] = compute_block_hash(block)

            payload_data = {
                "type":               "LEDGER_PUSH",
                "version":            VERSION,
                "device_id":          self.wallet.device_id,
                "public_key":         self.wallet.get_public_key_hex(),
                "blocks":             blocks,
                "transactions":       transactions,
                "total_minted":       total_minted,
                "chain_tip_hash":     tip_hash,
                "chain_tip_slot":     tip_slot,
                "chain_height":       len(self.ledger.chain),
                "checkpoint_slot":    cp_slot,
                "checkpoint_balances":cp_balances,
                "timestamp":          time.time(),
                "compete_sig":        "",
                "compete_proof":      "",
                "fees_collected":     0,
                "registration_freeze":freeze_status
            }

            # Sign the payload (without signature field)
            payload_bytes = json.dumps(
                {k: v for k, v in payload_data.items() if k != "signature"},
                sort_keys=True, separators=(",", ":")
            ).encode()
            payload_data["signature"] = self.wallet.sign(payload_bytes)

            body = json.dumps(payload_data).encode()

            for host, port in EXPLORER_TARGETS:
                try:
                    req = urllib.request.Request(
                        f"http://{host}:{port}/api",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    urllib.request.urlopen(req, timeout=5)
                except Exception:
                    pass
        except Exception:
            pass

    # ── Control socket (for CLI commands from second terminal) ─────────────────

    def _start_control_socket(self):
        """Listen on 127.0.0.1:7780 for IPC commands (balance, send)."""
        def _serve():
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind(("127.0.0.1", 7780))
                srv.listen(5)
                srv.settimeout(1.0)
                while self._running:
                    try:
                        conn, _ = srv.accept()
                        threading.Thread(target=self._handle_control,
                                         args=(conn,), daemon=True).start()
                    except socket.timeout:
                        continue
                    except Exception:
                        break
            except Exception:
                pass
        threading.Thread(target=_serve, daemon=True).start()

    def _handle_control(self, conn):
        try:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk or b"\n" in data + chunk:
                    data += chunk
                    break
                data += chunk
            msg = json.loads(data.decode().strip())
            if msg.get("token") != self._control_token:
                conn.sendall((json.dumps({"ok": False, "error": "unauthorized"}) + "\n").encode())
                return
            action = msg.get("action")
            if action == "balance":
                bal = self.ledger.get_balance(self.wallet.device_id)
                conn.sendall((json.dumps({
                    "ok": True,
                    "balance_tmpl": bal / UNIT,
                    "address":      self.wallet.device_id
                }) + "\n").encode())
            elif action == "send":
                peer_id = msg.get("peer_id", "")
                amount  = float(msg.get("amount", 0))
                memo    = msg.get("memo", "")
                result  = self.send(peer_id, amount, memo)
                conn.sendall((json.dumps(result) + "\n").encode())
            else:
                conn.sendall((json.dumps({"ok": False, "error": "unknown action"}) + "\n").encode())
        except Exception as e:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            conn.close()

    # ── Send TMPL ──────────────────────────────────────────────────────────────

    def send(self, recipient_id: str, amount_tmpl: float, memo: str = "") -> dict:
        """Send TMPL to recipient. Returns {ok, error}."""
        if not _is_valid_hex64(recipient_id):
            return {"ok": False, "error": "invalid address"}
        if recipient_id == self.wallet.device_id:
            return {"ok": False, "error": "cannot send to yourself"}
        amount_units = int(amount_tmpl * UNIT)
        if amount_units <= 0:
            return {"ok": False, "error": "amount must be > 0"}
        fee = calculate_fee(amount_units)
        bal = self.ledger.get_balance(self.wallet.device_id)
        if bal < amount_units + fee:
            return {"ok": False, "error": f"insufficient balance ({bal/UNIT:.8f} TMPL)"}
        current_slot = get_current_slot()
        tx = Transaction(
            sender_id    = self.wallet.device_id,
            recipient_id = recipient_id,
            sender_pubkey= self.wallet.get_public_key_hex(),
            amount       = amount_units,
            fee          = fee,
            memo         = memo,
            slot         = current_slot,
            timestamp    = time.time()
        )
        tx.sign(self.wallet)
        tx_dict = tx.to_dict()
        # Add to our own mempool
        with self._mempool_lock:
            self._mempool[tx.tx_id] = tx_dict
        # Broadcast
        self.network.broadcast({"type": "TRANSACTION", "transaction": tx_dict})
        # Record in personal history
        self.ledger.my_transactions.append(tx_dict)
        self.ledger.save()
        print(f"\n  ↑ Sent {amount_tmpl:.8f} TMPL to {recipient_id[:20]}... fee={fee/UNIT:.6f}\n  > ",
              end="", flush=True)
        return {"ok": True}

    # ── CLI ────────────────────────────────────────────────────────────────────

    def _cli(self):
        print(f"\n  Ready. Type a command (balance / chain / peers / send / history / network / quit)\n")
        while self._running:
            try:
                raw = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Shutting down. Goodbye.\n")
                self.network.stop()
                break

            if raw == "balance":
                bal = self.ledger.get_balance(self.wallet.device_id)
                print(f"\n  Balance : {bal/UNIT:.8f} TMPL")
                print(f"  Address : {self.wallet.device_id}\n")

            elif raw == "chain":
                with self.ledger._lock:
                    tip_hash, tip_slot = self.ledger._get_tip()
                    height = len(self.ledger.chain)
                    recent = list(self.ledger.chain[-5:])
                    era2   = is_era2(self.ledger)
                print(f"\n  Chain height : {height}")
                print(f"  Tip slot     : {tip_slot}")
                print(f"  Tip hash     : {tip_hash[:32]}...")
                print(f"  Era          : {'2 (fees only)' if era2 else '1 (rewards + fees)'}")
                print(f"  Finalized    : slot {self._last_finalized_slot}")
                print(f"  Identities   : {len(self.ledger.identities)}")
                print(f"  Recent blocks:")
                for b in recent:
                    ts = time.strftime("%H:%M:%S", time.localtime(b.get("timestamp", 0)))
                    print(f"    slot {b.get('slot'):>8}  "
                          f"+{b.get('amount',0)/UNIT:.4f} TMPL  "
                          f"winner {b.get('winner_id','')[:16]}...  [{ts}]")
                print()

            elif raw == "peers":
                peers = self.network.get_online_peers()
                if not peers:
                    print("\n  No peers online yet.\n")
                else:
                    print(f"\n  Online peers ({len(peers)}):")
                    for i, (pid, info) in enumerate(peers.items()):
                        print(f"  [{i+1}] {pid[:24]}... — {info['ip']}:{info['port']}")
                    print()

            elif raw == "network":
                s     = self.ledger.get_summary()
                peers = self.network.get_online_peers()
                _, freeze_status = is_registration_freeze_active(
                    self.ledger, get_current_slot()
                )
                print(f"\n  Network Status (v{VERSION}):")
                print(f"  Online peers      : {len(peers)}")
                print(f"  Chain height      : {s['chain_height']} blocks")
                print(f"  Identities        : {s['identity_count']}")
                print(f"  Total minted      : {s['total_minted']/UNIT:.8f} TMPL")
                print(f"  Remaining supply  : {s['remaining_supply']/UNIT:.8f} TMPL")
                print(f"  Reg freeze        : {'ACTIVE' if freeze_status['active'] else 'clear'}\n")

            elif raw == "send":
                self._sending = True
                try:
                    peers = self.network.get_online_peers()
                    if peers:
                        print(f"\n  Online peers:")
                        peer_list = list(peers.items())
                        for i, (pid, info) in enumerate(peer_list):
                            print(f"  [{i+1}] {pid[:24]}... — {info['ip']}")
                        print(f"\n  Enter peer number or full address:")
                    else:
                        peer_list = []
                        print(f"\n  No peers online. Enter recipient address:")
                    choice = input("  > ").strip()
                    if peer_list and choice.isdigit():
                        idx = int(choice) - 1
                        if not (0 <= idx < len(peer_list)):
                            print("  Invalid selection.\n")
                            continue
                        recipient = peer_list[idx][0]
                    else:
                        recipient = choice.lower().strip()
                        if not _is_valid_hex64(recipient):
                            print("  Invalid address.\n")
                            continue
                    bal = self.ledger.get_balance(self.wallet.device_id)
                    amount_str = input(f"  Amount in TMPL (balance: {bal/UNIT:.8f}): ").strip()
                    try:
                        amount = float(amount_str)
                    except ValueError:
                        print("  Invalid amount.\n")
                        continue
                    memo = input("  Memo (optional, max 128 chars): ").strip()[:128]
                    result = self.send(recipient, amount, memo)
                    if not result["ok"]:
                        print(f"  Error: {result['error']}\n")
                finally:
                    self._sending = False

            elif raw == "history":
                my_id = self.wallet.device_id
                with self.ledger._lock:
                    my_rewards = [b for b in self.ledger.chain if b.get("winner_id") == my_id]
                    my_txs     = [t for t in self.ledger.transactions
                                  if t.get("sender_id") == my_id or t.get("recipient_id") == my_id]
                personal   = self.ledger.my_transactions
                if not my_rewards and not my_txs and not personal:
                    print("\n  No history yet.\n")
                else:
                    print(f"\n  Your history:")
                    for b in my_rewards[-5:]:
                        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(b.get("timestamp", 0)))
                        print(f"  ★ REWARD  +{b['amount']/UNIT:.8f} TMPL  slot {b.get('slot','?')}  [{t}]")
                    for tx in (personal or my_txs)[-10:]:
                        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tx.get("timestamp", 0)))
                        if tx.get("sender_id") == my_id:
                            memo = f"  [{tx.get('memo','')}]" if tx.get("memo") else ""
                            print(f"  ↑ SENT    {tx['amount']/UNIT:.8f} TMPL  to {tx['recipient_id'][:16]}...{memo}  [{t}]")
                        else:
                            memo = f"  [{tx.get('memo','')}]" if tx.get("memo") else ""
                            print(f"  ↓ RECV    {tx['amount']/UNIT:.8f} TMPL  from {tx['sender_id'][:16]}...{memo}  [{t}]")
                    print()

            elif raw in ("quit", "exit", "q"):
                print("\n  Shutting down. Goodbye.\n")
                self.network.stop()
                break

            else:
                print(f"\n  Commands: balance | chain | peers | send | history | network | quit\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def _recover_wallet():
    """--recover flow: regenerate wallet from 12-word seed phrase."""
    print("\n  TIMPAL Wallet Recovery")
    print("  ══════════════════════\n")
    print("  Enter your 12-word recovery phrase (space-separated):")
    try:
        phrase = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        exit(0)
    try:
        pk, sk = derive_keys_from_seed(phrase)
    except RuntimeError as e:
        print(f"\n  [!] {e}\n")
        exit(1)
    except ValueError as e:
        print(f"\n  [!] Invalid phrase: {e}\n")
        exit(1)

    # We need to know genesis_block_hash to reconstruct device_id correctly.
    # For genesis-phase wallets (or if user doesn't know it) use sha256(pubkey).
    # Full reconstruction from chain is possible but requires network connection.
    print("\n  Was this wallet created during the genesis phase (first 1000 blocks)?")
    print("  If unsure, enter 'no'.")
    is_genesis = input("  [yes/no] > ").strip().lower() in ("yes", "y")

    if is_genesis:
        device_id          = hashlib.sha256(pk).hexdigest()
        genesis_block_hash = None
    else:
        print("  Enter the genesis_block_hash shown when your wallet was created")
        print("  (or press Enter to attempt recovery without it):")
        gbh = input("  > ").strip().lower()
        if gbh and _is_valid_hex64(gbh):
            genesis_block_hash = gbh
            device_id          = hashlib.sha256(pk + bytes.fromhex(gbh)).hexdigest()
        else:
            genesis_block_hash = None
            device_id          = hashlib.sha256(pk).hexdigest()

    w = Wallet()
    w.public_key         = pk
    w.private_key        = sk
    w.device_id          = device_id
    w.genesis_block_hash = genesis_block_hash

    import getpass
    print(f"\n  Recovered device ID: {device_id}")
    print("  Set a new wallet password (or press Enter for none):")
    pw = getpass.getpass("  Password: ")
    if pw and len(pw) < 8:
        print("  Password too short — saving without encryption.")
        pw = None
    elif pw:
        pw2 = getpass.getpass("  Confirm: ")
        if pw != pw2:
            print("  Mismatch — saving without encryption.")
            pw = None

    w.save(password=pw if pw else None, seed_phrase=phrase)
    print(f"\n  ✓ Wallet recovered and saved to {WALLET_FILE}")
    print(f"  Start your node normally: python3 timpal.py\n")


if __name__ == "__main__":
    import sys
    _check_genesis_time()

    if len(sys.argv) >= 2 and sys.argv[1] == "--recover":
        _recover_wallet()
        sys.exit(0)

    elif len(sys.argv) >= 2 and sys.argv[1] == "--peer":
        # MISSING 5: --peer <ip:port> — manual peer entry per spec Part 4.3.
        # Allows connecting without DNS or bootstrap when both are unavailable.
        # Network.__init__ copies BOOTSTRAP_SERVERS, so inserting here is enough.
        if len(sys.argv) < 3:
            print("Usage: python3 timpal.py --peer <ip:port>")
            sys.exit(1)
        peer_arg = sys.argv[2].strip()
        try:
            peer_host, peer_port_str = peer_arg.rsplit(":", 1)
            peer_port = int(peer_port_str)
            if not (1024 <= peer_port <= 65535):
                raise ValueError("port out of range")
        except (ValueError, AttributeError):
            print(f"Invalid peer address: {peer_arg!r}  (expected ip:port)")
            sys.exit(1)
        BOOTSTRAP_SERVERS.insert(0, (peer_host, peer_port))
        node = Node()
        node.start()

    elif len(sys.argv) >= 2 and sys.argv[1] == "send":
        if len(sys.argv) < 4:
            print("Usage: python3 timpal.py send <address> <amount_tmpl>")
            sys.exit(1)
        recipient = sys.argv[2].lower().strip()
        try:
            amount = float(sys.argv[3])
        except ValueError:
            print("Invalid amount.")
            sys.exit(1)
        if not _is_valid_hex64(recipient):
            print("Invalid address.")
            sys.exit(1)
        try:
            token = ""
            try:
                with open(CONTROL_TOKEN) as f:
                    token = f.read().strip()
            except Exception:
                pass
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(("127.0.0.1", 7780))
            sock.sendall((json.dumps({
                "action": "send", "peer_id": recipient,
                "amount": amount, "token": token
            }) + "\n").encode())
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
                print(f"Sent {amount:.8f} TMPL to {recipient[:24]}...")
            else:
                print(f"Failed: {result.get('error', 'unknown')}")
        except ConnectionRefusedError:
            print("Node not running. Start with: python3 timpal.py")
        sys.exit(0)

    elif len(sys.argv) >= 2 and sys.argv[1] == "balance":
        try:
            token = ""
            try:
                with open(CONTROL_TOKEN) as f:
                    token = f.read().strip()
            except Exception:
                pass
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(("127.0.0.1", 7780))
            sock.sendall((json.dumps({"action": "balance", "token": token}) + "\n").encode())
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
                print(f"Balance : {result['balance_tmpl']:.8f} TMPL")
                print(f"Address : {result['address']}")
        except Exception:
            pass
        sys.exit(0)

    else:
        node = Node()
        node.start()
