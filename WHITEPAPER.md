# TIMPAL — Plan B for Humanity

**The money that works when everything else stops working.**  
A Quantum-Resistant Peer-to-Peer Payment Protocol

March 8, 2026

---

## Abstract

TIMPAL is a peer-to-peer payment protocol designed to function without banks, payment processors, or centralized infrastructure. It uses quantum-resistant Dilithium3 cryptography, a distributed append-only ledger, a VRF-based node reward lottery, and a two-era economic model to create a fair, decentralized monetary system with a fixed supply of 250 million TMPL distributed over 37.5 years.

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

The protocol currently runs on Mac, Windows, and Linux computers running Python 3.8 or newer.
```
pip3 install dilithium-py
curl -O https://raw.githubusercontent.com/EvokiTimpal/timpal/main/timpal.py
python3 timpal.py
```

No configuration. No account creation. No KYC.

---

## 3. Protocol Architecture

### 3.1 Distributed Ledger

TIMPAL uses a distributed append-only ledger rather than a blockchain. Every node holds a complete copy. There are no blocks, no mining, and no proof-of-work. Transactions confirm immediately.

Double-spend prevention is enforced by checking the sender's balance before accepting any transaction. The first transaction seen by the network wins. Every two weeks, the network automatically creates a checkpoint — a cryptographically verified snapshot of all balances — and prunes the raw history before it. Nodes only need to store data since the last checkpoint, keeping the ledger lightweight forever.

### 3.2 Quantum-Resistant Cryptography

TIMPAL uses Dilithium3, selected as a post-quantum digital signature standard by NIST in 2024. Every device generates a unique key pair on first launch. The private key never leaves the device. All transactions and VRF tickets are signed and verifiable.

### 3.3 Network Topology

- **Local:** UDP broadcast on port 7778 for same-network peer discovery.
- **Global:** Bootstrap server at 5.78.187.91:7777 introduces nodes worldwide. Stores no funds. Controls nothing.

Once connected, nodes communicate directly peer-to-peer. The bootstrap server is not involved in transactions or rewards.

### 3.4 VRF Reward Lottery

Every 5 seconds, one node wins 1.0575 TMPL. The winner is selected using a Verifiable Random Function (VRF):

Each node signs the current time slot with its private key. The ticket is the hash of that signature — unpredictable without the private key. The node with the lowest ticket wins. To prevent cheating, the lottery uses a commit-reveal scheme: at the start of each slot every node submits a cryptographic commitment to the bootstrap registry. Two seconds later, all nodes reveal their actual tickets. A node cannot change its ticket after committing, and cannot selectively reveal only if it wins — the commitment binds it. Every node independently verifies all reveals and picks the same winner using identical math. The bootstrap registry stores commits and reveals but cannot influence the outcome — all verification happens on the nodes.

Because the ticket is derived from each node's unique private key signature, it is different every round — no node has a permanent advantage over any other. The design scales to millions of nodes with zero coordination overhead. As the number of nodes grows, reward distribution converges to statistically equal.

### 3.5 One Node Per Device

An OS-level file lock prevents more than one node running per device. Any second attempt exits immediately. More rewards require more physical devices — the same constraint for everyone.

### 3.6 Ledger Conflict Resolution

Each five-second time slot has exactly one winner. If two nodes claim the same slot — due to network latency or a temporary split — the reward with the lowest VRF ticket is canonical. This is the same rule used during the live lottery, so the conflict resolution is always consistent. After a checkpoint is applied, balances are calculated from the checkpoint snapshot forward rather than from the beginning of history.

### 3.7 Transaction Rate Limiting

Each device is limited to 60 transactions per minute. This prevents spam and flood attacks while comfortably supporting all legitimate use cases. Since one node per device is enforced at the OS level, this limit applies equally to every participant on the network.

### 3.8 Ledger Checkpoint System

Without checkpointing, the ledger would grow to approximately 118GB over 37.5 years, making it impractical for nodes in regions with limited storage or bandwidth.

Every 241,920 slots (two weeks), every node independently creates a checkpoint. The checkpoint records the balance of every address at that moment, the total supply minted, and SHA256 cryptographic hashes of all pruned rewards and transactions. These hashes are permanent proof that the pruned data was valid — anyone with the original data can verify the checkpoint is honest.

A 120-slot buffer (10 minutes) is applied before pruning, giving late-arriving data time to propagate across the network before being permanently removed. All rewards and transactions older than the buffer are pruned after the checkpoint is written.

Checkpoints are gossiped to peers automatically. A new node joining the network receives the latest checkpoint first, then only the data since that checkpoint — never the full history. The checkpoint system runs in a background thread and never interferes with the lottery or transactions.

The process is fully automatic and requires no human intervention. It runs identically on every node forever.

### 3.9 Protocol Version Enforcement

As TIMPAL evolves, updates may change protocol rules. Nodes running outdated versions could cause conflicts if they remain on the network after a rule-changing update.

Every node declares its version when connecting to any peer or bootstrap server. If the declared version is below the network minimum, the connection is rejected immediately with a clear message directing the operator to update from GitHub.

This follows the same model as Bitcoin: no central authority forces updates. Developers publish fixes openly. Node operators update voluntarily. Updated nodes automatically reject outdated ones. The network migrates to the new version through social consensus with no coordination required.

The minimum version is a constant defined in both timpal.py and bootstrap.py. When a rule-changing update is published, the minimum version is bumped. Nodes that do not update are naturally excluded from the network.

---

## 4. Two-Era Economic Model

**Era 1 — Distribution (Years 0 to 37.5)**

- All transactions are free
- Nodes earn through the VRF lottery: 1.0575 TMPL every 5 seconds
- Total of 250,000,000 TMPL minted over 37.5 years
- No pre-mine, no insider allocation, no founder rewards

**Era 2 — Sustaining (Year 37.5 onwards)**

- No new TMPL can ever be created — supply is fixed at 250,000,000
- Every transaction carries a fee of 0.0005 TMPL
- The fee for each 5-second slot is collected and split equally among all nodes that submitted a VRF commit for that slot — nodes that were provably active and participating in the network at that moment. At high transaction volume, routing all fees to a single winner would create a centralizing force — one lucky node capturing disproportionate value every slot. Splitting among all active participants keeps the reward structure flat and fair regardless of network size. This mechanism uses the existing commit registry infrastructure with zero additional overhead.
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

One node per device enforced at the OS level. More rewards require more physical devices — the same constraint for everyone.

### 6.2 Double-Spend Prevention

Every node validates sender balance against the full ledger before accepting any transaction.

### 6.3 Quantum Resistance

Dilithium3 protects all signatures against both classical and quantum computer attacks.

### 6.4 VRF Verification

Every reward includes a cryptographic ticket derived from the winner's private key signature. Any node can verify the winner is legitimate by confirming the ticket is the lowest value submitted for that round.

### 6.5 Bootstrap Server

Single point of failure for new node discovery only — not for network operation. Existing nodes continue peer-to-peer if bootstrap goes offline. Community-operated bootstrap servers are welcome.

---

### 6.6 Era 2 Fee Distribution

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

The two-era model ensures the network is self-sustaining forever — first through the VRF lottery, then through transaction fees. Whether there are 2 nodes or 2 million nodes, the protocol works the same way.

---

**GitHub:** https://github.com/EvokiTimpal/timpal
**Website:** https://timpal.org
**Bootstrap:** 5.78.187.91:7777

*This document is released into the public domain. No rights reserved.*
