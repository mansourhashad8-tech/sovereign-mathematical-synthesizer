"""Proof that the engine executes without any external gas sponsor.

These tests run the production code path against a real EVM (eth-tester)
exposed over JSON-RPC. Nothing is mocked: the same ``GasFlywheel``,
``InternalGasTreasury`` and ``GasValueLock`` bytecode that run against a public
network run here. What the test controls is the account's native balance, which
is the honest variable — the engine either pays for its own gas out of that
balance or it does not.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_account import Account
from eth_utils import keccak

from synthesizer import config as config_module
from synthesizer.config import SETTINGS
from synthesizer.gastreasury import InternalGasTreasury, SETTLE_TOTAL_GAS
from synthesizer.gasflywheel import GasFlywheel, Network

from .localchain import Chain

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


def _bytecode(name: str) -> str:
    return (CONTRACTS / f"{name}.bin").read_text().strip()


@pytest.fixture()
def chain():
    c = Chain()
    url, server = c.serve()
    c.url = url
    yield c
    server.shutdown()


@pytest.fixture()
def key(chain):
    """A real private key that the local EVM can verify signatures for."""
    return chain.session_key


def _net(chain, name="local"):
    return Network(name=name, chain_id=chain.chain_id, rpc_url=chain.url,
                   explorer_base="http://local/tx/", paymaster_url="")


def test_treasury_reports_shortfall_when_account_holds_nothing(chain, key):
    """With no native coin the treasury must say exactly how much is missing.

    This is the honest report the old design could not make: it names the
    shortfall in wei and the network, instead of claiming no sponsor exists.
    """
    # A genuinely empty address: eth-tester pre-funds its own accounts, so the
    # shortfall has to be measured on one it has never seen.
    eoa = Account.create().address
    treasury = InternalGasTreasury()
    net = _net(chain)

    q = treasury.quote(net, eoa, force=True)

    assert q.can_pay is False
    assert q.leg == ""
    assert q.native_wei == 0
    assert q.cost_wei > 0
    assert q.shortfall_wei == q.cost_wei
    assert q.bootstrap_wei == q.cost_wei
    assert "no_gas_leg" in q.reason


def test_treasury_chooses_native_leg_once_funded(chain, key):
    """One unit of gas flips the treasury to a payable leg."""
    eoa = Account.from_key(key).address
    chain.fund(eoa, 10**18)

    treasury = InternalGasTreasury()
    q = treasury.quote(_net(chain), eoa, force=True)

    assert q.can_pay is True
    assert q.leg == "native"
    assert q.shortfall_wei == 0
    assert q.bootstrap_wei == 0


def test_native_settlement_executes_and_moves_value(chain, key):
    """A plain signed transaction settles the lock and pays the beneficiary.

    This is the strongest claim the module makes: with native gas present, the
    engine needs no paymaster, no bundler and no sponsorship. The assertion on
    the beneficiary's token balance is what proves value actually arrived,
    rather than a transaction merely being mined.
    """
    token = chain.deploy(_bytecode("TestValueToken"))
    lock = chain.deploy(_bytecode("GasValueLock"))
    eoa = Account.from_key(key).address
    chain.fund(eoa, 10**18)

    # Fund the payer (the EOA itself) with the value token and approve the lock.
    tester = chain.tester
    tester.send_transaction({"from": tester.get_accounts()[0], "to": token,
                             "data": "0x40c10f19" + eoa[2:].rjust(64, "0")
                             + hex(50_000_000)[2:].rjust(64, "0"),
                             "gas": 200_000})
    tester.send_transaction({"from": eoa, "to": token,
                             "data": "0x095ea7b3" + lock[2:].rjust(64, "0")
                             + hex(50_000_000)[2:].rjust(64, "0"),
                             "gas": 200_000})

    beneficiary = SETTINGS.beneficiary
    before = _token_balance(chain, token, beneficiary)

    flywheel = GasFlywheel(key, eoa)
    intent_id = keccak(text="proof-intent-1")
    inner = _settle_calldata(intent_id, token, beneficiary)
    result = flywheel.settle_native(
        _net(chain), lock, "0x" + inner.hex(), gas_limit=300_000
    )

    assert result.mined, result.reason
    assert result.gas_paid_by == "native"
    assert result.tx_hash.startswith("0x")
    after = _token_balance(chain, token, beneficiary)
    assert after > before, f"beneficiary did not gain value: {before} -> {after}"


def test_settle_never_asks_a_paymaster_when_native_gas_exists(chain, key,
                                                             monkeypatch):
    """The paymaster rung must not be reached when the account can pay.

    Sponsorship used to be tried first, so a dead quota broke every settlement
    even when the account held gas. This asserts the ordering directly: the
    paymaster URL is unreachable, yet the settlement still succeeds.
    """
    token = chain.deploy(_bytecode("TestValueToken"))
    lock = chain.deploy(_bytecode("GasValueLock"))
    eoa = Account.from_key(key).address
    chain.fund(eoa, 10**18)

    tester = chain.tester
    tester.send_transaction({"from": tester.get_accounts()[0], "to": token,
                             "data": "0x40c10f19" + eoa[2:].rjust(64, "0")
                             + hex(50_000_000)[2:].rjust(64, "0"), "gas": 200_000})
    tester.send_transaction({"from": eoa, "to": token,
                             "data": "0x095ea7b3" + lock[2:].rjust(64, "0")
                             + hex(50_000_000)[2:].rjust(64, "0"), "gas": 200_000})

    flywheel = GasFlywheel(key, eoa)
    # A deliberately dead paymaster: if the code tried it first it would fail.
    net = Network(name="local", chain_id=chain.chain_id, rpc_url=chain.url,
                  explorer_base="http://local/tx/",
                  paymaster_url="http://127.0.0.1:1/dead")
    monkeypatch.setattr("synthesizer.gasflywheel._networks", lambda: [net])
    calldata = "0x" + _settle_calldata(
        keccak(text="proof-intent-2"), token, SETTINGS.beneficiary
    ).hex()

    result = flywheel.settle(calldata, attempts=[], direct_to=lock,
                             direct_data=calldata)

    assert result.mined, f"settlement failed: {result.reason} {result.attempts}"
    assert result.gas_paid_by in ("native", "self_deposit")
    assert "no_sponsorship" not in result.reason


def test_treasury_summary_names_the_bootstrap(chain, key):
    """An unfunded account is told the exact wei needed, not that it failed."""
    eoa = Account.create().address
    treasury = InternalGasTreasury()
    nets = [_net(chain)]

    summary = treasury.summary(nets, lambda n: eoa)

    assert summary["payable_networks"] == []
    assert summary["chosen"] == ""
    assert summary["bootstrap_wei"] > 0
    assert summary["bootstrap_network"] == "local"
    assert summary["quotes"][0]["leg"] == ""
    assert summary["quotes"][0]["shortfall_wei"] == summary["bootstrap_wei"]


def test_prefund_covers_entrypoint_arithmetic(chain, key):
    """The prefund must cover verification + call + preVerification gas."""
    flywheel = GasFlywheel(key, Account.from_key(key).address)
    op = {
        "verificationGasLimit": hex(200_000),
        "callGasLimit": hex(200_000),
        "preVerificationGas": hex(50_000),
    }
    assert flywheel.required_prefund(op, 10) == 450_000 * 10
    assert SETTLE_TOTAL_GAS == 450_000


# -- helpers ---------------------------------------------------------------


def _token_balance(chain: Chain, token: str, owner: str) -> int:
    out = chain.tester.call({
        "from": chain.tester.get_accounts()[0],
        "to": token,
        "data": "0x70a08231" + owner[2:].rjust(64, "0"),
        "gas": 100_000,
    })
    return int(out, 16) if out and out != "0x" else 0


def _settle_calldata(intent_id: bytes, token: str, relayer: str) -> bytes:
    """``settle(intentId, token, relayer, netProfit, fee, bps, ok, to)``."""
    from eth_abi import encode

    net_profit = 1_000_000      # 1.0 token
    fee = 100_000               # 0.1 token
    bps = 500
    return (
        keccak(text="settle(bytes32,address,address,uint256,uint256,uint256,bool,address)")[:4]
        + encode(
            ["bytes32", "address", "address", "uint256", "uint256", "uint256", "bool", "address"],
            [intent_id, token, relayer, net_profit, fee, bps, True, SETTINGS.beneficiary],
        )
    )
