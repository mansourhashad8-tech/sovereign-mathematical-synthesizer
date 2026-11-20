#!/usr/bin/env bash
# Rebuild the ZK artifacts from the circuit source.
#
# The powers of tau are generated locally, so this script is the whole ceremony:
# there is no trusted setup file to download and no external coordinator to
# trust. It is deterministic apart from the random contribution, which is why
# the resulting key is committed as build/zk/settlement_0.zkey by CI rather than
# regenerated per deploy.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p build/zk

CIRCOM=./node_modules/.bin/circom2
SNARKJS=./node_modules/.bin/snarkjs

echo "== circuit =="
$CIRCOM circuits/settlement.circom --r1cs --wasm -o build/zk

echo "== powers of tau =="
if [ ! -f build/zk/pot12_final.ptau ]; then
  $SNARKJS powersoftau new bn128 12 build/zk/pot12_0.ptau
  $SNARKJS powersoftau contribute build/zk/pot12_0.ptau build/zk/pot12_1.ptau \
    --name="synth" -e="$(head -c 32 /dev/urandom | sha256sum | cut -d' ' -f1)"
  $SNARKJS powersoftau prepare phase2 build/zk/pot12_1.ptau build/zk/pot12_final.ptau
fi
$SNARKJS powersoftau verify build/zk/pot12_final.ptau | tail -1

echo "== groth16 setup =="
$SNARKJS groth16 setup build/zk/settlement.r1cs build/zk/pot12_final.ptau build/zk/settlement_0.zkey
$SNARKJS zkey export verificationkey build/zk/settlement_0.zkey build/zk/verification_key.json

echo "== solidity verifier =="
python3 - <<'PY'
from synthesizer import zkproof
from pathlib import Path
Path("build/zk/Verifier.sol").write_text(zkproof.solidity_verifier_source())
print("wrote build/zk/Verifier.sol")
PY

echo "== contracts =="
node scripts/compile_contracts.js
echo "done"
