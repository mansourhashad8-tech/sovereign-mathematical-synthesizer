"""End-to-end proof of self-funded execution with the real EntryPoint.

This deploys the canonical ERC-4337 EntryPoint and SimpleAccountFactory
bytecode into a local EVM, then drives the production ``settle_self_funded``
path. Nothing is stubbed:

* the account is a real SimpleAccount, derived by the real factory;
* the operation is signed by the session key and verified by the real
  EntryPoint through ``handleOps``;
* the prefund comes from a deposit the engine makes itself.

There is no paymaster and no bundler anywhere in the path. If this test passes,
the claim "the engine pays its own gas" is a fact about the code, not a
description of an intention.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_abi import encode
from eth_utils import keccak

from synthesizer import gasflywheel
from synthesizer.config import SETTINGS
from synthesizer.gasflywheel import GasFlywheel

from .localchain import Chain

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"


def _artifact(name: str) -> dict:
    return json.loads((ARTIFACTS / f"{name}.json").read_text())


@pytest.fixture()
def erc4337_chain(monkeypatch):
    """A chain with the real EntryPoint and account factory deployed."""
    c = Chain()
    url, server = c.serve()
    c.url = url

    ep = _artifact("EntryPoint")
    entry_point = c.deploy(ep["bytecode"])
    factory_art = _artifact("SimpleAccountFactory")
    factory = c.deploy(
        factory_art["bytecode"]
        + encode(["address"], [entry_point]).hex()
    )

    # Point the engine at the locally deployed singletons.
    monkeypatch.setattr(gasflywheel, "ENTRY_POINT", entry_point)
    monkeypatch.setattr(gasflywheel, "SIMPLE_ACCOUNT_FACTORY", factory)
    c.entry_point = entry_point
    c.factory = factory

    yield c
    server.shutdown()


def _net(chain):
    from synthesizer.gasflywheel import Network
    return Network(name="local", chain_id=chain.chain_id, rpc_url=chain.url,
                   explorer_base="http://local/tx/", paymaster_url="")


def test_self_funded_operation_mines_without_paymaster(erc4337_chain):
    """A deposited, self-signed operation executes through ``handleOps``.

    The paymaster URL on the network is a dead port. If the code depended on
    sponsorship this would fail; it must succeed on the engine's own deposit.
    """
    chain = erc4337_chain
    key = chain.session_key
    eoa = chain.session_address
    chain.fund(eoa, 10**18)

    flywheel = GasFlywheel(key, eoa)
    net = _net(chain)
    account = flywheel.account_for(net)
    assert account, "factory must derive an account address"

    # A trivial call: the account sends 0 ETH to itself. The point of this test
    # is the gas mechanism, not the payload, so the smallest valid call is used.
    calldata = flywheel.call_data(account, 0)
    result = flywheel.settle_self_funded(net, calldata)

    assert result.mined, f"self-funded op failed: {result.reason} {result.attempts}"
    assert result.gas_paid_by == "self_deposit"
    assert result.tx_hash.startswith("0x")
    assert result.block_number > 0
    # The operation must be deployed now: a second attempt needs no initCode.
    assert flywheel._deployed(net, account) is True


def test_self_funded_settlement_delivers_value_to_beneficiary(erc4337_chain):
    """The full path: value reaches the locked beneficiary, gas is internal."""
    chain = erc4337_chain
    key = chain.session_key
    eoa = chain.session_address
    chain.fund(eoa, 10**18)

    token = chain.deploy(
        (Path(__file__).resolve().parent.parent / "contracts" / "TestValueToken.bin")
        .read_text().strip()
    )
    lock = chain.deploy(
        (Path(__file__).resolve().parent.parent / "contracts" / "GasValueLock.bin")
        .read_text().strip()
    )

    flywheel = GasFlywheel(key, eoa)
    net = _net(chain)
    account = flywheel.account_for(net)

    # Fund the smart account with value tokens and let it approve the lock.
    tester = chain.tester
    tester.send_transaction({
        "from": tester.get_accounts()[0], "to": token,
        "data": "0x40c10f19" + account[2:].rjust(64, "0")
        + hex(50_000_000)[2:].rjust(64, "0"),
        "gas": 300_000,
    })
    approve = (
        keccak(text="approve(address,uint256)")[:4]
        + encode(["address", "uint256"], [lock, 50_000_000])
    )
    first = flywheel.settle_self_funded(net, flywheel.call_data(token, 0, approve))
    assert first.mined, f"approve op failed: {first.reason} {first.attempts}"

    beneficiary = SETTINGS.beneficiary
    before = _token_balance(chain, token, beneficiary)

    settle = (
        keccak(text="settle(bytes32,address,address,uint256,uint256,uint256,bool,address)")[:4]
        + encode(
            ["bytes32", "address", "address", "uint256", "uint256",
             "uint256", "bool", "address"],
            [keccak(text="self-funded-intent"), token, beneficiary,
             1_000_000, 100_000, 500, True, beneficiary],
        )
    )
    second = flywheel.settle_self_funded(net, flywheel.call_data(lock, 0, settle))
    assert second.mined, f"settle op failed: {second.reason} {second.attempts}"

    after = _token_balance(chain, token, beneficiary)
    assert after > before, f"beneficiary gained nothing: {before} -> {after}"
    assert second.gas_paid_by == "self_deposit"


def test_prefund_shortfall_is_reported_not_attempted(erc4337_chain):
    """An unfunded engine reports the exact shortfall instead of a failure."""
    chain = erc4337_chain
    flywheel = GasFlywheel(chain.session_key, chain.session_address)
    net = _net(chain)
    # Drain the session account so the prefund cannot be met. The transfer
    # itself needs gas, so the amount sent has to leave room for it.
    tester = chain.tester
    bal = tester.get_balance(chain.session_address)
    gas_cost = 30_000 * (tester.get_block_by_number("latest").get("baseFeePerGas") or 10**9)
    drainable = bal - gas_cost - 10**15
    if drainable > 0:
        tester.send_transaction({
            "from": chain.session_address, "to": tester.get_accounts()[0],
            "value": drainable, "gas": 30_000,
        })
    account = flywheel.account_for(net)
    result = flywheel.settle_self_funded(net, flywheel.call_data(account, 0))

    assert not result.mined
    assert "native_shortfall" in result.reason
    assert "need=" in result.reason


def _token_balance(chain: Chain, token: str, owner: str) -> int:
    out = chain.tester.call({
        "from": chain.tester.get_accounts()[0], "to": token,
        "data": "0x70a08231" + owner[2:].rjust(64, "0"), "gas": 100_000,
    })
    return int(out, 16) if out and out != "0x" else 0
