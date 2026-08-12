import sys, json
from concurrent.futures import ThreadPoolExecutor
import ccxt, keyring
from PySide6.QtCore import QTimer, QThread, QObject, Signal
from PySide6.QtWidgets import QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QGroupBox,QDialog,QFormLayout,QLineEdit,QMessageBox,QTableWidget,QTableWidgetItem,QDoubleSpinBox,QSpinBox,QHeaderView

SERVICE="ArbitrageClient"

def save_credentials(ex,key,secret,password=""):
    keyring.set_password(SERVICE,ex,json.dumps({"api_key":key,"api_secret":secret,"password":password}))

def load_credentials(ex):
    x=keyring.get_password(SERVICE,ex)
    return json.loads(x) if x else None

def delete_credentials(ex):
    try:keyring.delete_password(SERVICE,ex)
    except Exception:pass

def create_exchange(name):
    c=load_credentials(name)
    if not c: raise RuntimeError(f"{name.upper()}: API credentials not found")
    cfg={"apiKey":c["api_key"],"secret":c["api_secret"],"enableRateLimit":True,"timeout":20000}
    if name=="bybit":
        cfg["options"]={"defaultType":"spot"}
    else:
        cfg["password"]=c["password"]
        cfg["options"]={"defaultType":"spot"}
    return ccxt.bybit(cfg) if name=="bybit" else ccxt.okx(cfg)

def f(x):
    try:return float(x or 0)
    except:return 0.0

def tickers(name):
    ex=create_exchange(name)
    ex.load_markets()
    if name=="bybit":
        data=ex.fetch_tickers(params={"category":"spot"})
    else:
        data=ex.fetch_tickers()
    out={}
    for s,t in data.items():
        if not s.endswith("/USDT") or s not in ex.markets: continue
        m=ex.markets[s]
        if m.get("active") is False or m.get("spot") is not True or m.get("quote")!="USDT": continue
        bid,ask=f(t.get("bid")),f(t.get("ask"))
        if bid>0 and ask>0:
            out[s]={"bid":bid,"ask":ask,"bv":f(t.get("bidVolume")),"av":f(t.get("askVolume"))}
    return out

def find(a,b,fb,fo,capital):
    rows=[]
    for s in set(a)&set(b):
        for buy,sell,bfee,sfee in [("BYBIT","OKX",fb,fo),("OKX","BYBIT",fo,fb)]:
            x,y=(a[s],b[s]) if buy=="BYBIT" else (b[s],a[s])
            bp,sp=x["ask"],y["bid"]
            gross=(sp/bp-1)*100
            net=(sp*(1-sfee)/(bp*(1+bfee))-1)*100
            q=capital/bp
            if x["av"]>0:q=min(q,x["av"])
            if y["bv"]>0:q=min(q,y["bv"])
            n=q*bp
            rows.append((s,buy,sell,bp,sp,gross,net,q,n,n*net/100))
    return sorted(rows,key=lambda r:r[6],reverse=True)

class Worker(QObject):
    ok=Signal(object,object)
    err=Signal(str)
    def __init__(self,fb,fo,capital,minnet):
        super().__init__();self.fb=fb;self.fo=fo;self.capital=capital;self.minnet=minnet
    def run(self):
        try:
            with ThreadPoolExecutor(max_workers=2) as p:
                fa=p.submit(tickers,"bybit"); fo=p.submit(tickers,"okx")
                a,b=fa.result(),fo.result()
            rows=[r for r in find(a,b,self.fb,self.fo,self.capital) if r[6]>=self.minnet]
            self.ok.emit(rows,(len(a),len(b),len(set(a)&set(b))))
        except Exception as e:self.err.emit(f"{type(e).__name__}: {e}")

class Api(QDialog):
    def __init__(self,ex,parent=None):
        super().__init__(parent);self.ex=ex;self.setWindowTitle("Connect "+ex.upper())
        v=QVBoxLayout(self);fml=QFormLayout();self.key=QLineEdit();self.sec=QLineEdit();self.sec.setEchoMode(QLineEdit.Password);self.pw=QLineEdit();self.pw.setEchoMode(QLineEdit.Password)
        fml.addRow("API Key:",self.key);fml.addRow("API Secret:",self.sec)
        if ex=="okx":fml.addRow("Passphrase:",self.pw)
        v.addLayout(fml);b=QPushButton("Save");b.clicked.connect(self.save);v.addWidget(b)
    def save(self):
        if not self.key.text() or not self.sec.text():
            QMessageBox.warning(self,"Error","API Key и Secret обязательны.");return
        save_credentials(self.ex,self.key.text(),self.sec.text(),self.pw.text());self.accept()

class ExRow(QWidget):
    def __init__(self,name,parent):
        super().__init__();self.name=name;self.p=parent;l=QHBoxLayout(self);l.addWidget(QLabel(name.upper()))
        self.st=QLabel();l.addWidget(self.st);l.addStretch();b=QPushButton("Connect / Test");b.clicked.connect(self.connect);l.addWidget(b);d=QPushButton("Delete");d.clicked.connect(self.delete);l.addWidget(d);self.refresh()
    def refresh(self):self.st.setText("● Connected" if load_credentials(self.name) else "○ Not connected")
    def connect(self):
        if not load_credentials(self.name):
            if Api(self.name,self.p).exec():self.refresh()
            return
        try:create_exchange(self.name).fetch_balance();self.st.setText("● Connected");self.p.refresh_balances()
        except Exception as e:self.st.setText("● Error");QMessageBox.critical(self.p,"Connection error",str(e))
    def delete(self):delete_credentials(self.name);self.refresh();self.p.refresh_balances()

class App(QMainWindow):
    def __init__(self):
        super().__init__();self.setWindowTitle("Arbitrage Client 0.3.2");self.resize(1120,720);self.th=None;self.wk=None;self.running=False
        v=QVBoxLayout();w=QWidget();w.setLayout(v);self.setCentralWidget(w)
        h=QLabel("ARBITRAGE CLIENT 0.3.2");h.setStyleSheet("font-size:26px;font-weight:bold;");v.addWidget(h);v.addWidget(QLabel("Read-only spot arbitrage scanner: BYBIT ↔ OKX."))
        g=QGroupBox("Exchanges");gl=QVBoxLayout(g);self.br=ExRow("bybit",self);self.orow=ExRow("okx",self);gl.addWidget(self.br);gl.addWidget(self.orow);v.addWidget(g)
        s=QGroupBox("Scanner settings");sl=QHBoxLayout(s)
        self.cap=QDoubleSpinBox();self.cap.setRange(1,1e7);self.cap.setValue(1000);self.cap.setDecimals(2)
        self.fb=QDoubleSpinBox();self.fb.setRange(0,5);self.fb.setValue(.1);self.fb.setDecimals(4)
        self.fo=QDoubleSpinBox();self.fo.setRange(0,5);self.fo.setValue(.1);self.fo.setDecimals(4)
        self.mn=QDoubleSpinBox();self.mn.setRange(-100,100);self.mn.setDecimals(4)
        self.iv=QSpinBox();self.iv.setRange(1,300);self.iv.setValue(5)
        for label,x in [("Capital USDT:",self.cap),("Bybit taker %:",self.fb),("OKX taker %:",self.fo),("Min net %:",self.mn),("Refresh sec:",self.iv)]:sl.addWidget(QLabel(label));sl.addWidget(x)
        v.addWidget(s)
        c=QHBoxLayout();self.start=QPushButton("▶ Start scanner");self.start.clicked.connect(self.toggle);c.addWidget(self.start)
        for text,fn in [("Refresh now",self.scan),("Refresh balances",self.refresh_balances)]:b=QPushButton(text);b.clicked.connect(fn);c.addWidget(b)
        c.addStretch();self.status=QLabel("Ready");c.addWidget(self.status);v.addLayout(c)
        self.tab=QTableWidget(0,10);self.tab.setHorizontalHeaderLabels(["Coin","BUY","SELL","Buy price","Sell price","Gross %","Net %","Qty","Notional","Est. profit"]);self.tab.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch);v.addWidget(self.tab)
        g=QGroupBox("USDT Spot Balance");bl=QHBoxLayout(g);self.bb=QLabel("BYBIT: —");self.oo=QLabel("OKX: —");bl.addWidget(self.bb);bl.addWidget(self.oo);v.addWidget(g)
        self.timer=QTimer();self.timer.timeout.connect(self.scan)
    def refresh_balances(self):
        for n,l in [("bybit",self.bb),("okx",self.oo)]:
            try:
                e=create_exchange(n);u=e.fetch_balance().get("USDT",{}).get("free",0);l.setText(f"{n.upper()}: {f(u):,.2f} USDT")
            except:l.setText(f"{n.upper()}: error")
    def toggle(self):
        self.running=not self.running
        if self.running:self.start.setText("■ Stop scanner");self.scan();self.timer.start(self.iv.value()*1000)
        else:self.start.setText("▶ Start scanner");self.timer.stop();self.status.setText("Stopped")
    def scan(self):
        if self.th and self.th.isRunning():return
        self.status.setText("Connecting to Bybit + OKX market data…");self.start.setEnabled(False)
        self.th=QThread();self.wk=Worker(self.fb.value()/100,self.fo.value()/100,self.cap.value(),self.mn.value());self.wk.moveToThread(self.th)
        self.th.started.connect(self.wk.run);self.wk.ok.connect(self.done);self.wk.err.connect(self.failed);self.wk.ok.connect(self.th.quit);self.wk.err.connect(self.th.quit);self.th.finished.connect(lambda:self.start.setEnabled(True));self.th.finished.connect(self.clear);self.th.start()
    def clear(self):self.wk=None;self.th=None
    def done(self,rows,stats):
        self.tab.setRowCount(len(rows))
        for i,r in enumerate(rows):
            vals=[r[0],r[1],r[2],f"{r[3]:,.8g}",f"{r[4]:,.8g}",f"{r[5]:.4f}%",f"{r[6]:.4f}%",f"{r[7]:,.8g}",f"{r[8]:,.2f}",f"${r[9]:,.2f}"]
            for j,x in enumerate(vals):self.tab.setItem(i,j,QTableWidgetItem(x))
        self.status.setText(f"OK: Bybit {stats[0]} | OKX {stats[1]} | common pairs {stats[2]} | opportunities {len(rows)}")
    def failed(self,msg):
        self.status.setText("Scanner error");QMessageBox.critical(self,"Scanner error","Не удалось получить рыночные данные.\\n\\n"+msg)
    def closeEvent(self,e):self.timer.stop();e.accept()

app=QApplication(sys.argv);win=App();win.show();sys.exit(app.exec())
