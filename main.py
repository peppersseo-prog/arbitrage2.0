import sys
import json
import keyring
import ccxt

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QGroupBox, QDialog, QFormLayout,
    QLineEdit, QMessageBox
)

SERVICE = "ArbitrageClient"


def save_credentials(exchange, api_key, api_secret, password=""):
    data = {
        "api_key": api_key,
        "api_secret": api_secret,
        "password": password
    }
    keyring.set_password(SERVICE, exchange, json.dumps(data))


def load_credentials(exchange):
    data = keyring.get_password(SERVICE, exchange)
    return json.loads(data) if data else None


def delete_credentials(exchange):
    try:
        keyring.delete_password(SERVICE, exchange)
    except Exception:
        pass


def create_exchange(exchange):
    credentials = load_credentials(exchange)

    if not credentials:
        return None

    if exchange == "bybit":
        return ccxt.bybit({
            "apiKey": credentials["api_key"],
            "secret": credentials["api_secret"],
            "enableRateLimit": True,
        })

    if exchange == "okx":
        return ccxt.okx({
            "apiKey": credentials["api_key"],
            "secret": credentials["api_secret"],
            "password": credentials["password"],
            "enableRateLimit": True,
        })

    return None


class ApiDialog(QDialog):

    def __init__(self, exchange, parent=None):
        super().__init__(parent)

        self.exchange = exchange

        self.setWindowTitle(f"Connect {exchange.upper()}")
        self.setMinimumWidth(450)

        layout = QVBoxLayout(self)

        form = QFormLayout()

        self.api_key = QLineEdit()

        self.api_secret = QLineEdit()
        self.api_secret.setEchoMode(QLineEdit.Password)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)

        form.addRow("API Key:", self.api_key)
        form.addRow("API Secret:", self.api_secret)

        if exchange == "okx":
            form.addRow("Passphrase:", self.password)

        layout.addLayout(form)

        info = QLabel(
            "Рекомендуется создать API-ключ без права Withdraw."
        )
        info.setWordWrap(True)

        layout.addWidget(info)

        save = QPushButton("Save")
        save.clicked.connect(self.save)

        layout.addWidget(save)

    def save(self):

        if not self.api_key.text() or not self.api_secret.text():
            QMessageBox.warning(
                self,
                "Error",
                "API Key и API Secret обязательны."
            )
            return

        save_credentials(
            self.exchange,
            self.api_key.text(),
            self.api_secret.text(),
            self.password.text()
        )

        QMessageBox.information(
            self,
            "Saved",
            "API credentials сохранены локально."
        )

        self.accept()


class ExchangeRow(QWidget):

    def __init__(self, exchange, parent):
        super().__init__()

        self.exchange = exchange
        self.parent_window = parent

        layout = QHBoxLayout(self)

        name = QLabel(exchange.upper())
        name.setMinimumWidth(100)

        self.status = QLabel()

        connect = QPushButton("Connect / Test")
        connect.clicked.connect(self.connect)

        delete = QPushButton("Delete")
        delete.clicked.connect(self.delete)

        layout.addWidget(name)
        layout.addWidget(self.status)
        layout.addStretch()
        layout.addWidget(connect)
        layout.addWidget(delete)

        self.refresh_status()

    def refresh_status(self):

        if load_credentials(self.exchange):
            self.status.setText("● API saved")
        else:
            self.status.setText("○ Not connected")

    def connect(self):

        if not load_credentials(self.exchange):

            dialog = ApiDialog(
                self.exchange,
                self.parent_window
            )

            if dialog.exec():
                self.refresh_status()

            return

        try:

            client = create_exchange(self.exchange)

            client.fetch_balance()

            self.status.setText("● Connected")

            self.parent_window.refresh_balances()

        except Exception as error:

            self.status.setText("● Error")

            QMessageBox.critical(
                self.parent_window,
                "Connection error",
                str(error)
            )

    def delete(self):

        delete_credentials(self.exchange)

        self.refresh_status()

        self.parent_window.refresh_balances()


class MainWindow(QMainWindow):

    def __init__(self):

        super().__init__()

        self.setWindowTitle("Arbitrage Client 0.2")

        self.resize(700, 450)

        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)

        title = QLabel("ARBITRAGE CLIENT")

        title.setStyleSheet(
            "font-size: 26px; font-weight: bold;"
        )

        layout.addWidget(title)

        subtitle = QLabel(
            "Local exchange connection manager"
        )

        layout.addWidget(subtitle)

        exchanges_box = QGroupBox("Exchanges")

        exchanges_layout = QVBoxLayout(exchanges_box)

        self.bybit = ExchangeRow(
            "bybit",
            self
        )

        self.okx = ExchangeRow(
            "okx",
            self
        )

        exchanges_layout.addWidget(self.bybit)
        exchanges_layout.addWidget(self.okx)

        layout.addWidget(exchanges_box)

        balance_box = QGroupBox(
            "USDT Spot Balance"
        )

        balance_layout = QVBoxLayout(balance_box)

        self.bybit_balance = QLabel(
            "BYBIT: —"
        )

        self.okx_balance = QLabel(
            "OKX: —"
        )

        balance_layout.addWidget(
            self.bybit_balance
        )

        balance_layout.addWidget(
            self.okx_balance
        )

        refresh = QPushButton(
            "Refresh balances"
        )

        refresh.clicked.connect(
            self.refresh_balances
        )

        balance_layout.addWidget(refresh)

        layout.addWidget(balance_box)

        layout.addStretch()

    def refresh_balances(self):

        for exchange, label in [
            ("bybit", self.bybit_balance),
            ("okx", self.okx_balance)
        ]:

            try:

                client = create_exchange(exchange)

                if not client:

                    label.setText(
                        f"{exchange.upper()}: —"
                    )

                    continue

                balance = client.fetch_balance()

                usdt = balance.get(
                    "USDT",
                    {}
                ).get(
                    "free",
                    0
                )

                label.setText(
                    f"{exchange.upper()}: "
                    f"{float(usdt):,.2f} USDT"
                )

            except Exception:

                label.setText(
                    f"{exchange.upper()}: error"
                )


if __name__ == "__main__":

    app = QApplication(sys.argv)

    window = MainWindow()

    window.show()

    sys.exit(app.exec())
