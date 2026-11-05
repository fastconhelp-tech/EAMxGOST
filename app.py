from flask import Flask, render_template, request, jsonify, redirect, url_for, session, render_template_string
import sqlite3, requests, re, datetime, urllib.parse, os, json, threading, time, concurrent.futures
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

def format_waktu_detik(seconds):
    try:
        s = int(seconds); d = s // 86400; h = (s % 86400) // 3600; m = (s % 3600) // 60
        if d > 0: return f"{d}d {h}h"
        if h > 0: return f"{h}h {m}m"
        return f"{m}m"
    except: return "-"

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

    def fetch_account(acc):
        acc_id, acc_name, acc_cookie = acc
        try:
            head = get_headers(acc_cookie)
            
            try:
                rm_req = requests.get("https://earnapp.com/dashboard/api/money?appid=earnapp", headers=head, timeout=6)
                if rm_req.status_code != 200:
                    return {"success": False, "data": {"id": acc_id, "name": acc_name, "error": f"HTTP {rm_req.status_code}: Cookie kedaluwarsa atau IP diblokir Cloudflare."}}
                rm = rm_req.json()
            except Exception as e:
                return {"success": False, "data": {"id": acc_id, "name": acc_name, "error": f"Koneksi Timeout (Sistem EarnApp lambat). {str(e)}"}}
                
            bal = float(rm.get('balance', 0) or 0)
            life = float(rm.get('earnings_total', rm.get('total_earnings', 0)) or 0)
            
            try:
                with sqlite3.connect(DB_FILE, timeout=3) as c_hist: 
                    c_hist.execute('INSERT OR REPLACE INTO history (account_id, date, balance, lifetime) VALUES (?, ?, ?, ?)', (acc_id, curr, bal, life))
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
            
            return {"success": True, "data": {"id": acc_id, "name": acc_name, "balance": bal, "lifetime": life, "total_usage": format_waktu(up_ms), "devices": dev_info, "active_nodes": ac, "blocked_nodes": bc, "total_nodes": len(dev_info)}}
            
        except Exception as e: 
            return {"success": False, "data": {"id": acc_id, "name": acc_name, "error": f"Kesalahan Sistem: {str(e)}"}}

    # MULTITHREADING AGAR LOADING EARNAPP KILAT
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(fetch_account, accounts))
    
    for res in results:
        dash["accounts"].append(res["data"])
        if res["success"]:
            dash["total_balance"] += res["data"].get("balance", 0)
            dash["total_lifetime"] += res["data"].get("lifetime", 0)
            dash["total_online"] += res["data"].get("active_nodes", 0)
            dash["total_nodes"] += res["data"].get("total_nodes", 0)

    return jsonify(dash)

# INI ADALAH FUNGSI YANG HILANG SEBELUMNYA - MEMBACA DATA PROXY GOST
@app.route('/api/gost/full', methods=['GET'])
@login_required
def api_gost_full():
    try:
        def load_j(f):
            try:
                with open(os.path.join(GOST_DIR, f), "r") as fp: return json.load(fp)
            except: return {}
        sts = {}
        try:
            with open(os.path.join(GOST_DIR, "settings.conf"), "r") as f:
                for l in f:
                    if "=" in l: k, v = l.strip().split("=", 1); sts[k] = v
        except: pass

        ipc, lst, p_st = load_j("ip_cache.json"), load_j("live_status.json"), load_j("proxy_state.json")
        bnd = {}
        all_zone_ports = []
        zones_raw = sts.get("ZONES", "1081-1090").split(",")
        for z in zones_raw:
            z = z.strip()
            if "-" in z:
                try:
                    s, e = map(int, z.split("-"))
                    all_zone_ports.extend(range(s, e + 1))
                except: pass
            try:
                with open(os.path.join(GOST_DIR, f"binds_{z}.txt"), "r") as f:
                    for l in f:
                        if "|" in l: p, prx = l.strip().split("|", 1); bnd[int(p)] = prx
            except: pass

        plist = []
        for p in all_zone_ports:
            if p in bnd:
                prx = bnd[p]
                ip = ipc.get(prx, "-") if isinstance(ipc, dict) else "-"
                st = lst.get(prx, "Offline") if isinstance(lst, dict) else "Offline"
                
                s_data = p_st.get(str(p), {}) if isinstance(p_st, dict) else {}
                asn = s_data.get("asn", "-")
                u_sec = s_data.get("usage_time", 0)
                usage_str = format_waktu_detik(u_sec) if u_sec > 0 else "-"
                
                b_time = s_data.get("bind_time", 0)
                uptime_str = "-"
                b_dt = "-"
                exp_dt = "-"
                if b_time > 0:
                    dt_obj = datetime.datetime.fromtimestamp(b_time)
                    b_dt = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
                    exp_dt = (dt_obj + datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                    uptime_str = format_waktu_detik(time.time() - b_time)

                plist.append({
                    "port": str(p), "proxy": str(prx), "ip": str(ip), 
                    "location": "-", "asn": str(asn), "status": str(st),
                    "usage_time": usage_str, "bind_date": b_dt, "exp_date": exp_dt, "uptime": uptime_str
                })
            else:
                plist.append({"port": str(p), "proxy": "", "ip": "-", "location": "-", "asn": "-", "status": "Free", "usage_time": "-", "bind_date": "-", "exp_date": "-", "uptime": "-"})
        
        return jsonify({"success": True, "settings": sts, "proxies": sorted(plist, key=lambda x: int(x["port"]))})
    except Exception as e: return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/gost/proxy_action', methods=['POST'])
@login_required
def api_gost_proxy_action():
    try:
        req = request.get_json()
        act = req.get("action")
        if act == "get_pool": return jsonify(requests.get(f"{GOST_API_URL}/api/pool", params={"zone": req.get("zone")}, timeout=5).json())
        elif act == "add_pool": return jsonify(requests.post(f"{GOST_API_URL}/api/pool/add", json=req, timeout=5).json())
        elif act == "del_pool": return jsonify(requests.post(f"{GOST_API_URL}/api/pool/del", json=req, timeout=5).json())
        elif act == "bind": return jsonify(requests.post(f"{GOST_API_URL}/api/bind", json=req, timeout=5).json())
        elif act == "settings": return jsonify(requests.post(f"{GOST_API_URL}/api/settings", json=req, timeout=5).json())
        elif act == "update_zones":
            new_zones = req.get("zones")
            with open(os.path.join(GOST_DIR, "settings.conf"), "r") as f: lines = f.readlines()
            with open(os.path.join(GOST_DIR, "settings.conf"), "w") as f:
                found = False
                for l in lines:
                    if l.startswith("ZONES="): f.write(f"ZONES={new_zones}\n"); found = True
                    else: f.write(l)
                if not found: f.write(f"ZONES={new_zones}\n")
            return jsonify({"success": True})
    except Exception as e: return jsonify({"success": False, "message": str(e)})
    return jsonify({"success": False})

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
