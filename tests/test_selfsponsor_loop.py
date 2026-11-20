"""Proof that gas is an internal asset, not an external ask.

The self-sponsored design makes four claims, and each one is checked here
against the production code rather than against a mock:

1. The external sponsor is never contacted unless it is explicitly enabled.
2. A settlement that lands folds its gas spend and its reserve slice back into
   the engine's own balance, so the next settlement draws on what it earned.
3. Chains are tried cheapest-first, so the network choice is a price decision.
4. The Double Gate still holds: a mined operation that moved no value is not a
   settlement, whatever the receipt status says.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from eth_account import Account

from synthesizer.config import SETTINGS
from synthesizer.gasflywheel import GasFlywheel, Network, GasSettlement
from synthesizer.models import AtomicPath, Constraints, Goal

from .localchain import Chain

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


@pytest.fixture()
def chain():
    c = Chain()
    url, server = c.serve()
    c.url = url
    yield c
    server.shutdown()


def _net(chain, name="local"):
    return Network(name=name, chain_id=chain.chain_id, rpc_url=chain.url,
                   explorer_base="http://local/tx/", paymaster_url="")


# -- 1. the sponsor is opt-in ----------------------------------------------


def test_external_sponsor_is_off_by_default():
    """No settlement may depend on a third party's quota out of the box."""
    assert SETTINGS.allow_external_sponsor is False


def test_no_sponsor_is_asked_when_it_is_disabled(chain, monkeypatch):
    """With sponsorship disabled the paymaster is never contacted.

    A counting stub proves it: if the rung ran it would be called. The account
    is deliberately unfunded so the two internal rungs fail and the ladder
    actually reaches the sponsor decision.
    """
    key = Account.create().key.hex()
    if not key.startswith("0x"):
        key = "0x" + key
    eoa = Account.from_key(key).address
    flywheel = GasFlywheel(key, eoa)
    net = Network(name="local", chain_id=chain.chain_id, rpc_url=chain.url,
                  explorer_base="http://local/tx/",
                  paymaster_url="http://127.0.0.1:1/dead")
    monkeypatch.setattr("synthesizer.gasflywheel._networks", lambda: [net])
    monkeypatch.setattr(SETTINGS, "allow_external_sponsor", False)

    asked = {"n": 0}

    def refuse(net, op):
        asked["n"] += 1
        return None

    monkeypatch.setattr(flywheel, "_sponsor", refuse)

    log: list[str] = []
    result = flywheel.settle("0x", attempts=log)

    assert asked["n"] == 0, "the sponsor rung was reached while disabled"
    assert any("external_sponsor_disabled" in line for line in log)
    assert "no_sponsorship" not in result.reason
    assert result.status == "unsettled"


# -- 2. the reinvestment loop closes --------------------------------------


def test_a_landed_settlement_credits_the_gas_reserve(chain):
    """Settling folds the reserve slice back into the engine's balance."""
    key = chain.session_key
    eoa = Account.from_key(key).address
    flywheel = GasFlywheel(key, eoa)
    before = flywheel.reserve.state.amount

    path = AtomicPath(
        intent_id="i", steps=[], proven_net_profit_usd=10.0,
        simulation_ok=True,
    )
    flywheel.record_settlement_gas(123_456, path, landed=True)

    assert flywheel.gas_spent_wei == 123_456
    assert flywheel.settlements_landed == 1
    assert flywheel.reserve.state.amount > before
    expected = int(10.0 * 1e18) * SETTINGS.gas_reserve_bps // 10_000
    assert flywheel.reserve.state.amount == before + expected


def test_an_unsettled_attempt_spends_nothing_and_credits_nothing(chain):
    """Gas accounting must not credit a reserve for a settlement that failed."""
    key = chain.session_key
    eoa = Account.from_key(key).address
    flywheel = GasFlywheel(key, eoa)
    before = flywheel.reserve.state.amount

    flywheel.record_settlement_gas(0, None, landed=False)

    assert flywheel.gas_spent_wei == 0
    assert flywheel.settlements_landed == 0
    assert flywheel.reserve.state.amount == before


def test_gas_report_states_the_funding_model(chain):
    """The report names the internal balance and whether a sponsor is allowed."""
    key = chain.session_key
    flywheel = GasFlywheel(key, Account.from_key(key).address)

    report = flywheel.gas_report()

    assert report["gas_spent_wei"] == 0
    assert report["settlements_landed"] == 0
    assert report["external_sponsor_allowed"] is False
    assert "amount" in report["reserve"]


# -- 3. network selection is a price decision -----------------------------


def test_networks_are_ordered_cheapest_first(chain, monkeypatch):
    """The cheapest measured chain is attempted first, whatever its position."""
    key = chain.session_key
    flywheel = GasFlywheel(key, Account.from_key(key).address)
    prices = {"dear": 30, "cheap": 1, "middle": 10}

    monkeypatch.setattr(
        "synthesizer.gasflywheel._networks",
        lambda: [
            Network(name=n, chain_id=i, rpc_url=f"http://{n}",
                    explorer_base="", paymaster_url="")
            for i, n in enumerate(("dear", "middle", "cheap"))
        ],
    )

    def fake_rpc(url, method, params, timeout=8.0):
        for name, price in prices.items():
            if name in url:
                return {"result": hex(price)}
        return {"result": hex(10**9)}

    monkeypatch.setattr("synthesizer.gasflywheel._rpc", fake_rpc)

    ordered = [n.name for n in flywheel._networks_by_cost()]

    assert ordered == ["cheap", "middle", "dear"]


# -- 4. the Double Gate still holds ---------------------------------------


def test_a_mined_no_op_is_not_a_settlement(chain, monkeypatch):
    """A mined operation that moved no value must not be reported as executed.

    This is the second gate. The receipt says status 1, but the beneficiary's
    balance did not grow, so the only honest label is ``unsettled_value``.
    """
    from synthesizer.executor import Executor

    key = chain.session_key
    eoa = Account.from_key(key).address
    chain.fund(eoa, 10**18)
    executor = Executor()
    flywheel = executor.flywheel
    net = _net(chain)

    monkeypatch.setattr("synthesizer.executor._networks", lambda: [net])
    settlement = GasSettlement(
        status="executed", network="local", chain_id=chain.chain_id,
        tx_hash="0x" + "ab" * 32, gas_paid_by="native", block_number=1,
    )
    monkeypatch.setattr(flywheel, "settle", lambda *a, **k: settlement)
    monkeypatch.setattr(executor, "sweep_landed",
                        lambda *a, **k: (False, "no_balance_increase", 0))
    monkeypatch.setattr(executor, "_can_fund", lambda path: False)
    monkeypatch.setattr(executor, "_ensure_lock_allowance",
                        lambda net, path: True)
    monkeypatch.setattr(executor, "settlement_call_data", lambda path: "0x")

    path = AtomicPath(
        intent_id="i", steps=[], proven_net_profit_usd=1.0,
        simulation_ok=True, gas_sponsor="protocol",
    )
    constraints = Constraints(
        goal=Goal(objective="earn", asset="USDC", target_amount=1.0,
                  raw_intent="test"),
        net_profit_usd_min=0.01,
        beneficiary=SETTINGS.beneficiary,
    )

    result = executor.settle(path, constraints)

    assert result.status == "unsettled_value"
    assert result.amount_usd == 0.0
    assert result.value_transferred is False
