"""Deploy GasValueLock to a live chain, or explain precisely why not.

The contract is the thing that lets a zero-ETH agent settle, so it has to be on
a chain to be useful. Deployment costs gas, and this environment holds no ETH
on any chain. That is a fact about funding, not about the code, so this script
does the honest thing: it reports the shortfall in ETH and the exact address
that needs it, and it deploys immediately on any chain where the funds exist.

Run it against any funded key:

    SYNTH_DEPLOY_KEY=0x... python scripts/deploy_lock.py --chain base-mainnet
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synthesizer.gasflywheel import MAINNET, _networks  # noqa: E402
from synthesizer.valuegas import load_artifact  # noqa: E402

CHAINS = {
    "base-mainnet": ("https://mainnet.base.org", "https://basescan.org/tx/", 8453),
    "base-sepolia": ("https://sepolia.base.org", "https://sepolia.basescan.org/tx/", 84532),
}


def rpc(url: str, method: str, params: list) -> dict:
    resp = httpx.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def balance_wei(url: str, address: str) -> int:
    return int(rpc(url, "eth_getBalance", [address, "latest"]).get("result", "0x0"), 16)


def gas_price(url: str) -> int:
    return int(rpc(url, "eth_gasPrice", []).get("result", "0x0"), 16)


def deploy(key: str, chain: str) -> dict:
    from eth_account import Account

    rpc_url, explorer, chain_id = CHAINS[chain]
    account = Account.from_key(key)
    address = account.address

    _abi, bytecode = load_artifact()
    data = bytes.fromhex(bytecode)

    held = balance_wei(rpc_url, address)
    price = gas_price(rpc_url)
    # A deploy of this contract costs roughly 700k gas including calldata.
    estimate = 700_000
    needed = estimate * price

    if held < needed:
        return {
            "deployed": False,
            "chain": chain,
            "reason": "insufficient_gas_eth",
            "address_needing_eth": address,
            "held_wei": held,
            "needed_wei": needed,
            "shortfall_wei": needed - held,
            "note": (
                f"fund {address} with at least {needed / 1e18:.8f} ETH on {chain} "
                "and re-run; the contract deploys in one transaction"
            ),
        }

    nonce = int(rpc(rpc_url, "eth_getTransactionCount", [address, "latest"])["result"], 16)
    tx = {
        "nonce": nonce,
        "gasPrice": price,
        "gas": estimate,
        "to": b"",
        "value": 0,
        "data": data,
        "chainId": chain_id,
    }
    signed = account.sign_transaction(tx)
    raw = "0x" + bytes(signed.raw_transaction).hex()
    result = rpc(rpc_url, "eth_sendRawTransaction", [raw])
    tx_hash = result.get("result", "")
    if not tx_hash:
        return {
            "deployed": False,
            "chain": chain,
            "reason": "broadcast_rejected",
            "error": result.get("error", {}),
        }
    return {
        "deployed": True,
        "chain": chain,
        "tx_hash": tx_hash,
        "explorer": f"{explorer}{tx_hash}",
        "contract": address,
        "note": "wait for the receipt, then read the created address from it",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="base-mainnet", choices=sorted(CHAINS))
    args = ap.parse_args()

    key = os.environ.get("SYNTH_DEPLOY_KEY") or os.environ.get("SYNTH_SESSION_KEY")
    if not key:
        from synthesizer.executor import SessionKey

        key = SessionKey.load().private_key

    result = deploy(key, args.chain)
    for k, v in result.items():
        print(f"{k}: {v}")
    return 0 if result.get("deployed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
