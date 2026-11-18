"""Scan GitHub live for fresh primitives.

The engine searches for repositories whose latest commit is younger than the
lookback window (default 48h) and that advertise the building blocks a
composition needs: paymasters, flash-mint protocols, liquidity-bootstrapping
pools, and formal-verification tooling. Results are cached in-process so a
single request does not hammer the API, and a warm seed list guarantees the
composer always has material even when the search is rate limited.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

import httpx

from .config import SETTINGS
from .models import Primitive

# Query -> primitive category. GitHub search ANDs the terms, so keep each
# query to one or two distinctive words.
QUERIES: dict[str, str] = {
    "paymaster": "paymaster",
    "flashmint": "flash_mint",
    "flashloan": "flash_loan",
    "liquidity-bootstrapping": "liquidity",
    "formal-verification": "verification",
    "intents": "solver",
    "account-abstraction": "session_key",
    "mev-boost": "private_mempool",
    "market-making": "market_making",
}

# Category -> known-good seed repos (used when live search yields nothing).
SEED_BY_CATEGORY: dict[str, list[str]] = {
    "paymaster": ["pimlico-io/erc20-paymaster", "eth-infinitism/account-abstraction"],
    "flash_mint": ["aave/aave-v3-core", "Uniswap/v3-core"],
    "flash_loan": ["aave/aave-v3-core", "Uniswap/v3-core"],
    "liquidity": ["balancer/balancer-v2-monorepo", "aave/aave-v3-core"],
    "verification": ["Z3Prover/z3", "cvc5/cvc5", "certora/CVL2"],
    "solver": ["langchain-ai/langgraph", "elizaOS/eliza"],
    "session_key": ["rhinestonewtf/module-sdk", "safe-global/safe-core-sdk"],
    "private_mempool": ["flashbots/mev-boost"],
    "market_making": ["hummingbot/hummingbot"],
    "account": ["coinbase/agentkit", "safe-global/safe-core-sdk"],
}


@dataclass
class _Cache:
    fetched_at: float = 0.0
    primitives: list[Primitive] | None = None


def _hours_since(iso: str) -> float:
    if not iso:
        return 1e9
    try:
        when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    delta = dt.datetime.now(dt.timezone.utc) - when
    return max(delta.total_seconds() / 3600.0, 0.0)


class GitHubScanner:
    def __init__(self, token: str | None = None) -> None:
        self.token = token if token is not None else SETTINGS.github_token
        self._cache = _Cache()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers

    def _search(self, query: str, category: str, limit: int = 6) -> list[Primitive]:
        url = "https://api.github.com/search/repositories"
        params = {
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": str(limit),
        }
        try:
            r = httpx.get(url, params=params, headers=self._headers(), timeout=20)
            if r.status_code != 200:
                return []
            items = r.json().get("items", [])
        except Exception:  # noqa: BLE001 - network is optional
            return []

        out: list[Primitive] = []
        for item in items:
            pushed = item.get("pushed_at", "")
            hours = _hours_since(pushed)
            if hours > SETTINGS.github_lookback_hours:
                continue
            out.append(
                Primitive(
                    name=item.get("name", ""),
                    repo=item.get("full_name", ""),
                    category=category,
                    description=(item.get("description") or "")[:240],
                    url=item.get("html_url", ""),
                    stars=item.get("stargazers_count", 0),
                    pushed_at=pushed,
                    freshness_hours=round(hours, 2),
                    properties={"language": item.get("language"), "source": "live"},
                )
            )
        return out

    def _seeds(self) -> list[Primitive]:
        out: list[Primitive] = []
        for category, repos in SEED_BY_CATEGORY.items():
            for repo in repos:
                out.append(
                    Primitive(
                        name=repo.split("/")[-1],
                        repo=repo,
                        category=category,
                        description=f"known {category} primitive",
                        url=f"https://github.com/{repo}",
                        stars=0,
                        pushed_at="",
                        freshness_hours=0.0,
                        properties={"source": "seed"},
                    )
                )
        return out

    def scan(self, force: bool = False) -> list[Primitive]:
        """Return the union of live (fresh) and seed primitives, de-duplicated."""
        import time

        if not force and self._cache.primitives is not None and time.time() - self._cache.fetched_at < 600:
            return self._cache.primitives

        found: dict[str, Primitive] = {}
        for query, category in QUERIES.items():
            for prim in self._search(query, category):
                found.setdefault(prim.key, prim)
        for prim in self._seeds():
            found.setdefault(prim.key, prim)

        primitives = sorted(
            found.values(),
            key=lambda p: (p.properties.get("source") != "live", p.freshness_hours),
        )
        self._cache = _Cache(fetched_at=time.time(), primitives=primitives)
        return primitives

    def fresh_count(self) -> int:
        return sum(1 for p in self.scan() if p.properties.get("source") == "live")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
