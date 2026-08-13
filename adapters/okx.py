from adapters.base import ExchangeAdapter

class OKXAdapter(ExchangeAdapter):
    name = "okx"
    def connect(self): raise NotImplementedError("OKX adapter will be implemented next.")
    def test_connection(self): return False
    def get_spot_markets(self): raise NotImplementedError
    def get_perpetual_markets(self): raise NotImplementedError
    def get_orderbook(self, market, limit=5): raise NotImplementedError
    def get_funding_rate(self, market): raise NotImplementedError
    def get_trading_fee(self, market): raise NotImplementedError
