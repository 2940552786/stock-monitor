"""后台推送守护进程：定时检测所有用户的信号并推送微信"""
import json, time, requests, os, hashlib, re

UA = 'Mozilla/5.0'
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'users.json')
PUSHED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pushed.json')

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def load_pushed():
    if os.path.exists(PUSHED_FILE):
        with open(PUSHED_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if d.get('_date') == time.strftime('%Y%m%d'):
            return d
    return {'_date': time.strftime('%Y%m%d'), 'keys': []}

def save_pushed(data):
    with open(PUSHED_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)

def push_wechat(webhook, content):
    try:
        requests.post(webhook, json={'msgtype': 'markdown', 'markdown': {'content': content}}, timeout=5)
    except:
        pass

def get_trend(code, market):
    tcode = f"{market}{code}"
    url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={tcode}"
    r = requests.get(url, headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=8)
    j = json.loads(r.text.replace('min_data=', '').strip().rstrip(';'))
    if j.get('code') != 0 or not j.get('data'):
        return None
    s = j['data'].get(tcode)
    if not s: return None
    inner = s.get('data') or s
    mins = inner.get('data', [])
    qt_arr = (s.get('qt', {}).get(tcode, []))
    pre_close = float(qt_arr[4]) if len(qt_arr) > 4 else 0
    trends = []
    pv, pa = 0, 0
    for m in mins:
        p = m.split(' ')
        if len(p) >= 3:
            cv, ca = int(float(p[2])), float(p[3]) if len(p) >= 4 else 0
            vv, aa = cv - pv, ca - pa
            if vv < 0: vv = 0
            if aa < 0: aa = 0
            pv, pa = cv, ca
            trends.append({'time': p[0], 'price': float(p[1]), 'volume': vv, 'amount': aa})
    return {'code': code, 'preClose': pre_close, 'trends': trends}

def get_name(code, market):
    try:
        sc = f"{market}{code}"
        r = requests.get(f'http://hq.sinajs.cn/list={sc}', headers={'User-Agent': UA, 'Referer': 'https://finance.sina.com.cn/'}, timeout=3)
        m = re.search(r'"([^"]*)"', r.text)
        if m:
            f = m.group(1).split(',')
            return f[0] if f[0] else code
    except:
        pass
    return code

def detect_events(trends):
    prices = [t['price'] for t in trends]
    n = len(prices)
    events = []
    def chg(i, m): return 0 if i < m else (prices[i] - prices[i-m]) / prices[i-m] * 100
    lsu, lsd = 0, 0
    for i in range(1, n):
        c3 = chg(i, 3)
        su = 3 if c3 >= 1.8 else (2 if c3 >= 1.2 else (1 if c3 >= 0.8 else 0))
        sd = 3 if c3 <= -1.8 else (2 if c3 <= -1.2 else (1 if c3 <= -0.8 else 0))
        if su > lsu:
            lb = ['', '轻度急拉', '中度急拉', '强烈急拉'][su]
            cl = ['', '#ff9933', '#ff7733', '#ff3333'][su]
            sv = ['', 'low', 'medium', 'high'][su]
            events.append({'t': 'surge', 'l': lb, 's': sv, 'p': prices[i], 'd': f'+{c3:.1f}%/3min', 'c': cl, 'time': trends[i]['time']})
        lsu = su
        if sd > lsd:
            lb = ['', '轻度急跌', '中度急跌', '强烈急跌'][sd]
            cl = ['', '#009944', '#00aa44', '#00cc44'][sd]
            sv = ['', 'low', 'medium', 'high'][sd]
            events.append({'t': 'plunge', 'l': lb, 's': sv, 'p': prices[i], 'd': f'{c3:.1f}%/3min', 'c': cl, 'time': trends[i]['time']})
        lsd = sd
        if i >= 5:
            hi = max(prices[:i]); lo = min(prices[:i])
            if prices[i] > hi: events.append({'t': 'new_high', 'l': '新高', 's': 'low', 'p': prices[i], 'd': f'¥{prices[i]:.2f}', 'c': '#ff4444', 'time': trends[i]['time']})
            if prices[i] < lo: events.append({'t': 'new_low', 'l': '新低', 's': 'low', 'p': prices[i], 'd': f'¥{prices[i]:.2f}', 'c': '#00cc66', 'time': trends[i]['time']})
    return events

def main_loop():
    print(f"[推送守护] 启动 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    while True:
        try:
            users = load_users()
            pushed = load_pushed()
            pushed_keys = set(pushed.get('keys', []))
            now = time.localtime()
            now_min = now.tm_hour * 60 + now.tm_min
            # 只在交易时段跑（9:25-15:05）
            if not ((9*60+25 <= now_min <= 11*60+35) or (13*60-5 <= now_min <= 15*60+5)):
                time.sleep(30)
                continue

            ICONS = {'surge': '🔥', 'plunge': '💧', 'new_high': '🚀', 'new_low': '💀'}
            for uname, udata in users.items():
                wh = udata.get('webhook', '')
                if not wh: continue
                wl = udata.get('watchlist', [])
                for s in wl:
                    code = s.get('code', '') if isinstance(s, dict) else str(s)
                    market = 'sh' if code.startswith(('6', '9')) else 'sz'
                    try:
                        td = get_trend(code, market)
                        if not td or not td['trends']: continue
                        ev = detect_events(td['trends'])
                        name = get_name(code, market)
                        for e in ev[-3:]:  # 只看最近3个事件
                            ek = f"{code}|{e['t']}|{e['time']}"
                            if ek in pushed_keys: continue
                            hm = e['time'][:5] if len(e['time']) >= 5 else e['time']
                            sig_min = int(hm[:2]) * 60 + int(hm[3:])
                            if abs(now_min - sig_min) > 5: continue
                            pushed_keys.add(ek)
                            icon = ICONS.get(e['t'], '📌')
                            color = e.get('c', '#ff4444').replace('#', '')
                            sev = {'high': '🔥🔥🔥', 'medium': '🔥🔥', 'low': '🔥'}.get(e.get('s', ''), '')
                            md = f"**{hm}**  {name}({code})  ¥{e['p']}\n{icon} **<font color=\"{color}\">{e['l']}</font>** {sev}  {e['d']}"
                            push_wechat(wh, md)
                    except: continue
            # 清理旧 key（只保留今天的）
            pushed['keys'] = [k for k in pushed_keys if k.split('|')[2][:5] >= time.strftime('%H:%M')]
            save_pushed(pushed)
        except Exception as e:
            print(f"[推送守护] 出错: {e}")
        time.sleep(8)

if __name__ == '__main__':
    main_loop()
