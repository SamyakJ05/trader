from enum import StrEnum


class Broker(StrEnum):
    ZERODHA = "zerodha"
    GROWW = "groww"
    ICICI_BREEZE = "icici_breeze"
    PAPER = "paper"  # internal simulator, behaves like a broker


class Environment(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"      # stop-loss limit
    SL_M = "SL_M"  # stop-loss market


class ProductType(StrEnum):
    CNC = "CNC"    # delivery
    MIS = "MIS"    # intraday
    NRML = "NRML"  # F&O overnight


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"
    BFO = "BFO"
    MCX = "MCX"
    CDS = "CDS"


class Validity(StrEnum):
    DAY = "DAY"
    IOC = "IOC"


class OrderStatus(StrEnum):
    PENDING_RISK = "PENDING_RISK"
    REJECTED_RISK = "REJECTED_RISK"
    ACCEPTED = "ACCEPTED"          # passed risk, not yet at broker/simulator
    SUBMITTED = "SUBMITTED"        # sent to broker
    OPEN = "OPEN"                  # live in the book
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"          # broker rejected
    FAILED = "FAILED"              # transport/internal failure

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.REJECTED_RISK,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
        }

    @property
    def is_working(self) -> bool:
        return self in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}


class StrategyStatus(StrEnum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    KILLED = "KILLED"
    ERROR = "ERROR"


class SignalType(StrEnum):
    ENTRY_LONG = "ENTRY_LONG"
    EXIT_LONG = "EXIT_LONG"
    ENTRY_SHORT = "ENTRY_SHORT"
    EXIT_SHORT = "EXIT_SHORT"


class RiskDecision(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"  # this order blocked
    HALT = "HALT"    # kill switch / daily loss — all trading halted


class RiskRuleType(StrEnum):
    MAX_DAILY_LOSS = "MAX_DAILY_LOSS"
    MAX_ORDER_NOTIONAL = "MAX_ORDER_NOTIONAL"
    # The capital ceilings. Neither is implied by the per-order or per-symbol
    # limits: ten orders of 50k each pass MAX_ORDER_NOTIONAL and
    # MAX_POSITION_SIZE while committing 5 lakh.
    #
    # EXPOSURE caps what is held at once, so selling frees room.
    # TURNOVER caps what is bought in a day regardless of sells, which is what
    # stops a strategy churning the same capital repeatedly.
    MAX_TOTAL_EXPOSURE = "MAX_TOTAL_EXPOSURE"
    MAX_DAILY_TURNOVER = "MAX_DAILY_TURNOVER"
    MAX_POSITION_SIZE = "MAX_POSITION_SIZE"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    DUPLICATE_ORDER_COOLDOWN = "DUPLICATE_ORDER_COOLDOWN"
    MARKET_HOURS = "MARKET_HOURS"


class BrokerAccountStatus(StrEnum):
    DISCONNECTED = "disconnected"
    PENDING_AUTH = "pending_auth"
    CONNECTED = "connected"
    SESSION_EXPIRED = "session_expired"
    ERROR = "error"


class AuditEventType(StrEnum):
    USER_ACTION = "USER_ACTION"
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    RISK_CHECK = "RISK_CHECK"
    ORDER_REQUESTED = "ORDER_REQUESTED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    BROKER_RESPONSE = "BROKER_RESPONSE"
    ORDER_STATE_CHANGED = "ORDER_STATE_CHANGED"
    ORDER_FILL = "ORDER_FILL"
    KILL_SWITCH = "KILL_SWITCH"
    BROKER_SESSION = "BROKER_SESSION"
    BROKER_SYNC = "BROKER_SYNC"
    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED"
    AI_PROPOSAL = "AI_PROPOSAL"
    AI_DECISION = "AI_DECISION"


class AIProposalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AdapterStatus(StrEnum):
    WORKING = "working"      # implemented and exercised end-to-end
    SCAFFOLD = "scaffold"    # endpoints wired, NOT verified against the real broker
    PLANNED = "planned"


class OptionRight(StrEnum):
    """Which side of an option contract, or neither for a future.

    Broker-neutral: Breeze spells these "call"/"put"/"others" in its order
    payload, while its security master reports CE/PE/XX, and Kite encodes the
    right into the tradingsymbol instead. Adapters translate; this is what the
    platform stores and reasons about.

    OTHERS rather than None for a future, because Breeze requires the field to
    be present and populated on every F&O order -- an empty string is
    rejected. Making it explicit here keeps that from being an adapter-level
    surprise.
    """

    CALL = "CALL"
    PUT = "PUT"
    OTHERS = "OTHERS"
