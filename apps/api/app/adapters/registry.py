from app.adapters.base import BrokerAdapter
from app.adapters.groww.adapter import GrowwAdapter
from app.adapters.icici_breeze.adapter import BreezeAdapter
from app.adapters.paper.adapter import PaperAdapter
from app.adapters.zerodha.adapter import ZerodhaAdapter
from app.core.config import BrokerEnvCredentials
from app.db.models import BrokerAccount
from app.domain.enums import Broker

_ADAPTERS: dict[Broker, type[BrokerAdapter]] = {
    Broker.PAPER: PaperAdapter,
    Broker.ZERODHA: ZerodhaAdapter,
    Broker.GROWW: GrowwAdapter,
    Broker.ICICI_BREEZE: BreezeAdapter,
}


def get_adapter(account: BrokerAccount) -> BrokerAdapter:
    broker = Broker(account.broker)
    credentials = BrokerEnvCredentials(account.credential_ref or "")
    return _ADAPTERS[broker](account, credentials)
