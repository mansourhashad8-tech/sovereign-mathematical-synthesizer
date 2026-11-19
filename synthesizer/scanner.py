"""Scan live sources for fresh primitives.

Two kinds of source:

* **GitHub** — repositories whose latest commit is younger than the lookback
  window and that advertise a building block a composition needs.
* **Live RPC feeds** — the chain itself, read keylessly. A gas price and the
  entry-point set from a public bundler are primitives too: they tell the
  executor which sponsor is available *right now*.

The scan never blocks a request. Queries fan out in parallel, the result is
cached, and a background warmup fills the cache at process start so the first
intent is as fast as the hundredth. A warm seed list guarantees the composer
always has material even when every network source is rate limited.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

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
    warming: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


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
        self._rate_limited_at: float = 0.0
        self._cache = _Cache()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers

    def _search(self, query: str, category: str, limit: int = 6) -> list[Primitive]:
        url = "https://api.github.com/search/repositories"
        # The freshness qualifier belongs in the query, not in a post-filter:
        # sorting by ``updated`` without it returns mostly stale repositories,
        # which the 48h check then throws away after spending the quota.
        since = (
            dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(hours=SETTINGS.github_lookback_hours)
        ).strftime("%Y-%m-%d")
        params = {
            "q": f"{query} pushed:>{since}",
            "sort": "updated",
            "order": "desc",
            "per_page": str(limit),
        }
        try:
            r = httpx.get(url, params=params, headers=self._headers(), timeout=20)
            if r.status_code != 200:
                if r.status_code in (403, 429):
                    self._rate_limited_at = time.time()
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
        """Return the cached primitive set, refreshing it in the background.

        A request must never wait on the network. If the cache is cold this
        returns the seed set immediately and kicks off a warmup thread, so the
        next request sees the live results. ``force=True`` is only used by
        offline tooling and tests.
        """
        with self._cache.lock:
            cached = self._cache.primitives
            fresh = cached is not None and time.time() - self._cache.fetched_at < 600

        if cached is not None and fresh and not force:
            return cached
        if cached is None:
            # Cold: serve seeds now, warm the cache off the request path.
            seeds = self._seeds()
            self._start_warmup()
            return seeds
        if not force:
            if time.time() - self._cache.fetched_at > 600:
                self._start_warmup()
            return cached

        return self._refresh()

    def _start_warmup(self) -> None:
        with self._cache.lock:
            if self._cache.warming:
                return
            self._cache.warming = True

        def _run() -> None:
            try:
                self._refresh()
            finally:
                with self._cache.lock:
                    self._cache.warming = False

        threading.Thread(target=_run, name="scanner-warmup", daemon=True).start()

    def _refresh(self) -> list[Primitive]:
        """Fan every source out in parallel and merge the results."""
        found: dict[str, Primitive] = {}
        jobs = [(q, c) for q, c in QUERIES.items()]
        with ThreadPoolExecutor(max_workers=min(8, len(jobs) + 1)) as pool:
            futures = [pool.submit(self._search, q, c) for q, c in jobs]
            futures.append(pool.submit(self._rpc_feed))
            for fut in futures:
                try:
                    for prim in fut.result(timeout=25):
                        found.setdefault(prim.key, prim)
                except Exception:  # noqa: BLE001 - a source may fail; others live
                    continue
        for prim in self._seeds():
            found.setdefault(prim.key, prim)

        primitives = sorted(
            found.values(),
            key=lambda p: (p.properties.get("source") != "live", p.freshness_hours),
        )
        with self._cache.lock:
            self._cache.fetched_at = time.time()
            self._cache.primitives = primitives
        return primitives

    def _rpc_feed(self) -> list[Primitive]:
        """Read the chain itself for primitives that exist right now."""
        out: list[Primitive] = []
        for url, chain in (
            (SETTINGS.public_bundler_url, SETTINGS.settlement_chain),
            (SETTINGS.rpc_url, SETTINGS.settlement_chain),
        ):
            try:
                r = httpx.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_supportedEntryPoints"
                        if "pimlico" in url
                        else "eth_gasPrice",
                        "params": [],
                    },
                    timeout=6,
                )
                if r.status_code != 200 or "result" not in r.json():
                    continue
                result = r.json()["result"]
                label = f"{chain}:{'entrypoints' if isinstance(result, list) else 'gas'}"
                out.append(
                    Primitive(
                        name=label,
                        repo=f"rpc://{url.split('//')[-1]}",
                        category="paymaster" if isinstance(result, list) else "private_mempool",
                        description=f"live chain feed ({label})",
                        url=url,
                        stars=0,
                        pushed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                        freshness_hours=0.0,
                        properties={"source": "live", "rpc": True, "value": result},
                    )
                )
            except Exception:  # noqa: BLE001 - optional source
                continue
        return out

    def rate_limited(self) -> bool:
        """True when GitHub refused the last scan for quota, not for content."""
        return time.time() - self._rate_limited_at < 3600

    def fresh_count(self) -> int:
        """Count live primitives without ever triggering a network call."""
        with self._cache.lock:
            cached = self._cache.primitives
        if cached is None:
            return sum(1 for p in self._seeds() if p.properties.get("source") == "live")
        return sum(1 for p in cached if p.properties.get("source") == "live")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
