# Arbitrage Client 0.3

Read-only spot arbitrage scanner for Bybit and OKX.

Features:
- local Windows keyring for API credentials
- Bybit + OKX spot tickers via CCXT
- common USDT markets
- both arbitrage directions
- gross spread
- net spread after configurable taker fees
- capital/notional/profit estimate
- minimum net-spread filter
- automatic refresh
- NO order placement, borrowing or withdrawals

Default taker fee is 0.10% per side. Replace it with your actual account fee.

For 0.3, API keys need read permission only. Do not enable Withdraw.
