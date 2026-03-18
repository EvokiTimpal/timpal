# TIMPAL — Quantum-Resistant Money Without Masters

Quantum-resistant. Worldwide. Instant. Free.

TIMPAL is a peer-to-peer payment protocol. No banks. No servers. No company. No control. Just people sending value directly to each other.

---

## Quick start

**Step 1 — Install dependencies:**
```
pip3 install dilithium-py cryptography
```

**Step 2 — Download TIMPAL:**
```
curl -O https://raw.githubusercontent.com/EvokiTimpal/timpal/main/timpal.py
```

**Step 3 — Run your node:**
```
python3 timpal.py
```

Your node starts, creates a quantum-resistant wallet, prompts you to set a password to encrypt it, connects to the worldwide network, and joins the reward lottery automatically.

---

## Commands

| Command | What it does |
|---|---|
| `balance` | Your current TMPL balance |
| `peers` | Online nodes connected to you |
| `send` | Send TMPL to a peer |
| `history` | Your transaction and reward history |
| `network` | Global network statistics |
| `quit` | Shut down your node |

---

## How it works

- **Distributed ledger** — Every node holds a full copy. No single point of failure.
- **Quantum-resistant cryptography** — Dilithium3, NIST 2024 post-quantum standard.
- **Encrypted wallet** — Private key encrypted with AES-256-GCM and a password you set. Never stored in plaintext.
- **Instant finality** — Transactions confirm immediately.
- **VRF reward lottery** — Every 5 seconds, one node wins 1.0575 TMPL. Winner selected by Verifiable Random Function using each node's private key signature — provably fair, no node has a permanent advantage.
- **One node per device** — Fairness enforced by the protocol.
- **250 million TMPL total supply** — Over 37.5 years. No pre-mine. No insider allocation.

---

## Tokenomics

| Property | Value |
|---|---|
| Total supply | 250,000,000 TMPL |
| Decimal places | 8 |
| Reward per round | 1.0575 TMPL |
| Round interval | 5 seconds |
| Distribution period | 37.5 years |
| Transaction fee | Free (first 37.5 years) |
| Transaction fee (after 37.5 years) | 0.0005 TMPL |
| Fee recipient | All nodes that submitted a VRF commit for the slot (split equally) |
| Pre-mine | None |
| Insider allocation | None |

---

## Requirements

- Python 3.8+
- Mac, Linux, Windows

---

## Wallet security

On first run, TIMPAL prompts you to set a password. Your private key is encrypted with AES-256-GCM using a key derived from your password via scrypt. The plaintext private key is never written to disk.

**If you forget your password, your TMPL is gone forever.** There is no recovery. Write it down and store it somewhere safe.

If you have an existing unencrypted wallet from a previous version, TIMPAL will prompt you to encrypt it on next startup.

---

## Auto-start on boot

Run your node automatically every time your computer starts.

**Mac:**
```
bash autostart_mac.sh
```

**Linux:**
```
bash autostart_linux.sh
```

**Windows:**
```
autostart_windows.bat
```

To stop auto-start, see instructions printed after running the script.

---

## Sending TMPL from the command line

While your node is running in one terminal, open a second terminal to send:

```
# Check your balance and full address
python3 timpal.py balance

# Send TMPL to another node
python3 timpal.py send <recipient_address> <amount>
```

Example:
```
python3 timpal.py send c9da12e12fcb8782dbf7660a... 10.0
```

---

## Bootstrap node

`bootstrap.timpal.org:7777` — door to the network. Stores no value. Controls nothing.

---

## Whitepaper

See [WHITEPAPER.md](WHITEPAPER.md) in this repository.

---

## Decentralization

TIMPAL is a protocol, not a product. Nobody owns it. Nobody controls it. The rules are in the code and the code is open. What gets built on top of it is for the community to decide.

---

## License

MIT
