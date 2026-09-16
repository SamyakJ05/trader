"""Natural-language strategy generator. One structured-output call producing a
draft constrained to registered strategy kinds; nothing is persisted here —
the UI previews the draft and creates it through the normal strategies API."""

from app.engines.strategy.runner import STRATEGY_REGISTRY
from app.services.ai.llm import LLMClient, LLMError

GENERATOR_SYSTEM = """You translate a plain-English trading idea into a strategy \
config for an algorithmic trading platform on Indian markets. Available strategy kinds \
and their params:

- sma_crossover: params {"fast": int (short SMA window), "slow": int (long SMA window), \
"quantity": int (shares per signal)}
- ai_agent: an LLM decides BUY/SELL/HOLD per evaluation. params {"instructions": str \
(plain-English trading instructions), "quantity": int, "min_interval_seconds": int (>=60), \
"max_decisions_per_day": int (1-50)}

Pick the kind that fits the idea best. Keep quantities small. rationale explains your \
choices in two sentences or fewer."""

# The account's own constraints, appended per request. These used to be baked
# into the prompt above as `"exchange": "NSE", "product": "MIS"` on both
# templates -- which is wrong for a Breeze account on both counts, so every
# strategy generated for one emitted orders the adapter refuses. The model
# cannot infer any of this from the schema.
_BROKER_RULES = {
    "icici_breeze": (
        'This account is ICICI Direct Breeze. Set "product": "CNC" (it has no '
        'MIS intraday product) and "exchange": "NSE" (BSE and MCX are '
        "unavailable). Breeze accepts no market orders, so also set "
        '"order_type": "LIMIT" and optionally "limit_buffer_pct" (0.003 = '
        "0.30% through the last price). Symbols are ICICI's own stock codes, "
        "NOT NSE tickers: RELIANCE is RELIND, INFOSYS is INFTEC. If you are "
        "not certain of a code, say so in the rationale rather than guessing "
        "— an unknown symbol is rejected when the strategy is created."
    ),
}

_DEFAULT_RULES = (
    'Set "exchange": "NSE" and "product": "MIS". Symbols are NSE trading '
    "symbols (e.g. RELIANCE, TCS, INFY)."
)

_ENVIRONMENT_RULES = {
    "paper": "This is a PAPER account: fills are simulated, no real money moves.",
    "live": (
        "This account is LIVE: the strategy you write will place REAL orders "
        "with REAL money once started. Keep quantities minimal and prefer "
        "conservative parameters."
    ),
}


def build_system_prompt(broker: str | None, environment: str | None) -> str:
    """The generator rules plus the target account's own constraints."""
    parts = [GENERATOR_SYSTEM, _BROKER_RULES.get(broker or "", _DEFAULT_RULES)]
    note = _ENVIRONMENT_RULES.get(environment or "")
    if note:
        parts.append(note)
    return "\n\n".join(parts)


def draft_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short human-readable strategy name"},
            "kind": {"type": "string", "enum": sorted(STRATEGY_REGISTRY.keys())},
            "symbols": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "params": {"type": "object", "additionalProperties": True},
            "rationale": {"type": "string"},
        },
        "required": ["name", "kind", "symbols", "params", "rationale"],
        "additionalProperties": False,
    }


def validate_draft(draft: dict) -> dict:
    """Re-validate model output server-side. Raises ValueError on bad drafts."""
    kind = draft.get("kind")
    if kind not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy kind {kind!r}; available: {sorted(STRATEGY_REGISTRY)}")
    name = str(draft.get("name", "")).strip()
    if not name:
        raise ValueError("Draft is missing a name")
    symbols = [str(s).strip().upper() for s in draft.get("symbols", []) if str(s).strip()]
    if not symbols:
        raise ValueError("Draft has no symbols")
    params = draft.get("params")
    if not isinstance(params, dict):
        raise ValueError("Draft params must be an object")
    return {
        "name": name[:128],
        "kind": kind,
        "symbols": symbols[:10],
        "params": params,
        "rationale": str(draft.get("rationale", "")).strip(),
    }


async def generate(
    llm: LLMClient,
    prompt: str,
    *,
    broker: str | None = None,
    environment: str | None = None,
) -> dict:
    draft = await llm.generate_json(
        build_system_prompt(broker, environment), prompt, draft_schema()
    )
    try:
        return validate_draft(draft)
    except ValueError as e:
        raise LLMError(f"Model produced an invalid strategy draft: {e}") from e
