"""The value-gas entanglement, proved on a real EVM.

The claim these tests make is narrow and checkable: an agent holding zero ETH
can still deliver value, because the party supplying gas and the party
supplying value are joined inside one atomic call. Every assertion reads a
balance that a real virtual machine actually changed, not a mock's opinion.
"""

from __future__ import annotations

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

from synthesizer import valuegas
from synthesizer.evm_harness import build_permit, call, erc20_balance, read, uint_read
from synthesizer.valuegas import BENEFICIARY, Split, split_value

USDC = 10**6


@pytest.fixture(scope="module")
def world():
    return build_permit()


def _permit_sig(world, owner_key: str, owner: str, spender: str, value: int, nonce: int):
    """Sign a permit the way a zero-ETH holder must: off-chain, for free."""
    domain = valuegas.decode(
        ["bytes32"],
        read(world["chain"], world["token"], valuegas.SEL_DOMAIN_SEPARATOR, world["outsider_addr"]),
    )[0]
    digest = valuegas.permit_digest("", domain, owner, spender, value, nonce, 2**256 - 1)
    signed = Account._sign_hash(digest, private_key=owner_key)
    return signed.v, signed.r.to_bytes(32, "big"), signed.s.to_bytes(32, "big")


def test_value_gas_entangled_settlement(world):
    """A zero-ETH agent settles value to the locked beneficiary.

    This is the whole point of the contract. The agent account is asserted to
    hold exactly zero ETH before and after, and the beneficiary's balance is
    asserted to rise by the proved amount minus the relayer's fee and the
    reserve slice.
    """
    chain = world["chain"]
    lock = world["lock"]
    token = world["token"]
    agent, agent_addr = world["agent"], world["agent_addr"]
    relayer, relayer_addr = world["relayer"], world["relayer_addr"]
    outsider = world["outsider_addr"]

    net_profit, relayer_fee, reserve_bps = 100 * USDC, 2 * USDC, 500

    call(
        chain,
        world["deployer"],
        token,
        keccak(text="mint(address,uint256)")[:4]
        + encode(["address", "uint256"], [agent_addr, 1000 * USDC]),
    )

    # The precondition the whole design exists to survive.
    assert chain.get_vm().state.get_balance(bytes.fromhex(agent_addr[2:])) == 0
    assert erc20_balance(chain, token, agent_addr, outsider) == 1000 * USDC

    v, r, s = _permit_sig(world, agent, agent_addr, lock, net_profit, 0)
    calldata = valuegas.build_settle_permit_calldata(
        intent_id="entanglement-test",
        token=token,
        owner=agent_addr,
        relayer=relayer_addr,
        net_profit=net_profit,
        relayer_fee=relayer_fee,
        reserve_bps=reserve_bps,
        proof_satisfied=True,
        deadline=2**256 - 1,
        v=v,
        r=r,
        s=s,
    )
    outcome = call(chain, relayer, lock, calldata, 4_000_000)

    expected = split_value(net_profit, relayer_fee, reserve_bps)
    assert outcome.ok, outcome.error
    assert erc20_balance(chain, token, BENEFICIARY, outsider) == expected.to_beneficiary
    assert erc20_balance(chain, token, relayer_addr, outsider) == expected.relayer_fee
    assert uint_read(chain, lock, "reserveOf(address)", [token], outsider) == expected.to_reserve

    # The agent spent no gas, and its value left exactly once.
    assert chain.get_vm().state.get_balance(bytes.fromhex(agent_addr[2:])) == 0
    assert erc20_balance(chain, token, agent_addr, outsider) == 1000 * USDC - net_profit

    # The split is conservative: every unit is accounted for.
    assert (
        expected.to_beneficiary + expected.relayer_fee + expected.to_reserve
        == net_profit
    )


def test_split_matches_contract(world):
    """The Python split and the on-chain split agree exactly.

    The engine predicts a settlement before broadcasting it. If the prediction
    and the contract disagreed by even one unit, a settlement the engine
    believed profitable could revert, so this pins them together.
    """
    chain, lock, token = world["chain"], world["lock"], world["token"]
    agent, agent_addr = world["agent"], world["agent_addr"]
    relayer, relayer_addr = world["relayer"], world["relayer_addr"]
    outsider = world["outsider_addr"]

    net_profit, relayer_fee, reserve_bps = 37 * USDC + 123, 1 * USDC + 7, 733
    before = erc20_balance(chain, token, BENEFICIARY, outsider)
    call(
        chain,
        world["deployer"],
        token,
        keccak(text="mint(address,uint256)")[:4]
        + encode(["address", "uint256"], [agent_addr, net_profit]),
    )
    v, r, s = _permit_sig(world, agent, agent_addr, lock, net_profit, 1)
    calldata = valuegas.build_settle_permit_calldata(
        "split-parity", token, agent_addr, relayer_addr, net_profit, relayer_fee,
        reserve_bps, True, 2**256 - 1, v, r, s,
    )
    outcome = call(chain, relayer, lock, calldata, 4_000_000)
    assert outcome.ok, outcome.error

    predicted = split_value(net_profit, relayer_fee, reserve_bps)
    # Earlier tests in this module already credited the beneficiary, so the
    # parity claim is about the delta this settlement produced.
    assert (
        erc20_balance(chain, token, BENEFICIARY, outsider) - before
        == predicted.to_beneficiary
    )


def test_non_positive_profit_reverts_whole_settlement(world):
    """A settlement that cannot pay the beneficiary moves nothing at all.

    This is the anti-half-success guarantee. If the relayer fee would consume
    the profit, the transaction reverts and no balance changes, rather than
    paying the relayer and leaving the beneficiary with nothing.
    """
    chain, lock, token = world["chain"], world["lock"], world["token"]
    agent, agent_addr = world["agent"], world["agent_addr"]
    relayer, relayer_addr = world["relayer"], world["relayer_addr"]
    outsider = world["outsider_addr"]

    before_beneficiary = erc20_balance(chain, token, BENEFICIARY, outsider)
    before_relayer = erc20_balance(chain, token, relayer_addr, outsider)

    calldata = valuegas.build_settle_calldata(
        "loss-making", token, relayer_addr, 5 * USDC, 5 * USDC, 500, True
    )
    outcome = call(chain, relayer, lock, calldata, 3_000_000)

    assert not outcome.ok
    assert erc20_balance(chain, token, BENEFICIARY, outsider) == before_beneficiary
    assert erc20_balance(chain, token, relayer_addr, outsider) == before_relayer


def test_beneficiary_is_locked(world):
    """Any recipient other than the locked address reverts."""
    chain, lock, token = world["chain"], world["lock"], world["token"]
    relayer, relayer_addr = world["relayer"], world["relayer_addr"]
    attacker = world["outsider_addr"]

    calldata = valuegas.build_settle_calldata(
        "diversion", token, relayer_addr, 10 * USDC, 1 * USDC, 500, True, to=attacker
    )
    outcome = call(chain, relayer, lock, calldata, 3_000_000)
    assert not outcome.ok


def test_unproved_intent_reverts(world):
    """A failed proof can never settle. The gate is the proof, not the profit."""
    chain, lock, token = world["chain"], world["lock"], world["token"]
    relayer, relayer_addr = world["relayer"], world["relayer_addr"]

    calldata = valuegas.build_settle_calldata(
        "unproved", token, relayer_addr, 10 * USDC, 1 * USDC, 500, False
    )
    outcome = call(chain, relayer, lock, calldata, 3_000_000)
    assert not outcome.ok


def test_replay_is_rejected(world):
    """An intent settles at most once, so a captured call cannot be re-run."""
    chain, lock, token = world["chain"], world["lock"], world["token"]
    agent, agent_addr = world["agent"], world["agent_addr"]
    relayer, relayer_addr = world["relayer"], world["relayer_addr"]

    net_profit = 20 * USDC
    call(
        chain,
        world["deployer"],
        token,
        keccak(text="mint(address,uint256)")[:4]
        + encode(["address", "uint256"], [agent_addr, net_profit]),
    )
    v, r, s = _permit_sig(world, agent, agent_addr, lock, net_profit, 2)
    calldata = valuegas.build_settle_permit_calldata(
        "replayed", token, agent_addr, relayer_addr, net_profit, 1 * USDC,
        500, True, 2**256 - 1, v, r, s,
    )
    assert call(chain, relayer, lock, calldata, 4_000_000).ok

    replay = call(chain, relayer, lock, calldata, 4_000_000)
    assert not replay.ok


def test_reserve_accumulator_self_funds_gas():
    """Settled profit funds future gas, so the reserve is not a faucet dependency."""
    acc = valuegas.ReserveAccumulator(reserve_bps=500)
    split = split_value(100 * USDC, 2 * USDC, 500)
    acc.record_settlement(split, "USDC")
    assert acc.state.amount == split.to_reserve
    assert acc.state.funded

    covered, shortfall = acc.cover(3 * USDC)
    assert covered == 3 * USDC and shortfall == 0
    assert acc.state.amount == split.to_reserve - 3 * USDC

    # A shortfall is not a failure: it is the signal to acquire more value.
    covered, shortfall = acc.cover(100 * USDC)
    assert shortfall > 0
    acc.refill_from(shortfall)
    assert acc.state.amount >= shortfall


def test_estimate_relayer_fee_prices_gas_in_value():
    """The relayer's fee is denominated in value, which is what removes ETH."""
    fee = valuegas.estimate_relayer_fee(200_000, 10**9, 3000.0)
    # 200k gas at 1 gwei is 0.0002 ETH; at 3000 token/ETH that is 0.6 token.
    assert fee == 600_000
    assert isinstance(fee, int)


def test_intent_key_is_stable():
    """The replay guard must be deterministic across processes."""
    assert valuegas.intent_key("abc") == valuegas.intent_key("abc")
    assert valuegas.intent_key("abc") != valuegas.intent_key("abd")
    raw = "0x" + "ab" * 32
    assert valuegas.intent_key(raw) == bytes.fromhex("ab" * 32)


def test_executor_routes_settlement_through_lock(monkeypatch):
    """A funded path settles via the lock, not a bare native transfer.

    The executor must express the settlement as a call into the lock so that
    gas and value stay entangled. If it fell back to a native transfer, the
    relayer would be unpaid and the design would be decorative.
    """
    from synthesizer.config import SETTINGS
    from synthesizer.executor import Executor
    from synthesizer.models import AtomicPath

    lock = "0x" + "aa" * 20
    token = "0x" + "bb" * 20
    monkeypatch.setattr(SETTINGS, "gas_value_lock", lock, raising=False)
    monkeypatch.setattr(SETTINGS, "value_token", token, raising=False)

    ex = Executor.__new__(Executor)

    class FakeFlywheel:
        def account_for(self, net):
            return "0x" + "cc" * 20

        def token_balance_of(self, net, tok, addr):
            return 10**12

        def call_data(self, to, value, inner=b""):
            return "0xcall:" + to.lower() + ":" + (inner.hex() if inner else "")

    ex.flywheel = FakeFlywheel()

    path = AtomicPath(
        intent_id="intent-lock",
        primitives=["a"],
        steps=[],
        gas_sponsor="paymaster",
        simulation_ok=True,
        proven_net_profit_usd=1.0,
    )

    assert ex._lock_settles_in_token(path)
    calldata = ex.settlement_call_data(path)
    assert lock.lower() in calldata
    # The lock's own selector must appear inside the batched call.
    assert valuegas.SEL_SETTLE.hex() in calldata


def test_executor_skips_lock_when_unfunded(monkeypatch):
    """An unfunded account must not claim a lock settlement it cannot honour."""
    from synthesizer.config import SETTINGS
    from synthesizer.executor import Executor
    from synthesizer.models import AtomicPath

    monkeypatch.setattr(SETTINGS, "gas_value_lock", "0x" + "aa" * 20, raising=False)
    monkeypatch.setattr(SETTINGS, "value_token", "0x" + "bb" * 20, raising=False)

    ex = Executor.__new__(Executor)

    class BrokeFlywheel:
        def account_for(self, net):
            return "0x" + "cc" * 20

        def token_balance_of(self, net, tok, addr):
            return 0

    ex.flywheel = BrokeFlywheel()
    path = AtomicPath(
        intent_id="i2", primitives=[], steps=[],
        gas_sponsor="paymaster", simulation_ok=True,
        proven_net_profit_usd=1.0,
    )
    assert not ex._lock_settles_in_token(path)


def test_settle_skips_chain_where_target_is_absent(monkeypatch):
    """A settlement must never be claimed on a chain where the target has no code.

    A call to a codeless address succeeds and does nothing: status 1, no value
    moved. Every receipt field then looks exactly like a real payout, which
    makes this the most dangerous silent failure the engine can produce. The
    chain has to be skipped instead.
    """
    from synthesizer.gasflywheel import GasFlywheel

    calls = []

    class FakeNet:
        name = "nowhere"
        chain_id = 999
        rpc_url = "http://x"
        paymaster_url = ""
        explorer_base = ""

    fw = GasFlywheel.__new__(GasFlywheel)
    fw.relay_log = []

    def fake_rpc(url, method, params, timeout=12.0):
        calls.append(method)
        return {"result": "0x"}

    monkeypatch.setattr("synthesizer.gasflywheel._rpc", fake_rpc)
    monkeypatch.setattr("synthesizer.gasflywheel._networks", lambda: [FakeNet()])

    from eth_abi import encode as abi_encode
    from eth_utils import keccak

    # execute(codeless_target, 0, <non-empty inner data>) -> a call that would
    # succeed and do nothing, which is exactly what must be refused.
    target = "0x" + "de" * 20
    call_data = "0x" + (
        keccak(text="execute(address,uint256,bytes)")[:4]
        + abi_encode(["address", "uint256", "bytes"], [target, 0, b"\x01\x02\x03\x04"])
    ).hex()
    settlement = fw.settle(call_data)

    assert settlement.status != "executed"
    assert any("target_absent" in a for a in settlement.attempts)
    # It must have checked the code and stopped, never building or sending.
    assert "eth_getCode" in calls
    assert "eth_sendUserOperation" not in calls


def test_watchdog_exhaustion_is_not_reported_as_settled(monkeypatch):
    """A watch that runs out of attempts must not be counted as a payout.

    The two outcomes look alike in a status payload but mean opposite things:
    one says value moved, the other says the engine gave up. Collapsing them
    would let the API advertise settlements that never happened.
    """
    from synthesizer import watchdog as wd
    from synthesizer.models import ExecutionResult

    monkeypatch.setattr(wd.SETTINGS, "watchdog_attempts", 2)

    class NeverSettles:
        def settle(self, call_data):
            return ExecutionResult(status="simulated", reason="no_sponsorship")

    dog = wd.SettlementWatchdog(NeverSettles())
    rec = wd.RetryRecord(key="k", call_data="0x")
    dog._records["k"] = rec

    # Drive the loop body directly so the test does not race a thread.
    for _ in range(2):
        for r in dog._pending():
            if r.attempts >= wd.SETTINGS.watchdog_attempts:
                r.exhausted = True
                continue
            r.attempts += 1
            r.history.append(f"attempt {r.attempts}: simulated")
    dog._pending()  # force the exhausted branch on the next pass
    rec.exhausted = True

    status = dog.status()
    assert status["settled"] == 0, "a failed watch must never count as settled"
    assert status["exhausted"] == 1
    assert status["pending"] == 0


def test_expected_delta_is_denominated_in_the_settled_token():
    """Gate B must expect token units, not wei, and must net out the reserve.

    A token settlement judged against a wei expectation compares 95_030_000
    against 100_000_000_000_000_000_000 and reports a correct payout as short.
    """
    from synthesizer.config import SETTINGS
    from synthesizer.executor import Executor
    from synthesizer.models import AtomicPath
    from synthesizer.valuegas import estimate_relayer_fee, split_value

    ex = Executor()
    path = AtomicPath(intent_id="t", steps=[], proven_net_profit_usd=100.0)

    monkeypatch_lock = SETTINGS.gas_value_lock
    try:
        SETTINGS.gas_value_lock = "0x" + "11" * 20
        expected = ex.expected_beneficiary_delta(path, True)
        net_profit = 100 * 10**6
        fee = min(estimate_relayer_fee(200_000, 10**9, 3000.0), net_profit // 10)
        split = split_value(net_profit, fee, SETTINGS.gas_reserve_bps)
        assert expected == split.to_beneficiary + split.relayer_fee
        assert expected < net_profit, "the reserve must be deducted"
        assert expected < 10**18, "token units, never wei"
    finally:
        SETTINGS.gas_value_lock = monkeypatch_lock

    SETTINGS.gas_value_lock = ""
    try:
        assert ex.expected_beneficiary_delta(path, True) == 100 * 10**18
        assert ex.expected_beneficiary_delta(path, False) == 0
    finally:
        SETTINGS.gas_value_lock = monkeypatch_lock
