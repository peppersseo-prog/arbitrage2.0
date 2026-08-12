# Arbitrage Client 0.7
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import ccxt
import keyring
from PySide6.QtCore import QTimer, QThread, QObject, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QGroupBox, QDialog, QFormLayout, QLineEdit,
    QMessageBox, QTableWidget, QTableWidgetItem, QDoubleSpinBox,
    QSpinBox, QHeaderView, QCheckBox
)

SERVICE = "ArbitrageClient"
EXCHANGES = ("bybit", "okx", "bitget")


def save_credentials(ex, key, secret, password=""):
    keyring.set_password(SERVICE, ex, json.dumps({
        "api_key": key, "api_secret": secret, "password": password
    }))


def load_credentials(ex):
    x = keyring.get_password(SERVICE, ex)
    return json.loads(x) if x else None


def delete_credentials(ex):
    try:
        keyring.delete_password(SERVICE, ex)
    except Exception:
        pass


def n(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0


def make(ex):
    c = load_credentials(ex)
    if not c:
        raise RuntimeError(f"{ex.upper()}: API credentials not found")

    cfg = {
        "apiKey": c["api_key"],
        "secret": c["api_secret"],
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    }

    if ex in ("okx", "bitget"):
        cfg["password"] = c.get("password", "")

    if ex == "okx":
        return ccxt.okx(cfg)
    if ex == "bitget":
        return ccxt.bitget(cfg)
    return ccxt.bybit(cfg)


def market_identity(market):
    """Use contract/network first, then baseId. Never ticker alone."""
    if not isinstance(market, dict):
        return None

    info = market.get("info") or {}

    address = (
        market.get("contractAddress") or
        market.get("contract") or
        market.get("tokenAddress") or
        info.get("contractAddress") or
        info.get("contract_address") or
        info.get("tokenAddress") or
        info.get("token_address") or
        info.get("address") or ""
    )

    network = (
        market.get("network") or
        market.get("chain") or
        info.get("network") or
        info.get("chain") or
        info.get("networkName") or
        info.get("chainName") or ""
    )

    address = str(address).strip().lower()
    network = str(network).strip().lower()

    if address:
        return ("contract", network, address)

    base_id = market.get("baseId")
    if base_id:
        return ("baseid", str(base_id).strip().lower())

    return None


def tickers(ex):
    exchange = make(ex)
    exchange.load_markets()

    symbols = []
    for symbol, market in exchange.markets.items():
        if not isinstance(symbol, str):
            continue
        if market.get("quote") != "USDT":
            continue
        if market.get("active") is False:
            continue
        if not (market.get("spot") is True or market.get("type") == "spot"):
            continue
        symbols.append(symbol)

    if ex == "bybit":
        data = exchange.fetch_tickers(symbols, params={"category": "spot"})
    else:
        data = exchange.fetch_tickers(symbols)

    out = {}
    for symbol, ticker in data.items():
        bid = n(ticker.get("bid"))
        ask = n(ticker.get("ask"))
        if bid > 0 and ask > 0:
            out[symbol] = {
                "bid": bid,
                "ask": ask,
                "market": exchange.markets[symbol],
            }

    return out


def book(ex, symbol):
    exchange = make(ex)
    exchange.load_markets()

    if ex == "bybit":
        return exchange.fetch_order_book(
            symbol, limit=50, params={"category": "spot"}
        )
    return exchange.fetch_order_book(symbol, limit=50)


def pair_candidates(markets, fees, minnet, enabled):
    """Find pairs by verified asset identity, not ticker equality."""
    identities = {}

    for ex in enabled:
        identities[ex] = {}
        for symbol, data in markets.get(ex, {}).items():
            identity = market_identity(data.get("market"))
            if identity is not None:
                identities[ex].setdefault(identity, []).append(symbol)

    result = []

    for buy_ex in enabled:
        for sell_ex in enabled:
            if buy_ex == sell_ex:
                continue

            common = (
                set(identities.get(buy_ex, {}))
                & set(identities.get(sell_ex, {}))
            )

            for identity in common:
                for buy_symbol in identities[buy_ex][identity]:
                    for sell_symbol in identities[sell_ex][identity]:
                        buy = markets[buy_ex][buy_symbol]
                        sell = markets[sell_ex][sell_symbol]

                        bp = buy["ask"]
                        sp = sell["bid"]
                        if bp <= 0 or sp <= 0:
                            continue

                        bf = fees[buy_ex]
                        sf = fees[sell_ex]
                        net = (
                            sp * (1 - sf) /
                            (bp * (1 + bf)) - 1
                        ) * 100

                        if net >= minnet:
                            result.append({
                                "net": net,
                                "buy_symbol": buy_symbol,
                                "sell_symbol": sell_symbol,
                                "buy": buy_ex,
                                "sell": sell_ex,
                                "identity": identity,
                            })

    result.sort(key=lambda x: x["net"], reverse=True)
    return result


def depth(buybook, sellbook, bf, sf, capital, minnet):
    def levels(raw):
        result = []
        for level in raw or []:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            p, q = n(level[0]), n(level[1])
            if p > 0 and q > 0:
                result.append((p, q))
        return result

    asks = sorted(levels(buybook.get("asks", [])))
    bids = sorted(levels(sellbook.get("bids", [])), reverse=True)

    if not asks or not bids:
        return None

    ai = bi = 0
    ar, br = asks[0][1], bids[0][1]
    qty = cost = sell = 0.0

    while ai < len(asks) and bi < len(bids):
        ap, bp = asks[ai][0], bids[bi][0]
        if bp <= ap:
            break

        cap = max(0.0, (capital - cost) / (ap * (1 + bf)))
        q = min(ar, br, cap)
        if q <= 0:
            break

        qty += q
        cost += q * ap
        sell += q * bp
        ar -= q
        br -= q

        if ar <= 1e-14:
            ai += 1
            if ai < len(asks):
                ar = asks[ai][1]

        if br <= 1e-14:
            bi += 1
            if bi < len(bids):
                br = bids[bi][1]

    if qty <= 0 or cost <= 0:
        return None

    profit = sell * (1 - sf) - cost * (1 + bf)
    net = profit / cost * 100
    if net < minnet:
        return None

    return {
        "qty": qty,
        "cost": cost,
        "sell": sell,
        "avg_buy": cost / qty,
        "avg_sell": sell / qty,
        "top_buy": asks[0][0],
        "top_sell": bids[0][0],
        "top_gross": (bids[0][0] / asks[0][0] - 1) * 100,
        "net": net,
        "profit": profit,
        "fees": cost * bf + sell * sf,
    }


class Worker(QObject):
    done = Signal(object, object)
    err = Signal(str)

    def __init__(self, fees, capital, minnet, limit, enabled):
        super().__init__()
        self.fees = fees
        self.capital = capital
        self.minnet = minnet
        self.limit = limit
        self.enabled = list(enabled)
        self.stop_requested = False

    def stop(self):
        self.stop_requested = True

    def _status(self, markets, checked):
        sets = [
            set(markets.get(ex, {}))
            for ex in self.enabled if markets.get(ex)
        ]
        common = len(set.intersection(*sets)) if len(sets) >= 2 else 0
        return {
            "bybit": len(markets.get("bybit", {})),
            "okx": len(markets.get("okx", {})),
            "bitget": len(markets.get("bitget", {})),
            "common": common,
            "checked": checked,
        }

    def run(self):
        markets = {}
        try:
            if len(self.enabled) < 2:
                self.done.emit([], self._status(markets, 0))
                return

            with ThreadPoolExecutor(max_workers=len(self.enabled)) as pool:
                futures = {pool.submit(tickers, ex): ex for ex in self.enabled}

                for future in as_completed(futures):
                    if self.stop_requested:
                        for f in futures:
                            f.cancel()
                        self.done.emit([], self._status(markets, 0))
                        return
                    markets[futures[future]] = future.result()

            for ex in EXCHANGES:
                markets.setdefault(ex, {})

            candidates = pair_candidates(
                markets, self.fees, self.minnet, self.enabled
            )[:self.limit]

            books = {}

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {}
                for c in candidates:
                    key = (
                        c["buy_symbol"], c["sell_symbol"],
                        c["buy"], c["sell"]
                    )
                    futures[pool.submit(
                        book, c["buy"], c["buy_symbol"]
                    )] = (key, "buy")
                    futures[pool.submit(
                        book, c["sell"], c["sell_symbol"]
                    )] = (key, "sell")

                for future in as_completed(futures):
                    if self.stop_requested:
                        for f in futures:
                            f.cancel()
                        self.done.emit(
                            [], self._status(markets, len(candidates))
                        )
                        return
                    key, side = futures[future]
                    try:
                        books.setdefault(key, {})[side] = future.result()
                    except Exception:
                        pass

            rows = []
            for c in candidates:
                if self.stop_requested:
                    self.done.emit(
                        [], self._status(markets, len(candidates))
                    )
                    return

                key = (
                    c["buy_symbol"], c["sell_symbol"],
                    c["buy"], c["sell"]
                )
                pair = books.get(key, {})
                if "buy" not in pair or "sell" not in pair:
                    continue

                r = depth(
                    pair["buy"], pair["sell"],
                    self.fees[c["buy"]], self.fees[c["sell"]],
                    self.capital, self.minnet
                )
                if r:
                    r.update(
                        symbol=c["buy_symbol"],
                        sell_symbol=c["sell_symbol"],
                        buy=c["buy"],
                        sell=c["sell"]
                    )
                    rows.append(r)

            rows.sort(key=lambda x: x["profit"], reverse=True)
            self.done.emit(rows, self._status(markets, len(candidates)))

        except Exception as e:
            if not self.stop_requested:
                self.err.emit(f"{type(e).__name__}: {e}")


class Api(QDialog):
    def __init__(self, ex, parent=None):
        super().__init__(parent)
        self.ex = ex
        self.setWindowTitle("Connect " + ex.upper())

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.k = QLineEdit()
        self.s = QLineEdit()
        self.s.setEchoMode(QLineEdit.Password)
        self.p = QLineEdit()
        self.p.setEchoMode(QLineEdit.Password)

        form.addRow("API Key:", self.k)
        form.addRow("API Secret:", self.s)
        if ex in ("okx", "bitget"):
            form.addRow("Passphrase:", self.p)

        layout.addLayout(form)
        button = QPushButton("Save")
        button.clicked.connect(self.save)
        layout.addWidget(button)

    def save(self):
        if not self.k.text() or not self.s.text():
            QMessageBox.warning(self, "Error", "API Key и Secret обязательны.")
            return
        if self.ex in ("okx", "bitget") and not self.p.text():
            QMessageBox.warning(self, "Error", "Passphrase обязательна.")
            return

        save_credentials(
            self.ex, self.k.text(), self.s.text(), self.p.text()
        )
        self.accept()


class ExchangeWidget(QWidget):
    def __init__(self, name, app):
        super().__init__()
        self.name = name
        self.app = app

        layout = QHBoxLayout(self)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(True)
        layout.addWidget(self.enabled)
        layout.addWidget(QLabel(name.upper()))

        self.state = QLabel()
        layout.addWidget(self.state)
        layout.addStretch()

        connect = QPushButton("Connect / Test")
        connect.clicked.connect(self.connect)
        layout.addWidget(connect)

        delete = QPushButton("Delete")
        delete.clicked.connect(self.delete)
        layout.addWidget(delete)

        self.refresh()

    def is_enabled(self):
        return self.enabled.isChecked()

    def refresh(self):
        self.state.setText(
            "● Connected" if load_credentials(self.name)
            else "○ Not connected"
        )

    def connect(self):
        if not load_credentials(self.name):
            if Api(self.name, self.app).exec():
                self.refresh()
            return
        try:
            make(self.name).fetch_balance()
            self.state.setText("● Connected")
            self.app.balances()
        except Exception as e:
            QMessageBox.critical(self.app, "Connection error", str(e))

    def delete(self):
        delete_credentials(self.name)
        self.refresh()
        self.app.balances()


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Arbitrage Client 0.7")
        self.resize(1450, 800)

        self.thread = None
        self.worker = None
        self.running = False

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        title = QLabel("ARBITRAGE CLIENT 0.7")
        title.setStyleSheet("font-size:26px;font-weight:bold;")
        layout.addWidget(title)
        layout.addWidget(QLabel(
            "Verified asset-identity spot arbitrage scanner: "
            "BYBIT ↔ OKX ↔ BITGET."
        ))

        group = QGroupBox("Exchanges")
        group_layout = QVBoxLayout(group)
        self.exchange_widgets = {}
        for ex in EXCHANGES:
            w = ExchangeWidget(ex, self)
            self.exchange_widgets[ex] = w
            group_layout.addWidget(w)
        layout.addWidget(group)

        settings_group = QGroupBox("Scanner settings")
        settings = QHBoxLayout(settings_group)

        self.cap = QDoubleSpinBox()
        self.cap.setRange(1, 1e7)
        self.cap.setValue(1000)
        self.cap.setDecimals(2)

        self.bf = QDoubleSpinBox()
        self.bf.setRange(0, 5)
        self.bf.setValue(.1)
        self.bf.setDecimals(4)

        self.of = QDoubleSpinBox()
        self.of.setRange(0, 5)
        self.of.setValue(.1)
        self.of.setDecimals(4)

        self.gf = QDoubleSpinBox()
        self.gf.setRange(0, 5)
        self.gf.setValue(.1)
        self.gf.setDecimals(4)

        self.mn = QDoubleSpinBox()
        self.mn.setRange(-100, 100)
        self.mn.setValue(0)
        self.mn.setDecimals(4)

        self.sec = QSpinBox()
        self.sec.setRange(3, 300)
        self.sec.setValue(10)

        self.lim = QSpinBox()
        self.lim.setRange(5, 300)
        self.lim.setValue(50)

        for label, widget in [
            ("Capital USDT:", self.cap),
            ("Bybit taker %:", self.bf),
            ("OKX taker %:", self.of),
            ("Bitget taker %:", self.gf),
            ("Min net %:", self.mn),
            ("Refresh sec:", self.sec),
            ("Order books:", self.lim),
        ]:
            settings.addWidget(QLabel(label))
            settings.addWidget(widget)

        layout.addWidget(settings_group)

        controls = QHBoxLayout()
        self.start = QPushButton("▶ Start scanner")
        self.start.clicked.connect(self.toggle)
        controls.addWidget(self.start)

        refresh = QPushButton("Refresh now")
        refresh.clicked.connect(self.scan)
        controls.addWidget(refresh)

        balances = QPushButton("Refresh balances")
        balances.clicked.connect(self.balances)
        controls.addWidget(balances)

        controls.addStretch()
        self.status = QLabel("Ready")
        controls.addWidget(self.status)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 13)
        self.table.setHorizontalHeaderLabels([
            "Coin", "BUY", "SELL", "Top buy", "Top sell",
            "Avg buy", "Avg sell", "Top gross %", "Real net %",
            "Qty", "Notional", "Profit", "Fees"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)

        balances_group = QGroupBox("USDT Spot Balance")
        balances_layout = QHBoxLayout(balances_group)
        self.balance_labels = {}
        for ex in EXCHANGES:
            label = QLabel(ex.upper() + ": —")
            self.balance_labels[ex] = label
            balances_layout.addWidget(label)
        balances_layout.addStretch()
        layout.addWidget(balances_group)

        self.timer = QTimer()
        self.timer.timeout.connect(self.scan)

    def balances(self):
        for ex in EXCHANGES:
            try:
                balance = make(ex).fetch_balance()
                free = n(balance.get("USDT", {}).get("free"))
                self.balance_labels[ex].setText(
                    f"{ex.upper()}: {free:,.2f} USDT"
                )
            except Exception:
                self.balance_labels[ex].setText(f"{ex.upper()}: error")

    def toggle(self):
        if self.running:
            self.running = False
            self.timer.stop()
            if self.worker:
                self.worker.stop()
            self.start.setEnabled(True)
            self.start.setText("▶ Start scanner")
            self.status.setText("Stopping…")
            return

        self.running = True
        self.start.setEnabled(True)
        self.start.setText("■ Stop scanner")
        self.scan()
        if self.running:
            self.timer.start(self.sec.value() * 1000)

    def scan(self):
        if self.thread and self.thread.isRunning():
            return

        enabled = [
            ex for ex in EXCHANGES
            if self.exchange_widgets[ex].is_enabled()
        ]

        if len(enabled) < 2:
            self.running = False
            self.timer.stop()
            self.start.setText("▶ Start scanner")
            self.status.setText("Enable at least 2 exchanges")
            return

        missing = [
            ex.upper() for ex in enabled if not load_credentials(ex)
        ]
        if missing:
            self.running = False
            self.timer.stop()
            self.start.setText("▶ Start scanner")
            self.status.setText("Connect: " + ", ".join(missing))
            return

        fees = {
            "bybit": self.bf.value() / 100,
            "okx": self.of.value() / 100,
            "bitget": self.gf.value() / 100,
        }

        self.status.setText(
            "Loading " + " + ".join(x.upper() for x in enabled)
            + " market data…"
        )

        self.thread = QThread()
        self.worker = Worker(
            fees, self.cap.value(), self.mn.value(),
            self.lim.value(), enabled
        )
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.done.connect(self.result)
        self.worker.err.connect(self.error)
        self.worker.done.connect(self.thread.quit)
        self.worker.err.connect(self.thread.quit)
        self.thread.finished.connect(self.cleanup)
        self.thread.start()

    def cleanup(self):
        self.worker = None
        self.thread = None

    def result(self, rows, status):
        if not self.running:
            self.status.setText("Stopped")
            return

        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            values = [
                r["symbol"], r["buy"].upper(), r["sell"].upper(),
                f'{r["top_buy"]:,.8g}', f'{r["top_sell"]:,.8g}',
                f'{r["avg_buy"]:,.8g}', f'{r["avg_sell"]:,.8g}',
                f'{r["top_gross"]:.4f}%', f'{r["net"]:.4f}%',
                f'{r["qty"]:,.8g}', f'${r["cost"]:,.2f}',
                f'${r["profit"]:,.4f}', f'${r["fees"]:,.4f}',
            ]
            for j, value in enumerate(values):
                self.table.setItem(i, j, QTableWidgetItem(value))

        self.status.setText(
            f'OK: Bybit {status["bybit"]} | '
            f'OKX {status["okx"]} | '
            f'Bitget {status["bitget"]} | '
            f'common enabled {status["common"]} | '
            f'depth checked {status["checked"]} | '
            f'opportunities {len(rows)}'
        )

    def error(self, message):
        if not self.running:
            self.status.setText("Stopped")
            return
        self.status.setText("Scanner error")
        QMessageBox.critical(
            self, "Scanner error",
            "Не удалось получить данные.\n\n" + message
        )

    def closeEvent(self, event):
        self.timer.stop()
        if self.worker:
            self.worker.stop()
        event.accept()


app = QApplication(sys.argv)
window = App()
window.show()
sys.exit(app.exec())
