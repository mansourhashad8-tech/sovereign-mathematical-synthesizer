"""Formal Analysis: Arabic intent -> a mathematical constraint system.

There are two layers:

1. A deterministic parser (always available, no network) that extracts the
   objective, asset, and target amount from Arabic or English text and always
   yields a valid constraint system.
2. An optional LLM pass that refines the reading of an ambiguous intent.

The deterministic layer is the floor: if the model is unreachable, the system
still produces a complete constraint system and proceeds.
"""

from __future__ import annotations

import re

from .brain import Brain
from .config import SETTINGS
from .models import Constraints, Goal, stable_hash

# Arabic-Indic digits -> ASCII.
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# Keyword tables (Arabic + transliteration + English).
_EARN_WORDS = ["اربح", "أربح", "ربح", "أرباح", "ارباح", "عائد", "دخل", "فلوس", "أموال",
               "اموال", "مكسب", "profit", "earn", "income", "yield", "money", "usd", "دولار"]
_SWAP_WORDS = ["أبدل", "ابدل", "أحوّل", "احول", "تبديل", "مبادلة", "swap", "convert", "exchange"]
_LIQ_WORDS = ["سيولة", "liquidity", "lp", "pool", "مزود", "توفير سيولة", "market making", "ماركت"]
_BRIDGE_WORDS = ["جسر", "أعبر", "اعبر", "bridge", "نقل بين", "cross"]
_STAKE_WORDS = ["ستيك", "أراهن", "اراهن", "stake", "staking", "رين", "yield farm"]

_ASSET_WORDS = {
    "USDC": ["usdc", "يوسي", "usdc"],
    "USDT": ["usdt", "tether"],
    "ETH": ["eth", "إيثيريوم", "ايثيريوم", "ايثريوم", "ethereum"],
    "DAI": ["dai", "داي"],
    "SOL": ["sol", "سولانا", "solana"],
}

# "no capital / no key / no signup" style constraints.
_NO_CAPITAL = ["بدون راس مال", "بدون رأس مال", "بلا راس مال", "بلا رأس مال",
               "no capital", "without capital", "zero capital", "بدون فلوس"]
_NO_KEY = ["بدون مفتاح", "بلا مفتاح", "no key", "without key", "بدون مفتاح خاص",
           "no private key", "بدون محفظة"]
_NO_SIGNUP = ["بدون تسجيل", "بدون حساب", "no signup", "no account", "no kyc",
              "بدون كاي", "بدون هوية", "no registration"]
_NO_GAS = ["بدون غاز", "بلا غاز", "no gas", "بدون رسوم", "gasless", "بدون رسوم غاز"]
_FAST = ["فورا", "فوراً", "حالا", "حالاً", "الان", "الآن", "now", "immediately", "فوري"]

_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
_AMOUNT_WORDS = {
    "مليار": 1_000_000_000, "billion": 1_000_000_000,
    "مليون": 1_000_000, "million": 1_000_000,
    "الف": 1_000, "ألف": 1_000, "thousand": 1_000,
}


def _normalise(text: str) -> str:
    text = text.translate(_AR_DIGITS)
    # Strip Arabic diacritics.
    text = re.sub(r"[\u064B-\u0652\u0670\u0640]", "", text)
    return text.strip()


def _find_amount(text: str) -> float:
    lowered = text.lower()
    match = _NUMBER_RE.search(lowered)
    base = float(match.group(1)) if match else 0.0
    for word, mult in _AMOUNT_WORDS.items():
        if word in lowered:
            base = base * mult if base else float(mult)
            break
    return base


def _pick_objective(text: str) -> str:
    lowered = text.lower()
    for words, name in (
        (_LIQ_WORDS, "provide_liquidity"),
        (_BRIDGE_WORDS, "bridge"),
        (_STAKE_WORDS, "stake"),
        (_SWAP_WORDS, "swap"),
        (_EARN_WORDS, "earn"),
    ):
        if any(w in lowered or w in text for w in words):
            return name
    return "earn"


def _pick_asset(text: str) -> str:
    lowered = text.lower()
    for asset, words in _ASSET_WORDS.items():
        if any(w in lowered for w in words):
            return asset
    return "USDC"


def parse_deterministic(raw_intent: str) -> Constraints:
    """Always-succeeds parse. The floor."""
    text = _normalise(raw_intent)
    lowered = text.lower()
    objective = _pick_objective(text)
    asset = _pick_asset(text)
    target = _find_amount(text)

    extra: list[str] = []
    no_capital = any(w in lowered or w in text for w in _NO_CAPITAL)
    no_key = any(w in lowered or w in text for w in _NO_KEY)
    no_signup = any(w in lowered or w in text for w in _NO_SIGNUP)
    no_gas = any(w in lowered or w in text for w in _NO_GAS)
    fast = any(w in lowered or w in text for w in _FAST)
    for flag, name in ((no_capital, "no_capital"), (no_key, "no_key"),
                       (no_signup, "no_signup"), (no_gas, "no_user_gas"),
                       (fast, "immediate")):
        if flag:
            extra.append(name)

    goal = Goal(
        objective=objective,
        asset=asset,
        target_amount=target,
        raw_intent=raw_intent,
    )
    # NetProfit > 0, tightened to the target when the user named one.
    floor = SETTINGS.min_net_profit_usd
    if target > 0:
        floor = max(floor, target)
    # The security posture is a system invariant, never a user preference: the
    # engine is non-custodial, never needs the user's key, and never asks the
    # user for gas. The intent's wording is recorded in `extra` for the
    # narrative, but it can never relax these flags.
    return Constraints(
        goal=goal,
        net_profit_usd_min=floor,
        allow_custody=False,
        allow_user_key=False,
        allow_user_gas=False,
        require_atomic=True,
        require_simulation=True,
        beneficiary=SETTINGS.beneficiary,
        extra=extra,
    )


_SYSTEM = (
    "You are the formal-analysis stage of a deterministic intent compiler. "
    "Read the user's intent (Arabic or English) and reply with ONLY a JSON "
    "object with keys: objective (one of earn, swap, provide_liquidity, "
    "bridge, stake), asset (a ticker like USDC/ETH), target_amount (number), "
    "constraints (array of short snake_case strings such as no_capital, "
    "no_key, no_signup, no_user_gas, immediate). No prose."
)


def parse(raw_intent: str, brain: Brain | None = None) -> tuple[Constraints, str]:
    """Return (constraints, source) where source is 'deterministic' or 'llm+det'."""
    base = parse_deterministic(raw_intent)
    brain = brain or Brain()
    data = brain.complete_json(_SYSTEM, raw_intent)
    if not data:
        return base, "deterministic"

    objective = str(data.get("objective", base.goal.objective)).strip().lower()
    if objective not in {"earn", "swap", "provide_liquidity", "bridge", "stake"}:
        objective = base.goal.objective
    asset = str(data.get("asset", base.goal.asset)).strip().upper() or base.goal.asset
    try:
        target = float(data.get("target_amount", base.goal.target_amount) or 0.0)
    except (TypeError, ValueError):
        target = base.goal.target_amount
    if target <= 0:
        target = base.goal.target_amount

    extra = list(dict.fromkeys(base.extra + [str(c) for c in data.get("constraints", [])]))

    goal = Goal(objective=objective, asset=asset, target_amount=target,
                raw_intent=raw_intent)
    floor = SETTINGS.min_net_profit_usd
    if target > 0:
        floor = max(floor, target)
    constraints = Constraints(
        goal=goal,
        net_profit_usd_min=floor,
        allow_custody=False,
        allow_user_key=False,
        allow_user_gas=False,
        require_atomic=True,
        require_simulation=True,
        beneficiary=SETTINGS.beneficiary,
        extra=extra,
    )
    return constraints, "llm+det"


def intent_id(constraints: Constraints) -> str:
    g = constraints.goal
    return stable_hash(g.objective, g.asset, f"{g.target_amount:.6f}", g.raw_intent)
