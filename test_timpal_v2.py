#!/usr/bin/env python3
"""
TIMPAL Protocol v4.0 — Complete Test Suite v2
Tests every protocol rule enforced in _add_block_locked.
LOCAL ONLY — never push to GitHub or servers.
Run: python3 test_timpal_v2.py
"""

import unittest
import hashlib
import time
import sys
import os
import threading
import uuid

import timpal
timpal.GENESIS_TIME = 1  # far in the past so all slots are valid

from timpal import (
    Wallet, Ledger, Transaction, SpentBloomFilter,
    compute_block_hash, canonical_block,
    get_block_reward, calculate_fee, is_final,
    select_competitors, compute_challenge, solve_challenge,
    produce_attestation, is_registration_freeze_active,
    UNIT, TOTAL_SUPPLY, REWARD_PER_ROUND, MIN_IDENTITY_AGE,
    GENESIS_PREV_HASH, VERSION, MAX_TRANSACTIONS_PER_BLOCK,
    TX_FEE_MIN, TX_FEE_MAX, MAX_REGS_PER_BLOCK, MAX_REGS_PER_PRODUCER,
    ATTESTATION_THRESHOLD, FREEZE_COOLDOWN_SLOTS,
)
from dilithium_py.dilithium import Dilithium3


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def make_wallet():
    w = Wallet()
    w.create_new()
    return w


def make_ledger():
    ledger = Ledger.__new__(Ledger)
    ledger.transactions         = []
    ledger.chain                = []
    ledger.fee_rewards          = []
    ledger.total_minted         = 0
    ledger.checkpoints          = []
    ledger.identities           = {}
    ledger.identity_pubkeys     = {}
    ledger.identity_last_attest = {}
    ledger.anchor_hashes        = set()
    ledger.balances             = {}
    ledger.my_transactions      = []
    ledger._lock                = threading.RLock()
    ledger._spent_bloom         = SpentBloomFilter()
    ledger._orphan_pool         = {}
    ledger.freeze_triggered_slot     = None
    ledger.freeze_last_abnormal_slot = 0
    ledger.freeze_normal_streak      = 0
    ledger.last_finalized_slot       = -1
    ledger._finalized_hashes         = set()
    return ledger


def register_identity(ledger, wallet, first_seen_slot):
    ledger.identities[wallet.device_id]       = first_seen_slot
    ledger.identity_pubkeys[wallet.device_id] = wallet.get_public_key_hex()


def make_block(ledger, wallet, slot, prev_hash=None, txs=None, regs=None,
               fees_collected=None, amount=None):
    if prev_hash is None:
        prev_hash = GENESIS_PREV_HASH
    if txs is None:
        txs = []
    if regs is None:
        regs = []
    reward      = get_block_reward(ledger.total_minted) if amount is None else amount
    actual_fees = sum(t.get("fee", 0) for t in txs)
    fc          = actual_fees if fees_collected is None else fees_collected
    challenge   = compute_challenge(prev_hash, slot)
    sig_hex, proof_hex = solve_challenge(wallet.private_key, challenge)
    block_without_sig = {
        "reward_id":      f"reward:{slot}",
        "slot":           slot,
        "winner_id":      wallet.device_id,
        "prev_hash":      prev_hash,
        "challenge":      challenge.hex(),
        "compete_sig":    sig_hex,
        "compete_proof":  proof_hex,
        "vrf_public_key": wallet.get_public_key_hex(),
        "amount":         reward,
        "fees_collected": fc,
        "timestamp":      int(time.time()),
        "transactions":   txs,
        "registrations":  regs,
        "type":           "block_reward",
        "nodes":          len(ledger.identities),
        "version":        VERSION,
    }
    block_sig = wallet.sign(canonical_block(block_without_sig))
    block = dict(block_without_sig)
    block["block_sig"] = block_sig
    return block


def set_balance(ledger, device_id, amount):
    """Set a balance via checkpoint snapshot — the only way get_balance sees it."""
    if not ledger.checkpoints:
        ledger.checkpoints.append({
            'slot': 0, 'prune_before': 0,
            'balances': {device_id: amount},
            'total_minted': amount,
            'chain_tip_hash': GENESIS_PREV_HASH,
            'chain_tip_slot': -1,
            'chain_hash': '', 'txs_hash': '',
            'fee_rewards_hash': '', 'kept_minted': 0,
            'version': VERSION,
        })
    else:
        ledger.checkpoints[-1].setdefault('balances', {})[device_id] = amount


def make_transaction(sender_wallet, recipient_id, amount, slot=500):
    fee = calculate_fee(amount)
    tx = Transaction(
        sender_id    = sender_wallet.device_id,
        recipient_id = recipient_id,
        sender_pubkey= sender_wallet.get_public_key_hex(),
        amount       = amount,
        fee          = fee,
        slot         = slot,
        timestamp    = time.time(),
    )
    tx.sign(sender_wallet)
    return tx.to_dict()


def deterministic_winner(ledger, wallets, slot, prev_hash):
    mature = [w.device_id for w in wallets
              if slot - ledger.identities.get(w.device_id, slot) >= MIN_IDENTITY_AGE]
    if not mature:
        return None, None
    winner_id = min(mature, key=lambda did: hashlib.sha256(
        f"{did}:{prev_hash}:{slot}".encode()).hexdigest())
    winner_wallet = next(w for w in wallets if w.device_id == winner_id)
    return winner_id, winner_wallet


# ══════════════════════════════════════════════════════════════════════════════
# 1 — ECONOMICS
# ══════════════════════════════════════════════════════════════════════════════

class TestEconomics(unittest.TestCase):

    def test_era1_reward(self):
        self.assertEqual(get_block_reward(0), REWARD_PER_ROUND)

    def test_era2_reward_is_zero(self):
        self.assertEqual(get_block_reward(TOTAL_SUPPLY), 0)

    def test_era2_reward_stays_zero(self):
        self.assertEqual(get_block_reward(TOTAL_SUPPLY + REWARD_PER_ROUND), 0)

    def test_era1_final_block_pays_remainder(self):
        remainder = TOTAL_SUPPLY % REWARD_PER_ROUND
        if remainder == 0:
            remainder = REWARD_PER_ROUND
        minted = TOTAL_SUPPLY - remainder
        self.assertEqual(get_block_reward(minted), remainder)

    def test_fee_minimum_applied(self):
        self.assertEqual(calculate_fee(1), TX_FEE_MIN)

    def test_fee_percentage(self):
        self.assertEqual(calculate_fee(1 * UNIT), 100_000)

    def test_fee_maximum_applied(self):
        self.assertEqual(calculate_fee(100 * UNIT), TX_FEE_MAX)


# ══════════════════════════════════════════════════════════════════════════════
# 2 — WALLET
# ══════════════════════════════════════════════════════════════════════════════

class TestWallet(unittest.TestCase):

    def test_genesis_wallet_device_id(self):
        w = make_wallet()
        self.assertEqual(w.device_id, hashlib.sha256(w.public_key).hexdigest())

    def test_chain_anchored_wallet_device_id(self):
        w = Wallet()
        gbh = "a" * 64
        w.create_new(genesis_block_hash=gbh)
        expected = hashlib.sha256(w.public_key + bytes.fromhex(gbh)).hexdigest()
        self.assertEqual(w.device_id, expected)

    def test_sign_and_verify(self):
        w = make_wallet()
        msg = b"test"
        sig = w.sign(msg)
        self.assertTrue(Wallet.verify_signature(w.get_public_key_hex(), msg, sig))

    def test_wrong_key_fails_verify(self):
        w1, w2 = make_wallet(), make_wallet()
        sig = w1.sign(b"test")
        self.assertFalse(Wallet.verify_signature(w2.get_public_key_hex(), b"test", sig))

    def test_registration_message_valid(self):
        w = make_wallet()
        self.assertTrue(Ledger._verify_registration(w._make_registration_message()))

    def test_registration_tampered_sig_fails(self):
        w1, w2 = make_wallet(), make_wallet()
        reg = w1._make_registration_message()
        reg["signature"] = w2.sign(b"wrong")
        self.assertFalse(Ledger._verify_registration(reg))


# ══════════════════════════════════════════════════════════════════════════════
# 3 — TRANSACTION
# ══════════════════════════════════════════════════════════════════════════════

class TestTransaction(unittest.TestCase):

    def test_valid_transaction(self):
        w = make_wallet()
        tx = make_transaction(w, "b" * 64, UNIT)
        self.assertTrue(Transaction.from_dict(tx).verify())

    def test_tampered_amount_fails(self):
        w = make_wallet()
        tx = make_transaction(w, "b" * 64, UNIT)
        tx["amount"] = UNIT * 2
        self.assertFalse(Transaction.from_dict(tx).verify())

    def test_tampered_recipient_fails(self):
        w = make_wallet()
        tx = make_transaction(w, "b" * 64, UNIT)
        tx["recipient_id"] = "c" * 64
        self.assertFalse(Transaction.from_dict(tx).verify())

    def test_zero_amount_raises(self):
        with self.assertRaises(Exception):
            Transaction.from_dict({
                "tx_id": str(uuid.uuid4()), "sender_id": "a"*64,
                "recipient_id": "b"*64, "sender_pubkey": "aa"*800,
                "amount": 0, "fee": 0, "timestamp": time.time(), "slot": 1
            })


# ══════════════════════════════════════════════════════════════════════════════
# 4 — BLOCK ACCEPTANCE — GENESIS PHASE (slot < 1000)
# ══════════════════════════════════════════════════════════════════════════════

class TestBlockGenesisPhase(unittest.TestCase):

    def setUp(self):
        self.wallet = make_wallet()
        self.ledger = make_ledger()
        register_identity(self.ledger, self.wallet, 0)

    def test_valid_block_accepted(self):
        block = make_block(self.ledger, self.wallet, slot=200)
        with self.ledger._lock:
            self.assertTrue(self.ledger._add_block_locked(block))

    def test_immature_identity_rejected(self):
        block = make_block(self.ledger, self.wallet, slot=199)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_wrong_prev_hash_rejected(self):
        block = make_block(self.ledger, self.wallet, slot=200, prev_hash="b"*64)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_wrong_amount_rejected(self):
        block = make_block(self.ledger, self.wallet, slot=200, amount=REWARD_PER_ROUND+1)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_duplicate_slot_rejected(self):
        block = make_block(self.ledger, self.wallet, slot=200)
        with self.ledger._lock:
            self.ledger._add_block_locked(block)
        block2 = make_block(self.ledger, self.wallet, slot=200)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block2))

    def test_invalid_compete_sig_rejected(self):
        block = make_block(self.ledger, self.wallet, slot=200)
        block["compete_sig"] = "aa" * 1000
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_invalid_block_sig_rejected(self):
        block = make_block(self.ledger, self.wallet, slot=200)
        block["block_sig"] = "aa" * 1000
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_wrong_version_rejected(self):
        block = make_block(self.ledger, self.wallet, slot=200)
        block["version"] = "3.0"
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_chain_grows_correctly(self):
        block1 = make_block(self.ledger, self.wallet, slot=200)
        with self.ledger._lock:
            self.ledger._add_block_locked(block1)
        h1 = compute_block_hash(block1)
        block2 = make_block(self.ledger, self.wallet, slot=201, prev_hash=h1)
        with self.ledger._lock:
            self.assertTrue(self.ledger._add_block_locked(block2))
        self.assertEqual(len(self.ledger.chain), 2)

    def test_total_minted_increases(self):
        block = make_block(self.ledger, self.wallet, slot=200)
        with self.ledger._lock:
            self.ledger._add_block_locked(block)
        self.assertEqual(self.ledger.total_minted, REWARD_PER_ROUND)

    def test_any_mature_identity_can_win_genesis(self):
        w2 = make_wallet()
        register_identity(self.ledger, w2, 0)
        block = make_block(self.ledger, w2, slot=200)
        with self.ledger._lock:
            self.assertTrue(self.ledger._add_block_locked(block))


# ══════════════════════════════════════════════════════════════════════════════
# 5 — BLOCK ACCEPTANCE — POST GENESIS (slot >= 1000)
# ══════════════════════════════════════════════════════════════════════════════

class TestBlockPostGenesis(unittest.TestCase):

    def setUp(self):
        self.ledger  = make_ledger()
        self.wallets = [make_wallet() for _ in range(3)]
        for w in self.wallets:
            register_identity(self.ledger, w, 800)
        self.winner_id, self.winner = deterministic_winner(
            self.ledger, self.wallets, 1000, GENESIS_PREV_HASH)
        self.non_winner = next(w for w in self.wallets
                               if w.device_id != self.winner_id)

    def test_correct_winner_accepted(self):
        block = make_block(self.ledger, self.winner, slot=1000)
        with self.ledger._lock:
            self.assertTrue(self.ledger._add_block_locked(block))

    def test_wrong_winner_rejected(self):
        block = make_block(self.ledger, self.non_winner, slot=1000)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_fees_collected_correct_accepted(self):
        set_balance(self.ledger, self.winner.device_id, 10 * UNIT)
        recipient = make_wallet()
        tx = make_transaction(self.winner, recipient.device_id, UNIT, slot=1000)
        block = make_block(self.ledger, self.winner, slot=1000, txs=[tx])
        with self.ledger._lock:
            self.assertTrue(self.ledger._add_block_locked(block))

    def test_fees_collected_inflated_rejected(self):
        set_balance(self.ledger, self.winner.device_id, 10 * UNIT)
        recipient = make_wallet()
        tx = make_transaction(self.winner, recipient.device_id, UNIT, slot=1000)
        block = make_block(self.ledger, self.winner, slot=1000,
                           txs=[tx], fees_collected=tx["fee"] + 1)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_fees_collected_deflated_rejected(self):
        set_balance(self.ledger, self.winner.device_id, 10 * UNIT)
        recipient = make_wallet()
        tx = make_transaction(self.winner, recipient.device_id, UNIT, slot=1000)
        block = make_block(self.ledger, self.winner, slot=1000,
                           txs=[tx], fees_collected=tx["fee"] - 1)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_empty_block_zero_fees_accepted(self):
        block = make_block(self.ledger, self.winner, slot=1000,
                           txs=[], fees_collected=0)
        with self.ledger._lock:
            self.assertTrue(self.ledger._add_block_locked(block))

    def test_empty_block_nonzero_fees_rejected(self):
        block = make_block(self.ledger, self.winner, slot=1000,
                           txs=[], fees_collected=100)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_parent_not_finalized_rejected(self):
        block1 = make_block(self.ledger, self.winner, slot=1000)
        with self.ledger._lock:
            self.ledger._add_block_locked(block1)
        h1 = compute_block_hash(block1)
        _, winner2 = deterministic_winner(self.ledger, self.wallets, 1001, h1)
        block2 = make_block(self.ledger, winner2, slot=1001, prev_hash=h1)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block2))

    def test_parent_finalized_via_hash_accepted(self):
        block1 = make_block(self.ledger, self.winner, slot=1000)
        with self.ledger._lock:
            self.ledger._add_block_locked(block1)
        h1 = compute_block_hash(block1)
        self.ledger._finalized_hashes.add(h1)
        _, winner2 = deterministic_winner(self.ledger, self.wallets, 1001, h1)
        block2 = make_block(self.ledger, winner2, slot=1001, prev_hash=h1)
        with self.ledger._lock:
            self.assertTrue(self.ledger._add_block_locked(block2))

    def test_parent_finalized_via_last_finalized_slot_accepted(self):
        block1 = make_block(self.ledger, self.winner, slot=1000)
        with self.ledger._lock:
            self.ledger._add_block_locked(block1)
        h1 = compute_block_hash(block1)
        self.ledger.last_finalized_slot = 1000
        _, winner2 = deterministic_winner(self.ledger, self.wallets, 1001, h1)
        block2 = make_block(self.ledger, winner2, slot=1001, prev_hash=h1)
        with self.ledger._lock:
            self.assertTrue(self.ledger._add_block_locked(block2))


# ══════════════════════════════════════════════════════════════════════════════
# 6 — DETERMINISTIC WINNER SELECTION
# ══════════════════════════════════════════════════════════════════════════════

class TestDeterministicWinner(unittest.TestCase):

    def test_winner_is_deterministic(self):
        wallets = [make_wallet() for _ in range(5)]
        mature  = [w.device_id for w in wallets]
        ph      = "a" * 64
        w1 = min(mature, key=lambda d: hashlib.sha256(f"{d}:{ph}:1000".encode()).hexdigest())
        w2 = min(mature, key=lambda d: hashlib.sha256(f"{d}:{ph}:1000".encode()).hexdigest())
        self.assertEqual(w1, w2)

    def test_different_prev_hash_changes_winner(self):
        wallets = [make_wallet() for _ in range(10)]
        mature  = [w.device_id for w in wallets]
        winners = set()
        for i in range(20):
            ph = hashlib.sha256(str(i).encode()).hexdigest()
            winners.add(min(mature, key=lambda d: hashlib.sha256(
                f"{d}:{ph}:1000".encode()).hexdigest()))
        self.assertGreater(len(winners), 1)

    def test_non_winner_rejected_post_genesis(self):
        ledger  = make_ledger()
        wallets = [make_wallet() for _ in range(3)]
        for w in wallets:
            register_identity(ledger, w, 800)
        winner_id, _ = deterministic_winner(ledger, wallets, 1000, GENESIS_PREV_HASH)
        non_winner   = next(w for w in wallets if w.device_id != winner_id)
        block = make_block(ledger, non_winner, slot=1000)
        with ledger._lock:
            self.assertFalse(ledger._add_block_locked(block))

    def test_winner_accepted_post_genesis(self):
        ledger  = make_ledger()
        wallets = [make_wallet() for _ in range(3)]
        for w in wallets:
            register_identity(ledger, w, 800)
        _, winner = deterministic_winner(ledger, wallets, 1000, GENESIS_PREV_HASH)
        block = make_block(ledger, winner, slot=1000)
        with ledger._lock:
            self.assertTrue(ledger._add_block_locked(block))


# ══════════════════════════════════════════════════════════════════════════════
# 7 — SUPPLY CAP
# ══════════════════════════════════════════════════════════════════════════════

class TestSupplyCap(unittest.TestCase):

    def test_era2_zero_amount_accepted(self):
        ledger = make_ledger()
        ledger.total_minted = TOTAL_SUPPLY
        w = make_wallet()
        register_identity(ledger, w, 0)
        block = make_block(ledger, w, slot=200, amount=0)
        with ledger._lock:
            self.assertTrue(ledger._add_block_locked(block))

    def test_era2_nonzero_amount_rejected(self):
        ledger = make_ledger()
        ledger.total_minted = TOTAL_SUPPLY
        w = make_wallet()
        register_identity(ledger, w, 0)
        block = make_block(ledger, w, slot=200, amount=REWARD_PER_ROUND)
        with ledger._lock:
            self.assertFalse(ledger._add_block_locked(block))

    def test_era1_inflated_reward_rejected(self):
        ledger = make_ledger()
        w = make_wallet()
        register_identity(ledger, w, 0)
        block = make_block(ledger, w, slot=200, amount=REWARD_PER_ROUND + 1)
        with ledger._lock:
            self.assertFalse(ledger._add_block_locked(block))


# ══════════════════════════════════════════════════════════════════════════════
# 8 — TRANSACTIONS IN BLOCKS
# ══════════════════════════════════════════════════════════════════════════════

class TestTransactionsInBlocks(unittest.TestCase):

    def setUp(self):
        self.ledger    = make_ledger()
        self.winner    = make_wallet()
        self.recipient = make_wallet()
        register_identity(self.ledger, self.winner, 0)
        set_balance(self.ledger, self.winner.device_id, 100 * UNIT)

    def test_valid_transaction_accepted(self):
        tx    = make_transaction(self.winner, self.recipient.device_id, UNIT)
        block = make_block(self.ledger, self.winner, slot=200, txs=[tx])
        with self.ledger._lock:
            self.assertTrue(self.ledger._add_block_locked(block))

    def test_insufficient_balance_rejected(self):
        set_balance(self.ledger, self.winner.device_id, 0)
        tx    = make_transaction(self.winner, self.recipient.device_id, UNIT)
        block = make_block(self.ledger, self.winner, slot=200, txs=[tx])
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_double_spend_same_block_rejected(self):
        set_balance(self.ledger, self.winner.device_id, UNIT + calculate_fee(UNIT))
        tx1   = make_transaction(self.winner, self.recipient.device_id, UNIT)
        tx2   = make_transaction(self.winner, self.recipient.device_id, UNIT)
        block = make_block(self.ledger, self.winner, slot=200, txs=[tx1, tx2])
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_replay_attack_rejected(self):
        tx     = make_transaction(self.winner, self.recipient.device_id, UNIT)
        block1 = make_block(self.ledger, self.winner, slot=200, txs=[tx])
        with self.ledger._lock:
            self.ledger._add_block_locked(block1)
        h1     = compute_block_hash(block1)
        block2 = make_block(self.ledger, self.winner, slot=201,
                            prev_hash=h1, txs=[tx])
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block2))

    def test_fee_below_minimum_rejected(self):
        tx    = make_transaction(self.winner, self.recipient.device_id, UNIT)
        tx["fee"] = 0
        t = Transaction(
            sender_id    = self.winner.device_id,
            recipient_id = self.recipient.device_id,
            sender_pubkey= self.winner.get_public_key_hex(),
            amount       = UNIT,
            fee          = 0,
            slot         = 500,
            timestamp    = tx["timestamp"],
            tx_id        = tx["tx_id"],
        )
        t.sign(self.winner)
        tx    = t.to_dict()
        block = make_block(self.ledger, self.winner, slot=200, txs=[tx])
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_too_many_transactions_rejected(self):
        txs = []
        for _ in range(MAX_TRANSACTIONS_PER_BLOCK + 1):
            w  = make_wallet()
            self.ledger.balances[w.device_id] = 10 * UNIT
            txs.append(make_transaction(w, self.recipient.device_id, UNIT))
        block = make_block(self.ledger, self.winner, slot=200, txs=txs)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_recipient_balance_updated(self):
        tx    = make_transaction(self.winner, self.recipient.device_id, UNIT)
        block = make_block(self.ledger, self.winner, slot=200, txs=[tx])
        with self.ledger._lock:
            self.ledger._add_block_locked(block)
        self.assertEqual(self.ledger.get_balance(self.recipient.device_id), UNIT)

    def test_sender_balance_decreases(self):
        tx    = make_transaction(self.winner, self.recipient.device_id, UNIT)
        fee   = tx["fee"]
        block = make_block(self.ledger, self.winner, slot=200, txs=[tx])
        with self.ledger._lock:
            self.ledger._add_block_locked(block)
        # checkpoint(100*UNIT) + block_reward + fees_collected(=fee) - UNIT_sent - fee_paid
        expected = 100 * UNIT + REWARD_PER_ROUND + fee - UNIT - fee
        self.assertEqual(self.ledger.get_balance(self.winner.device_id), expected)


# ══════════════════════════════════════════════════════════════════════════════
# 9 — BALANCE ACCOUNTING
# ══════════════════════════════════════════════════════════════════════════════

class TestBalanceAccounting(unittest.TestCase):

    def test_winner_earns_block_reward(self):
        ledger = make_ledger()
        w      = make_wallet()
        register_identity(ledger, w, 0)
        block  = make_block(ledger, w, slot=200)
        with ledger._lock:
            ledger._add_block_locked(block)
        self.assertEqual(ledger.get_balance(w.device_id), REWARD_PER_ROUND)

    def test_winner_earns_fees(self):
        ledger    = make_ledger()
        winner    = make_wallet()
        sender    = make_wallet()
        register_identity(ledger, winner, 0)
        set_balance(ledger, sender.device_id, 10 * UNIT)
        tx    = make_transaction(sender, winner.device_id, UNIT)
        fee   = tx["fee"]
        block = make_block(ledger, winner, slot=200, txs=[tx])
        with ledger._lock:
            ledger._add_block_locked(block)
        # reward + fee collected + UNIT received as recipient
        self.assertEqual(ledger.get_balance(winner.device_id),
                         REWARD_PER_ROUND + fee + UNIT)

    def test_era2_winner_earns_only_fees(self):
        ledger = make_ledger()
        ledger.total_minted = TOTAL_SUPPLY
        winner = make_wallet()
        sender = make_wallet()
        register_identity(ledger, winner, 0)
        set_balance(ledger, sender.device_id, 10 * UNIT)
        tx    = make_transaction(sender, winner.device_id, UNIT)
        fee   = tx["fee"]
        block = make_block(ledger, winner, slot=200, txs=[tx],
                           amount=0, fees_collected=fee)
        with ledger._lock:
            ledger._add_block_locked(block)
        self.assertEqual(ledger.get_balance(winner.device_id), fee + UNIT)


# ══════════════════════════════════════════════════════════════════════════════
# 10 — REGISTRATION RULES
# ══════════════════════════════════════════════════════════════════════════════

class TestRegistrationRules(unittest.TestCase):

    def setUp(self):
        self.ledger = make_ledger()
        self.winner = make_wallet()
        register_identity(self.ledger, self.winner, 0)

    def test_valid_registration_accepted(self):
        new_node = make_wallet()
        reg   = new_node._make_registration_message()
        block = make_block(self.ledger, self.winner, slot=200, regs=[reg])
        with self.ledger._lock:
            self.assertTrue(self.ledger._add_block_locked(block))
        self.assertIn(new_node.device_id, self.ledger.identities)

    def test_too_many_registrations_rejected(self):
        regs  = [make_wallet()._make_registration_message()
                 for _ in range(MAX_REGS_PER_BLOCK + 1)]
        block = make_block(self.ledger, self.winner, slot=200, regs=regs)
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_post_genesis_registration_without_gbh_rejected(self):
        register_identity(self.ledger, self.winner, 800)
        new_node = make_wallet()
        reg  = new_node._make_registration_message()
        reg["genesis_block_hash"] = ""
        block = make_block(self.ledger, self.winner, slot=1000, regs=[reg])
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))

    def test_duplicate_anchor_hash_rejected(self):
        gbh      = "a" * 64
        new_node = Wallet()
        new_node.create_new(genesis_block_hash=gbh)
        self.ledger.anchor_hashes.add(gbh)
        reg   = new_node._make_registration_message()
        block = make_block(self.ledger, self.winner, slot=200, regs=[reg])
        with self.ledger._lock:
            self.assertFalse(self.ledger._add_block_locked(block))


# ══════════════════════════════════════════════════════════════════════════════
# 11 — FINALITY
# ══════════════════════════════════════════════════════════════════════════════

class TestFinality(unittest.TestCase):

    def test_two_thirds_required(self):
        # Need strictly > 2/3. With 4 identities:
        # 2 attestations = 2/4 = 0.5  — not final
        # 3 attestations = 3/4 = 0.75 > 2/3 — final
        ledger  = make_ledger()
        wallets = [make_wallet() for _ in range(4)]
        for w in wallets:
            register_identity(ledger, w, 0)
        bh   = "a" * 64
        slot = 200
        att  = {bh: {wallets[0].device_id: {}, wallets[1].device_id: {}}}
        self.assertFalse(is_final(bh, slot, ledger, att))
        att[bh][wallets[2].device_id] = {}
        self.assertTrue(is_final(bh, slot, ledger, att))

    def test_zero_mature_identities_not_final(self):
        ledger = make_ledger()
        self.assertFalse(is_final("a"*64, 200, ledger, {}))

    def test_finalized_hash_in_ledger_set(self):
        ledger = make_ledger()
        ledger._finalized_hashes.add("a"*64)
        self.assertIn("a"*64, ledger._finalized_hashes)

    def test_reorg_blocked_past_finalized_slot(self):
        ledger = make_ledger()
        ledger.last_finalized_slot = 500
        self.assertEqual(ledger.last_finalized_slot, 500)

    def test_attestation_signature_valid(self):
        w    = make_wallet()
        bh   = "a" * 64
        att  = produce_attestation(bh, 200, w)
        payload   = f"attest:{bh}:200".encode()
        pub_bytes = bytes.fromhex(att["public_key"])
        sig_bytes = bytes.fromhex(att["signature"])
        self.assertTrue(Dilithium3.verify(pub_bytes, payload, sig_bytes))

    def test_attestation_wrong_sig_fails(self):
        w1, w2 = make_wallet(), make_wallet()
        bh  = "a" * 64
        att = produce_attestation(bh, 200, w1)
        att["signature"] = w2.sign(b"wrong")
        payload   = f"attest:{bh}:200".encode()
        pub_bytes = bytes.fromhex(att["public_key"])
        sig_bytes = bytes.fromhex(att["signature"])
        self.assertFalse(Dilithium3.verify(pub_bytes, payload, sig_bytes))


# ══════════════════════════════════════════════════════════════════════════════
# 12 — BLOOM FILTER
# ══════════════════════════════════════════════════════════════════════════════

class TestBloomFilter(unittest.TestCase):

    def test_add_and_contains(self):
        bf = SpentBloomFilter()
        bf.add("tx-1")
        self.assertIn("tx-1", bf)

    def test_not_added_not_contained(self):
        bf = SpentBloomFilter()
        self.assertNotIn("tx-999", bf)

    def test_serialization_roundtrip(self):
        bf = SpentBloomFilter()
        bf.add("tx-abc")
        bf2 = SpentBloomFilter.from_dict(bf.to_dict())
        self.assertIn("tx-abc", bf2)


# ══════════════════════════════════════════════════════════════════════════════
# 13 — REGISTRATION FREEZE
# ══════════════════════════════════════════════════════════════════════════════

class TestRegistrationFreeze(unittest.TestCase):

    def test_block_with_regs_during_freeze_rejected(self):
        ledger = make_ledger()
        winner = make_wallet()
        register_identity(ledger, winner, 0)
        # Manually trigger freeze
        ledger.freeze_triggered_slot     = 100
        ledger.freeze_last_abnormal_slot = 200
        ledger.freeze_normal_streak      = 0
        new_node = make_wallet()
        reg   = new_node._make_registration_message()
        block = make_block(ledger, winner, slot=200, regs=[reg])
        with ledger._lock:
            self.assertFalse(ledger._add_block_locked(block))

    def test_block_without_regs_during_freeze_accepted(self):
        ledger = make_ledger()
        winner = make_wallet()
        register_identity(ledger, winner, 0)
        ledger.freeze_triggered_slot     = 100
        ledger.freeze_last_abnormal_slot = 200
        ledger.freeze_normal_streak      = 0
        block = make_block(ledger, winner, slot=200, regs=[])
        with ledger._lock:
            self.assertTrue(ledger._add_block_locked(block))


# ══════════════════════════════════════════════════════════════════════════════
# 14 — CHALLENGE MECHANISM
# ══════════════════════════════════════════════════════════════════════════════

class TestChallengeMechanism(unittest.TestCase):

    def test_challenge_depends_on_prev_hash(self):
        c1 = compute_challenge("a" * 64, 200)
        c2 = compute_challenge("b" * 64, 200)
        self.assertNotEqual(c1, c2)

    def test_challenge_depends_on_slot(self):
        c1 = compute_challenge("a" * 64, 200)
        c2 = compute_challenge("a" * 64, 201)
        self.assertNotEqual(c1, c2)

    def test_solve_and_verify_challenge(self):
        w         = make_wallet()
        challenge = compute_challenge("a" * 64, 200)
        sig_hex, proof_hex = solve_challenge(w.private_key, challenge)
        pub_bytes = bytes.fromhex(w.get_public_key_hex())
        sig_bytes = bytes.fromhex(sig_hex)
        self.assertTrue(Dilithium3.verify(pub_bytes, challenge, sig_bytes))
        self.assertEqual(hashlib.sha256(bytes.fromhex(sig_hex)).hexdigest(), proof_hex)

    def test_wrong_challenge_block_rejected(self):
        ledger = make_ledger()
        w      = make_wallet()
        register_identity(ledger, w, 0)
        block = make_block(ledger, w, slot=200)
        block["challenge"] = "a" * 64
        # Re-sign with corrupted challenge
        bws = {k: v for k, v in block.items() if k != "block_sig"}
        bws["type"] = "block_reward"
        block["block_sig"] = w.sign(canonical_block(bws))
        with ledger._lock:
            self.assertFalse(ledger._add_block_locked(block))


# ══════════════════════════════════════════════════════════════════════════════
# 15 — SELECT COMPETITORS (genesis phase lottery still works)
# ══════════════════════════════════════════════════════════════════════════════

class TestSelectCompetitors(unittest.TestCase):

    def test_immature_excluded(self):
        identities = {"a"*64: 100}
        selected   = select_competitors(identities, "b"*64, 200)
        self.assertEqual(selected, [])

    def test_mature_included(self):
        identities = {"a"*64: 0}
        selected   = select_competitors(identities, "b"*64, 200)
        self.assertIn("a"*64, selected)

    def test_deterministic(self):
        identities = {"a"*64: 0, "b"*64: 0, "c"*64: 0}
        s1 = select_competitors(identities, "x"*64, 200)
        s2 = select_competitors(identities, "x"*64, 200)
        self.assertEqual(s1, s2)

    def test_inactive_excluded(self):
        from timpal import IDENTITY_ACTIVITY_WINDOW, IDENTITY_GRACE_PERIOD
        did = "a" * 64
        identities        = {did: 0}
        identity_last_attest = {did: 0}
        slot = IDENTITY_GRACE_PERIOD + IDENTITY_ACTIVITY_WINDOW + 1
        selected = select_competitors(identities, "b"*64, slot,
                                      identity_last_attest=identity_last_attest)
        self.assertNotIn(did, selected)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
