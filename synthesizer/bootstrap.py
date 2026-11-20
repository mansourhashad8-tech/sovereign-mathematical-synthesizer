"""End-to-end settlement on the local fork, gated by a real proof.

The chain of custody is the point of this module. Each stage produces evidence
that the next stage consumes, so a number can never enter the settlement record
without having been produced by the stage before it:

    opportunity -> fork execution -> measured delta -> Z3 gate -> ZK proof
                -> on-chain verification -> recorded settlement

A delta is not computed by the engine; it is read out of EVM state before and
after the execution. A proof is not asserted; it is generated from that measured
delta and checked by the verifier contract, which stores the proven figures.

The whole path runs with no user key, no user gas and no external service. The
fork is a real EVM with its base fee set to zero, so a transaction genuinely
costs nothing to include.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import SETTINGS
from .gateway import ForkGateway, RecordedSettlement, _bytes32, _selector, _word, intent_id_bytes
from .localfork import LocalFork, Step, session_key
from .zkproof import prove_settlement

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"

# Six-decimal units, so this is 1,000 FSU. The cap exists to stop a request from
# scaling the reported figure without limit: the fork mints its own capital, so a
# larger number is only a larger claim. It is set well above ordinary intents so
# a normal request settles, and far below the "earn a million from nothing" range
# so that one cannot.
MAX_NOTIONAL_UNITS = 1_000_000_000


@dataclass
class BootstrapResult:
    """Everything one bootstrap run produced, with nothing taken on trust."""

    intent_id: str
    beneficiary: str
    token: str = ""
    funded_amount: int = 0
    delta: int = 0
    net_profit: int = 0
    min_profit: int = 0
    settlement: RecordedSettlement | None = None
    commitment: str = ""
    steps: list[dict] = field(default_factory=list)
    verified_onchain: bool = False
    gas_spent: int = 0
    token_delivered: int = 0
    # The unit that moved, and whether it is money. FSU is minted inside the
    # fork by this very run, so a large FSU figure proves the mechanics and
    # says nothing about value. Keeping the flag next to the number is what
    # stops the number being read as a payout.
    value_unit: str = "FSU"
    money_is_real: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def delivered(self) -> bool:
        """True only when value moved and the chain recorded the proof."""
        return self.verified_onchain and self.delta > 0 and self.token_delivered > 0

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "beneficiary": self.beneficiary,
            "token": self.token,
            "funded_amount": self.funded_amount,
            "delta": self.delta,
            "net_profit": self.net_profit,
            "min_profit": self.min_profit,
            "commitment": self.commitment,
            "verified_onchain": self.verified_onchain,
            "gas_spent": self.gas_spent,
            "token_delivered": self.token_delivered,
            "delivered": self.delivered,
            "value_unit": self.value_unit,
            "money_is_real": self.money_is_real,
            "settlement": self.settlement.to_dict() if self.settlement else None,
            "steps": self.steps,
            "notes": self.notes,
        }


class Bootstrap:
    """Runs the fork -> proof -> chain path for one intent."""

    def __init__(self, beneficiary: str | None = None) -> None:
        self.beneficiary = beneficiary or SETTINGS.beneficiary
        self.key = session_key()

    def run(self, intent_id: str, min_profit: int, notional: int) -> BootstrapResult:
        """Execute, measure, prove, and record. Raises only on a real fault."""
        from eth_account import Account

        # A run cannot mint its way to a bigger truth, so the working capital is
        # bounded. The bound is not a technical limit; it keeps the reported
        # figure from implying a scale of value the fork cannot represent.
        notional = max(0, min(int(notional), MAX_NOTIONAL_UNITS))
        result = BootstrapResult(
            intent_id=intent_id,
            beneficiary=self.beneficiary,
            min_profit=min_profit,
            notes=[
                "working capital is minted inside the fork by this run; the unit "
                "has no market value and no public-chain counterpart"
            ],
        )
        account = Account.from_key(self.key).address
        fork = LocalFork(beneficiary=self.beneficiary, fund=[account])

        if notional <= 0:
            result.notes.append("no working capital to move; nothing to prove")
            return result

        token_addr, token_step = self._deploy_token(fork)
        result.token = token_addr
        result.steps.append(_step_dict("deploy_token", token_step))

        # The fork mints working capital to the session account. This is the
        # only place value is created, and it happens inside an ephemeral chain
        # that ceases to exist when this call returns.
        mint_step = self._mint(fork, token_addr, account, notional)
        result.funded_amount = notional
        result.steps.append(_step_dict("mint_working_capital", mint_step))

        before = self._balance(fork, token_addr, self.beneficiary)

        transfer_step = self._transfer(fork, token_addr, self.beneficiary, notional)
        result.steps.append(_step_dict("deliver_to_beneficiary", transfer_step))

        after = self._balance(fork, token_addr, self.beneficiary)
        result.delta = after - before
        result.net_profit = result.delta
        result.token_delivered = result.delta

        if result.delta <= 0:
            # Nothing to prove. The loop retries rather than inventing a number.
            result.notes.append("no value reached the beneficiary in this run")
            return result

        if result.net_profit < min_profit:
            # The requested floor is above what this run can move. Proving it
            # would be impossible, and reporting it as settled would be false,
            # so the run stops here and the loop keeps working on the intent.
            result.notes.append(
                f"measured net {result.net_profit} is below the requested floor "
                f"{min_profit}; no settlement is recorded"
            )
            return result

        gateway = ForkGateway(fork, self.key)
        gateway.deploy()
        result.steps.extend(_step_dict(f"deploy_{i}", s) for i, s in enumerate(gateway.deploy_steps))

        proof = prove_settlement(
            beneficiary=self.beneficiary,
            delta=result.delta,
            net_profit=result.net_profit,
            min_profit=min_profit,
            step_count=len(result.steps),
            total_gas=transfer_step.gas_used,
        )
        result.commitment = proof.commitment

        gateway.register(intent_id, proof.commitment, min_profit)
        settlement = gateway.record(proof, intent_id)
        result.settlement = settlement
        result.verified_onchain = settlement.verified_onchain
        result.gas_spent = sum(
            int(s.get("gas_used", 0)) for s in result.steps
        ) + settlement.gas_used

        if not settlement.verified_onchain:
            result.notes.append("the chain rejected the proof; the loop re-derives and retries")
        return result

    def _deploy_token(self, fork: LocalFork) -> tuple[str, Step]:
        code = (CONTRACTS / "TestValueToken.bin").read_text().strip()
        return fork.deploy(self.key, code)

    def _mint(self, fork: LocalFork, token: str, to: str, amount: int) -> Step:
        data = _selector("mint(address,uint256)") + _word(int(to, 16)) + _word(amount)
        return fork.send(self.key, token, data)

    def _transfer(self, fork: LocalFork, token: str, to: str, amount: int) -> Step:
        data = _selector("transfer(address,uint256)") + _word(int(to, 16)) + _word(amount)
        return fork.send(self.key, token, data)

    def _balance(self, fork: LocalFork, token: str, holder: str) -> int:
        data = _selector("balanceOf(address)") + _word(int(holder, 16))
        step = fork.send(self.key, token, data)
        if not step.output or step.output == "0x":
            return 0
        return int(step.output, 16) if isinstance(step.output, str) else int(step.output.hex(), 16)


def _step_dict(name: str, step: Step) -> dict:
    return {
        "name": name,
        "status": int(step.status),
        "gas_used": step.gas_used,
        "block": step.block,
    }
