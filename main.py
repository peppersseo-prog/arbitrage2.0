# Arbitrage Client 0.4
# Real order-book depth scanner for Bybit <-> OKX.
# Replace the repository main.py with this file and rebuild the Windows EXE.

import sys, json
from concurrent.futures import ThreadPoolExecutor, as_completed
import ccxt, keyring
from PySide6.QtCore import QTimer, QThread, QObject, Signal
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,
 QLabel,QPushButton,QGroupBox,QDialog,QFormLayout,QLineEdit,QMessageBox,
 QTableWidget,QTableWidgetItem,QDoubleSpinBox,QSpinBox,QHeaderView)

SERVICE="ArbitrageClient"

def save_credentials(ex,key,secret,password=""):
    keyring.set_password(SERVICE,ex,json.dumps({"api_key":key,"api_secret":secret,"password":password}))
def load_credentials(ex):
    x=keyring.get_password(SERVICE,ex); return json.loads(x) if x else None
def delete_credentials(ex):
    try: keyring.delete_password(SERVICE,ex)
    except Exception: pass
def n(x):
    try:return float(x or 0)
    except:return 0.0

def make(ex):
    c=load_credentials(ex)
    if not c: raise RuntimeError(f"{ex.upper()}: API credentials not found")
    cfg={"apiKey":c["api_key"],"secret":c["api_secret"],"enableRateLimit":True,"timeout":20000}
    if ex=="bybit":
        cfg["options"]={"defaultType":"spot"}
        return ccxt.bybit(cfg)
    cfg["password"]=c.get("password",""); cfg["options"]={"defaultType":"spot"}
    return ccxt.okx(cfg)

def tickers(ex):
    x=make(ex); x.load_markets()
    d=x.fetch_tickers(params={"category":"spot"}) if ex=="bybit" else x.fetch_tickers()
    out={}
    for s,t in d.items():
        if not s.endswith("/USDT") or s not in x.markets: continue
        m=x.markets[s]
        if m.get("active") is False or m.get("spot") is not True: continue
        bid,ask=n(t.get("bid")),n(t.get("ask"))
        if bid>0 and ask>0: out[s]={"bid":bid,"ask":ask}
    return out

def candidates(a,b,bf,of,minnet):
    z=[]
    for s in set(a)&set(b):
        for buy,sell,buyfee,sellfee in [
            ("BYBIT","OKX",bf,of),("OKX","BYBIT",of,bf)]:
            A=a[s] if buy=="BYBIT" else b[s]
            B=b[s] if sell=="OKX" else a[s]
            bp,sp=A["ask"],B["bid"]
            gross=(sp/bp-1)*100
            net=(sp*(1-sellfee)/(bp*(1+buyfee))-1)*100
            if net>=minnet:z.append((net,s,buy,sell))
    z.sort(reverse=True)
    return z

def book(ex,symbol):
    x=make(ex); x.load_markets()
    return x.fetch_order_book(symbol,limit=50,params={"category":"spot"}) if ex=="bybit" else x.fetch_order_book(symbol,limit=50)

def depth(buybook,sellbook,bf,sf,capital,minnet):
    asks=sorted([(n(p),n(q)) for p,q in buybook.get("asks",[]) if n(p)>0 and n(q)>0])
    bids=sorted([(n(p),n(q)) for p,q in sellbook.get("bids",[]) if n(p)>0 and n(q)>0],reverse=True)
    if not asks or not bids:return None
    ai=bi=0; ar=asks[0][1]; br=bids[0][1]
    qty=cost=sell=0.0
    while ai<len(asks) and bi<len(bids):
        ap,bp=asks[ai][0],bids[bi][0]
        if bp<=ap:break
        cap=max(0,(capital-cost)/(ap*(1+bf)))
        q=min(ar,br,cap)
        if q<=0:break
        qty+=q; cost+=q*ap; sell+=q*bp
        ar-=q;br-=q
        if ar<=1e-14:
            ai+=1
            if ai<len(asks):ar=asks[ai][1]
        if br<=1e-14:
            bi+=1
            if bi<len(bids):br=bids[bi][1]
    if qty<=0:return None
    profit=sell*(1-sf)-cost*(1+bf)
    net=profit/cost*100
    if net<minnet:return None
    return {"qty":qty,"cost":cost,"sell":sell,"avg_buy":cost/qty,"avg_sell":sell/qty,
            "top_buy":asks[0][0],"top_sell":bids[0][0],
            "top_gross":(bids[0][0]/asks[0][0]-1)*100,
            "net":net,"profit":profit,
            "fees":cost*bf+sell*sf}

class Worker(QObject):
    done=Signal(object,object); err=Signal(str)
    def __init__(self,bf,of,capital,minnet,limit):
        super().__init__();self.bf=bf;self.of=of;self.capital=capital;self.minnet=minnet;self.limit=limit
    def run(self):
        try:
            with ThreadPoolExecutor(2) as p:
                fa=p.submit(tickers,"bybit"); fb=p.submit(tickers,"okx")
                a,b=fa.result(),fb.result()
            cs=candidates(a,b,self.bf,self.of,self.minnet)[:self.limit]
            books={}
            with ThreadPoolExecutor(6) as p:
                fs={}
                for _,s,buy,sell in cs:
                    key=(s,buy,sell)
                    fs[p.submit(book,buy.lower(),s)] = (key,"buy")
                    fs[p.submit(book,sell.lower(),s)] = (key,"sell")
                for f in as_completed(fs):
                    key,side=fs[f]
                    try:books.setdefault(key,{})[side]=f.result()
                    except Exception:pass
            rows=[]
            for _,s,buy,sell in cs:
                key=(s,buy,sell); bb=books.get(key,{})
                if "buy" not in bb or "sell" not in bb:continue
                bf=self.bf if buy=="BYBIT" else self.of
                sf=self.of if sell=="OKX" else self.bf
                r=depth(bb["buy"],bb["sell"],bf,sf,self.capital,self.minnet)
                if r:r.update(symbol=s,buy=buy,sell=sell);rows.append(r)
            rows.sort(key=lambda x:x["profit"],reverse=True)
            self.done.emit(rows,{"bybit":len(a),"okx":len(b),"common":len(set(a)&set(b)),"checked":len(cs)})
        except Exception as e:self.err.emit(f"{type(e).__name__}: {e}")

class Api(QDialog):
    def __init__(self,ex,parent=None):
        super().__init__(parent);self.ex=ex;self.setWindowTitle("Connect "+ex.upper())
        v=QVBoxLayout(self);f=QFormLayout()
        self.k=QLineEdit();self.s=QLineEdit();self.s.setEchoMode(QLineEdit.Password);self.p=QLineEdit();self.p.setEchoMode(QLineEdit.Password)
        f.addRow("API Key:",self.k);f.addRow("API Secret:",self.s)
        if ex=="okx":f.addRow("Passphrase:",self.p)
        v.addLayout(f);b=QPushButton("Save");b.clicked.connect(self.save);v.addWidget(b)
    def save(self):
        if not self.k.text() or not self.s.text():QMessageBox.warning(self,"Error","API Key и Secret обязательны.");return
        save_credentials(self.ex,self.k.text(),self.s.text(),self.p.text());self.accept()

class Ex(QWidget):
    def __init__(self,name,app):
        super().__init__();self.name=name;self.app=app;l=QHBoxLayout(self);l.addWidget(QLabel(name.upper()))
        self.st=QLabel();l.addWidget(self.st);l.addStretch()
        b=QPushButton("Connect / Test");b.clicked.connect(self.connect);l.addWidget(b)
        b=QPushButton("Delete");b.clicked.connect(self.delete);l.addWidget(b);self.refresh()
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
        super().__init__();self.setWindowTitle("Arbitrage Client 0.4");self.resize(1280,760);self.th=None;self.w=None;self.running=False
        root=QWidget();self.setCentralWidget(root);v=QVBoxLayout(root)
        h=QLabel("ARBITRAGE CLIENT 0.4");h.setStyleSheet("font-size:26px;font-weight:bold;");v.addWidget(h)
        v.addWidget(QLabel("Read-only spot arbitrage scanner: BYBIT ↔ OKX with real order-book depth."))
        g=QGroupBox("Exchanges");gl=QVBoxLayout(g);self.by=Ex("bybit",self);self.ok=Ex("okx",self);gl.addWidget(self.by);gl.addWidget(self.ok);v.addWidget(g)
        g=QGroupBox("Scanner settings");sl=QHBoxLayout(g)
        self.cap=QDoubleSpinBox();self.cap.setRange(1,1e7);self.cap.setValue(1000);self.cap.setDecimals(2)
        self.bf=QDoubleSpinBox();self.bf.setRange(0,5);self.bf.setValue(.1);self.bf.setDecimals(4)
        self.of=QDoubleSpinBox();self.of.setRange(0,5);self.of.setValue(.1);self.of.setDecimals(4)
        self.mn=QDoubleSpinBox();self.mn.setRange(-100,100);self.mn.setValue(0);self.mn.setDecimals(4)
        self.sec=QSpinBox();self.sec.setRange(3,300);self.sec.setValue(10)
        self.lim=QSpinBox();self.lim.setRange(5,200);self.lim.setValue(50)
        for t,x in [("Capital USDT:",self.cap),("Bybit taker %:",self.bf),("OKX taker %:",self.of),("Min net %:",self.mn),("Refresh sec:",self.sec),("Order books:",self.lim)]:sl.addWidget(QLabel(t));sl.addWidget(x)
        v.addWidget(g)
        c=QHBoxLayout();self.start=QPushButton("▶ Start scanner");self.start.clicked.connect(self.toggle);c.addWidget(self.start)
        for t,f in [("Refresh now",self.scan),("Refresh balances",self.balances)]:b=QPushButton(t);b.clicked.connect(f);c.addWidget(b)
        c.addStretch();self.status=QLabel("Ready");c.addWidget(self.status);v.addLayout(c)
        self.tab=QTableWidget(0,13);self.tab.setHorizontalHeaderLabels(["Coin","BUY","SELL","Top buy","Top sell","Avg buy","Avg sell","Top gross %","Real net %","Qty","Notional","Profit","Fees"]);self.tab.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch);v.addWidget(self.tab)
        g=QGroupBox("USDT Spot Balance");bl=QHBoxLayout(g);self.bb=QLabel("BYBIT: —");self.oo=QLabel("OKX: —");bl.addWidget(self.bb);bl.addWidget(self.oo);bl.addStretch();v.addWidget(g)
        self.timer=QTimer();self.timer.timeout.connect(self.scan)
    def balances(self):
        for ex,label in [("bybit",self.bb),("okx",self.oo)]:
            try:label.setText(f"{ex.upper()}: {n(make(ex).fetch_balance().get('USDT',{}).get('free')):,.2f} USDT")
            except:label.setText(f"{ex.upper()}: error")
    def toggle(self):
        self.running=not self.running
        if self.running:self.start.setText("■ Stop scanner");self.scan();self.timer.start(self.sec.value()*1000)
        else:self.start.setText("▶ Start scanner");self.timer.stop();self.status.setText("Stopped")
    def scan(self):
        if self.th and self.th.isRunning():return
        self.status.setText("Loading tickers and order-book depth…");self.start.setEnabled(False)
        self.th=QThread();self.w=Worker(self.bf.value()/100,self.of.value()/100,self.cap.value(),self.mn.value(),self.lim.value());self.w.moveToThread(self.th)
        self.th.started.connect(self.w.run);self.w.done.connect(self.result);self.w.err.connect(self.error);self.w.done.connect(self.th.quit);self.w.err.connect(self.th.quit);self.th.finished.connect(lambda:self.start.setEnabled(True));self.th.finished.connect(self.cleanup);self.th.start()
    def cleanup(self):self.w=None;self.th=None
    def result(self,rows,st):
        self.tab.setRowCount(len(rows))
        for i,r in enumerate(rows):
            vals=[r["symbol"],r["buy"],r["sell"],f'{r["top_buy"]:,.8g}',f'{r["top_sell"]:,.8g}',f'{r["avg_buy"]:,.8g}',f'{r["avg_sell"]:,.8g}',f'{r["top_gross"]:.4f}%',f'{r["net"]:.4f}%',f'{r["qty"]:,.8g}',f'${r["cost"]:,.2f}',f'${r["profit"]:,.4f}',f'${r["fees"]:,.4f}']
            for j,x in enumerate(vals):self.tab.setItem(i,j,QTableWidgetItem(x))
        self.status.setText(f'OK: Bybit {st["bybit"]} | OKX {st["okx"]} | common {st["common"]} | depth checked {st["checked"]} | opportunities {len(rows)}')
    def error(self,msg):self.status.setText("Scanner error");QMessageBox.critical(self,"Scanner error","Не удалось получить данные.\n\n"+msg)
    def closeEvent(self,e):self.timer.stop();e.accept()

app=QApplication(sys.argv);win=App();win.show();sys.exit(app.exec())
