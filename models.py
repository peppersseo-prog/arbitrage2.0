from dataclasses import dataclass, field
from typing import Optional, List

@dataclass
class Market:
    exchange: str
    symbol: str
    base: str
    quote: str
    market_type: str
    active: bool = True
    contract_size: float = 1.0
    settle: Optional[str] = None
    linear: Optional[bool] = None

@dataclass
class OrderBookLevel:
    price: float
    amount: float

@dataclass
class OrderBook:
    exchange: str
    symbol: str
    market_type: str
    asks: List[OrderBookLevel] = field(default_factory=list)
    bids: List[OrderBookLevel] = field(default_factory=list)

@dataclass
class Funding:
    exchange: str
    symbol: str
    rate: float
    next_funding_time: Optional[int] = None

@dataclass
class TradingFee:
    exchange: str
    symbol: str
    market_type: str
    taker: float
