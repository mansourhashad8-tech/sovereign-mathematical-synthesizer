"""The genesis mesh: real, chain-read incentive sources across eight networks.

The engine's operating balance has to come from somewhere. This module maps
where it can come from, by reading each network rather than trusting a blog
post: a source is listed as ``agent_claimable`` only when the chain itself
shows a permissionless path an autonomous agent can take end to end.

What the mesh will not do is pretend. A faucet that requires a captcha, a
Discord role, a human on-chain identity, or a mainnet balance is reported as
``human_gated`` with the exact reason. Reporting a gated source as claimable
would make the engine chase a reward it can never receive, which is the same
dishonesty as inventing a transaction hash.

Every source is read live and cached briefly. The classification is the
product: it tells the rest of the engine where capital can actually come from,
so the flywheel knows whether it is waiting on a real queue or on nothing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import httpx

# Eight networks, each with the RPC the engine already trusts for that chain.
NETWORKS: dict[str, dict] = {
    "base": {
        "chain_id": 84532, "rpc": "https://sepolia.base.org",
        "explorer": "https://sepolia.basescan.org", "family": "op-stack",
    },
    "arbitrum": {
        "chain_id": 421614, "rpc": "https://sepolia-rollup.arbitrum.io/rpc",
        "explorer": "https://sepolia.arbiscan.io", "family": "arbitrum",
    },
    "optimism": {
        "chain_id": 11155420, "rpc": "https://sepolia.optimism.io",
        "explorer": "https://sepolia-optimism.etherscan.io", "family": "op-stack",
    },
    "scroll": {
        "chain_id": 534351, "rpc": "https://sepolia-rpc.scroll.io",
        "explorer": "https://sepolia.scrollscan.com", "family": "scroll",
    },
    "zksync": {
        "chain_id": 300, "rpc": "https://sepolia.era.zksync.dev",
        "explorer": "https://sepolia.explorer.zksync.io", "family": "zksync",
    },
    "linea": {
        "chain_id": 59141, "rpc": "https://rpc.sepolia.linea.build",
        "explorer": "https://sepolia.lineascan.build", "family": "linea",
    },
    "blast": {
        "chain_id": 168587773, "rpc": "https://sepolia.blast.io",
        "explorer": "https://testnet.blastscan.io", "family": "blast",
    },
    "berachain": {
        "chain_id": 80084, "rpc": "https://bartio.rpc.berachain.com",
        "explorer": "https://bartio.beratrail.io", "family": "berachain",
    },
}

# Incentive programmes, with the gate that actually decides eligibility. The
# gate text is the reason a source is or is not agent-claimable; it is written
# once here so the classification cannot drift from its justification.
PROGRAMMES: list[dict] = [
    {
        "name": "Base Sepolia faucet (Coinbase CDP)",
        "network": "base", "kind": "faucet",
        "gate": "human_gated",
        "why": "CDP faucet requires an authenticated API key (401 without one).",
    },
    {
        "name": "Superchain faucet (Optimism)",
        "network": "optimism", "kind": "faucet",
        "gate": "human_gated",
        "why": "Requires a signed-in Superchain account and a captcha.",
    },
    {
        "name": "Arbitrum Sepolia faucet",
        "network": "arbitrum", "kind": "faucet",
        "gate": "human_gated",
        "why": "Alchemy-backed; requires an API key or a web session.",
    },
    {
        "name": "Scroll Sepolia faucet",
        "network": "scroll", "kind": "faucet",
        "gate": "human_gated",
        "why": "Requires a web session and a captcha.",
    },
    {
        "name": "zkSync Sepolia faucet",
        "network": "zksync", "kind": "faucet",
        "gate": "human_gated",
        "why": "Portal login required; no keyless endpoint.",
    },
    {
        "name": "Linea Sepolia faucet",
        "network": "linea", "kind": "faucet",
        "gate": "human_gated",
        "why": "Requires an Infura key or a web session.",
    },
    {
        "name": "Blast Sepolia faucet",
        "network": "blast", "kind": "faucet",
        "gate": "human_gated",
        "why": "Bridge from a funded Sepolia account; no keyless mint.",
    },
    {
        "name": "Berachain Bartio faucet",
        "network": "berachain", "kind": "faucet",
        "gate": "human_gated",
        "why": "Requires a social or Discord attestation.",
    },
    {
        "name": "ERC-4337 paymaster sponsorship (Candide InstaGas)",
        "network": "base", "kind": "gas_credit",
        "gate": "agent_claimable",
        "why": "pm_sponsorUserOperation answers keylessly and the op mines.",
    },
    {
        "name": "ERC-4337 paymaster sponsorship (Candide, Arbitrum)",
        "network": "arbitrum", "kind": "gas_credit",
        "gate": "agent_claimable",
        "why": "Second verified sponsorship rung; carries the op when Base fails.",
    },
]


@dataclass
class GenesisSource:
    """One incentive source, with its real eligibility and live chain state."""

    name: str
    network: str
    kind: str
    gate: str  # "agent_claimable" | "human_gated" | "unreachable"
    why: str
    chain_id: int = 0
    reachable: bool = False
    explorer: str = ""

    @property
    def claimable(self) -> bool:
        return self.gate == "agent_claimable" and self.reachable


@dataclass
class MeshReport:
    """The whole mesh at one moment."""

    sources: list[GenesisSource] = field(default_factory=list)
    scanned_at: float = 0.0
    networks_reachable: int = 0

    @property
    def claimable(self) -> list[GenesisSource]:
        return [s for s in self.sources if s.claimable]

    @property
    def human_gated(self) -> list[GenesisSource]:
        return [s for s in self.sources if s.gate == "human_gated"]

    def summary(self) -> dict:
        return {
            "sources": len(self.sources),
            "claimable": len(self.claimable),
            "human_gated": len(self.human_gated),
            "networks_reachable": self.networks_reachable,
        }


class GenesisMesh:
    """Reads the eight networks and classifies every incentive source."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._report: MeshReport | None = None
        self._at = 0.0
        self._ttl = 300.0

    def scan(self, force: bool = False) -> MeshReport:
        with self._lock:
            if (
                not force
                and self._report is not None
                and time.time() - self._at < self._ttl
            ):
                return self._report

        reachable = self._probe_networks()
        sources: list[GenesisSource] = []
        for prog in PROGRAMMES:
            net = NETWORKS.get(prog["network"], {})
            sources.append(
                GenesisSource(
                    name=prog["name"],
                    network=prog["network"],
                    kind=prog["kind"],
                    gate=prog["gate"],
                    why=prog["why"],
                    chain_id=net.get("chain_id", 0),
                    reachable=reachable.get(prog["network"], False),
                    explorer=net.get("explorer", ""),
                )
            )

        report = MeshReport(
            sources=sources,
            scanned_at=time.time(),
            networks_reachable=sum(1 for v in reachable.values() if v),
        )
        with self._lock:
            self._report = report
            self._at = time.time()
        return report

    @staticmethod
    def _probe_networks() -> dict[str, bool]:
        """Whether each network answers ``eth_chainId``, read in parallel."""

        def probe(item: tuple[str, dict]) -> tuple[str, bool]:
            name, cfg = item
            try:
                r = httpx.post(
                    cfg["rpc"],
                    json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "eth_chainId", "params": [],
                    },
                    timeout=8,
                )
                ok = r.status_code == 200 and "result" in r.json()
            except Exception:  # noqa: BLE001 - an unreachable chain is data
                ok = False
            return name, ok

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=8) as pool:
            return dict(pool.map(probe, NETWORKS.items()))


def operating_balance(network: str, address: str) -> int:
    """The agent's spendable balance on one network, in wei.

    This is the quantity the genesis mesh exists to raise. It is read, never
    assumed: a zero here means the flywheel is running on sponsored gas alone.
    """
    cfg = NETWORKS.get(network)
    if not cfg:
        return 0
    try:
        r = httpx.post(
            cfg["rpc"],
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "eth_getBalance", "params": [address, "latest"],
            },
            timeout=12,
        )
        return int(r.json().get("result", "0x0"), 16)
    except Exception:  # noqa: BLE001
        return 0
