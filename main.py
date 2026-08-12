# Arbitrage Client 0.5
# Read-only spot arbitrage scanner: Bybit <-> OKX <-> Bitget.
# Uses CCXT and real order-book depth. No orders are placed.

import sys, json
from concurrent.futures import ThreadPoolExecutor, as_completed

import ccxt
import keyring

from PySide6.QtCore import QTimer, QThread, QObject, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QGroupBox, QDialog, QFormLayout, QLineEdit,
    QMessageBox, QTableWidget, QTableWidgetItem, QDoubleSpinBox,
    QSpinBox, QHeaderView
)

SERVICE = "ArbitrageClient"
EXCHANGES = ("bybit", "okx", "bitget")


def save_credentials(ex, key, secret, password=""):
    keyring.set_password(
        SERVICE, ex,
        json.dumps({"api_key": key, "api_secret": secret, "password": password})
    )


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
    }

    if ex == "okx":
        cfg["password"] = c.get("password", "")
        cfg["options"] = {"defaultType": "spot"}
        return ccxt.okx(cfg)

    if ex == "bitget":
        # Bitget calls the API passphrase "password" in CCXT.
        cfg["password"] = c.get("password", "")
        cfg["options"] = {"defaultType": "spot"}
        return ccxt.bitget(cfg)

    cfg["options"] = {"defaultType": "spot"}
    return ccxt.bybit(cfg)


def tickers(ex):
    x = make(ex)
    x.load_markets()

    if ex == "bybit":
        d = x.fetch_tickers(params={"category": "spot"})
    else:
        d = x.fetch_tickers()

    out = {}
    items = d.items() if hasattr(d, "items") else []

    for s, t in items:
        if not isinstance(s, str) or not s.endswith("/USDT") or s not in x.markets:
            continue

        m = x.markets[s]
        if m.get("active") is False or m.get("spot") is not True:
            continue

        bid = n(t.get("bid")) if isinstance(t, dict) else 0
        ask = n(t.get("ask")) if isinstance(t, dict) else 0

        if bid > 0 and ask > 0:
            out[s] = {"bid": bid, "ask": ask}

    return out


def book(ex, symbol):
    x = make(ex)
    x.load_markets()

    if ex == "bybit":
        return x.fetch_order_book(symbol, limit=50, params={"category": "spot"})

    return x.fetch_order_book(symbol, limit=50)


def pair_candidates(markets, fees, minnet):
    """
    Return all directed arbitrage pairs:
    buy on one exchange, sell on another.
    """
    z = []

    for buy_ex in EXCHANGES:
        for sell_ex in EXCHANGES:
            if buy_ex == sell_ex:
                continue

            common = set(markets[buy_ex]) & set(markets[sell_ex])

            for s in common:
                a = markets[buy_ex][s]
                b = markets[sell_ex][s]

                bp = a["ask"]
                sp = b["bid"]

                if bp <= 0 or sp <= 0:
                    continue

                buyfee = fees[buy_ex]
                sellfee = fees[sell_ex]

                gross = (sp / bp - 1) * 100
                net = (
                    sp * (1 - sellfee) /
                    (bp * (1 + buyfee)) - 1
                ) * 100

                if net >= minnet:
                    z.append((net, s, buy_ex, sell_ex))

    z.sort(reverse=True)
    return z


def depth(buybook, sellbook, bf, sf, capital, minnet):
    def levels(raw):
        out = []
        for level in (raw or []):
            # Some exchanges may return extra fields per level.
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            p = n(level[0])
            q = n(level[1])
            if p > 0 and q > 0:
                out.append((p, q))
        return out

    asks = sorted(levels(buybook.get("asks", [])))
    bids = sorted(levels(sellbook.get("bids", [])), reverse=True)

    if not asks or not bids:
        return None

    ai = bi = 0
    ar = asks[0][1]
    br = bids[0][1]

    qty = cost = sell = 0.0

    while ai < len(asks) and bi < len(bids):
        ap, bp = asks[ai][0], bids[bi][0]

        # If the best executable sell is not above the buy price,
        # there is no profitable overlap at this depth level.
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

    if qty <= 0:
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

    def __init__(self, fees, capital, minnet, limit):
        super().__init__()
        self.fees = fees
        self.capital = capital
        self.minnet = minnet
        self.limit = limit

    def run(self):
        try:
            markets = {}

            # Fetch all three exchanges in parallel.
            with ThreadPoolExecutor(3) as p:
                fs = {p.submit(tickers, ex): ex for ex in EXCHANGES}

                for f in as_completed(fs):
                    ex = fs[f]
                    markets[ex] = f.result()

            for ex in EXCHANGES:
                markets.setdefault(ex, {})

            cs = pair_candidates(markets, self.fees, self.minnet)

            # Limit expensive order-book requests.
            cs = cs[:self.limit]

            books = {}

            # Two order-book requests per opportunity.
            with ThreadPoolExecutor(10) as p:
                fs = {}

                for _, symbol, buy, sell in cs:
                    key = (symbol, buy, sell)

                    fs[p.submit(book, buy, symbol)] = (key, "buy")
                    fs[p.submit(book, sell, symbol)] = (key, "sell")

                for f in as_completed(fs):
                    key, side = fs[f]
                    try:
                        books.setdefault(key, {})[side] = f.result()
                    except Exception:
                        # A single bad/unsupported market must not kill the scan.
                        pass

            rows = []

            for _, symbol, buy, sell in cs:
                key = (symbol, buy, sell)
                bb = books.get(key, {})

                if "buy" not in bb or "sell" not in bb:
                    continue

                r = depth(
                    bb["buy"],
                    bb["sell"],
                    self.fees[buy],
                    self.fees[sell],
                    self.capital,
                    self.minnet
                )

                if r:
                    r.update(symbol=symbol, buy=buy, sell=sell)
                    rows.append(r)

            rows.sort(key=lambda x: x["profit"], reverse=True)

            status = {
                "bybit": len(markets["bybit"]),
                "okx": len(markets["okx"]),
                "bitget": len(markets["bitget"]),
                "common": (
                    len(set(markets["bybit"]) &
                        set(markets["okx"]) &
                        set(markets["bitget"]))
                ),
                "checked": len(cs),
            }

            self.done.emit(rows, status)

        except Exception as e:
            self.err.emit(f"{type(e).__name__}: {e}")


class Api(QDialog):
    def __init__(self, ex, parent=None):
        super().__init__(parent)
        self.ex = ex
        self.setWindowTitle("Connect " + ex.upper())

        v = QVBoxLayout(self)
        f = QFormLayout()

        self.k = QLineEdit()
        self.s = QLineEdit()
        self.s.setEchoMode(QLineEdit.Password)

        self.p = QLineEdit()
        self.p.setEchoMode(QLineEdit.Password)

        f.addRow("API Key:", self.k)
        f.addRow("API Secret:", self.s)

        if ex in ("okx", "bitget"):
            f.addRow("Passphrase:", self.p)

        v.addLayout(f)

        b = QPushButton("Save")
        b.clicked.connect(self.save)
        v.addWidget(b)

    def save(self):
        if not self.k.text() or not self.s.text():
            QMessageBox.warning(
                self,
                "Error",
                "API Key и Secret обязательны."
            )
            return

        if self.ex in ("okx", "bitget") and not self.p.text():
            QMessageBox.warning(
                self,
                "Error",
                "Passphrase обязательна для " + self.ex.upper() + "."
            )
            return

        save_credentials(
            self.ex,
            self.k.text(),
            self.s.text(),
            self.p.text()
        )

        self.accept()


class Ex(QWidget):
    def __init__(self, name, app):
        super().__init__()
        self.name = name
        self.app = app

        l = QHBoxLayout(self)
        l.addWidget(QLabel(name.upper()))

        self.st = QLabel()
        l.addWidget(self.st)
        l.addStretch()

        b = QPushButton("Connect / Test")
        b.clicked.connect(self.connect)
        l.addWidget(b)

        b = QPushButton("Delete")
        b.clicked.connect(self.delete)
        l.addWidget(b)

        self.refresh()

    def refresh(self):
        self.st.setText(
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
            self.st.setText("● Connected")
            self.app.balances()
        except Exception as e:
            QMessageBox.critical(
                self.app,
                "Connection error",
                str(e)
            )

    def delete(self):
        delete_credentials(self.name)
        self.refresh()
        self.app.balances()


class App(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Arbitrage Client 0.5")
        self.resize(1450, 800)

        self.th = None
        self.w = None
        self.running = False

        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)

        h = QLabel("ARBITRAGE CLIENT 0.5")
        h.setStyleSheet("font-size:26px;font-weight:bold;")
        v.addWidget(h)

        v.addWidget(QLabel(
            "Read-only spot arbitrage scanner: "
            "BYBIT ↔ OKX ↔ BITGET with real order-book depth."
        ))

        g = QGroupBox("Exchanges")
        gl = QVBoxLayout(g)

        self.ex_widgets = {}
        for ex in EXCHANGES:
            w = Ex(ex, self)
            self.ex_widgets[ex] = w
            gl.addWidget(w)

        v.addWidget(g)

        g = QGroupBox("Scanner settings")
        sl = QHBoxLayout(g)

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

        settings = [
            ("Capital USDT:", self.cap),
            ("Bybit taker %:", self.bf),
            ("OKX taker %:", self.of),
            ("Bitget taker %:", self.gf),
            ("Min net %:", self.mn),
            ("Refresh sec:", self.sec),
            ("Order books:", self.lim),
        ]

        for t, x in settings:
            sl.addWidget(QLabel(t))
            sl.addWidget(x)

        v.addWidget(g)

        c = QHBoxLayout()

        self.start = QPushButton("▶ Start scanner")
        self.start.clicked.connect(self.toggle)
        c.addWidget(self.start)

        for t, f in [
            ("Refresh now", self.scan),
            ("Refresh balances", self.balances)
        ]:
            b = QPushButton(t)
            b.clicked.connect(f)
            c.addWidget(b)

        c.addStretch()

        self.status = QLabel("Ready")
        c.addWidget(self.status)
        v.addLayout(c)

        self.tab = QTableWidget(0, 13)

        self.tab.setHorizontalHeaderLabels([
            "Coin", "BUY", "SELL",
            "Top buy", "Top sell",
            "Avg buy", "Avg sell",
            "Top gross %", "Real net %",
            "Qty", "Notional", "Profit", "Fees"
        ])

        self.tab.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )

        v.addWidget(self.tab)

        g = QGroupBox("USDT Spot Balance")
        bl = QHBoxLayout(g)

        self.balance_labels = {}

        for ex in EXCHANGES:
            label = QLabel(ex.upper() + ": —")
            self.balance_labels[ex] = label
            bl.addWidget(label)

        bl.addStretch()
        v.addWidget(g)

        self.timer = QTimer()
        self.timer.timeout.connect(self.scan)

    def balances(self):
        for ex in EXCHANGES:
            label = self.balance_labels[ex]

            try:
                bal = make(ex).fetch_balance()
                free = n(bal.get("USDT", {}).get("free"))
                label.setText(
                    f"{ex.upper()}: {free:,.2f} USDT"
                )
            except Exception:
                label.setText(f"{ex.upper()}: error")

    def toggle(self):
        self.running = not self.running

        if self.running:
            self.start.setText("■ Stop scanner")
            self.scan()
            self.timer.start(self.sec.value() * 1000)
        else:
            self.start.setText("▶ Start scanner")
            self.timer.stop()
            self.status.setText("Stopped")

    def scan(self):
        if self.th and self.th.isRunning():
            return

        self.status.setText(
            "Loading Bybit + OKX + Bitget market data…"
        )

        self.start.setEnabled(False)

        fees = {
            "bybit": self.bf.value() / 100,
            "okx": self.of.value() / 100,
            "bitget": self.gf.value() / 100,
        }

        self.th = QThread()

        self.w = Worker(
            fees,
            self.cap.value(),
            self.mn.value(),
            self.lim.value()
        )

        self.w.moveToThread(self.th)

        self.th.started.connect(self.w.run)
        self.w.done.connect(self.result)
        self.w.err.connect(self.error)

        self.w.done.connect(self.th.quit)
        self.w.err.connect(self.th.quit)

        self.th.finished.connect(
            lambda: self.start.setEnabled(True)
        )
        self.th.finished.connect(self.cleanup)

        self.th.start()

    def cleanup(self):
        self.w = None
        self.th = None

    def result(self, rows, st):
        self.tab.setRowCount(len(rows))

        for i, r in enumerate(rows):
            vals = [
                r["symbol"],
                r["buy"].upper(),
                r["sell"].upper(),
                f'{r["top_buy"]:,.8g}',
                f'{r["top_sell"]:,.8g}',
                f'{r["avg_buy"]:,.8g}',
                f'{r["avg_sell"]:,.8g}',
                f'{r["top_gross"]:.4f}%',
                f'{r["net"]:.4f}%',
                f'{r["qty"]:,.8g}',
                f'${r["cost"]:,.2f}',
                f'${r["profit"]:,.4f}',
                f'${r["fees"]:,.4f}',
            ]

            for j, x in enumerate(vals):
                self.tab.setItem(i, j, QTableWidgetItem(x))

        self.status.setText(
            f'OK: Bybit {st["bybit"]} | '
            f'OKX {st["okx"]} | '
            f'Bitget {st["bitget"]} | '
            f'common all 3 {st["common"]} | '
            f'depth checked {st["checked"]} | '
            f'opportunities {len(rows)}'
        )

    def error(self, msg):
        self.status.setText("Scanner error")
        QMessageBox.critical(
            self,
            "Scanner error",
            "Не удалось получить данные.\n\n" + msg
        )

    def closeEvent(self, e):
        self.timer.stop()
        e.accept()


app = QApplication(sys.argv)
win = App()
win.show()
sys.exit(app.exec())
