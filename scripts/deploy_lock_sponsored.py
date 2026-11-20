"""Deploy GasValueLock through the sponsored account, no ETH required.

The agent holds no ETH on any chain, but a paymaster sponsors operations on
Base Sepolia. That is enough to put the contract on a real chain: the smart
account calls the deterministic CREATE2 deployer inside a sponsored
UserOperation, so the deployment costs the agent nothing.

    python scripts/deploy_lock_sponsored.py [--chain base-sepolia]

The address is derived from the bytecode and a zero salt before broadcasting,
so a re-run against an already-deployed salt reports the existing address
instead of paying to fail.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_abi import encode  # noqa: E402
from eth_utils import keccak  # noqa: E402

from synthesizer.executor import SessionKey  # noqa: E402
from synthesizer.gasflywheel import GasFlywheel, _networks, _rpc  # noqa: E402
from synthesizer.valuegas import load_artifact  # noqa: E402

# Arachnid's deterministic deployment proxy, present on every major chain.
CREATE2_DEPLOYER = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
SALT = bytes(32)


def create2_address(bytecode: bytes) -> str:
    """The address the deployer produces for this bytecode and salt."""
    digest = keccak(
        b"\xff" + bytes.fromhex(CREATE2_DEPLOYER[2:]) + SALT + keccak(bytecode)
    )
    return "0x" + digest[12:].hex()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="base-sepolia")
    args = ap.parse_args()

    nets = {n.name: n for n in _networks()}
    net = nets.get(args.chain)
    if net is None:
        print(f"unknown chain {args.chain}; have {sorted(nets)}")
        return 2

    _abi, hexcode = load_artifact()
    bytecode = bytes.fromhex(hexcode)
    predicted = create2_address(bytecode)

    print(f"chain:        {net.name} ({net.chain_id})")
    print(f"predicted:    {predicted}")

    code = (_rpc(net.rpc_url, "eth_getCode", [predicted, "latest"]) or {}).get(
        "result", "0x"
    )
    if len(code) > 2:
        print(f"already deployed: {(len(code) - 2) // 2} bytes")
        return 0

    # The deployer expects salt || initCode; the account calls it through
    # execute(to, value, data), which is the only calldata shape it accepts.
    payload = SALT + bytecode
    call = keccak(text="execute(address,uint256,bytes)")[:4] + encode(
        ["address", "uint256", "bytes"], [CREATE2_DEPLOYER, 0, payload]
    )

    key = SessionKey.load()
    flywheel = GasFlywheel(key.private_key, "")
    # The factory is the only authority on the account address; a locally
    # derived value can differ and would deploy nothing.
    print(f"account:      {flywheel.account_for(net)}")
    settlement = flywheel.settle("0x" + call.hex())

    print(f"status:       {settlement.status}")
    print(f"gas paid by:  {settlement.gas_paid_by}")
    print(f"userOp hash:  {settlement.user_op_hash}")
    print(f"tx hash:      {settlement.tx_hash}")
    if settlement.explorer_url:
        print(f"explorer:     {settlement.explorer_url}")
    if settlement.status not in ("executed", "mined", "submitted"):
        print(f"reason:       {settlement.reason}")
        return 1

    after = (_rpc(net.rpc_url, "eth_getCode", [predicted, "latest"]) or {}).get(
        "result", "0x"
    )
    print(f"contract:     {predicted} ({(len(after) - 2) // 2} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
