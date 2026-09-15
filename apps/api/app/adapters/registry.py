from app.adapters.base import BrokerAdapter
from app.adapters.groww.adapter import GrowwAdapter
from app.adapters.icici_breeze.adapter import BreezeAdapter
from app.adapters.paper.adapter import PaperAdapter
from app.adapters.zerodha.adapter import ZerodhaAdapter
from app.core.config import BrokerEnvCredentials
from app.db.models import BrokerAccount
from app.domain.enums import Broker, Environment

_ADAPTERS: dict[Broker, type[BrokerAdapter]] = {
    Broker.PAPER: PaperAdapter,
    Broker.ZERODHA: ZerodhaAdapter,
    Broker.GROWW: GrowwAdapter,
    Broker.ICICI_BREEZE: BreezeAdapter,
}


def get_adapter(account: BrokerAccount) -> BrokerAdapter:
    """The adapter for this account's real broker, whatever its environment.

    Read paths want this even in paper environment: the whole point of the
    verification playbook's early stages is pulling a real account's profile,
    funds, holdings and security master while trading stays simulated. Use
    `get_trading_adapter` instead for anything that places, modifies or
    cancels an order.
    """
    broker = Broker(account.broker)
    credentials = BrokerEnvCredentials(account.credential_ref or "")
    return _ADAPTERS[broker](account, credentials)


def get_trading_adapter(account: BrokerAccount) -> BrokerAdapter:
    """The adapter an ORDER should be dispatched through.

    A real broker account in paper environment simulates: that is what paper
    environment means, and quotes.reference_price already prices it off the
    simulator for exactly this reason. Order dispatch used to decide that for
    itself and then call get_adapter(), which dispatches on broker alone --
    so a real Breeze account in paper environment took the simulated branch
    and sent the order to ICICI anyway, while the paper engine booked a
    fabricated fill for it. The live gate sat in the branch below and was
    never reached.

    PaperAdapter runs entirely off internal state and needs no broker
    credentials, so it can stand in for any broker.
    """
    if account.environment == Environment.PAPER.value:
        return PaperAdapter(account, BrokerEnvCredentials(account.credential_ref or ""))
    return get_adapter(account)
