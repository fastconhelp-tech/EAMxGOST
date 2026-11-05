from flask import Flask, render_template, request, jsonify, redirect, url_for, session, render_template_string
import sqlite3, requests, re, datetime, urllib.parse, os, json, threading, time
from functools import wraps

app = Flask(__name__)
app.secret_key = "rahasia_earnapp_super_aman_123"
ADMIN_USERNAME, ADMIN_PASSWORD = "admin", "admin"
DB_FILE = "/root/earnapp-web/earnapp.db"
GOST_API_URL = "http://127.0.0.1:5001"
GOST_DIR = "/opt/gost-proxy"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, cookie TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS history (account_id INTEGER, date TEXT, balance REAL, lifetime REAL, PRIMARY KEY (account_id, date))')
init_db()

def format_waktu(raw_val):
    try:
        if raw_val is None: return "0m"
        s = int(raw_val) // 1000; d = s // 86400; h = (s % 86400) // 3600; m = (s % 3600) // 60
        if d > 0: return f"{d}d {h}h"
        if h > 0: return f"{h}h {m}m"
        return f"{m}m"
    except: return "0m"

# INI FUNGSI YANG HILANG DAN MEMBUAT ERROR
def natural_key(text): return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(text))]

def get_headers(cookie):
    csrf_match = re.search(r'xsrf-token=([^;]+)', cookie)
    return {
        "User-Agent": "Mozilla/5.0", "Accept": "application/json",
        "Referer": "https://earnapp.com/dashboard/me/passive-income",
        "xsrf-token": urllib.parse.unquote(csrf_match.group(1)) if csrf_match else "", 
        "Cookie": cookie
    }

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('logged_in') is not True:
            if request.path.startswith('/api/'): return jsonify({"success": False, "message": "Ditolak."}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username') == ADMIN_USERNAME and request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True; return redirect(url_for('index'))
    return render_template_string("""<!DOCTYPE html><html data-bs-theme="dark"><head><title>Login</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet"></head><body class="d-flex align-items-center justify-content-center vh-100 bg-dark"><form method="POST" class="card p-4 bg-secondary"><input type="text" name="username" class="form-control mb-2" placeholder="User"><input type="password" name="password" class="form-control mb-3" placeholder="Pass"><button class="btn btn-primary">Login</button></form></body></html>""")

@app.route('/logout')
def logout(): session.pop('logged_in', None); return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    with sqlite3.connect(DB_FILE) as conn: accounts = conn.execute('SELECT id, name FROM accounts').fetchall()
    return render_template('index.html', accounts=accounts)

@app.route('/add_account', methods=['POST'])
@login_required
def add_account():
    name, cookie = request.form.get('name'), request.form.get('cookie')
    if name and cookie:
        with sqlite3.connect(DB_FILE) as conn: conn.execute('INSERT INTO accounts (name, cookie) VALUES (?, ?)', (name.strip(), cookie.strip()))
    return redirect(url_for('index'))

@app.route('/delete_account/<int:acc_id>')
@login_required
def delete_account(acc_id):
    with sqlite3.connect(DB_FILE) as conn: conn.execute('DELETE FROM accounts WHERE id = ?', (acc_id,)); conn.execute('DELETE FROM history WHERE account_id = ?', (acc_id,))
    return redirect(url_for('index'))

def trigger_gost_failover(node_name, detected_ip):
    try:
        match = re.search(r'W(\d+)', str(node_name), re.IGNORECASE)
        if match: requests.post(f"{GOST_API_URL}/api/failover", json={"port": 1080 + int(match.group(1))}, timeout=3)
    except: pass

@app.route('/api/stats')
@login_required
def api_stats():
    dash = {"total_balance": 0, "total_lifetime": 0, "total_online": 0, "total_nodes": 0, "accounts": []}
    try:
        with sqlite3.connect(DB_FILE) as conn: accounts = conn.execute('SELECT id, name, cookie FROM accounts').fetchall()
    except Exception as e: return jsonify({"success": False, "message": str(e)})
    
    curr = datetime.datetime.now().strftime("%Y-%m-%d")

    for acc in accounts:
        acc_id, acc_name, acc_cookie = acc
        try:
            head = get_headers(acc_cookie)
            
            try:
                rm_req = requests.get("https://earnapp.com/dashboard/api/money?appid=earnapp", headers=head, timeout=6)
                if rm_req.status_code != 200:
                    dash["accounts"].append({"id": acc_id, "name": acc_name, "error": f"HTTP {rm_req.status_code}: Cookie kedaluwarsa atau IP diblokir Cloudflare."})
                    continue
                rm = rm_req.json()
            except Exception as e:
                dash["accounts"].append({"id": acc_id, "name": acc_name, "error": f"Koneksi Timeout (Sistem EarnApp lambat)."})
                continue
                
            bal = float(rm.get('balance', 0) or 0)
            life = float(rm.get('earnings_total', rm.get('total_earnings', 0)) or 0)
            dash["total_balance"] += bal
            dash["total_lifetime"] += life
            
            try:
                with sqlite3.connect(DB_FILE, timeout=3) as c_hist: c_hist.execute('INSERT OR REPLACE INTO history (account_id, date, balance, lifetime) VALUES (?, ?, ?, ?)', (acc_id, curr, bal, life))
            except: pass
                
            try:
                rd_req = requests.get("https://earnapp.com/dashboard/api/devices?appid=earnapp", headers=head, timeout=6)
                rd = rd_req.json() if rd_req.status_code == 200 else []
            except: rd = []
            if not isinstance(rd, list): rd = []

            dev_info, up_ms = [], 0
            for d in rd:
                if not isinstance(d, dict): continue
                
                nm = str(d.get('title', d.get('name', 'Unknown')))
                st_raw = str(d.get('status', d.get('state', 'offline'))).lower()
                c_ips = d.get('ips', d.get('ip', []))
                if not isinstance(c_ips, list): c_ips = [str(c_ips)]
                
                is_on = "online" in st_raw
                is_blk = "banned" in st_raw or "blocked" in st_raw or "restricted" in st_raw
                
                st, err = "offline", ""
                if is_on: st = "online"
                elif is_blk: 
                    st, err = "blocked", "IP Banned/Restricted"
                    threading.Thread(target=trigger_gost_failover, args=(nm, "-"), daemon=True).start()
                
                u_raw = int(d.get('uptime', d.get('online_time', 0)) or 0)
                up_ms += u_raw
                
                dev_info.append({"name": nm, "earned": float(d.get('earned', d.get('amount', 0)) or 0), "uptime": format_waktu(u_raw), "country": str(d.get('country', 'UN')).upper(), "status": st, "error_reason": err, "ips": c_ips})
            
            dev_info.sort(key=lambda x: natural_key(x['name']))
            ac = sum(1 for x in dev_info if x['status'] == 'online')
            bc = sum(1 for x in dev_info if x['status'] == 'blocked')
            
            dash["total_online"] += ac
            dash["total_nodes"] += len(dev_info)
            dash["accounts"].append({"id": acc_id, "name": acc_name, "balance": bal, "lifetime": life, "total_usage": format_waktu(up_ms), "devices": dev_info, "active_nodes": ac, "blocked_nodes": bc, "total_nodes": len(dev_info)})
            
        except Exception as e: 
            dash["accounts"].append({"id": acc_id, "name": acc_name, "error": f"Kesalahan Sistem: {str(e)}"})

    return jsonify(dash)

@app.route('/api/gost/full', methods=['GET'])
@login_required
def api_gost_full(): return jsonify({"success": False, "message": "Gunakan GOST Manager lokal."})

@app.route('/api/gost/proxy_action', methods=['POST'])
@login_required
def api_gost_proxy_action():
    try:
        req = request.get_json(); act = req.get("action")
        if act == "get_pool": return jsonify(requests.get(f"{GOST_API_URL}/api/pool", params={"zone": req.get("zone")}, timeout=5).json())
        elif act == "add_pool": return jsonify(requests.post(f"{GOST_API_URL}/api/pool/add", json=req, timeout=5).json())
        elif act == "del_pool": return jsonify(requests.post(f"{GOST_API_URL}/api/pool/del", json=req, timeout=5).json())
        elif act == "bind": return jsonify(requests.post(f"{GOST_API_URL}/api/bind", json=req, timeout=5).json())
        elif act == "settings": return jsonify(requests.post(f"{GOST_API_URL}/api/settings", json=req, timeout=5).json())
        elif act == "update_zones":
            with open(os.path.join(GOST_DIR, "settings.conf"), "r") as f: lines = f.readlines()
            with open(os.path.join(GOST_DIR, "settings.conf"), "w") as f:
                found = False
                for l in lines:
                    if l.startswith("ZONES="): f.write(f"ZONES={req.get('zones')}\n"); found = True
                    else: f.write(l)
                if not found: f.write(f"ZONES={req.get('zones')}\n")
            return jsonify({"success": True})
    except: return jsonify({"success": False})

@app.route('/api/upload_excel', methods=['POST'])
@login_required
def upload_excel():
    if 'file' not in request.files: return jsonify({"success": False, "message": "Tidak ada file."})
    file = request.files['file']
    zone = request.form.get('zone')
    
    try:
        import pandas as pd
        if file.filename.endswith('.csv'): df = pd.read_csv(file)
        else: df = pd.read_excel(file)
        
        proxies = []
        if 'Proxy Command' in df.columns:
            proxies = df['Proxy Command'].dropna().astype(str).tolist()
        else:
            for col in df.columns:
                for val in df[col].dropna().astype(str):
                    if ':' in val and len(val.split(':')) >= 3: proxies.append(val)
        
        proxies = [p.strip() for p in proxies if p.strip()]
        if not proxies: return jsonify({"success": False, "message": "Kolom tidak valid."})
        
        res = requests.post(f"{GOST_API_URL}/api/pool/add", json={"zone": zone, "proxies": proxies}, timeout=5)
        return jsonify(res.json())
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
