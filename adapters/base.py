from abc import ABC, abstractmethod
from models import Market, OrderBook, Funding, TradingFee

class ExchangeAdapter(ABC):
    name = ""

    @abstractmethod
    def connect(self): ...

    @abstractmethod
    def test_connection(self) -> bool: ...

    @abstractmethod
    def get_spot_markets(self): ...

    @abstractmethod
    def get_perpetual_markets(self): ...

    @abstractmethod
    def get_orderbook(self, market: Market, limit: int = 5) -> OrderBook: ...

    @abstractmethod
    def get_funding_rate(self, market: Market) -> Funding: ...

    @abstractmethod
    def get_trading_fee(self, market: Market) -> TradingFee: ...
