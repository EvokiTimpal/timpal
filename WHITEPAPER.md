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

### 3.2 Quantum-Resistant Cryptography

TIMPAL uses Dilithium3, selected as a post-quantum digital signature standard by NIST in 2024. Every device generates a unique key pair on first launch. The private key never leaves the device.

### 3.3 Network Topology

- **Local:** UDP broadcast on port 7778 for same-network discovery.
- **Global:** Bootstrap server at 5.78.187.91:7777 introduces nodes worldwide. Stores no funds. Controls nothing.

### 3.4 One Node Per Device

An OS-level file lock prevents more than one node running per device. Any second attempt exits immediately. This makes the reward lottery fair for everyone.

### 3.5 Ledger Conflict Resolution

Each three-second time slot has exactly one winner. If two nodes claim the same slot, the earlier timestamp wins. Total minted supply is recalculated from scratch after every merge.

---

## 4. Tokenomics

| Property | Value |
|----------|-------|
| Total Supply | 250,000,000 TMPL |
| Reward Per Round | 0.6345 TMPL |
| Round Interval | Every 3 seconds |
| Distribution Period | 37.5 years |
| Transaction Fee (Year 0-37.5) | Free |
| Transaction Fee (After Year 37.5) | 0.0005 TMPL |
| Pre-mine | None |
| Insider Allocation | None |
```
0.6345 x 20 rounds/min x 60 x 24 x 365 = 6,669,864 TMPL/year
250,000,000 / 6,669,864 = 37.48 years
```

---

## 5. Security Model

- **Sybil Resistance:** One node per device. More rewards require more physical devices.
- **Double-Spend Prevention:** Every node validates balance before accepting any transaction.
- **Quantum Resistance:** Dilithium3 protects against both classical and quantum attacks.

---

## 6. Roadmap

- **Phase 1 — Live (March 2026):** Core protocol, quantum-resistant wallets, distributed ledger, node lottery, GitHub.
- **Phase 2 — Resilience:** Multiple bootstrap servers, offline transaction queuing.
- **Phase 3 — Mobile:** Android and iOS applications, GUI desktop client.
- **Phase 4 — Mesh:** Bluetooth and WiFi Direct — no internet required.
- **Phase 5 — Scale:** Millions of nodes, formal security audit.

---

## 7. Conclusion

TIMPAL is not a company. It is a protocol. Nobody owns it. Nobody controls it. The rules are in the code and the code is open.

- GitHub: https://github.com/EvokiTimpal/timpal
- Website: https://timpal.org
- Bootstrap: 5.78.187.91:7777

*This document is released into the public domain. No rights reserved.*
