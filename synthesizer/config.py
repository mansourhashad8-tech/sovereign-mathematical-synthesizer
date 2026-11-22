"""Central configuration.

The single locked constant is the beneficiary address. Everything else is a
knob that can be overridden by an environment variable, but nothing can change
the beneficiary: it is checked at every execution boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# The one address the system is allowed to pay. Locked in code, checked at
# every gate. No configuration value can override it.
BENEFICIARY: str = "0xe3d1c7dcd5e95b6441716b65f9c46b73e91b4d47"

# Ethereum Mainnet (where the sovereign intent compiler drafts).
MAINNET_CHAIN_ID: int = 1
# Base mainnet is the cheap settlement rail used by x402 machine payments.
BASE_CHAIN_ID: int = 8453

TREASURY_WATCH_ADDRESS: str = BENEFICIARY


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    # --- identity ---
    beneficiary: str = BENEFICIARY

    # --- generation loop ---
    # How long the forced generative loop keeps searching per request (seconds).
    loop_budget_seconds: int = _env_int("SYNTH_LOOP_BUDGET", 90)
    # Hard cap on composition attempts before we return the best proven path.
    max_attempts: int = _env_int("SYNTH_MAX_ATTEMPTS", 24)
    # Minimum acceptable net profit, in USD (constraint NetProfit > 0 becomes
    # NetProfit >= min_net_profit_usd).
    min_net_profit_usd: float = float(_env("SYNTH_MIN_NET_PROFIT", "0.01"))
    # The instant-execution budget: parse + instantiate + prove must finish in
    # this window. Everything slower is optional enrichment, never blocking.
    instant_deadline_ms: int = _env_int("SYNTH_INSTANT_DEADLINE_MS", 900)
    # Solver timeout per proof attempt.
    prover_timeout_ms: int = _env_int("SYNTH_PROVER_TIMEOUT_MS", 4000)
    # Deadline for the optional network enrichment (live scan / LLM polish).
    enrichment_deadline_ms: int = _env_int("SYNTH_ENRICH_DEADLINE_MS", 1200)

    # --- GitHub live scan ---
    github_lookback_hours: int = _env_int("SYNTH_GH_LOOKBACK_HOURS", 48)
    # A repository with no stars and no description is not a primitive, whatever
    # the search returns: the narrative names these as composed components, so a
    # name nobody can vouch for must not be cited as one.
    github_min_stars: int = _env_int("SYNTH_GH_MIN_STARS", 25)
    github_max_repos: int = _env_int("SYNTH_GH_MAX_REPOS", 40)
    github_token: str = _env("GITHUB_WRITE_TOKEN", "") or _env("GITHUB_TOKEN", "")

    # --- chain / settlement ---
    # Base Sepolia: a real public testnet where keyless bundlers and public RPCs
    # are reachable, so an execution can be broadcast and audited on an explorer.
    settlement_chain: str = _env("SYNTH_CHAIN", "base-sepolia")
    chain_id: int = _env_int("SYNTH_CHAIN_ID", 84532)
    explorer_base: str = _env(
        "SYNTH_EXPLORER", "https://sepolia.basescan.org/tx/"
    )
    rpc_url: str = _env("SYNTH_RPC_URL", "https://sepolia.base.org")
    # Extra endpoints for the same chain. A public node answers "0x" for state
    # it has not indexed yet, so an empty read is confirmed against a mirror
    # before the engine concludes a contract is missing.
    rpc_mirrors: list[str] = field(
        default_factory=lambda: [
            u.strip()
            for u in _env(
                "SYNTH_RPC_MIRRORS", "https://base-sepolia-rpc.publicnode.com"
            ).split(",")
            if u.strip()
        ]
    )
    # How long a bundler's sponsorship refusal is trusted before re-asking.
    # Candidates proved per parallel wave. A wave that proves anything ends the
    # sweep, so a small wave keeps the common case fast and still widens the
    # search when nothing proves.
    proof_wave_size: int = 4
    sponsor_refusal_ttl_seconds: float = 120.0
    # The same for a bundler that will not take the signed operation.
    broadcast_refusal_ttl_seconds: float = 60.0
    # Keyless public bundler used for reads and as the first broadcast rung.
    public_bundler_url: str = _env(
        "SYNTH_PUBLIC_BUNDLER", "https://public.pimlico.io/v2/84532/rpc"
    )
    # --- self-sustaining gas flywheel ---
    # A keyless paymaster that sponsors the whole operation. Pimlico's public
    # endpoint now demands a sponsorship policy id, so the working rung is
    # Candide's InstaGas: it sponsors ERC-4337 ops with no key and no deposit,
    # which is what makes a zero-capital execution possible at all.
    paymaster_url: str = _env(
        "SYNTH_PAYMASTER_URL", "https://api.candide.dev/public/v3/84532"
    )
    # Extra paymasters, tried after the primary. Comma-separated.
    extra_paymasters: list[str] = field(
        default_factory=lambda: [
            u.strip() for u in _env("SYNTH_PAYMASTERS", "").split(",") if u.strip()
        ]
    )
    # Fuel: the engine-owned SwayVM chain that replaced the sponsored EVM leg.
    # These point at a locally supervised node, so nothing here is a third party.
    fuel_rpc_url: str = _env("FUEL_RPC_URL", "http://127.0.0.1:4000/v1/graphql")
    fuel_port: int = _env_int("FUEL_PORT", 4000)
    fuel_core_bin: str = _env("FUEL_CORE_BIN", "")
    forc_bin: str = _env("FORC_BIN", "")
    fuel_consensus_key: str = _env("FUEL_CONSENSUS_KEY", "")
    # The Fuel leg is the primary settlement path; the EVM ladder stays as a
    # fallback for networks the engine already proved against.
    fuel_primary: bool = _env("SYNTH_FUEL_PRIMARY", "1") not in ("0", "false", "False", "")

    # Fallback networks, tried in order when the primary chain cannot settle.
    # Each entry is "name|chain_id|rpc|explorer|paymaster".
    fallback_networks: list[str] = field(
        default_factory=lambda: [
            n.strip()
            for n in _env(
                "SYNTH_FALLBACK_NETWORKS",
                # Arbitrum Sepolia is the verified second rung: Candide's public
                # InstaGas paymaster sponsors there and the operation mines.
                # Ethereum Sepolia was removed: its bundler caps userOp gas at
                # 15M and rejects the sponsored op, so it advertised redundancy
                # it could not deliver.
                "arbitrum-sepolia|421614|https://sepolia-rollup.arbitrum.io/rpc"
                "|https://sepolia.arbiscan.io/tx/|https://api.candide.dev/public/v3/421614",
            ).split(";")
            if n.strip()
        ]
    )
    # How long to wait for a userOperation receipt before declaring the op
    # accepted-but-not-yet-mined (which is still a real submission).
    receipt_timeout_seconds: float = float(_env("SYNTH_RECEIPT_TIMEOUT", "20"))
    receipt_poll_seconds: float = float(_env("SYNTH_RECEIPT_POLL", "1.5"))
    # A watchdog re-attempts any settlement that came back unsettled. Zero
    # means no cap: the engine keeps trying until the gas balance allows the
    # settlement, which is the whole promise. A positive value is a deliberate
    # cap for tests and for operators who want a bounded run.
    watchdog_attempts: int = _env_int("SYNTH_WATCHDOG_ATTEMPTS", 0)
    # The ceiling on the backoff between rounds, in seconds. A long wait is
    # correct here: the thing being waited for is an external balance change,
    # and hammering a chain that cannot pay only burns quota.
    watchdog_max_delay: float = _env_float("SYNTH_WATCHDOG_MAX_DELAY", 300.0)
    # Where the agent's own signing key lives. A stable identity is a
    # prerequisite for holding a balance: the smart account address is derived
    # from this key, so a per-process key is an account that can never
    # accumulate value.
    session_key_path: str = _env("SYNTH_SESSION_KEY_PATH", ".synth/session.key")
    # The gas reserve takes this slice of every settled profit, so the agent
    # becomes self-funding instead of depending on a faucet or a sponsor.
    gas_reserve_bps: int = _env_int("SYNTH_GAS_RESERVE_BPS", 500)
    # A paymaster that accepts the value token itself as gas, if one is ever
    # configured. Empty means the token leg is unavailable: a sponsor that pays
    # in native coin does not let the engine spend USDC on gas, and pretending
    # otherwise would promise a payment the chain would reject.
    token_paymaster_url: str = _env("SYNTH_TOKEN_PAYMASTER_URL", "")
    # Whether an external paymaster may be asked for gas at all. Off by
    # default: the engine funds itself, and a third party's quota must never
    # decide whether a settlement happens. Turning this on is an explicit
    # bootstrap decision, not the normal path.
    allow_external_sponsor: bool = _env_bool("SYNTH_ALLOW_EXTERNAL_SPONSOR", False)
    # The token the lock settles in. USDC is what the profit is denominated in.
    value_token: str = _env(
        "SYNTH_VALUE_TOKEN", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    )
    # Address of the deployed GasValueLock, once it exists on a chain.
    gas_value_lock: str = _env("SYNTH_GAS_VALUE_LOCK", "")
    # Whether a settlement should encode the proven profit as a real value
    # transfer. Off by default: the engine holds no capital, so a sponsored
    # no-op is the honest settlement until a funded route exists. The gas path
    # itself is proved either way.
    transfer_settlement_value: bool = _env_bool("SYNTH_TRANSFER_VALUE", False)
    # When no public gas source is reachable, the engine can still execute and
    # prove the settlement on the local fork. That is real execution with a real
    # proof, but it is not a public-chain payout, so it is reported under its
    # own network label and never as an on-chain delivery.
    settle_on_fork: bool = _env_bool("SYNTH_SETTLE_ON_FORK", True)
    # The path matrix runs every route to the target value concurrently. On by
    # default: a single blocked route must never be the system's answer.
    path_matrix: bool = _env_bool("SYNTH_PATH_MATRIX", True)
    # How long one matrix lane may run before it is stood down. A lane that
    # outlives this is reported as stalled, never as the outcome of the request.
    lane_timeout_s: float = float(_env("SYNTH_LANE_TIMEOUT_S", "8"))
    # How long the *answer* may wait on the matrix. Short by design: the chat
    # request must return in seconds, while the lanes themselves keep working.
    # The window is sized to the engine-owned chain, which is the route that
    # actually delivers and takes ~1.2s (node round trip + receipt). A shorter
    # window would abandon the paying route and report "still working" while the
    # money had in fact landed. The matrix returns as soon as any lane delivers,
    # so this is a ceiling, not a wait.
    matrix_budget_s: float = float(_env("SYNTH_MATRIX_BUDGET_S", "1.6"))
    # Lanes from finished requests may keep working, but only this many at once
    # across the whole process. Without a ceiling, repeated requests were measured
    # to grow the thread count on every call until each answer overran its
    # deadline under the contention.
    matrix_max_inflight: int = _env_int("SYNTH_MATRIX_MAX_INFLIGHT", 48)
    # The whole request must answer by this deadline. The matrix is given
    # whatever time is left after parsing and proving, so a slow proof does not
    # push the answer past the deadline and a fast one does not waste the window.
    request_deadline_s: float = float(_env("SYNTH_REQUEST_DEADLINE_S", "1.5"))
    # How long a non-finance request may wait for its deliverable. There is no
    # paying venue to race in that case, so the content *is* the answer and the
    # only question is whether the model answered. Bounded so a stalled model
    # cannot hold a request open forever.
    creator_deadline_s: float = float(_env("SYNTH_CREATOR_DEADLINE_S", "45"))
    # One thread per lane: a queued lane is an untried route, and the matrix
    # must try every route it claims to have entered.
    matrix_workers: int = _env_int("SYNTH_MATRIX_WORKERS", 12)
    # Concurrent background settlements. Settlement is I/O-bound; a small pool
    # keeps a burst of intents from opening unbounded sponsor handshakes.
    settlement_workers: int = _env_int("SYNTH_SETTLE_WORKERS", 4)
    # Extra bundlers, tried in order after the public one. Comma-separated.
    extra_bundlers: list[str] = field(
        default_factory=lambda: [
            u.strip()
            for u in _env("SYNTH_BUNDLERS", "").split(",")
            if u.strip()
        ]
    )

    # --- LLM brain ---
    pollinations_url: str = _env(
        "SYNTH_POLLINATIONS_URL", "https://text.pollinations.ai/openai"
    )
    gemini_key: str = _env("GEMINI_API_KEY", "") or _env("GOOGLE_API_KEY", "")
    groq_key: str = _env("GROQ_API_KEY", "")
    llm_timeout_seconds: int = _env_int("SYNTH_LLM_TIMEOUT", 40)

    # --- runtime ---
    host: str = _env("SYNTH_HOST", "0.0.0.0")
    port: int = _env_int("SYNTH_PORT", 12000)
    public_base_url: str = _env("SYNTH_PUBLIC_URL", "")

    # --- background mode ---
    # When true the loop keeps running after a request returns, 24/7, until the
    # intent's constraints are provable.
    always_on: bool = _env_bool("SYNTH_ALWAYS_ON", True)
    # How long the background pass waits between attempts at unsettled intents.
    keepalive_seconds: float = float(_env("SYNTH_KEEPALIVE_SECONDS", "120"))

    evidence_dir: str = _env("SYNTH_EVIDENCE_DIR", "evidence")

    # Known primitive repositories, used as a warm seed so a scan always has
    # something to compose even when the live search is rate limited.
    seed_repos: list[str] = field(
        default_factory=lambda: [
            "coinbase/agentkit",
            "safe-global/safe-core-sdk",
            "rhinestonewtf/module-sdk",
            "eth-infinitism/account-abstraction",
            "langchain-ai/langgraph",
            "elizaOS/eliza",
            "hummingbot/hummingbot",
            "aave/aave-v3-core",
            "Uniswap/v3-core",
            "balancer/balancer-v2-monorepo",
            "flashbots/mev-boost",
            "pimlico-io/erc20-paymaster",
            "bcnmy/scw-contracts",
            "certora/CVL2",
            "Z3Prover/z3",
            "cvc5/cvc5",
        ]
    )


SETTINGS = Settings()
