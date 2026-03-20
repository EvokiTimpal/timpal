# TIMPAL — Quantum-Resistant Money Without Masters

A Quantum-Resistant Peer-to-Peer Payment Protocol

March 2026 — v2.2

---

## Abstract

TIMPAL is a peer-to-peer payment protocol designed to function without banks, payment processors, or centralized infrastructure. It uses quantum-resistant Dilithium3 cryptography, a distributed append-only ledger, an eligibility-gated commit-reveal VRF lottery, and a two-era economic model to create a fair, decentralized monetary system with a fixed supply of 250 million TMPL distributed over 37.5 years.

The protocol enforces one node per physical device, preventing Sybil attacks and ensuring that participation in the reward system remains fair regardless of computational resources. Transactions are free and confirm instantly. No pre-mine. No insider allocation. No central authority.

---

## 1. The Problem

The global financial system excludes billions of people. A person in Manila, Nairobi, or Caracas with a smartphone has no reliable access to the basic tools of financial participation: the ability to send value to another person instantly, for free, without asking permission.

Existing solutions fail in predictable ways:

- Traditional banking requires physical infrastructure, government ID, and credit history that billions of people do not have.
- Existing cryptocurrencies require mining hardware, technical knowledge, or exposure to extreme price volatility.
- Mobile money systems are controlled by corporations that can freeze accounts, charge fees, or cease operations.
- All of the above fail during infrastructure outages, natural disasters, or political instability.

TIMPAL is built to work when everything else stops working.

---

## 2. The Solution

TIMPAL provides a simple protocol: run the software, earn rewards, send TMPL to anyone on the network instantly and for free. No registration. No bank account. No hardware beyond the device you already own.

The protocol runs on Mac, Windows, and Linux computers running Python 3.8 or newer.

```
pip3 install dilithium-py cryptography
curl -O https://raw.githubusercontent.com/EvokiTimpal/timpal/main/timpal.py
python3 timpal.py
```

No configuration. No account creation. No KYC.

---

## 3. Protocol Architecture

### 3.1 Distributed Ledger

TIMPAL uses a distributed append-only ledger rather than a blockchain. Every node holds a complete copy. There are no blocks, no mining, and no proof-of-work. Transactions confirm immediately.

Double-spend prevention is enforced by checking the sender's balance against the full ledger before accepting any transaction. The first transaction seen by the network wins. Every two weeks the network automatically creates a checkpoint — a cryptographically verified snapshot of all balances — and prunes the raw history before it. Nodes only need to store data since the last checkpoint, keeping the ledger lightweight forever.

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

Once connected, nodes communicate directly peer-to-peer. The bootstrap server is not involved in transactions or rewards. Community-operated bootstrap servers are welcome — the more servers, the more resilient the network.

### 3.5 Eligibility-Gated Commit-Reveal VRF Lottery

Every 5 seconds, one node wins 1.0575 TMPL. The lottery uses an eligibility gate and a commit-reveal scheme to ensure fairness at any network size.

**Eligibility gate.** Before each slot, every node independently checks whether it is eligible to participate. The check uses a deterministic hash of the node's device ID and the slot number:

```
eligible = sha256(f"{device_id}:{slot}") < threshold × 2²⁵⁶
```

The threshold targets approximately 10 eligible nodes per slot regardless of total network size. At 100 nodes the threshold is 1.0 (everyone is eligible). At 1,000,000 nodes the threshold is 0.00001 (roughly 10 nodes are eligible). Bootstrap and every node use the identical formula, so eligibility is deterministic and independently verifiable. Sybil attacks become economically irrational: multiplying device count multiplies cost, but expected reward per device remains constant because the eligible fraction shrinks proportionally.

**Commit phase (t=0.0).** Each eligible node signs the current slot number with its Dilithium3 private key. The VRF ticket is the SHA256 hash of that signature — unique per node per slot, unpredictable without the private key. The node submits a SHA256 commitment:

```
commit = sha256(f"{ticket}:{device_id}:{slot}")
```

The bootstrap server records the commit and responds with COMMIT_ACK or COMMIT_REJECTED (if the node is banned — see §3.6). A node that does not receive COMMIT_ACK does not proceed.

**Reveal phase (t=2.0).** Each committed node submits its actual ticket, signature, seed, and public key to the bootstrap server. The bootstrap server records it. A node that committed but does not reveal within the window is recorded as a missed reveal.

**Winner selection (t=4.0–4.5).** Every eligible node fetches all reveals from bootstrap and independently computes the collective target:

```
target = sha256(sorted_tickets_joined_with_colon)
```

This value cannot be known until the reveal window closes — no node can predict it in advance or cherry-pick whether to reveal based on whether it wins. The winner is the node whose ticket is closest to the target by integer distance, with device ID as a tiebreaker:

```
winner = min(verified_nodes, key=lambda d: (|ticket_int - target_int|, device_id))
```

Every node independently verifies all reveals using the committed hashes and Dilithium3 signatures, then picks the same winner using identical math. The bootstrap server stores commits and reveals but cannot influence the outcome — all verification and winner selection happens on the nodes.

**Push and gossip.** The node that computed the winner claims the reward, adds it to its ledger, and broadcasts it to the peer-to-peer network. All nodes verify the reward against their local commits and the same collective target before accepting it. The first valid reward for any slot is final — subsequent claims for the same slot are rejected.

### 3.6 Reveal Obligation Enforcement

Selective reveal is a potential attack: a node commits in every slot, then only reveals when it has computed that it would win, gaining information about the outcome before deciding whether to participate. This is prevented by the reveal obligation.

Any node that commits but does not reveal is recorded as a missed reveal. After two consecutive missed reveals, the node is banned from committing for 10 slots. During a ban, the bootstrap server rejects all commit submissions from that device ID, and the node skips those slots entirely. One missed reveal is forgiven as a legitimate network hiccup. Two consecutive misses trigger the ban.

The ban counter resets to zero after a ban is served. This makes selective reveal economically unattractive: the expected gain from cherry-picking a winning slot does not outweigh the cost of the 10-slot ban that follows.

### 3.7 One Node Per Device

An OS-level file lock prevents more than one node running per device. Any second attempt exits immediately. More rewards require more physical devices — the same constraint for everyone.

### 3.8 Ledger Conflict Resolution

Each five-second time slot has exactly one winner. If two nodes claim the same slot — due to network latency or a temporary partition — the first valid reward received is canonical. All subsequent claims for the same slot are rejected. This is consistent with the lottery design: all honest nodes independently compute the same winner, so the first arriving reward is always the correct one. A node that arrives later with the same winner is simply redundant; a node that arrives later with a different winner has either computed incorrectly or is acting maliciously.

### 3.9 Transaction Rate Limiting

Each device is limited to 60 transactions per minute. This prevents spam and flood attacks while comfortably supporting all legitimate use cases. Since one node per device is enforced at the OS level, this limit applies equally to every participant on the network.

### 3.10 Ledger Checkpoint System

Without checkpointing, the ledger would grow to approximately 118GB over 37.5 years, making it impractical for nodes in regions with limited storage or bandwidth.

Every 241,920 slots (approximately two weeks), every node independently creates a checkpoint. The checkpoint records the balance of every address at that moment, the total supply minted, and SHA256 cryptographic hashes of all pruned rewards and transactions. These hashes are permanent proof that the pruned data was valid — anyone with the original data can verify the checkpoint is honest.

A 120-slot buffer (10 minutes) is applied before pruning, giving late-arriving data time to propagate across the network before being permanently removed. All rewards and transactions older than the buffer are pruned after the checkpoint is written.

Checkpoints are gossiped to peers automatically. A new node joining the network receives the latest checkpoint first, then only the data since that checkpoint — never the full history. The checkpoint system runs in a background thread and never interferes with the lottery or transactions.

The process is fully automatic and requires no human intervention. It runs identically on every node forever.

### 3.11 Protocol Version Enforcement

Every node declares its version when connecting to any peer or bootstrap server. If the declared version is below the network minimum, the connection is rejected immediately with a clear message directing the operator to update from GitHub.

This follows the same model as Bitcoin: no central authority forces updates. Developers publish fixes openly. Node operators update voluntarily. Updated nodes automatically reject outdated ones. The network migrates to the new version through social consensus with no coordination required.

The minimum version is a constant defined in both timpal.py and bootstrap.py. When a rule-changing update is published, the minimum version is bumped. Nodes that do not update are naturally excluded from the network.

### 3.12 Push Authentication

Every node periodically pushes its ledger data to the explorer API at timpal.org. There is no shared secret. Instead, each push is signed with the node's Dilithium3 private key. The API verifies that the signature is valid and that `sha256(public_key) == device_id` before accepting any data. A node cannot impersonate another node's push — it would require the target's private key.

---

## 4. Two-Era Economic Model

**Era 1 — Distribution (Years 0 to 37.5)**

- All transactions are free
- Nodes earn through the eligibility-gated VRF lottery: 1.0575 TMPL every 5 seconds
- Total of 250,000,000 TMPL minted over 37.5 years
- No pre-mine, no insider allocation, no founder rewards

**Era 2 — Sustaining (Year 37.5 onwards)**

- No new TMPL can ever be created — supply is fixed at 250,000,000
- Every transaction carries a fee of 0.0005 TMPL
- The fee for each 5-second slot is collected and split equally among all nodes that submitted a VRF commit for that slot — nodes that were provably online and participating at that moment. Routing all fees to a single winner would create a centralizing force. Splitting among all active participants keeps the reward structure flat and fair regardless of network size. This mechanism uses the existing commit registry infrastructure with zero additional overhead.
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
| Eligible Nodes Per Slot | ~10 (scales with network size) |
| Transaction Fee (Era 1) | Free |
| Transaction Fee (Era 2) | 0.0005 TMPL |
| Fee Recipient | All nodes that submitted a VRF commit for the slot (split equally) |
| Pre-mine | None |
| Insider Allocation | None |

**Verification:**
1.0575 × 12 rounds/min × 60 × 24 × 365 = 6,669,864 TMPL/year
250,000,000 ÷ 6,669,864 = 37.48 years

---

## 6. Security Model

### 6.1 Sybil Resistance

One node per device enforced at the OS level. Additionally, the eligibility gate scales the participation threshold inversely with network size — multiplying device count multiplies infrastructure cost while expected reward per device stays constant. There is no economic incentive to run many nodes.

### 6.2 Double-Spend Prevention

Every node validates sender balance against the full ledger before accepting any transaction. The first valid transaction spending a given balance is canonical. All nodes independently enforce this rule.

### 6.3 Quantum Resistance

Dilithium3 protects all signatures — transactions, VRF tickets, and push authentication — against both classical and quantum computer attacks.

### 6.4 VRF Integrity

Every reward carries a cryptographic VRF ticket derived from the winner's Dilithium3 private key signature. Any node can independently verify the winner by confirming:

1. The committed hash matches: `sha256(ticket:device_id:slot) == commit`
2. The Dilithium3 signature is valid for the slot seed
3. The ticket matches: `sha256(signature) == ticket`
4. The ticket is the closest to the collective target among all verified participants

All four checks must pass. A reward missing any VRF field is rejected outright — there is no fallback path that accepts unverified rewards.

### 6.5 Selective Reveal Prevention

The collective target is the SHA256 of all tickets sorted and joined. It cannot be known until the reveal window closes. A node that commits cannot predict whether its ticket will win, so there is no information advantage to committing early and revealing selectively. Nodes that commit but do not reveal accumulate missed-reveal counts and are banned for 10 slots after two consecutive misses.

### 6.6 Wallet Security

Private keys are encrypted at rest with AES-256-GCM. The encryption key is derived from the user's password via scrypt with a random salt. The plaintext private key exists only in memory while the node is running and is never written to disk in any form.

### 6.7 Bootstrap Server Trust Model

The bootstrap server is a relay. It records commits and reveals but cannot verify Dilithium3 signatures — that is done on every node independently. A compromised or malicious bootstrap server can:

- Refuse to record commits (causing nodes to skip slots)
- Selectively withhold reveals (causing incorrect winner selection for nodes that query only that server)

It cannot:

- Forge a valid VRF ticket (requires the target node's private key)
- Force a node to accept an invalid reward (every node verifies independently)
- Steal funds or alter balances

Community-operated bootstrap servers reduce the impact of any single server failing or misbehaving. The more servers, the more resilient the network.

### 6.8 Era 2 Fee Distribution

In Era 2, fee distribution is designed to resist centralization. Routing all transaction fees to the slot winner would mean one node captures all fee income every 5 seconds — a structural advantage for well-connected or high-uptime nodes that compounds over time.

Instead, fees collected in each slot are split equally among all nodes that submitted a VRF commit for that slot. A VRF commit is cryptographic proof that a node was online and participating — it cannot be faked or submitted retroactively. The commit registry already exists for the lottery and requires no additional infrastructure.

The result: fee income scales with uptime, not luck. Any node running continuously earns a consistent share of network fees proportional to its participation, with no single node able to dominate.

---

## 7. Decentralization

TIMPAL is a protocol, not a product. Nobody owns it. Nobody controls it. The rules are in the code and the code is open.

The core protocol is complete. What gets built on top of it — mobile apps, GUI clients, exchanges, mesh networking, hardware wallets — is for the community to decide and build. The protocol does not depend on any of these things to function. It works today, as-is, for anyone with a computer and an internet connection.

Community bootstrap servers, community tools, and community applications are all welcome.

---

## 8. Conclusion

TIMPAL provides what the global financial system has failed to provide: a way for any person, anywhere, to hold and send value — instantly, for free, without asking permission.

The eligibility-gated lottery ensures the reward system remains fair and efficient whether the network has 10 nodes or 10 million. The collective target prevents any node from predicting or manipulating the outcome. The reveal obligation closes the selective-reveal attack. The two-era model ensures the network is self-sustaining forever — first through the lottery, then through transaction fees.

---

**GitHub:** https://github.com/EvokiTimpal/timpal
**Website:** https://timpal.org
**Bootstrap:** bootstrap.timpal.org:7777

*This document is released into the public domain. No rights reserved.*
