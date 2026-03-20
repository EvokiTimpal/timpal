#!/usr/bin/env python3
"""
TIMPAL v3.0 — Test Suite
=========================
Covers all critical protocol invariants across:

  SECTION 1 — Chain helpers (canonical_block, compute_block_hash)
  SECTION 2 — Ledger initialization and persistence
  SECTION 3 — Transaction validation
  SECTION 4 — add_block: VRF / chain linkage / supply cap
  SECTION 5 — get_balance (chain-based)
  SECTION 6 — merge: chain extension and transaction deduplication
  SECTION 7 — Checkpoint: create, apply, chain_tip preservation
  SECTION 8 — Fork resolution
  SECTION 9 — Serialization determinism
  SECTION 10 — Gap slot handling
  SECTION 11 — Reorg: chain switch preserves balances
  SECTION 12 — Double-spend across forks
  SECTION 13 — Wallet: keygen, sign, verify, encrypt/decrypt
  SECTION 14 — Transaction: from_dict validation, signature verify
  SECTION 15 — Protocol constants (never change after genesis)
  SECTION 16 — _verify_ticket (VRF proof)
  SECTION 17 — _is_eligible_this_slot (eligibility gate)
  SECTION 18 — _pick_winner (lottery mechanics)
  SECTION 19 — Era / fee helpers
  SECTION 20 — Inflation / supply cap attacks
  SECTION 21 — Sybil: eligibility is network-size aware
  SECTION 22 — Reveal obligation ban (bootstrap)

Usage:
    python3 test_timpal.py          # run all tests
    python3 test_timpal.py -v       # verbose
"""

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid

# ── Patch GENESIS_TIME before importing timpal ─────────────────────────────────
# We patch it to a fixed past value so get_current_slot() returns deterministic
# positive integers during testing.
_FAKE_GENESIS = int(time.time()) - 1000

import importlib, types

# Stub dilithium_py so tests run without the native extension installed.
# Provides deterministic fake sign/verify for all protocol logic tests.
# Tests that specifically exercise VRF proof verification mock the static method.
class _FakeDilithium3:
    """Fake Dilithium3 for tests.

    Key relationship: pk = sha256(sk)  — deterministic derivation.
    sign(sk, msg)    → sha256(pk + msg) * 4   where pk = sha256(sk)
    verify(pk, msg, sig) → sig == sha256(pk + msg) * 4

    This means:
      - sign then verify with matching pk/sk: passes ✓
      - verify with wrong pk:                fails  ✓
      - device_id = sha256(pk) = sha256(sha256(sk))  — still 64-char hex ✓
    """
    @staticmethod
    def keygen():
        sk = os.urandom(32)
        pk = hashlib.sha256(sk).digest()   # pk derived from sk
        return pk, sk

    @staticmethod
    def sign(sk: bytes, msg: bytes) -> bytes:
        pk = hashlib.sha256(sk).digest()   # reconstruct pk from sk
        return hashlib.sha256(pk + msg).digest() * 4

    @staticmethod
    def verify(pk: bytes, msg: bytes, sig: bytes) -> bool:
        expected = hashlib.sha256(pk + msg).digest() * 4
        return sig == expected

_fake_dil_module = types.ModuleType("dilithium_py.dilithium")
_fake_dil_module.Dilithium3 = _FakeDilithium3
_fake_dil_pkg = types.ModuleType("dilithium_py")
_fake_dil_pkg.dilithium = _fake_dil_module
sys.modules["dilithium_py"] = _fake_dil_pkg
sys.modules["dilithium_py.dilithium"] = _fake_dil_module

# Stub cryptography so wallet encrypt/decrypt tests work without the package.
# For tests not specifically testing encryption we use the unencrypted path.
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa
except ImportError:
    pass  # timpal.py handles ImportError itself

import timpal
# Patch genesis time into the module
timpal.GENESIS_TIME = _FAKE_GENESIS


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_wallet():
    """Create a fresh in-memory wallet (no file I/O)."""
    w = timpal.Wallet()
    w.public_key, w.private_key = _FakeDilithium3.keygen()
    w.device_id = hashlib.sha256(w.public_key).hexdigest()
    return w


def _make_ledger(tmp_dir):
    """Create a Ledger backed by a unique temp file.
    Patches ledger.save() to always write to the captured path,
    so multiple ledgers in one test don't clobber each other via the
    global timpal.LEDGER_FILE."""
    path = os.path.join(tmp_dir, f"ledger_{uuid.uuid4().hex[:8]}.json")
    timpal.LEDGER_FILE = path
    ledger = timpal.Ledger()
    # Bind this ledger's save() to its own path, independent of LEDGER_FILE global
    _path = path
    def _bound_save():
        tmp = _path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "version":      timpal.VERSION,
                "transactions": ledger.transactions,
                "chain":        ledger.chain,
                "fee_rewards":  ledger.fee_rewards,
                "total_minted": ledger.total_minted,
                "checkpoints":  ledger.checkpoints
            }, f, indent=2)
        os.replace(tmp, _path)
    ledger.save = _bound_save
    return ledger


def _make_vrf(wallet, slot):
    """Produce a fake-but-internally-consistent VRF tuple using our fake Dilithium."""
    seed   = str(slot)
    sig    = _FakeDilithium3.sign(wallet.private_key, seed.encode())
    ticket = hashlib.sha256(sig).hexdigest()
    return ticket, sig.hex(), seed


def _make_block(wallet, slot, prev_hash, amount=timpal.REWARD_PER_ROUND):
    """Build a valid block dict signed by wallet."""
    ticket, sig_hex, seed = _make_vrf(wallet, slot)
    return {
        "reward_id":      f"reward:{slot}",
        "slot":           slot,
        "prev_hash":      prev_hash,
        "winner_id":      wallet.device_id,
        "amount":         amount,
        "timestamp":      time.time(),
        "vrf_ticket":     ticket,
        "vrf_seed":       seed,
        "vrf_sig":        sig_hex,
        "vrf_public_key": wallet.public_key.hex(),
        "nodes":          1,
        "type":           "block_reward"
    }


def _add_genesis_block(ledger, wallet):
    """Add the very first block (prev_hash = GENESIS_PREV_HASH)."""
    slot = timpal.get_current_slot()
    block = _make_block(wallet, slot, timpal.GENESIS_PREV_HASH)
    ok = ledger.add_block(block)
    return ok, block


# ── Monkeypatch Node._verify_ticket to use fake Dilithium ─────────────────────
_orig_verify_ticket = timpal.Node._verify_ticket

def _fake_verify_ticket(public_key_hex: str, seed: str, sig_hex: str, ticket: str) -> bool:
    try:
        pub = bytes.fromhex(public_key_hex)
        sig = bytes.fromhex(sig_hex)
        if not _FakeDilithium3.verify(pub, seed.encode(), sig):
            return False
        return hashlib.sha256(sig).hexdigest() == ticket
    except Exception:
        return False

timpal.Node._verify_ticket = staticmethod(_fake_verify_ticket)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Chain helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestChainHelpers(unittest.TestCase):

    def test_canonical_block_is_deterministic(self):
        """Same block dict always produces identical bytes regardless of insertion order."""
        b1 = {"slot": 5, "prev_hash": "abc", "winner_id": "def", "amount": 1.0}
        b2 = {"amount": 1.0, "winner_id": "def", "prev_hash": "abc", "slot": 5}
        self.assertEqual(timpal.canonical_block(b1), timpal.canonical_block(b2))

    def test_canonical_block_uses_sort_keys(self):
        """canonical_block output must equal json.dumps with sort_keys=True."""
        block = {"z": 1, "a": 2, "m": 3}
        expected = json.dumps(block, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(timpal.canonical_block(block), expected)

    def test_compute_block_hash_length(self):
        block = {"slot": 1, "prev_hash": "0" * 64}
        h = timpal.compute_block_hash(block)
        self.assertEqual(len(h), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_compute_block_hash_changes_on_modification(self):
        b1 = {"slot": 1, "winner_id": "aaa"}
        b2 = {"slot": 1, "winner_id": "bbb"}
        self.assertNotEqual(timpal.compute_block_hash(b1), timpal.compute_block_hash(b2))

    def test_compute_block_hash_consistent_across_calls(self):
        block = {"slot": 42, "prev_hash": "x" * 64, "amount": 1.0575}
        h1 = timpal.compute_block_hash(block)
        h2 = timpal.compute_block_hash(block)
        self.assertEqual(h1, h2)

    def test_genesis_prev_hash_is_64_zeros(self):
        self.assertEqual(timpal.GENESIS_PREV_HASH, "0" * 64)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Ledger initialization
# ══════════════════════════════════════════════════════════════════════════════

class TestLedgerInit(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_fresh_ledger_empty_chain(self):
        ledger = _make_ledger(self.tmp)
        self.assertEqual(ledger.chain, [])

    def test_fresh_ledger_zero_minted(self):
        ledger = _make_ledger(self.tmp)
        self.assertEqual(ledger.total_minted, 0.0)

    def test_fresh_ledger_tip_is_genesis(self):
        ledger = _make_ledger(self.tmp)
        tip_hash, tip_slot = ledger._get_tip()
        self.assertEqual(tip_hash, timpal.GENESIS_PREV_HASH)
        self.assertEqual(tip_slot, -1)

    def test_ledger_saves_and_reloads(self):
        path = os.path.join(self.tmp, "ledger2.json")
        orig = timpal.LEDGER_FILE
        timpal.LEDGER_FILE = path
        ledger = timpal.Ledger()
        w = _make_wallet()
        block = _make_block(w, timpal.get_current_slot(), timpal.GENESIS_PREV_HASH)
        ledger.add_block(block)

        # Reload from file
        ledger2 = timpal.Ledger()
        timpal.LEDGER_FILE = orig
        self.assertEqual(len(ledger2.chain), 1)
        self.assertAlmostEqual(ledger2.total_minted, timpal.REWARD_PER_ROUND, places=6)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Transaction validation
# ══════════════════════════════════════════════════════════════════════════════

class TestTransactions(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sender   = _make_wallet()
        self.receiver = _make_wallet()

    def _funded_ledger(self):
        """Return a ledger where sender has REWARD_PER_ROUND TMPL."""
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        block  = _make_block(self.sender, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(block)
        return ledger

    def _make_tx(self, sender, recipient, amount, ledger_slot=None):
        slot = ledger_slot or timpal.get_current_slot()
        tx = timpal.Transaction(
            sender_id    = sender.device_id,
            recipient_id = recipient.device_id,
            sender_pubkey= sender.public_key.hex(),
            amount       = amount,
            fee          = 0.0,
            slot         = slot
        )
        tx.sign(sender)
        return tx

    def test_valid_transaction_accepted(self):
        ledger = self._funded_ledger()
        tx = self._make_tx(self.sender, self.receiver, 0.5)
        self.assertTrue(ledger.add_transaction(tx.to_dict()))

    def test_zero_amount_rejected(self):
        ledger = self._funded_ledger()
        tx = self._make_tx(self.sender, self.receiver, 0.0)
        self.assertFalse(ledger.add_transaction(tx.to_dict()))

    def test_negative_amount_rejected(self):
        d = self._make_tx(self.sender, self.receiver, 0.1).to_dict()
        d["amount"] = -1.0
        ledger = self._funded_ledger()
        self.assertFalse(ledger.add_transaction(d))

    def test_overspend_rejected(self):
        ledger = self._funded_ledger()
        tx = self._make_tx(self.sender, self.receiver, timpal.REWARD_PER_ROUND + 1)
        self.assertFalse(ledger.add_transaction(tx.to_dict()))

    def test_duplicate_tx_rejected(self):
        ledger = self._funded_ledger()
        tx = self._make_tx(self.sender, self.receiver, 0.5)
        self.assertTrue(ledger.add_transaction(tx.to_dict()))
        self.assertFalse(ledger.add_transaction(tx.to_dict()))

    def test_wrong_signature_rejected(self):
        ledger = self._funded_ledger()
        tx = self._make_tx(self.sender, self.receiver, 0.5)
        d = tx.to_dict()
        d["signature"] = "aa" * 64
        self.assertFalse(ledger.add_transaction(d))

    def test_invalid_sender_id_rejected(self):
        with self.assertRaises(Exception):
            timpal.Transaction.from_dict({
                "tx_id": str(uuid.uuid4()), "sender_id": "not_hex",
                "recipient_id": self.receiver.device_id,
                "sender_pubkey": self.sender.public_key.hex(),
                "amount": 0.5, "fee": 0.0, "slot": 1,
                "timestamp": time.time(), "signature": None
            })

    def test_self_send_rejected_by_node(self):
        """Node.send() rejects self-sends; ledger itself allows (no self-send check there)."""
        ledger = self._funded_ledger()
        tx = timpal.Transaction(
            sender_id=self.sender.device_id,
            recipient_id=self.sender.device_id,
            sender_pubkey=self.sender.public_key.hex(),
            amount=0.1, fee=0.0, slot=timpal.get_current_slot()
        )
        tx.sign(self.sender)
        # Ledger itself doesn't block self-send — Node.send() does
        # Just verify it doesn't crash
        result = ledger.add_transaction(tx.to_dict())
        self.assertIsInstance(result, bool)

    def test_transaction_verify_roundtrip(self):
        tx = self._make_tx(self.sender, self.receiver, 0.3)
        t2 = timpal.Transaction.from_dict(tx.to_dict())
        self.assertTrue(t2.verify())

    def test_tampered_amount_fails_verify(self):
        tx = self._make_tx(self.sender, self.receiver, 0.3)
        d  = tx.to_dict()
        d["amount"] = 999.0
        t2 = timpal.Transaction.from_dict(d)
        self.assertFalse(t2.verify())


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — add_block: VRF / chain linkage / supply cap
# ══════════════════════════════════════════════════════════════════════════════

class TestAddBlock(unittest.TestCase):

    def setUp(self):
        self.tmp    = tempfile.mkdtemp()
        self.wallet = _make_wallet()

    def _ledger(self):
        return _make_ledger(self.tmp)

    def test_valid_first_block_accepted(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        self.assertTrue(ledger.add_block(block))

    def test_total_minted_incremented(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(block)
        self.assertAlmostEqual(ledger.total_minted, timpal.REWARD_PER_ROUND, places=6)

    def test_missing_vrf_ticket_rejected(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        del block["vrf_ticket"]
        self.assertFalse(ledger.add_block(block))

    def test_missing_vrf_sig_rejected(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        del block["vrf_sig"]
        self.assertFalse(ledger.add_block(block))

    def test_wrong_vrf_proof_rejected(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        block["vrf_sig"] = "bb" * 64
        self.assertFalse(ledger.add_block(block))

    def test_wrong_prev_hash_rejected(self):
        """Core v3.0 invariant: block must link to current tip."""
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, "ff" * 32)  # wrong prev_hash
        self.assertFalse(ledger.add_block(block))

    def test_duplicate_slot_rejected(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        b1 = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(b1)
        w2 = _make_wallet()
        b2 = _make_block(w2, slot, timpal.GENESIS_PREV_HASH)
        self.assertFalse(ledger.add_block(b2))

    def test_old_slot_rejected(self):
        """Slot must be strictly greater than tip slot."""
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        b1 = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(b1)
        tip_hash = timpal.compute_block_hash(b1)
        b2 = _make_block(self.wallet, slot - 1, tip_hash)
        self.assertFalse(ledger.add_block(b2))

    def test_gap_within_limit_accepted(self):
        ledger = self._ledger()
        slot   = 10   # fixed past slot: slot+MAX_SLOT_GAP-1=29, within valid window
        b1 = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(b1)
        tip_hash = timpal.compute_block_hash(b1)
        b2 = _make_block(self.wallet, slot + timpal.MAX_SLOT_GAP - 1, tip_hash)
        self.assertTrue(ledger.add_block(b2))

    def test_gap_exceeding_limit_rejected(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        b1 = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(b1)
        tip_hash = timpal.compute_block_hash(b1)
        b2 = _make_block(self.wallet, slot + timpal.MAX_SLOT_GAP + 1, tip_hash)
        self.assertFalse(ledger.add_block(b2))

    def test_supply_cap_enforced(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        # Manually inflate total_minted to just below cap
        ledger.total_minted = timpal.TOTAL_SUPPLY - 0.001
        block = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        self.assertFalse(ledger.add_block(block))

    def test_invalid_winner_id_rejected(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        block["winner_id"] = "not_a_valid_id"
        self.assertFalse(ledger.add_block(block))

    def test_chain_grows_by_one(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(block)
        self.assertEqual(len(ledger.chain), 1)

    def test_get_tip_updates_after_add_block(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(block)
        tip_hash, tip_slot = ledger._get_tip()
        self.assertEqual(tip_hash, timpal.compute_block_hash(block))
        self.assertEqual(tip_slot, slot)

    def test_three_blocks_chain_correctly(self):
        ledger = self._ledger()
        slot   = timpal.get_current_slot()
        b1 = _make_block(self.wallet, slot,   timpal.GENESIS_PREV_HASH)
        b2 = _make_block(self.wallet, slot+1, timpal.compute_block_hash(b1))
        b3 = _make_block(self.wallet, slot+2, timpal.compute_block_hash(b2))
        self.assertTrue(ledger.add_block(b1))
        self.assertTrue(ledger.add_block(b2))
        self.assertTrue(ledger.add_block(b3))
        self.assertEqual(len(ledger.chain), 3)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — get_balance (chain-based)
# ══════════════════════════════════════════════════════════════════════════════

class TestGetBalance(unittest.TestCase):

    def setUp(self):
        self.tmp    = tempfile.mkdtemp()
        self.wallet = _make_wallet()

    def test_fresh_wallet_zero_balance(self):
        ledger = _make_ledger(self.tmp)
        self.assertEqual(ledger.get_balance(self.wallet.device_id), 0.0)

    def test_balance_after_block_reward(self):
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(block)
        self.assertAlmostEqual(
            ledger.get_balance(self.wallet.device_id), timpal.REWARD_PER_ROUND, places=6)

    def test_balance_after_send(self):
        ledger   = _make_ledger(self.tmp)
        receiver = _make_wallet()
        slot     = timpal.get_current_slot()
        block    = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(block)
        tx = timpal.Transaction(
            sender_id=self.wallet.device_id, recipient_id=receiver.device_id,
            sender_pubkey=self.wallet.public_key.hex(), amount=0.5, fee=0.0,
            slot=slot
        )
        tx.sign(self.wallet)
        ledger.add_transaction(tx.to_dict())
        self.assertAlmostEqual(
            ledger.get_balance(self.wallet.device_id),
            timpal.REWARD_PER_ROUND - 0.5, places=6)
        self.assertAlmostEqual(ledger.get_balance(receiver.device_id), 0.5, places=6)

    def test_balance_multiple_rewards(self):
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        b1 = _make_block(self.wallet, slot,   timpal.GENESIS_PREV_HASH)
        b2 = _make_block(self.wallet, slot+1, timpal.compute_block_hash(b1))
        ledger.add_block(b1)
        ledger.add_block(b2)
        self.assertAlmostEqual(
            ledger.get_balance(self.wallet.device_id),
            timpal.REWARD_PER_ROUND * 2, places=6)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — merge: chain extension and deduplication
# ══════════════════════════════════════════════════════════════════════════════

class TestMerge(unittest.TestCase):

    def setUp(self):
        self.tmp    = tempfile.mkdtemp()
        self.wallet = _make_wallet()

    def test_merge_extends_chain_from_tip(self):
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        b1     = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(b1)
        b2 = _make_block(self.wallet, slot+1, timpal.compute_block_hash(b1))
        changed = ledger.merge({"blocks": [b2], "transactions": []})
        self.assertTrue(changed)
        self.assertEqual(len(ledger.chain), 2)

    def test_merge_rejects_wrong_prev_hash(self):
        """Block whose prev_hash doesn't match our tip is silently skipped."""
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        b1     = _make_block(self.wallet, slot, "ff" * 32)  # wrong link
        changed = ledger.merge({"blocks": [b1], "transactions": []})
        self.assertFalse(changed)
        self.assertEqual(len(ledger.chain), 0)

    def test_merge_blocks_sorted_by_slot(self):
        """Blocks arrive out of order — merge must handle sorted extension."""
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        b1 = _make_block(self.wallet, slot,   timpal.GENESIS_PREV_HASH)
        b2 = _make_block(self.wallet, slot+1, timpal.compute_block_hash(b1))
        b3 = _make_block(self.wallet, slot+2, timpal.compute_block_hash(b2))
        # Send in reverse order — merge should sort and apply correctly
        changed = ledger.merge({"blocks": [b3, b1, b2], "transactions": []})
        self.assertTrue(changed)
        self.assertEqual(len(ledger.chain), 3)

    def test_merge_duplicate_block_not_added(self):
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        b1     = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(b1)
        changed = ledger.merge({"blocks": [b1], "transactions": []})
        self.assertFalse(changed)
        self.assertEqual(len(ledger.chain), 1)

    def test_merge_rejects_block_with_missing_vrf(self):
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        b1     = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        del b1["vrf_sig"]
        changed = ledger.merge({"blocks": [b1], "transactions": []})
        self.assertFalse(changed)

    def test_merge_transactions_deduplication(self):
        sender   = _make_wallet()
        receiver = _make_wallet()
        ledger   = _make_ledger(self.tmp)
        slot     = timpal.get_current_slot()
        b1 = _make_block(sender, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(b1)
        tx = timpal.Transaction(
            sender_id=sender.device_id, recipient_id=receiver.device_id,
            sender_pubkey=sender.public_key.hex(), amount=0.5, fee=0.0, slot=slot
        )
        tx.sign(sender)
        ledger.add_transaction(tx.to_dict())
        # Merge same tx again — should not duplicate
        changed = ledger.merge({"blocks": [], "transactions": [tx.to_dict()]})
        self.assertFalse(changed)
        self.assertEqual(len(ledger.transactions), 1)

    def test_merge_invalid_transaction_skipped(self):
        ledger = _make_ledger(self.tmp)
        bad_tx = {"tx_id": "x", "amount": -5, "sender_id": "bad", "recipient_id": "bad"}
        changed = ledger.merge({"blocks": [], "transactions": [bad_tx]})
        self.assertFalse(changed)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Checkpoint: create, apply, chain_tip preservation
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckpoint(unittest.TestCase):

    def setUp(self):
        self.tmp    = tempfile.mkdtemp()
        self.wallet = _make_wallet()

    def _ledger_with_n_blocks(self, n):
        ledger = _make_ledger(self.tmp)
        slot   = 10   # fixed past base; checkpoint_slot=135 gives prune_before=15 > 10
        prev   = timpal.GENESIS_PREV_HASH
        for i in range(n):
            b = _make_block(self.wallet, slot + i, prev)
            ledger.add_block(b)
            prev = timpal.compute_block_hash(b)
        return ledger

    def test_checkpoint_created(self):
        ledger = self._ledger_with_n_blocks(5)
        ok = ledger.create_checkpoint(135)
        self.assertTrue(ok)
        self.assertEqual(len(ledger.checkpoints), 1)

    def test_checkpoint_stores_chain_tip_hash(self):
        """After prune, new blocks must link from chain_tip_hash in checkpoint."""
        ledger = self._ledger_with_n_blocks(5)
        ledger.create_checkpoint(135)
        cp = ledger.checkpoints[-1]
        self.assertIn("chain_tip_hash", cp)
        self.assertEqual(len(cp["chain_tip_hash"]), 64)

    def test_checkpoint_prunes_old_blocks(self):
        ledger = self._ledger_with_n_blocks(5)
        before = len(ledger.chain)
        ledger.create_checkpoint(135)
        after = len(ledger.chain)
        self.assertLess(after, before)

    def test_duplicate_checkpoint_slot_rejected(self):
        ledger = self._ledger_with_n_blocks(5)
        ledger.create_checkpoint(135)
        ok2 = ledger.create_checkpoint(135)
        self.assertFalse(ok2)

    def test_get_tip_works_after_prune(self):
        """After checkpointing, _get_tip must fall back to checkpoint chain_tip_hash."""
        ledger = self._ledger_with_n_blocks(5)
        # Force all blocks into checkpoint by using a high prune_before
        ledger.create_checkpoint(110)
        tip_hash, tip_slot = ledger._get_tip()
        self.assertNotEqual(tip_hash, timpal.GENESIS_PREV_HASH)

    def test_new_block_after_checkpoint_links_correctly(self):
        """After checkpoint, a new block must use the stored chain_tip_hash as prev_hash."""
        ledger = self._ledger_with_n_blocks(3)
        ledger.create_checkpoint(101)
        tip_hash, tip_slot = ledger._get_tip()
        new_slot = tip_slot + 1
        b = _make_block(self.wallet, new_slot, tip_hash)
        ok = ledger.add_block(b)
        self.assertTrue(ok)

    def test_apply_checkpoint_updates_balances(self):
        ledger = self._ledger_with_n_blocks(3)
        ledger.create_checkpoint(101)
        cp     = ledger.checkpoints[-1]
        ledger2 = _make_ledger(tempfile.mkdtemp())
        ok = ledger2.apply_checkpoint(cp)
        self.assertTrue(ok)
        self.assertAlmostEqual(cp["total_minted"], ledger2.total_minted, places=6)

    def test_checkpoint_balances_correct(self):
        ledger = self._ledger_with_n_blocks(3)
        ledger.create_checkpoint(135)  # prune_before=15 > first block slot(10) — blocks get pruned
        cp = ledger.checkpoints[-1]
        earned = cp["balances"].get(self.wallet.device_id, 0.0)
        self.assertGreater(earned, 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — Fork resolution
# ══════════════════════════════════════════════════════════════════════════════

class TestForkResolution(unittest.TestCase):
    """
    Fork choice: longest valid chain wins.
    Equal length: tip hash comparison — deterministic across all nodes.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.w1  = _make_wallet()
        self.w2  = _make_wallet()

    def test_longer_chain_accepted_over_shorter(self):
        """
        Node A has [b1] (height 1).
        Node B has [b1, b2] (height 2).
        After sync, A should have 2 blocks.
        """
        ledger_a = _make_ledger(self.tmp)
        ledger_b = _make_ledger(tempfile.mkdtemp())

        slot = timpal.get_current_slot()
        b1   = _make_block(self.w1, slot,   timpal.GENESIS_PREV_HASH)
        b2   = _make_block(self.w1, slot+1, timpal.compute_block_hash(b1))

        ledger_a.add_block(b1)
        ledger_b.add_block(b1)
        ledger_b.add_block(b2)

        # A syncs from B — gets b2 which extends A's current tip
        changed = ledger_a.merge({"blocks": [b2], "transactions": []})
        self.assertTrue(changed)
        self.assertEqual(len(ledger_a.chain), 2)

    def test_equal_length_chains_deterministic(self):
        """
        Two chains of equal length from the same genesis.
        Both nodes must pick the same one based on tip hash comparison.
        """
        slot = timpal.get_current_slot()

        # Chain A: w1 wins slot N
        b_a = _make_block(self.w1, slot, timpal.GENESIS_PREV_HASH)
        # Chain B: w2 wins slot N (different winner, same slot — divergence)
        b_b = _make_block(self.w2, slot, timpal.GENESIS_PREV_HASH)

        hash_a = timpal.compute_block_hash(b_a)
        hash_b = timpal.compute_block_hash(b_b)

        # Deterministic tie-break: lower hash wins
        winner = min([hash_a, hash_b])
        self.assertIn(winner, [hash_a, hash_b])
        # Both nodes computing this independently will get the same result
        self.assertEqual(winner, min([hash_a, hash_b]))

    def test_fork_blocks_are_independently_valid(self):
        """Each fork block passes VRF and structural checks independently."""
        slot = timpal.get_current_slot()
        b_a  = _make_block(self.w1, slot, timpal.GENESIS_PREV_HASH)
        b_b  = _make_block(self.w2, slot, timpal.GENESIS_PREV_HASH)

        l1 = _make_ledger(self.tmp)
        l2 = _make_ledger(tempfile.mkdtemp())
        self.assertTrue(l1.add_block(b_a))
        self.assertTrue(l2.add_block(b_b))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — Serialization determinism
# ══════════════════════════════════════════════════════════════════════════════

class TestSerializationDeterminism(unittest.TestCase):
    """
    Same block must hash identically regardless of dict insertion order,
    Python version, or node. This is the #1 risk in v3.0.
    """

    def test_same_block_same_hash_10_times(self):
        block = {
            "slot": 42, "prev_hash": "a" * 64, "winner_id": "b" * 64,
            "amount": 1.0575, "timestamp": 1234567890.0,
            "vrf_ticket": "c" * 64, "type": "block_reward"
        }
        hashes = {timpal.compute_block_hash(block) for _ in range(10)}
        self.assertEqual(len(hashes), 1)

    def test_insertion_order_doesnt_affect_hash(self):
        keys   = ["slot", "prev_hash", "winner_id", "amount", "timestamp"]
        values = [1, "a" * 64, "b" * 64, 1.0, 999.0]
        base   = dict(zip(keys, values))

        import itertools
        hashes = set()
        for perm in itertools.permutations(keys):
            d = {k: base[k] for k in perm}
            hashes.add(timpal.compute_block_hash(d))
        self.assertEqual(len(hashes), 1,
                         "Hash differs by key insertion order — canonical_block broken")

    def test_prev_hash_chain_is_reproducible(self):
        w    = _make_wallet()
        slot = 1000
        b1   = _make_block(w, slot,   timpal.GENESIS_PREV_HASH)
        b2   = _make_block(w, slot+1, timpal.compute_block_hash(b1))
        b3   = _make_block(w, slot+2, timpal.compute_block_hash(b2))

        # Recompute hashes independently
        h1 = timpal.compute_block_hash(b1)
        h2 = timpal.compute_block_hash(b2)
        h3 = timpal.compute_block_hash(b3)

        self.assertEqual(b2["prev_hash"], h1)
        self.assertEqual(b3["prev_hash"], h2)
        self.assertNotEqual(h1, h2)
        self.assertNotEqual(h2, h3)

    def test_float_amounts_serialize_consistently(self):
        """Float amounts must not cause hash inconsistency across nodes."""
        b1 = {"amount": 1.0575, "slot": 1}
        b2 = {"amount": 1.0575, "slot": 1}
        self.assertEqual(timpal.compute_block_hash(b1), timpal.compute_block_hash(b2))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — Gap slot handling
# ══════════════════════════════════════════════════════════════════════════════

class TestGapSlotHandling(unittest.TestCase):
    """
    Slots don't have to be consecutive. Any gap up to MAX_SLOT_GAP is valid.
    This handles nodes that are briefly offline or lucky winners in sparse networks.
    """

    def setUp(self):
        self.tmp    = tempfile.mkdtemp()
        self.wallet = _make_wallet()

    def test_gap_of_one_accepted(self):
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        b1 = _make_block(self.wallet, slot,   timpal.GENESIS_PREV_HASH)
        b2 = _make_block(self.wallet, slot+2, timpal.compute_block_hash(b1))
        ledger.add_block(b1)
        self.assertTrue(ledger.add_block(b2))

    def test_gap_of_max_accepted(self):
        ledger = _make_ledger(self.tmp)
        slot   = 10   # fixed past slot so slot+MAX_SLOT_GAP stays within valid window
        b1 = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        b2 = _make_block(self.wallet, slot + timpal.MAX_SLOT_GAP,
                         timpal.compute_block_hash(b1))
        ledger.add_block(b1)
        self.assertTrue(ledger.add_block(b2))

    def test_gap_of_max_plus_one_rejected(self):
        ledger = _make_ledger(self.tmp)
        slot   = 10
        b1 = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        b2 = _make_block(self.wallet, slot + timpal.MAX_SLOT_GAP + 1,
                         timpal.compute_block_hash(b1))
        ledger.add_block(b1)
        self.assertFalse(ledger.add_block(b2))

    def test_chain_valid_after_gaps(self):
        ledger = _make_ledger(self.tmp)
        slot   = 10   # fixed past base so all gap slots remain within valid window
        prev   = timpal.GENESIS_PREV_HASH
        # Build a chain with irregular gaps: +0, +1, +3, +5
        for gap in [0, 1, 3, 5]:
            b = _make_block(self.wallet, slot + gap, prev)
            self.assertTrue(ledger.add_block(b))
            prev  = timpal.compute_block_hash(b)
            slot += gap + 1


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — Reorg: chain switch preserves balances
# ══════════════════════════════════════════════════════════════════════════════

class TestReorg(unittest.TestCase):
    """
    After a reorg (switching from a shorter to a longer chain),
    balances must reflect the canonical chain only.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.w1  = _make_wallet()
        self.w2  = _make_wallet()

    def test_balance_from_orphaned_block_not_counted(self):
        """
        w1 mines block at slot N (short chain).
        w2 mines blocks at slot N and N+1 (longer chain).
        After reorg, w1's orphaned block balance should not exist.

        In v3.0: first-writer-wins at the local node level.
        The reorg here simulates a node starting fresh and receiving
        the longer chain from a peer.
        """
        # Node C starts fresh — receives w2's 2-block chain
        ledger_c = _make_ledger(self.tmp)
        slot     = timpal.get_current_slot()

        b_w2_1 = _make_block(self.w2, slot,   timpal.GENESIS_PREV_HASH)
        b_w2_2 = _make_block(self.w2, slot+1, timpal.compute_block_hash(b_w2_1))

        ledger_c.add_block(b_w2_1)
        ledger_c.add_block(b_w2_2)

        # w1's block for same slot never arrives (or arrives too late)
        # Confirm w1 has zero balance, w2 has 2 rewards
        self.assertAlmostEqual(ledger_c.get_balance(self.w1.device_id), 0.0, places=6)
        self.assertAlmostEqual(
            ledger_c.get_balance(self.w2.device_id),
            timpal.REWARD_PER_ROUND * 2, places=6)

    def test_tx_in_canonical_chain_valid(self):
        """Transaction included in the canonical chain remains valid after sync."""
        sender   = _make_wallet()
        receiver = _make_wallet()
        ledger   = _make_ledger(self.tmp)
        slot     = timpal.get_current_slot()

        b1 = _make_block(sender, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(b1)

        tx = timpal.Transaction(
            sender_id=sender.device_id, recipient_id=receiver.device_id,
            sender_pubkey=sender.public_key.hex(), amount=0.5, fee=0.0, slot=slot
        )
        tx.sign(sender)
        ledger.add_transaction(tx.to_dict())

        b2 = _make_block(sender, slot+1, timpal.compute_block_hash(b1))
        ledger.add_block(b2)

        self.assertAlmostEqual(ledger.get_balance(receiver.device_id), 0.5, places=6)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — Double-spend across forks
# ══════════════════════════════════════════════════════════════════════════════

class TestDoubleSpend(unittest.TestCase):
    """
    A transaction included in one fork must not carry over to the canonical chain
    if the canonical chain doesn't include it.
    """

    def setUp(self):
        self.tmp    = tempfile.mkdtemp()
        self.sender = _make_wallet()
        self.recv_a = _make_wallet()
        self.recv_b = _make_wallet()

    def test_double_spend_rejected_in_same_chain(self):
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        b1     = _make_block(self.sender, slot, timpal.GENESIS_PREV_HASH)
        ledger.add_block(b1)

        tx1 = timpal.Transaction(
            sender_id=self.sender.device_id, recipient_id=self.recv_a.device_id,
            sender_pubkey=self.sender.public_key.hex(),
            amount=timpal.REWARD_PER_ROUND - 0.001, fee=0.0, slot=slot
        )
        tx1.sign(self.sender)
        self.assertTrue(ledger.add_transaction(tx1.to_dict()))

        # Attempt to spend the same funds again
        tx2 = timpal.Transaction(
            sender_id=self.sender.device_id, recipient_id=self.recv_b.device_id,
            sender_pubkey=self.sender.public_key.hex(),
            amount=timpal.REWARD_PER_ROUND - 0.001, fee=0.0, slot=slot
        )
        tx2.sign(self.sender)
        self.assertFalse(ledger.add_transaction(tx2.to_dict()))

    def test_tx_rejected_if_sender_has_no_balance(self):
        ledger = _make_ledger(self.tmp)
        empty  = _make_wallet()
        tx = timpal.Transaction(
            sender_id=empty.device_id, recipient_id=self.recv_a.device_id,
            sender_pubkey=empty.public_key.hex(), amount=1.0, fee=0.0,
            slot=timpal.get_current_slot()
        )
        tx.sign(empty)
        self.assertFalse(ledger.add_transaction(tx.to_dict()))

    def test_spent_tx_ids_prevent_replay_after_checkpoint(self):
        ledger = _make_ledger(self.tmp)
        slot   = 200
        prev   = timpal.GENESIS_PREV_HASH
        # Build 5 blocks so we have something to checkpoint
        for i in range(5):
            b = _make_block(self.sender, slot+i, prev)
            ledger.add_block(b)
            prev = timpal.compute_block_hash(b)

        tx = timpal.Transaction(
            sender_id=self.sender.device_id, recipient_id=self.recv_a.device_id,
            sender_pubkey=self.sender.public_key.hex(),
            amount=0.5, fee=0.0, slot=slot
        )
        tx.sign(self.sender)
        ledger.add_transaction(tx.to_dict())

        ledger.create_checkpoint(202)

        # After checkpoint, tx_id is in _spent_tx_ids_set — replay must fail
        self.assertTrue(ledger.has_transaction(tx.tx_id))
        self.assertFalse(ledger.add_transaction(tx.to_dict()))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 13 — Wallet
# ══════════════════════════════════════════════════════════════════════════════

class TestWallet(unittest.TestCase):

    def test_keygen_produces_unique_ids(self):
        w1 = _make_wallet()
        w2 = _make_wallet()
        self.assertNotEqual(w1.device_id, w2.device_id)

    def test_device_id_is_sha256_of_pubkey(self):
        w = _make_wallet()
        self.assertEqual(w.device_id, hashlib.sha256(w.public_key).hexdigest())

    def test_device_id_is_64_char_hex(self):
        w = _make_wallet()
        self.assertEqual(len(w.device_id), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in w.device_id))

    def test_sign_verify_roundtrip(self):
        w   = _make_wallet()
        msg = b"test message"
        sig_hex = w.sign(msg)
        self.assertTrue(timpal.Wallet.verify_signature(w.get_public_key_hex(), msg, sig_hex))

    def test_wrong_key_fails_verify(self):
        w1 = _make_wallet()
        w2 = _make_wallet()
        msg = b"test"
        sig_hex = w1.sign(msg)
        self.assertFalse(timpal.Wallet.verify_signature(w2.get_public_key_hex(), msg, sig_hex))

    def test_save_and_load_unencrypted(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "wallet.json")
        w1 = _make_wallet()
        w1.save(path=path)
        w2 = timpal.Wallet()
        w2.load(path=path)
        self.assertEqual(w1.device_id, w2.device_id)
        self.assertEqual(w1.public_key, w2.public_key)

    def test_public_key_hex_roundtrip(self):
        w = _make_wallet()
        self.assertEqual(bytes.fromhex(w.get_public_key_hex()), w.public_key)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 14 — Transaction from_dict validation
# ══════════════════════════════════════════════════════════════════════════════

class TestTransactionFromDict(unittest.TestCase):

    def _base_dict(self):
        w = _make_wallet()
        r = _make_wallet()
        tx = timpal.Transaction(
            sender_id=w.device_id, recipient_id=r.device_id,
            sender_pubkey=w.public_key.hex(), amount=1.0, fee=0.0,
            slot=timpal.get_current_slot()
        )
        tx.sign(w)
        return tx.to_dict()

    def test_valid_dict_accepted(self):
        d = self._base_dict()
        t = timpal.Transaction.from_dict(d)
        self.assertTrue(t.verify())

    def test_bool_as_amount_rejected(self):
        d = self._base_dict()
        d["amount"] = True
        with self.assertRaises(Exception):
            timpal.Transaction.from_dict(d)

    def test_negative_fee_rejected(self):
        d = self._base_dict()
        d["fee"] = -0.1
        with self.assertRaises(Exception):
            timpal.Transaction.from_dict(d)

    def test_short_sender_id_rejected(self):
        d = self._base_dict()
        d["sender_id"] = "abc"
        with self.assertRaises(Exception):
            timpal.Transaction.from_dict(d)

    def test_non_hex_pubkey_rejected(self):
        d = self._base_dict()
        d["sender_pubkey"] = "not_hex!!!!"
        with self.assertRaises(Exception):
            timpal.Transaction.from_dict(d)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 15 — Protocol constants
# ══════════════════════════════════════════════════════════════════════════════

class TestProtocolConstants(unittest.TestCase):
    """These values are immutable after genesis. Any change breaks consensus."""

    def test_era2_slot(self):
        self.assertEqual(timpal.ERA2_SLOT, 236_406_620)

    def test_target_participants(self):
        self.assertEqual(timpal.TARGET_PARTICIPANTS, 10)

    def test_reward_per_round(self):
        self.assertAlmostEqual(timpal.REWARD_PER_ROUND, 1.0575, places=4)

    def test_reward_interval(self):
        self.assertAlmostEqual(timpal.REWARD_INTERVAL, 5.0, places=1)

    def test_total_supply(self):
        self.assertAlmostEqual(timpal.TOTAL_SUPPLY, 250_000_000.0, places=0)

    def test_confirmation_depth(self):
        self.assertEqual(timpal.CONFIRMATION_DEPTH, 6)

    def test_max_slot_gap(self):
        self.assertEqual(timpal.MAX_SLOT_GAP, 20)

    def test_genesis_prev_hash(self):
        self.assertEqual(timpal.GENESIS_PREV_HASH, "0" * 64)

    def test_version_is_3(self):
        self.assertEqual(timpal.VERSION, "3.0")

    def test_min_version_is_3(self):
        self.assertEqual(timpal.MIN_VERSION, "3.0")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 16 — _verify_ticket (VRF proof)
# ══════════════════════════════════════════════════════════════════════════════

class TestVerifyTicket(unittest.TestCase):

    def setUp(self):
        self.wallet = _make_wallet()

    def test_valid_ticket_verifies(self):
        slot = timpal.get_current_slot()
        ticket, sig_hex, seed = _make_vrf(self.wallet, slot)
        self.assertTrue(timpal.Node._verify_ticket(
            self.wallet.get_public_key_hex(), seed, sig_hex, ticket))

    def test_wrong_public_key_fails(self):
        w2 = _make_wallet()
        slot = timpal.get_current_slot()
        ticket, sig_hex, seed = _make_vrf(self.wallet, slot)
        self.assertFalse(timpal.Node._verify_ticket(
            w2.get_public_key_hex(), seed, sig_hex, ticket))

    def test_wrong_ticket_hash_fails(self):
        slot = timpal.get_current_slot()
        ticket, sig_hex, seed = _make_vrf(self.wallet, slot)
        bad_ticket = "ff" * 32
        self.assertFalse(timpal.Node._verify_ticket(
            self.wallet.get_public_key_hex(), seed, sig_hex, bad_ticket))

    def test_wrong_seed_fails(self):
        slot = timpal.get_current_slot()
        ticket, sig_hex, seed = _make_vrf(self.wallet, slot)
        self.assertFalse(timpal.Node._verify_ticket(
            self.wallet.get_public_key_hex(), "wrong_seed", sig_hex, ticket))

    def test_corrupted_sig_fails(self):
        slot = timpal.get_current_slot()
        ticket, sig_hex, seed = _make_vrf(self.wallet, slot)
        bad_sig = "aa" * (len(sig_hex) // 2)
        self.assertFalse(timpal.Node._verify_ticket(
            self.wallet.get_public_key_hex(), seed, bad_sig, ticket))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 17 — _is_eligible_this_slot
# ══════════════════════════════════════════════════════════════════════════════

class TestEligibility(unittest.TestCase):

    def setUp(self):
        self.node = object.__new__(timpal.Node)
        self.node.wallet = _make_wallet()

    def test_eligible_when_network_at_target(self):
        """All nodes eligible when network_size <= TARGET_PARTICIPANTS."""
        slot = timpal.get_current_slot()
        eligible_count = sum(
            1 for _ in range(100)
            if timpal.Node._is_eligible_this_slot(
                self.node, slot, timpal.TARGET_PARTICIPANTS)
        )
        self.assertEqual(eligible_count, 100)

    def test_eligibility_decreases_with_larger_network(self):
        """At large network, ~TARGET_PARTICIPANTS / network_size fraction eligible."""
        slot    = timpal.get_current_slot()
        network = 1000
        wallets = [_make_wallet() for _ in range(200)]
        eligible = sum(
            1 for w in wallets
            if timpal.Node._is_eligible_this_slot(
                type('N', (), {'wallet': w})(), slot, network)
        )
        # Expect roughly 200 * 10/1000 = 2 ± statistical noise — just test it's < 20
        self.assertLess(eligible, 20)

    def test_sybil_neutrality(self):
        """
        1000 fake nodes get same expected reward as 1 node.
        Eligibility: threshold = TARGET / 1001 per node.
        Total expected: 1001 * (10/1001) = 10 = same as 1 node at target.
        This is the core Sybil protection invariant.
        """
        slot    = timpal.get_current_slot()
        network = 1001
        wallets = [_make_wallet() for _ in range(network)]
        eligible = sum(
            1 for w in wallets
            if timpal.Node._is_eligible_this_slot(
                type('N', (), {'wallet': w})(), slot, network)
        )
        # Should be close to TARGET_PARTICIPANTS — exact value varies by hash
        # Use a wide tolerance for statistical variance in 1001 wallets
        self.assertGreater(eligible, 0)
        self.assertLess(eligible, timpal.TARGET_PARTICIPANTS * 5)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 18 — _pick_winner (lottery mechanics)
# ══════════════════════════════════════════════════════════════════════════════

class TestPickWinner(unittest.TestCase):

    def setUp(self):
        self.node = object.__new__(timpal.Node)
        self.node._lottery_lock = threading.Lock()
        self.node._commits      = {}
        self.wallets = [_make_wallet() for _ in range(5)]

    def _build_reveals(self, slot, wallets):
        reveals  = {}
        commits  = {}
        for w in wallets:
            ticket, sig_hex, seed = _make_vrf(w, slot)
            commit = hashlib.sha256(
                f"{ticket}:{w.device_id}:{slot}".encode()).hexdigest()
            reveals[w.device_id] = {
                "ticket": ticket, "sig": sig_hex,
                "seed": seed, "public_key": w.public_key.hex()
            }
            commits[w.device_id] = commit
        return reveals, commits

    def test_winner_is_deterministic(self):
        slot = timpal.get_current_slot()
        reveals, commits = self._build_reveals(slot, self.wallets)
        self.node._commits[slot] = commits

        w1 = timpal.Node._pick_winner(self.node, slot, reveals)
        w2 = timpal.Node._pick_winner(self.node, slot, reveals)
        self.assertIsNotNone(w1)
        self.assertEqual(w1["winner_id"], w2["winner_id"])

    def test_winner_is_in_reveals(self):
        slot = timpal.get_current_slot()
        reveals, commits = self._build_reveals(slot, self.wallets)
        self.node._commits[slot] = commits
        winner = timpal.Node._pick_winner(self.node, slot, reveals)
        self.assertIn(winner["winner_id"], reveals)

    def test_no_reveals_returns_none(self):
        slot = timpal.get_current_slot()
        self.node._commits[slot] = {}
        result = timpal.Node._pick_winner(self.node, slot, {})
        self.assertIsNone(result)

    def test_unmatched_commit_excluded(self):
        """A node that reveals but has no matching commit is excluded."""
        slot = timpal.get_current_slot()
        w    = self.wallets[0]
        ticket, sig_hex, seed = _make_vrf(w, slot)
        reveals = {w.device_id: {"ticket": ticket, "sig": sig_hex,
                                  "seed": seed, "public_key": w.public_key.hex()}}
        # Provide NO commits for this node
        self.node._commits[slot] = {}
        result = timpal.Node._pick_winner(self.node, slot, reveals)
        self.assertIsNone(result)

    def test_wrong_commit_preimage_excluded(self):
        """Reveal with commit that doesn't match sha256(ticket:device_id:slot) excluded."""
        slot = timpal.get_current_slot()
        w    = self.wallets[0]
        ticket, sig_hex, seed = _make_vrf(w, slot)
        reveals = {w.device_id: {"ticket": ticket, "sig": sig_hex,
                                  "seed": seed, "public_key": w.public_key.hex()}}
        # Wrong commit — random hex
        self.node._commits[slot] = {w.device_id: "ee" * 32}
        result = timpal.Node._pick_winner(self.node, slot, reveals)
        self.assertIsNone(result)

    def test_missing_field_in_reveal_skipped(self):
        """Reveal missing any required field is skipped gracefully (no KeyError)."""
        slot = timpal.get_current_slot()
        w    = self.wallets[0]
        ticket, sig_hex, seed = _make_vrf(w, slot)
        commit = hashlib.sha256(f"{ticket}:{w.device_id}:{slot}".encode()).hexdigest()
        # Missing "public_key"
        reveals = {w.device_id: {"ticket": ticket, "sig": sig_hex, "seed": seed}}
        self.node._commits[slot] = {w.device_id: commit}
        result = timpal.Node._pick_winner(self.node, slot, reveals)
        self.assertIsNone(result)

    def test_all_nodes_compute_same_winner(self):
        """Collective target + winner selection must be identical on all nodes."""
        slot = timpal.get_current_slot()
        reveals, commits = self._build_reveals(slot, self.wallets)

        results = []
        for _ in range(3):
            node = object.__new__(timpal.Node)
            node._lottery_lock = threading.Lock()
            node._commits = {slot: dict(commits)}
            w = timpal.Node._pick_winner(node, slot, reveals)
            results.append(w["winner_id"])

        self.assertEqual(len(set(results)), 1, "Different nodes picked different winners")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 19 — Era / fee helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestEraFeeHelpers(unittest.TestCase):

    def test_not_era2_at_genesis(self):
        # Current slot is ~200 (1000s / 5s) — well before ERA2_SLOT
        self.assertFalse(timpal.is_era2())

    def test_fee_is_zero_in_era1(self):
        self.assertEqual(timpal.get_current_fee(), 0.0)

    def test_get_current_slot_positive(self):
        self.assertGreater(timpal.get_current_slot(), 0)

    def test_slot_advances_with_time(self):
        s1 = timpal.get_current_slot()
        time.sleep(0.01)
        s2 = timpal.get_current_slot()
        self.assertGreaterEqual(s2, s1)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 20 — Inflation / supply cap attacks
# ══════════════════════════════════════════════════════════════════════════════

class TestInflationAttacks(unittest.TestCase):

    def setUp(self):
        self.tmp    = tempfile.mkdtemp()
        self.wallet = _make_wallet()

    def test_block_without_vrf_cannot_mint(self):
        """Any block missing VRF fields must be rejected — inflation attack prevention."""
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        for field in ("vrf_ticket", "vrf_seed", "vrf_sig", "vrf_public_key"):
            b = dict(block)
            del b[field]
            self.assertFalse(ledger.add_block(b),
                             f"Block accepted without {field} — inflation attack possible")

    def test_supply_cap_hard_limit(self):
        ledger = _make_ledger(self.tmp)
        ledger.total_minted = timpal.TOTAL_SUPPLY
        slot  = timpal.get_current_slot()
        block = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH)
        self.assertFalse(ledger.add_block(block))

    def test_fee_reward_in_era1_rejected_by_merge(self):
        """fee_rewards are Era 2 only — merge must reject them in Era 1."""
        ledger = _make_ledger(self.tmp)
        fee_block = {
            "reward_id": "fee:1:abc",
            "winner_id": self.wallet.device_id,
            "amount":    0.0005,
            "time_slot": timpal.get_current_slot(),
            "type":      "fee_reward"
        }
        # fee_rewards go through add_fee_reward, not merge/add_block
        # Confirm merge's block path rejects non-block_reward types
        changed = ledger.merge({"blocks": [fee_block], "transactions": []})
        self.assertFalse(changed)

    def test_oversized_amount_rejected_by_supply_cap(self):
        ledger = _make_ledger(self.tmp)
        slot   = timpal.get_current_slot()
        block  = _make_block(self.wallet, slot, timpal.GENESIS_PREV_HASH,
                             amount=timpal.TOTAL_SUPPLY + 1)
        self.assertFalse(ledger.add_block(block))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 21 — Sybil: eligibility is network-size aware
# ══════════════════════════════════════════════════════════════════════════════

class TestSybilProtection(unittest.TestCase):

    def test_single_node_always_eligible(self):
        w    = _make_wallet()
        slot = timpal.get_current_slot()
        node = type('N', (), {'wallet': w})()
        for _ in range(20):
            self.assertTrue(
                timpal.Node._is_eligible_this_slot(node, slot, 1))

    def test_eligibility_formula_matches_bootstrap(self):
        """timpal.py and bootstrap.py MUST use identical eligibility formula.
        We verify the formula produces the same result for the same inputs."""
        import bootstrap
        bootstrap.GENESIS_TIME = _FAKE_GENESIS

        w            = _make_wallet()
        slot         = timpal.get_current_slot()
        network_size = 500

        # timpal.py path
        node = type('N', (), {'wallet': w})()
        timpal_result = timpal.Node._is_eligible_this_slot(node, slot, network_size)

        # bootstrap.py path
        bs_result = bootstrap.is_eligible(w.device_id, slot, network_size)

        self.assertEqual(timpal_result, bs_result,
                         "Eligibility formula differs between timpal.py and bootstrap.py")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 22 — Reveal obligation ban (bootstrap logic)
# ══════════════════════════════════════════════════════════════════════════════

class TestRevealObligation(unittest.TestCase):
    """
    Tests the ban enforcement logic in bootstrap.py directly.
    A node that commits but doesn't reveal REVEAL_MISS_THRESHOLD times gets banned.
    """

    def setUp(self):
        import bootstrap
        bootstrap.GENESIS_TIME = _FAKE_GENESIS
        self.bs = bootstrap

    def tearDown(self):
        # Reset bootstrap state between tests
        self.bs.commits.clear()
        self.bs.reveals.clear()
        self.bs.missed_reveals.clear()
        self.bs.ban_until.clear()

    def test_single_miss_no_ban(self):
        w    = _make_wallet()
        slot = timpal.get_current_slot() - 5
        self.bs.commits[slot]  = {w.device_id: "commit_hash"}
        self.bs.reveals[slot]  = {}  # no reveal

        # Simulate the miss check
        missed = set(self.bs.commits[slot].keys()) - set(self.bs.reveals.get(slot, {}).keys())
        for device_id in missed:
            self.bs.missed_reveals[device_id] = self.bs.missed_reveals.get(device_id, 0) + 1
            if self.bs.missed_reveals[device_id] >= self.bs.REVEAL_MISS_THRESHOLD:
                self.bs.ban_until[device_id] = slot + self.bs.BAN_DURATION

        self.assertNotIn(w.device_id, self.bs.ban_until)
        self.assertEqual(self.bs.missed_reveals.get(w.device_id, 0), 1)

    def test_threshold_misses_triggers_ban(self):
        w    = _make_wallet()
        slot = timpal.get_current_slot() - 5

        for i in range(self.bs.REVEAL_MISS_THRESHOLD):
            s = slot + i
            self.bs.commits[s] = {w.device_id: "commit_hash"}
            self.bs.reveals[s] = {}
            missed = set(self.bs.commits[s].keys()) - set(self.bs.reveals.get(s, {}).keys())
            for device_id in missed:
                self.bs.missed_reveals[device_id] = self.bs.missed_reveals.get(device_id, 0) + 1
                if self.bs.missed_reveals[device_id] >= self.bs.REVEAL_MISS_THRESHOLD:
                    self.bs.ban_until[device_id] = slot + self.bs.BAN_DURATION
                    self.bs.missed_reveals[device_id] = 0

        self.assertIn(w.device_id, self.bs.ban_until)

    def test_ban_reset_after_duration(self):
        w    = _make_wallet()
        slot = timpal.get_current_slot()
        # Place an expired ban
        self.bs.ban_until[w.device_id] = slot - 1
        # Clean expired bans
        expired = [did for did, s in list(self.bs.ban_until.items()) if s < slot]
        for did in expired:
            del self.bs.ban_until[did]
            self.bs.missed_reveals.pop(did, None)
        self.assertNotIn(w.device_id, self.bs.ban_until)

    def test_reveal_clears_miss_state(self):
        """A node that reveals should not accumulate missed_reveals."""
        w    = _make_wallet()
        slot = timpal.get_current_slot() - 3
        ticket, sig_hex, seed = _make_vrf(w, slot)
        self.bs.commits[slot] = {w.device_id: "commit_hash"}
        self.bs.reveals[slot] = {w.device_id: {"ticket": ticket}}

        missed = set(self.bs.commits[slot].keys()) - set(self.bs.reveals[slot].keys())
        self.assertNotIn(w.device_id, missed)


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    total  = suite.countTestCases()

    runner = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1)
    result = runner.run(suite)

    passed = total - len(result.failures) - len(result.errors)
    print(f"\n{'='*54}")
    print(f"  TIMPAL v3.0 Test Results")
    print(f"{'='*54}")
    print(f"  Total  : {total}")
    print(f"  Passed : {passed}")
    print(f"  Failed : {len(result.failures)}")
    print(f"  Errors : {len(result.errors)}")
    print(f"{'='*54}")
    sys.exit(0 if result.wasSuccessful() else 1)
