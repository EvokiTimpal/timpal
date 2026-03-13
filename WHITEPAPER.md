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

Double-spend prevention is enforced by checking the sender's balance against the complete ledger history before accepting any transaction. The first transaction seen by the network wins.

### 3.2 Quantum-Resistant Cryptography

TIMPAL uses Dilithium3, selected as a post-quantum digital signature standard by NIST in 2024. Every device generates a unique key pair on first launch. The private key never leaves the device. All transactions and VRF tickets are signed and verifiable.

### 3.3 Network Topology

- **Local:** UDP broadcast on port 7778 for same-network peer discovery.
- **Global:** Bootstrap server at 5.78.187.91:7777 introduces nodes worldwide. Stores no funds. Controls nothing.

Once connected, nodes communicate directly peer-to-peer. The bootstrap server is not involved in transactions or rewards.

### 3.4 VRF Reward Lottery

Every 5 seconds, one node wins 1.0575 TMPL. The winner is selected using a Verifiable Random Function (VRF):

Each node signs the current time slot with its private key. The ticket is the hash of that signature — unpredictable without the private key. The node with the lowest ticket wins.

Because the ticket is derived from each node's unique private key signature, it is different every round — no node has a permanent advantage over any other. The design scales to millions of nodes with zero coordination overhead. As the number of nodes grows, reward distribution converges to statistically equal.

### 3.5 One Node Per Device

An OS-level file lock prevents more than one node running per device. Any second attempt exits immediately. More rewards require more physical devices — the same constraint for everyone.

### 3.6 Ledger Conflict Resolution

Each five-second time slot has exactly one winner. If two nodes claim the same slot, the reward with the lowest VRF ticket is canonical — the same rule used during the live lottery. Total minted supply is recalculated from scratch after every merge.

### 3.7 Transaction Rate Limiting

Each device is limited to 60 transactions per minute. This prevents spam and flood attacks while comfortably supporting all legitimate use cases. Since one node per device is enforced at the OS level, this limit applies equally to every participant on the network.

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
- The fee goes to the node that first broadcast the transaction
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
| Fee Recipient | Node that broadcast the transaction |
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
