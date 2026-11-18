# Sovereign Mathematical Synthesizer · المُركّب الرياضي السيادي

A deterministic, **non-custodial** engine that turns an Arabic intent into a
**proven** atomic execution path. It has no failure branch: when no ready-made
solution exists it *invents* one by scanning live GitHub primitives, composing
them into a single atomic path, and searching for a mathematical proof
(Z3 + CVC5) that the path satisfies the intent's constraints — including
`NetProfit > 0`.

> لا يقول النظام «لا أستطيع». بل يقول فقط: «برهان رياضي جديد لم يُكتشف بعد».

---

## How it works

```
Arabic intent
   │
   ▼
1. Formal Analysis      intent.parse      → Constraint System
   │                                         (goal, security, NetProfit > 0)
   ▼
2. Local search         composer + prover  → try ready-made compositions
   │   (none proves?)
   ▼
3. Forced Generative Loop
   a. Scan GitHub Live  scanner.py         → primitives with commit < 48h
   b. Dynamic Compose   composer.py        → new atomic path
   c. Proof Generation  prover.py          → Z3 (+CVC5) certificate
   d. Execution Gate    executor.py        → prove again, then settle
   │
   ▼
Result: a proven path (executed or ready) + Arabic explanation + equations
        If nothing proves yet → the loop keeps running 24/7 until it does.
```

### The profit equation

A composed path is a chain of hops over a shared **notional** `N` — the
uncollateralised capital a flash-mint / flash-loan primitive lends inside the
same atomic transaction:

```
NetProfit = N · Σ(y_i − f_i) − gas ,   gas = 0   (protocol-sponsored)
```

A path with no capital primitive has `N = 0` and cannot earn — the prover
pins `N` to 0 and rejects it. This is the honest form of "earn with no
capital": the constraint is physical, not a preference.

### The security posture is a system invariant

- **Non-custodial.** The engine never holds user funds.
- **No user key.** It never asks for a seed phrase, private key, or mnemonic.
  It signs with its own ephemeral session key inside the process.
- **No user gas.** Gas is always sponsored by a protocol, paymaster, or barter.
- **Locked beneficiary.** Payouts go only to
  `0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47`. Any other target is rejected
  before signing.

---

## Run it

```bash
python scripts/go_live.py          # install, start, verify — one command
# or
pip install -r requirements.txt -r requirements-eth.txt
python -m uvicorn synthesizer.api:app --host 0.0.0.0 --port 12000
```

Open `/` for the Arabic chat UI.

### API

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET`  | `/` | Arabic chat UI |
| `GET`  | `/health` | liveness |
| `GET`  | `/status` | engine + chain state |
| `POST` | `/synthesize` | `{"intent": "..."}` → full synthesis |
| `GET`  | `/primitives` | current live primitive set |
| `GET`  | `/proofs/{id}` | proof certificate + Certora spec |

---

## Deploy (real backend, 24/7)

The engine is stateless per request, so it runs anywhere.

```bash
# Fly.io
fly launch --copy-config --dockerfile Dockerfile && fly deploy

# Render
#   connect the repo; render.yaml is picked up automatically

# Vercel (serverless)
vercel --prod
```

`Dockerfile` installs Z3 + CVC5. `scripts/keep_alive.py` is the 24/7 keeper.
`.github/workflows/synthesis-loop.yml` runs the forced generative loop on a
schedule, so the search never stops even between sessions.

Set any API keys as **environment variables** in the platform dashboard — never
in the repo. All keys are optional: the engine runs keyless via Pollinations.

---

## Tests

```bash
python -m pytest tests/ -q
```

Real code paths only: the real Z3 solver, the real composer, the real ERC-4337
encoding, and the real HTTP surface.
