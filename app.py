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
csrf_token = urllib.parse.unquote(csrf_match.group(1)) if csrf_match else ""
return {
    "User-Agent": "Mozilla/5.0", "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://earnapp.com", "Referer": "https://earnapp.com/dashboard/me/passive-income",
    "xsrf-token": csrf_token, "Cookie": cookie
}

def login_required(f):
@wraps(f)
def decorated_function(*args, **kwargs):
    if session.get('logged_in') is not True:
        if request.path.startswith('/api/'): return jsonify({"success": False, "message": "Akses Ditolak."}), 401
        return redirect(url_for('login'))
    return f(*args, **kwargs)
return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
error = None
if request.method == 'POST':
    if request.form.get('username') == ADMIN_USERNAME and request.form.get('password') == ADMIN_PASSWORD:
        session['logged_in'] = True; return redirect(url_for('index'))
    else: error = "Username atau Password salah!"

login_html = """
<!DOCTYPE html><html lang="id" data-bs-theme="dark"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Login - Multi-Account Monitoring</title><link rel="icon" href="https://i.ibb.co.com/wZtz8N8D/EAIcon.png" type="image/png"><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet"><style>body { background-color: #11111b; color: #cdd6f4; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }</style></head>
<body><div class="card p-4 shadow-lg" style="background-color: #1e1e2e; border: 1px solid #313244; width: 100%; max-width: 380px; border-radius: 16px;"><div class="text-center mb-4"><img src="https://i.ibb.co.com/wZtz8N8D/EAIcon.png" alt="Logo" style="width: 60px; height: 60px; margin-bottom: 10px;"><h4 class="fw-bold text-light">Secure Access</h4></div>{% if error %}<div class="alert alert-danger p-2 small text-center mb-3">{{ error }}</div>{% endif %}
<form method="POST"><input type="text" name="username" class="form-control mb-2 text-center" style="background-color: #181825; border-color: #313244; color: white;" placeholder="Username" required autofocus><input type="password" name="password" class="form-control mb-4 text-center" style="background-color: #181825; border-color: #313244; color: white;" placeholder="Password" required><button type="submit" class="btn btn-primary w-100 fw-bold">Login Dashboard</button></form></div></body></html>
"""
return render_template_string(login_html, error=error)

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
    if match: requests.post(f"{GOST_API_URL}/api/failover", json={"port": 1080 + int(match.group(1))}, timeout=5)
except: pass

@app.route('/api/stats')
@login_required
def api_stats():
with sqlite3.connect(DB_FILE) as conn: accounts = conn.execute('SELECT id, name, cookie FROM accounts').fetchall()
dash = {"total_balance": 0, "total_lifetime": 0, "total_online": 0, "total_nodes": 0, "accounts": []}
curr = datetime.datetime.now().strftime("%Y-%m-%d")

BLOCK_KEYWORDS = ["blocked", "ip_blocked", "banned", "vpn", "low quality", "low_quality", "not earning", "not_earning", "proxy", "datacenter", "hosting", "blacklisted", "abuse", "restricted", "suspended"]
ERROR_KEYS = {"error", "error_reason", "err", "reason", "ban_reason", "block_reason", "message", "msg", "problem", "warning", "alert", "status_reason"}
BOOL_BLOCK_KEYS = {"ip_blocked", "blocked", "is_blocked", "banned", "is_banned", "vpn", "is_vpn", "proxy", "is_proxy", "low_quality"}

def fetch_account(acc):
    acc_id, acc_name, acc_cookie = acc
    try:
        head = get_headers(acc_cookie)
        
        # CEK KONEKSI & CLOUDFLARE DENGAN TIMEOUT KETAT 8 DETIK
        rm_req = requests.get("https://earnapp.com/dashboard/api/money?appid=earnapp", headers=head, timeout=8)
        if rm_req.status_code != 200:
            raise Exception(f"EarnApp HTTP {rm_req.status_code}. (Cookie kedaluwarsa atau IP Cloudflare Blocked).")
        
        rm = rm_req.json()
        bal = float(rm.get('balance', 0) or 0)
        life = float(rm.get('earnings_total', rm.get('total_earnings', 0)) or 0)
        
        try:
            with sqlite3.connect(DB_FILE, timeout=5) as c_hist:
                c_hist.execute('INSERT OR REPLACE INTO history (account_id, date, balance, lifetime) VALUES (?, ?, ?, ?)', (acc_id, curr, bal, life))
        except: pass
            
        rd_req = requests.get("https://earnapp.com/dashboard/api/devices?appid=earnapp", headers=head, timeout=8)
        rd = rd_req.json() if rd_req.status_code == 200 else []
        if not isinstance(rd, list): rd = []
        
        uuids = {"devices": [{"uuid": d['uuid'], "appid": d.get('appid', 'node_earnapp.com')} for d in rd if isinstance(d, dict) and 'uuid' in d]}
        st_map = {}
        if uuids["devices"]:
            try:
                rs = requests.post("https://earnapp.com/dashboard/api/device_statuses?appid=earnapp", headers=head, json=uuids, timeout=8)
                if rs.status_code == 200:
                    r_json = rs.json()
                    if isinstance(r_json, dict): st_map = r_json.get("statuses", r_json.get("devices", r_json))
                    elif isinstance(r_json, list): st_map = {i["uuid"]: i for i in r_json if isinstance(i, dict) and "uuid" in i}
            except: pass

        dev_info, up_ms = [], 0
        for d in rd:
            if not isinstance(d, dict): continue
            try:
                raw_st = st_map.get(d.get('uuid'), {})
                reasons, is_blk, is_on, d_ip = [], False, False, ""
                
                def scan_obj(obj, ignore_keys=()):
                    nonlocal is_blk, is_on, d_ip
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            kl = str(k).lower()
                            if kl in ignore_keys or v is None: continue
                            if kl in ("ip", "detected_ip", "current_ip") and isinstance(v, str) and v.strip(): d_ip = v.strip()
                            if kl in BOOL_BLOCK_KEYS:
                                if v is True or str(v).lower() in ("true", "1", "yes"): is_blk = True; reasons.append(kl.replace("_", " ").title())
                                continue
                            if kl in ("online", "is_online") and (v is True or str(v).lower() == "true"): is_on = True
                            if kl in ERROR_KEYS:
                                if isinstance(v, str) and v.strip() and v.strip().lower() not in ("none", "null", "false", "0", "ok"): is_blk = True; reasons.append(v.strip())
                                elif isinstance(v, (dict, list)): scan_obj(v)
                                continue
                            if isinstance(v, str):
                                vl = v.strip().lower()
                                if any(kw in vl for kw in BLOCK_KEYWORDS): is_blk = True; reasons.append(v.strip())
                                elif kl in ("status", "state") and ("online" in vl or vl.isdigit()): is_on = True
                            elif isinstance(v, (dict, list)): scan_obj(v)
                    elif isinstance(obj, list):
                        for item in obj: scan_obj(item, ignore_keys)
                    elif isinstance(obj, str):
                        vl = obj.strip().lower()
                        if any(kw in vl for kw in BLOCK_KEYWORDS): is_blk = True; reasons.append(obj.strip())
                        elif "online" in vl or vl.isdigit(): is_on = True

                scan_obj(raw_st)
                scan_obj(d, ignore_keys=("title", "name", "uuid", "appid", "ips", "country", "rate", "bw", "total_bw", "redeem_bw", "earned", "earned_total", "uptime", "total_uptime", "online_time", "total_online_time"))

                if is_blk:
                    creasons = []
                    for r in reasons:
                        if r not in creasons: creasons.append(r)
                    rtxt = " | ".join(creasons) if creasons else "IP Blocked"
                    if d_ip and d_ip not in rtxt: rtxt = f"{rtxt} ({d_ip})"
                    st, err = "blocked", rtxt
                    nm = d.get('title', d.get('name', ''))
                    if nm: threading.Thread(target=trigger_gost_failover, args=(nm, d_ip), daemon=True).start()
                elif is_on: st, err = "online", ""
                else: st, err = "offline", ""

                r_ips = d.get('ips', d.get('ip', []))
                c_ips = [str(ip).strip() for ip in r_ips if str(ip).strip()] if isinstance(r_ips, list) else [ip.strip().strip("'\"") for ip in str(r_ips).split(',') if ip.strip()]
                if d_ip and d_ip not in c_ips: c_ips.append(f"{d_ip} (Detected)")
                u_raw = int(d.get('uptime', d.get('online_time', 0)) or 0)
                up_ms += u_raw
                dev_info.append({"name": str(d.get('title', d.get('name', 'Unknown'))), "earned": float(d.get('earned', d.get('amount', 0)) or 0), "uptime": format_waktu(u_raw), "country": str(d.get('country', 'UN')).upper(), "status": st, "error_reason": err, "ips": c_ips})
            except: pass
        
        dev_info.sort(key=lambda x: natural_key(x['name']))
        ac = sum(1 for x in dev_info if x['status'] == 'online')
        bc = sum(1 for x in dev_info if x['status'] == 'blocked')
        return {"success": True, "data": {"id": acc_id, "name": acc_name, "balance": bal, "lifetime": life, "total_usage": format_waktu(up_ms), "devices": dev_info, "active_nodes": ac, "blocked_nodes": bc, "total_nodes": len(dev_info)}}
    
    except Exception as e: 
        return {"success": False, "data": {"id": acc_id, "name": acc_name, "error": f"API EarnApp Error: {str(e)}"}}

# JALANKAN SEMUA AKUN SECARA BERSAMAAN (PARALLEL MULTITHREADING)
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
