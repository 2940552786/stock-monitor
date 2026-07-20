"""
A股分时图看盘工具 - 后端代理服务 v1.1
解决前端跨域问题，代理新浪/腾讯API请求
"""
import json
import re
import time
import datetime
import hashlib
import secrets
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

# ============ 用户系统 ============
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'users.json')
user_tokens = {}  # {token: username}

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
    data = request.get_json()
    if not data: return jsonify({'error': '请提供JSON'}), 400
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    webhook = data.get('webhook', '').strip()
    if not username or not password: return jsonify({'error': '用户名和密码不能为空'}), 400
    if len(username) < 2: return jsonify({'error': '用户名至少2个字符'}), 400
    if len(password) < 3: return jsonify({'error': '密码至少3位'}), 400
    users = _load_users()
    if username in users: return jsonify({'error': '用户名已存在'}), 400
    pw_hash = _hash_pw(password)
    users[username] = {'password': pw_hash, 'webhook': webhook, 'watchlist': [], 'confirmed_signals': {}, 'pushed_signals': []}
    _save_users(users)
    token = secrets.token_hex(16)
    user_tokens[token] = username
    return jsonify({'ok': True, 'token': token, 'username': username})

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json()
    if not data: return jsonify({'error': '请提供JSON'}), 400
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    webhook = data.get('webhook', '').strip()
    users = _load_users()
    if username not in users: return jsonify({'error': '用户名或密码错误'}), 401
    if users[username]['password'] != _hash_pw(password): return jsonify({'error': '用户名或密码错误'}), 401
    token = secrets.token_hex(16)
    user_tokens[token] = username
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
    data = request.get_json()
    if not data: return jsonify({'error': '请提供JSON'}), 400
    # 如果传了watchlist就更新，否则返回当前watchlist
    if 'watchlist' in data:
        user['watchlist'] = data.get('watchlist', [])
        users = _load_users()
        users[username] = user
        _save_users(users)
    return jsonify({'ok': True, 'watchlist': user.get('watchlist', [])})

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

        # 保存盘口快照到时序缓存

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

        result = {
            'code': info['code'], 'market': info['market'],
            'yesterday_close': pre_close,
            'trends': trends,
            'orderbook_timeline': orderbook_history.get(info['code'], []),
        }
        set_cache(key, result)
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': f'获取分时数据失败: {str(e)}'}), 500


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

def push_wechat(webhook, title, content):
    """通过企业微信机器人推送"""
    if not webhook:
        return
    try:
        data = {'msgtype': 'text', 'text': {'content': f"{title}\n{content}"}}
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

    # 每日重置（per-user）
    today = time.strftime('%Y%m%d')
    if user['confirmed_signals'].get('_date') != today:
        user['confirmed_signals'] = {'_date': today}
        user['pushed_signals'] = []
    confirmed_signals = user['confirmed_signals']
    pushed_signals = set(user['pushed_signals'])

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
            else:
                trends = trend_cached['trends']
                pre_close = trend_cached.get('yesterday_close', 0)

            if not trends or len(trends) < 10:
                continue

            # 顶底检测（预测版：纯左侧，实时判断）
            prices = [t['price'] for t in trends]
            volumes = [t['volume'] for t in trends]
            avg_prices = [t.get('avg_price', t['price']) for t in trends]
            n = len(prices)
            MAX_W = 12
            MIN_I = 6
            tops, bottoms = [], []

            # ── 涨跌停检测：所有分钟都检查，不受MIN_I限制 ──
            limit_up = pre_close * 1.10
            limit_down = pre_close * 0.90
            for i in range(1, n):
                cp = prices[i]
                if cp >= limit_up * 0.998:
                    prev_cp = prices[i-1]
                    if prev_cp < limit_up * 0.998:
                        tops.append({'index': i, 'price': round(cp, 2), 'time': trends[i]['time'], 'source': 'limit'})
                if cp <= limit_down * 1.002:
                    prev_cp = prices[i-1]
                    if prev_cp > limit_down * 1.002:
                        bottoms.append({'index': i, 'price': round(cp, 2), 'time': trends[i]['time'], 'source': 'limit'})

            for i in range(MIN_I, n):
                cp, cv, ap = prices[i], volumes[i], avg_prices[i]
                if cv <= 0:
                    continue

                # 渐进窗口：i=6→W=3, i=24→W=12
                effW = min(MAX_W, max(3, i // 2))
                window_vols = volumes[i-effW:i+1]
                avgV = sum(window_vols) / (effW + 1)
                # 动态放量门槛：只要有一笔即可
                vol_ok = cv > 1

                isLeftMax = all(prices[j] < cp for j in range(i-effW, i))
                isLeftMin = all(prices[j] > cp for j in range(i-effW, i))

                dev_pct = (cp - ap) / ap * 100

                # 自适应超涨/超跌阈值：日内价格偏离VWAP的P90分位数
                dyn_dev = 0.6  # 默认
                if i >= 15:
                    past_devs = [abs(prices[j] - avg_prices[j]) / avg_prices[j] * 100 for j in range(1, i+1) if avg_prices[j] > 0]
                    if past_devs:
                        past_devs.sort()
                        dyn_dev = max(past_devs[int(len(past_devs) * 0.85)], 0.25)

                # VWAP回归斜率：近8分钟均价趋势（%/min）
                vwap_slope = 0
                if i >= 8:
                    seg = avg_prices[i-7:i+1]  # 8个点
                    n_v = len(seg)
                    xm = (n_v - 1) / 2.0
                    ym = sum(seg) / n_v
                    num_v = sum((j - xm) * (seg[j] - ym) for j in range(n_v))
                    den_v = sum((j - xm) ** 2 for j in range(n_v))
                    if den_v != 0:
                        vwap_slope = (num_v / den_v) / ym * 100  # 相对于均值的%变化率

                # 自适应斜率阈值：日内VWAP分钟变化的80分位数 × 2
                slope_limit = 0.03  # 默认
                tick_slope = 0.01 / ap * 100 / 8 * 1.5 if ap > 0 else 0
                slope_limit = max(slope_limit, tick_slope)
                if i >= 20:
                    vwap_changes = []
                    for j in range(1, i + 1):
                        if avg_prices[j-1] > 0:
                            vwap_changes.append(abs(avg_prices[j] - avg_prices[j-1]) / avg_prices[j-1] * 100)
                    if vwap_changes:
                        vwap_changes.sort()
                        p80 = vwap_changes[int(len(vwap_changes) * 0.8)]
                        # tick粒度保护：低价股1分钱就是大波动
                        tick_slope = 0.01 / ap * 100 / 8 * 1.5 if ap > 0 else 0
                        slope_limit = max(p80 * 2, 0.015, tick_slope)
                top_slope_ok = vwap_slope < slope_limit
                bot_slope_ok = vwap_slope > -slope_limit

                # 价格-VWAP背离：当前偏离是否远超近8分钟平均水平
                divergence_ok = True
                if i >= 8:
                    current_dev = abs(cp - ap) / ap * 100
                    past_devs = [abs(prices[j] - avg_prices[j]) / avg_prices[j] * 100 for j in range(i-8, i)]
                    avg_past_dev = sum(past_devs) / len(past_devs)
                    divergence_ok = current_dev < avg_past_dev * 2.5

                if isLeftMax and vol_ok and dev_pct > dyn_dev and top_slope_ok and divergence_ok:
                    tops.append({'index': i, 'price': round(cp, 2), 'time': trends[i]['time']})
                if isLeftMin and vol_ok and dev_pct < -dyn_dev and bot_slope_ok and divergence_ok:
                    bottoms.append({'index': i, 'price': round(cp, 2), 'time': trends[i]['time']})

            # ── 信号确认：去重后直接入队 ──

            # 获取当前盘口委比（同时获取名称）
            order_imbalance = 0
            name = info['code']
            quote_key = f"quote:{info['sina_code']}"
            quote_cached = get_cached(quote_key)
            if quote_cached:
                order_imbalance = quote_cached.get('order_imbalance', 0)
                name = quote_cached.get('name', info['code'])
            else:
                try:
                    qurl = f"http://hq.sinajs.cn/list={info['sina_code']}"
                    qresp = requests.get(qurl, headers={'User-Agent': UA, 'Referer': 'https://finance.sina.com.cn'}, timeout=3)
                    qresp.encoding = 'gbk'
                    qm = re.search(r'"([^"]*)"', qresp.text)
                    if qm:
                        qf = qm.group(1).split(',')
                        if len(qf) >= 30:
                            ob = parse_orderbook(qf)
                            order_imbalance = ob['order_imbalance']
                        if len(qf) > 0 and qf[0]:
                            name = qf[0]
                except Exception:
                    pass

            # 获取或初始化已确认信号
            code_key = info['code']
            if code_key not in confirmed_signals:
                confirmed_signals[code_key] = {'tops': [], 'bottoms': []}
            stored = confirmed_signals[code_key]
            stored_top_times = {s['time'] for s in stored['tops']}
            stored_bot_times = {s['time'] for s in stored['bottoms']}

            # ── 信号入队 ──
            for s in tops:
                if s['time'] not in stored_top_times:
                    s['confidence'] = 'high'
                    stored['tops'].append(s)

            for s in bottoms:
                if s['time'] not in stored_bot_times:
                    s['confidence'] = 'high'
                    stored['bottoms'].append(s)

            if stored['tops'] or stored['bottoms']:
                results.append({
                    'code': info['code'],
                    'name': name,
                    'last_price': prices[-1] if prices else 0,
                    'pre_close': pre_close,
                    'order_imbalance': order_imbalance,
                    'tops': stored['tops'],
                    'bottoms': stored['bottoms'],
                })

        except Exception:
            continue

    # 微信推送新信号（仅推最近2分钟内的）
    now_hm = time.strftime('%H:%M')
    now_min = int(now_hm[:2]) * 60 + int(now_hm[3:])
    for r in results:
        for t in r.get('tops', []) + r.get('bottoms', []):
            sig_key = f"{r['code']}|{t['time']}"
            if sig_key not in pushed_signals:
                pushed_signals.add(sig_key)
                tm = t['time']
                hm = tm[11:16] if len(tm) >= 16 else tm[:5]
                sig_min = int(hm[:2]) * 60 + int(hm[3:])
                if now_min - sig_min <= 2:  # 只推2分钟内的
                    tp = '🔴顶' if t in r.get('tops', []) else '🟢底'
                    push_wechat(user.get('webhook', ''), f"{tp} {r['name']} {r['code']}", f"价格: ¥{t['price']}\n时间: {tm}\n{r['name']}({r['code']})")

    user['confirmed_signals'] = confirmed_signals
    user['pushed_signals'] = list(pushed_signals)
    users = _load_users()
    users[username] = user
    _save_users(users)

    return jsonify({'signals': results, 'time': time.strftime('%H:%M:%S'), 'ver': 3})


@app.route('/')
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'index.html')


if __name__ == '__main__':
    import sys
    port = int(os.environ.get('PORT', 5000))
    print("=" * 50)
    print("  A股分时图看盘工具 v1.1")
    print(f"  浏览器访问: http://localhost:{port}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False)
