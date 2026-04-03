# TIMPAL — Quantum-Resistant Money Without Masters
v4.0 — Quantum-resistant. Worldwide. Equal. Open.

Cryptocurrency promised open participation and equal access. What it delivered was mining farms, staking pools, and a playing field tilted permanently toward whoever arrived first with the most capital. TIMPAL is built on a different principle: every node has the same chance of winning, from the first day to the thousandth, with nothing but a computer and an internet connection. No mining hardware. No staking. No compounding advantage for early entrants.

It is also built for the threat the rest of the industry is pretending does not exist. Bitcoin and Ethereum wallets use ECDSA — cryptography that quantum computers will break. TIMPAL uses Dilithium3, the NIST 2024 post-quantum standard, as the only cryptographic primitive in the entire protocol. Every wallet created on TIMPAL is quantum-resistant from the first keystroke.

---

## Quick start

**Step 1 — Install dependencies:**
```
pip3 install dilithium-py cryptography pycryptodome mnemonic
```

**Step 2 — Download TIMPAL:**
```
curl -O https://raw.githubusercontent.com/EvokiTimpal/timpal/main/timpal.py
```

**Step 3 — Run your node:**
```
python3 timpal.py
```

Your node starts, creates a quantum-resistant wallet, shows you a 12-word recovery phrase you must write down, prompts you to set an encryption password, connects to the worldwide network, registers your identity on-chain, and joins the reward lottery automatically after a maturation period of ~33 minutes.

---

## Commands

| Command | What it does |
|---|---|
| `balance` | Your current TMPL balance and full wallet address |
| `chain` | Chain height, tip hash, and recent confirmed blocks |
| `peers` | Online nodes currently connected to you |
| `send` | Send TMPL to an address |
| `history` | Your transaction and reward history |
| `network` | Global network statistics |
| `quit` | Shut down your node cleanly |

---

## How it works

- **Chain-anchored distributed ledger** — Every node holds a full copy. Each reward is a block carrying a cryptographic link to the previous one — a single, unambiguous history. No proof-of-work. No mining.
- **Quantum-resistant cryptography** — Dilithium3, the NIST 2024 post-quantum standard. Every signature is future-proof against quantum computers.
- **Encrypted wallet with seed phrase recovery** — Private key encrypted with AES-256-GCM and a password you set. Never stored in plaintext. Recoverable via your 12-word BIP39 seed phrase.
- **Attestation-based cryptographic finality** — A block is final when more than 2/3 of all mature identities have signed it with Dilithium3 attestations. A finalized block cannot be reversed under any circumstances short of breaking Dilithium3.
- **On-chain identity registration** — When your node starts, it broadcasts a signed REGISTER message to peers. A block producer embeds it in the next block. Your identity is then on-chain with a verifiable `first_seen_slot`. After 200 slots (~33 minutes) your identity is mature and your node becomes eligible to compete in the lottery. This maturation rule is enforced at the consensus layer on every node — no bypass is possible via P2P or any other path.
- **Compete-based VRF lottery** — Every 10 seconds, one node wins 1.0575 TMPL. Each slot, ~10 nodes are selected deterministically using the previous block's hash — unpredictable until the previous block arrives. Each selected node signs a challenge and broadcasts a COMPETE message. The winner is the competitor whose proof hash is lowest — cryptographically fair, independently verifiable by every node.
- **Checkpoints every ~2.8 hours** — Every 1,000 slots, all nodes independently create a checkpoint. Before accepting a peer checkpoint, every node independently recomputes balances from its own chain history — a corrupted checkpoint cannot be accepted. The full identity table is preserved in every checkpoint and survives pruning. Keeps the ledger lightweight forever.
- **Fork resolution** — Heaviest valid chain wins. Weight is block count minus slot-gap penalties. Equal-weight forks resolve deterministically by tip hash. All nodes converge to the same chain regardless of arrival order.
- **One node per device** — Enforced by the protocol at the OS level. Running multiple terminals gives zero advantage.
- **125 million TMPL total supply** — Distributed over ~37.5 years. No pre-mine. No insider allocation. Zero.

---

## Tokenomics

| Property | Value |
|---|---|
| Total supply | 125,000,000 TMPL |
| Decimal places | 8 |
| Reward per round | 1.0575 TMPL |
| Round interval | 10 seconds |
| Distribution period | ~37.5 years |
| Eligible nodes per slot | ~10 (fixed target, regardless of network size) |
| Identity maturation period | 200 slots (~33 minutes) |
| Confirmation depth | 3 slots (~30 seconds) |
| Checkpoint interval | Every 1,000 slots (~2.8 hours) |
| Transaction fee | 0.1% of amount (min 0.0001 TMPL, max 0.01 TMPL) → slot winner |
| Pre-mine | None |
| Insider allocation | None |

---

## Requirements

- Python 3.8+
- Mac, Linux, Windows

---

## Quantum-resistant wallets

Every TIMPAL wallet is built on **Dilithium3** — the post-quantum digital signature standard selected by NIST in 2024. This matters because the cryptography protecting Bitcoin, Ethereum, and most existing wallets (ECDSA / secp256k1) is mathematically breakable by a sufficiently powerful quantum computer. When that threshold is crossed, any address whose public key has ever been exposed on-chain becomes vulnerable — funds can be stolen.

TIMPAL was designed from day one with this threat in mind. There is no ECDSA in the protocol anywhere. Every key pair, every transaction signature, every block signature, and every attestation uses Dilithium3. A quantum computer cannot derive your private key from your public key, your address, or anything broadcast on the network.

Your wallet is quantum-resistant from the moment it is created.

## Wallet security

On first run, TIMPAL generates a 12-word BIP39 recovery phrase and requires you to type it back in full before creating your wallet. Your private key is then encrypted with AES-256-GCM using a key derived from your password via scrypt. The plaintext private key is never written to disk.

**If you lose both your wallet file and your 12-word recovery phrase, your TMPL is gone forever.** There is no other recovery path. Write the phrase down on paper and store it somewhere safe. Never photograph it. Never put it in cloud storage. Never share it with anyone.

---

## Sending TMPL from the command line

While your node is running in one terminal, open a second terminal:

```
# Check your balance and full address
python3 timpal.py balance

# Send TMPL to another address
python3 timpal.py send <recipient_address> <amount>
```

Example:
```
python3 timpal.py send c9da12e12fcb8782dbf7660a... 10.0
```

---

## Keeping your node running

```
# Mac — prevent sleep while node is running
caffeinate -i python3 timpal.py

# Linux — run in a persistent screen session
screen -S timpal
python3 timpal.py
```

---

## Bootstrap node

`bootstrap.timpal.org:7777` — door to the network. Stores no value. Controls nothing. Not involved in transactions, rewards, or lottery operation. The network continues producing blocks even if bootstrap goes offline — all lottery and gossip traffic flows directly between peers.

---

## Whitepaper

See [WHITEPAPER.md](WHITEPAPER.md) in this repository.

---

## Decentralization

TIMPAL is a protocol, not a product. Nobody owns it. Nobody controls it. The rules are in the code and the code is open. What gets built on top of it — mobile apps, GUI clients, hardware wallets — is for the community to decide.

---

## License

MIT
