# TIMPAL — Plan B for Humanity

> *The money that works when everything else stops working.*

**Quantum-resistant. Worldwide. Instant. Free.**

TIMPAL is a peer-to-peer payment protocol. No banks. No servers. No company. No control. Just people sending value directly to each other.

---

## Quick start

**Step 1 — Install the dependency:**
```bash
pip3 install dilithium-py
```

**Step 2 — Download TIMPAL:**
```bash
curl -O https://raw.githubusercontent.com/EvokiTimpal/timpal/main/timpal.py
```

**Step 3 — Run your node:**
```bash
python3 timpal.py
```

Your node starts, creates a quantum-resistant wallet, connects to the worldwide network, and joins the reward lottery automatically.

---

## Commands

| Command   | What it does                        |
|-----------|-------------------------------------|
| `balance` | Your current TMPL balance           |
| `peers`   | Online nodes connected to you       |
| `send`    | Send TMPL to a peer                 |
| `history` | Your transaction and reward history |
| `network` | Global network statistics           |
| `quit`    | Shut down your node                 |

---

## How it works

- **Distributed ledger** — Every node holds a full copy. No single point of failure.
- **Quantum-resistant cryptography** — Dilithium3, NIST 2024 post-quantum standard.
- **Instant finality** — Transactions confirm immediately.
- **Node reward lottery** — Every 3 seconds, one random node wins 0.6345 TMPL.
- **One node per device** — Fairness enforced by the protocol.
- **250 million TMPL total supply** — Over 37.5 years. No pre-mine. No insider allocation.

---

## Tokenomics

| Property | Value |
|----------|-------|
| Total supply | 250,000,000 TMPL |
| Reward per round | 0.6345 TMPL |
| Round interval | 3 seconds |
| Distribution period | 37.5 years |
| Transaction fee | Free (first 37.5 years) |
| Pre-mine | None |
| Insider allocation | None |

---

## Requirements

- Python 3.8+
- Mac, Linux, Windows

---

## Roadmap

- [ ] Android app
- [ ] iOS app
- [ ] GUI desktop client
- [ ] Mesh network support (Bluetooth + WiFi Direct)

---

## Bootstrap node

`5.78.187.91:7777` — door to the network. Stores no value. Controls nothing.

---

## Whitepaper

See `TIMPAL_Whitepaper.docx` in this repository.

---

## License

MIT

---

*Built March 8, 2026. First transaction sent the same day.*

---

## Sending TMPL from the command line

While your node is running in one terminal, open a second terminal to send:
```bash
# Check your balance and full address
python3 timpal.py balance

# Send TMPL to another node
python3 timpal.py send <recipient_address> <amount>
```

Example:
```bash
python3 timpal.py send c9da12e12fcb8782dbf7660a... 10.0
```

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
