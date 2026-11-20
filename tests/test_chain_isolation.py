"""The mirror must never answer a question about a different chain.

``_read_state`` treats ``"0x"`` as ambiguous and asks a mirror to confirm. That
is correct for a lagging node, but a mirror configured for one deployment is
not necessarily on the same chain as the network being queried. Before this was
guarded, a base-mainnet read was confirmed by a base-sepolia node, which
reported sepolia's contract code for a mainnet address and made an undeployed
account look deployed. That wrong answer then blocked the ``initCode`` that
would have deployed it.
"""
from __future__ import annotations

from synthesizer import gasflywheel


def test_chain_id_is_read_and_cached():
    assert gasflywheel._chain_id_of("https://sepolia.base.org") == 84532
    # Second call is served from the cache rather than the node.
    assert gasflywheel._chain_id_of("https://sepolia.base.org") == 84532


def test_mirror_on_another_chain_is_not_consulted(monkeypatch):
    """A base-sepolia mirror must not confirm a base-mainnet empty result."""
    calls: list[str] = []

    def fake_rpc(url, method, params, timeout=12.0):
        if method != "eth_chainId":
            calls.append(url)
        if method == "eth_chainId":
            return {"result": hex(8453 if "mainnet" in url else 84532)}
        # The queried chain says "absent"; the mirror claims code exists.
        if "mainnet" in url:
            return {"result": "0x"}
        return {"result": "0xdeadbeef"}

    monkeypatch.setattr(gasflywheel, "_rpc", fake_rpc)
    monkeypatch.setattr(
        gasflywheel.SETTINGS, "rpc_mirrors",
        ["https://sepolia.base.org"],
        raising=False,
    )
    gasflywheel._CHAIN_IDS.clear()

    out = gasflywheel._read_state(
        "https://mainnet.base.org", "eth_getCode", ["0xabc", "latest"]
    )

    assert out == "0x", "the sepolia mirror must not answer for mainnet"
    # Only the queried chain was asked for state; the mirror was only asked
    # which chain it serves.
    assert calls == ["https://mainnet.base.org"]


def test_mirror_on_the_same_chain_is_consulted(monkeypatch):
    """A same-chain mirror still resolves a lagging empty answer."""
    def fake_rpc(url, method, params, timeout=12.0):
        if method == "eth_chainId":
            return {"result": hex(84532)}
        if url == "https://primary":
            return {"result": "0x"}
        return {"result": "0xfeed"}

    monkeypatch.setattr(gasflywheel, "_rpc", fake_rpc)
    monkeypatch.setattr(gasflywheel.SETTINGS, "rpc_mirrors",
                        ["https://mirror"], raising=False)
    gasflywheel._CHAIN_IDS.clear()

    out = gasflywheel._read_state("https://primary", "eth_getCode", ["0xabc", "latest"])
    assert out == "0xfeed"
