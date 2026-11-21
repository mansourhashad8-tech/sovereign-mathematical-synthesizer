# Memory — sovereign-intent-mesh / commerce engine

Repo: `mansourhashad8-tech/sovereign-mathematical-synthesizer`, branch
`sovereign-mathematical-synthesizer`. Live engine (runtime URL, port 12000):
`https://work-1-dmmqjcnqawqsycvn.prod-runtime.all-hands.dev/`.
Pages: `https://mansourhashad8-tech.github.io/sovereign-mathematical-synthesizer/`.
Arabic NL intent -> formal constraints -> live primitive composition -> Z3/CVC5
proof -> execution. Non-custodial; beneficiary locked to
`0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47`. 112 tests pass.

## ZK settlement — the leg that completes without gas (`0164c97`,`bd5ceb3`,`6fbfc75`)
Fork exec (py-evm, `base_fee=0`) -> measured balance delta -> Groth16 proof ->
verifier inside the fork -> recorded settlement. Public signals
`[ok, beneficiary, minProfit, delta, netProfit, commitment]`; the contract reads
`delta`/`netProfit` from the proof, never the caller. Honest proof records at
325,716 gas; replay / inflated profit / wrong beneficiary / unregistered
commitment all revert. Three bugs gave proofs that verified locally and failed on
chain: **degenerate powers of tau** (no contribution -> IC points are the
identity), **stale `Verifier.sol`** (exported at 4 signals, used with 6), and
**G2 coordinate order** (never hand-encode points; use snarkjs
`zkey export soliditycalldata`).
**Honesty is load-bearing, not the cryptography.** The fork mints its own working
capital, so a valid proof says nothing about value: results carry
`money_is_real=False`, `value_unit=FSU`, working capital capped at 1,000 FSU, and
a floor above capacity records **no** settlement.

## Hard rules learned (do not re-litigate)
- Security flags (no custody / no user key / no user gas) are **system
  invariants**, never user preferences. The intent parser hardcodes them.
- A path with no flash primitive pins `N=0` and cannot earn.
- Profit model: `NetProfit = N·Σ(y_i−f_i) − gas`, `gas=0` (internal).
- When a solver returns a witness, **the witness is the truth**; any value
  recomputed from declared inputs is a different number wearing the same name.
- A receipt is not a payout: the double gate requires mined **and** a measured
  positive beneficiary delta.
- CVC5 pip wheel works (the binary does not); `mkReal` rejects scientific
  notation — format as `f"{v:.12f}".rstrip('0').rstrip('.')`.
- GitHub search: always add `pushed:>YYYY-MM-DD` or the quota is spent on stale
  repos that the 48h filter then drops.
- Secrets are injected per command; a server started in one command does not
  inherit `GITHUB_TOKEN` later unless exported at launch.
- The live uvicorn on port 12000 does **not** auto-reload — restart it by hand
  after changes.

## Where the money question stands
Gas is solved internally (the fork path needs none). **Value is not**: the
beneficiary holds 0 wei on base-mainnet, base-sepolia, arbitrum-sepolia,
optimism-sepolia and eth-sepolia, and every keyless sponsor is exhausted or
gated. The fork leg proves the full mechanics end to end but its unit has no
market value. Reliable external bounties are human-gated by design. Detail in
`2026-11-18.md`, `2026-11-16.md`, `2026-11-09.md`.

## Settlement leg — engine-owned Fuel chain (commit `e9c5b5a`, 2026-11-18)
Fuel is now the **primary** settlement leg; the sponsored EVM ladder is fallback.
- `fuel-core 0.47.1` + Sway contract `sway/settlement_ledger`: beneficiary locked
  as a compile-time constant, a commitment already settled is rejected.
  `synthesizer/fuel.py` supervises the node.
- **Why gasless is true:** the node runs `--min-gas-price 0`, so the only input a
  tx needs is a genesis UTXO. No sponsor, no user funds, external spend = 0.
- Settlement is **inline** (~1s), so the Arabic answer carries a real tx id, the
  block, the measured gas, and the exact delta credited.
- Endpoints: `/fuel` (ledger state), `/fuel/tx/{id}` (receipt re-queried from the
  node, because a public explorer has no record of a local chain).
- **Honesty boundary:** delivered amounts are ledger units on an engine-owned local
  chain, not withdrawable USDC. Never describe them as public-network money.
- Gotchas: block field is `transactionIds`; a replay must report the *original* tx;
  runtime state (`sway/chain/genesis/`, `contract_id.txt`, `settlements.json`) is
  gitignored while the compiled ledger artifacts are committed.
Detail: `2026-11-18.md`.

## Deploying the container — two defects only a container reveals (2026-11-18)
Building the image is not testing it. Run the built image with no node and no state.
- **Missing binaries:** the image had no `fuel-core`, so the primary leg could only
  ever report a fallback. A `debian:bookworm-slim` build stage downloads the release
  and copies `fuel-core*` into `/usr/local/bin`; `sway/` must also be `COPY`d because
  the leg shells out to `sway/chain/settle.mjs`.
- **Baked runtime state:** `COPY sway ./sway` shipped `contract_id.txt` and
  `sway/chain/genesis/` from the build machine. The container's node starts a
  *different* chain, so the cached id named a contract that did not exist and every
  settlement failed `InputContractDoesNotExist`. Exclude that state in
  `.dockerignore`, and never trust the file: `FuelHand.verified_contract_id()` reads
  the ledger's storage and returns the id only if the chain answers for it.
- Lesson generalises: any id/hash cached on disk describes *the chain it was written
  for*, not "the deployment". Verify against the live chain before use.

## Older detail — archived, read on demand
`archive-2026-11-18-full-index.md` holds the previous full index (gas flywheel
history, x402 shop, tender desk, Superteam Earn, self-provisioning, recovery,
and the commerce-engine module map). Daily logs: `2026-10-30.md` (engine build,
order->settle loop, margin model), `2026-10-31.md` (x402 routes, keyless
testnet facilitator), `2026-11-06.md` (tender desk, first real money),
`2026-11-07.md` (Superteam Earn, self-provisioning), `2026-11-09.md` (why
revenue stayed at $0.01: ephemeral sandbox address, 1800s/20-sandbox caps),
`2026-11-13.md`, `2026-11-16.md` (bounty venue reality check, $0 earned),
`2026-11-18.md` (this build, in full).

## Settlement record durability (2026-11-21)
The published page must render the committed ledger, not only rows the current
node answers for — a fresh CI node answers for its own tx alone, so strict
filtering reproduces the 281→1 shrink. Every row carries `verified_now`; the UI
labels checkable receipts differently from recorded ones. Overlapping CI runs
union ledgers via `scripts/merge_ledgers.py` (by `tx_hash`). The loop job never
publishes (no Fuel node → its ids are unlookupable). Cron is best-effort and was
observed skipping a slot, so liveness rests on the ledger, not on the schedule.
Detail: `2026-11-18.md`.
