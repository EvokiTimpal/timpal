#!/usr/bin/env python3
"""
TIMPAL Reorg & Fork Convergence Tests
--------------------------------------
5 protocol-level tests (+ 1 bonus) extending the existing 121-test suite.

Tests:
  1. Basic reorg — node switches to longer incoming chain
  2. Shorter chain rejected — node keeps its longer chain
  3. Equal-length tie-break — lower tip hash wins, deterministically
  4. Deep reorg — fork point deep in chain, full rebuild correct
  5. Partition simulation — two nodes diverge and converge
  6. Post-reorg tx prune — phantom transactions removed after chain switch

Run: python3 test_reorg.py
     python3 test_reorg.py -v   (verbose)
"""

import unittest
import hashlib
import time
import os
import sys
import json
import shutil
import tempfile
from unittest.mock import patch

# ── Mock dilithium_py so timpal imports cleanly without the package ────────────
from unittest.mock import MagicMock
_dil_mock = MagicMock()
_dil_mock.Dilithium3.keygen.return_value = (b"\xab" * 1312, b"\xcd" * 2528)
_dil_mock.Dilithium3.sign.return_value   = b"\xef" * 2420
_dil_mock.Dilithium3.verify.return_value = True
sys.modules.setdefault("dilithium_py",           _dil_mock)
sys.modules.setdefault("dilithium_py.dilithium", _dil_mock)

_cry_mock = MagicMock()
sys.modules.setdefault("cryptography",                                    _cry_mock)
sys.modules.setdefault("cryptography.hazmat",                             _cry_mock)
sys.modules.setdefault("cryptography.hazmat.primitives",                  _cry_mock)
sys.modules.setdefault("cryptography.hazmat.primitives.ciphers",         _cry_mock)
sys.modules.setdefault("cryptography.hazmat.primitives.ciphers.aead",    _cry_mock)
sys.modules.setdefault("cryptography.hazmat.primitives.kdf",             _cry_mock)
sys.modules.setdefault("cryptography.hazmat.primitives.kdf.scrypt",      _cry_mock)
sys.modules.setdefault("cryptography.hazmat.backends",                   _cry_mock)

# ── Import timpal without triggering __main__ ──────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import timpal

# Set GENESIS_TIME so get_current_slot() returns sensible values.
# 100 000 seconds in the past → current slot ≈ 20 000 — well above 0.
timpal.GENESIS_TIME = int(time.time()) - 100_000


# ── Test block / chain factories ───────────────────────────────────────────────

_DUMMY_PUBKEY = "ab" * 64   # 128 hex chars — non-empty, passes `if not pub` check
_DUMMY_SIG    = "cd" * 64   # 128 hex chars

WINNER_A = "a" * 64   # alice
WINNER_B = "b" * 64   # bob
WINNER_C = "c" * 64   # charlie


def make_block(slot: int, prev_hash: str, winner_id: str = WINNER_A,
               amount: float = None) -> dict:
    """Return a well-formed block dict with dummy VRF fields.
    Node._verify_ticket must be patched to return True for these to pass
    validation.  The ticket is a deterministic sha256 so canonical_block()
    produces identical bytes on every call for the same arguments.
    """
    if amount is None:
        amount = timpal.REWARD_PER_ROUND
    ticket = hashlib.sha256(f"ticket:{slot}:{winner_id}".encode()).hexdigest()
    return {
        "reward_id":      f"reward:{slot}",
        "slot":           slot,
        "prev_hash":      prev_hash,
        "winner_id":      winner_id,
        "amount":         amount,
        "timestamp":      int(time.time()),
        "vrf_ticket":     ticket,
        "vrf_seed":       str(slot),
        "vrf_sig":        _DUMMY_SIG,
        "vrf_public_key": _DUMMY_PUBKEY,
        "nodes":          1,
        "type":           "block_reward",
    }


def build_chain(length: int, *,
                fork_hash: str = None,
                fork_slot: int = None,
                winner_id: str = WINNER_A) -> list:
    """Build a chain of `length` blocks, optionally from a fork point.

    If fork_hash / fork_slot are omitted the chain starts from genesis
    (GENESIS_PREV_HASH, slot -1).
    """
    if fork_hash is None:
        fork_hash = timpal.GENESIS_PREV_HASH
    if fork_slot is None:
        fork_slot = -1

    blocks   = []
    prev     = fork_hash
    for i in range(length):
        slot = fork_slot + 1 + i
        b    = make_block(slot, prev, winner_id=winner_id)
        prev = timpal.compute_block_hash(b)
        blocks.append(b)
    return blocks


def make_tx(sender_id: str, sender_pubkey_hex: str, recipient_id: str,
            amount: float, slot: int = 0) -> dict:
    """Return a minimal transaction dict (signature not verified in these tests)."""
    import uuid
    return {
        "tx_id":        str(uuid.uuid4()),
        "sender_id":    sender_id,
        "recipient_id": recipient_id,
        "sender_pubkey": sender_pubkey_hex,
        "amount":       amount,
        "fee":          0.0,
        "slot":         slot,
        "timestamp":    time.time(),
        "signature":    "00" * 32,   # dummy — not verified in reorg tests
    }


# ── Base test class ────────────────────────────────────────────────────────────

class ReorgTestBase(unittest.TestCase):
    """Provides a fresh in-memory Ledger for each test with VRF patched out."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        ledger_path = os.path.join(self.tmpdir, "ledger.json")
        self._p_file = patch.object(timpal, "LEDGER_FILE", ledger_path)
        self._p_vrf  = patch.object(timpal.Node, "_verify_ticket", return_value=True)
        self._p_file.start()
        self._p_vrf.start()

    def tearDown(self):
        self._p_vrf.stop()
        self._p_file.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def fresh_ledger(self) -> timpal.Ledger:
        import uuid
        timpal.LEDGER_FILE = os.path.join(self.tmpdir, f"ledger_{uuid.uuid4().hex}.json")
        return timpal.Ledger()

    def add_chain(self, ledger: timpal.Ledger, blocks: list):
        """Add blocks one-by-one via add_block(); fails test on rejection."""
        for b in blocks:
            ok = ledger.add_block(b)
            self.assertTrue(ok, f"add_block() rejected block at slot {b['slot']}")


# ── Tests ──────────────────────────────────────────────────────────────────────

class Test1_BasicReorg(ReorgTestBase):
    """Node on chain A switches to longer chain B after partition heals."""

    def test_switches_to_longer_chain(self):
        ledger  = self.fresh_ledger()
        chain_a = build_chain(3, winner_id=WINNER_A)
        self.add_chain(ledger, chain_a)
        self.assertEqual(len(ledger.chain), 3)
        self.assertAlmostEqual(ledger.get_balance(WINNER_A),
                               3 * timpal.REWARD_PER_ROUND, places=6)

        # Chain B is one block longer from genesis
        chain_b = build_chain(4, winner_id=WINNER_B)
        changed = ledger.merge({"blocks": chain_b, "transactions": []})

        self.assertTrue(changed, "merge() should return True after reorg")
        self.assertEqual(len(ledger.chain), 4)
        # Alice's rewards gone; Bob has all four
        self.assertAlmostEqual(ledger.get_balance(WINNER_A), 0.0, places=6)
        self.assertAlmostEqual(ledger.get_balance(WINNER_B),
                               4 * timpal.REWARD_PER_ROUND, places=6)
        # total_minted reflects new chain
        self.assertAlmostEqual(ledger.total_minted,
                               4 * timpal.REWARD_PER_ROUND, places=6)


class Test2_ShorterChainRejected(ReorgTestBase):
    """Node keeps its longer chain when offered a shorter alternative."""

    def test_keeps_longer_chain(self):
        ledger  = self.fresh_ledger()
        chain_a = build_chain(5, winner_id=WINNER_A)
        self.add_chain(ledger, chain_a)

        chain_b = build_chain(3, winner_id=WINNER_B)
        changed = ledger.merge({"blocks": chain_b, "transactions": []})

        self.assertEqual(len(ledger.chain), 5,
                         "Shorter incoming chain must not replace longer local chain")
        self.assertAlmostEqual(ledger.get_balance(WINNER_A),
                               5 * timpal.REWARD_PER_ROUND, places=6)
        self.assertAlmostEqual(ledger.get_balance(WINNER_B), 0.0, places=6)


class Test3_EqualLengthTieBreak(ReorgTestBase):
    """Equal-length forks resolve to lower tip hash, regardless of arrival order."""

    def _run_scenario(self, first_chain, second_chain, expected_tip):
        ledger = self.fresh_ledger()
        self.add_chain(ledger, first_chain)
        ledger.merge({"blocks": second_chain, "transactions": []})
        actual_tip = timpal.compute_block_hash(ledger.chain[-1])
        self.assertEqual(actual_tip, expected_tip,
                         "Tie-break must produce lower tip hash regardless of arrival order")

    def test_lower_tip_hash_wins_both_orderings(self):
        chain_a = build_chain(3, winner_id=WINNER_A)
        chain_b = build_chain(3, winner_id=WINNER_B)

        tip_a = timpal.compute_block_hash(chain_a[-1])
        tip_b = timpal.compute_block_hash(chain_b[-1])
        expected_tip = min(tip_a, tip_b)

        # Ordering 1: local=loser, incoming=winner
        loser  = chain_b if expected_tip == tip_a else chain_a
        winner = chain_a if expected_tip == tip_a else chain_b
        self._run_scenario(loser,  winner, expected_tip)

        # Ordering 2: local=winner, incoming=loser
        self._run_scenario(winner, loser,  expected_tip)

    def test_tiebreak_is_consistent_across_three_nodes(self):
        """Three nodes seeing chains in different orders must converge."""
        chain_a = build_chain(4, winner_id=WINNER_A)
        chain_b = build_chain(4, winner_id=WINNER_B)

        tip_a = timpal.compute_block_hash(chain_a[-1])
        tip_b = timpal.compute_block_hash(chain_b[-1])
        expected = min(tip_a, tip_b)

        tips = []
        for first, second in [(chain_a, chain_b), (chain_b, chain_a),
                               (chain_a, chain_b)]:
            ledger = self.fresh_ledger()
            self.add_chain(ledger, first)
            ledger.merge({"blocks": second, "transactions": []})
            tips.append(timpal.compute_block_hash(ledger.chain[-1]))

        self.assertTrue(all(t == expected for t in tips),
                        f"All nodes must converge: {tips}")


class Test4_DeepReorg(ReorgTestBase):
    """Fork point deep in the chain; full rebuild is correct."""

    def test_deep_fork_switches_correctly(self):
        ledger  = self.fresh_ledger()
        chain_a = build_chain(10, winner_id=WINNER_A)
        self.add_chain(ledger, chain_a)
        self.assertEqual(len(ledger.chain), 10)

        # Fork after block index 2 (i.e. after 3 blocks)
        fork_block = chain_a[2]
        fork_hash  = timpal.compute_block_hash(fork_block)
        fork_slot  = fork_block["slot"]

        # Chain B tail: 9 blocks from fork → total depth 3+9=12 > 10
        chain_b_tail = build_chain(9, fork_hash=fork_hash,
                                   fork_slot=fork_slot, winner_id=WINNER_B)
        changed = ledger.merge({"blocks": chain_b_tail, "transactions": []})

        self.assertTrue(changed, "Deep reorg should succeed")
        self.assertEqual(len(ledger.chain), 12,
                         f"Expected 12 blocks (3+9), got {len(ledger.chain)}")

        # First 3 blocks are chain A's
        for i in range(3):
            self.assertEqual(ledger.chain[i]["winner_id"], WINNER_A,
                             f"Block {i} should still belong to chain A")
        # Remaining 9 are chain B's
        for i in range(3, 12):
            self.assertEqual(ledger.chain[i]["winner_id"], WINNER_B,
                             f"Block {i} should belong to chain B")

        # total_minted: 3 from A + 9 from B = 12 blocks
        expected = round(12 * timpal.REWARD_PER_ROUND, 8)
        self.assertAlmostEqual(ledger.total_minted, expected, places=6)

        # Chain is internally linked: every prev_hash must match
        for i in range(1, len(ledger.chain)):
            expected_prev = timpal.compute_block_hash(ledger.chain[i - 1])
            actual_prev   = ledger.chain[i]["prev_hash"]
            self.assertEqual(actual_prev, expected_prev,
                             f"Chain linkage broken at index {i}")


class Test5_PartitionSimulation(ReorgTestBase):
    """Two partitioned nodes diverge then converge to canonical chain."""

    def test_nodes_converge_after_partition(self):
        ledger1 = self.fresh_ledger()
        ledger2 = self.fresh_ledger()

        # During partition: node1 builds 5 blocks, node2 builds 7 (will win)
        chain1 = build_chain(5, winner_id=WINNER_A)
        chain2 = build_chain(7, winner_id=WINNER_B)
        self.add_chain(ledger1, chain1)
        self.add_chain(ledger2, chain2)

        # Reconnect: each node merges the other's chain
        ledger1.merge({"blocks": list(ledger2.chain), "transactions": []})
        ledger2.merge({"blocks": list(chain1),        "transactions": []})

        # Both must have 7 blocks
        self.assertEqual(len(ledger1.chain), 7)
        self.assertEqual(len(ledger2.chain), 7)

        # Both must have identical tip hashes
        tip1 = timpal.compute_block_hash(ledger1.chain[-1])
        tip2 = timpal.compute_block_hash(ledger2.chain[-1])
        self.assertEqual(tip1, tip2,
                         "Both nodes must converge to identical chain tip")

    def test_equal_partition_converges_deterministically(self):
        """Two equal-length chains from partition converge to same canonical chain."""
        ledger1 = self.fresh_ledger()
        ledger2 = self.fresh_ledger()

        chain1 = build_chain(6, winner_id=WINNER_A)
        chain2 = build_chain(6, winner_id=WINNER_B)
        self.add_chain(ledger1, chain1)
        self.add_chain(ledger2, chain2)

        ledger1.merge({"blocks": list(ledger2.chain), "transactions": []})
        ledger2.merge({"blocks": list(ledger1.chain), "transactions": []})

        tip1 = timpal.compute_block_hash(ledger1.chain[-1])
        tip2 = timpal.compute_block_hash(ledger2.chain[-1])
        self.assertEqual(tip1, tip2,
                         "Equal-length partition must still converge deterministically")


class Test6_PostReorgTxPrune(ReorgTestBase):
    """Transactions funded by reorged-out rewards are pruned after chain switch."""

    def test_phantom_tx_removed_after_reorg(self):
        """Alice earns in chain A, sends TMPL; after reorg to chain B, tx is pruned."""
        ledger  = self.fresh_ledger()

        # Chain A: alice wins 3 blocks
        chain_a = build_chain(3, winner_id=WINNER_A)
        self.add_chain(ledger, chain_a)

        alice_start = ledger.get_balance(WINNER_A)
        self.assertAlmostEqual(alice_start, 3 * timpal.REWARD_PER_ROUND, places=6)

        # Alice sends 1 TMPL to Bob — valid under chain A's balances
        # We inject it directly since we can't sign with a real Dilithium key in tests
        tx = make_tx(sender_id=WINNER_A, sender_pubkey_hex=_DUMMY_PUBKEY,
                     recipient_id=WINNER_B, amount=1.0, slot=3)
        ledger.transactions.append(tx)
        ledger.save()

        # Confirm Bob received it under chain A
        self.assertAlmostEqual(ledger.get_balance(WINNER_B), 1.0, places=6)

        # Chain B: bob wins 4 blocks — longer, triggers reorg.
        # After reorg alice has 0 block rewards → tx is no longer fundable.
        chain_b = build_chain(4, winner_id=WINNER_B)
        ledger.merge({"blocks": chain_b, "transactions": []})

        self.assertEqual(len(ledger.chain), 4,
                         "Reorg to chain B should have happened")

        # The phantom transaction must have been pruned
        self.assertEqual(len(ledger.transactions), 0,
                         "Transaction funded by reorged-out reward must be pruned")

        # Alice has 0, Bob has exactly 4 block rewards (no phantom +1)
        self.assertAlmostEqual(ledger.get_balance(WINNER_A), 0.0, places=6)
        self.assertAlmostEqual(ledger.get_balance(WINNER_B),
                               4 * timpal.REWARD_PER_ROUND, places=6)

    def test_valid_tx_survives_reorg(self):
        """A transaction funded by blocks that survive the reorg is kept."""
        ledger = self.fresh_ledger()

        # Both chains share first 3 blocks from chain A
        chain_a      = build_chain(10, winner_id=WINNER_A)
        shared       = chain_a[:3]
        fork_block   = shared[-1]
        fork_hash    = timpal.compute_block_hash(fork_block)
        fork_slot    = fork_block["slot"]
        self.add_chain(ledger, shared)

        # Charlie wins shared block 0 → alice still has blocks 1,2 surviving
        # Actually let's keep it simple: alice wins all 3 shared blocks,
        # so she has funds regardless of which fork wins.
        # Send tx from alice (funds come from shared blocks — survive any fork)
        tx = make_tx(sender_id=WINNER_A, sender_pubkey_hex=_DUMMY_PUBKEY,
                     recipient_id=WINNER_C, amount=1.0, slot=fork_slot)
        ledger.transactions.append(tx)
        ledger.save()

        # Now fork: chain B extends from the shared tip with 5 more blocks
        chain_b_tail = build_chain(5, fork_hash=fork_hash,
                                   fork_slot=fork_slot, winner_id=WINNER_B)
        # Chain A also extends: only 2 more blocks — B is longer, reorg happens
        chain_a_tail = build_chain(2, fork_hash=fork_hash,
                                   fork_slot=fork_slot, winner_id=WINNER_A)
        self.add_chain(ledger, chain_a_tail)

        ledger.merge({"blocks": chain_b_tail, "transactions": []})

        # Reorg should have happened (3+5=8 > 3+2=5)
        self.assertEqual(len(ledger.chain), 8)

        # Alice's tx is funded by the 3 SHARED blocks (which survive the reorg)
        # → tx should NOT have been pruned
        self.assertEqual(len(ledger.transactions), 1,
                         "Transaction funded by surviving shared blocks must be kept")
        self.assertAlmostEqual(ledger.get_balance(WINNER_C), 1.0, places=6)


# ── Runner ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
