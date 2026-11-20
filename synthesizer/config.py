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
    # A watchdog re-attempts any settlement that came back simulated.
    watchdog_attempts: int = _env_int("SYNTH_WATCHDOG_ATTEMPTS", 3)
    # Where the agent's own signing key lives. A stable identity is a
    # prerequisite for holding a balance: the smart account address is derived
    # from this key, so a per-process key is an account that can never
    # accumulate value.
    session_key_path: str = _env("SYNTH_SESSION_KEY_PATH", ".synth/session.key")
    # The gas reserve takes this slice of every settled profit, so the agent
    # becomes self-funding instead of depending on a faucet or a sponsor.
    gas_reserve_bps: int = _env_int("SYNTH_GAS_RESERVE_BPS", 500)
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
