# TIMPAL — Plan B for Humanity

> *The money that works when everything else stops working.*

**A Quantum-Resistant Peer-to-Peer Payment Protocol**

*March 8, 2026*

---

## Abstract

TIMPAL is a peer-to-peer payment protocol designed to function without banks, payment processors, or centralized infrastructure. It uses quantum-resistant Dilithium3 cryptography, a distributed append-only ledger, and a time-based node reward lottery to create a fair, decentralized monetary system with a fixed supply of 250 million TMPL distributed over 37.5 years.

The protocol enforces one node per physical device, preventing Sybil attacks and ensuring that participation in the reward system remains fair regardless of computational resources. Transactions are free and confirm instantly. No pre-mine. No insider allocation. No central authority.

---

## 1. The Problem

The global financial system excludes billions of people. A person in Manila, Nairobi, or Caracas with a $50 smartphone has no reliable access to the basic tools of financial participation: the ability to send value to another person instantly, for free, without asking permission.

Existing solutions fail in predictable ways:

- **Traditional banking** requires physical infrastructure, government ID, and credit history that billions of people do not have.
- **Existing cryptocurrencies** require mining hardware, technical knowledge, or exposure to extreme price volatility.
- **Mobile money systems** are controlled by corporations and telecommunications companies that can freeze accounts, charge fees, or cease operations.
- **All of the above fail** during infrastructure outages, natural disasters, or political instability — precisely when people need them most.

TIMPAL is built to work when everything else stops working.

---

## 2. The Solution

TIMPAL provides a simple protocol: run the software, earn rewards, send TMPL to anyone on the network instantly and for free. No registration. No bank account. No hardware beyond the device you already own.

The protocol currently runs on Mac, Windows, and Linux computers running Python 3.8 or newer. The software creates a quantum-resistant cryptographic identity for the device, connects to the worldwide peer network, and immediately begins participating in the reward lottery. Mobile and embedded device support is on the roadmap.

The entire onboarding process takes under two minutes:

```bash
pip3 install dilithium-py
curl -O https://raw.githubusercontent.com/EvokiTimpal/timpal/main/timpal.py
python3 timpal.py
```

That is the complete installation. No configuration. No account creation. No KYC.

---

## 3. Protocol Architecture

### 3.1 Distributed Ledger

TIMPAL uses a distributed append-only ledger rather than a blockchain. Every node holds a complete copy of the ledger. There are no blocks, no mining, and no proof-of-work. Transactions are verified against the ledger at the moment of submission and confirmed immediately.

Double-spend prevention is enforced by checking the sender's balance against the complete ledger history before accepting any transaction. The first transaction seen by the network wins. Conflicting transactions from disconnected nodes are resolved by timestamp — the earlier transaction is canonical.

### 3.2 Quantum-Resistant Cryptography

TIMPAL uses the Dilithium3 algorithm for all cryptographic identity and transaction signing. Dilithium3 was selected as a post-quantum digital signature standard by NIST in 2024. It is resistant to attacks from both classical and quantum computers.

Every device that runs TIMPAL generates a unique Dilithium3 key pair on first launch. The public key becomes the device's identity on the network. The private key never leaves the device. Transactions are signed with the private key and verified by any node using the public key.

The choice of Dilithium3 over classical algorithms such as Ed25519 or ECDSA is deliberate. Cryptographically relevant quantum computers are expected within 10 to 15 years. A payment protocol designed to last 37.5 years must be quantum-resistant from day one.

### 3.3 Network Topology

The TIMPAL network uses a hybrid peer discovery model:

- **Local network:** UDP broadcast on port 7778 allows nodes on the same WiFi network to find each other without any external dependency.
- **Global network:** A bootstrap server at `5.78.187.91:7777` introduces nodes to peers worldwide. The bootstrap server stores no funds and controls nothing. It is a directory, not a bank.

Once two nodes are connected, they communicate directly peer-to-peer. The bootstrap server is not involved in transactions. If the bootstrap server goes offline, nodes already connected continue operating. New nodes must wait for the bootstrap to return or connect via local network discovery.

### 3.4 One Node Per Device

A critical design requirement is fairness in the reward lottery. A naive implementation would allow a single operator to run hundreds of nodes on one machine and win the majority of rewards, undermining the egalitarian premise of the protocol.

TIMPAL enforces one node per physical device using two mechanisms working together. First, the wallet is stored in the user's home directory rather than the working directory, so all instances on the same machine share the same cryptographic identity. Second, an OS-level file lock prevents more than one instance from running simultaneously. Any attempt to start a second node on the same device displays an error and exits immediately.

### 3.5 Ledger Conflict Resolution

When nodes that have been disconnected reconnect and merge their ledgers, conflicts can arise. TIMPAL resolves these conflicts with deterministic rules:

- **For transactions:** any transaction with a valid sender balance that has not been seen before is accepted. If two transactions conflict, the transaction with the earlier timestamp is accepted.
- **For rewards:** each three-second time slot can have exactly one winner. If two nodes claim the same time slot, the reward with the earlier timestamp is canonical. The later claim is discarded. Total minted supply is recalculated from scratch after every merge.

These rules ensure that regardless of network partitions, reconnections, or node failures, the ledger converges to a consistent state with no double-minting.

---

## 4. Tokenomics

| Property | Value |
|----------|-------|
| Total Supply | 250,000,000 TMPL |
| Reward Per Round | 0.6345 TMPL |
| Round Interval | Every 3 seconds |
| Distribution Period | 37.5 years |
| Transaction Fee (Year 0–37.5) | Free |
| Transaction Fee (After Year 37.5) | 0.0005 TMPL |
| Pre-mine | None |
| Insider Allocation | None |
| Founder Allocation | None |

### 4.1 Supply Calculation

The total supply of 250,000,000 TMPL is distributed through the node reward lottery at a rate of 0.6345 TMPL every 3 seconds:

```
0.6345 × 20 rounds/min × 60 min/hr × 24 hr/day × 365 days = 6,669,864 TMPL/year
250,000,000 ÷ 6,669,864 = 37.48 years
```

The 37.5-year distribution period was chosen deliberately. It is long enough that no individual or organization can accumulate a dominant position quickly, and short enough that the distribution completes within a human lifetime.

### 4.2 Post-Distribution Economy

After the 37.5-year distribution period, the total supply is fixed at 250,000,000 TMPL. No new TMPL can ever be created. The network sustains node operators through transaction fees of 0.0005 TMPL per transaction.

### 4.3 No Pre-mine

There is no pre-mine. There is no founder allocation. There is no investor allocation. The protocol launched on March 8, 2026 with zero pre-distributed coins.

---

## 5. Security Model

### 5.1 Sybil Resistance

The one-node-per-device enforcement provides Sybil resistance at the application layer. An attacker who wishes to increase their share of rewards must acquire additional physical devices — the same constraint that applies to every other participant.

### 5.2 Double-Spend Prevention

Every node validates the sender's balance against the complete ledger before accepting a transaction. A transaction that would result in a negative balance is rejected.

### 5.3 Quantum Resistance

All signatures use Dilithium3. An attacker with a cryptographically relevant quantum computer cannot forge signatures or impersonate another node. This protection extends to both transaction signing and node identity.

### 5.4 Bootstrap Server

The bootstrap server is a single point of failure for new node discovery but not for network operation. Existing connected nodes continue operating if the bootstrap goes offline. Multiple community-operated bootstrap servers are encouraged and planned.

---

## 6. Roadmap

- **Phase 1 — Live (March 2026):** Core protocol, quantum-resistant wallets, distributed ledger, node reward lottery, one-node-per-device enforcement, bootstrap server, GitHub repository.
- **Phase 2 — Resilience:** Multiple independent bootstrap servers, ledger synchronization hardening, offline transaction queuing.
- **Phase 3 — Mobile:** Android and iOS applications, simplified mobile onboarding, GUI desktop client.
- **Phase 4 — Mesh Networking:** Bluetooth peer discovery, WiFi Direct support — enabling TIMPAL to operate without internet infrastructure entirely.
- **Phase 5 — Scale:** Performance optimization for millions of nodes, formal security audit.

---

## 7. Conclusion

TIMPAL is not a company. It is not a product. It is a protocol — in the same sense that TCP/IP and HTTP are protocols. Nobody owns it. Nobody controls it. The rules are written in the code and the code is open.

The people who need this most are the ones who have been excluded from the financial system their entire lives. TIMPAL is built for them.

**The network is open to anyone.**

- GitHub: [github.com/EvokiTimpal/timpal](https://github.com/EvokiTimpal/timpal)
- Website: [timpal.org](https://timpal.org)
- Bootstrap: `5.78.187.91:7777`

---

*This document is released into the public domain. No rights reserved.*
