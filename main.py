import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor

import ccxt
import keyring
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QGroupBox, QDialog, QFormLayout, QLineEdit,
    QMessageBox, QTableWidget, QTableWidgetItem, QDoubleSpinBox,
    QSpinBox, QHeaderView
)

SERVICE = "ArbitrageClient"

def save_credentials(exchange, api_key, api_secret, password=""):
    keyring.set_password(SERVICE, exchange, json.dumps({
        "api_key": api_key, "api_secret": api_secret, "password": password
    }))

def load_credentials(exchange):
    value = keyring.get_password(SERVICE, exchange)
    return json.loads(value) if value else None

def delete_credentials(exchange):
    try:
        keyring.delete_password(SERVICE, exchange)
    except Exception:
        pass

def create_exchange(name):
    c = load_credentials(name)
    if not c:
        return None
    cfg = {"apiKey": c["api_key"], "secret": c["api_secret"],
           "enableRateLimit": True, "timeout": 10000}
    if name == "bybit":
        return ccxt.bybit(cfg)
    if name == "okx":
        cfg["password"] = c["password"]
        return ccxt.okx(cfg)
    return None

def flt(x, default=0.0):
    try:
        return default if x is None else float(x)
    except Exception:
        return default

def load_tickers(name):
    ex = create_exchange(name)
    if not ex:
        raise RuntimeError(f"{name.upper()}: API credentials not found")
    ex.load_markets()
    tickers = ex.fetch_tickers()
    out = {}
    for symbol, t in tickers.items():
        if not symbol.endswith("/USDT") or symbol not in ex.markets:
            continue
        m = ex.markets[symbol]
        if m.get("active") is False or m.get("spot") is False:
            continue
        bid, ask = flt(t.get("bid")), flt(t.get("ask"))
        if bid <= 0 or ask <= 0:
            continue
        out[symbol] = {
            "bid": bid, "ask": ask,
            "bid_volume": flt(t.get("bidVolume")),
            "ask_volume": flt(t.get("askVolume"))
        }
    return out

def opportunities(a, b, fee_a, fee_b, capital):
    rows = []
    for symbol in set(a) & set(b):
        for buy_name, buy, buy_fee, sell_name, sell, sell_fee in [
            ("BYBIT", a[symbol], fee_a, "OKX", b[symbol], fee_b),
            ("OKX", b[symbol], fee_b, "BYBIT", a[symbol], fee_a)
        ]:
            buy_price, sell_price = buy["ask"], sell["bid"]
            gross = (sell_price / buy_price - 1) * 100
            net = (sell_price * (1-sell_fee) /
                   (buy_price * (1+buy_fee)) - 1) * 100
            qty = capital / buy_price
            if buy["ask_volume"] > 0:
                qty = min(qty, buy["ask_volume"])
            if sell["bid_volume"] > 0:
                qty = min(qty, sell["bid_volume"])
            notional = qty * buy_price
            rows.append({
                "symbol": symbol, "buy": buy_name, "sell": sell_name,
                "buy_price": buy_price, "sell_price": sell_price,
                "gross": gross, "net": net, "qty": qty,
                "notional": notional, "profit": notional * net / 100
            })
    return sorted(rows, key=lambda x: x["net"], reverse=True)

class ApiDialog(QDialog):
    def __init__(self, exchange, parent=None):
        super().__init__(parent)
        self.exchange = exchange
        self.setWindowTitle(f"Connect {exchange.upper()}")
        self.setMinimumWidth(450)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.key = QLineEdit()
        self.secret = QLineEdit(); self.secret.setEchoMode(QLineEdit.Password)
        self.passphrase = QLineEdit(); self.passphrase.setEchoMode(QLineEdit.Password)
        form.addRow("API Key:", self.key)
        form.addRow("API Secret:", self.secret)
        if exchange == "okx":
            form.addRow("Passphrase:", self.passphrase)
        layout.addLayout(form)
        info = QLabel("Для 0.3 достаточно API с правами чтения. Withdraw и Trade не нужны.")
        info.setWordWrap(True); layout.addWidget(info)
        btn = QPushButton("Save"); btn.clicked.connect(self.save); layout.addWidget(btn)

    def save(self):
        if not self.key.text() or not self.secret.text():
            QMessageBox.warning(self, "Error", "API Key и API Secret обязательны.")
            return
        save_credentials(self.exchange, self.key.text(), self.secret.text(), self.passphrase.text())
        self.accept()

class ExchangeRow(QWidget):
    def __init__(self, name, parent):
        super().__init__()
        self.name, self.parent_window = name, parent
        l = QHBoxLayout(self)
        n = QLabel(name.upper()); n.setMinimumWidth(90)
        self.status = QLabel()
        c = QPushButton("Connect / Test"); c.clicked.connect(self.connect)
        d = QPushButton("Delete"); d.clicked.connect(self.delete)
        l.addWidget(n); l.addWidget(self.status); l.addStretch(); l.addWidget(c); l.addWidget(d)
        self.refresh()

    def refresh(self):
        self.status.setText("● Connected" if load_credentials(self.name) else "○ Not connected")

    def connect(self):
        if not load_credentials(self.name):
            if ApiDialog(self.name, self.parent_window).exec():
                self.refresh()
            return
        try:
            create_exchange(self.name).fetch_balance()
            self.status.setText("● Connected")
            self.parent_window.refresh_balances()
        except Exception as e:
            self.status.setText("● Error")
            QMessageBox.critical(self.parent_window, "Connection error", str(e))

    def delete(self):
        delete_credentials(self.name); self.refresh(); self.parent_window.refresh_balances()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Arbitrage Client 0.3")
        self.resize(1120, 720)
        self.busy = False
        self.scanning = False

        root = QVBoxLayout()
        w = QWidget(); w.setLayout(root); self.setCentralWidget(w)

        title = QLabel("ARBITRAGE CLIENT 0.3")
        title.setStyleSheet("font-size:26px;font-weight:bold;")
        root.addWidget(title)
        root.addWidget(QLabel("Read-only spot arbitrage scanner: BYBIT ↔ OKX. No order placement."))

        box = QGroupBox("Exchanges"); bl = QVBoxLayout(box)
        self.bybit = ExchangeRow("bybit", self); self.okx = ExchangeRow("okx", self)
        bl.addWidget(self.bybit); bl.addWidget(self.okx); root.addWidget(box)

        settings = QGroupBox("Scanner settings"); sl = QHBoxLayout(settings)
        sl.addWidget(QLabel("Capital USDT:"))
        self.capital = QDoubleSpinBox(); self.capital.setRange(1, 10000000); self.capital.setValue(1000); self.capital.setDecimals(2)
        sl.addWidget(self.capital)
        sl.addWidget(QLabel("Bybit taker %:"))
        self.fee_b = QDoubleSpinBox(); self.fee_b.setRange(0,5); self.fee_b.setValue(.10); self.fee_b.setDecimals(4); sl.addWidget(self.fee_b)
        sl.addWidget(QLabel("OKX taker %:"))
        self.fee_o = QDoubleSpinBox(); self.fee_o.setRange(0,5); self.fee_o.setValue(.10); self.fee_o.setDecimals(4); sl.addWidget(self.fee_o)
        sl.addWidget(QLabel("Min net %:"))
        self.min_net = QDoubleSpinBox(); self.min_net.setRange(-100,100); self.min_net.setValue(0); self.min_net.setDecimals(4); sl.addWidget(self.min_net)
        sl.addWidget(QLabel("Refresh sec:"))
        self.interval = QSpinBox(); self.interval.setRange(1,300); self.interval.setValue(5); sl.addWidget(self.interval)
        root.addWidget(settings)

        controls = QHBoxLayout()
        self.start = QPushButton("▶ Start scanner"); self.start.clicked.connect(self.toggle); controls.addWidget(self.start)
        now = QPushButton("Refresh now"); now.clicked.connect(self.scan); controls.addWidget(now)
        bal = QPushButton("Refresh balances"); bal.clicked.connect(self.refresh_balances); controls.addWidget(bal)
        controls.addStretch()
        self.status = QLabel("Ready"); controls.addWidget(self.status)
        root.addLayout(controls)

        self.table = QTableWidget(0,10)
        self.table.setHorizontalHeaderLabels(["Coin","BUY","SELL","Buy price","Sell price","Gross %","Net %","Qty","Notional","Est. profit"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        root.addWidget(self.table)

        bb = QGroupBox("USDT Spot Balance"); bbl = QHBoxLayout(bb)
        self.bbal = QLabel("BYBIT: —"); self.obal = QLabel("OKX: —")
        bbl.addWidget(self.bbal); bbl.addWidget(self.obal); root.addWidget(bb)

        self.timer = QTimer(self); self.timer.timeout.connect(self.scan)

    def refresh_balances(self):
        for name, label in [("bybit", self.bbal), ("okx", self.obal)]:
            try:
                ex = create_exchange(name)
                if not ex: label.setText(f"{name.upper()}: —"); continue
                usdt = ex.fetch_balance().get("USDT", {}).get("free", 0)
                label.setText(f"{name.upper()}: {flt(usdt):,.2f} USDT")
            except Exception:
                label.setText(f"{name.upper()}: error")

    def toggle(self):
        self.scanning = not self.scanning
        if self.scanning:
            self.start.setText("■ Stop scanner"); self.scan(); self.timer.start(self.interval.value()*1000)
        else:
            self.start.setText("▶ Start scanner"); self.timer.stop(); self.status.setText("Stopped")

    def scan(self):
        if self.busy: return
        self.busy = True; self.status.setText("Loading tickers…")
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fb, fo = pool.submit(load_tickers,"bybit"), pool.submit(load_tickers,"okx")
                a, b = fb.result(), fo.result()
            rows = opportunities(a,b,self.fee_b.value()/100,self.fee_o.value()/100,self.capital.value())
            rows = [r for r in rows if r["net"] >= self.min_net.value()]
            self.table.setRowCount(len(rows))
            for i,r in enumerate(rows):
                vals = [r["symbol"],r["buy"],r["sell"],f'{r["buy_price"]:,.8g}',f'{r["sell_price"]:,.8g}',
                        f'{r["gross"]:.4f}%',f'{r["net"]:.4f}%',f'{r["qty"]:.8g}',
                        f'{r["notional"]:,.2f}',f'${r["profit"]:,.2f}']
                for j,v in enumerate(vals):
                    item=QTableWidgetItem(v); item.setTextAlignment(Qt.AlignRight|Qt.AlignVCenter if j>=3 else Qt.AlignLeft|Qt.AlignVCenter)
                    self.table.setItem(i,j,item)
            self.status.setText(f"Found {len(rows)} opportunities | common symbols: {len(set(a)&set(b))}")
        except Exception as e:
            self.status.setText("Scanner error")
            QMessageBox.critical(self,"Scanner error",str(e))
        finally:
            self.busy=False

    def closeEvent(self,e):
        self.timer.stop(); e.accept()

if __name__ == "__main__":
    app=QApplication(sys.argv)
    win=MainWindow(); win.show()
    sys.exit(app.exec())
