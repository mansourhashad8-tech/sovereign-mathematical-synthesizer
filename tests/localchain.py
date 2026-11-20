"""A real EVM behind a JSON-RPC server, so the production code runs unmodified.

The point is not to mock the chain. eth-tester executes the same EVM bytecode
the public networks do, so a settlement that succeeds here is a settlement that
the code can actually produce. What is simulated is only the *gas balance*, and
that is the honest part: the test funds the account so the self-funded path can
be exercised, then asserts the engine pays for its own gas from that balance.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from eth_tester import EthereumTester, PyEVMBackend
from eth_tester.exceptions import TransactionNotFound
from eth_utils import to_checksum_address


class Chain:
    """A single eth-tester chain wrapped in the JSON-RPC the engine speaks."""

    def __init__(self, chain_id: int | None = None) -> None:
        # The chain id is baked into the chain, not just advertised over RPC.
        # py-evm defaults the CHAINID opcode to 0, so a contract that derives a
        # domain separator from it (the ERC-4337 EntryPoint does) hashes a
        # different value than the client assumes and every signature fails
        # with AA24. Configuring the chain makes the opcode agree.
        from eth.chains.base import MiningChain
        from eth.vm.forks import PragueVM

        genesis = PyEVMBackend._generate_genesis_params(overrides={"gas_limit": 30_000_000})
        self.backend = PyEVMBackend(genesis_parameters=genesis)
        self.tester = EthereumTester(backend=self.backend)
        # Advertise whatever chain id the EVM actually uses. The CHAINID opcode
        # feeds the EntryPoint's domain separator, so an RPC that reports a
        # different number than the opcode returns makes every userOperation
        # signature fail with AA24 while looking like a signing bug.
        actual = self.backend.chain.chain_id
        if chain_id is not None and chain_id != actual:
            chain_cls = MiningChain.configure(
                __name__="TestChain",
                vm_configuration=((0, PragueVM),),
                chain_id=chain_id,
            )
            self.backend = PyEVMBackend(
                genesis_parameters=genesis,
                vm_configuration=chain_cls.vm_configuration,
            )
            self.tester = EthereumTester(backend=self.backend)
            actual = chain_id
        self.chain_id = actual
        self._nonces: dict[str, int] = {}
        # The engine signs with a real private key. eth-tester only signs for
        # the keys it owns, so the test session key is one of them rather than
        # an arbitrary constant: that keeps the signing path genuine.
        self.session_key = "0x" + self.backend.account_keys[1].to_hex().removeprefix("0x")
        self.session_address = self.tester.get_accounts()[1]

    # -- helpers the tests use directly -----------------------------------

    def fund(self, address: str, wei: int) -> None:
        donor = self.tester.get_accounts()[0]
        self.tester.send_transaction({
            "from": donor, "to": address, "value": wei, "gas": 100_000,
        })

    def deploy(self, bytecode_hex: str, sender: str | None = None,
               gas: int = 12_000_000) -> str:
        """Deploy bytecode and return the created contract address.

        The gas limit is generous on purpose: the ERC-4337 EntryPoint needs
        about 5.3M to deploy, and a limit that is too low reports as a failed
        deployment rather than an out-of-gas error.
        """
        sender = sender or self.tester.get_accounts()[0]
        tx = {"from": sender, "data": bytecode_hex, "gas": gas}
        tx_hash = self.tester.send_transaction(tx)
        receipt = self.tester.get_transaction_receipt(tx_hash)
        return receipt["contract_address"]

    # -- JSON-RPC ----------------------------------------------------------

    def handle(self, method: str, params: list):
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "net_version":
            return str(self.chain_id)
        if method == "eth_gasPrice":
            return hex(1_000_000_000)
        if method == "eth_getBalance":
            return hex(self.tester.get_balance(to_checksum_address(params[0])))
        if method == "eth_getCode":
            return self.tester.get_code(to_checksum_address(params[0]))
        if method == "eth_call":
            call = params[0]
            return self.tester.call({
                "from": call.get("from", self.tester.get_accounts()[0]),
                "to": to_checksum_address(call["to"]),
                "data": call.get("data", "0x"),
                "value": int(call.get("value", "0x0"), 16),
                "gas": 3_000_000,
            })
        if method == "eth_getTransactionCount":
            addr = to_checksum_address(params[0])
            return hex(self.tester.get_nonce(addr))
        if method == "eth_sendRawTransaction":
            try:
                tx_hash = self.tester.send_raw_transaction(params[0])
            except Exception as exc:  # noqa: BLE001 - surface as a JSON-RPC error
                raise RuntimeError(str(exc)) from exc
            return tx_hash.hex() if not tx_hash.startswith("0x") else tx_hash
        if method == "eth_getTransactionReceipt":
            try:
                r = self.tester.get_transaction_receipt(params[0])
            except TransactionNotFound:
                return None
            return {
                "transactionHash": r["transaction_hash"].hex()
                if not r["transaction_hash"].startswith("0x")
                else r["transaction_hash"],
                "status": hex(r["status"]),
                "blockNumber": hex(r["block_number"]),
                "gasUsed": hex(r["gas_used"]),
            }
        if method == "eth_blockNumber":
            return hex(self.tester.get_block_by_number("latest")["number"])
        raise ValueError(f"unsupported method {method}")

    def serve(self) -> tuple[str, ThreadingHTTPServer]:
        chain = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                try:
                    result = chain.handle(body.get("method"), body.get("params") or [])
                    payload = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
                except Exception as exc:  # noqa: BLE001 - JSON-RPC error shape
                    payload = {
                        "jsonrpc": "2.0", "id": body.get("id"),
                        "error": {"code": -32000, "message": str(exc)},
                    }
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):  # silence the test server
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}", server
