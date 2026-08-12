import sys,json
from concurrent.futures import ThreadPoolExecutor,as_completed
import ccxt,keyring
from PySide6.QtCore import QTimer,QThread,QObject,Signal
from PySide6.QtWidgets import QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QGroupBox,QDialog,QFormLayout,QLineEdit,QMessageBox,QTableWidget,QTableWidgetItem,QDoubleSpinBox,QSpinBox,QHeaderView,QCheckBox

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
    def __init__(self,fees,capital,minnet,limit,enabled):
        super().__init__();self.fees=fees;self.capital=capital;self.minnet=minnet;self.limit=limit;self.enabled=list(enabled);self.stop_requested=False
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
            cs=candidates(m,self.fees,self.minnet,self.enabled)[:self.limit];books={}
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

class App(QMainWindow):
    def __init__(self):
        super().__init__();self.setWindowTitle("Arbitrage Client 0.8.1");self.resize(1450,800);self.th=None;self.w=None;self.running=False
        root=QWidget();self.setCentralWidget(root);v=QVBoxLayout(root);h=QLabel("ARBITRAGE CLIENT 0.8.1");h.setStyleSheet("font-size:26px;font-weight:bold;");v.addWidget(h);v.addWidget(QLabel("Strict asset-identity spot arbitrage scanner: BYBIT ↔ OKX ↔ BITGET."))
        g=QGroupBox("Exchanges");gl=QVBoxLayout(g);self.ex_widgets={}
        for e in EXCHANGES:self.ex_widgets[e]=Ex(e,self);gl.addWidget(self.ex_widgets[e])
        v.addWidget(g);g=QGroupBox("Scanner settings");sl=QHBoxLayout(g)
        self.cap=QDoubleSpinBox();self.cap.setRange(1,1e7);self.cap.setValue(1000);self.cap.setDecimals(2)
        self.bf=QDoubleSpinBox();self.bf.setRange(0,5);self.bf.setValue(.1);self.bf.setDecimals(4)
        self.of=QDoubleSpinBox();self.of.setRange(0,5);self.of.setValue(.1);self.of.setDecimals(4)
        self.gf=QDoubleSpinBox();self.gf.setRange(0,5);self.gf.setValue(.1);self.gf.setDecimals(4)
        self.mn=QDoubleSpinBox();self.mn.setRange(-100,100);self.mn.setValue(0);self.mn.setDecimals(4)
        self.sec=QSpinBox();self.sec.setRange(3,300);self.sec.setValue(10)
        self.lim=QSpinBox();self.lim.setRange(5,300);self.lim.setValue(50)
        for t,x in [("Capital USDT:",self.cap),("Bybit taker %:",self.bf),("OKX taker %:",self.of),("Bitget taker %:",self.gf),("Min net %:",self.mn),("Refresh sec:",self.sec),("Order books:",self.lim)]:sl.addWidget(QLabel(t));sl.addWidget(x)
        v.addWidget(g);c=QHBoxLayout();self.start=QPushButton("▶ Start scanner");self.start.clicked.connect(self.toggle);c.addWidget(self.start)
        for t,f in [("Refresh now",self.scan),("Refresh balances",self.balances)]:b=QPushButton(t);b.clicked.connect(f);c.addWidget(b)
        c.addStretch();self.status=QLabel("Ready");c.addWidget(self.status);v.addLayout(c)
        self.tab=QTableWidget(0,14);self.tab.setHorizontalHeaderLabels(["Coin BUY","Coin SELL","BUY","SELL","Top buy","Top sell","Avg buy","Avg sell","Top gross %","Real net %","Qty","Notional","Profit","Fees"]);self.tab.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch);v.addWidget(self.tab)
        g=QGroupBox("USDT Spot Balance");bl=QHBoxLayout(g);self.balance_labels={}
        for e in EXCHANGES:self.balance_labels[e]=QLabel(e.upper()+": —");bl.addWidget(self.balance_labels[e])
        bl.addStretch();v.addWidget(g);self.timer=QTimer();self.timer.timeout.connect(self.scan)
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
        fees={"bybit":self.bf.value()/100,"okx":self.of.value()/100,"bitget":self.gf.value()/100};self.th=QThread();self.w=Worker(fees,self.cap.value(),self.mn.value(),self.lim.value(),enabled);self.w.moveToThread(self.th);self.th.started.connect(self.w.run);self.w.done.connect(self.result);self.w.err.connect(self.error);self.w.done.connect(self.th.quit);self.w.err.connect(self.th.quit);self.th.finished.connect(self.cleanup);self.th.start()
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
