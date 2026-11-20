"""Settle real value through the deployed GasValueLock, with zero ETH.

This is the end-to-end proof the engine exists to produce: a token holder with
no native balance pays nothing for gas, and value still reaches the locked
beneficiary. Every number printed here is read back from the chain, so the
report is evidence rather than intent.

    python scripts/live_settle.py --amount 100

The account address comes from the factory, never from a local derivation: a
wrong address is indistinguishable from a right one until nothing moves.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_abi import encode  # noqa: E402
from eth_utils import keccak  # noqa: E402

from synthesizer import valuegas  # noqa: E402
from synthesizer.executor import SessionKey  # noqa: E402
from synthesizer.gasflywheel import GasFlywheel, _networks, _rpc  # noqa: E402

LOCK = "0xc6ea0246578299e4079e22293e68664f8b6e143a"
TOKEN = "0x7c5455b7f38c30605a298a88b8df84fdebb56419"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", type=float, default=100.0)
    ap.add_argument("--fee", type=float, default=2.0)
    ap.add_argument("--reserve-bps", type=int, default=500)
    ap.add_argument("--chain", default="base-sepolia")
    args = ap.parse_args()

    nets = {n.name: n for n in _networks()}
    net = nets[args.chain]
    rpc = net.rpc_url

    key = SessionKey.load()
    flywheel = GasFlywheel(key.private_key, "")
    account = flywheel.account_for(net)

    def call(to: str, data: str) -> int:
        result = (_rpc(rpc, "eth_call", [{"to": to, "data": data}, "latest"]) or {}).get(
            "result", "0x"
        )
        return int(result, 16) if result not in ("0x", "") else 0

    def token_uint(fn: str, args_: list[str]) -> int:
        data = "0x" + (keccak(text=fn)[:4] + encode(["address"] * len(args_), args_)).hex()
        return call(TOKEN, data)

    def eth_balance(address: str) -> int:
        result = (_rpc(rpc, "eth_getBalance", [address, "latest"]) or {}).get(
            "result", "0x0"
        )
        return int(result, 16)

    def execute_call(to: str, data: bytes) -> str:
        return "0x" + (
            keccak(text="execute(address,uint256,bytes)")[:4]
            + encode(["address", "uint256", "bytes"], [to, 0, data])
        ).hex()

    beneficiary = valuegas.BENEFICIARY
    units = int(args.amount * 10**6)
    fee = int(args.fee * 10**6)

    print(f"account:     {account}")
    print(f"native:      {eth_balance(account)} wei  <- gas is never paid from here")
    print(f"held:        {token_uint('balanceOf(address)', [account]) / 1e6} tokens")

    before_beneficiary = token_uint("balanceOf(address)", [beneficiary])
    before_reserve = call(
        LOCK, "0x" + (keccak(text="gasReserve(address)")[:4] + encode(["address"], [TOKEN])).hex()
    )

    # A zero-ETH holder cannot pay for its own approval, so the approval rides
    # the same sponsored channel as the settlement.
    approve = keccak(text="approve(address,uint256)")[:4] + encode(
        ["address", "uint256"], [LOCK, units]
    )
    if token_uint("allowance(address,address)", [account, LOCK]) < units:
        step = flywheel.settle(execute_call(TOKEN, approve))
        print(f"approve:     {step.status} on {step.network}")
        if step.status != "executed":
            print(f"  refused: {step.attempts}")
            return 1
        time.sleep(5)

    inner = valuegas.build_settle_calldata(
        intent_id=f"live-{int(time.time())}",
        token=TOKEN,
        relayer=account,
        net_profit=units,
        relayer_fee=fee,
        reserve_bps=args.reserve_bps,
        proof_satisfied=True,
        to=beneficiary,
    )
    settlement = flywheel.settle(execute_call(LOCK, inner))
    print(f"settle:      {settlement.status} on {settlement.network}")
    print(f"gas paid by: {settlement.gas_paid_by}")
    print(f"tx:          {settlement.tx_hash}")
    print(f"explorer:    {settlement.explorer_url}")
    if settlement.status != "executed":
        print(f"  refused: {settlement.attempts}")
        return 1

    time.sleep(8)
    gained = token_uint("balanceOf(address)", [beneficiary]) - before_beneficiary
    reserve = (
        call(
            LOCK,
            "0x" + (keccak(text="gasReserve(address)")[:4] + encode(["address"], [TOKEN])).hex(),
        )
        - before_reserve
    )
    print(f"beneficiary: +{gained / 1e6} tokens")
    print(f"reserve:     +{reserve / 1e6} tokens")
    print(f"native:      {eth_balance(account)} wei  <- still nothing spent")
    return 0 if gained > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
