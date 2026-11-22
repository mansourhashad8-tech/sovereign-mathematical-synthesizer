# Memory — sovereign-intent-mesh / commerce engine

Repo: `mansourhashad8-tech/sovereign-mathematical-synthesizer`, branch
`sovereign-mathematical-synthesizer`. Live engine (runtime URL, port 12000):
`https://work-1-dmmqjcnqawqsycvn.prod-runtime.all-hands.dev/`.
Pages: `https://mansourhashad8-tech.github.io/sovereign-mathematical-synthesizer/`.
Arabic NL intent -> formal constraints -> live primitive composition -> Z3/CVC5
proof -> execution. Non-custodial; beneficiary locked to
`0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47`. 112 tests pass.

## ZK settlement — the leg that completes without gas (`0164c97`,`bd5ceb3`,`6fbfc75`)
Fork exec (py-evm, `base_fee=0`) -> measured balance delta -> Groth16 proof -> verifier
inside the fork -> recorded settlement. Contract reads `delta`/`netProfit` from the proof,
never the caller. Three bugs proved locally yet failed on chain: **degenerate powers of
tau**, **stale `Verifier.sol`** (4 signals vs 6), **G2 coordinate order** (never hand-encode
points; use `zkey export soliditycalldata`).
**Honesty is load-bearing, not the cryptography.** The fork mints its own capital, so a
valid proof says nothing about value: `money_is_real=False`, `value_unit=FSU`, capital
capped at 1,000 FSU, and a floor above capacity records **no** settlement.

## Hard rules learned (do not re-litigate)
- Security flags (no custody / no user key / no user gas) are **system invariants**, never
  user preferences; the intent parser hardcodes them.
- A path with no flash primitive pins `N=0` and cannot earn.
- Profit model: `NetProfit = N·Σ(y_i−f_i) − gas`, `gas=0` (internal).
- When a solver returns a witness, **the witness is the truth**; a value recomputed from
  declared inputs is a different number wearing the same name.
- A receipt is not a payout: the double gate needs mined **and** a measured positive
  beneficiary delta.
- CVC5 pip wheel works (the binary does not); `mkReal` rejects scientific notation — format
  as `f"{v:.12f}".rstrip('0').rstrip('.')`.
- GitHub search: always add `pushed:>YYYY-MM-DD` or the quota is spent on stale repos.
- Secrets are injected per command; a server started in one command does not inherit
  `GITHUB_TOKEN` later unless exported at launch.
- The live uvicorn on port 12000 does **not** auto-reload — restart it by hand.

## Where the money question stands
Gas is solved internally (the fork path needs none). **Value is not**: the
beneficiary holds 0 wei on base-mainnet, base-sepolia, arbitrum-sepolia,
optimism-sepolia and eth-sepolia, and every keyless sponsor is exhausted or
gated. The fork leg proves the full mechanics end to end but its unit has no
market value. Reliable external bounties are human-gated by design. Detail in
`2026-11-18.md`, `2026-11-16.md`, `2026-11-09.md`.

## Settlement leg — engine-owned Fuel chain (commit `e9c5b5a`, 2026-11-18)
Fuel is the **primary** settlement leg; the sponsored EVM ladder is fallback.
- `fuel-core 0.47.1` + Sway contract `sway/settlement_ledger`: beneficiary is a
  compile-time constant, a commitment already settled is rejected. `synthesizer/fuel.py`
  supervises the node.
- **Why gasless is true:** the node runs `--min-gas-price 0`, so the only input a tx needs
  is a genesis UTXO. No sponsor, no user funds, external spend = 0.
- Settlement is **inline** (~1s), so the Arabic answer carries a real tx id, block, gas and
  the exact delta credited. Endpoints: `/fuel`, `/fuel/tx/{id}` (re-queried from the node,
  because no public explorer has a local chain).
- **Honesty boundary:** delivered amounts are ledger units on an engine-owned local chain,
  not withdrawable USDC. Never describe them as public-network money.
- Gotchas: block field is `transactionIds`; a replay reports the *original* tx; runtime
  state (`genesis/`, `contract_id.txt`, `settlements.json`) is gitignored while compiled
  artifacts are committed.

## Deploying the container — two defects only a container reveals (2026-11-18)
Building the image is not testing it; run it with no node and no state.
- **Missing binaries:** no `fuel-core`, so the primary leg could only report a fallback. A
  `debian:bookworm-slim` stage installs the release; `sway/` must be `COPY`d (the leg shells
  out to `sway/chain/settle.mjs`).
- **Baked runtime state:** `COPY sway` shipped `contract_id.txt` + `genesis/` from the build
  machine; the container's node starts a *different* chain, so settlements failed
  `InputContractDoesNotExist`. Exclude in `.dockerignore`; never trust the file —
  `FuelHand.verified_contract_id()` returns an id only if the chain answers for it.
- Generalises: a cached id/hash describes *the chain it was written for*, not "the
  deployment". Verify against the live chain.

## Deploy gate + topics gate (2026-11-18)
- 2.0s deploy gate (`.github` "test") blocks `pages`/`image`. Two causes, both fixed: CPU
  lanes ran ~16s past the answer holding the GIL into the next request (now gated by
  `cpu_lane_min_window_s`); the deadline was spent twice (`max(0.4, remaining)` plus an
  untimed balance RPC on the answer path — baseline read moved to background `work()`).
- `scanner.py`: **topics cannot vouch for a primitive** (a tweet archive matched
  `market-making` only via a self-assigned topic). Haystack = name + description.
- Historical ledger rows keep their citations; the gate governs what *new* answers adopt.
- Live backend: `uvicorn synthesizer.api:app --host 0.0.0.0 --port 12000`, UI at `/`.

## Older detail — archived, read on demand
`archive-2026-11-18-full-index.md` holds the previous full index (gas flywheel, x402 shop,
tender desk, Superteam Earn, self-provisioning, commerce module map). Daily logs:
`2026-10-30.md` (engine build, order->settle, margin model), `2026-10-31.md` (x402, keyless
testnet facilitator), `2026-11-06.md` (tender desk, first real money), `2026-11-07.md`
(Superteam Earn), `2026-11-09.md` (why revenue stayed $0.01: ephemeral sandbox address,
1800s/20-sandbox caps), `2026-11-16.md` (bounty venues, $0), `2026-11-18.md` (full build).

## Settlement record durability (2026-11-21)
The published page must render the committed ledger, not only rows the current
node answers for — a fresh CI node answers for its own tx alone, so strict
filtering reproduces the 281→1 shrink. Every row carries `verified_now`; the UI
labels checkable receipts differently from recorded ones. Overlapping CI runs
union ledgers via `scripts/merge_ledgers.py` (by `tx_hash`). The loop job never
publishes (no Fuel node → its ids are unlookupable). Cron is best-effort and was
observed skipping a slot, so liveness rests on the ledger, not on the schedule.
Detail: `2026-11-18.md`.

## Reverse Value Engine (commit `a358254`) — many routes, not one command
Answers a value intent by entering **every** route family at once (`synthesizer/matrix.py`:
`PathMatrix`/`Lane`/`build_lanes`), first *measured* payout wins. Two hard-won rules:
(1) the matrix gets `request_deadline_s - elapsed`, never a fixed budget — slow lanes are
reported `continuing` and keep working after the reply, not "failed"; (2) CPU lanes (local
EVM fork, z3) must be `cpu_bound=True` and start only if no network lane delivered, else
they starve the paying lane of the GIL (+500ms). Lane narration must never call a
`stood_down` lane a failure. Detail: `2026-11-18.md`.

## The 2.0s deadline flake = GIL starvation, not slow proving (commit `6654cd0`)
A repeated-intent latency flake (`synthesis` looked slow) was pure-Python work
declared as I/O stealing the GIL from the next request. Fixes: `evm` lane is
`cpu_bound=True`; CPU lanes take one process-wide `_CPU_LANE_SLOT`; and
`settlement_workers` 4→1 (settlement measured 76% CPU). 2–3 overruns/20 → 0/60.
**Do not "fix" timing by cutting the proof sweep short — that breaks determinism
(the core invariant).** Detail + the `cpu/wall` probe technique: `2026-11-18.md`.
