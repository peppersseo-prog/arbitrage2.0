from adapters.bybit import BybitAdapter

API_ORDERBOOK_DEPTH = 5
CALCULATION_DEPTHS = (1, 2, 3)

def match_spot_perpetual(spot_markets, perpetual_markets):
    spot = {(m.base.upper(), m.quote.upper()): m for m in spot_markets if m.active}
    perp = {(m.base.upper(), m.quote.upper()): m for m in perpetual_markets if m.active and m.linear}
    return [(spot[k], perp[k]) for k in sorted(set(spot) & set(perp))]

def vwap(levels, depth):
    levels = levels[:depth]
    qty = sum(x.amount for x in levels)
    if qty <= 0:
        return None, 0.0
    return sum(x.price * x.amount for x in levels) / qty, qty

def calculate_spread(spot_book, perp_book, depth):
    if depth not in CALCULATION_DEPTHS:
        raise ValueError("Calculation depth must be 1, 2 or 3")
    spot_price, spot_qty = vwap(spot_book.asks, depth)
    perp_price, perp_qty = vwap(perp_book.bids, depth)
    if spot_price is None or perp_price is None:
        return None
    return {
        "spot_vwap": spot_price,
        "perp_vwap": perp_price,
        "hedge_qty": min(spot_qty, perp_qty),
        "gross_spread_pct": (perp_price / spot_price - 1) * 100,
        "depth": depth,
    }

if __name__ == "__main__":
    print("Arbitrage Scanner 1.0")
    print("Architecture: adapters for Bybit / OKX / Bitget")
    print("Bybit: implemented")
    print("API order-book depth: 5")
    print("Calculation depth: 1 / 2 / 3")
