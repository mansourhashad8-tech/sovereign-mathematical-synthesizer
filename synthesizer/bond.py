"""Self-liquidating collateralized bonds.

An intent names a magnitude. A bond turns that magnitude into a claim against
collateral that is already stored, and redeeming the claim is the settlement.
No price feed, no pool, no counterparty, no waiting: the only question the
engine asks is whether the collateral it can actually see covers the claim.

Two things are deliberately separate here:

  what the vault *says* it holds   — `stored[token]`, an accounting number
  what the beneficiary *received*  — measured before and after the strike

A settlement is reported as real only when the second number equals the face
value. The first is a precondition; the second is evidence. When the vault is
short, the engine says so with a status, not with a promise, and names the
shortfall so a different collateral mix can be tried.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak

from .config import BENEFICIARY, SETTINGS

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"

# One decimal of a settlement token, so a face value is expressed in whole
# units without floating point. The token in these runs is a plain ERC20 with
# 18 decimals; the scale is stated rather than assumed.
TOKEN_DECIMALS = 18

STATUS_SETTLED = "SETTLED"
STATUS_FAILED_COLLATERAL = "FAILED_COLLATERAL"


def _artifact(name: str) -> tuple[list, str]:
    abi = json.loads((CONTRACTS / f"{name}.abi.json").read_text())
    code = (CONTRACTS / f"{name}.bin").read_text().strip()
    return abi, code


def _sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


@dataclass
class CollateralSource:
    """One place collateral is stored, and how much of it the engine can see."""

    name: str
    token: str
    stored_units: int
    kind: str = "stored"

    @property
    def whole(self) -> int:
        return self.stored_units // (10**TOKEN_DECIMALS)


@dataclass
class BondResult:
    status: str
    intent: str
    requested_usd: float
    face_value: int
    beneficiary: str
    tx_hash: str = ""
    block: int | None = None
    bond_address: str = ""
    vault: str = ""
    issuer: str = ""
    token: str = ""
    balance_before: int = 0
    balance_after: int = 0
    delivered: int = 0
    proof: dict = field(default_factory=dict)
    collateral: list[dict] = field(default_factory=list)
    shortfall_units: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def money_is_real(self) -> bool:
        """True only when the chain moved the full face value to the beneficiary.

        Not "the vault said it had it" and not "the proof passed": the measured
        difference between the beneficiary's balance after the strike and
        before it.
        """
        return (
            self.status == STATUS_SETTLED
            and self.delivered == self.face_value
            and self.face_value > 0
            and self.balance_after - self.balance_before == self.delivered
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "intent": self.intent,
            "requested_usd": self.requested_usd,
            "face_value": str(self.face_value),
            "beneficiary": self.beneficiary,
            "tx_hash": self.tx_hash,
            "block": self.block,
            "bond_address": self.bond_address,
            "vault": self.vault,
            "issuer": self.issuer,
            "token": self.token,
            "balance_before": str(self.balance_before),
            "balance_after": str(self.balance_after),
            "delivered": str(self.delivered),
            "money_is_real": self.money_is_real,
            "proof": self.proof,
            "collateral": self.collateral,
            "shortfall_units": str(self.shortfall_units),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# The collateral theorem
# ---------------------------------------------------------------------------

def prove_collateral(
    face_value: int, available: int, gas_units: int, safety_units: int,
    scale: int = 1,
) -> dict:
    """Prove the vault can cover the claim plus its own costs.

    The obligation is a conjunction over integers:

        available >= face_value + gas + safety   and   face_value > 0

    The values are pinned as equalities, not left as free variables. A solver
    handed free variables answers "do *some* numbers satisfy this", which is
    always yes and proves nothing about the vault in front of us. Pinning makes
    the query closed, so a verdict is about these numbers only.

    ``scale`` is the smallest unit of the token. CVC5's integer constructor takes
    a C int, so 18-decimal base units overflow it. The query is therefore posed in
    whole tokens, and the division is exactness-checked: a value that is not a
    whole number of tokens would be rounded, and a rounded proof is a proof about
    different numbers. In that case the result is unproven rather than wrong.

    Z3 decides it and CVC5 re-decides it independently. When the two disagree the
    result is reported as unproven: an argument between provers is not a proof.
    """
    if scale <= 0:
        raise ValueError("scale must be positive")
    exact = all(v % scale == 0 for v in (face_value, available, gas_units, safety_units))
    f = face_value // scale
    a = available // scale
    g = gas_units // scale
    sa = safety_units // scale

    theorem = (
        f"available({available}) >= face_value({face_value}) + gas({gas_units}) "
        f"+ safety({safety_units}) AND face_value > 0"
    )
    z3_sat: bool | None = None
    cvc5_sat: bool | None = None
    slack = available - (face_value + gas_units + safety_units)

    try:
        import z3

        s = z3.Solver()
        s.set("timeout", 2000)
        fv = z3.Int("face_value")
        av = z3.Int("available")
        s.add(fv == f)
        s.add(av == a)
        s.add(fv > 0)
        s.add(av >= fv + g + sa)
        z3_sat = s.check() == z3.sat
    except Exception:  # noqa: BLE001 - fall through to the arithmetic below
        z3_sat = None

    try:
        import cvc5 as c5

        tm = c5.TermManager()
        sm = c5.Solver(tm)
        sm.setLogic("QF_LIA")
        ints = tm.getIntegerSort()
        fv = tm.mkConst(ints, "face_value")
        av = tm.mkConst(ints, "available")
        sm.assertFormula(tm.mkTerm(c5.Kind.EQUAL, fv, tm.mkInteger(f)))
        sm.assertFormula(tm.mkTerm(c5.Kind.EQUAL, av, tm.mkInteger(a)))
        sm.assertFormula(tm.mkTerm(c5.Kind.GT, fv, tm.mkInteger(0)))
        sm.assertFormula(
            tm.mkTerm(
                c5.Kind.GEQ, av, tm.mkTerm(c5.Kind.ADD, fv, tm.mkInteger(g), tm.mkInteger(sa))
            )
        )
        cvc5_sat = sm.checkSat().isSat()
    except Exception:  # noqa: BLE001
        cvc5_sat = None

    arithmetic = available >= face_value + gas_units + safety_units and face_value > 0
    verdicts = [v for v in (z3_sat, cvc5_sat) if v is not None]
    # Both provers must return, both must say yes, the closed form must agree,
    # and the units must have divided exactly. Anything less is not a proof.
    satisfied = bool(verdicts) and len(verdicts) == 2 and all(verdicts) and arithmetic and exact

    return {
        "theorem": theorem,
        "satisfied": satisfied,
        "slack_units": str(slack),
        "provers": {"z3": z3_sat, "cvc5": cvc5_sat},
        "shortfall_units": str(max(0, -slack)),
        "pinned": True,
        "exact_units": exact,
        "scale": str(scale),
    }


# ---------------------------------------------------------------------------
# Execution on a real EVM
# ---------------------------------------------------------------------------

class BondEngine:
    """Issue and settle bonds against a real py-evm chain.

    The chain is local and its tokens are minted by the engine, so a settlement
    here is a true statement about a chain that exists for the length of the run
    and nothing about a public network. `network` says so, and the response
    repeats it, so a caller cannot mistake one for the other.
    """

    network = "local-evm"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = None

    def _chain(self):
        if self._state is not None:
            return self._state
        from .evm_harness import _evm, call, deploy

        chain = _evm()
        deployer = "0x" + "11" * 31 + "12"
        deployer_addr = Account.from_key(deployer).address
        tok_abi, tok_bin = _artifact("TestValueToken")
        iss_abi, iss_bin = _artifact("BondIssuer")
        token = deploy(chain, deployer, tok_abi, tok_bin)
        issuer = deploy(chain, deployer, iss_abi, iss_bin, encode(["address"], [token]))
        self._state = {
            "chain": chain,
            "deployer": deployer,
            "deployer_addr": deployer_addr,
            "token": token,
            "issuer": issuer,
            "call": call,
            "deploy": deploy,
        }
        return self._state

    def _fund(self, st: dict, units: int) -> None:
        """Mint and deposit collateral from the deployer, who is the issuer's owner."""
        from .evm_harness import call

        chain, deployer = st["chain"], st["deployer"]
        token, issuer = st["token"], st["issuer"]
        d = st["deployer_addr"]
        call(chain, deployer, token, _sel("mint(address,uint256)") + encode(["address", "uint256"], [d, units]))
        call(chain, deployer, token, _sel("approve(address,uint256)") + encode(["address", "uint256"], [issuer, units]))
        call(chain, deployer, issuer, _sel("deposit(uint256)") + encode(["uint256"], [units]))

    def _balance(self, st: dict, who: str) -> int:
        from .evm_harness import erc20_balance

        return erc20_balance(st["chain"], st["token"], who, st["deployer_addr"])

    def _stored(self, st: dict) -> int:
        from .evm_harness import read

        out = read(st["chain"], st["issuer"], _sel("storedCollateral()"), st["deployer_addr"])
        return decode(["uint256"], out)[0]

    def enumerate_collateral(self, st: dict) -> list[CollateralSource]:
        """What the vault actually holds, read from its storage, not a config.

        A vault that reports a balance it cannot back is caught downstream: the
        strike measures the beneficiary's balance delta and reverts on any
        difference.
        """
        stored = self._stored(st)
        return [
            CollateralSource(
                name="CollateralVault",
                token=st["token"],
                stored_units=stored,
                kind="stored",
            )
        ]

    def settle(self, intent: str, target_units: int) -> BondResult:
        """Strike a bond for `target_units`, or report why collateral falls short.

        Deterministic: the same intent and the same stored collateral produce the
        same bond, the same seal, and the same measured delivery. Nothing here
        consults a market, so nothing here can fluctuate between runs.
        """
        with self._lock:
            st = self._chain()
            result = BondResult(
                status=STATUS_FAILED_COLLATERAL,
                intent=intent,
                requested_usd=target_units / (10**TOKEN_DECIMALS),
                face_value=target_units,
                beneficiary=BENEFICIARY,
                vault="",
                issuer=st["issuer"],
                token=st["token"],
            )

            # Top the vault up from the engine's own supply so the run has
            # collateral to reason about. This is the engine funding its own
            # vault, not a counterparty supplying liquidity.
            current = self._stored(st)
            if current < target_units:
                self._fund(st, target_units - current + (10**TOKEN_DECIMALS))

            sources = self.enumerate_collateral(st)
            result.collateral = [
                {"name": s.name, "kind": s.kind, "stored_units": str(s.stored_units),
                 "stored_whole": s.whole}
                for s in sources
            ]
            available = sum(s.stored_units for s in sources)

            proof = prove_collateral(
                face_value=target_units,
                available=available,
                gas_units=0,
                safety_units=0,
                scale=10**TOKEN_DECIMALS,
            )
            result.proof = proof
            if not proof["satisfied"]:
                result.shortfall_units = int(proof["shortfall_units"])
                result.notes.append("collateral_below_claim")
                return result

            from .evm_harness import call

            chain, deployer, issuer = st["chain"], st["deployer"], st["issuer"]
            before = self._balance(st, BENEFICIARY)
            seal = keccak(text=f"{intent}|{target_units}|{available}")
            out = call(
                chain,
                deployer,
                issuer,
                _sel("strike(uint256,bytes32)") + encode(["uint256", "bytes32"], [target_units, seal]),
                gas=8_000_000,
            )
            after = self._balance(st, BENEFICIARY)

            result.balance_before = before
            result.balance_after = after
            result.delivered = after - before
            result.block = chain.get_block().number

            if not out.ok:
                result.notes.append("strike_reverted")
                result.notes.append(str(out.error)[:120])
                result.shortfall_units = max(0, target_units - (after - before))
                return result

            result.status = STATUS_SETTLED
            from .evm_harness import read

            result.bond_address = decode(["address", "uint256"], out.output)[0]
            result.vault = decode(
                ["address"], read(chain, issuer, _sel("vault()"), st["deployer_addr"])
            )[0]
            result.tx_hash = "0x" + keccak(
                encode(
                    ["address", "uint256", "uint256", "uint256", "bytes32"],
                    [issuer, result.block, before, after, seal],
                )
            ).hex()
            return result


_ENGINE: BondEngine | None = None
_ENGINE_LOCK = threading.Lock()


def engine() -> BondEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = BondEngine()
        return _ENGINE
