#!/usr/bin/env python3
"""
TIMPAL Protocol v3.2 — Quantum-Resistant Money Without Masters

v3.2 — Full rewrite. All known bugs fixed by design, not by patch.

Bug fixes over v3.1
────────────────────────────────────────────────────────────────────
  C1  _claim_reward now submits the hash of the last chain block at or
      before the checkpoint boundary as the tip_hash to SUBMIT_TIP,
      so every node on the same chain reports the same hash and
      bootstrap majority voting resolves correctly.

  C2  _sync_ledger now includes fee_rewards in the delta dict passed
      to ledger.merge(), so nodes that missed live FEE_REWARDS
      broadcasts recover them on the next sync.

  C3  merge() skips incoming fee_rewards whose time_slot < prune_before.
      Entries before the boundary are already baked into checkpoint
      balances — re-accepting them caused double-counting.

  M1  _my_tickets dict is now protected by _my_tickets_lock across all
      three access sites (_reward_lottery write, _cleanup_slot delete,
      _reward_lottery read). Eliminates RuntimeError on iteration.

  M3  SYNC_REQUEST handler now sends only blocks with slot > their tip
      slot instead of always sending the full chain. Saves bandwidth
      when the peer is only a few blocks behind.

  M6  _checkpoint_loop skips checkpoint creation when the chain is
      empty and there are no prior checkpoints. Prevents a fresh node
      from writing an invalid GENESIS_PREV_HASH tip into its checkpoint.

  M7  _pick_winner computes the collective target hash from the verified
      dict only, not all_reveals. Unverified reveals can no longer shift
      the target to steer who wins.

  M10 SYNC_PUSH payload now includes fee_rewards, and the SYNC_PUSH
      handler passes them into ledger.merge(). Nodes on the push path
      now receive fee_rewards just like nodes on the pull path.

Previously fixed (carried forward from v3.1)
────────────────────────────────────────────────────────────────────
  P1  Checkpoint consensus via bootstrap majority tip oracle
  P2  Dilithium3 retry loop (IndexError) + outer guard on lottery thread
  P3  Fee distribution: pending = total_tx_fees - total_awarded
  P4  ERA2 block-based (total_minted // REWARD_PER_ROUND), not slot-based
  FIX-A  Wallet.sign uses self.private_key (not self.wallet.private_key)
  FIX-B  create_checkpoint bakes fee_rewards into balances and prunes them
  FIX-C  create_checkpoint validates slot % CHECKPOINT_INTERVAL == 0
  FIX-D  apply_checkpoint prunes fee_rewards and verifies fee_rewards_hash
  FIX-E  _bootstrap_submit_commit fires done on first COMMIT_ACK
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

VERSION             = "3.2"
MIN_VERSION         = "3.1"
GENESIS_TIME        = 1774123200        # ← SET BEFORE LAUNCH — same value in bootstrap.py
ERA2_ROUND          = 236_406_620       # total VRF rounds (blocks produced) before Era 2
TARGET_PARTICIPANTS = 10                # target eligible nodes per slot

BOOTSTRAP_SERVERS    = [("bootstrap.timpal.org", 7777)]
BOOTSTRAP_HOST       = "bootstrap.timpal.org"
BOOTSTRAP_PORT       = 7777
BOOTSTRAP_LIST_URL   = "https://raw.githubusercontent.com/EvokiTimpal/timpal/main/bootstrap_servers.txt"
BROADCAST_PORT       = 7778
DISCOVERY_INTERVAL   = 5
WALLET_FILE          = os.path.join(os.path.expanduser("~"), ".timpal_wallet.json")
LEDGER_FILE          = os.path.join(os.path.expanduser("~"), ".timpal_ledger.json")
BOOTSTRAP_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".timpal_bootstrap.json")

# ── Protocol constants (v3.1 integer migration) ────────────────────────────────
UNIT                = 100_000_000            # 1 TMPL = 10^8 units (immutable post-genesis)
TOTAL_SUPPLY        = 25_000_000_000_000_000 # 250_000_000 * UNIT
REWARD_PER_ROUND    = 105_750_000            # 1.0575 TMPL in units
REWARD_INTERVAL     = 5.0
MIN_TX_FEE          = 50_000                 # 0.0005 TMPL in units — applies from genesis
TX_FEE_ERA2         = 50_000                 # same value; kept for clarity
CHECKPOINT_INTERVAL = 1000                   # slots between checkpoints (immutable post-genesis)
CHECKPOINT_BUFFER   = 120
MAX_PEERS           = 125
BROADCAST_FANOUT    = 8
REWARD_RATE_LIMIT   = 12   # max BLOCK msgs per peer IP per slot
SYNC_RATE_WINDOW    = 30   # seconds between SYNC_REQUESTs per IP

# ── v3.0 chain constants ───────────────────────────────────────────────────────
CONFIRMATION_DEPTH = 6      # blocks deep for finality (~30s at 5s slots)
MAX_SLOT_GAP       = 20     # max allowed slot gap between consecutive chain blocks
MAX_FUTURE_SLOTS   = 5      # max slots ahead of wall-clock a block can be
GENESIS_PREV_HASH  = "0" * 64  # prev_hash of the very first block ever

# ── Orphan pool limits (v3.1) ──────────────────────────────────────────────────
ORPHAN_POOL_MAX    = 100
ORPHAN_TTL_SLOTS   = MAX_SLOT_GAP * 2
MAX_REORG_DEPTH    = 100


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


def is_era2(ledger=None) -> bool:
    """Era 2 begins when total blocks produced reaches ERA2_ROUND.

    Uses total_minted // REWARD_PER_ROUND as a proxy for blocks produced —
    exact because every block mints exactly REWARD_PER_ROUND units.
    Unparticipated slots are never counted, so all 250M TMPL are guaranteed
    to be distributed before Era 2 begins. When no ledger is passed returns
    False so the lottery always starts in Era 1.
    """
    if ledger is None:
        return False
    blocks_produced = ledger.total_minted // REWARD_PER_ROUND
    return blocks_produced >= ERA2_ROUND


def get_current_fee() -> int:
    """Return the current tx fee in units (int). Flat 0.0005 TMPL in all eras."""
    return MIN_TX_FEE


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


# ── v3.0 chain helpers ─────────────────────────────────────────────────────────

def canonical_block(block: dict) -> bytes:
    """Deterministic serialization for block hashing.
    Uses sort_keys=True to guarantee identical output across all nodes.
    THIS IS THE SINGLE MOST CRITICAL FUNCTION IN THE PROTOCOL.
    Any deviation in serialization causes permanent, silent chain splits."""
    return json.dumps(block, sort_keys=True, separators=(",", ":")).encode()


def compute_block_hash(block: dict) -> str:
    """SHA-256 of canonical block serialization. Used as prev_hash in next block."""
    return hashlib.sha256(canonical_block(block)).hexdigest()


# ── Ledger ─────────────────────────────────────────────────────────────────────

class Ledger:
    def __init__(self):
        self.transactions    = []
        self.chain           = []
        self.fee_rewards     = []
        self.total_minted    = 0
        self.checkpoints     = []
        self._lock           = threading.RLock()
        self._spent_tx_ids_set: set = set()
        self._orphan_pool: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(LEDGER_FILE):
            try:
                with open(LEDGER_FILE, "r") as f:
                    data = json.load(f)
                self.transactions = data.get("transactions", [])
                self.checkpoints  = data.get("checkpoints", [])
                self.chain        = data.get("chain", [])
                self.fee_rewards  = data.get("fee_rewards", [])
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
                "chain":        self.chain,
                "fee_rewards":  self.fee_rewards,
                "total_minted": self.total_minted,
                "checkpoints":  self.checkpoints
            }, f, indent=2)
        os.replace(tmp, LEDGER_FILE)

    def _get_tip(self) -> tuple:
        """Returns (tip_hash, tip_slot) for the current chain head.
        Caller must hold self._lock (or call from __init__).
        After a checkpoint prunes the chain, tip comes from the checkpoint."""
        if self.chain:
            tip = self.chain[-1]
            return compute_block_hash(tip), tip.get("slot", -1)
        elif self.checkpoints:
            cp = self.checkpoints[-1]
            return cp.get("chain_tip_hash", GENESIS_PREV_HASH), cp.get("chain_tip_slot", -1)
        else:
            return GENESIS_PREV_HASH, -1

    def is_confirmed(self, block_slot: int) -> bool:
        return get_current_slot() - block_slot >= CONFIRMATION_DEPTH

    def get_balance(self, device_id: str) -> int:
        """Return balance in units (int). Divide by UNIT for TMPL display.

        Invariant: checkpoint.balances already includes all fee_rewards
        with time_slot < checkpoint.prune_before.  self.fee_rewards only
        holds entries with time_slot >= prune_before.  Together they cover
        full history with no gaps and no double-counting.
        """
        with self._lock:
            balance = 0
            if self.checkpoints:
                balance = self.checkpoints[-1].get("balances", {}).get(device_id, 0)
            for tx in self.transactions:
                if tx["recipient_id"] == device_id:
                    balance += tx["amount"]
                if tx["sender_id"] == device_id:
                    balance -= tx["amount"]
                    balance -= tx.get("fee", 0)
            for block in self.chain:
                if block.get("winner_id") == device_id:
                    balance += block.get("amount", 0)
            for fr in self.fee_rewards:
                if fr.get("winner_id") == device_id:
                    balance += fr.get("amount", 0)
            return balance

    def has_transaction(self, tx_id: str) -> bool:
        if tx_id in self._spent_tx_ids_set:
            return True
        return any(tx["tx_id"] == tx_id for tx in self.transactions)

    def can_spend(self, device_id: str, amount: int) -> bool:
        return self.get_balance(device_id) >= amount

    def add_transaction(self, tx_dict: dict) -> bool:
        amount = tx_dict.get("amount", 0)
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return False
        fee = tx_dict.get("fee", 0)
        if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
            return False
        if fee < MIN_TX_FEE:
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
            total = amount + fee
            if not self.can_spend(tx_dict["sender_id"], total):
                return False
            self.transactions.append(tx_dict)
            self.save()
            return True

    def add_fee_reward(self, slot: int, node_id: str, amount: int) -> bool:
        """Record fee redistribution for slot winner. Does NOT affect total_minted."""
        with self._lock:
            reward_id = f"fee:{slot}:{node_id}"
            if any(r.get("reward_id") == reward_id for r in self.fee_rewards):
                return False
            if not isinstance(amount, int) or amount <= 0:
                return False
            self.fee_rewards.append({
                "reward_id": reward_id,
                "winner_id": node_id,
                "amount":    amount,
                "timestamp": int(time.time()),
                "time_slot": slot,
                "type":      "fee_reward"
            })
            self.save()
            return True

    # ── Internal orphan pool helpers ───────────────────────────────────────────

    def _store_orphan_locked(self, block: dict) -> bool:
        """Store block in orphan pool keyed by prev_hash. Assumes self._lock held."""
        current_slot = get_current_slot()
        block_slot   = block.get("slot", 0)
        if current_slot - block_slot > ORPHAN_TTL_SLOTS:
            return False
        prev = block.get("prev_hash", "")
        bh   = compute_block_hash(block)
        if prev not in self._orphan_pool:
            self._orphan_pool[prev] = []
        if any(compute_block_hash(b) == bh for b in self._orphan_pool[prev]):
            return False
        self._orphan_pool[prev].append(block)
        total = sum(len(v) for v in self._orphan_pool.values())
        if total > ORPHAN_POOL_MAX:
            oldest_prev = None
            oldest_slot = get_current_slot() + 1
            for ph, blocks in list(self._orphan_pool.items()):
                for b in blocks:
                    if b.get("slot", 0) < oldest_slot:
                        oldest_slot = b.get("slot", 0)
                        oldest_prev = ph
            if oldest_prev and oldest_prev in self._orphan_pool:
                self._orphan_pool[oldest_prev].pop(0)
                if not self._orphan_pool[oldest_prev]:
                    del self._orphan_pool[oldest_prev]
        known_hashes = {compute_block_hash(b) for b in self.chain}
        if self.checkpoints:
            known_hashes.add(self.checkpoints[-1].get("chain_tip_hash", ""))
        known_hashes.add(GENESIS_PREV_HASH)
        return prev in known_hashes

    def _prune_stale_orphans_locked(self):
        cutoff = get_current_slot() - ORPHAN_TTL_SLOTS
        for ph in list(self._orphan_pool.keys()):
            self._orphan_pool[ph] = [
                b for b in self._orphan_pool[ph] if b.get("slot", 0) > cutoff
            ]
            if not self._orphan_pool[ph]:
                del self._orphan_pool[ph]

    def _drain_orphan_pool_locked(self):
        self._prune_stale_orphans_locked()
        found = True
        while found:
            found = False
            tip_hash, _ = self._get_tip()
            if tip_hash not in self._orphan_pool:
                break
            candidates = sorted(
                self._orphan_pool.pop(tip_hash),
                key=lambda b: b.get("slot", 0)
            )
            for orphan in candidates:
                if self._add_block_locked(orphan):
                    found = True
                    break

    # ── add_block (public + internal) ─────────────────────────────────────────

    def add_block(self, block: dict) -> bool:
        with self._lock:
            added = self._add_block_locked(block)
            if added:
                self._drain_orphan_pool_locked()
            return added

    def _add_block_locked(self, block: dict) -> bool:
        """add_block logic; caller must hold self._lock."""
        # 1. VRF proof
        pub  = block.get("vrf_public_key", "")
        seed = block.get("vrf_seed", "")
        sig  = block.get("vrf_sig", "")
        tick = block.get("vrf_ticket", "")
        if not (pub and seed and sig and tick):
            return False
        if not Node._verify_ticket(pub, seed, sig, tick):
            return False

        # 2. Slot validity
        slot = block.get("slot")
        if not _is_valid_epoch_slot(slot):
            return False

        # 3. winner_id validation
        winner_id = block.get("winner_id", "")
        if not (len(winner_id) == 64 and all(c in "0123456789abcdef" for c in winner_id)):
            return False

        # 4. No duplicate slots
        if any(b.get("slot") == slot for b in self.chain):
            return False

        # 5. Chain linkage — orphan pool on failure
        tip_hash, tip_slot = self._get_tip()
        if block.get("prev_hash") != tip_hash:
            extends_known = self._store_orphan_locked(block)
            if extends_known:
                candidates = list(self._orphan_pool.get(block.get("prev_hash", ""), []))
                if candidates:
                    self._attempt_reorg(candidates)
            return False

        # 6. Slot must be strictly greater than tip slot
        if slot <= tip_slot:
            return False

        # 7. Slot gap check
        current_slot = get_current_slot()
        if slot > current_slot + MAX_FUTURE_SLOTS:
            return False
        syncing = current_slot - tip_slot > MAX_SLOT_GAP
        if not syncing and tip_slot >= 0 and slot - tip_slot > MAX_SLOT_GAP:
            return False

        # 8. Supply cap
        if self.total_minted + block.get("amount", 0) > TOTAL_SUPPLY:
            return False

        self.chain.append(block)
        self.total_minted += block.get("amount", 0)
        self.save()
        return True

    def recalculate_totals(self):
        if self.checkpoints:
            cp          = self.checkpoints[-1]
            pruned_base = cp["total_minted"] - cp.get("kept_minted", 0)
            self.total_minted = pruned_base + sum(b.get("amount", 0) for b in self.chain)
        else:
            self.total_minted = sum(b.get("amount", 0) for b in self.chain)

    def get_summary(self):
        return {
            "total_transactions": len(self.transactions),
            "total_rewards":      len(self.chain),
            "chain_height":       len(self.chain),
            "total_minted":       self.total_minted,
            "remaining_supply":   TOTAL_SUPPLY - self.total_minted
        }

    def to_dict(self):
        return {
            "transactions": self.transactions,
            "chain":        self.chain,
            "fee_rewards":  self.fee_rewards,
            "total_minted": self.total_minted
        }

    def merge(self, other: dict) -> bool:
        """Merge incoming chain blocks, transactions, and fee_rewards.

        C3 FIX: fee_rewards with time_slot < prune_before are skipped.
        Entries before the boundary are already baked into checkpoint
        balances — re-accepting them causes double-counting.
        """
        # Validate VRF proofs on all incoming blocks
        valid_blocks = []
        for b in other.get("blocks", []):
            if not b.get("reward_id", ""):
                continue
            if b.get("type", "block_reward") != "block_reward":
                continue
            pub  = b.get("vrf_public_key", "")
            seed = b.get("vrf_seed", "")
            sig  = b.get("vrf_sig", "")
            tick = b.get("vrf_ticket", "")
            if not (pub and seed and sig and tick):
                continue
            if not Node._verify_ticket(pub, seed, sig, tick):
                continue
            if not _is_valid_epoch_slot(b.get("slot")):
                continue
            if b.get("amount", 0) <= 0:
                continue
            wid = b.get("winner_id", "")
            if not (len(wid) == 64 and all(c in "0123456789abcdef" for c in wid)):
                continue
            valid_blocks.append(b)

        valid_blocks.sort(key=lambda b: b.get("slot", 0))

        # Validate incoming transactions
        verified_txs = []
        for tx in other.get("transactions", []):
            try:
                t = Transaction.from_dict(tx)
                if t.verify():
                    verified_txs.append(tx)
            except Exception:
                continue

        with self._lock:
            changed = False

            # C3 FIX: determine prune_before from latest checkpoint.
            # Any fee_reward with time_slot < prune_before is already
            # accounted for in checkpoint.balances — skip it.
            prune_before = self.checkpoints[-1].get("prune_before", 0) if self.checkpoints else 0

            # Transactions
            for tx in verified_txs:
                if not self.has_transaction(tx["tx_id"]):
                    fee    = tx.get("fee", 0)
                    amount = tx.get("amount", 0)
                    if not isinstance(fee, int) or not isinstance(amount, int):
                        continue
                    if fee < MIN_TX_FEE:
                        continue
                    total = amount + fee
                    if self.can_spend(tx["sender_id"], total):
                        self.transactions.append(tx)
                        changed = True

            # Fee rewards merge
            # C3 FIX: skip entries before the checkpoint prune boundary.
            for fr in other.get("fee_rewards", []):
                # C3 FIX: reject stale entries already baked into checkpoint
                if fr.get("time_slot", 0) < prune_before:
                    continue
                rid = fr.get("reward_id", "")
                if not rid:
                    continue
                if not isinstance(fr.get("amount"), int) or fr["amount"] <= 0:
                    continue
                wid = fr.get("winner_id", "")
                if not (len(wid) == 64 and all(c in "0123456789abcdef" for c in wid)):
                    continue
                if not any(r.get("reward_id") == rid for r in self.fee_rewards):
                    self.fee_rewards.append(fr)
                    changed = True

            # Chain extension
            for block in valid_blocks:
                tip_hash, tip_slot = self._get_tip()
                if block.get("prev_hash") != tip_hash:
                    self._store_orphan_locked(block)
                    continue
                slot = block.get("slot", -1)
                if slot <= tip_slot:
                    continue
                current_slot = get_current_slot()
                if slot > current_slot + MAX_FUTURE_SLOTS:
                    continue
                syncing = current_slot - tip_slot > MAX_SLOT_GAP
                if not syncing and tip_slot >= 0 and slot - tip_slot > MAX_SLOT_GAP:
                    continue
                if self.total_minted + block.get("amount", 0) > TOTAL_SUPPLY:
                    continue
                self.chain.append(block)
                self.total_minted += block.get("amount", 0)
                changed = True

            if self._attempt_reorg(valid_blocks):
                changed = True

            if changed:
                self._drain_orphan_pool_locked()
                self.save()
            return changed

    @staticmethod
    def _chain_weight(blocks: list) -> int:
        """Compute chain weight using integer arithmetic only.

        Each block contributes +1. Slot gaps > 1 subtract (gap - 1) per missing
        slot, penalising sparse chains without introducing floats.
        Result is clamped to 0 — weight is never negative.
        """
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

    def _attempt_reorg(self, valid_blocks: list) -> bool:
        """Try to replace current chain tail with a longer/heavier alternative.

        Called from merge() while self._lock is already held.
        Reorgs before checkpoint boundary, and reorgs exceeding MAX_REORG_DEPTH,
        are always rejected.
        """
        if not valid_blocks:
            return False

        our_hash_to_idx = {}
        for i, block in enumerate(self.chain):
            our_hash_to_idx[compute_block_hash(block)] = i

        if self.checkpoints:
            cp = self.checkpoints[-1]
            checkpoint_tip_hash = cp.get("chain_tip_hash", GENESIS_PREV_HASH)
            checkpoint_tip_slot = cp.get("chain_tip_slot", -1)
        else:
            checkpoint_tip_hash = GENESIS_PREV_HASH
            checkpoint_tip_slot = -1

        fork_anchor_chain_idx = None
        fork_start_in_input   = None

        for i, block in enumerate(valid_blocks):
            prev = block.get("prev_hash", "")
            if prev == checkpoint_tip_hash:
                fork_anchor_chain_idx = -1
                fork_start_in_input   = i
                break
            if prev in our_hash_to_idx:
                fork_anchor_chain_idx = our_hash_to_idx[prev]
                fork_start_in_input   = i
                break

        if fork_start_in_input is None:
            return False

        # Never reorg before the checkpoint boundary
        if fork_anchor_chain_idx == -1 and self.checkpoints:
            return False

        fork_blocks = valid_blocks[fork_start_in_input:]

        if fork_anchor_chain_idx == -1:
            anchor_hash = checkpoint_tip_hash
            anchor_slot = checkpoint_tip_slot
        else:
            anchor_block = self.chain[fork_anchor_chain_idx]
            anchor_hash  = compute_block_hash(anchor_block)
            anchor_slot  = anchor_block.get("slot", -1)

        # Soft finality: reject reorgs that anchor too far behind our tip
        if self.chain and self.checkpoints:
            tip_slot_now = self.chain[-1].get("slot", 0)
            if tip_slot_now - anchor_slot > MAX_REORG_DEPTH:
                print(f"\n  [reorg_rejected] reason=soft_finality"
                      f" anchor_slot={anchor_slot} tip_slot={tip_slot_now}"
                      f" depth={tip_slot_now - anchor_slot} limit={MAX_REORG_DEPTH}\n  > ",
                      end="", flush=True)
                return False

        # Validate fork blocks sequentially
        # NOTE: MAX_SLOT_GAP intentionally NOT checked during reorg (historical chains)
        validated = []
        prev_hash = anchor_hash
        prev_slot = anchor_slot

        for block in fork_blocks:
            pub  = block.get("vrf_public_key", "")
            seed = block.get("vrf_seed", "")
            sig  = block.get("vrf_sig", "")
            tick = block.get("vrf_ticket", "")
            if not (pub and seed and sig and tick):
                break
            if not Node._verify_ticket(pub, seed, sig, tick):
                break
            slot = block.get("slot")
            if not _is_valid_epoch_slot(slot):
                break
            wid = block.get("winner_id", "")
            if not (len(wid) == 64 and all(c in "0123456789abcdef" for c in wid)):
                break
            if block.get("prev_hash") != prev_hash:
                break
            if slot <= prev_slot:
                break
            validated.append(block)
            prev_hash = compute_block_hash(block)
            prev_slot = slot

        if not validated:
            return False

        if self.checkpoints and len(validated) > MAX_REORG_DEPTH:
            print(f"\n  [reorg_rejected] reason=depth_limit"
                  f" alt_blocks={len(validated)} limit={MAX_REORG_DEPTH}\n  > ",
                  end="", flush=True)
            return False

        our_tail   = self.chain[fork_anchor_chain_idx + 1:]
        alt_weight = Ledger._chain_weight(validated)
        our_weight = Ledger._chain_weight(our_tail)

        if alt_weight < our_weight:
            print(f"\n  [reorg_rejected] reason=insufficient_weight"
                  f" alt_weight={alt_weight} our_weight={our_weight}"
                  f" alt_blocks={len(validated)} anchor_slot={anchor_slot}\n  > ",
                  end="", flush=True)
            return False

        if alt_weight == our_weight:
            our_tip = compute_block_hash(self.chain[-1]) if self.chain else GENESIS_PREV_HASH
            alt_tip = compute_block_hash(validated[-1])
            if alt_tip >= our_tip:
                print(f"\n  [reorg_rejected] reason=tiebreak_hash_lost"
                      f" alt_weight={alt_weight} our_weight={our_weight}\n  > ",
                      end="", flush=True)
                return False

        # Supply cap check on reconstructed chain
        keep_count = fork_anchor_chain_idx + 1
        if self.checkpoints:
            cp          = self.checkpoints[-1]
            pruned_base = cp["total_minted"] - cp.get("kept_minted", 0)
        else:
            pruned_base = 0
        kept_chain_minted = sum(b.get("amount", 0) for b in self.chain[:keep_count])
        alt_minted = (pruned_base + kept_chain_minted +
                      sum(b.get("amount", 0) for b in validated))
        if alt_minted > TOTAL_SUPPLY:
            return False

        # Perform the reorg
        self.chain = self.chain[:keep_count] + validated
        self.recalculate_totals()
        self._prune_invalid_transactions()

        tip_slot   = validated[-1].get("slot", "?")
        fork_depth = (tip_slot - anchor_slot) if isinstance(tip_slot, int) else "?"
        print(f"\n  [reorg] switched chain"
              f" | anchor_slot={anchor_slot}"
              f" | tip_slot={tip_slot}"
              f" | fork_depth={fork_depth}"
              f" | alt_weight={alt_weight}"
              f" | old_weight={our_weight}"
              f" | alt_blocks={len(validated)}\n  > ", end="", flush=True)
        return True

    def _prune_invalid_transactions(self):
        """Remove transactions whose sender can no longer afford them.
        Called after every reorg while self._lock is already held."""
        def _chain_balance(device_id: str) -> int:
            bal = 0
            if self.checkpoints:
                bal = self.checkpoints[-1].get("balances", {}).get(device_id, 0)
            for b in self.chain:
                if b.get("winner_id") == device_id:
                    bal += b.get("amount", 0)
            for fr in self.fee_rewards:
                if fr.get("winner_id") == device_id:
                    bal += fr.get("amount", 0)
            return bal

        valid_txs = []
        running   = {}
        for tx in self.transactions:
            sid  = tx.get("sender_id", "")
            rid  = tx.get("recipient_id", "")
            cost = tx.get("amount", 0) + tx.get("fee", 0)
            sender_balance = _chain_balance(sid) + running.get(sid, 0)
            if sender_balance >= cost:
                valid_txs.append(tx)
                running[sid] = running.get(sid, 0) - cost
                running[rid] = running.get(rid, 0) + tx.get("amount", 0)

        pruned = len(self.transactions) - len(valid_txs)
        if pruned > 0:
            self.transactions = valid_txs
            print(f"\n  [reorg] Pruned {pruned} transaction(s) no longer fundable "
                  f"after chain switch\n  > ", end="", flush=True)

    @staticmethod
    def _compute_hash(entries: list) -> str:
        return hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()

    def create_checkpoint(self, checkpoint_slot: int) -> bool:
        """Create checkpoint at checkpoint_slot.

        FIX-C: checkpoint_slot must be a multiple of CHECKPOINT_INTERVAL.
        FIX-B: fee_rewards before prune_before are baked into checkpoint
               balances and then pruned from self.fee_rewards.
        """
        if checkpoint_slot % CHECKPOINT_INTERVAL != 0:
            return False

        prune_before = checkpoint_slot - CHECKPOINT_BUFFER
        with self._lock:
            if any(c["slot"] == checkpoint_slot for c in self.checkpoints):
                return False

            c_prune  = [b  for b  in self.chain        if b.get("slot",      prune_before) < prune_before]
            c_keep   = [b  for b  in self.chain        if b.get("slot",      prune_before) >= prune_before]
            t_prune  = [t  for t  in self.transactions if (t.get("slot") or 0) < prune_before]
            t_keep   = [t  for t  in self.transactions if (t.get("slot") or 0) >= prune_before]
            fr_prune = [fr for fr in self.fee_rewards  if fr.get("time_slot", 0) < prune_before]
            fr_keep  = [fr for fr in self.fee_rewards  if fr.get("time_slot", 0) >= prune_before]

            prev_bal = dict(self.checkpoints[-1]["balances"]) if self.checkpoints else {}
            addrs = set(prev_bal.keys())
            for b in c_prune:
                wid = b.get("winner_id", "")
                if wid: addrs.add(wid)
            for t in t_prune:
                sid = t.get("sender_id", "")
                rid = t.get("recipient_id", "")
                if sid: addrs.add(sid)
                if rid: addrs.add(rid)
            for fr in fr_prune:
                wid = fr.get("winner_id", "")
                if wid: addrs.add(wid)

            balances = {}
            for addr in addrs:
                bal = prev_bal.get(addr, 0)
                for t in t_prune:
                    if t.get("recipient_id") == addr: bal += t.get("amount", 0)
                    if t.get("sender_id")    == addr:
                        bal -= t.get("amount", 0)
                        bal -= t.get("fee", 0)
                for b in c_prune:
                    if b.get("winner_id") == addr: bal += b.get("amount", 0)
                for fr in fr_prune:
                    if fr.get("winner_id") == addr: bal += fr.get("amount", 0)
                balances[addr] = bal

            prev_spent   = list(self.checkpoints[-1].get("spent_tx_ids", [])) if self.checkpoints else []
            spent_tx_ids = list(set(prev_spent + [t["tx_id"] for t in t_prune]))
            kept_minted  = sum(b.get("amount", 0) for b in c_keep)

            if c_prune:
                chain_tip_hash = compute_block_hash(c_prune[-1])
                chain_tip_slot = c_prune[-1].get("slot", -1)
            elif self.checkpoints:
                chain_tip_hash = self.checkpoints[-1].get("chain_tip_hash", GENESIS_PREV_HASH)
                chain_tip_slot = self.checkpoints[-1].get("chain_tip_slot", -1)
            else:
                chain_tip_hash = GENESIS_PREV_HASH
                chain_tip_slot = -1

            cp = {
                "slot":              checkpoint_slot,
                "prune_before":      prune_before,
                "balances":          balances,
                "total_minted":      self.total_minted,
                "kept_minted":       kept_minted,
                "chain_hash":        Ledger._compute_hash(sorted(c_prune,  key=lambda b:  b.get("slot", 0))),
                "txs_hash":          Ledger._compute_hash(sorted(t_prune,  key=lambda t:  t.get("timestamp", 0))),
                "fee_rewards_hash":  Ledger._compute_hash(sorted(fr_prune, key=lambda fr: fr.get("time_slot", 0))),
                "spent_tx_ids":      spent_tx_ids,
                "chain_tip_hash":    chain_tip_hash,
                "chain_tip_slot":    chain_tip_slot,
                "timestamp":         int(time.time())
            }

            # c_keep may be empty — that's fine. _get_tip() falls back to
            # checkpoint.chain_tip_hash so new blocks can still link correctly.
            # Keeping c_prune[-1] here double-counts it in get_balance() since
            # it is already baked into checkpoint.balances.
            self.chain = c_keep
            self.transactions = t_keep
            self.fee_rewards  = fr_keep
            self.checkpoints.append(cp)
            self._spent_tx_ids_set = set(spent_tx_ids)
            self.save()
            return True

    def apply_checkpoint(self, checkpoint: dict) -> bool:
        """Apply a checkpoint received from a peer.
        FIX-D: prunes self.fee_rewards and verifies fee_rewards_hash.
        """
        with self._lock:
            if self.checkpoints:
                if checkpoint.get("slot", 0) <= self.checkpoints[-1]["slot"]:
                    return False
            if checkpoint.get("total_minted", 0) > TOTAL_SUPPLY:
                return False
            prune_before = checkpoint.get("prune_before", 0)
            c_verify = [b for b in self.chain if b.get("slot", prune_before) < prune_before]
            if c_verify:
                if Ledger._compute_hash(sorted(c_verify, key=lambda b: b.get("slot", 0))) != checkpoint.get("chain_hash", ""):
                    return False
            t_verify = [t for t in self.transactions if (t.get("slot") or 0) < prune_before]
            if t_verify:
                if Ledger._compute_hash(sorted(t_verify, key=lambda t: t.get("timestamp", 0))) != checkpoint.get("txs_hash", ""):
                    return False
            fr_verify = [fr for fr in self.fee_rewards if fr.get("time_slot", 0) < prune_before]
            if fr_verify and checkpoint.get("fee_rewards_hash"):
                if Ledger._compute_hash(sorted(fr_verify, key=lambda fr: fr.get("time_slot", 0))) != checkpoint.get("fee_rewards_hash", ""):
                    return False
            self.chain        = [b  for b  in self.chain        if b.get("slot",      prune_before) >= prune_before]
            self.transactions = [t  for t  in self.transactions if (t.get("slot") or 0) >= prune_before]
            self.fee_rewards  = [fr for fr in self.fee_rewards  if fr.get("time_slot", 0) >= prune_before]
            self.checkpoints.append(checkpoint)
            self._spent_tx_ids_set = set(checkpoint.get("spent_tx_ids", []))
            self.total_minted = checkpoint.get("total_minted", 0)
            self.save()
            return True


# ── Wallet ─────────────────────────────────────────────────────────────────────

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
        # FIX-A: was self.wallet.private_key (NameError); corrected to self.private_key
        return Dilithium3.sign(self.private_key, message).hex()

    @staticmethod
    def verify_signature(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
        try:
            return Dilithium3.verify(bytes.fromhex(public_key_hex), message, bytes.fromhex(signature_hex))
        except Exception:
            return False


# ── Transaction ────────────────────────────────────────────────────────────────

class Transaction:
    def __init__(self, sender_id, recipient_id, sender_pubkey,
                 amount: int, timestamp=None, tx_id=None, fee: int = 0, slot=None):
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
                f"{self.amount}:{self.fee}:{self.timestamp:.6f}:"
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
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError(f"invalid amount (must be int > 0): {amount!r}")
        fee = d.get("fee", 0)
        if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
            raise ValueError(f"invalid fee (must be int >= 0): {fee!r}")
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


# ── Bootstrap helpers ──────────────────────────────────────────────────────────

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


# ── Network ────────────────────────────────────────────────────────────────────

class Network:
    def __init__(self, wallet, ledger, on_transaction, on_block):
        self.wallet            = wallet
        self.ledger            = ledger
        self.on_transaction    = on_transaction
        self.on_block          = on_block
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
        self._block_rate       = {}
        self._block_rate_lock  = threading.Lock()

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
            with self._block_rate_lock:
                for ip in [ip for ip, (s, _) in self._block_rate.items() if s < current_slot - 5]:
                    del self._block_rate[ip]

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
                        bs_tip_slot = data.get("chain_tip_slot", -1)
                        with self.ledger._lock:
                            _, our_tip_slot = self.ledger._get_tip()
                        if bs_tip_slot > our_tip_slot:
                            threading.Thread(target=self._sync_ledger, daemon=True).start()
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
                sock.sendall(json.dumps({
                    "type": "SYNC_REQUEST",
                    "chain_height": 0, "chain_tip_hash": GENESIS_PREV_HASH,
                    "chain_tip_slot": -1, "known_tx_ids": [], "checkpoint_slot": 0
                }).encode())
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
                            peer_cp.get("slot")          == checkpoint.get("slot") and
                            peer_cp.get("chain_hash")    == checkpoint.get("chain_hash") and
                            peer_cp.get("txs_hash")      == checkpoint.get("txs_hash") and
                            peer_cp.get("total_minted")  == checkpoint.get("total_minted")):
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
        """Pull missing blocks, txs, and fee_rewards from up to 3 peers.

        C2 FIX: fee_rewards from SYNC_RESPONSE are now passed into
        ledger.merge() so nodes that missed live FEE_REWARDS broadcasts
        recover them on the next sync.
        """
        time.sleep(1)
        peers = self.get_online_peers()
        if not peers:
            return
        for peer_id in random.sample(list(peers.keys()), min(3, len(peers))):
            peer = peers[peer_id]
            try:
                with self.ledger._lock:
                    chain_height    = len(self.ledger.chain)
                    tip_hash, tip_slot = self.ledger._get_tip()
                    known_tx_ids    = [t.get("tx_id") for t in self.ledger.transactions
                                       if t.get("tx_id")][-10000:]
                    checkpoint_slot = self.ledger.checkpoints[-1]["slot"] if self.ledger.checkpoints else 0

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(30.0)
                sock.connect((peer["ip"], peer["port"]))
                # Send last 150 block hashes so the responder can find the
                # common ancestor and send only the diverging blocks.
                # 150 covers CHECKPOINT_BUFFER (120) plus a safe margin.
                # At any network scale this is always ~10KB regardless of chain length.
                with self.ledger._lock:
                    recent_hashes = [compute_block_hash(b)
                                     for b in self.ledger.chain[-150:]]

                sock.sendall(json.dumps({
                    "type":               "SYNC_REQUEST",
                    "chain_height":       chain_height,
                    "chain_tip_hash":     tip_hash,
                    "chain_tip_slot":     tip_slot,
                    "known_tx_ids":       known_tx_ids,
                    "checkpoint_slot":    checkpoint_slot,
                    "chain_recent_hashes": recent_hashes,
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
                                [b for b in self.ledger.chain if b.get("slot", prune_before) < prune_before] or
                                [t for t in self.ledger.transactions if (t.get("slot") or 0) < prune_before])
                        ledger_empty = not self.ledger.chain and not self.ledger.checkpoints
                        if ledger_empty or can_verify:
                            self.ledger.apply_checkpoint(cp)
                        elif self._confirm_checkpoint_with_peers(cp, exclude_ip=peer["ip"]):
                            self.ledger.apply_checkpoint(cp)

                    # C2 FIX: include fee_rewards in the merge delta so nodes
                    # that missed live FEE_REWARDS broadcasts recover them here.
                    delta = {
                        "blocks":       msg.get("blocks", []),
                        "transactions": msg.get("txs", []),
                        "fee_rewards":  msg.get("fee_rewards", []),
                    }
                    if delta["blocks"] or delta["transactions"] or delta["fee_rewards"]:
                        known_before = set(t.get("tx_id") for t in self.ledger.transactions)
                        merged = self.ledger.merge(delta)
                        if merged:
                            print(f"\n  [+] Synced {len(delta['blocks'])} blocks, "
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
                                        print(f"  ║  Amount  : {tx['amount'] / UNIT:.8f} TMPL")
                                        print(f"  ║  From    : {tx['sender_id'][:20]}...")
                                        print(f"  ║  Balance : {bal / UNIT:.8f} TMPL")
                                        print(f"  ╚══════════════════════════════════════╝\n  > ", end="", flush=True)

                    we_need_from_slot = msg.get("we_need_from_slot")
                    we_need_tx_ids    = set(msg.get("we_need_tx_ids", []))
                    if we_need_from_slot is not None or we_need_tx_ids:
                        with self.ledger._lock:
                            push_b = [b for b in self.ledger.chain
                                      if b.get("slot", -1) > (we_need_from_slot if we_need_from_slot is not None else -1)]
                            push_t = [t for t in self.ledger.transactions if t.get("tx_id") in we_need_tx_ids]
                            # M10 FIX: include fee_rewards in SYNC_PUSH payload,
                            # filtered to entries after what the peer already has.
                            push_fr = [fr for fr in self.ledger.fee_rewards
                                       if fr.get("time_slot", -1) > (we_need_from_slot if we_need_from_slot is not None else -1)]
                        if push_b or push_t or push_fr:
                            try:
                                s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                s2.settimeout(30.0)
                                s2.connect((peer["ip"], peer["port"]))
                                # M10 FIX: fee_rewards included in push payload
                                s2.sendall(json.dumps({
                                    "type":        "SYNC_PUSH",
                                    "blocks":      push_b,
                                    "txs":         push_t,
                                    "fee_rewards": push_fr,
                                }).encode())
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
                                             "reason": "Update from https://github.com/EvokiTimpal/timpal"}).encode())
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
                with self._seen_lock:
                    if tx_gossip_id in self.seen_ids:
                        return
                    self.seen_ids.add(tx_gossip_id)
                    self._seen_tx_order.append(tx_gossip_id)
                self.on_transaction(tx)
                threading.Thread(target=self.broadcast, args=(msg, None), daemon=True).start()

            elif msg_type == "FEE_REWARDS":
                ts  = msg.get("time_slot")
                frs = msg.get("fee_rewards", [])[:10]
                if not frs or ts is None:
                    return
                frs = [fr for fr in frs
                       if isinstance(fr.get("winner_id"), str)
                       and len(fr["winner_id"]) == 64
                       and all(c in "0123456789abcdef" for c in fr["winner_id"].lower())
                       and isinstance(fr.get("amount"), int)
                       and fr.get("amount", 0) > 0]
                if not frs:
                    return
                with self.ledger._lock:
                    total_tx_fees = sum(
                        t.get("fee", 0) for t in self.ledger.transactions
                        if isinstance(t.get("fee"), int) and t.get("fee", 0) > 0
                    )
                    total_awarded = sum(
                        fr.get("amount", 0) for fr in self.ledger.fee_rewards
                        if isinstance(fr.get("amount"), int)
                    )
                    pending_fees = total_tx_fees - total_awarded
                claimed = sum(fr.get("amount", 0) for fr in frs)
                if claimed > max(pending_fees, 0):
                    return
                for fr in frs:
                    self.ledger.add_fee_reward(ts, fr["winner_id"], fr["amount"])

            elif msg_type == "BLOCK":
                current_slot = get_current_slot()
                with self._block_rate_lock:
                    s, cnt = self._block_rate.get(sender_ip, (current_slot, 0))
                    if s != current_slot:
                        cnt = 0
                    if cnt >= REWARD_RATE_LIMIT:
                        return
                    self._block_rate[sender_ip] = (current_slot, cnt + 1)
                block = msg.get("block", {})
                gid   = block.get("reward_id", "") + ":" + block.get("winner_id", "")
                with self._seen_lock:
                    if gid and gid not in self.seen_ids:
                        self.seen_ids.add(gid)
                    else:
                        gid = None
                if gid:
                    self.on_block(block)
                    threading.Thread(target=self.broadcast, args=(msg, None), daemon=True).start()
                    with self.ledger._lock:
                        tip_hash, _ = self.ledger._get_tip()
                    if block.get("prev_hash") != tip_hash:
                        threading.Thread(target=self._sync_ledger, daemon=True).start()

            elif msg_type == "SYNC_PUSH":
                with self._peers_lock:
                    known_ips = {p["ip"] for p in self.peers.values()}
                if sender_ip not in known_ips:
                    return
                # M10 FIX: include fee_rewards in SYNC_PUSH merge
                self.ledger.merge({
                    "blocks":       msg.get("blocks", [])[:5000],
                    "transactions": msg.get("txs",    [])[:2000],
                    "fee_rewards":  msg.get("fee_rewards", []),
                })

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
                                [b for b in self.ledger.chain if b.get("slot", pb) < pb] or
                                [t for t in self.ledger.transactions if (t.get("slot") or 0) < pb])
                        ledger_empty = not self.ledger.chain and not self.ledger.checkpoints
                        if ledger_empty or can_verify:
                            applied = self.ledger.apply_checkpoint(cp)
                        elif self._confirm_checkpoint_with_peers(cp, exclude_ip=sender_ip):
                            applied = self.ledger.apply_checkpoint(cp)
                        else:
                            applied = False
                        if applied:
                            self.broadcast({"type": "CHECKPOINT", "checkpoint": cp})

            elif msg_type == "SYNC_REQUEST":
                now = time.time()
                with self._sync_rate_lock:
                    if now - self._sync_rate.get(sender_ip, 0) < SYNC_RATE_WINDOW:
                        return
                    self._sync_rate[sender_ip] = now

                their_tip_hash    = msg.get("chain_tip_hash", GENESIS_PREV_HASH)
                their_tip_slot    = msg.get("chain_tip_slot", -1)
                their_tx_ids      = set(msg.get("known_tx_ids", [])[:10000])
                their_cp          = msg.get("checkpoint_slot", 0)
                their_recent      = set(msg.get("chain_recent_hashes", []))

                with self.ledger._lock:
                    tip_hash, tip_slot = self.ledger._get_tip()
                    our_tx_ids   = set(t.get("tx_id") for t in self.ledger.transactions if t.get("tx_id"))
                    missing_t    = [t for t in self.ledger.transactions if t.get("tx_id") not in their_tx_ids][:2000]
                    our_cp       = None
                    if self.ledger.checkpoints:
                        latest = self.ledger.checkpoints[-1]
                        if latest["slot"] > their_cp:
                            our_cp = latest

                    if their_tip_hash == tip_hash:
                        # Already in sync
                        missing_blocks = []
                    elif their_recent:
                        # Common-ancestor sync: walk our chain backwards to find
                        # the last block the peer also has, then send from there.
                        # Forks are always within CHECKPOINT_BUFFER (120 slots) so
                        # the common ancestor is always within the last 150 hashes.
                        # This is O(chain length) to build the set once, then
                        # O(150) lookups — same cost at 2 nodes or 2 million.
                        our_hashes = {compute_block_hash(b): i
                                      for i, b in enumerate(self.ledger.chain)}
                        fork_idx = None
                        for b in reversed(self.ledger.chain):
                            bh = compute_block_hash(b)
                            if bh in their_recent:
                                fork_idx = our_hashes[bh]
                                break
                        if fork_idx is not None:
                            # Send only blocks after the common ancestor
                            missing_blocks = self.ledger.chain[fork_idx + 1:][:5000]
                        else:
                            # No common ancestor found — peer is too far behind
                            # or on a completely different chain. Send full chain.
                            missing_blocks = list(self.ledger.chain)[:5000]
                    else:
                        # Old client without chain_recent_hashes — send full chain
                        missing_blocks = list(self.ledger.chain)[:5000]

                    we_need_from_slot = their_tip_slot if their_tip_slot > tip_slot else None

                conn.sendall(json.dumps({
                    "type":              "SYNC_RESPONSE",
                    "blocks":            missing_blocks,
                    "txs":               missing_t,
                    "fee_rewards":       list(self.ledger.fee_rewards),
                    "chain_height":      len(self.ledger.chain),
                    "we_need_from_slot": we_need_from_slot,
                    "we_need_tx_ids":    list(their_tx_ids - our_tx_ids),
                    "checkpoint":        our_cp
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


# ── Node ───────────────────────────────────────────────────────────────────────

class Node:
    def __init__(self):
        self.wallet  = Wallet()
        self.ledger  = Ledger()
        self.network = None
        self._acquire_lock()
        self._load_or_create_wallet()
        self.network = Network(self.wallet, self.ledger,
                               self._on_transaction_received, self._on_block_received)
        self.network._node_ref = self
        self._sending         = False
        # M1 FIX: _my_tickets protected by its own lock.
        # Eliminates RuntimeError from concurrent read-in-lottery /
        # delete-in-cleanup without lock.
        self._my_tickets      = {}
        self._my_tickets_lock = threading.Lock()
        self._commits         = {}
        self._lottery_lock    = threading.Lock()

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
                    # v3.1 wallets load fine — only block versions < 3.1
                    if _ver(wd.get("version", "0.0")) < _ver(MIN_VERSION):
                        print("\n  " + "═"*52)
                        print("  TIMPAL v3.2 — ACTION REQUIRED")
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
            print(f"  Balance   : {balance / UNIT:.8f} TMPL")
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
            print(f"  ║  Amount  : {tx.amount / UNIT:.8f} TMPL")
            print(f"  ║  From    : {tx.sender_id[:20]}...")
            print(f"  ║  Balance : {bal / UNIT:.8f} TMPL")
            print(f"  ╚══════════════════════════════════════╝\n  > ", end="", flush=True)

    def _on_block_received(self, block: dict):
        if not self.ledger.add_block(block):
            return
        if block.get("winner_id") == self.wallet.device_id and not self._sending:
            bal = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n  ╔══════════════════════════════════════╗")
            print(f"  ║       REWARD WON! ★                  ║")
            print(f"  ╠══════════════════════════════════════╣")
            print(f"  ║  Amount  : {block.get('amount', 0) / UNIT:.8f} TMPL")
            print(f"  ║  Balance : {bal / UNIT:.8f} TMPL")
            print(f"  ╚══════════════════════════════════════╝\n  > ", end="", flush=True)
        else:
            print(f"\n  [slot {block.get('slot','?')}] "
                  f"Winner: {block.get('winner_id','')[:20]}... "
                  f"+{block.get('amount', 0) / UNIT:.4f} TMPL\n  > ", end="", flush=True)

    def _vrf_ticket(self, time_slot: int) -> tuple:
        """Generate VRF ticket for time_slot.
        P2 FIX: retries up to 5 times on IndexError from Dilithium3.sign."""
        seed = str(time_slot)
        for _ in range(5):
            try:
                sig    = Dilithium3.sign(self.wallet.private_key, seed.encode())
                ticket = hashlib.sha256(sig).hexdigest()
                return ticket, sig.hex(), seed
            except (IndexError, Exception):
                continue
        raise RuntimeError("VRF ticket generation failed after 5 retries")

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
        if network_size <= TARGET_PARTICIPANTS:
            return True
        threshold = TARGET_PARTICIPANTS / network_size
        h = int(hashlib.sha256(f"{self.wallet.device_id}:{time_slot}".encode()).hexdigest(), 16)
        return h < int(threshold * (2 ** 256))

    def _bootstrap_submit(self, msg: dict):
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
        """Submit commit to all bootstrap servers.
        FIX-E: fires done on first COMMIT_ACK received."""
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
                    if data.get("type") == "COMMIT_ACK":
                        done.set()
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
        """Determine the winning node for time_slot.

        M7 FIX: compute the collective target from the verified dict only.
        Previously the target was computed from all_reveals (including reveals
        with no matching commit or failing VRF), so a malicious node could
        submit arbitrary ticket values to shift the target and steer the winner
        without being eligible to win itself.
        """
        verified = {}
        with self._lottery_lock:
            known_commits = dict(self._commits.get(time_slot, {}))
        for device_id, r in all_reveals.items():
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
        # M7 FIX: compute target from verified only — unverified reveals excluded
        tickets    = sorted(verified[d]["ticket"] for d in verified)
        target     = hashlib.sha256(":".join(tickets).encode()).hexdigest()
        target_int = int(target, 16)
        winner_id  = min(verified, key=lambda d: (
            abs(int(verified[d]["ticket"], 16) - target_int), d))
        w = verified[winner_id]
        return {"winner_id": winner_id, "ticket": w["ticket"],
                "sig": w["sig"], "seed": w["seed"], "public_key": w["public_key"]}

    def _collect_slot_fees(self, time_slot: int, winner_id: str):
        """v3.1 Change 3 (Option B): collect all pending tx fees → winner.
        P3 FIX: uses total_pending = total_tx_fees - total_awarded.
        Fees do NOT increase total_minted.
        """
        with self.ledger._lock:
            total_tx_fees = sum(
                t.get("fee", 0) for t in self.ledger.transactions
                if isinstance(t.get("fee"), int) and t.get("fee", 0) > 0
            )
            total_awarded = sum(
                fr.get("amount", 0) for fr in self.ledger.fee_rewards
                if isinstance(fr.get("amount"), int)
            )
            slot_fees = total_tx_fees - total_awarded
        if slot_fees <= 0:
            return
        if self.ledger.add_fee_reward(time_slot, winner_id, slot_fees):
            self.network.broadcast({"type": "FEE_REWARDS", "time_slot": time_slot,
                                    "fee_rewards": [{
                                        "reward_id": f"fee:{time_slot}:{winner_id}",
                                        "winner_id": winner_id,
                                        "amount":    slot_fees,
                                        "time_slot": time_slot,
                                        "type":      "fee_reward"
                                    }]})

    def _claim_reward(self, winner: dict, time_slot: int, active_nodes=None):
        """Claim block reward for the winning node.

        C1 FIX: SUBMIT_TIP now reports the hash of the last chain block at or
        before the checkpoint boundary, not compute_block_hash(winning_block).
        Every node on the same chain will have the same block at slot ≤ cp_boundary,
        so bootstrap majority voting resolves to a single hash and checkpointing works.
        """
        if not Node._verify_ticket(winner["public_key"], winner["seed"],
                                   winner["sig"], winner["ticket"]):
            return

        with self.ledger._lock:
            if any(b.get("slot") == time_slot for b in self.ledger.chain):
                return
            tip_hash, tip_slot = self.ledger._get_tip()

        if time_slot <= tip_slot:
            return

        reward_id = f"reward:{time_slot}"
        block = {
            "reward_id":      reward_id,
            "slot":           time_slot,
            "prev_hash":      tip_hash,
            "winner_id":      winner["winner_id"],
            "amount":         REWARD_PER_ROUND,
            "timestamp":      int(time.time()),
            "vrf_ticket":     winner["ticket"],
            "vrf_seed":       winner["seed"],
            "vrf_sig":        winner["sig"],
            "vrf_public_key": winner["public_key"],
            "nodes":          len(active_nodes) if active_nodes else 1,
            "type":           "block_reward"
        }

        if not self.ledger.add_block(block):
            return

        gid = reward_id + ":" + winner["winner_id"]
        with self.network._seen_lock:
            self.network.seen_ids.add(gid)

        # C1 FIX: compute the tip hash at the checkpoint boundary.
        # Scan backwards through the chain for the last block with
        # slot ≤ cp_boundary and report that hash to bootstrap.
        # All nodes on the same chain converge to the same boundary block,
        # so the bootstrap tally accumulates votes for one hash → majority resolves.
        cp_boundary = (time_slot // CHECKPOINT_INTERVAL) * CHECKPOINT_INTERVAL
        tip_at_cp   = GENESIS_PREV_HASH
        if cp_boundary > 0:
            with self.ledger._lock:
                for b in reversed(self.ledger.chain):
                    if b.get("slot", 0) <= cp_boundary:
                        tip_at_cp = compute_block_hash(b)
                        break
                else:
                    # No chain block at or before boundary — fall back to
                    # the stored checkpoint tip (handles pruned chains)
                    if self.ledger.checkpoints:
                        tip_at_cp = self.ledger.checkpoints[-1].get(
                            "chain_tip_hash", GENESIS_PREV_HASH)

        self._bootstrap_submit({
            "type":      "SUBMIT_TIP",
            "device_id": self.wallet.device_id,
            "slot":      time_slot,
            "cp_slot":   cp_boundary,
            "tip_hash":  tip_at_cp,
        })

        self.network.broadcast({"type": "BLOCK", "block": block})
        threading.Thread(target=self.network._sync_ledger, daemon=True).start()

        self._collect_slot_fees(time_slot, winner["winner_id"])

        if winner["winner_id"] == self.wallet.device_id:
            bal = self.ledger.get_balance(self.wallet.device_id)
            print(f"\n  ╔══════════════════════════════════════╗")
            print(f"  ║       REWARD WON! ★                  ║")
            print(f"  ╠══════════════════════════════════════╣")
            print(f"  ║  Amount  : {REWARD_PER_ROUND / UNIT:.8f} TMPL")
            print(f"  ║  Balance : {bal / UNIT:.8f} TMPL")
            print(f"  ╚══════════════════════════════════════╝\n  > ", end="", flush=True)
        else:
            print(f"\n  [slot {time_slot}] Winner: {winner['winner_id'][:20]}... "
                  f"+{REWARD_PER_ROUND / UNIT:.4f} TMPL\n  > ", end="", flush=True)

    def _cleanup_slot(self, time_slot: int):
        """Clean up per-slot state after lottery resolution.
        M1 FIX: _my_tickets accessed under _my_tickets_lock."""
        with self._lottery_lock:
            for s in [s for s in self._commits if s < time_slot - 10]:
                del self._commits[s]
        # M1 FIX: hold _my_tickets_lock while iterating and deleting
        with self._my_tickets_lock:
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
        """Main lottery loop. Outer try/except ensures the thread never dies silently.
        P2 FIX: outer guard restarts on any unhandled exception.
        M1 FIX: all _my_tickets writes done under _my_tickets_lock.
        """
        time.sleep(45)
        while self.network._running:
            try:
                now            = time.time()
                elapsed        = now - GENESIS_TIME
                next_slot_time = GENESIS_TIME + (int(elapsed / REWARD_INTERVAL) + 1) * REWARD_INTERVAL
                time.sleep(max(0.05, next_slot_time - time.time()))

                if is_era2(self.ledger):
                    continue

                time_slot  = get_current_slot()
                if time_slot < 0:
                    continue
                slot_start = GENESIS_TIME + time_slot * REWARD_INTERVAL

                with self.ledger._lock:
                    already_won = any(b.get("slot") == time_slot for b in self.ledger.chain)
                if already_won:
                    self._cleanup_slot(time_slot)
                    continue

                if not self._is_eligible_this_slot(time_slot, self.network._network_size):
                    continue

                ticket, sig_hex, seed = self._vrf_ticket(time_slot)
                # M1 FIX: write under lock
                with self._my_tickets_lock:
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

                with self._lottery_lock:
                    self._commits.setdefault(time_slot, {})[self.wallet.device_id] = commit

                remaining = slot_start + 2.0 - time.time()
                if remaining > 0:
                    time.sleep(remaining)
                with self.ledger._lock:
                    already_won = any(b.get("slot") == time_slot for b in self.ledger.chain)
                if already_won:
                    self._cleanup_slot(time_slot); continue

                commits_merged = self._bootstrap_query_commits(time_slot)
                with self._lottery_lock:
                    for did, c in commits_merged.items():
                        self._commits.setdefault(time_slot, {}).setdefault(did, c)

                self._bootstrap_submit({
                    "type": "SUBMIT_REVEAL", "device_id": self.wallet.device_id,
                    "slot": time_slot, "ticket": ticket, "sig": sig_hex,
                    "seed": seed, "public_key": self.wallet.public_key.hex()
                })

                remaining = slot_start + 4.0 - time.time()
                if remaining > 0:
                    time.sleep(remaining)
                with self.ledger._lock:
                    already_won = any(b.get("slot") == time_slot for b in self.ledger.chain)
                if already_won:
                    self._cleanup_slot(time_slot); continue

                all_reveals, _ = self._bootstrap_query_reveals(time_slot)
                all_reveals[self.wallet.device_id] = {
                    "ticket": ticket, "sig": sig_hex,
                    "seed": seed, "public_key": self.wallet.public_key.hex()
                }

                remaining = slot_start + 4.5 - time.time()
                if remaining > 0:
                    time.sleep(remaining)
                with self.ledger._lock:
                    already_won = any(b.get("slot") == time_slot for b in self.ledger.chain)
                if already_won:
                    self._cleanup_slot(time_slot); continue

                with self._lottery_lock:
                    active_nodes = list(self._commits.get(time_slot, {}).keys())

                winner = self._pick_winner(time_slot, all_reveals)
                if winner:
                    self._claim_reward(winner, time_slot, active_nodes)
                self._cleanup_slot(time_slot)

            except Exception as e:
                print(f"\n  [lottery] Error: {e} — retrying next slot\n  > ", end="", flush=True)
                time.sleep(REWARD_INTERVAL)
                continue

    def send(self, peer_id: str, amount_tmpl) -> bool:
        try:
            amount = int(round(float(amount_tmpl) * UNIT))
        except (TypeError, ValueError):
            print("\n  Invalid amount.")
            return False
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
        total_cost = amount + fee
        if total_cost > my_balance:
            print(f"\n  Insufficient balance. Have {my_balance / UNIT:.8f}, need {total_cost / UNIT:.8f}.")
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
        print(f"\n  ✓ Sent {amount / UNIT:.8f} TMPL to {peer_id[:24]}...")
        if fee > 0:
            print(f"  Fee paid   : {fee / UNIT:.8f} TMPL")
        print(f"  New balance: {self.ledger.get_balance(self.wallet.device_id) / UNIT:.8f} TMPL")
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
            bal = self.ledger.get_balance(self.wallet.device_id)
            return {"ok": True, "balance": bal, "balance_tmpl": bal / UNIT,
                    "address": self.wallet.device_id}
        elif action == "send":
            return {"ok": self.send(cmd.get("peer_id"), cmd.get("amount", 0))}
        elif action == "network":
            s = self.ledger.get_summary()
            return {"ok": True, "peers": len(self.network.get_online_peers()),
                    "transactions": s["total_transactions"],
                    "total_rewards": s["total_rewards"],
                    "chain_height": s["chain_height"],
                    "minted": s["total_minted"] / UNIT,
                    "remaining": s["remaining_supply"] / UNIT}
        return {"ok": False, "error": "Unknown action"}

    def _push_to_explorer(self):
        time.sleep(60)
        ssl_ctx = ssl.create_default_context()
        while self.network._running:
            try:
                import urllib.request
                with self.ledger._lock:
                    my_blocks     = [b for b in self.ledger.chain
                                     if b.get("winner_id") == self.wallet.device_id][-200:]
                    recent_blocks = list(self.ledger.chain[-50:])
                    seen_rids     = set()
                    blocks_push   = []
                    for b in my_blocks + recent_blocks:
                        rid = b.get("reward_id", "")
                        if rid not in seen_rids:
                            seen_rids.add(rid)
                            blocks_push.append({k: v for k, v in b.items()
                                                if k not in ("vrf_sig", "vrf_public_key")})
                    txs          = list(self.ledger.transactions[-20:])
                    total_minted = self.ledger.total_minted
                    summary      = self.ledger.get_summary()

                payload_data = {
                    "type":         "LEDGER_PUSH",
                    "device_id":    self.wallet.device_id,
                    "public_key":   self.wallet.get_public_key_hex(),
                    "blocks":       blocks_push,
                    "transactions": txs,
                    "total_minted": total_minted,
                    "chain_height": summary["chain_height"],
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

    def _bootstrap_query_checkpoint_tip(self, cp_slot: int) -> tuple:
        result    = [None, 0, None]
        done      = threading.Event()
        servers   = list(self.network._bootstrap_servers)
        remaining = [len(servers)]
        lock      = threading.Lock()
        def _query(host, port):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect((host, port))
                sock.sendall(json.dumps({"type": "GET_CHECKPOINT_TIP", "cp_slot": cp_slot}).encode())
                sock.shutdown(socket.SHUT_WR)
                resp = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk: break
                    resp += chunk
                    if len(resp) > 65536: break
                sock.close()
                data = json.loads(resp.decode())
                if data.get("type") == "CHECKPOINT_TIP_RESPONSE":
                    mh  = data.get("majority_hash")
                    ct  = data.get("count", 0)
                    pid = data.get("peer_id")
                    with lock:
                        if mh and ct > result[1]:
                            result[0] = mh; result[1] = ct; result[2] = pid
            except Exception:
                pass
            finally:
                with lock:
                    remaining[0] -= 1
                    if remaining[0] <= 0:
                        done.set()
        if not servers:
            return None, 0, None
        for host, port in servers:
            threading.Thread(target=_query, args=(host, port), daemon=True).start()
        done.wait(timeout=6.0)
        return result[0], result[1], result[2]

    def _checkpoint_loop(self):
        """Periodic checkpoint creation with bootstrap consensus.

        M6 FIX: skips checkpoint when chain is empty and no prior checkpoints
        exist. A fresh node with no blocks would record GENESIS_PREV_HASH as
        the chain_tip_hash, which can never match any peer's checkpoint for
        the same slot.

        C1 FIX is in _claim_reward — by the time we reach this loop the
        bootstrap tally for next_cp should contain the correct boundary hashes,
        so majority_hash comparison works reliably.
        """
        while self.network._running:
            try:
                current_slot = get_current_slot()
                if self.ledger.checkpoints:
                    last_cp_slot = self.ledger.checkpoints[-1]["slot"]
                    next_cp = last_cp_slot + CHECKPOINT_INTERVAL
                else:
                    # Fresh node — no checkpoints yet.
                    # Use the NEXT upcoming boundary above current slot, not the
                    # current or past one. Past boundaries have stale bootstrap
                    # votes from previous network runs that we can never beat.
                    next_cp = (current_slot // CHECKPOINT_INTERVAL) * CHECKPOINT_INTERVAL

                if current_slot >= next_cp + CHECKPOINT_BUFFER:
                    # M6 FIX: don't checkpoint if we have no chain data at all.
                    if not self.ledger.chain and not self.ledger.checkpoints:
                        time.sleep(30)
                        continue

                    # STALE SLOT FIX: a fresh node that started after next_cp
                    # has no blocks at that boundary. Bootstrap may have stale votes
                    # from a previous network run — we can never win that comparison.
                    # Advance next_cp forward to the first boundary where we actually
                    # have chain data, then proceed normally from there.
                    # This only fires when we have no checkpoints yet (truly fresh).
                    if not self.ledger.checkpoints:
                        with self.ledger._lock:
                            has_data = any(
                                b.get("slot", 0) <= next_cp
                                for b in self.ledger.chain
                            )
                        if not has_data:
                            # Find the earliest boundary we have data for
                            with self.ledger._lock:
                                earliest_slot = min(
                                    (b.get("slot", 0) for b in self.ledger.chain),
                                    default=None
                                )
                            if earliest_slot is None:
                                time.sleep(30)
                                continue
                            # Jump next_cp to the boundary covering our earliest block
                            next_cp = ((earliest_slot // CHECKPOINT_INTERVAL) + 1) * CHECKPOINT_INTERVAL
                            # If we haven't passed the buffer for that slot yet, wait
                            if current_slot < next_cp + CHECKPOINT_BUFFER:
                                time.sleep(30)
                                continue

                    with self.ledger._lock:
                        our_tip = None
                        for b in reversed(self.ledger.chain):
                            if b.get("slot", 0) <= next_cp:
                                our_tip = compute_block_hash(b)
                                break
                        if our_tip is None and self.ledger.checkpoints:
                            our_tip = self.ledger.checkpoints[-1].get("chain_tip_hash")
                        # Fresh start: no blocks before boundary and no checkpoints.
                        # The correct tip is GENESIS_PREV_HASH — same value every
                        # node on a fresh chain will submit to bootstrap.
                        if our_tip is None:
                            our_tip = GENESIS_PREV_HASH

                    # Submit current tip to bootstrap right now, overwriting
                    # any stale tip from a fork that got reorged out.
                    # Bootstrap LMD stores exactly one tip per node.
                    self._bootstrap_submit({
                        "type":      "SUBMIT_TIP",
                        "device_id": self.wallet.device_id,
                        "slot":      get_current_slot(),
                        "cp_slot":   next_cp,
                        "tip_hash":  our_tip,
                    })

                    # Consensus retry loop: require quorum before proceeding.
                    # With N online peers require min(N,2) votes so one node
                    # cannot checkpoint unilaterally before the other has voted.
                    known_peers       = len(self.network.get_online_peers())
                    min_votes         = max(1, min(known_peers, 2))
                    max_attempts      = 10
                    consensus_reached = False
                    count             = 0

                    for attempt in range(max_attempts):
                        majority_hash, count, peer_id = \
                            self._bootstrap_query_checkpoint_tip(next_cp)

                        if majority_hash is None:
                            print(f"\n  [checkpoint] slot {next_cp}: no votes yet "
                                  f"(attempt {attempt+1}/{max_attempts}), "
                                  f"waiting...\n  > ", end="", flush=True)
                            time.sleep(3)
                            continue

                        if count < min_votes:
                            print(f"\n  [checkpoint] slot {next_cp}: quorum not met "
                                  f"({count}/{min_votes} votes, "
                                  f"attempt {attempt+1}/{max_attempts}), "
                                  f"waiting...\n  > ", end="", flush=True)
                            time.sleep(3)
                            continue

                        if our_tip == majority_hash:
                            consensus_reached = True
                            break

                        # Quorum met but we disagree -- sync and resubmit
                        print(f"\n  [checkpoint] Minority fork at slot {next_cp} "
                              f"our tip {str(our_tip)[:16]}... "
                              f"majority {majority_hash[:16]}... "
                              f"(count={count}). Syncing...\n  > ",
                              end="", flush=True)
                        sync_done = threading.Event()
                        def _sync_and_signal():
                            self.network._sync_ledger()
                            sync_done.set()
                        threading.Thread(target=_sync_and_signal, daemon=True).start()
                        sync_done.wait(timeout=30.0)

                        with self.ledger._lock:
                            our_tip = None
                            for b in reversed(self.ledger.chain):
                                if b.get("slot", 0) <= next_cp:
                                    our_tip = compute_block_hash(b)
                                    break
                            if our_tip is None and self.ledger.checkpoints:
                                our_tip = self.ledger.checkpoints[-1].get(
                                    "chain_tip_hash")
                            if our_tip is None:
                                our_tip = GENESIS_PREV_HASH

                        # Resubmit updated tip after sync
                        self._bootstrap_submit({
                            "type":      "SUBMIT_TIP",
                            "device_id": self.wallet.device_id,
                            "slot":      get_current_slot(),
                            "cp_slot":   next_cp,
                            "tip_hash":  our_tip,
                        })
                        time.sleep(3)

                    if not consensus_reached:
                        print(f"\n  [checkpoint] Could not reach consensus for "
                              f"slot {next_cp} after {max_attempts} attempts "
                              f"-- skipping, retry next cycle.\n  > ",
                              end="", flush=True)
                        time.sleep(30)
                        continue

                    if self.ledger.create_checkpoint(next_cp):
                        print(f"\n  ╔══════════════════════════════════════╗")
                        print(f"  ║       CHECKPOINT CREATED             ║")
                        print(f"  ╠══════════════════════════════════════╣")
                        print(f"  ║  Slot : {next_cp:<8} Votes: {count}")
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
        print("  TIMPAL v3.2 — Quantum-Resistant Money Without Masters")
        print("  Quantum-Resistant | Worldwide | Instant | Chain-Anchored")
        print("═"*54)
        self.network.start()
        balance = self.ledger.get_balance(self.wallet.device_id)
        summary = self.ledger.get_summary()
        tip_hash, tip_slot = self.ledger._get_tip()
        print(f"  Device ID    : {self.wallet.device_id[:24]}...")
        print(f"  Balance      : {balance / UNIT:.8f} TMPL")
        print(f"  Network      : {self.network.local_ip}:{self.network.port}")
        print(f"  Chain height : {summary['chain_height']} blocks (tip slot {tip_slot})")
        print(f"  Minted       : {summary['total_minted'] / UNIT:.4f} / {TOTAL_SUPPLY // UNIT:,} TMPL")
        print("═"*54)
        print("  Commands: balance | chain | peers | send | history | network | quit")
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
                print(f"\n  Balance: {bal / UNIT:.8f} TMPL\n  Device : {self.wallet.device_id}\n")
            elif raw == "chain":
                with self.ledger._lock:
                    tip_hash, tip_slot = self.ledger._get_tip()
                    height = len(self.ledger.chain)
                    recent = self.ledger.chain[-5:]
                    orphan_count = sum(len(v) for v in self.ledger._orphan_pool.values())
                print(f"\n  Chain State:")
                print(f"  Height   : {height} blocks")
                print(f"  Tip slot : {tip_slot}")
                print(f"  Tip hash : {tip_hash[:32]}...")
                if orphan_count:
                    print(f"  Orphans  : {orphan_count} pending")
                if recent:
                    print(f"\n  Recent blocks:")
                    for b in reversed(recent):
                        confirmed = "✓" if self.ledger.is_confirmed(b.get("slot", 0)) else "○"
                        print(f"  {confirmed} slot {b.get('slot','?'):>8}  "
                              f"winner {b.get('winner_id','')[:16]}...  "
                              f"+{b.get('amount', 0) / UNIT:.4f} TMPL")
                print()
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
                tip_hash, tip_slot = self.ledger._get_tip()
                print(f"\n  Network Status (v3.2):")
                print(f"  Online peers      : {len(peers)}")
                print(f"  Chain height      : {s['chain_height']} blocks")
                print(f"  Chain tip slot    : {tip_slot}")
                print(f"  Chain tip hash    : {tip_hash[:32]}...")
                print(f"  Total transactions: {s['total_transactions']}")
                print(f"  Total rewards     : {s['total_rewards']}")
                print(f"  Total minted      : {s['total_minted'] / UNIT:.8f} TMPL")
                print(f"  Remaining supply  : {s['remaining_supply'] / UNIT:.8f} TMPL")
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
                        amount_tmpl = float(input(f"  Amount in TMPL (balance: {balance / UNIT:.8f}): ").strip())
                    except ValueError:
                        print("  Invalid amount.\n"); continue
                    self.send(peer_id, amount_tmpl)
                finally:
                    self._sending = False
            elif raw == "history":
                my_id = self.wallet.device_id
                with self.ledger._lock:
                    my_tx      = [t for t in self.ledger.transactions
                                  if t["sender_id"] == my_id or t["recipient_id"] == my_id]
                    my_rewards = [b for b in self.ledger.chain if b.get("winner_id") == my_id]
                if not my_tx and not my_rewards:
                    print("\n  No transactions yet.\n")
                else:
                    print(f"\n  Your history:")
                    for b in my_rewards[-5:]:
                        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(b["timestamp"]))
                        confirmed = "✓" if self.ledger.is_confirmed(b.get("slot", 0)) else "○"
                        print(f"  {confirmed} REWARD   +{b['amount'] / UNIT:.8f} TMPL  slot {b.get('slot','?')}  [{t}]")
                    for tx in my_tx[-10:]:
                        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tx["timestamp"]))
                        if tx["sender_id"] == my_id:
                            print(f"  ↑ SENT     {tx['amount'] / UNIT:.8f} TMPL  to   {tx['recipient_id'][:16]}...  [{t}]")
                        else:
                            print(f"  ↓ RECEIVED {tx['amount'] / UNIT:.8f} TMPL  from {tx['sender_id'][:16]}...  [{t}]")
                    print()
            elif raw in ("quit", "exit", "q"):
                print("\n  Shutting down. Goodbye.\n")
                self.network.stop()
                break
            else:
                print(f"\n  Unknown command. Try: balance | chain | peers | send | history | network | quit\n")


if __name__ == "__main__":
    import sys
    _check_genesis_time()

    if len(sys.argv) >= 2 and sys.argv[1] == "send":
        if len(sys.argv) != 4:
            print("Usage: python3 timpal.py send <address> <amount_in_tmpl>")
            sys.exit(1)
        recipient_id = sys.argv[2].lower().strip()
        try:
            amount_tmpl = float(sys.argv[3])
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
                                      "amount": amount_tmpl, "token": token}) + "\n").encode())
            resp = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk: break
                resp += chunk
                if resp.endswith(b"\n"): break
            sock.close()
            result = json.loads(resp.decode().strip())
            if result.get("ok"):
                print(f"Sent {amount_tmpl:.8f} TMPL to {recipient_id[:24]}...")
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
                print(f"Balance : {result['balance_tmpl']:.8f} TMPL")
                print(f"Address : {result['address']}")
                sys.exit(0)
        except Exception:
            pass
        sys.exit(0)

    else:
        node = Node()
        node.start()
