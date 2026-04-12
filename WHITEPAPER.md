# TIMPAL — Quantum-Resistant Money Without Masters

A Quantum-Resistant Peer-to-Peer Payment Protocol

April 2026 — v4.0

---

## Abstract

TIMPAL is a peer-to-peer payment protocol designed to function without banks, payment processors, or centralized infrastructure. It uses quantum-resistant Dilithium3 cryptography, a chain-anchored distributed ledger, a deterministic single-winner lottery, attestation-based cryptographic finality, on-chain identity registration with consensus-enforced maturation, and a two-era economic model to create a fair, decentralized monetary system with a fixed supply of 125 million TMPL distributed over approximately 37.5 years.

The protocol enforces one node per physical device and requires every node to register its identity on-chain and wait 200 slots (~33 minutes) before participating in block production. Transaction fees are 0.1% of the send amount (minimum 0.0001 TMPL, maximum 0.01 TMPL), paid to the slot winner, from genesis. No pre-mine. No insider allocation. No central authority.

---

## 1. The Problem

Cryptocurrency made a promise: open participation, no gatekeepers, equal access for anyone with a computer and an internet connection. That promise was broken almost immediately — and the breakage was structural, not accidental.

**The playing field was never level.** Bitcoin mining consolidated rapidly into industrial operations: warehouses of purpose-built hardware drawing megawatts of power, operated by companies with access to cheap electricity and capital that ordinary people simply do not have. The response from the industry — proof of stake — did not fix the problem. It replaced the hardware advantage with a capital advantage. You earn in proportion to what you already own. The more you started with, the more you accumulate. Someone who arrived early, or arrived wealthy, locked in a compounding lead that no latecomer can close. In both systems, the outcome is determined before you sit down.

**The timing advantage is permanent.** Early participants in existing networks hold positions of structural dominance that have nothing to do with effort, skill, or contribution. A node started today does not compete on the same terms as one started five years ago. The gap only widens. For the vast majority of people encountering these systems for the first time, the rational conclusion is that it is already too late — that the opportunity has passed to those who moved first. That conclusion is correct for every system where the rules were written to reward early entry over equal participation.

**The cryptographic foundation is already cracked.** Bitcoin, Ethereum, and virtually every major cryptocurrency secure their wallets with ECDSA — an algorithm that Shor's algorithm, running on a sufficiently powerful quantum computer, can break entirely. Any address that has ever exposed a public key on-chain becomes retroactively vulnerable the moment that threshold is crossed. Credible estimates place that threshold within this decade. When it arrives, there is no patch, no warning, and no migration that will reach everyone in time. Every protocol that did not build quantum resistance from the ground up cannot add it without starting over. Most of them will not start over.

These are not problems that can be fixed with an update. They are the result of foundational decisions that cannot be undone. A new protocol is required — one that treats equal participation and future-proof security not as features to be added, but as principles that every other design decision must answer to.

---

## 2. The Solution

TIMPAL answers each of those problems directly.

**On the equal playing field.** The protocol selects a single winner per slot using a deterministic lottery seeded by the previous block's hash. That selection is mathematically fair — every registered mature identity has equal probability of winning, regardless of how long they have been running, how many coins they hold, or when they joined. A node started today does not compete at a disadvantage against a node that has been running for three years. The odds are identical. There is no staking mechanism. There is no mining difficulty that rewards those with more hardware. The only requirement is a computer, an internet connection, and time — and those are equal for everyone.

**On the permanent advantage problem.** Existing networks reward early entrants with positions of structural dominance that compound indefinitely. TIMPAL's winner selection is based on two things only: which identities are currently registered, mature, and active on-chain, and the hash of the previous block. How long a node has been running does not affect its winning odds. How many rewards it has previously earned does not affect its winning odds. Every registered mature active node competes on the same terms in every slot, whether it joined at genesis or joined yesterday. Early participation earns early rewards — but it does not purchase a permanent advantage over everyone who comes after.

**On the quantum threat.** TIMPAL uses Dilithium3 as its sole cryptographic primitive — the post-quantum digital signature standard selected by NIST in 2024. There is no ECDSA anywhere in the protocol. Every wallet, every transaction signature, every block proof, and every attestation is built on cryptography that quantum computers cannot break with any known algorithm. This was not added as a feature after the fact. It is the foundation the protocol was built on from the first line of code.

The result is a network where any person with a Mac, Windows, or Linux computer can participate on equal terms — earning rewards through the lottery, sending TMPL to anyone on the network, and holding value in a wallet that does not depend on the continued irrelevance of quantum computing.

```
pip3 install dilithium-py cryptography pycryptodome mnemonic qrcode miniupnpc
curl -O https://raw.githubusercontent.com/EvokiTimpal/timpal/main/timpal.py
python3 timpal.py
```

No configuration. No account creation. No KYC. No hardware purchase. No coins required to start.

---

## 3. Protocol Architecture

### 3.1 Chain-Anchored Distributed Ledger

TIMPAL uses a chain-anchored distributed ledger. Every node holds a complete copy. There is no proof-of-work and no mining. Each ten-second time slot produces exactly one block — the reward won by the VRF lottery winner for that slot. Every block carries a `prev_hash` field: the SHA-256 hash of the previous block's canonical serialization. This links every block to a single unambiguous history.

The chain gives the protocol global ordering and partition recovery. When two nodes that were disconnected reconnect, their chains are compared. The heavier chain wins — weight is computed as the number of blocks minus a penalty for slot gaps greater than 1, so a dense chain always beats a sparse one. On equal weight, the chain with the lower tip hash wins — a deterministic rule that produces identical outcomes on every node regardless of which chain arrived first.

Blocks at least 5 slots deep (~50 seconds) achieve cryptographic finality via the attestation mechanism described in §3.8. This is the protocol's finality boundary — a block carrying a supermajority attestation cannot be reversed under any circumstances.

Double-spend prevention is enforced by validating each sender's balance against the full chain before accepting any transaction. Intra-block double-spend is prevented by tracking each sender's cumulative debit across all transactions within the same block. Every ~2.8 hours the network automatically creates a checkpoint — a cryptographically verified snapshot of all balances — and prunes the raw history before it. Nodes only need to store data since the last checkpoint, keeping the ledger lightweight forever.

### 3.2 Quantum-Resistant Cryptography

TIMPAL uses Dilithium3, selected as a post-quantum digital signature standard by NIST in 2024. Every device generates a unique Dilithium3 key pair on first launch. There is no ECDSA, RSA, or any classically vulnerable algorithm anywhere in the protocol.

This means every TIMPAL wallet is quantum-resistant from the moment it is created. A quantum computer cannot derive a private key from a public key, a wallet address, or anything broadcast on-chain. The private key never leaves the device. All transactions, winner proofs, attestations, block signatures, and push authentication are signed with Dilithium3 and verifiable against both quantum and classical attacks.

This is not an upgrade path or a migration layer on top of legacy cryptography — it is the only cryptographic primitive the protocol uses. There is no fallback to ECDSA under any condition.

### 3.3 Wallet Encryption and Recovery

The private key is encrypted at rest using AES-256-GCM. The encryption key is derived from a user-supplied password using scrypt (N=131072, r=8, p=1) with a randomly generated 32-byte salt. A random 12-byte nonce is used for each save. The plaintext private key is never written to disk. A minimum passphrase length of 20 characters is enforced at wallet creation.

On first run, the node generates a 12-word BIP39 recovery phrase from 128 bits of entropy. The user must type all 12 words back in full before the wallet is saved — there is no way to skip this step. The same phrase can be used to deterministically recover the Dilithium3 keypair on a new device. The security of the wallet is bounded by the strength of both the password and the physical security of the written phrase.

### 3.4 Network Topology

- **Local:** UDP broadcast on port 7778 for same-network peer discovery.
- **Global:** Bootstrap server at bootstrap.timpal.org:7777 introduces nodes worldwide. Stores no funds. Controls nothing. Not involved in transactions, rewards, or lottery operation.

Once connected, nodes communicate directly peer-to-peer. The bootstrap server serves only as a peer directory. Its role in the protocol is strictly limited: it accepts five message types (HELLO, PING, GET_PEERS, REGISTER_BOOTSTRAP, GET_BOOTSTRAP_SERVERS) and handles nothing else. Community-operated bootstrap servers are welcome — the more servers, the more resilient the network.

### 3.5 VRF Lottery

Every 10 seconds, one node wins 1.0575 TMPL. Every eligible node competes every slot by signing a challenge with its Dilithium3 private key and broadcasting the result. The node whose signature produces the lowest SHA-256 hash wins. This is a Verifiable Random Function (VRF): the outcome is unpredictable until all competitors have broadcast, yet it is independently verifiable by every node.

**Challenge.** At the start of each slot, a challenge is derived from the previous block's hash:

```
challenge = sha256(f"challenge:{prev_block_hash}:{slot}")
```

The challenge cannot be pre-computed because it depends on the previous block hash, which is unknown until that block arrives.

**Competing.** Every active mature identity signs the challenge with its Dilithium3 private key and broadcasts a COMPETE message:

```
compete_sig   = Dilithium3.sign(private_key, challenge)
compete_proof = sha256(compete_sig)
```

The compete_sig is unpredictable to anyone who does not hold that node's private key. No node can know its own proof — or anyone else's — before signing.

**Winner selection.** After COMPETEs are collected, the winner is the competitor with the lowest proof hash:

```
winner = competitor with lowest sha256(compete_sig)
```

This is determined independently by every node from the received COMPETE messages. An attacker controlling K of N active identities has K/N probability of winning — no more.

**Block construction.** The winning node builds a block containing `winner_id`, `compete_sig`, `compete_proof`, `reward_amount`, `fees_collected`, pending transactions (up to 500), pending identity registrations (up to 10), and the `prev_hash`. The winning node then signs the entire canonical block with its Dilithium3 key (`block_sig`). The block is broadcast to all peers.

**Verification.** Every node that receives a block independently verifies:

1. `winner_id` is an active mature identity (not dormant)
2. `challenge == sha256(f"challenge:{prev_block_hash}:{slot}")`
3. `Dilithium3.verify(public_key, challenge, compete_sig)` passes
4. `sha256(compete_sig) == compete_proof`
5. `winner_id` derives correctly from `public_key`
6. `Dilithium3.verify(public_key, canonical_block_without_sig, block_sig)` passes

All six checks must pass. A block failing any check is rejected outright. Exactly one valid block can exist per slot.

### 3.6 One Node Per Device

An OS-level file lock prevents more than one node running per device. Any second attempt exits immediately. More rewards require more physical devices — the same constraint for everyone.

### 3.7 Fork Choice and Chain Convergence

**Normal extension.** When a node receives a new block, it validates all six winner verification rules, checks that `prev_hash` matches the current chain tip, and appends the block. This is the common case.

**Fork detection and reorg.** When a node receives blocks that do not connect to its current tip, the protocol detects a fork and attempts a chain reorganization:

1. Find the fork point where the incoming chain branches from the local chain.
2. Walk forward from that fork point, performing full cryptographic verification on every incoming block sequentially — winner proof, block signature, all transaction signatures, chain linkage, slot ordering, supply cap.
3. Compare the weight of the incoming tail to the local tail from the fork point.

**Fork choice rule.** The heavier chain wins. Weight is block count minus a penalty for slot gaps greater than 1. On equal weight, the chain whose tip hash is numerically lower wins. This rule is deterministic and order-independent — every node arrives at the same decision regardless of which chain it saw first.

**Depth limits.** Reorgs anchoring more than 100 slots behind the current tip are rejected outright, preventing long-range reorg attacks while allowing honest partition recovery.

**Post-reorg cleanup.** After switching chains, `total_minted` is recomputed from the new chain and any transactions no longer funded by surviving block rewards are removed. This prevents phantom balances.

### 3.8 Attestation-Based Cryptographic Finality

After accepting a new block, every eligible node produces an attestation — a Dilithium3 signature over `f"attest:{block_hash}:{slot}"` — and broadcasts it to peers:

```
payload   = f"attest:{block_hash}:{slot}".encode()
signature = Dilithium3.sign(private_key, payload)
```

**Committee-based finality.** To keep finality fast at any network size, attestation uses a randomly-selected committee of 512 nodes per block. The committee is selected deterministically from the block hash and slot number — it cannot be predicted before the block is produced. When the network has fewer than 512 active mature nodes, every active node is on the committee (identical to the simple majority rule).

```
committee = deterministic_select(active_mature, block_hash, slot, size=512)
finalized = (committee_attestations / committee_size) > 2/3
```

At 33% attacker share the probability of corrupting more than 2/3 of a 512-node committee is approximately 10⁻⁴⁴ — computationally impossible.

Every attestation is verified: the attesting node must be a known registered identity, its public key must match the key stored in `identity_pubkeys` at registration time (preventing attestation forgery), the node must be a member of the committee for that block, and the Dilithium3 signature must be valid. A node cannot attest on behalf of another — it would require the target's private key.

A finalized block cannot be reorged under any circumstances short of breaking Dilithium3. This is a stronger guarantee than probabilistic finality: it requires an attacker to corrupt more than 1/3 of the attestation committee with valid post-quantum signatures.

### 3.9 Transaction Rate Limiting

Each device is limited to 10 unconfirmed transactions in the mempool at any time, and transactions expire after 100 slots (~17 minutes) if not included in a block. This prevents spam and flood attacks while comfortably supporting all legitimate use cases.

### 3.10 Ledger Checkpoint System

Every 1,000 slots (approximately 2.8 hours), every node independently creates a checkpoint. The checkpoint records:

- The balance of every address at that moment (including all fee rewards)
- The complete identity registration table with `first_seen_slot` for every identity
- The `identity_pubkeys` mapping (device_id → public key hex) for attestation binding
- The total supply minted
- The SHA-256 hash of the chain tip at the time of pruning

Before accepting a checkpoint received from a peer, every node independently recomputes the balance of every address from its own local chain history and rejects the checkpoint if the recomputed balances do not match. A checkpoint with corrupted or manipulated balances cannot be accepted by honest nodes.

A 120-slot buffer (~20 minutes) is applied before pruning, giving late-arriving data time to propagate. The identity table is never pruned — it grows with the number of unique nodes ever registered and survives every checkpoint cycle indefinitely.

A new node joining the network receives the latest checkpoint, then only the blocks produced since that checkpoint — never the full history.

### 3.11 Protocol Version Enforcement

Every node declares its version when connecting to any peer or bootstrap server. If the declared version is below the network minimum, the connection is rejected immediately with a message directing the operator to update from GitHub.

### 3.12 Push Authentication

Every node periodically pushes its ledger data to the explorer API. Each push is signed with the node's Dilithium3 private key. The API verifies that the signature is valid before accepting any data. A node cannot impersonate another node's push without the target's private key.

### 3.13 On-Chain Identity Registration and Maturation

Every new node broadcasts a signed `REGISTER` message to its peers on startup. This message contains the node's device ID, public key, genesis block hash (for post-genesis wallets), and a Dilithium3 signature. Peers validate the signature, verify device ID derivation, and store the registration in a pending pool.

When a node wins a slot and produces a block, it embeds up to 10 pending registrations from its pool directly into the block's `registrations` field. When any node accepts that block, it records each valid registration with the block's slot number as the identity's `first_seen_slot`:

```
identities[device_id]      = block.slot
identity_pubkeys[device_id] = public_key_hex
```

This value is consensus-derived from the chain. It cannot be forged, backdated, or manipulated.

**Maturation rule.** Every node enforces the following inside `_add_block_locked()`, the single function that accepts blocks into the chain:

```
if block.slot >= 1000:  # post-genesis phase
    first_seen = identities.get(winner_id)
    if first_seen is None:
        return False   # unknown identity — not registered on-chain
    if block.slot - first_seen < MIN_IDENTITY_AGE:
        return False   # identity too young
```

`MIN_IDENTITY_AGE = 200` slots (~33 minutes). A producer cannot include their own REGISTER in the same block they are winning — registrations embedded in a block are processed after the block is appended, so the maturation check always sees the state before the current block.

**Security properties.** This design provides:

- **No instant activation.** A new identity must wait 200 slots after its REGISTER is included in a block before it can produce valid blocks.
- **No bypass via P2P.** Blocks from identities whose `first_seen_slot` is too recent are rejected by every honest node, regardless of how the block was received.
- **Pruning compatibility.** Identity data lives in checkpoint state and survives the checkpoint pruning cycle indefinitely.

### 3.14 Payment URI Standard

TIMPAL defines a standard URI format for payment requests:

```
timpal:<device_id>?amount=<tmpl>&memo=<text>&label=<n>
```

The `device_id` is the recipient's 64-character hex address. `amount` is in TMPL (decimal). `memo` is an optional signed payment reference included in the transaction payload (max 128 characters). `label` is a display-only hint for the recipient's name — it is never included in the transaction or its signature.

Any node can generate a payment request URI and display it as a QR code directly in the terminal using the `receive` command:

```
> receive              — show address QR code only
> receive 4.50         — QR code with amount pre-filled
> receive 4.50 Inv-123 — QR code with amount and memo pre-filled
```

A customer scans the QR code and their wallet pre-fills the recipient address, amount, and memo automatically. This eliminates manual address entry and the risk of copy-paste errors — critical for merchant adoption.

The URI standard is fixed at the protocol level and does not change. Any wallet, mobile app, or point-of-sale system built on TIMPAL uses the same format, making QR codes interoperable across all implementations.

---



**Era 1 — Distribution (Years 0 to ~37.5)**

- Every transaction carries a fee of 0.1% of the send amount (minimum 0.0001 TMPL, maximum 0.01 TMPL), paid to the slot winner
- Nodes earn through the deterministic single-winner lottery: 1.0575 TMPL every 10 seconds
- Total of 125,000,000 TMPL minted over approximately 37.5 years
- No pre-mine, no insider allocation, no founder rewards

**Era 2 — Sustaining (Year ~37.5 onwards)**

- No new TMPL can ever be created — supply is fixed at 125,000,000 forever
- Transaction fees continue unchanged
- The fee for each 10-second slot goes to the slot winner
- The protocol is self-sustaining forever with no inflation

The transition is automatic. No upgrade required. No vote.

---

## 5. Tokenomics

| Property | Value |
|---|---|
| Total Supply | 125,000,000 TMPL |
| Decimal Places | 8 |
| Reward Per Round (Era 1) | 1.0575 TMPL |
| Round Interval | Every 10 seconds |
| Distribution Period | ~37.5 years |
| Eligible Nodes Per Slot | 1 deterministic winner per slot |
| Identity Maturation Period | 200 slots (~33 minutes) |
| Transaction Fee | 0.1% of amount (min 0.0001 TMPL, max 0.01 TMPL) → slot winner |
| Checkpoint Interval | Every 1,000 slots (~2.8 hours) |
| Pre-mine | None |
| Insider Allocation | None |
| Confirmation Depth | 5 slots (~50 seconds) |

**Verification:**
1.0575 TMPL × 6 rounds/min × 60 × 24 × 365 = 3,334,932 TMPL/year
125,000,000 ÷ 3,334,932 = 37.48 years

---

## 6. Security Model

### 6.1 Sybil Resistance

Sybil resistance operates at five independent layers:

1. **One node per device** enforced at the OS level via file lock.
2. **VRF lottery** — every active mature identity competes every slot by signing the challenge. The winner is the competitor with the lowest `sha256(compete_sig)`. An attacker controlling K of N active identities has K/N probability of winning — expected reward per identity stays constant regardless of total network size.
3. **Chain-anchored wallet creation** (post-genesis phase): every new wallet must anchor its `device_id` to a live block hash from the running network via `sha256(public_key + block_hash)`. Offline mass wallet generation is physically impossible — the chain produces block hashes at fixed speed regardless of attacker CPU.
4. **On-chain identity maturation**: every identity must register on-chain and wait 200 slots (~33 minutes) before it can produce valid blocks. This check is enforced at the consensus layer in every node — it cannot be bypassed via P2P, bootstrap, or any other path.
5. **Identity activity decay**: an identity that has not attested or produced a block for more than 8,640 slots (~24 hours) is excluded from the lottery and from the finality quorum. Dormant accumulated identities hold zero lottery power — an attacker must keep every fake identity online and attesting continuously to retain influence.

### 6.2 Double-Spend Prevention

Every node validates sender balance against the full chain before accepting any transaction. Within a single block, each sender's cumulative debit is tracked across all transactions in that block — a sender cannot include multiple transactions that collectively exceed their balance. After a chain reorganization, the balances are recomputed from the surviving chain — if a sender's block reward was on the reorged-out branch, their balance drops accordingly. Any subsequent block attempting to include their transaction will fail the balance check and reject it. Transactions expire from the mempool after 100 slots (~17 minutes) if not included in a block.

### 6.3 Quantum Resistance

Dilithium3 protects all signatures — transactions, VRF compete messages, attestations, identity registrations, and push authentication — against both classical and quantum computer attacks.

### 6.4 Winner Proof Integrity

Every block carries a `compete_sig` derived from the winner's Dilithium3 private key signing the slot challenge. Any node can independently verify the winner by confirming all six winner verification rules described in §3.5. A block missing any proof field is rejected outright.

### 6.5 Attestation Security

Each attestation is bound to a specific public key stored in `identity_pubkeys` at registration time. An attacker cannot send an attestation using another node's `device_id` with their own key — the supplied public key must match the registered key. The attesting node must also be a member of the deterministically-selected 512-node committee for that block — attestations from nodes outside the committee are rejected before signature verification. Every attestation is Dilithium3-signed over a payload that includes the block hash and slot number, preventing replay across slots or blocks.

### 6.6 Chain Integrity

Every block's `prev_hash` must equal the SHA-256 of the previous block's canonical serialization. Canonical serialization uses `json.dumps(block, sort_keys=True, separators=(",",":"))` — deterministic across all nodes regardless of platform or Python version. Integer arithmetic is used throughout to eliminate floating-point precision divergence.

### 6.7 Fork Attack Resistance

Building a heavier chain requires producing blocks at a faster rate than the honest network. Since there is no proof-of-work, the relevant resource is eligible identities. The honest network collectively selects winners at a faster rate than any single attacker with a realistic number of devices. Reorg paths require full cryptographic verification of every block in the fork — unsigned or improperly signed blocks are rejected at the reorg layer, not just at block acceptance.

### 6.8 Wallet Security

Private keys are encrypted at rest with AES-256-GCM. The encryption key is derived from the user's password via scrypt with a random salt. The plaintext private key exists only in memory while the node is running and is never written to disk in any form. The 12-word recovery phrase provides an independent recovery path derived deterministically from entropy stored offline.

### 6.9 Bootstrap Server Trust Model

The bootstrap server is a peer directory. It cannot:

- Influence the lottery (winner selection is deterministic from chain data, not from bootstrap)
- Force a node to accept an invalid block (every node verifies chain linkage, VRF, and identity maturation independently)
- Steal funds or alter balances
- Corrupt a checkpoint (every node independently recomputes balances from local chain history before accepting)
- Bypass identity maturation (enforced at the consensus layer, not at bootstrap)

A compromised bootstrap server can only deny peer introductions — nodes that already know each other continue operating without it.

---

## 7. Decentralization

TIMPAL is a protocol, not a product. Nobody owns it. Nobody controls it. The rules are in the code and the code is open.

Community bootstrap servers, community tools, and community applications are all welcome.

---

## 8. Conclusion

TIMPAL provides what the global financial system has failed to provide: a way for any person, anywhere, to hold and send value — without asking permission.

The deterministic single-winner lottery ensures fairness whether the network has 10 nodes or 10 million. Winner selection using the previous block's hash makes the lottery unpredictable until the last moment and independently verifiable by every node — exactly one valid winner per slot, no equivocation possible. Attestation-based finality provides cryptographic — not probabilistic — irreversibility, backed by Dilithium3 signatures from a supermajority of active registered identities. On-chain identity registration with consensus-enforced maturation prevents instant identity activation. Independent checkpoint balance verification means no node can corrupt the ledger's balance history without detection. Intra-block double-spend tracking closes the multi-transaction balance drain attack. The two-era model ensures the network is self-sustaining forever — first through the lottery and fees, then through fees alone.

---

**GitHub:** https://github.com/EvokiTimpal/timpal
**Website:** https://timpal.org
**Bootstrap:** bootstrap.timpal.org:7777

*This document is released into the public domain. No rights reserved.*
