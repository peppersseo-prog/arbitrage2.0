import sys,json
from concurrent.futures import ThreadPoolExecutor,as_completed
import ccxt,keyring
from PySide6.QtCore import QTimer,QThread,QObject,Signal
from PySide6.QtWidgets import QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QGroupBox,QDialog,QFormLayout,QLineEdit,QMessageBox,QTableWidget,QTableWidgetItem,QDoubleSpinBox,QSpinBox,QHeaderView,QCheckBox,QMenuBar

SERVICE="ArbitrageClient"; EXCHANGES=("bybit","okx","bitget")

def save_credentials(ex,key,secret,password=""):
    keyring.set_password(SERVICE,ex,json.dumps({"api_key":key,"api_secret":secret,"password":password}))
def load_credentials(ex):
    x=keyring.get_password(SERVICE,ex); return json.loads(x) if x else None
def delete_credentials(ex):
    try:keyring.delete_password(SERVICE,ex)
    except Exception:pass
def n(x):
    try:return float(x or 0)
    except:return 0.0
def txt(x):return str(x or "").strip().lower()

def make(ex):
    c=load_credentials(ex)
    if not c: raise RuntimeError(f"{ex.upper()}: API credentials not found")
    cfg={"apiKey":c["api_key"],"secret":c["api_secret"],"enableRateLimit":True,"timeout":20000,"options":{"defaultType":"spot"}}
    if ex in ("okx","bitget"):cfg["password"]=c.get("password","")
    return getattr(ccxt,ex)(cfg)

def contract_ids(m):
    """Extract any contract/address information exposed by CCXT/exchange."""
    if not isinstance(m, dict):
        return set()

    info = m.get("info") or {}
    out = set()

    addrkeys = (
        "contractAddress", "contract_address",
        "tokenAddress", "token_address",
        "address", "contractAddr", "tokenAddr",
        "contractAddressList", "contract_address_list",
    )
    netkeys = (
        "network", "chain", "chainName", "networkName",
        "chainType", "chainTypeName",
    )

    addresses = []
    networks = []

    for source in (m, info):
        if not isinstance(source, dict):
            continue

        for key in addrkeys:
            value = source.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        a = (
                            item.get("contractAddress")
                            or item.get("contract_address")
                            or item.get("address")
                            or item.get("tokenAddress")
                        )
                        net = (
                            item.get("network")
                            or item.get("chain")
                            or item.get("chainName")
                            or item.get("networkName")
                        )
                        if a:
                            out.add((txt(net), txt(a)))
                    elif value:
                        addresses.append(txt(item))
            elif value:
                addresses.append(txt(value))

        for key in netkeys:
            value = source.get(key)
            if isinstance(value, list):
                networks.extend(txt(x) for x in value if x)
            elif value:
                networks.append(txt(value))

    for address in addresses:
        for network in (networks or [""]):
            out.add((network, address))

    return out


def identity_candidates(m):
    """
    Return several identity signals.

    Strong signals:
      - contract + network
      - explicit asset/coin id

    Medium signal:
      - CCXT baseId + quote currency + spot type

    Ticker alone is deliberately NOT enough.
    """
    if not isinstance(m, dict):
        return set()

    result = set()

    for item in contract_ids(m):
        result.add(("contract", item))

    info = m.get("info") or {}

    for source in (m, info):
        if not isinstance(source, dict):
            continue
        for key in ("assetId", "asset_id", "coinId", "coin_id"):
            value = source.get(key)
            if value not in (None, ""):
                result.add(("assetid", txt(value)))

    base_id = m.get("baseId")
    base = m.get("base")
    quote = m.get("quote")
    market_type = m.get("type")

    # baseId is NOT enough by itself. It is only used as a fallback
    # identity for conventional spot assets after excluding obvious
    # synthetic/RWA/tokenized instruments.
    info_text = " ".join(
        txt(v) for v in info.values()
        if isinstance(v, (str, int, float))
    )

    suspicious_words = (
        "rwa", "stock", "equity", "share", "tokenized",
        "synthetic", "prestock", "xstock", "fractional"
    )

    if base_id and base and quote == "USDT" and market_type == "spot":
        if not any(word in info_text for word in suspicious_words):
            result.add((
                "base",
                txt(base_id),
                txt(quote),
            ))

    return result


def identity(m):
    """
    Select the best available identity.

    Important:
    We no longer require a contract address for every asset.
    This restores normal BTC/ETH/SOL-style matching while preventing
    ticker-only matches such as unrelated XTER assets.
    """
    candidates = identity_candidates(m)

    strong = sorted(
        x for x in candidates
        if x[0] in ("contract", "assetid")
    )

    if strong:
        return ("strong", tuple(strong))

    base = sorted(
        x for x in candidates
        if x[0] == "base"
    )

    if base:
        return ("base", tuple(base))

    return None


def margin_info(ex):
    """
    Return a set of base assets that can be borrowed for spot-margin selling.

    This is intentionally conservative:
    - We first inspect CCXT margin markets.
    - Then, where possible, inspect exchange-specific market metadata.
    - If an exchange/API does not expose reliable borrowability information,
      the asset is NOT considered margin-sellable.

    The scanner therefore avoids presenting a false "borrow available" result.
    """
    x = make(ex)
    x.load_markets()
    result = set()

    for symbol, m in x.markets.items():
        if not isinstance(symbol, str):
            continue
        if m.get("quote") != "USDT":
            continue
        if m.get("spot") is not True:
            continue

        # CCXT flags / market type.
        is_margin = bool(
            m.get("margin")
            or m.get("marginMode")
            or m.get("marginModes")
            or m.get("type") in ("margin", "spotMargin")
        )

        info = m.get("info") or {}
        info_text = " ".join(
            txt(v) for v in info.values()
            if isinstance(v, (str, int, float, bool))
        )

        # Exchange metadata frequently contains an explicit borrow flag.
        borrow_flag = None
        for key in (
            "borrowable", "borrowAble", "borrowableBase",
            "baseBorrowable", "canBorrow", "enableBorrow",
            "isBorrowable", "borrowEnabled"
        ):
            if key in m:
                borrow_flag = m.get(key)
                break
            if key in info:
                borrow_flag = info.get(key)
                break

        if borrow_flag is not None:
            ok = str(borrow_flag).lower() in (
                "true", "1", "yes", "y", "enabled", "enable"
            )
            if ok:
                result.add(m.get("base") or symbol.split("/")[0])
            continue

        # If CCXT explicitly says margin is supported, accept it as a
        # provisional borrowable candidate. The actual borrow request is
        # NOT performed by this read-only scanner.
        if is_margin:
            result.add(m.get("base") or symbol.split("/")[0])

    # Exchange-specific fallback: some CCXT market structures expose
    # margin pairs via type but not the top-level margin flag.
    if not result:
        for symbol, m in x.markets.items():
            if not isinstance(symbol, str) or m.get("quote") != "USDT":
                continue
            if m.get("spot") is not True:
                continue
            info = m.get("info") or {}
            text = " ".join(txt(v) for v in info.values())
            if any(k in text for k in ("borrowable", "borrowablebase", "borrowable_base")):
                result.add(m.get("base") or symbol.split("/")[0])

    return {txt(x) for x in result if x}


def margin_sellable(ex, base):
    """Cheap per-asset check using the cached margin set."""
    return txt(base) in margin_info(ex)


def tickers(ex):
    x=make(ex); x.load_markets()
    symbols=[s for s,m in x.markets.items() if isinstance(s,str) and m.get("quote")=="USDT" and m.get("active") is not False and (m.get("spot") is True or m.get("type")=="spot")]
    d=x.fetch_tickers(symbols,params={"category":"spot"}) if ex=="bybit" else x.fetch_tickers(symbols)
    out={}
    for s,t in d.items():
        bid,ask=n(t.get("bid")),n(t.get("ask"))
        if bid>0 and ask>0:out[s]={"bid":bid,"ask":ask,"market":x.markets.get(s),"identity":identity(x.markets.get(s))}
    return out

def book(ex,symbol):
    x=make(ex); x.load_markets()
    return x.fetch_order_book(symbol,limit=50,params={"category":"spot"}) if ex=="bybit" else x.fetch_order_book(symbol,limit=50)

def candidates(markets,fees,minnet,enabled):
    ids={}
    for ex in enabled:
        ids[ex]={}
        for s,d in markets.get(ex,{}).items():
            i=d.get("identity")
            if i is not None:ids[ex].setdefault(i,[]).append(s)
    out=[]
    for be in enabled:
        for se in enabled:
            if be==se:continue
            for i in set(ids.get(be,{}))&set(ids.get(se,{})):
                for bs in ids[be][i]:
                    for ss in ids[se][i]:
                        bp,sp=markets[be][bs]["ask"],markets[se][ss]["bid"]
                        bf,sf=fees[be],fees[se]
                        if bp<=0 or sp<=0:continue
                        net=(sp*(1-sf)/(bp*(1+bf))-1)*100
                        if net>=minnet:out.append((net,bs,ss,be,se,i))
    out.sort(reverse=True,key=lambda z:z[0]);return out

def depth(bb,sb,bf,sf,capital,minnet):
    def lv(raw):
        return [(n(a[0]),n(a[1])) for a in (raw or []) if isinstance(a,(list,tuple)) and len(a)>=2 and n(a[0])>0 and n(a[1])>0]
    asks=sorted(lv(bb.get("asks",[])));bids=sorted(lv(sb.get("bids",[])),reverse=True)
    if not asks or not bids:return None
    ai=bi=0;ar=asks[0][1];br=bids[0][1];qty=cost=sell=0
    while ai<len(asks) and bi<len(bids):
        ap,bp=asks[ai][0],bids[bi][0]
        if bp<=ap:break
        q=min(ar,br,max(0,(capital-cost)/(ap*(1+bf))))
        if q<=0:break
        qty+=q;cost+=q*ap;sell+=q*bp;ar-=q;br-=q
        if ar<=1e-14:
            ai+=1
            if ai<len(asks):ar=asks[ai][1]
        if br<=1e-14:
            bi+=1
            if bi<len(bids):br=bids[bi][1]
    if qty<=0 or cost<=0:return None
    profit=sell*(1-sf)-cost*(1+bf);net=profit/cost*100
    if net<minnet:return None
    return {"qty":qty,"cost":cost,"sell":sell,"avg_buy":cost/qty,"avg_sell":sell/qty,"top_buy":asks[0][0],"top_sell":bids[0][0],"top_gross":(bids[0][0]/asks[0][0]-1)*100,"net":net,"profit":profit,"fees":cost*bf+sell*sf}

class Worker(QObject):
    done=Signal(object,object);err=Signal(str)
    def __init__(self,fees,capital,minnet,maxnet,limit,enabled,require_margin):
        super().__init__()
        self.fees=fees
        self.capital=capital
        self.minnet=minnet
        self.maxnet=maxnet
        self.limit=limit
        self.enabled=list(enabled)
        self.require_margin=require_margin
        self.stop_requested=False
        self.margin_assets={}
    def stop(self):self.stop_requested=True
    def status(self,m,c):
        sets=[set(m.get(e,{})) for e in self.enabled if m.get(e)]
        return {
            "bybit": len(m.get("bybit", {})),
            "okx": len(m.get("okx", {})),
            "bitget": len(m.get("bitget", {})),
            "common": len(set.intersection(*sets)) if len(sets) >= 2 else 0,
            "checked": c,
            "identified": sum(
                1 for ex in self.enabled
                for d in m.get(ex, {}).values()
                if d.get("identity") is not None
            ),
        }
    def run(self):
        m={}
        try:
            with ThreadPoolExecutor(max_workers=len(self.enabled)) as p:
                fs={p.submit(tickers,e):e for e in self.enabled}
                for f in as_completed(fs):
                    if self.stop_requested:
                        for q in fs:q.cancel()
                        self.done.emit([],self.status(m,0));return
                    m[fs[f]]=f.result()
            for e in EXCHANGES:m.setdefault(e,{})
            if self.require_margin:
                with ThreadPoolExecutor(max_workers=len(self.enabled)) as mp:
                    mf={mp.submit(margin_info,e):e for e in self.enabled}
                    for f in as_completed(mf):
                        if self.stop_requested:
                            for q in mf:q.cancel()
                            self.done.emit([],self.status(m,0));return
                        try:self.margin_assets[mf[f]]=f.result()
                        except Exception:self.margin_assets[mf[f]]=set()

            cs=candidates(m,self.fees,self.minnet,self.enabled)

            if self.require_margin:
                cs=[
                    item for item in cs
                    if txt(m[item[3]][item[1]].get("market",{}).get("base") or item[1].split("/")[0])
                    in self.margin_assets.get(item[4],set())
                ]

            cs=[
                item for item in cs
                if item[0] <= self.maxnet
            ][:self.limit]

            books={}
            with ThreadPoolExecutor(max_workers=10) as p:
                fs={}
                for _,bs,ss,be,se,_ in cs:
                    k=(bs,ss,be,se);fs[p.submit(book,be,bs)]=(k,"buy");fs[p.submit(book,se,ss)]=(k,"sell")
                for f in as_completed(fs):
                    if self.stop_requested:
                        for q in fs:q.cancel()
                        self.done.emit([],self.status(m,len(cs)));return
                    k,side=fs[f]
                    try:books.setdefault(k,{})[side]=f.result()
                    except:pass
            rows=[]
            for _,bs,ss,be,se,_ in cs:
                if self.stop_requested:self.done.emit([],self.status(m,len(cs)));return
                k=(bs,ss,be,se);b=books.get(k,{})
                if "buy" not in b or "sell" not in b:continue
                r=depth(b["buy"],b["sell"],self.fees[be],self.fees[se],self.capital,self.minnet)
                if r:r.update(symbol=bs,sell_symbol=ss,buy=be,sell=se);rows.append(r)
            rows.sort(key=lambda x:x["profit"],reverse=True);self.done.emit(rows,self.status(m,len(cs)))
        except Exception as e:
            if not self.stop_requested:self.err.emit(f"{type(e).__name__}: {e}")

class Api(QDialog):
    def __init__(self,ex,parent=None):
        super().__init__(parent);self.ex=ex;self.setWindowTitle("Connect "+ex.upper());v=QVBoxLayout(self);f=QFormLayout()
        self.k=QLineEdit();self.s=QLineEdit();self.s.setEchoMode(QLineEdit.Password);self.p=QLineEdit();self.p.setEchoMode(QLineEdit.Password)
        f.addRow("API Key:",self.k);f.addRow("API Secret:",self.s)
        if ex in ("okx","bitget"):f.addRow("Passphrase:",self.p)
        v.addLayout(f);b=QPushButton("Save");b.clicked.connect(self.save);v.addWidget(b)
    def save(self):
        if not self.k.text() or not self.s.text():QMessageBox.warning(self,"Error","API Key и Secret обязательны.");return
        if self.ex in ("okx","bitget") and not self.p.text():QMessageBox.warning(self,"Error","Passphrase обязательна.");return
        save_credentials(self.ex,self.k.text(),self.s.text(),self.p.text());self.accept()

class Ex(QWidget):
    def __init__(self,name,app):
        super().__init__();self.name=name;self.app=app;l=QHBoxLayout(self);self.enabled=QCheckBox("Enabled");self.enabled.setChecked(True);l.addWidget(self.enabled);l.addWidget(QLabel(name.upper()));self.st=QLabel();l.addWidget(self.st);l.addStretch()
        b=QPushButton("Connect / Test");b.clicked.connect(self.connect);l.addWidget(b);b=QPushButton("Delete");b.clicked.connect(self.delete);l.addWidget(b);self.refresh()
    def is_enabled(self):return self.enabled.isChecked()
    def refresh(self):self.st.setText("● Connected" if load_credentials(self.name) else "○ Not connected")
    def connect(self):
        if not load_credentials(self.name):
            if Api(self.name,self.app).exec():self.refresh()
            return
        try:make(self.name).fetch_balance();self.st.setText("● Connected");self.app.balances()
        except Exception as e:QMessageBox.critical(self.app,"Connection error",str(e))
    def delete(self):delete_credentials(self.name);self.refresh();self.app.balances()


class ApiManager(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setWindowTitle("API / Connections")
        self.resize(600,260)
        v=QVBoxLayout(self)
        for ex in EXCHANGES:
            row=QHBoxLayout()
            row.addWidget(QLabel(ex.upper()))
            state=QLabel("● Connected" if load_credentials(ex) else "○ Not connected")
            row.addWidget(state)
            row.addStretch()
            b=QPushButton("Edit / Connect")
            b.clicked.connect(lambda _,e=ex:self.edit(e))
            row.addWidget(b)
            d=QPushButton("Delete")
            d.clicked.connect(lambda _,e=ex:self.remove(e))
            row.addWidget(d)
            v.addLayout(row)
        close=QPushButton("Close");close.clicked.connect(self.accept);v.addWidget(close)

    def edit(self,ex):
        if Api(ex,self).exec():
            self.accept()
            QTimer.singleShot(0,lambda:ApiManager(self.parent()).exec())

    def remove(self,ex):
        delete_credentials(ex)
        QMessageBox.information(self,"API","Credentials deleted for "+ex.upper())
        self.accept()
        QTimer.singleShot(0,lambda:ApiManager(self.parent()).exec())


class ExchangeManager(QDialog):
    def __init__(self,app):
        super().__init__(app)
        self.app=app
        self.setWindowTitle("Enabled Exchanges")
        v=QVBoxLayout(self)
        for ex in EXCHANGES:
            cb=QCheckBox(ex.upper())
            cb.setChecked(app.ex_widgets[ex].is_enabled())
            cb.stateChanged.connect(lambda state,e=ex:self.set_enabled(e,state))
            v.addWidget(cb)
        b=QPushButton("Close");b.clicked.connect(self.accept);v.addWidget(b)

    def set_enabled(self,ex,state):
        self.app.ex_widgets[ex].enabled.setChecked(bool(state))



class App(QMainWindow):
    def __init__(self):
        super().__init__();self.setWindowTitle("Arbitrage Client 0.9");self.resize(1450,800);self.th=None;self.w=None;self.running=False
        self.build_menu()
        root=QWidget();self.setCentralWidget(root);v=QVBoxLayout(root);h=QLabel("ARBITRAGE CLIENT 0.9");h.setStyleSheet("font-size:26px;font-weight:bold;");v.addWidget(h);v.addWidget(QLabel("Spot arbitrage scanner: BYBIT ↔ OKX ↔ BITGET with optional SELL-side margin/borrow filter."))
        g=QGroupBox("Exchanges");gl=QVBoxLayout(g);self.ex_widgets={}
        for e in EXCHANGES:
            self.ex_widgets[e]=Ex(e,self)
            self.ex_widgets[e].setVisible(False)
        g.setVisible(False)
        v.addWidget(g)

        g=QGroupBox("Scanner settings");sl=QHBoxLayout(g)
        self.cap=QDoubleSpinBox();self.cap.setRange(1,1e7);self.cap.setValue(1000);self.cap.setDecimals(2)
        self.bf=QDoubleSpinBox();self.bf.setRange(0,5);self.bf.setValue(.1);self.bf.setDecimals(4)
        self.of=QDoubleSpinBox();self.of.setRange(0,5);self.of.setValue(.1);self.of.setDecimals(4)
        self.gf=QDoubleSpinBox();self.gf.setRange(0,5);self.gf.setValue(.1);self.gf.setDecimals(4)
        self.mn=QDoubleSpinBox();self.mn.setRange(-100,100);self.mn.setValue(0);self.mn.setDecimals(4)
        self.mx=QDoubleSpinBox();self.mx.setRange(-100,1000);self.mx.setValue(100);self.mx.setDecimals(4)
        self.margin_required=QCheckBox("Require SELL margin / borrow")
        self.margin_required.setChecked(False)
        self.sec=QSpinBox();self.sec.setRange(3,300);self.sec.setValue(10)
        self.lim=QSpinBox();self.lim.setRange(5,300);self.lim.setValue(50)
        for t,x in [
            ("Capital USDT:",self.cap),
            ("Bybit taker %:",self.bf),
            ("OKX taker %:",self.of),
            ("Bitget taker %:",self.gf),
            ("Min net %:",self.mn),
            ("Max net %:",self.mx),
            ("Refresh sec:",self.sec),
            ("Order books:",self.lim)
        ]:
            sl.addWidget(QLabel(t));sl.addWidget(x)
        sl.addWidget(self.margin_required)
        v.addWidget(g);c=QHBoxLayout();self.start=QPushButton("▶ Start scanner");self.start.clicked.connect(self.toggle);c.addWidget(self.start)
        for t,f in [("Refresh now",self.scan),("Refresh balances",self.balances)]:b=QPushButton(t);b.clicked.connect(f);c.addWidget(b)
        c.addStretch();self.status=QLabel("Ready");c.addWidget(self.status);v.addLayout(c)
        self.tab=QTableWidget(0,14);self.tab.setHorizontalHeaderLabels(["Coin BUY","Coin SELL","BUY","SELL","Top buy","Top sell","Avg buy","Avg sell","Top gross %","Real net %","Qty","Notional","Profit","Fees"]);self.tab.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch);v.addWidget(self.tab)
        g=QGroupBox("USDT Spot Balance");bl=QHBoxLayout(g);self.balance_labels={}
        for e in EXCHANGES:self.balance_labels[e]=QLabel(e.upper()+": —");bl.addWidget(self.balance_labels[e])
        bl.addStretch();v.addWidget(g);self.timer=QTimer();self.timer.timeout.connect(self.scan)
    def build_menu(self):
        mb=self.menuBar()

        file_menu=mb.addMenu("File")
        exit_action=QAction("Exit",self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        ex_menu=mb.addMenu("Exchanges")
        api_action=QAction("API / Connections",self)
        api_action.triggered.connect(lambda:ApiManager(self).exec())
        ex_menu.addAction(api_action)
        enabled_action=QAction("Enabled Exchanges",self)
        enabled_action.triggered.connect(lambda:ExchangeManager(self).exec())
        ex_menu.addAction(enabled_action)

        settings_menu=mb.addMenu("Settings")
        scanner_action=QAction("Scanner settings",self)
        scanner_action.triggered.connect(self.show_scanner_settings)
        settings_menu.addAction(scanner_action)

        help_menu=mb.addMenu("Help")
        about=QAction("About",self)
        about.triggered.connect(lambda:QMessageBox.information(
            self,"About",
            "Arbitrage Client 0.9\\n"
            "Read-only spot arbitrage scanner.\\n"
            "Optional SELL-side margin/borrow filter."
        ))
        help_menu.addAction(about)

    def show_scanner_settings(self):
        QMessageBox.information(
            self,
            "Scanner settings",
            "Настройки сканера находятся в верхнем блоке окна.\\n\\n"
            "Доходность: Min / Max.\\n"
            "Margin filter: требует возможность SELL через borrow."
        )

    def balances(self):
        for e in EXCHANGES:
            try:self.balance_labels[e].setText(f'{e.upper()}: {n(make(e).fetch_balance().get("USDT",{}).get("free")):,.2f} USDT')
            except:self.balance_labels[e].setText(f"{e.upper()}: error")
    def toggle(self):
        if self.running:
            self.running=False;self.timer.stop()
            if self.w:self.w.stop()
            self.start.setEnabled(True);self.start.setText("▶ Start scanner");self.status.setText("Stopping…");return
        self.running=True;self.start.setEnabled(True);self.start.setText("■ Stop scanner");self.scan()
        if self.running:self.timer.start(self.sec.value()*1000)
    def scan(self):
        if self.th and self.th.isRunning():return
        enabled=[e for e in EXCHANGES if self.ex_widgets[e].is_enabled()]
        if len(enabled)<2:self.running=False;self.timer.stop();self.start.setText("▶ Start scanner");self.status.setText("Enable at least 2 exchanges");return
        missing=[e.upper() for e in enabled if not load_credentials(e)]
        if missing:self.running=False;self.timer.stop();self.start.setText("▶ Start scanner");self.status.setText("Connect: "+", ".join(missing));return
        self.status.setText("Loading "+" + ".join(e.upper() for e in enabled)+" market data…")
        fees={"bybit":self.bf.value()/100,"okx":self.of.value()/100,"bitget":self.gf.value()/100};self.th=QThread();self.w=Worker(fees,self.cap.value(),self.mn.value(),self.mx.value(),self.lim.value(),enabled,self.margin_required.isChecked());self.w.moveToThread(self.th);self.th.started.connect(self.w.run);self.w.done.connect(self.result);self.w.err.connect(self.error);self.w.done.connect(self.th.quit);self.w.err.connect(self.th.quit);self.th.finished.connect(self.cleanup);self.th.start()
    def cleanup(self):self.w=None;self.th=None
    def result(self,rows,st):
        if not self.running:self.status.setText("Stopped");return
        self.tab.setRowCount(len(rows))
        for i,r in enumerate(rows):
            vals=[r["symbol"],r["sell_symbol"],r["buy"].upper(),r["sell"].upper(),f'{r["top_buy"]:,.8g}',f'{r["top_sell"]:,.8g}',f'{r["avg_buy"]:,.8g}',f'{r["avg_sell"]:,.8g}',f'{r["top_gross"]:.4f}%',f'{r["net"]:.4f}%',f'{r["qty"]:,.8g}',f'${r["cost"]:,.2f}',f'${r["profit"]:,.4f}',f'${r["fees"]:,.4f}']
            for j,x in enumerate(vals):self.tab.setItem(i,j,QTableWidgetItem(x))
        self.status.setText(
            f'OK: Bybit {st["bybit"]} | OKX {st["okx"]} | '
            f'Bitget {st["bitget"]} | common ticker {st["common"]} | '
            f'identified {st.get("identified", 0)} | '
            f'candidates {st["checked"]} | opportunities {len(rows)}'
        )
    def error(self,msg):
        if not self.running:self.status.setText("Stopped");return
        self.status.setText("Scanner error");QMessageBox.critical(self,"Scanner error","Не удалось получить данные.\n\n"+msg)
    def closeEvent(self,e):
        self.timer.stop()
        if self.w:self.w.stop()
        e.accept()

app=QApplication(sys.argv);win=App();win.show();sys.exit(app.exec())
