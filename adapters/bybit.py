import ccxt
from models import Market, OrderBook, OrderBookLevel, Funding, TradingFee
from adapters.base import ExchangeAdapter

class BybitAdapter(ExchangeAdapter):
    name = "bybit"

    def __init__(self, api_key="", api_secret=""):
        self.exchange = ccxt.bybit({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {"defaultType": "spot"},
        })
        self._markets_loaded = False

    def connect(self):
        self.exchange.load_markets()
        self._markets_loaded = True

    def test_connection(self):
        try:
            self.connect()
            self.exchange.fetch_balance({"type": "spot"})
            return True
        except Exception:
            return False

    def _ensure_markets(self):
        if not self._markets_loaded:
            self.connect()

    def _market(self, m, market_type):
        return Market(
            exchange=self.name,
            symbol=m["symbol"],
            base=m.get("base") or "",
            quote=m.get("quote") or "",
            market_type=market_type,
            active=m.get("active") is not False,
            contract_size=float(m.get("contractSize") or 1),
            settle=m.get("settle"),
            linear=m.get("linear"),
        )

    def get_spot_markets(self):
        self._ensure_markets()
        return [
            self._market(m, "spot")
            for m in self.exchange.markets.values()
            if m.get("spot") is True and m.get("quote") == "USDT"
        ]

    def get_perpetual_markets(self):
        self._ensure_markets()
        return [
            self._market(m, "perpetual")
            for m in self.exchange.markets.values()
            if m.get("swap") is True
            and m.get("quote") == "USDT"
            and m.get("linear") is True
        ]

    def get_orderbook(self, market, limit=5):
        limit = min(max(int(limit), 1), 5)
        params = {"category": "spot" if market.market_type == "spot" else "linear"}
        raw = self.exchange.fetch_order_book(market.symbol, limit=limit, params=params)

        def convert(items):
            result = []
            for x in (items or [])[:limit]:
                if len(x) >= 2:
                    p, q = float(x[0]), float(x[1])
                    if p > 0 and q > 0:
                        result.append(OrderBookLevel(p, q))
            return result

        return OrderBook(
            self.name, market.symbol, market.market_type,
            convert(raw.get("asks")), convert(raw.get("bids"))
        )

    def get_funding_rate(self, market):
        if market.market_type != "perpetual":
            return Funding(self.name, market.symbol, 0.0)
        raw = self.exchange.fetch_funding_rate(market.symbol)
        info = raw.get("info") or {}
        rate = raw.get("fundingRate", info.get("fundingRate", 0))
        ts = raw.get("fundingTimestamp", info.get("nextFundingTime"))
        try:
            rate = float(rate or 0)
        except Exception:
            rate = 0.0
        try:
            ts = int(ts) if ts is not None else None
        except Exception:
            ts = None
        return Funding(self.name, market.symbol, rate, ts)

    def get_trading_fee(self, market):
        try:
            raw = self.exchange.fetch_trading_fee(market.symbol)
            if raw.get("taker") is not None:
                return TradingFee(self.name, market.symbol, market.market_type, float(raw["taker"]))
        except Exception:
            pass
        taker = 0.001 if market.market_type == "spot" else 0.00055
        return TradingFee(self.name, market.symbol, market.market_type, taker)
