// Compile the verifier and the settlement gateway to bin/abi artifacts.
//
// The verifier source is generated from the Groth16 key by snarkjs, so it is
// never hand-edited: the constants in it must match the key that signs the
// proofs. Rebuilding is `scripts/build_zk.sh`.
const solc = require("solc");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const contracts = path.join(root, "contracts");

const sources = {
  "Verifier.sol": {
    content: fs.readFileSync(path.join(root, "build/zk/Verifier.sol"), "utf8"),
  },
  "SettlementGateway.sol": {
    content: fs.readFileSync(path.join(contracts, "SettlementGateway.sol"), "utf8"),
  },
};

const input = {
  language: "Solidity",
  sources,
  settings: {
    optimizer: { enabled: true, runs: 200 },
    outputSelection: { "*": { "*": ["abi", "evm.bytecode.object"] } },
  },
};

const out = JSON.parse(solc.compile(JSON.stringify(input)));

const errors = (out.errors || []).filter((e) => e.severity === "error");
if (errors.length) {
  errors.forEach((e) => console.error(e.formattedMessage));
  process.exit(1);
}

for (const file of Object.keys(out.contracts || {})) {
  for (const [name, contract] of Object.entries(out.contracts[file])) {
    const code = contract.evm.bytecode.object;
    if (!code) continue;
    fs.writeFileSync(path.join(contracts, `${name}.bin`), code);
    fs.writeFileSync(path.join(contracts, `${name}.abi.json`), JSON.stringify(contract.abi, null, 1));
    console.log(`${name}: ${code.length / 2} bytes of bytecode`);
  }
}
