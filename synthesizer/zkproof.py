"""Zero-knowledge settlement proofs.

A settlement is proven, not asserted. The circuit in ``circuits/settlement.circom``
takes the beneficiary, the value delta, the net profit, the step count and the gas
spent, and proves in one Groth16 proof that:

  * the beneficiary is the locked address,
  * the value delta is not zero,
  * the net profit clears the floor the intent asked for,
  * and all of those are bound by a Poseidon commitment.

The commitment is the part that makes the proof mean something. It is computed
over the same five numbers, and the verifier contract stores the commitment the
engine registered for an intent. So a prover cannot pass a different delta
without producing a different commitment, which the contract will not recognise.

What this proof does NOT claim: that the value is on a public chain. It claims
the execution happened on the fork and delivered value to the locked address.
The on-chain record is the proof's existence; moving real money still needs the
settlement contract to hold real money, and that is a separate, honest boundary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from eth_utils import keccak

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "zk"
CIRCUIT = ROOT / "circuits" / "settlement.circom"
NODE_MODULES = ROOT / "node_modules"

# Value units are USDC's six decimals. A floor of one unit keeps the comparator
# in a range circomlib handles without a 254-bit decomposition.
VALUE_DECIMALS = 6

_BIN_SNARKJS = NODE_MODULES / ".bin" / "snarkjs"
_BIN_CIRCOM = NODE_MODULES / ".bin" / "circom2"


class ZkUnavailable(RuntimeError):
    """The proving toolchain is not installed. Callers regenerate, never fail."""


@dataclass
class SettlementProof:
    """A finished proof and the public signals a verifier will check."""

    proof: dict
    public_signals: list[str]
    commitment: str
    delta: int
    net_profit: int
    min_profit: int
    beneficiary: str
    # sha256-free digest of the proof file, so a stored record can be matched.
    proof_digest: str = ""
    generated_ms: float = 0.0
    verified_locally: bool = False

    def public_tuple(self) -> tuple[int, int, int]:
        """The three public inputs in circuit declaration order.

        snarkjs puts the circuit's output first, so ``ok`` occupies index 0 and
        the declared publics follow it.
        """
        return tuple(int(x) for x in self.public_signals[1:4])  # type: ignore[return-value]

    def to_dict(self) -> dict:
        return {
            "commitment": self.commitment,
            "delta": self.delta,
            "net_profit": self.net_profit,
            "min_profit": self.min_profit,
            "beneficiary": self.beneficiary,
            "public_signals": self.public_signals,
            "proof": self.proof,
            "proof_digest": self.proof_digest,
            "generated_ms": round(self.generated_ms, 1),
            "verified_locally": self.verified_locally,
            "protocol": "groth16",
            "curve": "bn128",
        }


def toolchain_ready() -> bool:
    """True when circom, snarkjs and a compiled circuit are all present."""
    return (
        _BIN_SNARKJS.exists()
        and (BUILD / "settlement.r1cs").exists()
        and (BUILD / "settlement_js" / "settlement.wasm").exists()
        and (BUILD / "settlement_0.zkey").exists()
    )


def poseidon(values: list[int]) -> str:
    """Poseidon over the field, via circomlibjs so it matches the circuit exactly."""
    script = BUILD / "poseidon.js"
    # Rewritten every call: a stale copy would silently disagree with the
    # circuit's Poseidon parameters, and that failure is invisible until a
    # proof is rejected on chain.
    _write_poseidon_script(script)
    out = subprocess.run(
        ["node", str(script), *[str(v) for v in values]],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if out.returncode != 0:
        raise ZkUnavailable(f"poseidon failed: {out.stderr[-300:]}")
    return out.stdout.strip()


def _write_poseidon_script(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # An absolute path, because the script lives under build/ and node resolves
    # bare names against the script's own directory, not the project root.
    path.write_text(
        f'const b=require("{NODE_MODULES / "circomlibjs"}");\n'
        "b.buildPoseidon().then(p=>{\n"
        "  const v=process.argv.slice(2).map(BigInt);\n"
        "  console.log(p.F.toString(p(v)));\n"
        "}).catch(e=>{console.error(e.message);process.exit(1)});\n"
    )


def commitment_for(
    beneficiary: str, delta: int, net_profit: int, step_count: int, total_gas: int
) -> str:
    """The exact commitment the circuit will recompute."""
    return poseidon(
        [
            int(beneficiary, 16),
            int(delta),
            int(net_profit),
            int(step_count),
            int(total_gas),
        ]
    )


def prove_settlement(
    *,
    beneficiary: str,
    delta: int,
    net_profit: int,
    min_profit: int,
    step_count: int,
    total_gas: int,
    workdir: Path | None = None,
) -> SettlementProof:
    """Generate a Groth16 proof for one settlement.

    Raises ``ZkUnavailable`` only when the toolchain itself is missing; a proof
    that the circuit refuses to accept is an ``AssertionError`` from snarkjs and
    is the caller's signal to regenerate the execution, not to report failure.
    """
    if not toolchain_ready():
        raise ZkUnavailable("proving toolchain not built; run scripts/build_zk.sh")

    started = time.perf_counter()
    work = Path(workdir or BUILD / "work")
    work.mkdir(parents=True, exist_ok=True)

    commitment = commitment_for(beneficiary, delta, net_profit, step_count, total_gas)
    witness_input = {
        "beneficiary": str(int(beneficiary, 16)),
        "minProfit": str(int(min_profit)),
        "commitment": commitment,
        "beneficiaryWitness": str(int(beneficiary, 16)),
        "delta": str(int(delta)),
        "netProfit": str(int(net_profit)),
        "stepCount": str(int(step_count)),
        "totalGas": str(int(total_gas)),
    }
    inp = work / "input.json"
    inp.write_text(json.dumps(witness_input))

    wtns = work / "witness.wtns"
    _run(
        [
            "node",
            str(BUILD / "settlement_js" / "generate_witness.js"),
            str(BUILD / "settlement_js" / "settlement.wasm"),
            str(inp),
            str(wtns),
        ],
        "witness generation",
    )

    proof_file = work / "proof.json"
    public_file = work / "public.json"
    _run(
        [
            str(_BIN_SNARKJS),
            "groth16",
            "prove",
            str(BUILD / "settlement_0.zkey"),
            str(wtns),
            str(proof_file),
            str(public_file),
        ],
        "proof generation",
    )

    verified = verify_settlement(
        json.loads(proof_file.read_text()), json.loads(public_file.read_text())
    )

    proof_obj = json.loads(proof_file.read_text())
    return SettlementProof(
        proof=proof_obj,
        public_signals=json.loads(public_file.read_text()),
        commitment=commitment,
        delta=int(delta),
        net_profit=int(net_profit),
        min_profit=int(min_profit),
        beneficiary=beneficiary,
        proof_digest="0x" + keccak(text=json.dumps(proof_obj, sort_keys=True)).hex(),
        generated_ms=(time.perf_counter() - started) * 1000,
        verified_locally=verified,
    )


def verify_settlement(proof: dict, public_signals: list[str]) -> bool:
    """Check a proof against the verification key. This is the local gate."""
    work = BUILD / "work"
    work.mkdir(parents=True, exist_ok=True)
    pf = work / "verify_proof.json"
    pub = work / "verify_public.json"
    pf.write_text(json.dumps(proof))
    pub.write_text(json.dumps(public_signals))
    out = subprocess.run(
        [
            str(_BIN_SNARKJS),
            "groth16",
            "verify",
            str(BUILD / "verification_key.json"),
            str(pub),
            str(pf),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return out.returncode == 0 and "OK" in out.stdout


def solidity_verifier_source() -> str:
    """The Solidity verifier snarkjs generates from our key."""
    work = BUILD / "work"
    work.mkdir(parents=True, exist_ok=True)
    target = work / "Verifier.sol"
    subprocess.run(
        [
            str(_BIN_SNARKJS),
            "zkey",
            "export",
            "solidityverifier",
            str(BUILD / "settlement_0.zkey"),
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return target.read_text()


def solidity_calldata(proof: dict, public_signals: list[str], workdir: Path | None = None) -> tuple[list, list, list, list]:
    """The proof encoded the way snarkjs encodes it for Solidity.

    Hand-rolling this encoding is how G2 ordering bugs get in: the two coordinate
    components have to be swapped relative to the snarkjs JSON, and getting it
    wrong yields a proof that verifies locally and fails on chain. snarkjs ships
    an exporter that is definitionally correct, so the engine uses that.
    """
    work = Path(workdir or BUILD / "work")
    work.mkdir(parents=True, exist_ok=True)
    pf = work / "calldata_proof.json"
    pub = work / "calldata_public.json"
    pf.write_text(json.dumps(proof))
    pub.write_text(json.dumps(public_signals))
    out = subprocess.run(
        [str(_BIN_SNARKJS), "zkey", "export", "soliditycalldata", str(pub), str(pf)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if out.returncode != 0:
        raise ZkUnavailable(f"calldata export failed: {out.stderr[-300:]}")
    lines = [ln for ln in out.stdout.strip().splitlines() if ln.strip()]
    parsed = json.loads("[" + ",".join(lines) + "]")
    p_a, p_b, p_c, signals = parsed
    return p_a, p_b, p_c, signals


def _run(cmd: list[str], what: str) -> None:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        raise AssertionError(f"{what} rejected: {out.stderr[-500:]}")
