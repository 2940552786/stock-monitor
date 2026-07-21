"""
A股盘口分时看盘工具 - 后端代理服务
解决前端跨域问题，代理新浪/腾讯API请求，支持集合竞价、盘口委比、筹码分布
"""
import json
import re
import time
import datetime
import hashlib
import secrets
from flask import Flask, request, jsonify, send_from_directory
import requests
import os

app = Flask(__name__)

# ============ 用户系统 ============
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'users.json')
user_tokens = {}  # {token: username}

def _load_tokens():
    global user_tokens
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                users = json.load(f)
            for u, d in users.items():
                t = d.get('_token', '')
                if t: user_tokens[t] = u
            print(f"[认证] 加载了 {len(user_tokens)} 个持久化token")
    except: pass

_load_tokens()

def _load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _save_users(users):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False)

def _hash_pw(pw, salt=''):
    return hashlib.sha256((pw + salt).encode()).hexdigest()

def _get_user_from_request():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    username = user_tokens.get(token)
    if not username:
        return None, None
    users = _load_users()
    return username, users.get(username)

# ============ 交易时段判断 ============
def is_trading_time():
    """检查当前是否在A股连续竞价时段（周一～周五 9:30-11:30, 13:00-15:00）"""
    now = datetime.datetime.now()
    if now.weekday() >= 5:  # 周六、周日
        return False
    t = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= t < 11 * 60 + 30:
        return True
    if 13 * 60 <= t < 15 * 60:
        return True
    return False


def is_auction_time(market='sz'):
    """检查当前是否在集合竞价时段
    早盘: 9:15-9:25 (沪深都有)
    尾盘: 14:57-15:00 (仅深市 sz/0开头/3开头/2开头)
    返回: 'pre' | 'post' | None
    """
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return None
    t = now.hour * 60 + now.minute
    if 9 * 60 + 15 <= t < 9 * 60 + 25:
        return 'pre'
    # 尾盘集合竞价仅深市
    if market and market not in ('sh', 'bj'):
        if 14 * 60 + 57 <= t < 15 * 60:
            return 'post'
    return None

# ============ 通用请求头 ============
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# ============ 缓存 ============
cache = {}
CACHE_TTL = {'quote': 3, 'trend': 30, 'chip': 15}

# 盘口历史持久化
import atexit
import threading
_save_lock = threading.Lock()
OB_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ob_history.json')
orderbook_history = {}  # {code: [{time, imbalance, bid_total, ask_total, bids:[{price,vol}×5], asks:[{price,vol}×5]}, ...]}
MAX_OB_HISTORY = 242

def _load_ob_history():
    """从文件加载盘口历史"""
    global orderbook_history
    try:
        if os.path.exists(OB_HISTORY_FILE):
            with open(OB_HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            saved_date = data.get('_date', '')
            today = time.strftime('%Y%m%d')
            if saved_date == today:
                orderbook_history = {k: v for k, v in data.items() if not k.startswith('_')}
                print(f"[盘口] 加载今日历史 {sum(len(v) for v in orderbook_history.values())} 条快照")
            else:
                print(f"[盘口] 文件日期{saved_date} ≠ 今日{today}，已清空")
    except Exception as e:
        print(f"[盘口] 加载失败: {e}")

def _save_ob_history():
    """保存盘口历史到文件（仅交易时段），原子写入+锁防损坏"""
    if not is_trading_time():
        return
    with _save_lock:
        try:
            data = {'_date': time.strftime('%Y%m%d')}
            data.update(orderbook_history)
            tmp = OB_HISTORY_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, OB_HISTORY_FILE)
        except Exception as e:
            print(f"[盘口] 保存失败: {e}")

_load_ob_history()
atexit.register(_save_ob_history)

# 集合竞价历史持久化
AUCTION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'call_auction.json')
call_auction_history = {}  # {code: [{time, price, volume}, ...]}
MAX_AUCTION_SNAPS = 300  # 最大竞价快照数

def _load_auction_history():
    """从文件加载集合竞价历史"""
    global call_auction_history
    try:
        if os.path.exists(AUCTION_FILE):
            with open(AUCTION_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            saved_date = data.get('_date', '')
            today = time.strftime('%Y%m%d')
            if saved_date == today:
                call_auction_history = {k: v for k, v in data.items() if not k.startswith('_')}
                print(f"[竞价] 加载今日历史 {sum(len(v) for v in call_auction_history.values())} 条快照")
            else:
                print(f"[竞价] 文件日期{saved_date} ≠ 今日{today}，已清空")
    except Exception as e:
        print(f"[竞价] 加载失败: {e}")

def _save_auction_history():
    """保存集合竞价历史到文件，原子写入+锁防损坏"""
    # 竞价数据保存也覆盖竞价+连续交易时段
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return
    t = now.hour * 60 + now.minute
    # 在 9:15-15:10 之间都可以保存
    if not (9 * 60 + 15 <= t < 15 * 60 + 10):
        return
    with _save_lock:
        try:
            data = {'_date': time.strftime('%Y%m%d')}
            data.update(call_auction_history)
            tmp = AUCTION_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, AUCTION_FILE)
        except Exception as e:
            print(f"[竞价] 保存失败: {e}")

_load_auction_history()
atexit.register(_save_auction_history)

# 已确认信号缓存
confirmed_signals = {}
pushed_signals = set()  # 已推送信号，避免重复推微信

def get_cached(key):
    if key in cache:
        data, ts = cache[key]
        ttl = CACHE_TTL.get(key.split(':')[0], 5)
        if time.time() - ts < ttl:
            return data
    return None

def set_cache(key, data):
    cache[key] = (data, time.time())

# ============ 股票代码转换 ============
def parse_code(raw):
    raw = raw.strip().lower().replace(' ', '')
    if raw.startswith('sh'):
        market, code = 'sh', raw[2:]
    elif raw.startswith('sz'):
        market, code = 'sz', raw[2:]
    else:
        code = raw
        if code.startswith(('6', '9')):
            market = 'sh'
        elif code.startswith(('0', '3', '2')):
            market = 'sz'
        elif code.startswith(('8', '4')):
            market = 'bj'
        else:
            market = 'sh'
    return {
        'code': code, 'market': market,
        'sina_code': f"{market}{code}",
    }


# ============ 盘口解析 ============
def parse_orderbook(f):
    """从新浪API返回字段解析五档买卖盘口，返回dict"""
    bids, asks = [], []
    for i in range(5):
        bids.append({
            'price': float(f[11 + i*2] or 0),
            'volume': int(float(f[10 + i*2] or 0)),
        })
        asks.append({
            'price': float(f[21 + i*2] or 0),
            'volume': int(float(f[20 + i*2] or 0)),
        })
    bid_total = sum(b['volume'] for b in bids)
    ask_total = sum(a['volume'] for a in asks)
    total = bid_total + ask_total
    imbalance = round((bid_total - ask_total) / total * 100, 2) if total > 0 else 0
    # 近端委比：仅用买一+买二 vs 卖一+卖二，更能反映即时买卖压力
    near_bid = sum(b['volume'] for b in bids[:2])
    near_ask = sum(a['volume'] for a in asks[:2])
    near_total = near_bid + near_ask
    near_imbalance = round((near_bid - near_ask) / near_total * 100, 2) if near_total > 0 else 0
    return {
        'bids': bids, 'asks': asks,
        'bid_total': bid_total, 'ask_total': ask_total,
        'order_imbalance': imbalance,
        'near_imbalance': near_imbalance,
    }

# ============ 认证路由 ============

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    webhook = data.get('webhook', '').strip()
    if not username or not password: return jsonify({'error': '用户名和密码不能为空'}), 400
    if len(username) < 2: return jsonify({'error': '用户名至少2个字符'}), 400
    if len(password) < 3: return jsonify({'error': '密码至少3位'}), 400
    if not webhook: return jsonify({'error': '请填写企业微信机器人Webhook URL（必填）。\n获取方法：打开企业微信 → 群聊设置 → 群机器人 → 添加机器人 → 复制Webhook地址。\n用于接收异动信号推送，如不需要推送请仍填写一个占位URL。'}), 400
    if not webhook.startswith('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key='):
        return jsonify({'error': 'Webhook URL格式不正确，应以 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key= 开头。\n请在企业微信群聊设置 → 群机器人中获取正确的Webhook地址。'}), 400
    users = _load_users()
    if username in users: return jsonify({'error': '用户名已存在'}), 400
    pw_hash = _hash_pw(password)
    users[username] = {'password': pw_hash, 'webhook': webhook, 'watchlist': [], 'confirmed_signals': {}, 'pushed_signals': [], 'role': 'user'}
    token = secrets.token_hex(16)
    users[username]['_token'] = token
    _save_users(users)
    user_tokens[token] = username
    return jsonify({'ok': True, 'token': token, 'username': username})

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    webhook = data.get('webhook', '').strip()
    users = _load_users()
    if username not in users: return jsonify({'error': '用户名或密码错误'}), 401
    if users[username]['password'] != _hash_pw(password): return jsonify({'error': '用户名或密码错误'}), 401
    token = secrets.token_hex(16)
    user_tokens[token] = username
    users[username]['_token'] = token
    # 如果输入了新webhook则更新
    if webhook:
        users[username]['webhook'] = webhook
    _save_users(users)
    return jsonify({'ok': True, 'token': token, 'username': username, 'webhook': users[username].get('webhook', '')})

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    user_tokens.pop(token, None)
    return jsonify({'ok': True})

@app.route('/api/watchlist/sync', methods=['POST'])
def api_watchlist_sync():
    username, user = _get_user_from_request()
    if not username: return jsonify({'error': '请先登录'}), 401
    data = request.get_json(silent=True) or {}
    if 'watchlist' in data:
        user['watchlist'] = data.get('watchlist', [])
        users = _load_users()
        users[username] = user
        _save_users(users)
    return jsonify({'ok': True, 'watchlist': user.get('watchlist', []), 'role': user.get('role', 'user')})

@app.route('/api/admin/users', methods=['GET'])
def api_admin_users():
    username, user = _get_user_from_request()
    if not username or user.get('role') != 'admin':
        return jsonify({'error': '无权限'}), 403
    users = _load_users()
    result = []
    for u, d in users.items():
        result.append({
            'username': u,
            'role': d.get('role', 'user'),
            'watchlist': d.get('watchlist', []),
            'webhook': d.get('webhook', '')[:50] + '...' if d.get('webhook') else '',
            'signal_count': len(d.get('pushed_signals', []))
        })
    return jsonify({'users': result})

@app.route('/api/admin/users/<target>', methods=['DELETE'])
def api_admin_delete_user(target):
    username, user = _get_user_from_request()
    if not username or user.get('role') != 'admin':
        return jsonify({'error': '无权限'}), 403
    if target == username:
        return jsonify({'error': '不能删除自己'}), 400
    users = _load_users()
    if target not in users:
        return jsonify({'error': '用户不存在'}), 404
    del users[target]
    _save_users(users)
    # 清除该用户的token
    for t, u in list(user_tokens.items()):
        if u == target:
            del user_tokens[t]
    return jsonify({'ok': True})

# ============ API 路由 ============

@app.route('/api/quote')
def api_quote():
    """实时报价 - 新浪财经"""
    raw_code = request.args.get('code', '')
    if not raw_code: return jsonify({'error': '请提供股票代码'}), 400

    info = parse_code(raw_code)
    key = f"quote:{info['sina_code']}"
    cached = get_cached(key)
    if cached: return jsonify(cached)

    try:
        url = f"http://hq.sinajs.cn/list={info['sina_code']}"
        resp = requests.get(url, headers={'User-Agent': UA, 'Referer': 'https://finance.sina.com.cn'}, timeout=5)
        resp.encoding = 'gbk'
        match = re.search(r'"([^"]*)"', resp.text)
        if not match: return jsonify({'error': '数据解析失败'}), 500

        f = match.group(1).split(',')
        if len(f) < 32: return jsonify({'error': '数据字段不足'}), 500

        price = float(f[3] or 0)
        yclose = float(f[2] or 0)

        # 解析盘口数据
        ob = parse_orderbook(f)

        # ── 集合竞价数据采集 ──
        auction_phase = is_auction_time(info['market'])
        if auction_phase:
            # 新浪API在竞价时段: field[6]=bid1(虚拟撮合价), field[10]=bid1量(虚拟匹配量)
            auction_price = float(f[6] or 0)
            auction_vol = int(float(f[10] or 0))
            now_ts = time.strftime('%H:%M:%S')
            if auction_price > 0:
                code_key = info['code']
                if code_key not in call_auction_history:
                    call_auction_history[code_key] = []
                # 去重：同一秒不重复记录
                hist = call_auction_history[code_key]
                if not hist or hist[-1]['time'] != now_ts:
                    hist.append({
                        'time': now_ts,
                        'price': round(auction_price, 2),
                        'volume': auction_vol,
                        'phase': auction_phase,  # 'pre' or 'post'
                    })
                    if len(hist) > MAX_AUCTION_SNAPS:
                        hist[:] = hist[-MAX_AUCTION_SNAPS:]
                # 每5次保存一次
                if len(hist) % 5 == 0:
                    _save_auction_history()

        data = {
            'name': f[0], 'open': float(f[1] or 0),
            'yesterday_close': yclose, 'price': price,
            'high': float(f[4] or 0), 'low': float(f[5] or 0),
            'volume': int(float(f[8] or 0)), 'amount': float(f[9] or 0),
            'change': round(price - yclose, 2),
            'change_pct': round((price - yclose) / yclose * 100, 2) if yclose else 0,
            'time': f[31] if len(f) > 31 else '',
            'code': info['code'], 'market': info['market'],
            # 盘口数据
            'bids': ob['bids'], 'asks': ob['asks'],
            'bid_total': ob['bid_total'], 'ask_total': ob['ask_total'],
            'order_imbalance': ob['order_imbalance'],
        }
        set_cache(key, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trend')
def api_trend():
    """日内分时数据 - 腾讯行情API"""
    raw_code = request.args.get('code', '')
    if not raw_code: return jsonify({'error': '请提供股票代码'}), 400

    info = parse_code(raw_code)
    key = f"trend:{info['sina_code']}"
    cached = get_cached(key)
    if cached: return jsonify(cached)

    try:
        tcode = f"{info['market']}{info['code']}"
        url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={tcode}"
        resp = requests.get(url, headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=5)
        text = resp.text

        # 解析 JSONP: min_data={...}
        json_str = text.replace('min_data=', '', 1).strip()
        if json_str.endswith(';'):
            json_str = json_str[:-1]
        data = json.loads(json_str)

        if data.get('code') != 0 or not data.get('data'):
            return jsonify({'error': '腾讯API返回异常', 'raw_code': data.get('code')}), 500

        stock_section = data['data'].get(tcode)
        if not stock_section or not isinstance(stock_section, dict):
            return jsonify({'error': '未找到股票数据'}), 500

        inner = stock_section.get('data') or stock_section
        mins = inner.get('data', [])

        # 解析昨收价 (从 qt 字段)
        qt = stock_section.get('qt', {})
        qt_arr = qt.get(tcode, [])
        pre_close = float(qt_arr[4]) if len(qt_arr) > 4 else 0
        date_str = qt_arr[30] if len(qt_arr) > 30 else ''
        if date_str and len(date_str) >= 8:
            date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

        # 解析分钟数据: "HHMM price cum_volume cum_amount"
        # 腾讯数据是累计的，需要转为每分时独立量
        raw_trends = []
        for m in mins:
            parts = m.split(' ')
            if len(parts) >= 3:
                time_str = parts[0]
                formatted_time = f"{time_str[:2]}:{time_str[2:4]}"
                full_time = f"{date_str} {formatted_time}" if date_str else formatted_time
                raw_trends.append({
                    'time': full_time,
                    'price': float(parts[1]),
                    'cum_volume': int(float(parts[2])),
                    'cum_amount': float(parts[3]) if len(parts) >= 4 and parts[3] else 0,
                })

        # 转为每分时独立量
        trends = []
        prev_cum_vol = 0
        prev_cum_amt = 0
        for i, rt in enumerate(raw_trends):
            per_vol = rt['cum_volume'] - prev_cum_vol
            per_amt = rt['cum_amount'] - prev_cum_amt
            if per_vol < 0: per_vol = 0  # 防止异常数据
            if per_amt < 0: per_amt = 0
            prev_cum_vol = rt['cum_volume']
            prev_cum_amt = rt['cum_amount']
            trends.append({
                'time': rt['time'],
                'price': rt['price'],
                'avg_price': 0,
                'volume': per_vol,       # ★ 每分时独立成交量(手)
                'amount': per_amt,       # ★ 每分时独立成交额(元)
            })

        # 计算均价 (累计成交额/累计成交量)
        cum_amount = 0
        cum_volume = 0
        for t in trends:
            cum_volume += t['volume']
            cum_amount += t['amount']
            t['avg_price'] = round(cum_amount / (cum_volume * 100), 2) if cum_volume > 0 else t['price']

        # ── 构建集合竞价数据 ──
        auction_snaps = call_auction_history.get(info['code'], [])
        auction_pre = [{'time': s['time'], 'price': s['price'], 'volume': s['volume']}
                       for s in auction_snaps if s.get('phase') == 'pre']
        auction_post = [{'time': s['time'], 'price': s['price'], 'volume': s['volume']}
                        for s in auction_snaps if s.get('phase') == 'post']

        result = {
            'code': info['code'], 'market': info['market'],
            'yesterday_close': pre_close,
            'trends': trends,
            'orderbook_timeline': orderbook_history.get(info['code'], []),
            'call_auction': {
                'pre': auction_pre,
                'post': auction_post,
            },
        }
        set_cache(key, result)
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': f'获取分时数据失败: {str(e)}'}), 500


@app.route('/api/call-auction')
def api_call_auction():
    """获取集合竞价数据（早盘9:15-9:25 + 尾盘14:57-15:00深市）"""
    raw_code = request.args.get('code', '')
    if not raw_code:
        return jsonify({'error': '请提供股票代码'}), 400

    info = parse_code(raw_code)
    code = info['code']
    snaps = call_auction_history.get(code, [])

    auction_pre = [{'time': s['time'], 'price': s['price'], 'volume': s['volume']}
                   for s in snaps if s.get('phase') == 'pre']
    auction_post = [{'time': s['time'], 'price': s['price'], 'volume': s['volume']}
                    for s in snaps if s.get('phase') == 'post']

    return jsonify({
        'code': code,
        'market': info['market'],
        'call_auction': {
            'pre': auction_pre,
            'post': auction_post,
        },
        'pre_count': len(auction_pre),
        'post_count': len(auction_post),
        'last_pre_time': auction_pre[-1]['time'] if auction_pre else None,
        'last_post_time': auction_post[-1]['time'] if auction_post else None,
    })


@app.route('/api/orderbook/history')
def api_orderbook_history():
    """获取完整盘口历史数据（含五档明细，服务器重启后可回溯）"""
    raw_code = request.args.get('code', '')
    if not raw_code:
        return jsonify({'error': '请提供股票代码'}), 400

    info = parse_code(raw_code)
    code = info['code']
    ob_list = orderbook_history.get(code, [])

    # 支持时间范围过滤
    from_time = request.args.get('from', '')
    to_time = request.args.get('to', '')

    if from_time or to_time:
        filtered = []
        for snap in ob_list:
            t = snap['time']
            if from_time and t < from_time:
                continue
            if to_time and t > to_time:
                continue
            filtered.append(snap)
        ob_list = filtered

    # 支持限制条数（最新的N条）
    limit = request.args.get('limit', '')
    if limit:
        try:
            n = int(limit)
            ob_list = ob_list[-n:] if n > 0 else ob_list
        except ValueError:
            pass

    return jsonify({
        'code': code,
        'market': info['market'],
        'snapshots': ob_list,
        'count': len(ob_list),
        'last_time': ob_list[-1]['time'] if ob_list else None,
    })


@app.route('/api/chip')
def api_chip():
    """筹码分布数据 - 基于日内数据计算"""
    raw_code = request.args.get('code', '')
    if not raw_code: return jsonify({'error': '请提供股票代码'}), 400

    info = parse_code(raw_code)
    key = f"chip:{info['sina_code']}"
    cached = get_cached(key)
    if cached: return jsonify(cached)

    result = {
        'code': info['code'], 'chips': [], 'avg_cost': 0,
        'profit_ratio': 0, 'concentration_90': 0,
        'cost_90_high': 0, 'cost_90_low': 0,
    }

    # 尝试从 trend 缓存获取数据，没拿到就等一下再试
    trend_key = f"trend:{info['sina_code']}"
    trend_cached = get_cached(trend_key)
    
    if not trend_cached:
        time.sleep(0.5)
        trend_cached = get_cached(trend_key)

    mins_data = None
    current_price = 0
    pre_close = 0

    if trend_cached and trend_cached.get('trends'):
        mins_data = trend_cached['trends']
        pre_close = trend_cached.get('yesterday_close', 0)
        quote_key = f"quote:{info['sina_code']}"
        quote_cached = get_cached(quote_key)
        if quote_cached:
            current_price = quote_cached.get('price', 0)

    # 如果缓存没有，主动请求腾讯API
    if not mins_data:
        try:
            tcode = f"{info['market']}{info['code']}"
            url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={tcode}"
            resp = requests.get(url, headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=5)
            text = resp.text
            json_str = text.replace('min_data=', '', 1).strip().rstrip(';')
            data = json.loads(json_str)

            if data.get('code') == 0 and data.get('data'):
                stock_section = data['data'].get(tcode)
                if stock_section and isinstance(stock_section, dict):
                    inner = stock_section.get('data') or stock_section
                    raw_mins = inner.get('data', [])
                    qt = stock_section.get('qt', {})
                    qt_arr = qt.get(tcode, [])
                    current_price = float(qt_arr[3]) if len(qt_arr) > 3 else 0
                    pre_close = float(qt_arr[4]) if len(qt_arr) > 4 else 0

                    mins_data = []
                    prev_vol = 0
                    for m in raw_mins:
                        parts = m.split(' ')
                        if len(parts) >= 3:
                            cum_vol = int(float(parts[2]))
                            per_vol = cum_vol - prev_vol
                            if per_vol < 0: per_vol = 0
                            prev_vol = cum_vol
                            mins_data.append({
                                'price': float(parts[1]),
                                'volume': per_vol,  # ★ 每分时独立量
                            })
        except Exception:
            pass

    # 基于分钟数据计算筹码分布
    if mins_data:
        try:
            prices_volumes = [(m['price'], m['volume']) for m in mins_data if m['volume'] > 0]
            if not prices_volumes:
                set_cache(key, result)
                return jsonify(result)

            all_prices = [pv[0] for pv in prices_volumes]
            min_p = min(all_prices)
            max_p = max(all_prices)
            price_range = max_p - min_p
            if price_range == 0:
                price_range = max_p * 0.02

            # 按1分钱粒度划分价格区间
            bucket_size = 0.01  # 一分钱
            # 价格对齐到分
            min_p_aligned = round(min_p - 0.005, 2)
            max_p_aligned = round(max_p + 0.005, 2)
            BUCKETS = max(10, int((max_p_aligned - min_p_aligned) / bucket_size) + 1)
            BUCKETS = min(BUCKETS, 5000)  # 最多5000个桶，防止异常
            buckets_vol = [0] * BUCKETS

            for p, v in prices_volumes:
                idx = int((p - min_p_aligned) / bucket_size)
                if 0 <= idx < BUCKETS:
                    buckets_vol[idx] += v

            total_vol_lots = sum(buckets_vol)
            chips = []
            for i, vol in enumerate(buckets_vol):
                price = min_p + bucket_size * (i + 0.5)
                chips.append({'price': round(price, 2), 'percent': round(vol / total_vol_lots * 100, 2) if total_vol_lots else 0})

            # 平均成本：从分时数据按price×volume独立加权计算
            total_value = sum(p * v * 100 for p, v in prices_volumes)
            total_v_lots = sum(v for _, v in prices_volumes)
            avg_cost = round(total_value / (total_v_lots * 100), 2) if total_v_lots > 0 else 0

            # 90%和70%成本区间和集中度
            sorted_chips = sorted(chips, key=lambda x: x['price'])
            cum_pct = 0
            cost_5 = cost_95 = cost_15 = cost_85 = sorted_chips[0]['price']
            for c in sorted_chips:
                cum_pct += c['percent']
                if cum_pct >= 5 and cost_5 == sorted_chips[0]['price']:
                    cost_5 = c['price']
                if cum_pct >= 15 and cost_15 == sorted_chips[0]['price']:
                    cost_15 = c['price']
                if cum_pct >= 85 and cost_85 == sorted_chips[0]['price']:
                    cost_85 = c['price']
                if cum_pct >= 95:
                    cost_95 = c['price']
                    break

            concentration_90 = round((cost_95 - cost_5) / avg_cost * 100, 1) if avg_cost > 0 else 0
            concentration_70 = round((cost_85 - cost_15) / avg_cost * 100, 1) if avg_cost > 0 else 0

            # 获利比例
            if current_price == 0 and mins_data:
                current_price = mins_data[-1]['price']
            total_v = sum(v for _, v in prices_volumes)
            profit_count = sum(v for p, v in prices_volumes if p <= current_price)
            profit_ratio = round(profit_count / total_v * 100, 1) if total_v > 0 else 0

            result = {
                'code': info['code'],
                'chips': chips,
                'avg_cost': round(avg_cost, 2),
                'profit_ratio': profit_ratio,
                'concentration_90': concentration_90,
                'cost_90_high': round(cost_95, 2),
                'cost_90_low': round(cost_5, 2),
                'concentration_70': concentration_70,
                'cost_70_high': round(cost_85, 2),
                'cost_70_low': round(cost_15, 2),
                'current_price': current_price,
                'pre_close': pre_close,
            }
        except Exception:
            pass

    set_cache(key, result)
    return jsonify(result)


@app.route('/api/quote/batch')
def api_quote_batch():
    """批量获取报价"""
    codes = request.args.get('codes', '')
    if not codes: return jsonify({'error': '请提供股票代码'}), 400

    code_list = [c.strip() for c in codes.split(',') if c.strip()][:10]
    sina_codes, code_map = [], {}

    for raw in code_list:
        info = parse_code(raw)
        sina_codes.append(info['sina_code'])
        code_map[info['sina_code']] = info

    try:
        url = f"http://hq.sinajs.cn/list={','.join(sina_codes)}"
        resp = requests.get(url, headers={'User-Agent': UA, 'Referer': 'https://finance.sina.com.cn'}, timeout=5)
        resp.encoding = 'gbk'
        results = []

        for line in resp.text.strip().split('\n'):
            match = re.search(r'hq_str_(\w+)="([^"]*)"', line)
            if not match: continue
            sina_code = match.group(1)
            f = match.group(2).split(',')
            if len(f) < 32: continue
            info = code_map.get(sina_code)
            if not info: continue

            price = float(f[3] or 0)
            yclose = float(f[2] or 0)
            # 顺便保存盘口快照，让自选股持续积累委比数据
            ob = parse_orderbook(f)
            # ── 竞价数据采集（批量接口也收集）──
            auction_phase = is_auction_time(info['market'])
            if auction_phase:
                auction_price = float(f[6] or 0)
                auction_vol = int(float(f[10] or 0))
                now_ts = time.strftime('%H:%M:%S')
                if auction_price > 0:
                    code_key = info['code']
                    if code_key not in call_auction_history:
                        call_auction_history[code_key] = []
                    hist = call_auction_history[code_key]
                    if not hist or hist[-1]['time'] != now_ts:
                        hist.append({
                            'time': now_ts,
                            'price': round(auction_price, 2),
                            'volume': auction_vol,
                            'phase': auction_phase,
                        })
                        if len(hist) > MAX_AUCTION_SNAPS:
                            hist[:] = hist[-MAX_AUCTION_SNAPS:]
            results.append({
                'code': info['code'], 'market': info['market'],
                'name': f[0], 'price': price,
                'change_pct': round((price - yclose) / yclose * 100, 2) if yclose else 0,
                'volume': int(float(f[8] or 0)),
            })
        return jsonify({'stocks': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============ 微信推送 ============
PUSH_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'push_token.txt')
PUSH_WEBHOOK = ''
if os.path.exists(PUSH_TOKEN_FILE):
    with open(PUSH_TOKEN_FILE, 'r') as f:
        PUSH_WEBHOOK = f.read().strip()
    print(f"[推送] 企业微信Webhook已加载")

def push_wechat(webhook, content):
    """通过企业微信机器人推送（markdown格式）"""
    if not webhook:
        return
    try:
        data = {'msgtype': 'markdown', 'markdown': {'content': content}}
        requests.post(webhook, json=data, timeout=5)
    except:
        pass

@app.route('/api/signals')
def api_signals():
    """批量检测所有自选股的顶底信号（多用户版）"""
    # 获取当前用户
    username, user = _get_user_from_request()
    if not username or not user:
        return jsonify({'error': '请先登录'}), 401

    codes = request.args.get('codes', '')
    if not codes:
        return jsonify({'error': '请提供股票代码'}), 400

    # 每日重置（per-user）+ 检测逻辑版本号变更自动清空
    today = time.strftime('%Y%m%d')
    DETECT_VER = 12  # 每次改检测逻辑就 +1，旧信号自动清空重检
    if user.get('_signal_date') != today or user.get('_signal_ver') != DETECT_VER:
        user['confirmed_signals'] = {}
        user['_signal_date'] = today
        user['_signal_ver'] = DETECT_VER
        user['pushed_signals'] = []
    confirmed_signals = user['confirmed_signals']

    code_list = [c.strip() for c in codes.split(',') if c.strip()][:10]
    results = []

    for raw in code_list:
        try:
            info = parse_code(raw)
            trend_key = f"trend:{info['sina_code']}"
            trend_cached = get_cached(trend_key)

            if not trend_cached or not trend_cached.get('trends'):
                tcode = f"{info['market']}{info['code']}"
                url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={tcode}"
                resp = requests.get(url, headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=5)
                text = resp.text
                json_str = text.replace('min_data=', '', 1).strip().rstrip(';')
                data = json.loads(json_str)
                if data.get('code') != 0 or not data.get('data'):
                    continue
                stock_section = data['data'].get(tcode)
                if not stock_section or not isinstance(stock_section, dict):
                    continue
                inner = stock_section.get('data') or stock_section
                raw_mins = inner.get('data', [])
                qt = stock_section.get('qt', {})
                qt_arr = qt.get(tcode, [])
                pre_close = float(qt_arr[4]) if len(qt_arr) > 4 else 0
                date_str = qt_arr[30] if len(qt_arr) > 30 else ''
                if date_str and len(date_str) >= 8:
                    date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                trends = []
                prev_cum_vol = 0
                prev_cum_amt = 0
                for m in raw_mins:
                    parts = m.split(' ')
                    if len(parts) >= 3:
                        time_str = parts[0]
                        formatted_time = f"{time_str[:2]}:{time_str[2:4]}"
                        full_time = f"{date_str} {formatted_time}" if date_str else formatted_time
                        cum_vol = int(float(parts[2]))
                        cum_amt = float(parts[3]) if len(parts) >= 4 and parts[3] else 0
                        per_vol = cum_vol - prev_cum_vol
                        per_amt = cum_amt - prev_cum_amt
                        if per_vol < 0: per_vol = 0
                        if per_amt < 0: per_amt = 0
                        prev_cum_vol = cum_vol
                        prev_cum_amt = cum_amt
                        trends.append({
                            'time': full_time, 'price': float(parts[1]),
                            'volume': per_vol, 'amount': per_amt,
                        })
                cum_amount = 0
                cum_volume = 0
                for t in trends:
                    cum_volume += t['volume']
                    cum_amount += t['amount']
                    t['avg_price'] = round(cum_amount / (cum_volume * 100), 2) if cum_volume > 0 else t['price']
                # 计算多空指标（新加载的数据需要算，缓存数据已有）
            else:
                trends = trend_cached['trends']
                pre_close = trend_cached.get('yesterday_close', 0)

            if not trends or len(trends) < 10:
                continue

            # ── 实时异动检测 ──
            prices = [t['price'] for t in trends]
            volumes = [t['volume'] for t in trends]
            avg_prices = [t.get('avg_price', t['price']) for t in trends]
            n = len(prices)
            events = []

            def chg(i, nmin):
                if i < nmin: return 0
                return (prices[i] - prices[i-nmin]) / prices[i-nmin] * 100

            def dkv(i, k):
                v = trends[i].get(k)
                return v if v is not None else None

            # 获取股票名
            name = info['code']
            qk = f"quote:{info['sina_code']}"
            qc = get_cached(qk)
            if qc:
                name = qc.get('name', info['code'])
            else:
                try:
                    qurl = f"http://hq.sinajs.cn/list={info['sina_code']}"
                    qresp = requests.get(qurl, headers={'User-Agent': UA, 'Referer': 'https://finance.sina.com.cn'}, timeout=3)
                    qresp.encoding = 'gbk'
                    qm = re.search(r'"([^"]*)"', qresp.text)
                    if qm:
                        qf = qm.group(1).split(',')
                        if len(qf) > 0 and qf[0]:
                            name = qf[0]
                except:
                    pass

            events = []
            # 从已存储事件恢复上次的严重级别，避免跨轮询重复报
            ck = info['code']
            stored_all = confirmed_signals.get(ck, [])
            stored_surge = [s for s in stored_all if s['t']=='surge']
            stored_plunge = [s for s in stored_all if s['t']=='plunge']
            last_sv_up = {'high':3,'medium':2,'low':1}.get(stored_surge[-1]['s'],0) if stored_surge else 0
            last_sv_dn = {'high':3,'medium':2,'low':1}.get(stored_plunge[-1]['s'],0) if stored_plunge else 0
            for i in range(1, n):
                cp = prices[i]; cv = volumes[i]
                pp = prices[i-1]

                c3 = chg(i, 3)
                sv_up = 3 if c3 >= 1.8 else (2 if c3 >= 1.2 else (1 if c3 >= 0.8 else 0))
                sv_dn = 3 if c3 <= -1.8 else (2 if c3 <= -1.2 else (1 if c3 <= -0.8 else 0))
                if sv_up > last_sv_up:
                    lb = ['','轻度急拉','中度急拉','强烈急拉'][sv_up]
                    cl = ['','#ff9933','#ff7733','#ff3333'][sv_up]
                    events.append({'t':'surge','l':lb,'s':['low','medium','high'][sv_up-1],'p':cp,'d':f'+{c3:.1f}%/3min','c':cl,'idx':i})
                last_sv_up = sv_up
                if sv_dn > last_sv_dn:
                    lb = ['','轻度急跌','中度急跌','强烈急跌'][sv_dn]
                    cl = ['','#009944','#00aa44','#00cc44'][sv_dn]
                    events.append({'t':'plunge','l':lb,'s':['low','medium','high'][sv_dn-1],'p':cp,'d':f'{c3:.1f}%/3min','c':cl,'idx':i})
                last_sv_dn = sv_dn

                if i >= 5:
                    hi = max(prices[:i]); lo = min(prices[:i])
                    if cp > hi: events.append({'t':'new_high','l':'新高','s':'low','p':cp,'d':f'¥{cp:.2f}','c':'#ff4444','idx':i})
                    if cp < lo: events.append({'t':'new_low','l':'新低','s':'low','p':cp,'d':f'¥{cp:.2f}','c':'#00cc66','idx':i})

            # 去重
            sev = {'high':3,'medium':2,'low':1}
            dedup = {}; out = []
            for e in events:
                k = f"{e['t']}_{e['idx']//3}"
                if k in dedup:
                    if sev.get(e['s'],0) > sev.get(out[dedup[k]]['s'],0):
                        out[dedup[k]] = e
                else:
                    dedup[k] = len(out); out.append(e)
            events = out

            # 入队
            if ck not in confirmed_signals: confirmed_signals[ck] = []
            stored = confirmed_signals[ck]
            skeys = {f"{s['t']}|{s['time']}" for s in stored}
            for e in events:
                e['time'] = trends[e['idx']]['time']
                sk = f"{e['t']}|{e['time']}"
                if sk not in skeys:
                    stored.append({k:e[k] for k in ['t','l','s','p','d','c','time']})
                    skeys.add(sk)
            if len(stored) > 50: stored = stored[-50:]
            confirmed_signals[ck] = stored

            if stored:
                results.append({
                    'code': info['code'], 'name': name,
                    'last_price': prices[-1] if prices else 0,
                    'pre_close': pre_close,
                    'events': stored,
                })

        except Exception:
            continue

    # ── 推送 + 保存 ──
    now_hm = time.strftime('%H:%M')
    now_min = int(now_hm[:2]) * 60 + int(now_hm[3:])
    all_users = _load_users()
    for uname, udata in all_users.items():
        wh = udata.get('webhook', '')
        if not wh: continue
        if udata.get('_push_date') != today:
            udata['pushed_signals'] = []; udata['_push_date'] = today
        pushed = set(udata.get('pushed_signals', []))
        wl_codes = {s.get('code','') if isinstance(s,dict) else str(s) for s in udata.get('watchlist',[])}
        for r in results:
            if r['code'] not in wl_codes: continue
            # 按分钟分组，合并同时间事件
            byMin = {}
            ICONS = {'surge':'🔥','plunge':'💧','new_high':'🚀','new_low':'💀'}
            for e in r.get('events', []):
                ek = f"{r['code']}|{e['t']}|{e['time']}"
                if ek in pushed: continue
                tm = e['time']; hm = tm[11:16] if len(tm)>=16 else tm[:5]
                if abs(now_min - (int(hm[:2])*60+int(hm[3:]))) > 5: continue
                pushed.add(ek)
                mk = f"{r['code']}|{hm}"
                if mk not in byMin: byMin[mk] = {'name':r['name'],'code':r['code'],'tm':hm,'price':e['p'],'lines':[]}
                icon = ICONS.get(e['t'], '📌')
                color = e.get('c','#ff4444').replace('#','')
                sev = {'high':'🔥🔥🔥','medium':'🔥🔥','low':'🔥'}.get(e.get('s',''),'')
                byMin[mk]['lines'].append(f"{icon} **<font color=\"{color}\">{e['l']}</font>** {sev}  {e['d']}")
            for mk, grp in byMin.items():
                md = f"**{grp['tm']}**  {grp['name']}({grp['code']})  ¥{grp['price']}\n" + '\n'.join(grp['lines'])
                push_wechat(wh, md)
        udata['pushed_signals'] = list(pushed)

    user['confirmed_signals'] = confirmed_signals
    if username in all_users:
        user['pushed_signals'] = all_users[username].get('pushed_signals', [])
        user['_push_date'] = all_users[username].get('_push_date', today)
    all_users[username] = user
    _save_users(all_users)

    return jsonify({'signals': results, 'time': time.strftime('%H:%M:%S'), 'ver': 4})


@app.route('/')
def index():
    from flask import make_response
    resp = make_response(send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


if __name__ == '__main__':
    import sys
    port = int(os.environ.get('PORT', 5000))
    print("=" * 50)
    print("  A股盘口分时 · 集合竞价 · 筹码博弈")
    print(f"  浏览器访问: http://localhost:{port}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False)
