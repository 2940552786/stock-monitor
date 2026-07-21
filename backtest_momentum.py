"""
动能衰减过滤回测
对比：无过滤 vs 有动能衰减过滤 的胜率差异
在多个股票×多个交易日上验证过滤效果
"""
import json
import requests
import sys

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

STOCKS = ['000779', '600519', '000001', '300750', '002594', '600602', '600795']
DATES = ['20260718', '20260717', '20260716', '20260715', '20260711']


def get_minute_data(code, date_str):
    """获取指定股票指定日期的分钟数据（腾讯API）"""
    market = 'sh' if code.startswith(('6', '9')) else 'sz'
    tcode = f"{market}{code}"
    url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={tcode}&date={date_str}"
    try:
        resp = requests.get(url, headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=10)
        text = resp.text
        json_str = text.replace('min_data=', '', 1).strip().rstrip(';')
        data = json.loads(json_str)
        stock_section = data.get('data', {}).get(tcode, {})
        inner = stock_section.get('data') or stock_section
        mins = inner.get('data', [])

        qt = stock_section.get('qt', {})
        qt_arr = qt.get(tcode, [])
        pre_close = float(qt_arr[4]) if len(qt_arr) > 4 else 0

        trends = []
        prev_cum_vol = 0
        prev_cum_amt = 0
        for m in mins:
            parts = m.split(' ')
            if len(parts) >= 3:
                cum_vol = int(float(parts[2]))
                cum_amt = float(parts[3]) if len(parts) >= 4 and parts[3] else 0
                per_vol = cum_vol - prev_cum_vol
                per_amt = cum_amt - prev_cum_amt
                if per_vol < 0:
                    per_vol = 0
                if per_amt < 0:
                    per_amt = 0
                prev_cum_vol = cum_vol
                prev_cum_amt = cum_amt
                trends.append({
                    'time': parts[0],
                    'price': float(parts[1]),
                    'volume': per_vol,
                    'amount': per_amt,
                })

        # 计算VWAP均价
        cum_amount = 0
        cum_volume = 0
        for t in trends:
            cum_volume += t['volume']
            cum_amount += t['amount']
            t['avg_price'] = round(cum_amount / (cum_volume * 100), 2) if cum_volume > 0 else t['price']

        return trends, pre_close
    except Exception as e:
        return [], 0


def detect_signals(trends, use_momentum_filter=True, decay_ratio=0.7):
    """
    检测顶底信号（与server.py逻辑一致）
    use_momentum_filter: 是否启用动能衰减过滤
    decay_ratio: 衰减比率阈值
    """
    prices = [t['price'] for t in trends]
    volumes = [t['volume'] for t in trends]
    avg_prices = [t.get('avg_price', t['price']) for t in trends]
    n = len(prices)
    MAX_W = 12
    tops, bottoms = [], []
    filtered_tops, filtered_bottoms = 0, 0  # 被动能衰减过滤掉的信号数

    for i in range(6, n):
        cp, cv, ap = prices[i], volumes[i], avg_prices[i]
        if cv <= 0:
            continue

        effW = min(MAX_W, max(3, i // 2))
        window_vols = volumes[i - effW:i + 1]
        sv = sorted(window_vols)
        p75_vol = sv[int(len(sv) * 0.75)]
        vol_ok = cv > p75_vol or (i > 0 and volumes[i - 1] > p75_vol)

        isLeftMax = all(prices[j] < cp for j in range(i - effW, i))
        isLeftMin = all(prices[j] > cp for j in range(i - effW, i))

        dev_pct = (cp - ap) / ap * 100

        # 自适应超涨/超跌阈值
        dyn_dev = 0.25
        if i >= 20:
            past_devs = [abs(prices[j] - avg_prices[j]) / avg_prices[j] * 100
                         for j in range(1, i + 1) if avg_prices[j] > 0]
            if past_devs:
                past_devs.sort()
                dyn_dev = max(past_devs[int(len(past_devs) * 0.8)], 0.15)

        # VWAP斜率
        vwap_slope = 0
        slope_limit = 0.03
        if i >= 8:
            vwap_slope = (avg_prices[i] - avg_prices[i - 5]) / avg_prices[i - 5] * 100 / 5
        tick_slope = 0.01 / ap * 100 / 5 * 1.5 if ap > 0 else 0
        slope_limit = max(slope_limit, tick_slope)
        if i >= 20:
            vwap_changes = []
            for j in range(1, i + 1):
                if avg_prices[j - 1] > 0:
                    vwap_changes.append(abs(avg_prices[j] - avg_prices[j - 1]) / avg_prices[j - 1] * 100)
            if vwap_changes:
                vwap_changes.sort()
                p80 = vwap_changes[int(len(vwap_changes) * 0.8)]
                tick_slope = 0.01 / ap * 100 / 5 * 1.5 if ap > 0 else 0
                slope_limit = max(p80 * 2, 0.015, tick_slope)
        top_slope_ok = vwap_slope < slope_limit
        bot_slope_ok = vwap_slope > -slope_limit

        # 价格-VWAP背离
        divergence_ok = True
        if i >= 8:
            current_dev = abs(cp - ap) / ap * 100
            past_devs = [abs(prices[j] - avg_prices[j]) / avg_prices[j] * 100 for j in range(i - 8, i)]
            avg_past_dev = sum(past_devs) / len(past_devs)
            divergence_ok = current_dev < avg_past_dev * 2.5

        # ⑥ 动能衰竭检查
        momentum_exhaustion_ok = True
        if use_momentum_filter and i >= 10:
            short_delta = prices[i] - prices[i - 3]
            prev_delta = prices[i - 3] - prices[i - 6]

            if isLeftMax and prev_delta > 0:
                if short_delta >= prev_delta * decay_ratio:
                    momentum_exhaustion_ok = False
            if isLeftMin and prev_delta < 0:
                if short_delta <= prev_delta * decay_ratio:
                    momentum_exhaustion_ok = False

        # 判断信号
        if isLeftMax and vol_ok and dev_pct > dyn_dev and top_slope_ok and divergence_ok:
            if momentum_exhaustion_ok:
                tops.append({'index': i, 'price': cp, 'time': trends[i]['time']})
            else:
                filtered_tops += 1

        if isLeftMin and vol_ok and dev_pct < -dyn_dev and bot_slope_ok and divergence_ok:
            if momentum_exhaustion_ok:
                bottoms.append({'index': i, 'price': cp, 'time': trends[i]['time']})
            else:
                filtered_bottoms += 1

    return tops, bottoms, filtered_tops, filtered_bottoms


def evaluate_signal(trends, sig, sig_type, forward_min=10, threshold_pct=0.5):
    """评估单个信号：未来forward_min分钟内是否反转超过threshold_pct%"""
    idx = sig['index']
    prices = [t['price'] for t in trends]
    n = len(prices)
    sig_price = sig['price']
    end_idx = min(idx + forward_min, n - 1)

    if sig_type == 'top':
        future_min = min(prices[idx + 1:end_idx + 1]) if idx + 1 <= end_idx else sig_price
        drop_pct = (sig_price - future_min) / sig_price * 100
        return drop_pct >= threshold_pct, drop_pct
    else:
        future_max = max(prices[idx + 1:end_idx + 1]) if idx + 1 <= end_idx else sig_price
        rise_pct = (future_max - sig_price) / sig_price * 100
        return rise_pct >= threshold_pct, rise_pct


def run_backtest(decay_ratios=None, forward_min=10, threshold_pct=0.5):
    """主回测：对比无过滤 vs 不同衰减比率"""
    if decay_ratios is None:
        decay_ratios = [0.5, 0.6, 0.7, 0.8, 0.9]

    print("=" * 75)
    print("  动能衰减过滤回测")
    print(f"  股票: {', '.join(STOCKS)}")
    print(f"  日期: {', '.join(DATES)}")
    print(f"  评估标准: 未来{forward_min}分钟 反转>{threshold_pct}%")
    print("=" * 75)
    print()

    # 收集所有日期的数据
    all_days = []
    for code in STOCKS:
        for date_str in DATES:
            trends, pre_close = get_minute_data(code, date_str)
            if trends and len(trends) >= 30:
                all_days.append({
                    'code': code, 'date': date_str,
                    'trends': trends, 'pre_close': pre_close,
                })

    print(f"  有效交易日: {len(all_days)}天")
    print()

    # ── 基准：无过滤 ──
    base_tops, base_bottoms = [], []
    for day in all_days:
        t, b, _, _ = detect_signals(day['trends'], use_momentum_filter=False)
        for s in t:
            s['code'] = day['code']; s['date'] = day['date']; s['trends'] = day['trends']
            base_tops.append(s)
        for s in b:
            s['code'] = day['code']; s['date'] = day['date']; s['trends'] = day['trends']
            base_bottoms.append(s)

    base_tw = sum(1 for s in base_tops if evaluate_signal(s['trends'], s, 'top', forward_min, threshold_pct)[0])
    base_bw = sum(1 for s in base_bottoms if evaluate_signal(s['trends'], s, 'bottom', forward_min, threshold_pct)[0])
    base_tp = base_tw / len(base_tops) * 100 if base_tops else 0
    base_bp = base_bw / len(base_bottoms) * 100 if base_bottoms else 0
    base_avg = (base_tp + base_bp) / 2

    print(f"  【基准】无过滤")
    print(f"    顶: {len(base_tops)}信号  胜率: {base_tw}/{len(base_tops)}={base_tp:.1f}%")
    print(f"    底: {len(base_bottoms)}信号  胜率: {base_bw}/{len(base_bottoms)}={base_bp:.1f}%")
    print(f"    平均胜率: {base_avg:.1f}%")
    print()

    # ── 测试不同衰减比率 ──
    print(f"  {'衰减比':<8} {'顶信号':<8} {'顶胜率':<10} {'底信号':<8} {'底胜率':<10} {'平均胜率':<10} {'过滤顶':<8} {'过滤底':<8} {'胜率提升':<10}")
    print("  " + "-" * 73)

    best_ratio, best_imp = None, -999
    for ratio in decay_ratios:
        test_tops, test_bottoms = [], []
        total_ft, total_fb = 0, 0
        for day in all_days:
            t, b, ft, fb = detect_signals(day['trends'], use_momentum_filter=True, decay_ratio=ratio)
            total_ft += ft
            total_fb += fb
            for s in t:
                s['code'] = day['code']; s['date'] = day['date']; s['trends'] = day['trends']
                test_tops.append(s)
            for s in b:
                s['code'] = day['code']; s['date'] = day['date']; s['trends'] = day['trends']
                test_bottoms.append(s)

        tw = sum(1 for s in test_tops if evaluate_signal(s['trends'], s, 'top', forward_min, threshold_pct)[0])
        bw = sum(1 for s in test_bottoms if evaluate_signal(s['trends'], s, 'bottom', forward_min, threshold_pct)[0])
        tp = tw / len(test_tops) * 100 if test_tops else 0
        bp = bw / len(test_bottoms) * 100 if test_bottoms else 0
        avg_p = (tp + bp) / 2
        imp = avg_p - base_avg
        mark = " ⭐" if imp > 2 else (" ✓" if imp > 0 else "")

        print(f"  {ratio:<8} {len(test_tops):<8} {tp:<10.1f}% {len(test_bottoms):<8} {bp:<10.1f}% {avg_p:<10.1f}% {total_ft:<8} {total_fb:<8} {imp:+.1f}%{mark}")

        if imp > best_imp:
            best_imp = imp
            best_ratio = ratio

    print()
    print("=" * 75)
    if best_ratio and best_imp > 0:
        print(f"  ✅ 推荐衰减比率: {best_ratio}  平均胜率提升: +{best_imp:.1f}%")
    else:
        print(f"  ⚠️ 所有比率均未显著提升胜率，建议调整参数重新测试")
    print("=" * 75)


if __name__ == '__main__':
    # 可通过命令行参数覆盖
    forward_min = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    threshold_pct = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    run_backtest(forward_min=forward_min, threshold_pct=threshold_pct)
