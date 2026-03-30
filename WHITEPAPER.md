# TIMPAL — Quantum-Resistant Money Without Masters

A Quantum-Resistant Peer-to-Peer Payment Protocol

March 30, 2026 — v3.3

---

## Abstract

TIMPAL is a peer-to-peer payment protocol designed to function without banks, payment processors, or centralized infrastructure. It uses quantum-resistant Dilithium3 cryptography, a chain-anchored distributed ledger, an eligibility-gated commit-reveal VRF lottery, on-chain identity registration with consensus-enforced maturation, and a two-era economic model to create a fair, decentralized monetary system with a fixed supply of 250 million TMPL distributed over 37.5 years.

The protocol enforces one node per physical device and requires every node to register its identity on-chain and wait 200 slots (~16.7 minutes) before participating in block production. Every transaction costs 0.0005 TMPL, paid to the slot winner, from genesis. No pre-mine. No insider allocation. No central authority.

Genesis launched March 28, 2026 at 6:00 AM PST.

---

## 1. The Problem

The global financial system excludes billions of people. A person in Manila, Nairobi, or Caracas with a smartphone has no reliable access to the basic tools of financial participation: the ability to send value to another person instantly, without asking permission.

Existing solutions fail in predictable ways:

- Traditional banking requires physical infrastructure, government ID, and credit history that billions of people do not have.
- Existing cryptocurrencies require mining hardware, technical knowledge, or exposure to extreme price volatility.
- Mobile money systems are controlled by corporations that can freeze accounts, charge fees, or cease operations.
- All of the above fail during infrastructure outages, natural disasters, or political instability.

TIMPAL is built to work when everything else stops working.

---

## 2. The Solution

TIMPAL provides a simple protocol: run the software, earn rewards, send TMPL to anyone on the network instantly. No registration. No bank account. No hardware beyond the device you already own.

The protocol runs on Mac, Windows, and Linux computers running Python 3.8 or newer.

```
pip3 install dilithium-py cryptography
curl -O https://raw.githubusercontent.com/EvokiTimpal/timpal/main/timpal.py
python3 timpal.py
```

No configuration. No account creation. No KYC.

---

## 3. Protocol Architecture

### 3.1 Chain-Anchored Distributed Ledger

TIMPAL uses a chain-anchored distributed ledger. Every node holds a complete copy. There is no proof-of-work and no mining. Each five-second time slot produces exactly one block — the reward won by the VRF lottery winner for that slot. Every block carries a `prev_hash` field: the SHA-256 hash of the previous block's canonical serialization. This links every block to a single unambiguous history.

The chain gives the protocol global ordering and partition recovery. When two nodes that were disconnected reconnect, their chains are compared. The heavier chain wins — weight is computed as the number of blocks minus a penalty for slot gaps, so a dense chain always beats a sparse one of equal or greater block count. On equal weight, the chain with the lower tip hash wins — a deterministic rule that produces identical outcomes on every node regardless of which chain arrived first.

Blocks at least six slots deep are considered confirmed (~30 seconds). This is the protocol's finality depth — a transaction buried under six blocks cannot be reversed under normal network conditions.

Double-spend prevention is enforced by checking the sender's balance against the full chain before accepting any transaction. Every ~83 minutes the network automatically creates a checkpoint — a cryptographically verified snapshot of all balances — and prunes the raw history before it. Nodes only need to store data since the last checkpoint, keeping the ledger lightweight forever.

### 3.2 Quantum-Resistant Cryptography

TIMPAL uses Dilithium3, selected as a post-quantum digital signature standard by NIST in 2024. Every device generates a unique key pair on first launch. The private key never leaves the device. All transactions and VRF tickets are signed and verifiable against quantum and classical attacks.

### 3.3 Wallet Encryption

The private key is encrypted at rest using AES-256-GCM. The encryption key is derived from a user-supplied password using scrypt (N=131072, r=8, p=1) with a randomly generated 32-byte salt. A random 12-byte nonce is used for each save. The plaintext private key is never written to disk.

On first run, the user is prompted to set a password before the wallet is saved. On subsequent runs, the password is required to decrypt and load the wallet. Wrong passwords are rejected before the key is loaded — the wallet file is never modified on a failed attempt.

Existing unencrypted wallets are detected on startup and the user is offered an immediate migration path. The protocol will not run with an unencrypted wallet without explicit user acknowledgment.

The security of the wallet is bounded by the strength of the password. There is no recovery mechanism — a forgotten password means permanent loss of access to the wallet.

### 3.4 Network Topology

- **Local:** UDP broadcast on port 7778 for same-network peer discovery.
- **Global:** Bootstrap server at bootstrap.timpal.org:7777 introduces nodes worldwide. Stores no funds. Controls nothing.

Once connected, nodes communicate directly peer-to-peer. The bootstrap server is not involved in transactions, rewards, or lottery operation. It serves only as a peer directory and optional lottery relay. Community-operated bootstrap servers are welcome — the more servers, the more resilient the network.

### 3.5 Eligibility-Gated Commit-Reveal VRF Lottery

Every 5 seconds, one node wins 1.0575 TMPL. The lottery uses an eligibility gate and a commit-reveal scheme to ensure fairness at any network size. Commits and reveals are propagated both through the bootstrap server and directly between peers via P2P gossip, ensuring the lottery continues even if bootstrap is unreachable.

**Eligibility gate.** Before each slot, every node independently checks whether it is eligible to participate. The check uses a deterministic hash of the node's device ID and the slot number:

```
eligible = sha256(f"{device_id}:{slot}") < threshold × 2²⁵⁶
```

The threshold targets approximately 10 eligible nodes per slot regardless of total network size. At 100 nodes the threshold is 1.0 (everyone is eligible). At 1,000,000 nodes the threshold is 0.00001 (roughly 10 nodes are eligible). Bootstrap and every node use the identical formula, so eligibility is deterministic and independently verifiable. Sybil attacks become economically irrational: multiplying device count multiplies cost, but expected reward per device remains constant because the eligible fraction shrinks proportionally.

**Commit phase (t=0.0).** Each eligible node signs the current slot number with its Dilithium3 private key. The VRF ticket is the SHA256 hash of that signature — unique per node per slot, unpredictable without the private key. The node submits a SHA256 commitment:

```
commit = sha256(f"{ticket}:{device_id}:{slot}")
```

The commit is submitted to the bootstrap server and simultaneously broadcast as a `LOTTERY_COMMIT` message directly to connected peers. The bootstrap server responds with COMMIT_ACK or COMMIT_REJECTED (if the node is banned — see §3.6). A node that does not receive COMMIT_ACK does not proceed.

**Reveal phase (t=2.0).** Each committed node submits its actual ticket, signature, seed, and public key to the bootstrap server and simultaneously broadcasts a `LOTTERY_REVEAL` message directly to connected peers. The bootstrap server records it. A node that committed but does not reveal within the window is recorded as a missed reveal.

**Winner selection (t=4.0–4.5).** Every eligible node collects all reveals from bootstrap and from peers received directly via P2P gossip, merging both sources. Each node independently computes the collective target:

```
target = sha256(sorted_tickets_joined_with_colon)
```

This value cannot be known until the reveal window closes — no node can predict it in advance or cherry-pick whether to reveal based on whether it wins. The winner is the node whose ticket is closest to the target by integer distance, with device ID as a tiebreaker:

```
winner = min(verified_nodes, key=lambda d: (|ticket_int - target_int|, device_id))
```

Every node independently verifies all reveals using the committed hashes and Dilithium3 signatures, then picks the same winner using identical math. The bootstrap server stores commits and reveals but cannot influence the outcome — all verification and winner selection happens on the nodes.

**Block construction.** The winning node builds a block containing the winner's identity, the reward amount, all four VRF proof fields, any pending identity registrations (up to 10), and — critically — the SHA-256 hash of the previous block (`prev_hash`). This links the new block to the chain. The block is added to the local chain and broadcast to all peers.

**SUBMIT_TIP.** After winning, the node also notifies the bootstrap server of the new chain tip. This allows joining nodes to immediately know how far ahead the chain is and request only the blocks they are missing.

### 3.6 Reveal Obligation Enforcement

Selective reveal is a potential attack: a node commits in every slot, then only reveals when it has computed that it would win, gaining information about the outcome before deciding whether to participate. This is prevented by the reveal obligation.

Any node that commits but does not reveal is recorded as a missed reveal. After one missed reveal, the node is banned from committing for 10 slots. During a ban, the bootstrap server rejects all commit submissions from that device ID, and the node skips those slots entirely.

The ban counter resets to zero after a ban is served. This makes selective reveal economically unattractive: the expected gain from cherry-picking a winning slot does not outweigh the cost of the 10-slot ban that follows.

### 3.7 One Node Per Device

An OS-level file lock prevents more than one node running per device. Any second attempt exits immediately. More rewards require more physical devices — the same constraint for everyone.

### 3.8 Fork Choice and Chain Convergence

**Normal extension.** When a node receives a new block, it validates the VRF proof, checks that `prev_hash` matches the current chain tip, and appends the block. This is the common case.

**Fork detection and reorg.** When a node receives blocks that do not connect to its current tip — because the network was partitioned, or because two nodes won the lottery in the same slot due to a race — the protocol detects a fork and attempts a chain reorganization:

1. Build a hash-to-index map of the current chain.
2. Find the block in the incoming set whose `prev_hash` connects to the local chain or checkpoint tip.
3. Walk forward from that fork point, validating every block in the incoming set sequentially (VRF proof, chain linkage, slot ordering, supply cap).
4. Compare the **weight** of the incoming tail to the local tail from the fork point.

**Fork choice rule.** The heavier chain wins. Weight is computed as the number of blocks minus a penalty for slot gaps greater than 1 — a dense chain always beats a sparse chain, closing the sparse-chain attack vector. On equal weight, the chain whose tip hash is numerically lower wins. This tie-breaking rule is deterministic and order-independent — every node in the network arrives at the same decision regardless of which chain it saw first.

```
weight = blocks - sum(gap - 1 for each gap > 1 between consecutive slots)
```

**Soft finality and depth limit.** Reorgs that would anchor more than 100 slots behind the current tip are rejected outright — this prevents long-range reorg attacks while allowing honest partition recovery. A reorg replacing more than 100 blocks in a single operation is also rejected to prevent CPU exhaustion at scale.

**Post-reorg state cleanup.** After switching chains, the protocol recomputes `total_minted` from the new chain and removes any transactions that are no longer funded by surviving block rewards. This prevents phantom balances.

**Finality.** A block is considered confirmed once it is at least `CONFIRMATION_DEPTH = 6` slots deep (~30 seconds). Confirmed blocks cannot be reversed under normal network conditions.

### 3.9 Transaction Rate Limiting

Each device is limited to 60 transactions per minute. This prevents spam and flood attacks while comfortably supporting all legitimate use cases. Since one node per device is enforced at the OS level, this limit applies equally to every participant on the network.

### 3.10 Ledger Checkpoint System

Without checkpointing, the ledger would grow impractically large over 37.5 years, making it difficult for nodes in regions with limited storage or bandwidth.

Every 1,000 slots (approximately 83 minutes), every node independently creates a checkpoint. The checkpoint records the balance of every address at that moment — including all fee rewards earned up to that point — the complete identity registration table, the total supply minted, the SHA-256 hash of the chain tip at the time of pruning, and cryptographic hashes of all pruned rewards, transactions, and fee rewards. The chain tip hash is stored so that blocks produced after the checkpoint can be correctly linked — a block's `prev_hash` must match the stored tip even though the block it references has been pruned.

Before accepting a checkpoint received from a peer, every node independently recomputes the balance of every address from its own local chain history and rejects the checkpoint if the recomputed balances do not match. This means a checkpoint with a corrupted or manipulated balance distribution cannot be accepted by honest nodes — the local chain history is the ground truth, not the peer's claim.

A 120-slot buffer (10 minutes) is applied before pruning, giving late-arriving data time to propagate across the network before being permanently removed. Checkpoints are gossiped to peers automatically. A new node joining the network receives the latest checkpoint first, then only the blocks since that checkpoint — never the full history.

The process is fully automatic and requires no human intervention. It runs identically on every node forever.

### 3.11 Protocol Version Enforcement

Every node declares its version when connecting to any peer or bootstrap server. If the declared version is below the network minimum, the connection is rejected immediately with a clear message directing the operator to update from GitHub.

This follows the same model as Bitcoin: no central authority forces updates. Developers publish fixes openly. Node operators update voluntarily. Updated nodes automatically reject outdated ones. The network migrates to the new version through social consensus with no coordination required.

The minimum version is a constant defined in both timpal.py and bootstrap.py. When a rule-changing update is published, the minimum version is bumped. Nodes that do not update are naturally excluded from the network.

### 3.12 Push Authentication

Every node periodically pushes its ledger data to the explorer API at timpal.org. There is no shared secret. Instead, each push is signed with the node's Dilithium3 private key. The API verifies that the signature is valid and that `sha256(public_key) == device_id` before accepting any data. A node cannot impersonate another node's push — it would require the target's private key.

### 3.13 On-Chain Identity Registration and Maturation

Every new node broadcasts a signed `REGISTER` message to its peers on startup. This message contains the node's device ID, public key, genesis block hash (if applicable), and a Dilithium3 signature over `f"{device_id}:{genesis_block_hash}"`. Peers validate the signature and device ID derivation, then store the registration in a pending pool.

When a node wins a slot and produces a block, it embeds up to 10 pending registrations from its pool directly into the block's `registrations` field. When any node accepts that block, it records each valid registration with the block's slot number as the identity's `first_seen_slot`:

```
identities[device_id] = block.slot
```

This value is consensus-derived — it comes from the chain, not from any self-declared field or network message. It cannot be forged, backdated, or manipulated by the registering node.

**Maturation rule.** Every node enforces the following check inside `_add_block_locked()`, the single function that accepts blocks into the chain:

```
if block.slot >= 1000:  # post-genesis phase only
    first_seen = identities.get(winner_id)
    if first_seen is None:
        return False   # unknown identity — not registered on-chain
    if block.slot - first_seen < MIN_IDENTITY_AGE:
        return False   # identity too young
```

`MIN_IDENTITY_AGE = 200` slots (~16.7 minutes). This check runs before the block is appended, so self-registration in the same block cannot bypass it — a producer cannot include their own REGISTER in the same block they are winning.

**Ordering invariant.** Registrations embedded in a block are processed after the block is appended to the chain. This ensures:

1. The maturation check sees the identity table as it existed before this block
2. New registrations take effect from this block's slot onward
3. A block producer cannot register and immediately use that identity in the same block

**Checkpoint persistence.** The identity table is included in every checkpoint:

```
checkpoint["identities"] = {device_id: first_seen_slot, ...}
```

When a checkpoint is applied, identities are merged using earliest-slot-wins logic. The identity table is never pruned — it grows with the number of unique nodes ever seen, which is bounded by the wallet creation rate. A node joining the network after the genesis phase receives the identity table in the first checkpoint it downloads and can immediately enforce the maturation rule for all future blocks.

**Security properties.** This design provides:

- **No instant activation.** A new identity must wait 200 slots after its REGISTER is included in a block before it can produce valid blocks. An attacker cannot create a wallet and immediately win slots.
- **No bypass via P2P.** Even if an attacker skips the bootstrap server entirely and connects only via P2P gossip, their blocks will be rejected by every honest node because their `first_seen_slot` will be either absent (never registered) or too recent.
- **Determinism.** Every honest node independently derives the same `first_seen_slot` from the same chain — there is no coordination required and no possibility of disagreement between nodes that share the same chain.
- **Pruning compatibility.** Because identity data lives in checkpoint state rather than raw block history, it survives the checkpoint pruning cycle indefinitely.

---

## 4. Two-Era Economic Model

**Era 1 — Distribution (Years 0 to 37.5)**

- Every transaction costs 0.0005 TMPL, paid to the slot winner
- Nodes earn through the eligibility-gated VRF lottery: 1.0575 TMPL every 5 seconds
- Total of 250,000,000 TMPL minted over 37.5 years
- No pre-mine, no insider allocation, no founder rewards

**Era 2 — Sustaining (Year 37.5 onwards)**

- No new TMPL can ever be created — supply is fixed at 250,000,000
- Every transaction carries a fee of 0.0005 TMPL
- The fee for each 5-second slot goes to the slot winner — the node that won the VRF lottery for that slot and built the block. This is consistent across both eras: the slot winner takes all fees for that slot.
- The protocol is self-sustaining forever with no inflation

---

## 5. Tokenomics

| Property | Value |
|---|---|
| Total Supply | 250,000,000 TMPL |
| Decimal Places | 8 |
| Reward Per Round (Era 1) | 1.0575 TMPL |
| Round Interval | Every 5 seconds |
| Distribution Period | 37.5 years |
| Eligible Nodes Per Slot | ~10 (fixed target, regardless of network size) |
| Identity Maturation Period | 200 slots (~16.7 minutes) |
| Transaction Fee (Era 1) | 0.0005 TMPL → slot winner |
| Transaction Fee (Era 2) | 0.0005 TMPL → slot winner |
| Checkpoint Interval | Every 1,000 slots (~83 minutes) |
| Pre-mine | None |
| Insider Allocation | None |
| Confirmation Depth | 6 slots (~30 seconds) |

**Verification:**
1.0575 × 12 rounds/min × 60 × 24 × 365 = 6,669,864 TMPL/year
250,000,000 ÷ 6,669,864 = 37.48 years

---

## 6. Security Model

### 6.1 Sybil Resistance

Sybil resistance operates at four independent layers:

1. **One node per device** enforced at the OS level via file lock.
2. **Eligibility gate** scales the participation threshold inversely with network size — multiplying device count multiplies infrastructure cost while expected reward per device stays constant.
3. **Chain-anchored wallet creation** (post-genesis phase): every new wallet must anchor its `device_id` to a live block hash from the running network. Offline mass wallet generation is physically impossible — the chain produces block hashes at fixed speed regardless of attacker CPU.
4. **On-chain identity maturation**: every identity must register on-chain and wait 200 slots (~16.7 minutes) before it can produce valid blocks. This check is enforced at the consensus layer in every node — it cannot be bypassed via P2P, bootstrap, or any other path. An attacker creating many wallets must wait 200 slots per identity before any of them become useful, converting a potential burst attack into a slow, expensive, time-constrained operation.

### 6.2 Double-Spend Prevention

Every node validates sender balance against the full chain before accepting any transaction. The first valid transaction spending a given balance is canonical. All nodes independently enforce this rule. After a chain reorganization, transactions that are no longer funded by surviving block rewards are automatically removed from the ledger.

### 6.3 Quantum Resistance

Dilithium3 protects all signatures — transactions, VRF tickets, identity registrations, and push authentication — against both classical and quantum computer attacks.

### 6.4 VRF Integrity

Every block carries a cryptographic VRF ticket derived from the winner's Dilithium3 private key signature. Any node can independently verify the winner by confirming:

1. The committed hash matches: `sha256(ticket:device_id:slot) == commit`
2. The Dilithium3 signature is valid for the slot seed
3. The ticket matches: `sha256(signature) == ticket`
4. The ticket is the closest to the collective target among all verified participants

All four checks must pass. A block missing any VRF field is rejected outright — there is no fallback path that accepts unverified blocks.

### 6.5 Selective Reveal Prevention

The collective target is the SHA256 of all tickets sorted and joined. It cannot be known until the reveal window closes. A node that commits cannot predict whether its ticket will win, so there is no information advantage to committing early and revealing selectively. Nodes that commit but do not reveal are banned for 10 slots after one missed reveal.

### 6.6 Chain Integrity

Every block's `prev_hash` must equal the SHA-256 of the previous block's canonical serialization. Canonical serialization uses `json.dumps(block, sort_keys=True, separators=(",",":"))` — deterministic across all nodes regardless of platform or Python version. Integer arithmetic is used throughout to eliminate floating-point precision divergence.

Any block with an incorrect `prev_hash` is rejected. An attacker cannot insert or reorder blocks without recomputing every subsequent hash in the chain, which requires forging all VRF proofs — computationally infeasible.

### 6.7 Fork Attack Resistance

A fork attack requires building a heavier chain than the honest chain. Since there is no proof-of-work, the relevant resource is time: an attacker can produce at most one block per slot per eligible device. The honest network collectively produces blocks at a faster rate than any single attacker with a realistic number of devices. The chain weight rule ensures the honest chain always wins — sparse chains built by isolated attackers are penalised by gap deductions.

### 6.8 Wallet Security

Private keys are encrypted at rest with AES-256-GCM. The encryption key is derived from the user's password via scrypt with a random salt. The plaintext private key exists only in memory while the node is running and is never written to disk in any form.

### 6.9 Bootstrap Server Trust Model

The bootstrap server is an optional relay. It records commits and reveals and gossips them to querying nodes, but it is not the sole path for lottery data — nodes also exchange commits and reveals directly via P2P gossip. A compromised or malicious bootstrap server can:

- Refuse to relay commits or reveals (honest nodes receive them from peers directly — lottery continues unaffected)
- Report a false or stale chain tip

It cannot:

- Stop lottery operation (P2P gossip delivers commits and reveals independently of bootstrap)
- Forge a valid VRF ticket (requires the target node's private key)
- Force a node to accept an invalid block (every node verifies chain linkage, VRF, and identity maturation independently)
- Steal funds or alter balances
- Corrupt a checkpoint (every node independently recomputes balances from local chain history before accepting)
- Bypass identity maturation (the maturation check is enforced at the consensus layer, not at bootstrap)

Community-operated bootstrap servers reduce the impact of any single server failing or misbehaving. The more servers, the more resilient the network. Bootstrap is the door to the network — not the lock.

---

## 7. Decentralization

TIMPAL is a protocol, not a product. Nobody owns it. Nobody controls it. The rules are in the code and the code is open.

The core protocol is complete. What gets built on top of it — mobile apps, GUI clients, exchanges, mesh networking, hardware wallets — is for the community to decide and build. The protocol does not depend on any of these things to function. It works today, as-is, for anyone with a computer and an internet connection.

Community bootstrap servers, community tools, and community applications are all welcome.

---

## 8. Conclusion

TIMPAL provides what the global financial system has failed to provide: a way for any person, anywhere, to hold and send value — without asking permission.

The eligibility-gated lottery ensures the reward system remains fair and efficient whether the network has 10 nodes or 10 million. The collective target prevents any node from predicting or manipulating the outcome. The reveal obligation closes the selective-reveal attack. P2P lottery gossip ensures the network continues producing blocks even if every bootstrap server goes offline. On-chain identity registration with consensus-enforced maturation prevents instant identity activation — every new participant must establish a verifiable on-chain history before winning slots. Independent checkpoint balance verification means no node can corrupt the ledger's balance history without detection. The chain spine anchors all rewards to a single, deterministic history. Chain-weight fork resolution with deterministic tie-breaking guarantees global convergence under any network partition. The two-era model ensures the network is self-sustaining forever — first through the lottery and fees, then through fees alone.

---

**GitHub:** https://github.com/EvokiTimpal/timpal
**Website:** https://timpal.org
**Bootstrap:** bootstrap.timpal.org:7777

*This document is released into the public domain. No rights reserved.*
