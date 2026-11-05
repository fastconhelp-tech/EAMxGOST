from flask import Flask, render_template, request, jsonify, redirect, url_for, session, render_template_string
import sqlite3
import requests
import re
import datetime
import urllib.parse
from functools import wraps
import threading

app = Flask(__name__)
app.secret_key = "rahasia_earnapp_super_aman_123"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin"

DB_FILE = "/root/earnapp-web/earnapp.db"
GOST_API_URL = "http://127.0.0.1:5001/api/failover" # Kabel penghubung ke GOST TUI

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

def natural_key(text):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(text))]

def get_headers(cookie):
    csrf_match = re.search(r'xsrf-token=([^;]+)', cookie)
    csrf_token = urllib.parse.unquote(csrf_match.group(1)) if csrf_match else ""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://earnapp.com",
        "Referer": "https://earnapp.com/dashboard/me/passive-income",
        "xsrf-token": csrf_token,
        "Cookie": cookie
    }

# ==========================================
# AUTO-FAILOVER TRIGGER
# ==========================================
def trigger_gost_failover(node_name, detected_ip):
    """Fungsi penembak jitu: Mengirim sinyal ke GOST API jika node terblokir"""
    try:
        # Ekstrak nomor port dari nama node (misal W4 -> port 1084, W10 -> port 1090)
        # Asumsi pola nama: W + urutan (W1=1081, W2=1082... W10=1090)
        match = re.search(r'W(\d+)', str(node_name), re.IGNORECASE)
        if match:
            idx = int(match.group(1))
            target_port = 1080 + idx # Kalkulasi port dasar 1080 + urutan
            
            payload = {"port": target_port}
            res = requests.post(GOST_API_URL, json=payload, timeout=5)
            
            if res.status_code == 200:
                print(f"[AUTO-HEAL] Node {node_name} (Port {target_port}) Blocked. GOST Failover dipicu! Status: {res.json().get('message', 'OK')}")
            else:
                print(f"[AUTO-HEAL] Gagal memicu GOST untuk Node {node_name}. HTTP {res.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"[AUTO-HEAL] GOST API tidak merespon: {e}. Pastikan GOST TUI berjalan.")
    except Exception as e:
        print(f"[AUTO-HEAL] Error saat memicu failover: {e}")

# ==========================================
# PEMINDAI MENDALAM STATUS NODE (DEEP SCANNER)
# ==========================================
BLOCK_KEYWORDS = [
    "blocked", "ip_blocked", "banned", "vpn", "low quality", "low_quality",
    "not earning", "not_earning", "proxy", "datacenter", "hosting",
    "blacklisted", "abuse", "restricted", "suspended"
]

ERROR_KEYS = {
    "error", "error_reason", "err", "reason", "ban_reason", "block_reason",
    "message", "msg", "problem", "warning", "alert", "status_reason"
}

BOOL_BLOCK_KEYS = {
    "ip_blocked", "blocked", "is_blocked", "banned", "is_banned",
    "vpn", "is_vpn", "proxy", "is_proxy", "low_quality"
}

def analyze_device_status(dev_dict, raw_st):
    reasons = []
    is_blocked = False
    is_online = False
    detected_ip = ""

    def scan_obj(obj, ignore_keys=()):
        nonlocal is_blocked, is_online, detected_ip
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_low = str(k).lower()
                if k_low in ignore_keys or v is None:
                    continue

                if k_low in ("ip", "detected_ip", "current_ip", "remote_ip") and isinstance(v, str) and v.strip():
                    detected_ip = v.strip()

                if k_low in BOOL_BLOCK_KEYS:
                    if v is True or str(v).lower() in ("true", "1", "yes"):
                        is_blocked = True
                        reasons.append(k_low.replace("_", " ").title())
                    continue

                if k_low in ("online", "is_online") and (v is True or str(v).lower() == "true"):
                    is_online = True

                if k_low in ERROR_KEYS:
                    if isinstance(v, str) and v.strip() and v.strip().lower() not in ("none", "null", "false", "0", "ok"):
                        is_blocked = True
                        reasons.append(v.strip())
                    elif isinstance(v, (dict, list)):
                        scan_obj(v)
                    continue

                if isinstance(v, str):
                    v_low = v.strip().lower()
                    if any(kw in v_low for kw in BLOCK_KEYWORDS):
                        is_blocked = True
                        reasons.append(v.strip())
                    elif k_low in ("status", "state", "mode") and ("online" in v_low or v_low.isdigit()):
                        is_online = True
                elif isinstance(v, (dict, list)):
                    scan_obj(v)

        elif isinstance(obj, list):
            for item in obj:
                scan_obj(item, ignore_keys)
        elif isinstance(obj, str):
            v_low = obj.strip().lower()
            if any(kw in v_low for kw in BLOCK_KEYWORDS):
                is_blocked = True
                reasons.append(obj.strip())
            elif "online" in v_low or v_low.isdigit():
                is_online = True
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            if obj > 0:
                is_online = True

    scan_obj(raw_st)
    scan_obj(dev_dict, ignore_keys=(
        "title", "name", "uuid", "appid", "ips", "country", "rate",
        "bw", "total_bw", "redeem_bw", "earned", "earned_total",
        "uptime", "total_uptime", "online_time", "total_online_time"
    ))

    if is_blocked:
        clean_reasons = []
        for r in reasons:
            if r not in clean_reasons: clean_reasons.append(r)
        reason_text = " | ".join(clean_reasons) if clean_reasons else "IP Blocked / VPN"
        if detected_ip and detected_ip not in reason_text:
            reason_text = f"{reason_text} ({detected_ip})"
        
        # JIKA NODE BLOCKED, TEMBAK GOST SECARA ASYNCHRONOUS AGAR DASHBOARD TIDAK LAG
        node_title = dev_dict.get('title', dev_dict.get('name', ''))
        if node_title:
            threading.Thread(target=trigger_gost_failover, args=(node_title, detected_ip), daemon=True).start()
            
        return "blocked", reason_text, detected_ip
    elif is_online:
        return "online", "", detected_ip
    else:
        return "offline", "", detected_ip

# ==========================================
# SISTEM LOGIN & PROTEKSI
# ==========================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('logged_in') is not True:
            if request.path.startswith('/api/'):
                return jsonify({"success": False, "message": "Akses Ditolak. Silakan Login."}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('username') == ADMIN_USERNAME and request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            error = "Username atau Password salah! Akses ditolak."
    
    login_html = """
    <!DOCTYPE html>
    <html lang="id" data-bs-theme="dark">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Login - EarnApp Multi-Account Monitoring</title>
        <link rel="icon" href="https://i.ibb.co.com/wZtz8N8D/EAIcon.png" type="image/png">
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>body { background-color: #11111b; color: #cdd6f4; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }</style>
    </head>
    <body>
        <div class="card p-4 shadow-lg" style="background-color: #1e1e2e; border: 1px solid #313244; width: 100%; max-width: 380px; border-radius: 16px;">
            <div class="text-center mb-4">
                <img src="https://i.ibb.co.com/wZtz8N8D/EAIcon.png" alt="Logo" style="width: 60px; height: 60px; margin-bottom: 10px;">
                <h4 class="fw-bold text-light">Secure Access</h4>
                <p class="text-secondary small mb-0">Masukkan kredensial administrator</p>
            </div>
            {% if error %}<div class="alert alert-danger p-2 small text-center mb-3">{{ error }}</div>{% endif %}
            <form method="POST">
                <input type="text" name="username" class="form-control mb-2 text-center" style="background-color: #181825; border-color: #313244; color: white;" placeholder="Username" required autofocus>
                <input type="password" name="password" class="form-control mb-4 text-center" style="background-color: #181825; border-color: #313244; color: white;" placeholder="Password" required>
                <button type="submit" class="btn btn-primary w-100 fw-bold">Login Dashboard</button>
            </form>
        </div>
    </body>
    </html>
    """
    return render_template_string(login_html, error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# ==========================================
# ROUTE DASHBOARD & API
# ==========================================
@app.route('/')
@login_required
def index():
    with sqlite3.connect(DB_FILE) as conn:
        accounts = conn.execute('SELECT id, name FROM accounts').fetchall()
    return render_template('index.html', accounts=accounts)

@app.route('/add_account', methods=['POST'])
@login_required
def add_account():
    name, cookie = request.form.get('name'), request.form.get('cookie')
    if name and cookie:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute('INSERT INTO accounts (name, cookie) VALUES (?, ?)', (name.strip(), cookie.strip()))
    return redirect(url_for('index'))

@app.route('/delete_account/<int:acc_id>')
@login_required
def delete_account(acc_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('DELETE FROM accounts WHERE id = ?', (acc_id,))
        conn.execute('DELETE FROM history WHERE account_id = ?', (acc_id,))
    return redirect(url_for('index'))

@app.route('/api/raw_check')
@login_required
def api_raw_check():
    with sqlite3.connect(DB_FILE) as conn: accounts = conn.execute('SELECT id, name, cookie FROM accounts').fetchall()
    out = {}
    for acc_id, name, cookie in accounts:
        headers = get_headers(cookie)
        try:
            res_devs = requests.get("https://earnapp.com/dashboard/api/devices?appid=earnapp", headers=headers, timeout=12).json()
            payload_uuids = {"devices": [{"uuid": d['uuid'], "appid": d.get('appid', 'node_earnapp.com')} for d in res_devs if isinstance(d, dict) and 'uuid' in d]}
            res_st = requests.post("https://earnapp.com/dashboard/api/device_statuses?appid=earnapp", headers=headers, json=payload_uuids, timeout=12).json()
            out[name] = {"devices_sample": res_devs[:5] if isinstance(res_devs, list) else res_devs, "device_statuses": res_st}
        except Exception as e: out[name] = {"error": str(e)}
    return jsonify(out)

@app.route('/api/withdraw', methods=['POST'])
@login_required
def api_withdraw():
    data = request.get_json() or {}
    acc_id, email, payment_method = data.get('acc_id'), str(data.get('email', '')).strip(), str(data.get('payment_method', 'paypal.com')).strip()

    if not acc_id or not email: return jsonify({"success": False, "message": "ID Akun dan Email PayPal wajib diisi."}), 400
    with sqlite3.connect(DB_FILE) as conn: row = conn.execute('SELECT name, cookie FROM accounts WHERE id = ?', (acc_id,)).fetchone()
    if not row: return jsonify({"success": False, "message": "Akun tidak ditemukan di database."}), 404

    acc_name, cookie = row
    headers = get_headers(cookie)
    try:
        res_money = requests.get("https://earnapp.com/dashboard/api/money?appid=earnapp", headers=headers, timeout=12).json()
        balance = float(res_money.get('balance', 0) or 0)
        if balance < 2.5: return jsonify({"success": False, "message": f"Saldo akun {acc_name} saat ini ${balance:.2f} (Minimal $2.50)."}), 400
        
        payload = {"to": email, "payment_method": payment_method}
        res_wd = requests.post("https://earnapp.com/dashboard/api/redeem?appid=earnapp", headers=headers, json=payload, timeout=15)
        
        if res_wd.status_code == 200: return jsonify({"success": True, "message": f"Berhasil! Penarikan ${balance:.2f} sedang diproses ke {email}."})
        else: return jsonify({"success": False, "message": "Penarikan ditolak oleh server EarnApp."}), 400
    except Exception as e: return jsonify({"success": False, "message": f"Kesalahan jaringan: {str(e)}"}), 500

@app.route('/api/stats')
@login_required
def api_stats():
    with sqlite3.connect(DB_FILE) as conn:
        accounts = conn.execute('SELECT id, name, cookie FROM accounts').fetchall()
    dashboard_data = {"total_balance": 0, "total_lifetime": 0, "total_online": 0, "total_nodes": 0, "accounts": []}
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")

    for acc in accounts:
        acc_id, name, cookie = acc
        headers = get_headers(cookie)
        try:
            res_money = requests.get("https://earnapp.com/dashboard/api/money?appid=earnapp", headers=headers, timeout=12).json()
            balance = float(res_money.get('balance', 0) or 0)
            lifetime = float(res_money.get('earnings_total', res_money.get('total_earnings', 0)) or 0)
            
            dashboard_data["total_balance"] += balance
            dashboard_data["total_lifetime"] += lifetime
            
            with sqlite3.connect(DB_FILE) as conn_hist:
                conn_hist.execute('INSERT OR REPLACE INTO history (account_id, date, balance, lifetime) VALUES (?, ?, ?, ?)', (acc_id, current_date, balance, lifetime))
                hist_rows = conn_hist.execute('SELECT date, balance FROM history WHERE account_id = ? ORDER BY date ASC LIMIT 30', (acc_id,)).fetchall()
            
            chart_history = {"labels": [r[0][-5:] for r in hist_rows], "values": [r[1] for r in hist_rows]}

            res_devs = requests.get("https://earnapp.com/dashboard/api/devices?appid=earnapp", headers=headers, timeout=12).json()
            if not isinstance(res_devs, list): res_devs = []
                
            payload_uuids = {
                "devices": [
                    {"uuid": d['uuid'], "appid": d.get('appid', 'node_earnapp.com')}
                    for d in res_devs if isinstance(d, dict) and 'uuid' in d
                ]
            }
            status_map = {}
            if payload_uuids["devices"]:
                res_st_req = requests.post("https://earnapp.com/dashboard/api/device_statuses?appid=earnapp", headers=headers, json=payload_uuids, timeout=12)
                if res_st_req.status_code == 200:
                    res_status = res_st_req.json()
                    if isinstance(res_status, dict):
                        if "statuses" in res_status and isinstance(res_status["statuses"], dict):
                            status_map = res_status["statuses"]
                        elif "devices" in res_status and isinstance(res_status["devices"], dict):
                            status_map = res_status["devices"]
                        else: status_map = res_status
                    elif isinstance(res_status, list):
                        for item in res_status:
                            if isinstance(item, dict) and "uuid" in item:
                                status_map[item["uuid"]] = item
            
            devices_info, acc_uptime_ms = [], 0
            for d in res_devs:
                if not isinstance(d, dict): continue
                uuid = d.get('uuid')
                raw_st = status_map.get(uuid, {})
                
                st, error_reason, detected_ip = analyze_device_status(d, raw_st)

                raw_ips = d.get('ips', d.get('ip', []))
                clean_ips = [str(ip).strip() for ip in raw_ips if str(ip).strip()] if isinstance(raw_ips, list) else [ip.strip().strip("'\"") for ip in str(raw_ips).split(',') if ip.strip()]
                if detected_ip and detected_ip not in clean_ips:
                    clean_ips.append(f"{detected_ip} (Detected)")

                uptime_raw = int(d.get('uptime', d.get('online_time', 0)) or 0)
                tot_uptime_raw = int(d.get('total_uptime', d.get('total_online_time', 0)) or 0)
                acc_uptime_ms += uptime_raw

                devices_info.append({
                    "name": str(d.get('title', d.get('name', 'Unknown'))),
                    "earned": float(d.get('earned', d.get('amount', 0)) or 0),
                    "earned_total": float(d.get('earned_total', d.get('total_earned', 0)) or 0),
                    "uptime": format_waktu(uptime_raw),
                    "total_uptime": format_waktu(tot_uptime_raw),
                    "uptime_raw": uptime_raw,
                    "country": str(d.get('country', 'UN')).upper(),
                    "status": st,
                    "error_reason": error_reason,
                    "ips": clean_ips
                })

            devices_info.sort(key=lambda x: natural_key(x['name']))

            chart_global, chart_top = {"labels": [], "values": []}, {"labels": [], "values": [], "date": "-"}
            try:
                res_usage = requests.get("https://earnapp.com/dashboard/api/usage?appid=earnapp&step=daily", headers=headers, timeout=12)
                if res_usage.status_code == 200:
                    usage_data = res_usage.json()
                    if isinstance(usage_data, list) and len(usage_data) > 0:
                        agregasi_harian, all_dates = {}, set()
                        for p in usage_data:
                            if isinstance(p, dict) and "data" in p:
                                for tgl, durasi in p.get("data", {}).items():
                                    all_dates.add(tgl); val = int(durasi) if durasi is not None else 0
                                    agregasi_harian[tgl] = agregasi_harian.get(tgl, 0) + val
                        
                        if agregasi_harian:
                            tgl_tersortir = sorted(agregasi_harian.keys())[-7:]
                            chart_global["labels"] = [t[5:] for t in tgl_tersortir]
                            chart_global["values"] = [round(agregasi_harian[t] / 86400000, 2) for t in tgl_tersortir]
                        
                        if all_dates:
                            latest_date = sorted(list(all_dates))[-1]; chart_top["date"] = latest_date; node_daily = []
                            for p in usage_data:
                                if isinstance(p, dict):
                                    nm = str(p.get("title", p.get("name", "Unknown"))); raw_pakai = p.get("data", {}).get(latest_date, 0) if isinstance(p.get("data"), dict) else 0
                                    pakai = int(raw_pakai) if raw_pakai is not None else 0; node_daily.append((nm, pakai))
                            node_daily.sort(key=lambda x: x[1], reverse=True); top_10 = node_daily[:10]
                            chart_top["labels"] = [n[0][:12] for n in top_10]; chart_top["values"] = [round(n[1] / 3600000, 1) for n in top_10]
            except Exception: pass

            active_count = sum(1 for x in devices_info if x['status'] == 'online')
            blocked_count = sum(1 for x in devices_info if x['status'] == 'blocked')
            dashboard_data["total_online"] += active_count
            dashboard_data["total_nodes"] += len(devices_info)
            dashboard_data["accounts"].append({
                "id": acc_id, "name": name, "balance": balance, "lifetime": lifetime,
                "total_usage": format_waktu(acc_uptime_ms), "devices": devices_info,
                "active_nodes": active_count, "blocked_nodes": blocked_count,
                "total_nodes": len(devices_info), "chart_global": chart_global,
                "chart_top": chart_top, "chart_history": chart_history
            })
        except Exception as e:
            dashboard_data["accounts"].append({"id": acc_id, "name": name, "error": str(e)})

    return jsonify(dashboard_data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
