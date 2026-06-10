"""Natural-language strategy generator. One structured-output call producing a
draft constrained to registered strategy kinds; nothing is persisted here —
the UI previews the draft and creates it through the normal strategies API."""

from app.engines.strategy.runner import STRATEGY_REGISTRY
from app.services.ai.llm import LLMClient, LLMError

GENERATOR_SYSTEM = """You translate a plain-English trading idea into a strategy \
config for a paper-trading platform on Indian markets (NSE). Available strategy kinds \
and their params:

- sma_crossover: params {"fast": int (short SMA window), "slow": int (long SMA window), \
"quantity": int (shares per signal), "exchange": "NSE", "product": "MIS"}
- ai_agent: an LLM decides BUY/SELL/HOLD per evaluation. params {"instructions": str \
(plain-English trading instructions), "quantity": int, "min_interval_seconds": int (>=60), \
"max_decisions_per_day": int (1-50), "exchange": "NSE", "product": "MIS"}

Pick the kind that fits the idea best. Symbols are NSE trading symbols (e.g. RELIANCE, \
TCS, INFY). Keep quantities small (paper account). rationale explains your choices in \
two sentences or fewer."""


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


async def generate(llm: LLMClient, prompt: str) -> dict:
    draft = await llm.generate_json(GENERATOR_SYSTEM, prompt, draft_schema())
    try:
        return validate_draft(draft)
    except ValueError as e:
        raise LLMError(f"Model produced an invalid strategy draft: {e}") from e
