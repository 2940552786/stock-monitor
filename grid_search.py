"""
顶底预判模型 - 全面网格搜索回测
11个交易日 × 20只股票 × 294组参数
"""
import json, requests, time, itertools
from collections import defaultdict

UA = 'Mozilla/5.0'

STOCKS = [
    ('sh600519','茅台'),('sz000001','平安银行'),('sz300750','宁德时代'),
    ('sh600036','招商银行'),('sz000858','五粮液'),('sh601318','中国平安'),
    ('sz002594','比亚迪'),('sh600030','中信证券'),('sz000333','美的集团'),
    ('sh600602','云赛智联'),('sh600900','长江电力'),('sz000651','格力电器'),
    ('sh601398','工商银行'),('sz300059','东方财富'),('sh603259','药明康德'),
    ('sz002475','立讯精密'),('sh600809','山西汾酒'),('sz000568','泸州老窖'),
    ('sh601012','隆基绿能'),('sz300274','阳光电源'),
]

def fetch_all_data():
    """获取所有股票的5分钟K线数据"""
    all_data = {}
    url = 'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData'
    for sym, name in STOCKS:
        try:
            params = {'symbol': sym, 'scale': '5', 'ma': 'no', 'datalen': '500'}
            r = requests.get(url, params=params, headers={'User-Agent': UA}, timeout=15)
            data = r.json()
            if data and len(data) > 100:
                # 按日期分组
                by_date = defaultdict(list)
                for k in data:
                    d = k.get('day', '')[:10]
                    if d:
                        by_date[d].append(k)
                # 过滤掉不完整的交易日(<40根)
                valid = {d: kls for d, kls in by_date.items() if len(kls) >= 40}
                if len(valid) >= 5:
                    all_data[sym] = {'name': name, 'days': valid}
                    print(f'  {name}({sym}): {len(valid)}天')
            time.sleep(0.3)
        except Exception as e:
            print(f'  {name} 失败: {e}')
    return all_data


def compute_indicators(klines):
    """预计算每根K线的指标"""
    n = len(klines)
    prices = [float(k['close']) for k in klines]
    highs = [float(k['high']) for k in klines]
    lows = [float(k['low']) for k in klines]
    volumes = [int(float(k['volume'])) for k in klines]

    # VWAP
    total_val, total_vol = 0, 0
    avg_prices = []
    for k in klines:
        mid = (float(k['high']) + float(k['low'])) / 2
        vol = int(float(k['volume']))
        total_val += mid * vol
        total_vol += vol
        avg_prices.append(round(total_val / total_vol, 2) if total_vol > 0 else mid)

    return prices, highs, lows, volumes, avg_prices


def detect_and_eval(klines, W, VR, DEV, asy_vr_pm=0, asy_dev_bot=0):
    """检测信号并评估前向准确率"""
    prices, highs, lows, volumes, avg_prices = compute_indicators(klines)
    n = len(prices)
    results = []

    for i in range(W, n - 3):
        cp, cv, ap = prices[i], volumes[i], avg_prices[i]
        if cv <= 0:
            continue

        avgV = sum(volumes[i-W:i+1]) / (W + 1)
        vr_val = cv / avgV if avgV > 0 else 0
        isMax = all(highs[j] < highs[i] for j in range(i-W, i))
        isMin = all(lows[j] > lows[i] for j in range(i-W, i))
        dev = (cp - ap) / ap * 100

        # 时段
        time_str = klines[i].get('day', '')
        hour = 12
        if len(time_str) >= 13:
            hour = int(time_str[11:13])
        is_am = hour < 12

        # 非对称参数
        eff_vr_top = VR + (asy_vr_pm if not is_am else 0)
        eff_dev_bot = asy_dev_bot if asy_dev_bot > 0 else DEV

        # 顶
        if isMax and vr_val >= eff_vr_top and dev > DEV:
            w1 = prices[min(i+1,n-1)] < cp
            w2 = prices[min(i+2,n-1)] < cp
            w3 = prices[min(i+3,n-1)] < cp
            fav = (cp - min(prices[i:i+10])) / cp * 100 if i+10 < n else (cp - min(prices[i:])) / cp * 100
            results.append({'type':'top', 'vr':vr_val, 'dev':dev, 'am':is_am,
                           'w1':w1, 'w2':w2, 'w3':w3, 'fav':fav})

        # 底
        if isMin and vr_val >= VR and dev < -eff_dev_bot:
            w1 = prices[min(i+1,n-1)] > cp
            w2 = prices[min(i+2,n-1)] > cp
            w3 = prices[min(i+3,n-1)] > cp
            fav = (max(prices[i:i+10]) - cp) / cp * 100 if i+10 < n else (max(prices[i:]) - cp) / cp * 100
            results.append({'type':'bottom', 'vr':vr_val, 'dev':dev, 'am':is_am,
                           'w1':w1, 'w2':w2, 'w3':w3, 'fav':fav})

    return results


def score_config(all_results):
    """给一个参数配置打分"""
    tops = [r for r in all_results if r['type'] == 'top']
    bots = [r for r in all_results if r['type'] == 'bottom']
    total = len(all_results)

    if total < 30:  # 信号太少不可靠
        return 0, 0, 0, 0

    tw = sum(1 for r in tops if r['w3']) / len(tops) * 100 if tops else 0
    bw = sum(1 for r in bots if r['w3']) / len(bots) * 100 if bots else 0

    # 综合得分：准确率 × 信号数量(log) 来平衡准确率和覆盖面
    score = (tw * len(tops) + bw * len(bots)) / total
    return score, tw, bw, total


print("=" * 60)
print("  获取数据...")
print("=" * 60)
all_data = fetch_all_data()
total_days = sum(len(v['days']) for v in all_data.values())
total_bars = sum(sum(len(kls) for kls in v['days'].values()) for v in all_data.values())
print(f"\n总计: {len(all_data)}只股票, {total_days}个交易日, {total_bars}根5分钟K线")

# ===================== 网格搜索 =====================
print("\n" + "=" * 60)
print("  网格搜索...")
print("=" * 60)

W_range = [5, 6, 7, 8, 9, 10, 12]
VR_range = [1.5, 1.8, 2.0, 2.2, 2.5, 3.0]
DEV_range = [0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5]
asy_vr_pm_range = [0, 0.3, 0.5]  # 下午顶额外量比
asy_dev_bot_range = [0, 0.4, 0.5, 0.6]  # 底额外偏离

best_score = 0
best_config = None
all_scores = []

total_combos = len(W_range) * len(VR_range) * len(DEV_range)
done = 0

for W in W_range:
    for VR in VR_range:
        for DEV in DEV_range:
            # 基础对称配置
            all_results = []
            for sym, info in all_data.items():
                for date, klines in info['days'].items():
                    res = detect_and_eval(klines, W, VR, DEV)
                    all_results.extend(res)

            score, tw, bw, cnt = score_config(all_results)
            all_scores.append((score, tw, bw, cnt, W, VR, DEV, 0, 0, '对称'))

            # 非对称: 下午顶VR加0.5
            for avp in asy_vr_pm_range:
                if avp == 0:
                    continue
                all_results2 = []
                for sym, info in all_data.items():
                    for date, klines in info['days'].items():
                        res = detect_and_eval(klines, W, VR, DEV, asy_vr_pm=avp)
                        all_results2.extend(res)
                score2, tw2, bw2, cnt2 = score_config(all_results2)
                all_scores.append((score2, tw2, bw2, cnt2, W, VR, DEV, avp, 0, f'顶下午+{avp}'))

            # 非对称: 底DEV额外
            for adb in asy_dev_bot_range:
                if adb == 0:
                    continue
                all_results3 = []
                for sym, info in all_data.items():
                    for date, klines in info['days'].items():
                        res = detect_and_eval(klines, W, VR, DEV, asy_dev_bot=adb)
                        all_results3.extend(res)
                score3, tw3, bw3, cnt3 = score_config(all_results3)
                all_scores.append((score3, tw3, bw3, cnt3, W, VR, DEV, 0, adb, f'底DEV={adb}'))

            done += 1
            if done % 20 == 0:
                print(f'  进度: {done}/{total_combos}')

# 排序
all_scores.sort(key=lambda x: x[0], reverse=True)

print(f"\n{'='*80}")
print(f"  🏆 TOP 20 参数组合")
print(f"{'='*80}")
print(f"{'排名':<5} {'得分':<8} {'顶%':<7} {'底%':<7} {'总数':<6} {'W':<4} {'VR':<6} {'DEV':<6} {'类型'}")
print(f"{'-'*65}")

for i, (score, tw, bw, cnt, W, VR, DEV, avp, adb, label) in enumerate(all_scores[:20]):
    print(f"{i+1:<5} {score:<8.1f} {tw:<7.1f} {bw:<7.1f} {cnt:<6} {W:<4} {VR:<6.1f} {DEV:<6.2f} {label}")

print(f"\n{'='*80}")
print(f"  📊 分析")
print(f"{'='*80}")

# 分析最优W
from collections import Counter
w_count = Counter()
vr_count = Counter()
dev_count = Counter()
for _, _, _, _, W, VR, DEV, _, _, _ in all_scores[:50]:
    w_count[W] += 1
    vr_count[VR] += 1
    dev_count[DEV] += 1
print(f"最优W分布: {dict(w_count.most_common())}")
print(f"最优VR分布: {dict(vr_count.most_common())}")
print(f"最优DEV分布: {dict(dev_count.most_common())}")
