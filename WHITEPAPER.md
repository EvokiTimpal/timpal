# TIMPAL — Plan B for Humanity

> *The money that works when everything else stops working.*

**A Quantum-Resistant Peer-to-Peer Payment Protocol**

*March 8, 2026*

---

## Abstract

TIMPAL is a peer-to-peer payment protocol designed to function without banks, payment processors, or centralized infrastructure. It uses quantum-resistant Dilithium3 cryptography, a distributed append-only ledger, and a VRF-based node reward lottery to create a fair, decentralized monetary system with a fixed supply of 250 million TMPL distributed over 37.5 years.

The protocol enforces one node per physical device, preventing Sybil attacks and ensuring that participation in the reward system remains fair regardless of computational resources. Transactions are free and confirm instantly. No pre-mine. No insider allocation. No central authority.

---

## 1. The Problem

The global financial system excludes billions of people. A person in Manila, Nairobi, or Caracas with a smartphone has no reliable access to the basic tools of financial participation: the ability to send value to another person instantly, for free, without asking permission.

Existing solutions fail in predictable ways:

- **Traditional banking** requires physical infrastructure, government ID, and credit history that billions of people do not have.
- **Existing cryptocurrencies** require mining hardware, technical knowledge, or exposure to extreme price volatility.
- **Mobile money systems** are controlled by corporations that can freeze accounts, charge fees, or cease operations.
- **All of the above fail** during infrastructure outages, natural disasters, or political instability.

TIMPAL is built to work when everything else stops working.

---

## 2. The Solution

TIMPAL provides a simple protocol: run the software, earn rewards, send TMPL to anyone on the network instantly and for free. No registration. No bank account. No hardware beyond the device you already own.

The protocol currently runs on Mac, Windows, and Linux computers running Python 3.8 or newer. Mobile and embedded device support is on the roadmap.
```bash
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
- **Global:** Bootstrap server at `5.78.187.91:7777` introduces nodes worldwide. Stores no funds. Controls nothing.

Once connected, nodes communicate directly peer-to-peer. The bootstrap server is not involved in transactions or rewards.

### 3.4 VRF Reward Lottery

Every 3 seconds, one node wins 0.6345 TMPL. The winner is selected using a Verifiable Random Function (VRF):

1. Each node computes its ticket: `SHA256(device_id + time_slot)`
2. Each node broadcasts its ticket to peers
3. The node with the **lowest ticket value** wins
4. The winner broadcasts the reward with the ticket as cryptographic proof
5. Every node independently verifies the ticket before accepting the reward

This design scales to millions of nodes because no peer list is required. Every node computes its own ticket independently. The winner is determined by pure mathematics — not by voting, not by coordination, not by any central authority. Any node can verify any reward by recomputing the ticket from the winner's device ID and the time slot.

### 3.5 One Node Per Device

An OS-level file lock prevents more than one node running per device. Any second attempt exits immediately. This makes the VRF lottery fair — more rewards require more physical devices, not more processes.

### 3.6 Ledger Conflict Resolution

Each three-second time slot has exactly one winner. If two nodes claim the same slot, the reward with the earlier timestamp is canonical. The later claim is discarded. Total minted supply is recalculated from scratch after every merge — never trusted from incremental additions.

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
```
0.6345 × 20 rounds/min × 60 × 24 × 365 = 6,669,864 TMPL/year
250,000,000 ÷ 6,669,864 = 37.48 years
```

After distribution completes, supply is fixed forever. Transaction fees sustain node operators indefinitely.

---

## 5. Security Model

### 5.1 Sybil Resistance
One node per device enforced at the OS level. More rewards require more physical devices — the same constraint for everyone.

### 5.2 Double-Spend Prevention
Every node validates sender balance against the full ledger before accepting any transaction.

### 5.3 Quantum Resistance
Dilithium3 protects all signatures against both classical and quantum computer attacks.

### 5.4 VRF Verification
Every reward includes a cryptographic ticket. Any node can verify the winner is legitimate by recomputing `SHA256(device_id + time_slot)` and confirming it is the lowest value seen for that slot.

### 5.5 Bootstrap Server
Single point of failure for new node discovery only — not for network operation. Existing nodes continue peer-to-peer if bootstrap goes offline. Multiple community bootstrap servers are planned.

---

## 6. Roadmap

- **Phase 1 — Live (March 2026):** Core protocol, quantum-resistant wallets, distributed ledger, VRF lottery, one-node-per-device, bootstrap server, GitHub.
- **Phase 2 — Resilience:** Multiple independent bootstrap servers, offline transaction queuing.
- **Phase 3 — Mobile:** Android and iOS applications, GUI desktop client.
- **Phase 4 — Mesh:** Bluetooth and WiFi Direct — no internet required.
- **Phase 5 — Scale:** Millions of nodes, formal security audit, exchange API.

---

## 7. Conclusion

TIMPAL is not a company. It is a protocol. Nobody owns it. Nobody controls it. The rules are in the code and the code is open.

The VRF lottery ensures that whether there are 2 nodes or 2 million nodes on the network, every participant has a fair, cryptographically provable chance of winning each round. No mining rigs. No staking pools. Just run the software.

- GitHub: https://github.com/EvokiTimpal/timpal
- Website: https://timpal.org
- Bootstrap: `5.78.187.91:7777`

---

*This document is released into the public domain. No rights reserved.*
