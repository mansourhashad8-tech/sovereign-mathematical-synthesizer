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
2. Instant synthesis    templates + composer → candidates, ranked by predicted net
   │
   ▼
3. Proof sweep          prover.py           → waves of 4, Z3 in parallel,
   │                                           CVC5 second opinion on the winner
   ▼
4. Execution gate       executor.py         → fund gas internally, sign, broadcast,
   │                                           broadcast to every bundler at once
   ▼
Result: a proven path + Arabic explanation + equations, in the same request.

Primitives (GitHub, commit < 48h), account identity, and gas price are
resolved once at process start, so the proof itself is pure in-memory work.
```

### The proof is instant; settlement is not

Proving takes milliseconds. A real settlement is network work — a gas leg, a
broadcast, a receipt — and takes seconds. Blocking the
answer on it would spend the latency contract to buy it, so the two are
separated:

```
POST /synthesize        → proof + narrative, execution.status = "pending"   (~90ms)
   background thread    → gas leg → sign → broadcast → wait for receipt
GET  /intent/{id}       → the same result, now execution.status = "executed"
                          with the real tx_hash and explorer link
```

The result object is upgraded in place, so the answer a caller already holds
gains its transaction hash. A settlement the internal treasury cannot yet fund is retried
by the watchdog and reported as `simulated` with no hash — never with an
invented one.

### The profit equation

A composed path is a chain of hops over a shared **notional** `N` — the
uncollateralised capital a flash-mint / flash-loan primitive lends inside the
same atomic transaction:

```
NetProfit = N · Σ(y_i − f_i) − gas ,   gas ≤ reserve   (self-funded)
```

A path with no capital primitive has `N = 0` and cannot earn — the prover
pins `N` to 0 and rejects it. This is the honest form of "earn with no
capital": the constraint is physical, not a preference.

### Latency is part of the contract

Version 2 makes the answer, not a promise of one. Measured on the live engine
with a warm cache:

| Intent | Decision time | Source |
| ------ | ------------- | ------ |
| `اربح 4000 دولار من السيولة` | ~60 ms | composed path, SAT |
| `حول 1000 دولار الى ايثيريوم` | ~76 ms | composed path, SAT |
| `اريد ربح مليار دولار` | ~60 ms | UNSAT bound certificate |

The design that gets there:

- **Pre-computed template library** (`synthesizer/templates.py`): proven
  compositions are instantiated, not re-derived.
- **Wave sweep** (`loop.py`): candidates are ordered by predicted net profit and
  proved four at a time; the sweep stops as soon as a wave proves.
- **CVC5 on the winner only.** The second opinion never delays the sweep.
- **One pooled HTTP client**, so chain reads reuse a connection.
- **Deterministic composition.** The RNG is derived from the constraint system
  and the primitive set is sorted, so the same intent yields the same path in
  every process.

`X-Synthesis-Ms`, `X-Synthesis-Source`, and `X-Synthesis-Background: false` are
returned on every `/synthesize` response, so the latency claim is auditable
rather than asserted.

### What "no failure" means, precisely

The engine has no refusal branch. Every intent is answered, in the same
request, in one of two ways:

1. **A proven path.** Z3 finds a witness for `NetProfit >= target` with
   `custody = false`, `user_key = false`, `gas_sponsor != user`, and the locked
   beneficiary. CVC5 confirms it independently.
2. **A bound certificate.** When no path can satisfy the target, the prover
   returns UNSAT over the maximum reachable notional
   (`MAX_NOTIONAL_USD`, a physical cap set by real flash-liquidity depth). The
   certificate states the exact ceiling and the shortfall. This is a proof, not
   an apology.

A target that exceeds the physical ceiling gets an honest mathematical bound,
not a fabricated transaction. The engine never claims money moved when it did
not: `execution.status` is `executed` only when a bundler accepted the signed
operation and returned a hash.

### Settlement: what works keyless today

The execution path is real ERC-4337. It derives the counterfactual
SimpleAccount address from the on-chain factory, signs a v0.6 UserOperation with
its own ephemeral session key, and fires `eth_sendUserOperation` at every
configured bundler in parallel.

### Gas is internal, keyless, and proven on chain

Gas is not asked for. The engine funds its own settlements from resources it
holds, and an external paymaster is a switch that is **off by default**
(`SYNTH_ALLOW_EXTERNAL_SPONSOR=false`). The ladder is tried in this order, and
each rung is measured live rather than assumed:

| # | leg | what it needs |
|---|---|---|
| 1 | `native` | the session EOA holds enough native coin |
| 2 | `deposit` | a funded EntryPoint deposit covers the operation |
| 3 | `reinvest` | the lock's earned reserve covers it |
| 4 | `flash` | an Aave V3 pool is deployed to borrow the fee leg |
| 5 | `token` | a paymaster that accepts the value token as gas exists |
| — | `external sponsor` | only when `SYNTH_ALLOW_EXTERNAL_SPONSOR=true` |

The profit-reinvestment loop is what makes rung 3 real. When a settlement lands,
`GasFlywheel.record_settlement_gas` records the gas it spent and credits the
reserve slice (`SYNTH_GAS_RESERVE_BPS`, 5% by default) back into
`ReserveAccumulator`. The next settlement draws on gas the engine already
earned, so each success widens the next one's headroom:

```
reserve' = reserve + floor(net_profit_units · reserve_bps / 10_000)
```

Measured on the local chain: a settlement mines with `gas_paid_by=self_deposit`,
spends `34_410_000_000_000` wei, and credits exactly `500_000_000_000_000_000`
units of reserve on a 10 USDC profit — the loop closes, with no sponsor in it.

Networks are ordered **cheapest-first by live `eth_gasPrice`**, so settlement is
a price decision rather than a configuration order. A chain that will not answer
sorts last instead of disappearing.

The honest limit, measured on 2026-11-18: every leg is ultimately denominated in
one native unit, because the chain charges its fee before contract code runs. A
flash loan cannot pay the fee for opening itself, and no chain with a zero gas
price exists (the cheapest measured, Gnosis Chiado, is 7 wei but rejects a
zero-balance sender). That single unit is tracked as an explicit
`bootstrap_wei` in `/status`, not hidden behind a failed broadcast.

Gas is internal; **value is not**. A transfer moves funds only when the smart
account holds them, which is checked against the node before the operation is
built. On an unfunded account the settlement still executes and still proves the
gas path, and it reports `value_transferred: false` rather than implying a
payout that did not happen.

### The security posture is a system invariant

- **Non-custodial.** The engine never holds user funds.
- **No user key.** It never asks for a seed phrase, private key, or mnemonic.
  It signs with its own ephemeral session key inside the process.
- **No user gas.** Gas is funded internally: self balance, EntryPoint deposit,
earned reserve, flash loan, or token gas. An external sponsor is opt-in only.
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
| `POST` | `/primitives/refresh` | force a live rescan (after a quota window clears) |
| `GET`  | `/proofs/{id}` | proof certificate + Certora spec |
| `GET`  | `/templates` | the pre-computed composition library |

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

### Where the numbers come from

Every surface reports the same figure, and it is the one the solver proved.
The solver picks concrete rates inside each hop's band; the path adopts those
values plus the witness notional and the certified net profit
(`AtomicPath.proven_net_profit_usd`). The narrative, the executor's settlement
amount, the API payload and the UI footer all read that single value, so they
cannot drift apart.

Two things the engine will not do:

- It will not report a ceiling of zero for a market it never measured. When no
  admissible composition exists, `prove_bound` returns `detail="no_path"` and a
  certificate that says so; a real ceiling is computed from the best
  composition the current primitive set offers.
- It will not hide a degraded scan. GitHub's search quota is 30 requests per
  minute, so `/status` exposes `scan_rate_limited`, and the narrative states
  that the engine fell back to its pinned primitive set instead of pretending
  the live scan succeeded.
