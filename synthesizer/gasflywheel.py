"""The self-sustaining gas flywheel.

Gas is the only thing standing between a proved path and a settled one, so it
must never be the user's problem and never a dead end. This module owns that
concern and nothing else: it turns a signed intent into a real, mined
ERC-4337 transaction whose gas is paid by a third party.

The rungs, in order of preference:

1. **Keyless paymaster sponsorship.** A paymaster signs the operation's gas
   costs. The account's balance is never consulted, so the beneficiary's funds
   are untouched. This is the primary rung and the one that makes a
   zero-capital execution possible.
2. **Network redundancy.** The same operation is rebuilt and retried on every
   configured fallback network, because a sponsorship quota or a stalled
   bundler on one chain says nothing about another.
3. **Accepted-but-unmined.** If a bundler accepts the operation but the receipt
   has not landed inside the timeout, that is still a real submission with a
   real userOp hash. It is reported as submitted, not as a failure.

What this module refuses to do: invent a hash. A transaction hash it returns
came from a bundler, and its receipt, when available, came from a node.

The encoding here is the part that is easy to get wrong. An ERC-4337 v0.6
userOp hash covers the *packed* operation, which includes every gas field and
the paymaster data. Sponsorship is therefore a two-phase handshake: sign, ask
for sponsorship, then re-sign over the operation that now carries the
paymaster's fields. Signing once and reusing that signature over different gas
fields is what produces the "invalid signature" rejections that look like a
dead paymaster but are really a stale signature.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import httpx
from eth_abi import decode
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak, to_checksum_address

from .config import SETTINGS
from .valuegas import ReserveAccumulator

# The canonical ERC-4337 v0.6 entry point and SimpleAccountFactory. Both are
# deployed on every major testnet, so the initCode we sign is real.
ENTRY_POINT = "0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789"
SIMPLE_ACCOUNT_FACTORY = "0x9406Cc6185a346906296840746125a0E44976454"

# Gas limits for a batch that deploys the account and makes one call. Generous
# on purpose: the paymaster pays, and an out-of-gas op wastes a full round trip.
DEFAULT_CALL_GAS = 200_000
DEFAULT_VERIFICATION_GAS = 200_000
DEFAULT_PRE_VERIFICATION_GAS = 50_000
# Deploying the account inside the operation costs about 300k of verification
# gas (measured against the real SimpleAccountFactory). Budgeting the deployed
# case's 200k makes the first operation fail with AA13, which reads as an
# initCode bug rather than an underestimated limit.
DEPLOY_VERIFICATION_GAS = 400_000
# Outer-transaction allowance on top of the operation's own gas limits, for the
# EntryPoint's bookkeeping, the deposit transfer and event logs. Measured at
# about 330k for an account-deploying operation, so this leaves a margin.
HANDLE_OPS_OVERHEAD_GAS = 400_000

# Cost of the transaction that credits the EntryPoint deposit. Fixed and small:
# one call, no account deployment.
DEPOSIT_TX_GAS = 60_000

# Gas limit for a plain signed transaction that calls the lock directly.
NATIVE_TX_GAS = 300_000

@dataclass
class Relayer:
    """A gas provider the engine can hand a settlement to.

    ``reachable`` is discovered, never assumed. A relayer that cannot be
    contacted is recorded as such so the report says which rungs were actually
    available and which were not, instead of implying a sponsorship that never
    existed.
    """

    name: str
    kind: str  # "sponsor" | "relay" | "automation"
    endpoint: str
    reachable: bool = False
    detail: str = ""
    mainnet_sponsors: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "endpoint": self.endpoint,
            "reachable": self.reachable,
            "detail": self.detail,
            "mainnet_sponsors": self.mainnet_sponsors,
        }


# The three named relayers, plus the two paths the engine already uses. The
# order is the order they are tried: cheap and keyless first, key-gated last.
RELAYERS: tuple[Relayer, ...] = (
    Relayer(
        name="candide",
        kind="sponsor",
        endpoint="https://api.candide.dev/public/v3/8453",
    ),
    Relayer(
        name="gelato",
        kind="relay",
        endpoint="https://relay.gelato.digital",
    ),
    Relayer(
        name="chainlink-automation",
        kind="automation",
        endpoint="https://automation.chain.link/base",
    ),
)


def _hex(data: bytes) -> str:
    return "0x" + data.hex()


def _strip0x(value: str) -> str:
    return value[2:] if value.startswith("0x") else value


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _http() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    timeout=12.0,
                    limits=httpx.Limits(
                        max_keepalive_connections=16, max_connections=32
                    ),
                    headers={"Content-Type": "application/json"},
                )
    return _client


def _rpc(url: str, method: str, params: list, timeout: float = 12.0) -> dict | None:
    """One JSON-RPC call. A dead endpoint returns None, never raises."""
    try:
        r = _http().post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001 - an unreachable source is not a failure
        return None


# Public endpoints answer ``"0x"`` for state they simply have not indexed yet.
# An empty result is therefore not evidence of absence, and code that treats it
# as such will conclude a deployed contract does not exist.
_CHAIN_IDS: dict[str, int] = {}


def _chain_id_of(url: str) -> int:
    """The chain an endpoint actually serves, cached.

    A mirror is only a second opinion about the *same* chain. Endpoints are
    configured per deployment, not per network, so a mirror can easily point at
    another chain entirely — reading a base-mainnet address from a base-sepolia
    node returns that chain's state for the same address and silently answers a
    different question.
    """
    if url in _CHAIN_IDS:
        return _CHAIN_IDS[url]
    data = _rpc(url, "eth_chainId", [], timeout=8)
    raw = (data or {}).get("result")
    try:
        value = int(raw, 16)
    except (TypeError, ValueError):
        value = 0
    if value:
        _CHAIN_IDS[url] = value
    return value


def _read_state(url: str, method: str, params: list, timeout: float = 12.0,
                attempts: int = 3) -> str | None:
    """A state read that distinguishes "absent" from "not answered yet".

    Returns the hex result, or None when no endpoint produced a usable answer.
    A caller that gets None must not conclude the state is empty; it must treat
    the chain as unreadable, which is a different thing.
    """
    target_chain = _chain_id_of(url)
    for attempt in range(attempts):
        data = _rpc(url, method, params, timeout=timeout)
        result = (data or {}).get("result")
        if isinstance(result, str) and result.startswith("0x"):
            if result != "0x":
                return result
            # "0x" may be a genuine empty result or a lagging node. Confirm
            # with a second endpoint *on the same chain* before believing it.
            for mirror in SETTINGS.rpc_mirrors:
                if mirror == url:
                    continue
                if target_chain and _chain_id_of(mirror) != target_chain:
                    continue
                mirror_data = _rpc(mirror, method, params, timeout=timeout)
                mirror_result = (mirror_data or {}).get("result")
                if isinstance(mirror_result, str) and mirror_result.startswith("0x"):
                    return mirror_result
            return result
        time.sleep(0.4 * (attempt + 1))
    return None


@dataclass
class Network:
    """One chain the flywheel can settle on."""

    name: str
    chain_id: int
    rpc_url: str
    explorer_base: str
    paymaster_url: str


# Base mainnet. Value has to land somewhere real, so the engine always knows
# how to reach it even when the configured primary is a testnet.
MAINNET_CHAIN_ID = 8453
MAINNET = Network(
    name="base-mainnet",
    chain_id=MAINNET_CHAIN_ID,
    rpc_url="https://mainnet.base.org",
    explorer_base="https://basescan.org/tx/",
    paymaster_url="",
)


@dataclass
class GasSettlement:
    """The outcome of one attempt to settle an operation with sponsored gas."""

    status: str  # "executed" | "submitted" | "unsettled" | "reverted"
    network: str = ""
    chain_id: int = 0
    # The real on-chain transaction hash, once the operation is mined.
    tx_hash: str = ""
    # The ERC-4337 userOp hash the bundler returned. Distinct from tx_hash and
    # available one step earlier: a bundler accepts an op before it is mined.
    user_op_hash: str = ""
    explorer_url: str = ""
    paymaster: str = ""
    account: str = ""
    gas_paid_by: str = ""  # "paymaster" | "none"
    actual_gas_cost_wei: int = 0
    block_number: int = 0
    reason: str = ""
    attempts: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def mined(self) -> bool:
        return self.status == "executed" and bool(self.tx_hash)


def _networks() -> list[Network]:
    """The primary network first, then every configured fallback.

    Base mainnet is always appended when it is not already primary. Without it
    the engine can settle on a testnet but has nowhere to put real value, which
    is the exact split this module exists to close.
    """
    nets = [
        Network(
            name=SETTINGS.settlement_chain,
            chain_id=SETTINGS.chain_id,
            rpc_url=SETTINGS.rpc_url,
            explorer_base=SETTINGS.explorer_base,
            paymaster_url=SETTINGS.paymaster_url,
        )
    ]
    for spec in SETTINGS.fallback_networks:
        parts = spec.split("|")
        if len(parts) != 5:
            continue
        name, chain_id, rpc_url, explorer, paymaster = parts
        try:
            nets.append(
                Network(
                    name=name,
                    chain_id=int(chain_id),
                    rpc_url=rpc_url,
                    explorer_base=explorer,
                    paymaster_url=paymaster,
                )
            )
        except ValueError:
            continue

    if not any(net.chain_id == MAINNET_CHAIN_ID for net in nets):
        nets.append(MAINNET)
    return nets


class GasFlywheel:
    """Builds, sponsors, signs, and broadcasts a gasless ERC-4337 operation."""

    def __init__(self, session_key: str, account_address: str) -> None:
        self.session_key = session_key
        self._session_address = Account.from_key(session_key).address
        self._account_hint = account_address
        self._account_cache: dict[int, str] = {}
        # The self-funding reserve. Its whole purpose is to stop the engine
        # from ever being unable to pay for gas, which is what used to make a
        # funded intent die at the broadcast step.
        self.reserve = ReserveAccumulator()
        self.relay_log: list[str] = []
        # The internal gas balance, as the engine sees it: what it has spent on
        # its own settlements and how many landed. Together with ``reserve``
        # this is the profit-reinvestment loop's state.
        self.gas_spent_wei: int = 0
        self.settlements_landed: int = 0

    # -- relayers ----------------------------------------------------------

    def probe_relayers(self, timeout: float = 6.0) -> list[Relayer]:
        """Discover which relayers actually answer right now.

        Registration with a relayer is a network fact, not a configuration
        value, so it is measured. Each probe records what came back. A relayer
        that is DNS-dead or returns a non-API page is marked unreachable with
        the reason, because claiming a sponsorship that was never granted is
        the same lie as inventing a transaction hash.
        """
        results: list[Relayer] = []
        for spec in RELAYERS:
            relay = Relayer(
                name=spec.name,
                kind=spec.kind,
                endpoint=spec.endpoint,
            )
            try:
                if spec.kind == "sponsor":
                    relay.reachable, relay.mainnet_sponsors, relay.detail = (
                        self._probe_sponsorship(spec.endpoint, timeout)
                    )
                else:
                    resp = httpx.get(spec.endpoint, timeout=timeout, follow_redirects=True)
                    # A marketing page is not an API. Only treat a JSON answer
                    # as a usable relay endpoint.
                    is_json = resp.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    relay.reachable = resp.status_code == 200 and is_json
                    relay.detail = (
                        f"http_{resp.status_code}_json" if is_json
                        else f"http_{resp.status_code}_html_not_api"
                    )
            except httpx.HTTPError as exc:
                relay.reachable = False
                relay.detail = f"unreachable:{type(exc).__name__}"
            except Exception as exc:  # noqa: BLE001 - probe must never raise
                relay.reachable = False
                relay.detail = f"error:{type(exc).__name__}"
            results.append(relay)
        self.relay_log = [f"{r.name}:{r.detail}" for r in results]
        return results

    def _probe_sponsorship(
        self, url: str, timeout: float
    ) -> tuple[bool, bool, str]:
        """Ask a paymaster for a real sponsorship on mainnet.

        Supporting the entry point is not the same as agreeing to pay. A
        paymaster can advertise the entry point and still refuse every
        operation by policy, which is exactly what Candide does on Base
        mainnet. The only honest test is to submit an operation and read the
        answer, so this does that and reports the refusal verbatim.
        """
        try:
            net = MAINNET
            op = self._build_op(net, "0x")
            if op is None:
                return False, False, "no_account"
            op["signature"] = self._sign_op(op, net.chain_id)
            data = _rpc(
                url,
                "pm_sponsorUserOperation",
                [op, ENTRY_POINT, {"chainId": hex(net.chain_id)}],
                timeout=timeout,
            )
            if not data:
                return False, False, "no_answer"
            if data.get("result"):
                return True, True, "sponsored"
            error = data.get("error") or {}
            code = error.get("code", "?")
            message = str(error.get("message", ""))[:80]
            return True, False, f"refused:{code}:{message}"
        except Exception as exc:  # noqa: BLE001 - probe must never raise
            return False, False, f"error:{type(exc).__name__}"

    def relay_report(self) -> dict:
        """The relayer picture, for the delivery report."""
        probes = self.probe_relayers()
        return {
            "relayers": [r.as_dict() for r in probes],
            "reachable": [r.name for r in probes if r.reachable],
            "mainnet_sponsors": [r.name for r in probes if r.mainnet_sponsors],
            "reserve": self.reserve.summary(),
        }

    def record_settlement_gas(self, gas_cost_wei: int, path=None,
                              landed: bool = False) -> None:
        """Close the profit-reinvestment loop for one settled operation.

        Gas that a settlement paid is deducted from the running balance, and the
        reserve slice the lock set aside is credited back. The two together are
        what makes the next settlement independent: it draws on gas this engine
        already earned instead of asking a sponsor for it.
        """
        if gas_cost_wei > 0:
            self.gas_spent_wei += int(gas_cost_wei)
        self.settlements_landed += 1 if landed else 0
        # The lock's own reserve grows by ``gas_reserve_bps`` of the settled
        # value. Crediting the same bps here keeps the engine's accounting and
        # the chain's in step, so a later quote does not promise a reserve the
        # contract does not hold.
        if landed and path is not None:
            try:
                profit_units = int(path.proven_net_profit_usd * 1e18)
            except (TypeError, ValueError):
                profit_units = 0
            if profit_units > 0:
                self.reserve.refill_from(
                    profit_units * SETTINGS.gas_reserve_bps // 10_000
                )

    def gas_report(self) -> dict:
        """How the engine has funded its own gas so far."""
        return {
            "gas_spent_wei": self.gas_spent_wei,
            "settlements_landed": self.settlements_landed,
            "external_sponsor_allowed": SETTINGS.allow_external_sponsor,
            "reserve": self.reserve.summary(),
        }

    # -- account -----------------------------------------------------------

    def account_for(self, net: Network) -> str:
        """The authoritative SimpleAccount address, read from the factory.

        Asking the factory (rather than deriving locally) matters: a locally
        derived CREATE2 value that disagrees with the factory produces the
        ``AA14 initCode must return sender`` rejection, which looks like a
        broken paymaster but is really a wrong address.
        """
        cached = self._account_cache.get(net.chain_id)
        if cached:
            return cached
        data = _hex(
            _selector("getAddress(address,uint256)")
            + abi_encode(["address", "uint256"], [self._session_address, 0])
        )
        result = _rpc(
            net.rpc_url, "eth_call", [{"to": SIMPLE_ACCOUNT_FACTORY, "data": data}, "latest"]
        )
        address = ""
        if result and isinstance(result.get("result"), str) and len(result["result"]) >= 42:
            address = "0x" + result["result"][-40:]
        if not address:
            address = self._account_hint
        if address:
            self._account_cache[net.chain_id] = address
        return address

    def _init_code(self) -> str:
        payload = (
            _selector("createAccount(address,uint256)")
            + abi_encode(["address", "uint256"], [self._session_address, 0])
        )
        return SIMPLE_ACCOUNT_FACTORY + payload.hex()

    def _nonce(self, net: Network, sender: str) -> int:
        """Read the account's current entry-point nonce (0 if not deployed)."""
        data = _hex(_selector("getNonce(address,uint192)") + abi_encode(
            ["address", "uint192"], [sender, 0]
        ))
        result = _rpc(
            net.rpc_url, "eth_call", [{"to": ENTRY_POINT, "data": data}, "latest"]
        )
        if result and isinstance(result.get("result"), str):
            try:
                return int(result["result"], 16)
            except ValueError:
                return 0
        return 0

    def balance_of(self, net: Network, address: str) -> int:
        """Native balance in wei, read from the node.

        A value transfer needs the account to hold the value. Gas is sponsored,
        but value is not: conservation of value is not a policy this engine can
        override, so the amount must be checked rather than assumed.
        """
        result = _rpc(
            net.rpc_url, "eth_getBalance", [address, "latest"], timeout=15
        )
        raw = (result or {}).get("result", "0x0")
        try:
            return int(str(raw), 16)
        except ValueError:
            return 0

    def token_balance(self, net: Network, token: str, address: str) -> int:
        """ERC-20 balance in base units, read from the node.

        Stablecoin profit lands as a token balance, not as native value, so the
        sweep is verified against the token the path actually pays in.
        """
        data = "0x70a08231" + "0" * 24 + _strip0x(address).lower()
        result = _rpc(
            net.rpc_url, "eth_call", [{"to": token, "data": data}, "latest"], timeout=15
        )
        raw = (result or {}).get("result", "0x")
        try:
            return int(str(raw), 16)
        except ValueError:
            return 0

    def _deployed(self, net: Network, sender: str) -> bool:
        code = _read_state(net.rpc_url, "eth_getCode", [sender, "latest"])
        return code is not None and len(code) > 2

    def _target_has_code(self, net: Network, call_data: str) -> bool:
        """Whether this call needs a contract that exists on this chain.

        A call to an address with no code succeeds and does nothing. The EVM
        reports status 1, the bundler reports success, and the settlement looks
        mined while no value moved. That is the most dangerous failure this
        engine can produce, because it is indistinguishable from a real payout
        in every receipt field.

        The check applies only when the call carries inner calldata. A plain
        value transfer to an EOA has an empty payload and no code to find, so
        requiring code there would reject every ordinary payout.
        """
        try:
            raw = bytes.fromhex(call_data[2:] if call_data.startswith("0x") else call_data)
        except ValueError:
            return True
        if len(raw) < 4:
            return True
        try:
            target, _value, inner = decode(
                ["address", "uint256", "bytes"], raw[4:], strict=False
            )
        except Exception:  # noqa: BLE001 - unknown shape, do not block settlement
            return True
        if not inner:
            return True
        if int(target, 16) == 0:
            return True
        code = _read_state(net.rpc_url, "eth_getCode", [target, "latest"])
        # None means no endpoint could answer. Refusing to settle is the safe
        # reading: settling on an unread chain risks the no-op this guards.
        return code is not None and len(code) > 2

    # -- encoding ----------------------------------------------------------

    @staticmethod
    def user_op_hash(op: dict, chain_id: int) -> bytes:
        """The real ERC-4337 v0.6 userOp hash.

        Covers every field the entry point verifies, including all gas limits
        and the paymaster data. Omitting any of them yields a signature the
        account rejects.
        """
        packed = abi_encode(
            [
                "address", "uint256", "bytes32", "bytes32",
                "uint256", "uint256", "uint256", "uint256", "uint256", "bytes32",
            ],
            [
                op["sender"],
                int(op["nonce"], 16),
                keccak(bytes.fromhex(_strip0x(op["initCode"]))),
                keccak(bytes.fromhex(_strip0x(op["callData"]))),
                int(op["callGasLimit"], 16),
                int(op["verificationGasLimit"], 16),
                int(op["preVerificationGas"], 16),
                int(op["maxFeePerGas"], 16),
                int(op["maxPriorityFeePerGas"], 16),
                keccak(bytes.fromhex(_strip0x(op["paymasterAndData"]))),
            ],
        )
        return keccak(
            abi_encode(
                ["bytes32", "address", "uint256"],
                [keccak(packed), ENTRY_POINT, chain_id],
            )
        )

    def _sign_op(self, op: dict, chain_id: int) -> str:
        digest = self.user_op_hash(op, chain_id)
        signed = Account.sign_message(
            encode_defunct(hexstr=_hex(digest)), private_key=self.session_key
        )
        return _hex(signed.signature)

    @staticmethod
    def call_data(beneficiary: str, value_wei: int, inner: bytes = b"") -> str:
        """``SimpleAccount.execute(to, value, data)`` over the locked address."""
        return _hex(
            _selector("execute(address,uint256,bytes)")
            + abi_encode(["address", "uint256", "bytes"], [beneficiary, value_wei, inner])
        )

    def token_balance_of(self, net: Network, token: str, address: str) -> int:
        """ERC20 balance in base units, read from the node.

        The value leg settles in a token, not in native ETH, so the funding
        check has to read the token. Reading native balance here would report
        zero for an account that is holding the value it is about to deliver.
        """
        data = _hex(
            keccak(text="balanceOf(address)")[:4]
            + abi_encode(["address"], [address])
        )
        raw = _read_state(
            net.rpc_url, "eth_call", [{"to": token, "data": data}, "latest"],
            timeout=15,
        )
        try:
            return int(str(raw), 16)
        except (TypeError, ValueError):
            return 0

    def token_allowance(self, net: Network, token: str, owner: str, spender: str) -> int:
        """How much ``spender`` may pull from ``owner``, in base units.

        A lock settlement is a ``transferFrom``, so this is the gate that
        decides whether the pull can succeed at all.
        """
        data = _hex(
            keccak(text="allowance(address,address)")[:4]
            + abi_encode(["address", "address"], [owner, spender])
        )
        raw = _read_state(
            net.rpc_url, "eth_call", [{"to": token, "data": data}, "latest"],
            timeout=15,
        )
        try:
            return int(str(raw), 16)
        except (TypeError, ValueError):
            return 0

    def _build_op(self, net: Network, call_data: str) -> dict | None:
        sender = self.account_for(net)
        if not sender:
            return None
        deployed = self._deployed(net, sender)
        return {
            "sender": sender,
            "nonce": hex(self._nonce(net, sender)),
            # initCode is only legal while the account does not exist.
            "initCode": "0x" if deployed else self._init_code(),
            "callData": call_data,
            "callGasLimit": hex(DEFAULT_CALL_GAS),
            "verificationGasLimit": hex(
                DEFAULT_VERIFICATION_GAS if deployed else DEPLOY_VERIFICATION_GAS
            ),
            "preVerificationGas": hex(DEFAULT_PRE_VERIFICATION_GAS),
            "maxFeePerGas": "0x1",
            "maxPriorityFeePerGas": "0x1",
            # Placeholder until the paymaster answers; included in the hash.
            "paymasterAndData": "0x" + "00" * 52,
            "signature": "0x" + "00" * 65,
        }

    # -- paymaster ---------------------------------------------------------

    def _sponsor(self, net: Network, op: dict) -> dict | None:
        """Ask the paymasters for sponsorship. First real offer wins."""
        urls = [net.paymaster_url] + list(SETTINGS.extra_paymasters)
        for url in urls:
            if not url:
                continue
            data = _rpc(
                url,
                "pm_sponsorUserOperation",
                [op, ENTRY_POINT, {"chainId": hex(net.chain_id)}],
                timeout=20,
            )
            if not data:
                continue
            result = data.get("result")
            # ERC-7677 returns {paymaster, paymasterData, ...}; older servers
            # return a flat object with paymasterAndData.
            if isinstance(result, dict) and (
                "paymasterAndData" in result or "paymaster" in result
            ):
                return result
        return None

    @staticmethod
    def _apply_sponsorship(op: dict, offer: dict) -> None:
        for key in (
            "paymasterAndData",
            "callGasLimit",
            "verificationGasLimit",
            "preVerificationGas",
            "maxFeePerGas",
            "maxPriorityFeePerGas",
        ):
            if key in offer and offer[key]:
                op[key] = offer[key]
        if "paymasterAndData" not in offer and offer.get("paymaster"):
            data = _strip0x(str(offer.get("paymasterData", "0x")))
            op["paymasterAndData"] = str(offer["paymaster"]) + data

    # -- self-signed native execution --------------------------------------

    def native_balance(self, net: Network, address: str | None = None) -> int:
        """Native coin balance of the session EOA, in wei."""
        target = address or self._session_address
        raw = _read_state(net.rpc_url, "eth_getBalance", [target, "latest"])
        try:
            return int(str(raw), 16)
        except (TypeError, ValueError):
            return 0

    def native_gas_price(self, net: Network) -> int:
        data = _rpc(net.rpc_url, "eth_gasPrice", [])
        raw = (data or {}).get("result")
        try:
            return int(raw, 16) if raw else 0
        except (TypeError, ValueError):
            return 0

    def can_self_sign(self, net: Network, gas_limit: int) -> bool:
        """Whether the session EOA can carry a transaction on its own balance.

        This decides between asking a paymaster and paying directly. A
        self-signed transaction needs no paymaster, no EntryPoint and no
        sponsorship quota, so whenever the account holds gas this is strictly
        the better path.
        """
        price = self.native_gas_price(net)
        return price > 0 and self.native_balance(net) >= price * gas_limit

    def settle_native(self, net: Network, to: str, data_hex: str,
                      gas_limit: int = 300_000) -> GasSettlement:
        """Execute the settlement as a plain signed transaction from the EOA.

        No paymaster, no bundler, no userOperation. The session key signs an
        ordinary transaction addressed straight at ``to`` and the node charges
        the EOA's own balance. This is what makes the engine self-sponsored
        once a single unit of gas exists: the dependency on third-party
        sponsorship disappears instead of being retried.

        ``to``/``data_hex`` are the direct call, not the account-abstracted
        wrapper. Sending the wrapper here would address the beneficiary with
        calldata meant for the lock, which mines cleanly and pays nobody.
        """
        started = time.time()
        price = self.native_gas_price(net)
        balance = self.native_balance(net)
        if price <= 0:
            return GasSettlement(
                status="unsettled", network=net.name, chain_id=net.chain_id,
                reason=f"{net.name}: no_gas_price", gas_paid_by="none",
            )
        if balance < price * gas_limit:
            return GasSettlement(
                status="unsettled", network=net.name, chain_id=net.chain_id,
                reason=(
                    f"{net.name}: native_shortfall("
                    f"have={balance},need={price * gas_limit})"
                ),
                gas_paid_by="none",
            )

        tx = {
            # eth_account rejects a lower-case address, so it is checksummed
            # at the point of signing.
            "to": to_checksum_address(to),
            "value": 0,
            "data": bytes.fromhex(_strip0x(data_hex)),
            "gas": gas_limit,
            "gasPrice": price,
            "nonce": self._eoa_nonce(net),
            "chainId": net.chain_id,
        }
        try:
            signed = Account.sign_transaction(tx, self.session_key)
            raw = signed.raw_transaction.hex()
        except Exception as exc:  # noqa: BLE001 - signing is local and total
            return GasSettlement(
                status="unsettled", network=net.name, chain_id=net.chain_id,
                reason=f"{net.name}: sign_failed:{type(exc).__name__}",
                gas_paid_by="none",
            )
        if not raw.startswith("0x"):
            raw = "0x" + raw

        data = _rpc(net.rpc_url, "eth_sendRawTransaction", [raw], timeout=25)
        tx_hash = (data or {}).get("result")
        if not isinstance(tx_hash, str):
            err = ((data or {}).get("error") or {}).get("message", "")
            return GasSettlement(
                status="unsettled", network=net.name, chain_id=net.chain_id,
                reason=f"{net.name}: rejected:{str(err)[:120]}",
                gas_paid_by="none",
                elapsed_ms=(time.time() - started) * 1000.0,
            )

        receipt = self._tx_receipt(net, tx_hash)
        cost = price * gas_limit
        if receipt is None:
            return GasSettlement(
                status="submitted", network=net.name, chain_id=net.chain_id,
                tx_hash=tx_hash,
                explorer_url=f"{net.explorer_base}{tx_hash}",
                account=self._session_address,
                gas_paid_by="native",
                actual_gas_cost_wei=cost,
                reason="accepted_by_rpc;receipt_pending",
                elapsed_ms=(time.time() - started) * 1000.0,
            )

        success = int(str(receipt.get("status", "0x0")), 16) == 1
        used = int(str(receipt.get("gasUsed", "0x0")), 16)
        return GasSettlement(
            status="executed" if success else "reverted",
            network=net.name,
            chain_id=net.chain_id,
            tx_hash=tx_hash,
            explorer_url=f"{net.explorer_base}{tx_hash}",
            account=self._session_address,
            gas_paid_by="native",
            actual_gas_cost_wei=used * price if used else cost,
            block_number=int(str(receipt.get("blockNumber", "0x0")), 16),
            reason="mined" if success else "mined_but_reverted",
            elapsed_ms=(time.time() - started) * 1000.0,
        )

    def _eoa_nonce(self, net: Network) -> int:
        raw = _read_state(net.rpc_url, "eth_getTransactionCount",
                          [self._session_address, "pending"])
        try:
            return int(str(raw), 16)
        except (TypeError, ValueError):
            return 0

    # -- self-funded execution (no paymaster, no bundler) ------------------

    def entrypoint_deposit(self, net: Network, account: str | None = None) -> int:
        """ETH the EntryPoint already holds for this account, in wei."""
        target = account or self.account_for(net)
        if not target:
            return 0
        data = _hex(
            _selector("balanceOf(address)") + abi_encode(["address"], [target])
        )
        raw = _read_state(net.rpc_url, "eth_call",
                          [{"to": ENTRY_POINT, "data": data}, "latest"])
        try:
            return int(str(raw), 16)
        except (TypeError, ValueError):
            return 0

    def _send_eoa_tx(self, net: Network, to: str, data: bytes, gas: int,
                     value: int = 0) -> tuple[str, str]:
        """Sign and broadcast one transaction from the session EOA.

        Returns ``(tx_hash, "")`` on acceptance or ``("", reason)`` on refusal.
        Used by both legs of self-funded execution, so the signing and
        broadcast logic exists once.
        """
        price = self.native_gas_price(net)
        if price <= 0:
            return "", f"{net.name}:no_gas_price"
        tx = {
            "to": to_checksum_address(to),
            "value": value,
            "data": data,
            "gas": gas,
            "gasPrice": price,
            "nonce": self._eoa_nonce(net),
            "chainId": net.chain_id,
        }
        try:
            raw = Account.sign_transaction(tx, self.session_key).raw_transaction.hex()
        except Exception as exc:  # noqa: BLE001 - signing is local and total
            return "", f"{net.name}:sign_failed:{type(exc).__name__}"
        if not raw.startswith("0x"):
            raw = "0x" + raw
        out = _rpc(net.rpc_url, "eth_sendRawTransaction", [raw], timeout=25)
        tx_hash = (out or {}).get("result")
        if not isinstance(tx_hash, str):
            err = ((out or {}).get("error") or {}).get("message", "")
            return "", f"{net.name}:rejected:{str(err)[:110]}"
        return tx_hash, ""

    def _wait_success(self, net: Network, tx_hash: str) -> tuple[bool, str]:
        receipt = self._tx_receipt(net, tx_hash)
        if receipt is None:
            return False, f"receipt_pending:{tx_hash}"
        ok = int(str(receipt.get("status", "0x0")), 16) == 1
        return ok, "ok" if ok else f"reverted:{tx_hash}"

    def settle_self_funded(self, net: Network, call_data: str,
                           gas_limit: int | None = None) -> GasSettlement:
        """Execute a settlement paying gas from the account's own ETH.

        This is the path that removes both dependencies at once:

        * **no paymaster** — the EntryPoint is handed a deposit, so the
          operation is prefunded by the account rather than by a sponsor;
        * **no bundler** — ``handleOps`` is an ordinary function on the
          EntryPoint, so the session key can call it in a plain transaction.
          A bundler exists to relay *someone else's* operation; when the
          account pays for itself there is nobody to relay it.

        Both steps are signed by the session key and broadcast straight to the
        node. Once the account holds enough ETH for one operation, this leg
        succeeds every time without asking anyone for permission, which is what
        "self-sponsored" has to mean to be worth claiming.
        """
        started = time.time()
        account = self.account_for(net)
        if not account:
            return GasSettlement(
                status="unsettled", network=net.name, chain_id=net.chain_id,
                reason=f"{net.name}: no_account", gas_paid_by="none",
            )

        price = self.native_gas_price(net)
        if price <= 0:
            return GasSettlement(
                status="unsettled", network=net.name, chain_id=net.chain_id,
                reason=f"{net.name}: no_gas_price", gas_paid_by="none",
            )

        # Build the operation the account would run: no paymaster, so the
        # EntryPoint draws the prefund from the account's own deposit.
        op = self._build_op(net, call_data)
        if op is None:
            return GasSettlement(
                status="unsettled", network=net.name, chain_id=net.chain_id,
                reason=f"{net.name}: no_op", gas_paid_by="none",
            )
        op["maxFeePerGas"] = hex(price)
        op["maxPriorityFeePerGas"] = hex(price)
        op["paymasterAndData"] = "0x"
        op["signature"] = self._sign_op(op, net.chain_id)

        # The outer transaction must be able to run the whole operation. The
        # EntryPoint enforces the op's own limits inside it, so an outer limit
        # below verificationGas + callGas fails with AA95 out of gas even
        # though every inner limit was met.
        inner = (
            int(op["verificationGasLimit"], 16)
            + int(op["callGasLimit"], 16)
            + int(op["preVerificationGas"], 16)
        )
        if gas_limit is None or gas_limit < inner:
            gas_limit = inner + HANDLE_OPS_OVERHEAD_GAS

        required = self.required_prefund(op, price)
        deposit = self.entrypoint_deposit(net, account)
        log: list[str] = [f"{net.name}: prefund_needed={required} deposit={deposit}"]

        # The EOA must hold the prefund plus the cost of the transactions that
        # carry it. Both are counted, because a deposit that leaves nothing for
        # the handleOps call itself strands the operation.
        eoa_balance = self.native_balance(net)
        overhead = price * (DEPOSIT_TX_GAS + gas_limit)
        if eoa_balance < overhead + max(required - deposit, 0):
            return GasSettlement(
                status="unsettled", network=net.name, chain_id=net.chain_id,
                reason=(
                    f"{net.name}: native_shortfall(have={eoa_balance},"
                    f"need={overhead + max(required - deposit, 0)})"
                ),
                gas_paid_by="none", attempts=log,
            )

        # Step 1: top the EntryPoint deposit up if it cannot cover the prefund.
        if deposit < required:
            top_up = required - deposit
            data = _selector("depositTo(address)") + abi_encode(
                ["address"], [account]
            )
            tx, err = self._send_eoa_tx(net, ENTRY_POINT, data, DEPOSIT_TX_GAS,
                                        value=top_up)
            if not tx:
                log.append(f"deposit:{err}")
                return GasSettlement(
                    status="unsettled", network=net.name, chain_id=net.chain_id,
                    reason=err, gas_paid_by="none", attempts=log,
                )
            ok, why = self._wait_success(net, tx)
            log.append(f"deposit:{tx[:18]}:{why}")
            if not ok:
                return GasSettlement(
                    status="unsettled", network=net.name, chain_id=net.chain_id,
                    reason=f"deposit_failed:{why}", gas_paid_by="none",
                    attempts=log,
                )

        # Step 2: hand the operation to the EntryPoint directly.
        data = _selector("handleOps((address,uint256,bytes,bytes,uint256,uint256,"
                         "uint256,uint256,uint256,bytes,bytes)[],address)") + \
            abi_encode(
                ["(address,uint256,bytes,bytes,uint256,uint256,uint256,"
                 "uint256,uint256,bytes,bytes)[]", "address"],
                [[(
                    op["sender"],
                    int(op["nonce"], 16),
                    bytes.fromhex(_strip0x(op["initCode"])),
                    bytes.fromhex(_strip0x(op["callData"])),
                    int(op["callGasLimit"], 16),
                    int(op["verificationGasLimit"], 16),
                    int(op["preVerificationGas"], 16),
                    int(op["maxFeePerGas"], 16),
                    int(op["maxPriorityFeePerGas"], 16),
                    bytes.fromhex(_strip0x(op["paymasterAndData"])),
                    bytes.fromhex(_strip0x(op["signature"])),
                )], SETTINGS.beneficiary],
            )
        tx, err = self._send_eoa_tx(net, ENTRY_POINT, data, gas_limit)
        if not tx:
            log.append(f"handleOps:{err}")
            return GasSettlement(
                status="unsettled", network=net.name, chain_id=net.chain_id,
                reason=err, gas_paid_by="none", attempts=log,
                elapsed_ms=(time.time() - started) * 1000.0,
            )

        receipt = self._tx_receipt(net, tx)
        if receipt is None:
            return GasSettlement(
                status="submitted", network=net.name, chain_id=net.chain_id,
                tx_hash=tx, explorer_url=f"{net.explorer_base}{tx}",
                account=account, gas_paid_by="self_deposit",
                reason="handleOps_accepted;receipt_pending", attempts=log,
                elapsed_ms=(time.time() - started) * 1000.0,
            )

        success = int(str(receipt.get("status", "0x0")), 16) == 1
        used = int(str(receipt.get("gasUsed", "0x0")), 16)
        log.append(f"handleOps:{tx[:18]}:{'mined' if success else 'reverted'}")
        return GasSettlement(
            status="executed" if success else "reverted",
            network=net.name, chain_id=net.chain_id, tx_hash=tx,
            explorer_url=f"{net.explorer_base}{tx}", account=account,
            gas_paid_by="self_deposit",
            actual_gas_cost_wei=used * price if used else price * gas_limit,
            block_number=int(str(receipt.get("blockNumber", "0x0")), 16),
            reason="mined" if success else "mined_but_reverted",
            attempts=log, elapsed_ms=(time.time() - started) * 1000.0,
        )

    @staticmethod
    def required_prefund(op: dict, price: int) -> int:
        """The ETH the EntryPoint will hold against one operation.

        ERC-4337 prefunds ``(verification + call) * maxFeePerGas`` plus the
        preVerificationGas penalty. Underestimating it makes ``handleOps``
        revert with AA51, which reads like a bad signature but is really an
        arithmetic shortfall.
        """
        gas = (
            int(op["verificationGasLimit"], 16)
            + int(op["callGasLimit"], 16)
            + int(op["preVerificationGas"], 16)
        )
        return gas * price

    def _tx_receipt(self, net: Network, tx_hash: str) -> dict | None:
        deadline = time.time() + SETTINGS.receipt_timeout_seconds
        while time.time() < deadline:
            data = _rpc(net.rpc_url, "eth_getTransactionReceipt", [tx_hash],
                        timeout=10)
            receipt = (data or {}).get("result")
            if isinstance(receipt, dict):
                return receipt
            time.sleep(SETTINGS.receipt_poll_seconds)
        return None

    # -- broadcast ---------------------------------------------------------

    def _send(self, net: Network, op: dict) -> tuple[str, str] | tuple[None, str]:
        """Send to the bundler; returns (userOpHash, bundler_url_or_reason)."""
        bundlers = [SETTINGS.public_bundler_url, net.paymaster_url]
        bundlers = [b for b in dict.fromkeys(bundlers) if b]
        last = "no_bundler_answered"
        for url in bundlers:
            data = _rpc(url, "eth_sendUserOperation", [op, ENTRY_POINT], timeout=25)
            if data and isinstance(data.get("result"), str):
                return data["result"], url
            if data and isinstance(data.get("error"), dict):
                last = str(data["error"].get("message", ""))[:160]
        return None, last

    def _receipt(self, net: Network, user_op_hash: str) -> dict | None:
        """Poll for the userOperation receipt, then resolve the nested tx hash."""
        deadline = time.time() + SETTINGS.receipt_timeout_seconds
        while time.time() < deadline:
            for url in (net.paymaster_url, SETTINGS.public_bundler_url, net.rpc_url):
                if not url:
                    continue
                data = _rpc(url, "eth_getUserOperationReceipt", [user_op_hash], timeout=10)
                result = (data or {}).get("result")
                if isinstance(result, dict) and result.get("receipt"):
                    return result
            time.sleep(SETTINGS.receipt_poll_seconds)
        return None

    # -- the flywheel ------------------------------------------------------

    def _networks_by_cost(self) -> list[Network]:
        """The configured networks, cheapest measured gas first.

        Network-agnostic execution: the engine should settle on the chain that
        charges least, not on whichever one happens to be listed first. Price is
        read live, and a chain that will not answer sorts last rather than
        disappearing, so it is still attempted when nothing cheaper works.
        """
        nets = _networks()
        priced: list[tuple[int, int, Network]] = []
        for index, net in enumerate(nets):
            try:
                data = _rpc(net.rpc_url, "eth_gasPrice", [], timeout=8)
                raw = (data or {}).get("result")
                price = int(raw, 16) if raw else 0
            except Exception:  # noqa: BLE001 - an unreadable price is not fatal
                price = 0
            priced.append((price or 1 << 62, index, net))
        priced.sort(key=lambda item: (item[0], item[1]))
        return [net for _, _, net in priced]

    def settle(self, call_data: str, attempts: list[str] | None = None,
               direct_to: str = "", direct_data: str = "") -> GasSettlement:
        """Settle one call, paying gas from the engine's own resources first.

        The order is the whole point of the self-sponsored design. Gas is an
        internal asset: the engine spends what it holds before it asks anyone.

        1. **self-funded** — deposit into the EntryPoint and call ``handleOps``
           directly. No paymaster, no bundler, no permission.
        2. **self-signed native** — a plain transaction carrying the call. The
           cheapest of all, used when the call needs no account abstraction.
        3. **external sponsor** — only when ``SYNTH_ALLOW_EXTERNAL_SPONSOR`` is
           set. Off by default: the engine must not depend on a third party's
           quota, and a sponsor is a bridge to the first native unit, not a
           permanent source of gas.

        Each network is tried in turn and the first mined settlement wins. The
        chains are ordered by measured cost, so the cheapest payable one is
        reached first rather than whichever happened to be configured first.
        """
        started = time.time()
        log = attempts if attempts is not None else []
        accepted: GasSettlement | None = None

        for net in self._networks_by_cost():
            if not self._target_has_code(net, call_data):
                # Nothing to call here. Settling anyway would produce a mined
                # transaction that moved nothing, so this chain is skipped
                # rather than allowed to report a phantom success.
                log.append(f"{net.name}: target_absent")
                continue

            # -- rung 1: the account pays for itself, no bundler involved --
            self_funded = self.settle_self_funded(net, call_data)
            if self_funded.mined:
                log.extend(self_funded.attempts)
                log.append(f"{net.name}: mined:{self_funded.tx_hash[:18]}")
                self_funded.attempts = log
                self_funded.elapsed_ms = (time.time() - started) * 1000.0
                return self_funded
            log.extend(self_funded.attempts[-2:])

            # -- rung 2: a plain signed transaction, when that suffices -----
            # The direct target is the lock, which is where a settlement's
            # calldata is addressed. The AA wrapper in ``call_data`` is for the
            # account, not for this leg, so it is not used here.
            if direct_to and direct_data and self.can_self_sign(net, NATIVE_TX_GAS):
                native = self.settle_native(
                    net, direct_to, direct_data, NATIVE_TX_GAS
                )
                if native.mined:
                    log.append(f"{net.name}: native_mined:{native.tx_hash[:18]}")
                    native.attempts = log
                    native.elapsed_ms = (time.time() - started) * 1000.0
                    return native
                log.append(f"{net.name}: native:{native.reason[:60]}")
                if native.status == "submitted":
                    accepted = accepted or native

            # -- rung 3: an external sponsor, only when explicitly allowed --
            if not SETTINGS.allow_external_sponsor:
                log.append(f"{net.name}: external_sponsor_disabled")
                continue

            op = self._build_op(net, call_data)
            if op is None:
                log.append(f"{net.name}: no_account")
                continue

            # Phase 1: sign, so the paymaster can validate the operation.
            op["signature"] = self._sign_op(op, net.chain_id)
            offer = self._sponsor(net, op)
            if offer is None:
                log.append(f"{net.name}: sponsor_declined")
                continue
            self._apply_sponsorship(op, offer)

            # Phase 2: re-sign over the operation the paymaster just changed.
            op["signature"] = self._sign_op(op, net.chain_id)
            user_op_hash, where = self._send(net, op)
            if not user_op_hash:
                log.append(f"{net.name}: rejected:{where}")
                continue

            paymaster = str(op.get("paymasterAndData", ""))[:42]
            receipt = self._receipt(net, user_op_hash)
            if receipt is None:
                log.append(f"{net.name}: accepted_unmined")
                if accepted is None:
                    accepted = GasSettlement(
                        status="submitted",
                        network=net.name,
                        chain_id=net.chain_id,
                        user_op_hash=user_op_hash,
                        explorer_url=f"{net.explorer_base}{user_op_hash}",
                        paymaster=paymaster,
                        account=op["sender"],
                        gas_paid_by="paymaster",
                        reason=f"accepted_by:{where};receipt_pending",
                        attempts=log,
                    )
                continue

            inner = receipt["receipt"]
            tx_hash = str(inner.get("transactionHash", ""))
            success = bool(receipt.get("success", True))
            log.append(f"{net.name}: mined:{tx_hash[:18]}")
            return GasSettlement(
                status="executed" if success else "reverted",
                network=net.name,
                chain_id=net.chain_id,
                tx_hash=tx_hash,
                user_op_hash=user_op_hash,
                explorer_url=f"{net.explorer_base}{tx_hash}",
                paymaster=str(receipt.get("paymaster") or paymaster),
                account=str(receipt.get("sender") or op["sender"]),
                gas_paid_by="paymaster",
                actual_gas_cost_wei=int(str(receipt.get("actualGasCost", "0x0")), 16)
                if str(receipt.get("actualGasCost", "")).startswith("0x")
                else 0,
                block_number=int(str(inner.get("blockNumber", "0x0")), 16)
                if str(inner.get("blockNumber", "")).startswith("0x")
                else 0,
                reason="mined" if success else "mined_but_reverted",
                attempts=log,
                elapsed_ms=(time.time() - started) * 1000.0,
            )

        if accepted is not None:
            accepted.elapsed_ms = (time.time() - started) * 1000.0
            return accepted

        return GasSettlement(
            status="unsettled",
            reason="no_network_settled:" + ";".join(log[-3:]) if log else "no_network_settled",
            attempts=log,
            elapsed_ms=(time.time() - started) * 1000.0,
        )


def verify_transfer(
    net: Network, tx_hash: str, beneficiary: str
) -> tuple[bool, str]:
    """Confirm a mined transaction exists and its success status is 1.

    This is the delivery check: a hash alone proves nothing about whether the
    operation actually ran. Reading the receipt back from the node closes that
    gap.
    """
    data = _rpc(net.rpc_url, "eth_getTransactionReceipt", [tx_hash], timeout=20)
    receipt = (data or {}).get("result")
    if not isinstance(receipt, dict):
        return False, "receipt_not_found"
    status = int(str(receipt.get("status", "0x0")), 16)
    if status != 1:
        return False, f"tx_reverted:{tx_hash}"
    return True, f"confirmed_in_block:{int(str(receipt.get('blockNumber', '0x0')), 16)}"
