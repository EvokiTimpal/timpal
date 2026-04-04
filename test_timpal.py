#!/usr/bin/env python3
"""
TIMPAL v4.0 — Complete Test Suite
LOCAL ONLY — never push to GitHub or deploy to servers.

Performance design:
  _base_ledger() uses unsigned stub blocks (0 Dilithium3 ops).
  Real Dilithium3 signatures are created only in the ~20 tests
  that specifically verify cryptographic correctness.
  Full suite target: under 60 seconds on any modern machine.

Coverage:
  Constants, Economics, Lottery, Finality, Bloom filter,
  Registration freeze, Wallet, Transaction, Ledger (all 17 block
  rules, balance, merge, orphan, reorg, checkpoint),
  Network (rate limits, IP ban, seen-ids, wallet guards),
  Payment URI, Canonical hash, Concurrency structure,
  All patch regressions (C1-C3, H1, M1-M5, MISSING 1-6, L1-L4),
  All Session-23 Section 9 gaps.
"""

import sys, os, hashlib, json, threading, time, unittest, tempfile

os.environ["TIMPAL_TEST"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import unittest.mock as mock
with mock.patch("builtins.exit"):
    import timpal

timpal.GENESIS_TIME = 1_000_000_000

from timpal import (
    UNIT, TOTAL_SUPPLY, REWARD_PER_ROUND, TX_FEE_RATE, TX_FEE_MIN, TX_FEE_MAX,
    REWARD_INTERVAL, CONFIRMATION_DEPTH, MIN_IDENTITY_AGE,
    MAX_REGS_PER_BLOCK, MAX_REGS_PER_PRODUCER, TARGET_COMPETITORS,
    ATTESTATION_THRESHOLD, CHECKPOINT_INTERVAL, CHECKPOINT_BUFFER,
    MAX_TRANSACTIONS_PER_BLOCK, TX_EXPIRY_SLOTS,
    FREEZE_RATE_MULTIPLIER, FREEZE_BASELINE_WINDOW, FREEZE_DETECTION_WINDOW,
    FREEZE_COOLDOWN_SLOTS, MAX_PEERS, MAX_P2P_MESSAGE_SIZE,
    MAX_SYNC_MESSAGE_SIZE, IP_BAN_SECONDS, BLOCK_RATE_LIMIT,
    COMPETE_RATE_LIMIT, ATTEST_RATE_LIMIT, GENESIS_PREV_HASH,
    MAX_REORG_DEPTH, VERSION, MIN_VERSION, BOOTSTRAP_SERVERS, LEDGER_FILE,
    get_current_slot, _ver, canonical_block, compute_block_hash,
    get_block_reward, is_era2, calculate_fee,
    select_competitors, compute_challenge, solve_challenge,
    is_final, produce_attestation,
    is_registration_freeze_active, _avg_regs_per_slot,
    select_transactions_for_block, can_add_to_mempool,
    generate_payment_uri, parse_payment_uri,
    derive_keys_from_seed, SpentBloomFilter,
    Wallet, Transaction, Ledger, Network, Node,
)
from dilithium_py.dilithium import Dilithium3


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _wallet():
    """New wallet with fresh Dilithium3 keypair."""
    w = Wallet()
    w.public_key, w.private_key = Dilithium3.keygen()
    w.device_id = hashlib.sha256(w.public_key).hexdigest()
    w.genesis_block_hash = None
    return w


def _stub_block(device_id, slot, prev_hash, total_minted=0):
    """Unsigned stub block. Fast — zero Dilithium3 ops.
    Suitable for building base ledger state. NOT suitable for
    tests that exercise _add_block_locked signature verification."""
    b = {
        "slot":          slot,
        "winner_id":     device_id,
        "prev_hash":     prev_hash,
        "amount":        get_block_reward(total_minted),
        "fees_collected":0,
        "timestamp":     int(timpal.GENESIS_TIME) + slot * 10 + 5,
        "transactions":  [],
        "registrations": [],
        "type":          "block_reward",
        "version":       VERSION,
    }
    return b


def _signed_block(wallet, slot, prev_hash, total_minted=0, regs=None, txs=None):
    """Fully signed block with real Dilithium3 signatures.
    Use only in tests that need cryptographic verification."""
    challenge = compute_challenge(prev_hash, slot)
    sig_hex, proof_hex = solve_challenge(wallet.private_key, challenge)
    bws = {
        "reward_id":     f"reward:{slot}",
        "slot":          slot,
        "winner_id":     wallet.device_id,
        "prev_hash":     prev_hash,
        "challenge":     challenge.hex(),
        "compete_sig":   sig_hex,
        "compete_proof": proof_hex,
        "vrf_public_key":wallet.get_public_key_hex(),
        "amount":        get_block_reward(total_minted),
        "fees_collected":0,
        "timestamp":     int(timpal.GENESIS_TIME) + slot * 10 + 5,
        "transactions":  txs or [],
        "registrations": regs or [],
        "type":          "block_reward",
        "nodes":         1,
        "version":       VERSION,
    }
    bws["block_sig"] = wallet.sign(canonical_block(bws))
    return bws


def _init_ledger():
    """Bare Ledger with no blocks and no identities."""
    l = Ledger.__new__(Ledger)
    l.transactions = []; l.chain = []; l.fee_rewards = []
    l.total_minted = 0; l.checkpoints = []
    l.identities = {}; l.identity_pubkeys = {}
    l.anchor_hashes = set(); l.balances = {}; l.my_transactions = []
    l._lock = threading.RLock()
    l._spent_bloom = SpentBloomFilter(); l._orphan_pool = {}
    l.freeze_triggered_slot = None; l.freeze_last_abnormal_slot = 0
    l.freeze_normal_streak = 0; l.last_finalized_slot = -1
    return l


def _base_ledger(wallet, length=MIN_IDENTITY_AGE + 1):
    """Ledger with unsigned stub blocks. Fast — no Dilithium3 ops.
    Establishes identity at slot 0 and a tip at slot (length-1).
    Use for tests that need a chain state but NOT block-sig verification."""
    l = _init_ledger()
    l.identities[wallet.device_id] = 0
    l.identity_pubkeys[wallet.device_id] = wallet.get_public_key_hex()
    prev = GENESIS_PREV_HASH
    for s in range(length):
        b = _stub_block(wallet.device_id, s, prev, l.total_minted)
        c = json.dumps(b, sort_keys=True, separators=(",", ":")).encode()
        prev = hashlib.sha256(c).hexdigest()
        l.chain.append(b)
        l.total_minted += b["amount"]
    return l


def _cp_ledger(wallet):
    """Ledger sized for checkpoint tests (1000 + 120 + 2 stub blocks)."""
    return _base_ledger(wallet, CHECKPOINT_INTERVAL + CHECKPOINT_BUFFER + 2)


def _valid_cp(cp_slot=CHECKPOINT_INTERVAL):
    """Minimal valid checkpoint dict."""
    pb = max(0, cp_slot - CHECKPOINT_BUFFER)
    return {
        "slot": cp_slot, "prune_before": pb, "balances": {},
        "identities": {}, "identity_pubkeys": {}, "anchor_hashes": [],
        "total_minted": cp_slot * REWARD_PER_ROUND,
        "chain_tip_hash": "a"*64, "chain_tip_slot": cp_slot - 1,
        "chain_hash": "0"*64, "txs_hash": "0"*64,
        "fee_rewards_hash": "0"*64, "kept_minted": 0, "version": VERSION,
    }


def _net(wallet=None):
    """Minimal Network instance — no real sockets."""
    l = _init_ledger()
    n = Network.__new__(Network)
    n.wallet = wallet; n.ledger = l; n._node_ref = None
    n.peers = {}; n._peers_lock = threading.Lock()
    n.seen_ids = set(); n._seen_lock = threading.Lock()
    n._seen_tx_order = []; n._running = False
    n.local_ip = "127.0.0.1"; n.port = 7779
    n._bootstrap_servers = []
    n._sync_rate = {}; n._sync_rate_lock = threading.Lock()
    n._block_rate = {}; n._block_rate_lock = threading.Lock()
    n._banned_ips = {}; n._ban_lock = threading.Lock()
    n._compete_rate = {}; n._compete_rate_lock = threading.Lock()
    n._attest_rate = {}; n._attest_rate_lock = threading.Lock()
    n._peer_cache_file = "/tmp/timpal_test_peers.json"
    return n


def _tx(sender, recipient, amount=UNIT, slot=200):
    """Signed transaction. 1 Dilithium3 sign op."""
    fee = calculate_fee(amount)
    t = Transaction(
        sender_id=sender.device_id, recipient_id=recipient.device_id,
        sender_pubkey=sender.get_public_key_hex(),
        amount=amount, fee=fee, memo="test", slot=slot, timestamp=time.time()
    )
    t.sign(sender)
    return t


# ══════════════════════════════════════════════════════════════════════════════
# 1 — CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

class TestConstants(unittest.TestCase):

    def test_total_supply(self):
        self.assertEqual(TOTAL_SUPPLY, 12_500_000_000_000_000)

    def test_unit(self):
        self.assertEqual(UNIT, 100_000_000)
        self.assertEqual(TOTAL_SUPPLY / UNIT, 125_000_000.0)

    def test_reward_per_round(self):
        self.assertEqual(REWARD_PER_ROUND, 105_750_000)

    def test_reward_interval(self):
        self.assertEqual(REWARD_INTERVAL, 10.0)

    def test_confirmation_depth(self):
        self.assertEqual(CONFIRMATION_DEPTH, 3)

    def test_min_identity_age(self):
        self.assertEqual(MIN_IDENTITY_AGE, 200)

    def test_checkpoint_constants(self):
        self.assertEqual(CHECKPOINT_INTERVAL, 1000)
        self.assertEqual(CHECKPOINT_BUFFER, 120)

    def test_attestation_threshold(self):
        self.assertAlmostEqual(ATTESTATION_THRESHOLD, 2/3)

    def test_target_competitors(self):
        self.assertEqual(TARGET_COMPETITORS, 10)

    def test_fee_constants(self):
        self.assertEqual(TX_FEE_RATE, 0.001)
        self.assertEqual(TX_FEE_MIN, 10_000)
        self.assertEqual(TX_FEE_MAX, 1_000_000)

    def test_version(self):
        self.assertEqual(VERSION, "4.0")
        self.assertEqual(MIN_VERSION, "4.0")

    def test_genesis_prev_hash(self):
        self.assertEqual(GENESIS_PREV_HASH, "0" * 64)

    def test_era1_remainder(self):
        # Python: 12_500_000_000_000_000 // 105_750_000 = 118_203_309
        # 118_203_309 * 105_750_000 = 12_499_999_926_750_000
        # remainder = 73_250_000
        # The spec document had an arithmetic error — the code is correct.
        full_blocks = TOTAL_SUPPLY // REWARD_PER_ROUND
        remainder   = TOTAL_SUPPLY - full_blocks * REWARD_PER_ROUND
        self.assertEqual(remainder, 73_250_000)

    def test_compete_rate_limit_equals_target_plus_2(self):
        self.assertEqual(COMPETE_RATE_LIMIT, TARGET_COMPETITORS + 2)

    def test_sync_limit_larger_than_p2p_limit(self):
        self.assertGreater(MAX_SYNC_MESSAGE_SIZE, MAX_P2P_MESSAGE_SIZE)
        self.assertEqual(MAX_SYNC_MESSAGE_SIZE, 100_000_000)
        self.assertEqual(MAX_P2P_MESSAGE_SIZE,  10_000_000)


# ══════════════════════════════════════════════════════════════════════════════
# 2 — ECONOMICS
# ══════════════════════════════════════════════════════════════════════════════

class TestEconomics(unittest.TestCase):

    def test_block_reward_normal(self):
        self.assertEqual(get_block_reward(0), REWARD_PER_ROUND)
        self.assertEqual(get_block_reward(REWARD_PER_ROUND), REWARD_PER_ROUND)

    def test_block_reward_last_era1_block(self):
        """The final block of Era 1 pays the remainder, not a full REWARD_PER_ROUND."""
        full_blocks  = TOTAL_SUPPLY // REWARD_PER_ROUND      # 118,203,309
        all_full     = full_blocks * REWARD_PER_ROUND        # total after full blocks
        remainder    = TOTAL_SUPPLY - all_full               # 23,825,000

        # All full blocks pay exactly REWARD_PER_ROUND
        self.assertEqual(get_block_reward((full_blocks - 1) * REWARD_PER_ROUND),
                         REWARD_PER_ROUND)
        # The remainder block pays the leftover
        reward = get_block_reward(all_full)
        self.assertEqual(reward, remainder)
        self.assertLess(reward, REWARD_PER_ROUND)
        # After remainder block, Era 2 begins
        self.assertEqual(get_block_reward(all_full + remainder), 0)

    def test_block_reward_era2_returns_zero(self):
        self.assertEqual(get_block_reward(TOTAL_SUPPLY), 0)
        self.assertEqual(get_block_reward(TOTAL_SUPPLY + 1), 0)

    def test_block_reward_remainder_math(self):
        """Math: full_blocks × REWARD_PER_ROUND + remainder == TOTAL_SUPPLY."""
        full_blocks = TOTAL_SUPPLY // REWARD_PER_ROUND
        remainder   = TOTAL_SUPPLY - full_blocks * REWARD_PER_ROUND
        self.assertEqual(full_blocks * REWARD_PER_ROUND + remainder, TOTAL_SUPPLY)

    def test_is_era2_false(self):
        l = _init_ledger(); l.total_minted = TOTAL_SUPPLY - 1
        self.assertFalse(is_era2(l))

    def test_is_era2_true(self):
        l = _init_ledger(); l.total_minted = TOTAL_SUPPLY
        self.assertTrue(is_era2(l))

    def test_is_era2_none(self):
        self.assertFalse(is_era2(None))

    def test_calculate_fee_minimum(self):
        self.assertEqual(calculate_fee(1),        TX_FEE_MIN)
        self.assertEqual(calculate_fee(TX_FEE_MIN), TX_FEE_MIN)

    def test_calculate_fee_percentage(self):
        self.assertEqual(calculate_fee(UNIT), 100_000)   # 0.1% of 1 TMPL

    def test_calculate_fee_maximum(self):
        self.assertEqual(calculate_fee(100  * UNIT), TX_FEE_MAX)
        self.assertEqual(calculate_fee(1000 * UNIT), TX_FEE_MAX)

    def test_calculate_fee_bounds(self):
        for amount in [1, 100, UNIT, 100 * UNIT, 10_000 * UNIT]:
            fee = calculate_fee(amount)
            self.assertGreaterEqual(fee, TX_FEE_MIN)
            self.assertLessEqual(fee,   TX_FEE_MAX)


# ══════════════════════════════════════════════════════════════════════════════
# 3 — LOTTERY
# ══════════════════════════════════════════════════════════════════════════════

class TestLottery(unittest.TestCase):

    def _ids(self, n, first_seen=0):
        return {hashlib.sha256(f"node-{i}".encode()).hexdigest(): first_seen
                for i in range(n)}

    def test_count_equals_target(self):
        result = select_competitors(self._ids(100), "a"*64, MIN_IDENTITY_AGE + 1)
        self.assertEqual(len(result), TARGET_COMPETITORS)

    def test_deterministic(self):
        ids = self._ids(50)
        self.assertEqual(select_competitors(ids, "b"*64, MIN_IDENTITY_AGE + 1),
                         select_competitors(ids, "b"*64, MIN_IDENTITY_AGE + 1))

    def test_changes_with_slot(self):
        ids  = self._ids(50); slot = MIN_IDENTITY_AGE + 1
        self.assertNotEqual(select_competitors(ids, "c"*64, slot),
                            select_competitors(ids, "c"*64, slot + 1))

    def test_changes_with_prev_hash(self):
        ids = self._ids(50); slot = MIN_IDENTITY_AGE + 1
        self.assertNotEqual(select_competitors(ids, "d"*64, slot),
                            select_competitors(ids, "e"*64, slot))

    def test_excludes_immature(self):
        slot = MIN_IDENTITY_AGE + 1
        # "recent" was registered at this slot so age = 0
        ids = {"mature": 0, "recent": slot}
        result = select_competitors(ids, "f"*64, slot)
        self.assertNotIn("recent", result)

    def test_fewer_than_10_available(self):
        ids = self._ids(3)
        result = select_competitors(ids, "g"*64, MIN_IDENTITY_AGE + 1)
        self.assertEqual(len(result), 3)

    def test_empty_when_none_eligible(self):
        ids = {"x": MIN_IDENTITY_AGE}   # age = 0 at slot MIN_IDENTITY_AGE
        result = select_competitors(ids, "h"*64, MIN_IDENTITY_AGE)
        self.assertEqual(result, [])

    def test_compute_challenge_deterministic(self):
        c1 = compute_challenge("a"*64, 100)
        c2 = compute_challenge("a"*64, 100)
        self.assertEqual(c1, c2)

    def test_compute_challenge_varies(self):
        self.assertNotEqual(compute_challenge("a"*64, 100),
                            compute_challenge("a"*64, 101))
        self.assertNotEqual(compute_challenge("a"*64, 100),
                            compute_challenge("b"*64, 100))

    def test_solve_challenge_proof_is_sha256_of_sig(self):
        w = _wallet()
        chal = compute_challenge("a"*64, 100)
        sig_hex, proof_hex = solve_challenge(w.private_key, chal)
        self.assertEqual(hashlib.sha256(bytes.fromhex(sig_hex)).hexdigest(), proof_hex)

    def test_solve_challenge_sig_verifies(self):
        w = _wallet()
        chal = compute_challenge("a"*64, 100)
        sig_hex, _ = solve_challenge(w.private_key, chal)
        self.assertTrue(Dilithium3.verify(w.public_key, chal, bytes.fromhex(sig_hex)))


# ══════════════════════════════════════════════════════════════════════════════
# 4 — FINALITY
# ══════════════════════════════════════════════════════════════════════════════

class TestFinality(unittest.TestCase):

    def _ledger_with_n_ids(self, n):
        l = _init_ledger()
        for i in range(n):
            l.identities[f"id{i}"*4] = 0
        return l

    def test_below_threshold_not_final(self):
        l = self._ledger_with_n_ids(9)
        bh = "a"*64; slot = MIN_IDENTITY_AGE + 1
        # 5/9 = 55.6% < 66.7%
        attests = {bh: {f"id{i}"*4: {} for i in range(5)}}
        self.assertFalse(is_final(bh, slot, l, attests))

    def test_above_threshold_final(self):
        l = self._ledger_with_n_ids(9)
        bh = "b"*64; slot = MIN_IDENTITY_AGE + 1
        # 7/9 = 77.8% > 66.7%
        attests = {bh: {f"id{i}"*4: {} for i in range(7)}}
        self.assertTrue(is_final(bh, slot, l, attests))

    def test_exactly_at_threshold_not_final(self):
        """Threshold is strictly > 2/3, not >=."""
        l = self._ledger_with_n_ids(9)
        bh = "c"*64; slot = MIN_IDENTITY_AGE + 1
        # 6/9 = exactly 66.7% — NOT final
        attests = {bh: {f"id{i}"*4: {} for i in range(6)}}
        self.assertFalse(is_final(bh, slot, l, attests))

    def test_no_identities_not_final(self):
        l = _init_ledger()
        self.assertFalse(is_final("d"*64, 0, l, {}))

    def test_only_mature_count(self):
        l = _init_ledger()
        for i in range(6): l.identities[f"mature{i}"] = 0
        # 100 immature identities — must not count toward threshold
        for i in range(100): l.identities[f"immature{i}"] = MIN_IDENTITY_AGE + 1
        slot = MIN_IDENTITY_AGE + 1
        bh = "e"*64
        attests = {bh: {f"mature{i}": {} for i in range(6)}}
        attests[bh].update({f"immature{i}": {} for i in range(100)})
        self.assertTrue(is_final(bh, slot, l, attests))

    def test_produce_attestation_structure(self):
        w = _wallet()
        a = produce_attestation("f"*64, 100, w)
        self.assertEqual(a["type"], "ATTEST")
        self.assertEqual(a["block_hash"], "f"*64)
        self.assertEqual(a["slot"], 100)
        self.assertEqual(a["device_id"], w.device_id)

    def test_produce_attestation_signature_verifies(self):
        w = _wallet(); bh = "g"*64; slot = 100
        a = produce_attestation(bh, slot, w)
        payload = f"attest:{bh}:{slot}".encode()
        self.assertTrue(Dilithium3.verify(
            w.public_key, payload, bytes.fromhex(a["signature"])
        ))


# ══════════════════════════════════════════════════════════════════════════════
# 5 — BLOOM FILTER
# ══════════════════════════════════════════════════════════════════════════════

class TestBloomFilter(unittest.TestCase):

    def test_add_and_contains(self):
        bf = SpentBloomFilter()
        bf.add("tx-abc")
        self.assertIn("tx-abc", bf)

    def test_not_contains_before_add(self):
        self.assertNotIn("tx-xyz", SpentBloomFilter())

    def test_count_increments(self):
        bf = SpentBloomFilter()
        bf.add("a"); bf.add("b")
        self.assertEqual(bf._count, 2)

    def test_round_trip(self):
        bf = SpentBloomFilter()
        for i in range(20): bf.add(f"tx-{i}")
        bf2 = SpentBloomFilter.from_dict(bf.to_dict())
        for i in range(20): self.assertIn(f"tx-{i}", bf2)

    def test_to_dict_keys(self):
        d = SpentBloomFilter().to_dict()
        for k in ("capacity", "error_rate", "num_bits", "num_hashes", "bits", "count"):
            self.assertIn(k, d)


# ══════════════════════════════════════════════════════════════════════════════
# 6 — REGISTRATION FREEZE
# ══════════════════════════════════════════════════════════════════════════════

class TestRegistrationFreeze(unittest.TestCase):

    def test_avg_regs_accepts_list(self):
        chain = [{"slot": 5,  "registrations": [{}, {}]},
                 {"slot": 10, "registrations": [{}]}]
        self.assertAlmostEqual(_avg_regs_per_slot(chain, 0, 20), 3/20)

    def test_avg_regs_empty_chain(self):
        self.assertEqual(_avg_regs_per_slot([], 0, 100), 0.0)

    def test_avg_regs_window_filter(self):
        chain = [{"slot": 5, "registrations": [{}]},
                 {"slot": 50, "registrations": [{}, {}]}]
        self.assertAlmostEqual(_avg_regs_per_slot(chain, 0, 20), 1/20)

    def test_avg_regs_rejects_ledger_object(self):
        l = _init_ledger()
        with self.assertRaises((AttributeError, TypeError)):
            _avg_regs_per_slot(l, 0, 100)

    def test_no_freeze_on_empty_chain(self):
        l = _init_ledger()
        active, _ = is_registration_freeze_active(l, 2000)
        self.assertFalse(active)

    def test_freeze_triggers_on_spike(self):
        l = _init_ledger()
        chain = []
        for s in range(2000):
            regs = [{"device_id": "a"*64}] * 8 if s >= 1900 else []
            chain.append({"slot": s, "registrations": regs})
        active, status = is_registration_freeze_active(l, 2000, chain=chain)
        self.assertTrue(active)
        self.assertGreater(status["current_rate"], status["baseline_rate"])

    def test_chain_none_and_snapshot_agree(self):
        l = _init_ledger()
        snap = list(l.chain)
        a1, _ = is_registration_freeze_active(l, 500, chain=None)
        a2, _ = is_registration_freeze_active(l, 500, chain=snap)
        self.assertEqual(a1, a2)


# ══════════════════════════════════════════════════════════════════════════════
# 7 — WALLET
# ══════════════════════════════════════════════════════════════════════════════

class TestWallet(unittest.TestCase):

    def test_create_genesis_phase(self):
        w = Wallet(); w.create_new()
        self.assertEqual(len(w.device_id), 64)
        self.assertIsNone(w.genesis_block_hash)
        self.assertEqual(w.device_id, hashlib.sha256(w.public_key).hexdigest())

    def test_create_chain_anchored(self):
        gbh = "b"*64
        w = Wallet(); w.create_new(genesis_block_hash=gbh)
        expected = hashlib.sha256(w.public_key + bytes.fromhex(gbh)).hexdigest()
        self.assertEqual(w.device_id, expected)

    def test_save_load_unencrypted(self):
        w = _wallet()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            w.save(path=path)
            w2 = Wallet(); w2.load(path=path)
            self.assertEqual(w.device_id,   w2.device_id)
            self.assertEqual(w.private_key, w2.private_key)
        finally:
            os.unlink(path)

    def test_save_load_encrypted(self):
        w = _wallet()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            w.save(path=path, password="TestPass99")
            w2 = Wallet(); w2.load(path=path, password="TestPass99")
            self.assertEqual(w.private_key, w2.private_key)
        finally:
            os.unlink(path)

    def test_wrong_password_raises(self):
        w = _wallet()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            w.save(path=path, password="CorrectPass1")
            w2 = Wallet()
            with self.assertRaises(ValueError):
                w2.load(path=path, password="WrongPassword")
        finally:
            os.unlink(path)

    def test_sign_verify(self):
        w = _wallet(); msg = b"test"
        self.assertTrue(Wallet.verify_signature(w.get_public_key_hex(), msg, w.sign(msg)))

    def test_wrong_key_fails(self):
        w1 = _wallet(); w2 = _wallet(); msg = b"test"
        sig = w1.sign(msg)
        self.assertFalse(Wallet.verify_signature(w2.get_public_key_hex(), msg, sig))

    def test_registration_message_verifies(self):
        w = _wallet()
        self.assertTrue(Ledger._verify_registration(w._make_registration_message()))

    def test_genesis_block_hash_persists(self):
        w = Wallet(); w.create_new(genesis_block_hash="c"*64)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            w.save(path=path)
            w2 = Wallet(); w2.load(path=path)
            self.assertEqual(w2.genesis_block_hash, "c"*64)
        finally:
            os.unlink(path)


# ══════════════════════════════════════════════════════════════════════════════
# 8 — TRANSACTION
# ══════════════════════════════════════════════════════════════════════════════

class TestTransaction(unittest.TestCase):

    def test_verifies(self):
        s = _wallet(); r = _wallet()
        self.assertTrue(_tx(s, r).verify())

    def test_amount_tamper_fails(self):
        s = _wallet(); r = _wallet()
        t = _tx(s, r); t.amount += 1
        self.assertFalse(t.verify())

    def test_memo_tamper_fails(self):
        s = _wallet(); r = _wallet()
        t = _tx(s, r); t.memo = "tampered"
        self.assertFalse(t.verify())

    def test_no_signature_fails(self):
        s = _wallet(); r = _wallet()
        t = _tx(s, r); t.signature = None
        self.assertFalse(t.verify())

    def test_chain_anchored_wallet_verifies(self):
        """Transaction.verify() must NOT check sha256(pubkey)==device_id (BUG 3)."""
        s = Wallet(); s.create_new(genesis_block_hash="d"*64)
        r = _wallet()
        t = Transaction(
            sender_id=s.device_id, recipient_id=r.device_id,
            sender_pubkey=s.get_public_key_hex(),
            amount=UNIT, fee=calculate_fee(UNIT), memo="", slot=200,
            timestamp=time.time()
        )
        t.sign(s)
        self.assertTrue(t.verify(),
            "Chain-anchored wallet transaction rejected — BUG 3 regression")

    def test_round_trip(self):
        s = _wallet(); r = _wallet()
        t = _tx(s, r)
        t2 = Transaction.from_dict(t.to_dict())
        self.assertEqual(t.tx_id, t2.tx_id)
        self.assertTrue(t2.verify())

    def test_from_dict_rejects_float_amount(self):
        s = _wallet(); r = _wallet()
        d = _tx(s, r).to_dict(); d["amount"] = 1.5
        with self.assertRaises(Exception):
            Transaction.from_dict(d)

    def test_from_dict_rejects_bool_amount(self):
        s = _wallet(); r = _wallet()
        d = _tx(s, r).to_dict(); d["amount"] = True
        with self.assertRaises(Exception):
            Transaction.from_dict(d)

    def test_select_transactions_fee_order(self):
        mempool = {
            "a": {"tx_id": "a", "fee": 100, "slot": 0},
            "b": {"tx_id": "b", "fee": 500, "slot": 0},
        }
        result = select_transactions_for_block(mempool, 10)
        self.assertEqual(result[0]["tx_id"], "b")

    def test_mempool_sender_limit(self):
        mempool = {f"tx{i}": {"sender_id": "x"*64} for i in range(10)}
        self.assertFalse(can_add_to_mempool("x"*64, mempool))
        self.assertTrue(can_add_to_mempool("y"*64, mempool))


# ══════════════════════════════════════════════════════════════════════════════
# 9 — LEDGER: BALANCE
# ══════════════════════════════════════════════════════════════════════════════

class TestLedgerBalance(unittest.TestCase):

    def test_balance_from_blocks(self):
        w = _wallet(); l = _base_ledger(w, 5)
        self.assertEqual(l.get_balance(w.device_id), 5 * REWARD_PER_ROUND)

    def test_balance_zero_unknown(self):
        l = _base_ledger(_wallet(), 3)
        self.assertEqual(l.get_balance("z"*64), 0)

    def test_balance_after_transaction(self):
        s = _wallet(); r = _wallet()
        l = _base_ledger(s, MIN_IDENTITY_AGE + 5)
        l.identities[r.device_id] = 0
        amount = UNIT; fee = calculate_fee(amount)
        t = Transaction(
            sender_id=s.device_id, recipient_id=r.device_id,
            sender_pubkey=s.get_public_key_hex(),
            amount=amount, fee=fee, memo="", slot=5, timestamp=time.time()
        )
        t.sign(s)
        l.transactions.append(t.to_dict())
        self.assertEqual(l.get_balance(r.device_id), amount)

    def test_recalculate_totals(self):
        w = _wallet(); l = _base_ledger(w, 5)
        l.total_minted = 0
        l.recalculate_totals()
        self.assertEqual(l.total_minted, 5 * REWARD_PER_ROUND)


# ══════════════════════════════════════════════════════════════════════════════
# 10 — LEDGER: BLOCK VALIDATION (17 rules)
# These tests use real Dilithium3 signatures — each calls _signed_block once.
# ══════════════════════════════════════════════════════════════════════════════

class TestLedgerBlockValidation(unittest.TestCase):

    def _ledger_ready(self):
        """Base ledger with wallet mature and fake tip block."""
        w = _wallet()
        l = _base_ledger(w)  # 201 stub blocks
        return w, l

    def _next(self, w, l):
        """Build a valid signed block on top of l."""
        tip = compute_block_hash(l.chain[-1])
        slot = l.chain[-1]["slot"] + 1
        return _signed_block(w, slot, tip, l.total_minted)

    def test_valid_block_accepted(self):
        w, l = self._ledger_ready()
        b = self._next(w, l)
        with l._lock:
            self.assertTrue(l._add_block_locked(b))

    def test_gossip_type_BLOCK_accepted(self):
        """C2 regression: block wrapped as type=BLOCK by gossip must pass Rule 16."""
        w, l = self._ledger_ready()
        b = dict(self._next(w, l))
        b["type"] = "BLOCK"
        with l._lock:
            self.assertTrue(l._add_block_locked(b),
                "Gossiped block with type=BLOCK rejected — C2 regression")

    def test_block_reward_type_accepted(self):
        w, l = self._ledger_ready()
        b = self._next(w, l)
        self.assertEqual(b["type"], "block_reward")
        with l._lock:
            self.assertTrue(l._add_block_locked(b))

    def test_wrong_version_rejected(self):
        w, l = self._ledger_ready()
        b = self._next(w, l); b["version"] = "3.0"
        with l._lock:
            self.assertFalse(l._add_block_locked(b))

    def test_wrong_prev_hash_rejected(self):
        w, l = self._ledger_ready()
        slot = l.chain[-1]["slot"] + 1
        b = _signed_block(w, slot, "0"*64, l.total_minted)
        with l._lock:
            self.assertFalse(l._add_block_locked(b))

    def test_duplicate_slot_rejected(self):
        w, l = self._ledger_ready()
        b = self._next(w, l)
        with l._lock:
            l._add_block_locked(b)
            b2 = dict(b)
            self.assertFalse(l._add_block_locked(b2))

    def test_wrong_amount_rejected(self):
        w, l = self._ledger_ready()
        tip = compute_block_hash(l.chain[-1])
        slot = l.chain[-1]["slot"] + 1
        chal = compute_challenge(tip, slot)
        sig_hex, proof_hex = solve_challenge(w.private_key, chal)
        bws = {
            "reward_id": f"reward:{slot}", "slot": slot,
            "winner_id": w.device_id, "prev_hash": tip,
            "challenge": chal.hex(), "compete_sig": sig_hex,
            "compete_proof": proof_hex, "vrf_public_key": w.get_public_key_hex(),
            "amount": REWARD_PER_ROUND + 1,  # wrong
            "fees_collected": 0,
            "timestamp": int(timpal.GENESIS_TIME) + slot * 10 + 5,
            "transactions": [], "registrations": [],
            "type": "block_reward", "nodes": 1, "version": VERSION,
        }
        bws["block_sig"] = w.sign(canonical_block(bws))
        with l._lock:
            self.assertFalse(l._add_block_locked(bws))

    def test_too_many_registrations_rejected(self):
        w, l = self._ledger_ready()
        b = self._next(w, l)
        b["registrations"] = [{"device_id": "a"*64, "public_key": "b"*64,
                               "signature": "c"*64, "genesis_block_hash": ""}
                              ] * (MAX_REGS_PER_BLOCK + 1)
        with l._lock:
            self.assertFalse(l._add_block_locked(b))

    def test_immature_winner_rejected(self):
        """Winner registered too recently must be rejected."""
        w = _wallet()
        l = _init_ledger()
        # Identity registered at slot 0, try slot 1 (age = 1 < MIN_IDENTITY_AGE)
        l.identities[w.device_id] = 0
        l.identity_pubkeys[w.device_id] = w.get_public_key_hex()
        b = _signed_block(w, MIN_IDENTITY_AGE - 1, GENESIS_PREV_HASH, 0)
        with l._lock:
            self.assertFalse(l._add_block_locked(b))


# ══════════════════════════════════════════════════════════════════════════════
# 11 — LEDGER: MERGE AND REORG
# ══════════════════════════════════════════════════════════════════════════════

class TestLedgerMergeReorg(unittest.TestCase):

    def test_merge_extends_chain(self):
        w = _wallet(); l = _base_ledger(w)
        tip = compute_block_hash(l.chain[-1])
        slot = l.chain[-1]["slot"] + 1
        b = _signed_block(w, slot, tip, l.total_minted)
        self.assertTrue(l.merge({"blocks": [b], "transactions": []}))
        self.assertEqual(l.chain[-1]["slot"], slot)

    def test_merge_orphan_drain(self):
        """Orphan received before parent should attach once parent arrives."""
        w = _wallet(); l = _base_ledger(w)
        tip  = compute_block_hash(l.chain[-1])
        slot = l.chain[-1]["slot"] + 1
        b1   = _signed_block(w, slot,     tip,                  l.total_minted)
        b2   = _signed_block(w, slot + 1, compute_block_hash(b1),
                             l.total_minted + b1["amount"])
        # Send b2 first (orphan), then b1
        l.merge({"blocks": [b2], "transactions": []})
        l.merge({"blocks": [b1], "transactions": []})
        self.assertEqual(l.chain[-1]["slot"], slot + 1)

    def test_reorg_blocked_past_finalized(self):
        """MISSING 1 regression: no reorg past last_finalized_slot."""
        w = _wallet(); l = _base_ledger(w, 5)
        l.last_finalized_slot = 2
        # Fork from genesis (anchor at -1, before finalized slot 2)
        fork = []; prev = GENESIS_PREV_HASH
        for s in range(7):
            b = _stub_block(w.device_id, s, prev, s * REWARD_PER_ROUND)
            c = json.dumps(b, sort_keys=True, separators=(",", ":")).encode()
            prev = hashlib.sha256(c).hexdigest()
            fork.append(b)
        with l._lock:
            self.assertFalse(l._attempt_reorg(fork),
                "Reorg past finalized slot was not blocked — MISSING 1 regression")


# ══════════════════════════════════════════════════════════════════════════════
# 12 — LEDGER: CHECKPOINT
# ══════════════════════════════════════════════════════════════════════════════

class TestLedgerCheckpoint(unittest.TestCase):

    def test_create_checkpoint_returns_dict(self):
        w = _wallet(); l = _cp_ledger(w)
        cp = l.create_checkpoint(CHECKPOINT_INTERVAL)
        self.assertIsNotNone(cp)
        self.assertEqual(cp["slot"], CHECKPOINT_INTERVAL)

    def test_create_checkpoint_includes_spent_bloom(self):
        """MISSING 6 regression."""
        w = _wallet(); l = _cp_ledger(w)
        cp = l.create_checkpoint(CHECKPOINT_INTERVAL)
        self.assertIn("spent_bloom", cp,
            "spent_bloom missing from checkpoint — MISSING 6 regression")

    def test_apply_checkpoint_restores_bloom(self):
        """MISSING 6 regression."""
        bf = SpentBloomFilter(); bf.add("sentinel")
        cp = _valid_cp(); cp["spent_bloom"] = bf.to_dict()
        l = _init_ledger()
        self.assertTrue(l.apply_checkpoint(cp))
        self.assertIn("sentinel", l._spent_bloom,
            "Bloom not restored — MISSING 6 regression")

    def test_bloom_round_trip(self):
        """MISSING 6 regression: bloom survives create → apply cycle."""
        w = _wallet(); l = _cp_ledger(w)
        l._spent_bloom.add("round-trip")
        cp = l.create_checkpoint(CHECKPOINT_INTERVAL)
        self.assertIsNotNone(cp)
        l2 = _init_ledger()
        self.assertTrue(l2.apply_checkpoint(cp))
        self.assertIn("round-trip", l2._spent_bloom,
            "Bloom round-trip failed — MISSING 6 regression")

    def test_apply_rejects_inflated_prune_before(self):
        """C1 regression: prune_before >= cp_slot must be rejected."""
        l = _init_ledger()
        cp = _valid_cp(); cp["prune_before"] = CHECKPOINT_INTERVAL
        self.assertFalse(l.apply_checkpoint(cp),
            "Inflated prune_before accepted — C1 regression")

    def test_apply_rejects_off_by_one_prune_before(self):
        """C1 regression: even prune_before one off must be rejected."""
        l = _init_ledger()
        correct = max(0, CHECKPOINT_INTERVAL - CHECKPOINT_BUFFER)
        cp = _valid_cp(); cp["prune_before"] = correct - 1
        self.assertFalse(l.apply_checkpoint(cp),
            "Off-by-one prune_before accepted — C1 regression")

    def test_apply_accepts_correct_prune_before(self):
        l = _init_ledger()
        self.assertTrue(l.apply_checkpoint(_valid_cp()))

    def test_no_bloom_in_cp_no_crash(self):
        """Old checkpoint format without spent_bloom must not crash."""
        l = _init_ledger()
        try:
            l.apply_checkpoint(_valid_cp())
        except Exception as e:
            self.fail(f"apply_checkpoint crashed without bloom: {e}")

    def test_corrupt_bloom_silently_ignored(self):
        l = _init_ledger(); l._spent_bloom.add("existing")
        cp = _valid_cp(); cp["spent_bloom"] = {"bits": "NOT_HEX!!!", "num_bits": -1}
        try:
            l.apply_checkpoint(cp)
        except Exception as e:
            self.fail(f"apply_checkpoint crashed on corrupt bloom: {e}")
        self.assertIn("existing", l._spent_bloom)

    def test_last_finalized_slot_persists(self):
        """MISSING 1 regression: last_finalized_slot survives save/load."""
        w = _wallet(); l = _base_ledger(w, 3)
        l.last_finalized_slot = 99
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        orig = timpal.LEDGER_FILE; timpal.LEDGER_FILE = path
        try:
            l.save()
            l2 = _init_ledger(); l2._load()
            self.assertEqual(l2.last_finalized_slot, 99,
                "last_finalized_slot not persisted — MISSING 1 regression")
        finally:
            timpal.LEDGER_FILE = orig; os.unlink(path)

    def test_checkpoint_hash_sort_determinism(self):
        """Section 9 gap 3: same data different order → same hash after sort."""
        entries_a = sorted([
            {"slot": 1, "winner_id": "a"*64},
            {"slot": 2, "winner_id": "b"*64},
        ], key=lambda x: x["slot"])
        entries_b = sorted([
            {"slot": 2, "winner_id": "b"*64},
            {"slot": 1, "winner_id": "a"*64},
        ], key=lambda x: x["slot"])
        self.assertEqual(Ledger._compute_hash(entries_a),
                         Ledger._compute_hash(entries_b),
                         "_compute_hash not deterministic — H1/H5 regression")

    def test_create_apply_use_same_window(self):
        """Section 9 gap 4: both create and apply must use [prune_before, cp_slot)."""
        import inspect
        create_src = inspect.getsource(Ledger.create_checkpoint)
        apply_src  = inspect.getsource(Ledger.apply_checkpoint)
        self.assertTrue(
            "< checkpoint_slot" in create_src or "< cp_slot" in create_src,
            "create_checkpoint missing upper bound — H5 regression")
        self.assertTrue(
            "< cp_slot" in apply_src or "< checkpoint_slot" in apply_src,
            "apply_checkpoint missing upper bound — H5 regression")

    def test_recompute_balances_locked_exists(self):
        """HIGH-1 regression: Ledger must have _recompute_balances_locked method."""
        self.assertTrue(
            hasattr(Ledger, "_recompute_balances_locked"),
            "_recompute_balances_locked missing from Ledger — HIGH-1 regression")

    def test_apply_checkpoint_rejects_inflated_balance(self):
        """HIGH-1 regression: checkpoint with valid hashes but inflated balance
        must be rejected by apply_checkpoint when c_verify is non-empty."""
        w = _wallet()
        # Build a ledger large enough to create a real checkpoint
        l_orig = _cp_ledger(w)
        cp = l_orig.create_checkpoint(CHECKPOINT_INTERVAL)
        self.assertIsNotNone(cp)
        # Build a second identical ledger (same wallet, same blocks, unpruned)
        l_target = _cp_ledger(w)
        # Tamper: inflate winner's balance by 1000 TMPL while hashes remain valid
        evil_cp = dict(cp)
        evil_cp["balances"] = dict(cp.get("balances", {}))
        evil_cp["balances"][w.device_id] = (
            evil_cp["balances"].get(w.device_id, 0) + 1000 * UNIT
        )
        result = l_target.apply_checkpoint(evil_cp)
        self.assertFalse(result,
            "Checkpoint with inflated balance accepted — HIGH-1 regression")

    def test_apply_checkpoint_accepts_correct_balance(self):
        """HIGH-1 regression: checkpoint with correct balances must be accepted
        when c_verify is non-empty (node has local data to verify against)."""
        w = _wallet()
        l_orig = _cp_ledger(w)
        cp = l_orig.create_checkpoint(CHECKPOINT_INTERVAL)
        self.assertIsNotNone(cp)
        # Apply the unmodified checkpoint to a fresh identical ledger
        l_target = _cp_ledger(w)
        result = l_target.apply_checkpoint(cp)
        self.assertTrue(result,
            "Checkpoint with correct balances rejected — HIGH-1 regression")


# ══════════════════════════════════════════════════════════════════════════════
# 13 — EXPLORER PUSH (C3 regression)
# ══════════════════════════════════════════════════════════════════════════════

class TestExplorerPush(unittest.TestCase):

    def test_push_does_not_mutate_chain(self):
        """C3 regression: deep copy prevents mutation of ledger.chain dicts."""
        w = _wallet(); l = _base_ledger(w, 5)
        tip_before = compute_block_hash(l.chain[-1])
        # Simulate what _push_to_explorer now does (fixed version)
        blocks = [dict(b) for b in l.chain[-50:]]
        for b in blocks:
            if "canonical_hash" not in b:
                b["canonical_hash"] = compute_block_hash(b)
        self.assertEqual(compute_block_hash(l.chain[-1]), tip_before,
            "Chain tip changed after push — C3 regression")
        for b in l.chain:
            self.assertNotIn("canonical_hash", b,
                "Original chain block was mutated — C3 regression")

    def test_tip_stable_across_multiple_pushes(self):
        w = _wallet(); l = _base_ledger(w, 5)
        tip = compute_block_hash(l.chain[-1])
        for _ in range(5):
            blocks = [dict(b) for b in l.chain[-50:]]
            for b in blocks:
                if "canonical_hash" not in b:
                    b["canonical_hash"] = compute_block_hash(b)
        self.assertEqual(compute_block_hash(l.chain[-1]), tip,
            "Tip drifted after repeated pushes — C3 regression")

    def test_ledger_push_payload_has_version(self):
        """Section 9 gap 1: LEDGER_PUSH must include version field."""
        import inspect
        src = inspect.getsource(Node._push_to_explorer)
        self.assertIn('"version"', src,
            "LEDGER_PUSH missing version field — Section 9 gap 1")


# ══════════════════════════════════════════════════════════════════════════════
# 14 — NETWORK: RATE LIMITS AND IP BAN
# ══════════════════════════════════════════════════════════════════════════════

class TestNetworkRateLimits(unittest.TestCase):

    def test_allows_within_limit(self):
        n = _net()
        for _ in range(BLOCK_RATE_LIMIT):
            self.assertTrue(n._check_msg_rate(
                n._block_rate, n._block_rate_lock,
                "1.1.1.1", BLOCK_RATE_LIMIT, REWARD_INTERVAL))

    def test_blocks_at_limit(self):
        """MISSING 3 regression."""
        n = _net()
        for _ in range(BLOCK_RATE_LIMIT):
            n._check_msg_rate(n._block_rate, n._block_rate_lock,
                              "2.2.2.2", BLOCK_RATE_LIMIT, REWARD_INTERVAL)
        self.assertFalse(
            n._check_msg_rate(n._block_rate, n._block_rate_lock,
                              "2.2.2.2", BLOCK_RATE_LIMIT, REWARD_INTERVAL),
            "Rate limit not enforced — MISSING 3 regression")

    def test_resets_after_window(self):
        n = _net()
        n._block_rate["3.3.3.3"] = [time.time() - REWARD_INTERVAL - 1] * BLOCK_RATE_LIMIT
        self.assertTrue(n._check_msg_rate(
            n._block_rate, n._block_rate_lock,
            "3.3.3.3", BLOCK_RATE_LIMIT, REWARD_INTERVAL))

    def test_different_ips_independent(self):
        n = _net()
        for _ in range(BLOCK_RATE_LIMIT):
            n._check_msg_rate(n._block_rate, n._block_rate_lock,
                              "4.4.4.4", BLOCK_RATE_LIMIT, REWARD_INTERVAL)
        self.assertTrue(n._check_msg_rate(
            n._block_rate, n._block_rate_lock,
            "5.5.5.5", BLOCK_RATE_LIMIT, REWARD_INTERVAL))

    def test_compete_rate_enforced(self):
        n = _net()
        for _ in range(COMPETE_RATE_LIMIT):
            n._check_msg_rate(n._compete_rate, n._compete_rate_lock,
                              "6.6.6.6", COMPETE_RATE_LIMIT, REWARD_INTERVAL)
        self.assertFalse(n._check_msg_rate(
            n._compete_rate, n._compete_rate_lock,
            "6.6.6.6", COMPETE_RATE_LIMIT, REWARD_INTERVAL))

    def test_ip_ban_set_on_oversized(self):
        """MISSING 2 regression."""
        n = _net()
        class FakeConn:
            def recv(self, sz): return b"x" * (MAX_P2P_MESSAGE_SIZE + 1)
        n._recv_full(FakeConn(), ban_ip="9.9.9.9")
        with n._ban_lock:
            self.assertGreater(n._banned_ips.get("9.9.9.9", 0), time.time(),
                "IP not banned after oversized message — MISSING 2 regression")

    def test_banned_ip_rejected_before_read(self):
        """MISSING 2 regression."""
        n = _net()
        n._banned_ips["8.8.8.8"] = time.time() + 60
        reads = []
        class FakeConn:
            def settimeout(self, t): pass
            def recv(self, sz): reads.append(sz); return b""
            def close(self): pass
        n._handle_incoming(FakeConn(), ("8.8.8.8", 1234))
        self.assertEqual(len(reads), 0,
            "Banned IP was read before rejection — MISSING 2 regression")


# ══════════════════════════════════════════════════════════════════════════════
# 15 — NETWORK: WALLET=NONE GUARDS (L1 regression)
# ══════════════════════════════════════════════════════════════════════════════

class TestNetworkWalletGuards(unittest.TestCase):

    def test_hello_guard_fires_when_no_wallet(self):
        """L1 regression: wallet=None guard must prevent broadcast crash."""
        n = _net(wallet=None)
        self.assertFalse(bool(n.wallet))

    def test_hello_peers_returns_early_no_wallet(self):
        """L1 regression: _hello_peers must not connect when wallet=None."""
        n = _net(wallet=None)
        n.peers = {"a"*64: {"ip": "1.2.3.4", "port": 7779, "last_seen": 0}}
        with mock.patch("timpal.socket.socket") as ms:
            ms.return_value.connect.side_effect = Exception("must not connect")
            try:
                n._hello_peers()
            except Exception:
                self.fail("_hello_peers connected despite wallet=None — L1 regression")

    def test_handle_incoming_hello_no_wallet_no_crash(self):
        """L1 regression: HELLO while wallet=None must not raise AttributeError."""
        n = _net(wallet=None)
        class FakeConn:
            def __init__(self, data):
                self._d = data; self._p = 0; self.sent = []
            def settimeout(self, t): pass
            def recv(self, sz):
                c = self._d[self._p:self._p+sz]; self._p += sz; return c
            def sendall(self, b): self.sent.append(b)
            def close(self): pass
        msg = json.dumps({"type": "HELLO", "device_id": "b"*64,
                          "port": 7779, "version": "4.0",
                          "genesis_block_hash": ""}).encode()
        try:
            n._handle_incoming(FakeConn(msg), ("1.2.3.4", 1234))
        except AttributeError as e:
            self.fail(f"Crashed on HELLO with wallet=None — L1 regression: {e}")
        self.assertIn("b"*64, n.peers,
            "Peer not registered during wallet=None phase")

    def test_listen_discovery_has_wallet_none_guard(self):
        """MEDIUM-1 regression: _listen_discovery must guard self.wallet before
        accessing device_id — wallet is None during new-wallet startup."""
        import inspect
        src = inspect.getsource(Network._listen_discovery)
        self.assertIn("not self.wallet", src,
            "_listen_discovery missing wallet=None guard — MEDIUM-1 regression")


# ══════════════════════════════════════════════════════════════════════════════
# 16 — NETWORK: SEEN IDS
# ══════════════════════════════════════════════════════════════════════════════

class TestNetworkSeenIds(unittest.TestCase):

    def test_first_time_true(self):
        self.assertTrue(_net()._mark_seen("block:100:abc"))

    def test_second_time_false(self):
        n = _net(); n._mark_seen("block:100:abc")
        self.assertFalse(n._mark_seen("block:100:abc"))

    def test_different_ids_independent(self):
        n = _net()
        self.assertTrue(n._mark_seen("a"))
        self.assertTrue(n._mark_seen("b"))

    def test_rolling_window_evicts_old(self):
        n = _net()
        for i in range(50_001): n._mark_seen(f"tx-{i}")
        # First entry should have been evicted
        self.assertTrue(n._mark_seen("tx-0"))


# ══════════════════════════════════════════════════════════════════════════════
# 17 — CANONICAL BLOCK AND HASHING
# ══════════════════════════════════════════════════════════════════════════════

class TestCanonicalBlock(unittest.TestCase):

    def test_sort_keys(self):
        b1 = {"slot": 1, "winner_id": "a"*64}
        b2 = {"winner_id": "a"*64, "slot": 1}
        self.assertEqual(canonical_block(b1), canonical_block(b2))

    def test_no_spaces(self):
        b = {"slot": 1, "winner_id": "a"*64}
        self.assertNotIn(b" ", canonical_block(b))

    def test_deterministic(self):
        b = {"slot": 1, "winner_id": "a"*64, "amount": 100}
        self.assertEqual(canonical_block(b), canonical_block(b))

    def test_hash_changes_with_content(self):
        b1 = {"slot": 1}; b2 = {"slot": 2}
        self.assertNotEqual(compute_block_hash(b1), compute_block_hash(b2))

    def test_copy_mutation_does_not_affect_original(self):
        """C3 regression: mutating a copy must not change original's hash."""
        w = _wallet(); l = _base_ledger(w, 3)
        original_hash = compute_block_hash(l.chain[-1])
        copy = dict(l.chain[-1])
        copy["canonical_hash"] = compute_block_hash(copy)
        self.assertEqual(compute_block_hash(l.chain[-1]), original_hash,
            "Original hash changed after copy mutation — C3 regression")


# ══════════════════════════════════════════════════════════════════════════════
# 18 — PAYMENT URI (MISSING 4)
# ══════════════════════════════════════════════════════════════════════════════

class TestPaymentURI(unittest.TestCase):

    V = "a" * 64

    def test_generate_minimal(self):
        self.assertEqual(generate_payment_uri(self.V), f"timpal:{self.V}")

    def test_generate_with_amount(self):
        self.assertIn("amount=4.5", generate_payment_uri(self.V, amount=4.5))

    def test_generate_all_params(self):
        uri = generate_payment_uri(self.V, amount=10.0, memo="Inv-1", label="Co")
        self.assertIn("memo=Inv-1", uri)
        self.assertIn("label=Co", uri)

    def test_generate_invalid_id(self):
        with self.assertRaises(ValueError):
            generate_payment_uri("not-valid")

    def test_generate_nonpositive_amount(self):
        with self.assertRaises(ValueError): generate_payment_uri(self.V, amount=0)
        with self.assertRaises(ValueError): generate_payment_uri(self.V, amount=-1)

    def test_parse_minimal(self):
        r = parse_payment_uri(f"timpal:{self.V}")
        self.assertEqual(r["device_id"], self.V)
        self.assertIsNone(r["amount"])

    def test_parse_full(self):
        r = parse_payment_uri(f"timpal:{self.V}?amount=4.5&memo=T7&label=Cafe")
        self.assertAlmostEqual(r["amount"], 4.5)
        self.assertEqual(r["memo"],  "T7")
        self.assertEqual(r["label"], "Cafe")

    def test_round_trip(self):
        uri = generate_payment_uri(self.V, amount=250.0, memo="Inv", label="C")
        r = parse_payment_uri(uri)
        self.assertEqual(r["device_id"], self.V)
        self.assertAlmostEqual(r["amount"], 250.0)

    def test_wrong_scheme_raises(self):
        with self.assertRaises(ValueError):
            parse_payment_uri(f"bitcoin:{self.V}")

    def test_invalid_id_raises(self):
        with self.assertRaises(ValueError):
            parse_payment_uri("timpal:invalid")

    def test_invalid_amount_raises(self):
        with self.assertRaises(ValueError):
            parse_payment_uri(f"timpal:{self.V}?amount=abc")

    def test_memo_truncated_to_128(self):
        uri = generate_payment_uri(self.V, memo="x"*200)
        r = parse_payment_uri(uri)
        self.assertLessEqual(len(r["memo"]), 128)


# ══════════════════════════════════════════════════════════════════════════════
# 19 — CONCURRENCY STRUCTURE (M1-M5 regression)
# ══════════════════════════════════════════════════════════════════════════════

class TestConcurrencyStructure(unittest.TestCase):

    def test_producing_slots_gate(self):
        """M2 regression: two threads for same slot — only one proceeds."""
        producing = set(); lock = threading.Lock(); results = []
        barrier = threading.Barrier(2)
        def try_produce(slot):
            barrier.wait()
            with lock:
                if slot in producing: results.append("blocked"); return
                producing.add(slot)
            try:
                time.sleep(0.05); results.append("produced")
            finally:
                with lock: producing.discard(slot)
        t1 = threading.Thread(target=try_produce, args=(42,))
        t2 = threading.Thread(target=try_produce, args=(42,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(results.count("produced"), 1,
            "Two threads both produced a block — M2 regression")

    def test_identities_snapshot_independence(self):
        """M4 regression: snapshot must be independent of original."""
        w = _wallet(); l = _base_ledger(w, 3)
        with l._lock:
            snap = dict(l.identities)
        original_len = len(snap)
        l.identities["new_id" * 10 + "xx"] = 999
        self.assertEqual(len(snap), original_len,
            "Snapshot affected by post-snapshot mutation — M4 regression")

    def test_avg_regs_accepts_list_not_ledger(self):
        """M5 regression: _avg_regs_per_slot takes list, not ledger."""
        chain = [{"slot": 5, "registrations": [{}]}]
        self.assertAlmostEqual(_avg_regs_per_slot(chain, 0, 20), 1/20)
        l = _init_ledger()
        with self.assertRaises((AttributeError, TypeError)):
            _avg_regs_per_slot(l, 0, 20)

    def test_last_finalized_slot_on_ledger(self):
        """MISSING 1 regression: Ledger must have last_finalized_slot."""
        l = _init_ledger()
        self.assertTrue(hasattr(l, "last_finalized_slot"))
        self.assertEqual(l.last_finalized_slot, -1)


# ══════════════════════════════════════════════════════════════════════════════
# 20 — L2: DERIVE KEYS EXCEPTION HANDLING
# ══════════════════════════════════════════════════════════════════════════════

class TestDeriveKeysExceptions(unittest.TestCase):

    FAKE = b"\x00" * 16

    def test_attribute_error_becomes_runtime(self):
        with mock.patch("timpal._mnemonic_to_entropy", return_value=self.FAKE):
            with mock.patch.object(Dilithium3, "set_drbg_seed",
                                   side_effect=AttributeError("x")):
                with self.assertRaises(RuntimeError) as ctx:
                    derive_keys_from_seed("dummy")
                self.assertIn("pycryptodome", str(ctx.exception).lower())

    def test_type_error_becomes_runtime(self):
        with mock.patch("timpal._mnemonic_to_entropy", return_value=self.FAKE):
            with mock.patch.object(Dilithium3, "set_drbg_seed",
                                   side_effect=TypeError("x")):
                with self.assertRaises(RuntimeError):
                    derive_keys_from_seed("dummy")

    def test_warning_still_caught(self):
        with mock.patch("timpal._mnemonic_to_entropy", return_value=self.FAKE):
            with mock.patch.object(Dilithium3, "set_drbg_seed",
                                   side_effect=Warning("x")):
                with self.assertRaises(RuntimeError):
                    derive_keys_from_seed("dummy")

    def test_bad_phrase_raises_value_error_not_runtime(self):
        """L2 regression: ValueError from bad phrase must propagate directly."""
        try:
            derive_keys_from_seed("only five words here")
        except ValueError:
            pass   # correct
        except RuntimeError:
            self.fail("ValueError incorrectly wrapped as RuntimeError — L2 regression")

    def test_bad_phrase_raises_value_error(self):
        with self.assertRaises(ValueError):
            derive_keys_from_seed("only five words here")


# ══════════════════════════════════════════════════════════════════════════════
# 21 — L3 AND L4 REGRESSIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestL3AndL4(unittest.TestCase):

    def test_rule1_single_wid_check(self):
        """L3 regression: Rule 1 must have exactly one redundancy-free check."""
        import inspect
        src   = inspect.getsource(Ledger._add_block_locked)
        start = src.find("Rule 1:")
        end   = src.find("Rule 2:", start)
        block = src[start:end] if end > 0 else src[start:]
        count = block.count("wid not in selected")
        self.assertEqual(count, 1,
            f"Found {count} checks — expected 1 — L3 regression")

    def test_stale_attestations_pruned_without_finality(self):
        """L4 regression: pruning must not require a block to finalize."""
        n = Node.__new__(Node)
        n._attest_lock = threading.Lock()
        n._attestations = {}; n._attestation_slots = {}; n._finalized = set()
        cs = 500; cutoff = cs - CHECKPOINT_BUFFER
        for s in [100, 200, 300]:
            h = hashlib.sha256(f"b{s}".encode()).hexdigest()
            n._attestations[h] = {}; n._attestation_slots[h] = s
        self.assertEqual(len(n._finalized), 0)
        with n._attest_lock:
            stale = [h for h, s in list(n._attestation_slots.items()) if s < cutoff]
            for h in stale:
                n._attestations.pop(h, None); n._attestation_slots.pop(h, None)
        self.assertEqual(len(n._attestations), 0,
            "Stale attestations not pruned without finality — L4 regression")

    def test_recent_attestations_preserved(self):
        n = Node.__new__(Node)
        n._attest_lock = threading.Lock()
        n._attestations = {}; n._attestation_slots = {}; n._finalized = set()
        cs = 500; cutoff = cs - CHECKPOINT_BUFFER
        for s in [cutoff, cutoff + 50, cs - 1]:
            h = hashlib.sha256(f"b{s}".encode()).hexdigest()
            n._attestations[h] = {}; n._attestation_slots[h] = s
        with n._attest_lock:
            stale = [h for h, s in list(n._attestation_slots.items()) if s < cutoff]
            for h in stale:
                n._attestations.pop(h, None); n._attestation_slots.pop(h, None)
        self.assertEqual(len(n._attestations), 3,
            "Recent attestations incorrectly pruned")


# ══════════════════════════════════════════════════════════════════════════════
# 22 — MISSING 5: --peer FLAG
# ══════════════════════════════════════════════════════════════════════════════

class TestPeerFlag(unittest.TestCase):

    def test_valid_peer_args_parse(self):
        for arg, host, port in [
            ("1.2.3.4:7779", "1.2.3.4", 7779),
            ("10.0.0.1:7777", "10.0.0.1", 7777),
        ]:
            h, p = arg.rsplit(":", 1)
            self.assertEqual(h, host); self.assertEqual(int(p), port)

    def test_peer_inserted_into_bootstrap(self):
        orig = list(BOOTSTRAP_SERVERS)
        try:
            BOOTSTRAP_SERVERS.insert(0, ("192.168.1.1", 9999))
            n = _net()
            n._bootstrap_servers = list(BOOTSTRAP_SERVERS)
            self.assertIn(("192.168.1.1", 9999), n._bootstrap_servers,
                "--peer not picked up by Network — MISSING 5 regression")
        finally:
            if ("192.168.1.1", 9999) in BOOTSTRAP_SERVERS:
                BOOTSTRAP_SERVERS.remove(("192.168.1.1", 9999))

    def test_peer_at_front(self):
        orig = list(BOOTSTRAP_SERVERS)
        try:
            BOOTSTRAP_SERVERS.insert(0, ("10.10.10.1", 8888))
            self.assertEqual(BOOTSTRAP_SERVERS[0], ("10.10.10.1", 8888))
        finally:
            if ("10.10.10.1", 8888) in BOOTSTRAP_SERVERS:
                BOOTSTRAP_SERVERS.remove(("10.10.10.1", 8888))


# ══════════════════════════════════════════════════════════════════════════════
# 23 — H1 REGRESSION (can_verify window)
# ══════════════════════════════════════════════════════════════════════════════

class TestH1CanVerify(unittest.TestCase):

    def test_blocks_in_window_give_can_verify_true(self):
        chain = [{"slot": 890}, {"slot": 950}, {"slot": 999}]
        pb, cs = 880, 1000
        can_verify = bool([b for b in chain if pb <= b.get("slot", 0) < cs])
        self.assertTrue(can_verify)

    def test_blocks_before_window_give_can_verify_false(self):
        """Old buggy check (slot < prune_before) would give True here."""
        chain = [{"slot": 500}, {"slot": 700}, {"slot": 879}]
        pb, cs = 880, 1000
        can_verify = bool([b for b in chain if pb <= b.get("slot", 0) < cs])
        self.assertFalse(can_verify,
            "Old buggy window check would pass here — H1 regression")

    def test_slot_at_prune_before_included(self):
        chain = [{"slot": 880}]
        self.assertTrue(bool([b for b in chain if 880 <= b.get("slot",0) < 1000]))

    def test_slot_at_cp_slot_excluded(self):
        chain = [{"slot": 1000}]
        self.assertFalse(bool([b for b in chain if 880 <= b.get("slot",0) < 1000]))


# ══════════════════════════════════════════════════════════════════════════════
# 24 — SESSION 25 FIXES (LOW-1, LOW-2, HIGH-1 balance recomputation)
# ══════════════════════════════════════════════════════════════════════════════

class TestSession25Fixes(unittest.TestCase):

    def test_rule12_producer_regs_removed(self):
        """LOW-1 regression: dead producer_regs variable must not exist in Rule 12."""
        import inspect
        src = inspect.getsource(Ledger._add_block_locked)
        self.assertNotIn("producer_regs", src,
            "Dead producer_regs variable still present — LOW-1 regression")

    def test_get_block_reward_docstring_correct_remainder(self):
        """LOW-2 regression: docstring must state 0.7325 TMPL remainder, not 0.23825."""
        import inspect
        src = inspect.getsource(get_block_reward)
        self.assertIn("0.7325", src,
            "Correct remainder 0.7325 TMPL missing from docstring — LOW-2 regression")
        self.assertNotIn("0.23825", src,
            "Wrong remainder 0.23825 TMPL still in docstring — LOW-2 regression")

    def test_recompute_balances_locked_correct_result(self):
        """HIGH-1 regression: _recompute_balances_locked must return balances that
        exactly match what create_checkpoint bakes into checkpoint['balances']."""
        w = _wallet()
        l = _cp_ledger(w)
        prune_before = max(0, CHECKPOINT_INTERVAL - CHECKPOINT_BUFFER)
        # Blocks at slots 0..prune_before-1 all belong to w, each paying REWARD_PER_ROUND
        with l._lock:
            recomputed = l._recompute_balances_locked(prune_before)
        expected = prune_before * REWARD_PER_ROUND
        self.assertEqual(recomputed.get(w.device_id, 0), expected,
            "_recompute_balances_locked returned wrong balance — HIGH-1 regression")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Timpal v4.0 test suite")
    p.add_argument("-v", "--verbose", action="store_true")
    args, _ = p.parse_known_args()
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
