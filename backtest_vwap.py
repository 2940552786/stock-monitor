"""
VWAP动量衰减条件回测
测试不同参数组合下，加入VWAP动量条件后信号胜率的变化
"""
import json
import time
import requests
from collections import defaultdict

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

# ============ 数据获取 ============
def get_minute_data(code, date_str):
    """获取指定股票指定日期的分钟数据（腾讯API）"""
    market = 'sh' if code.startswith(('6','9')) else 'sz'
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
                if per_vol < 0: per_vol = 0
                if per_amt < 0: per_amt = 0
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
        print(f"  获取失败: {e}")
        return [], 0


# ============ 信号检测（当前逻辑） ============
def detect_signals(trends, W=8, VR=1.2, DEV=0.3):
    """检测顶底信号（仅量价层，不含盘口）"""
    prices = [t['price'] for t in trends]
    volumes = [t['volume'] for t in trends]
    avg_prices = [t['avg_price'] for t in trends]
    n = len(prices)
    tops, bottoms = [], []
    
    for i in range(W, n):
        cp, cv, ap = prices[i], volumes[i], avg_prices[i]
        if cv <= 0:
            continue
        
        avgV = sum(volumes[i-W:i+1]) / (W + 1)
        isLeftMax = all(prices[j] < cp for j in range(i-W, i))
        isLeftMin = all(prices[j] > cp for j in range(i-W, i))
        dev_pct = (cp - ap) / ap * 100
        
        if isLeftMax and cv > avgV * VR and dev_pct > DEV:
            tops.append({'index': i, 'price': cp, 'time': trends[i]['time']})
        if isLeftMin and cv > avgV * VR and dev_pct < -DEV:
            bottoms.append({'index': i, 'price': cp, 'time': trends[i]['time']})
    
    return tops, bottoms


# ============ 胜率评估 ============
def evaluate_signal(trends, sig, sig_type, forward_min=10, threshold_pct=0.5):
    idx = sig['index']
    prices = [t['price'] for t in trends]
    n = len(prices)
    sig_price = sig['price']
    end_idx = min(idx + forward_min, n - 1)
    
    if sig_type == 'top':
        future_min = min(prices[idx+1:end_idx+1]) if idx+1 <= end_idx else sig_price
        drop_pct = (sig_price - future_min) / sig_price * 100
        return drop_pct >= threshold_pct, drop_pct
    else:
        future_max = max(prices[idx+1:end_idx+1]) if idx+1 <= end_idx else sig_price
        rise_pct = (future_max - sig_price) / sig_price * 100
        return rise_pct >= threshold_pct, rise_pct


# ============ VWAP动量衰减（改进版） ============
def check_vwap_momentum_v2(trends, idx, sig_type, lookback=5, slope_threshold=0.02):
    """
    改进版：直接看VWAP近N分钟的涨速是否太快
    顶：VWAP还在快速拉升 → 假顶，过滤
    底：VWAP还在快速下跌 → 假底，过滤
    slope_threshold: VWAP每分钟涨跌幅度阈值(%)
    """
    if idx < lookback:
        return True
    
    avg_prices = [t['avg_price'] for t in trends]
    recent_slope = (avg_prices[idx] - avg_prices[idx - lookback]) / avg_prices[idx - lookback] * 100 / lookback
    
    if sig_type == 'top':
        # VWAP还在涨(斜率>阈值) → 假顶，过滤掉（返回False）
        return recent_slope < slope_threshold
    else:
        # VWAP还在跌(斜率< -阈值) → 假底，过滤掉
        return recent_slope > -slope_threshold


# ============ VWAP价格背离（新增） ============
def check_price_vwap_divergence(trends, idx, sig_type, lookback=5):
    """
    价格与VWAP的背离程度
    顶：价格创新高但VWAP离价格越来越远 → 虚高，假顶
    底：价格创新低但VWAP离价格越来越远 → 虚低，假底
    返回 True = 背离不大，可信
    """
    if idx < lookback:
        return True
    
    prices = [t['price'] for t in trends]
    avg_prices = [t['avg_price'] for t in trends]
    
    # 当前价格偏离VWAP的百分比
    current_dev = abs(prices[idx] - avg_prices[idx]) / avg_prices[idx] * 100
    
    # 过去N分钟平均偏离
    past_devs = [abs(prices[j] - avg_prices[j]) / avg_prices[j] * 100 for j in range(idx - lookback, idx)]
    avg_past_dev = sum(past_devs) / len(past_devs)
    
    # 如果当前偏离远超历史平均 → 可能是情绪化冲高/杀跌 → 假信号
    return current_dev < avg_past_dev * 2.0


# ============ 主回测v2 ============
def run_backtest_v2(stocks, dates, forward_min=10, threshold_pct=0.5):
    print("=" * 70)
    print("  VWAP动量条件回测 v2 — 多种方案对比")
    print("=" * 70)
    
    all_tops = []
    all_bottoms = []
    
    for code in stocks:
        for date_str in dates:
            trends, pre_close = get_minute_data(code, date_str)
            if not trends:
                continue
            tops, bottoms = detect_signals(trends)
            for t in tops:
                t['code'] = code; t['date'] = date_str; t['trends'] = trends
                all_tops.append(t)
            for b in bottoms:
                b['code'] = code; b['date'] = date_str; b['trends'] = trends
                all_bottoms.append(b)
    
    print(f"  总信号: {len(all_tops)}顶 + {len(all_bottoms)}底")
    print(f"  评估标准: 未来{forward_min}分钟 反转>{threshold_pct}%")
    print()
    
    def winrate(signals, sig_type):
        if not signals: return 0, 0
        wins = sum(1 for s in signals if evaluate_signal(s['trends'], s, sig_type, forward_min, threshold_pct)[0])
        return wins, len(signals)
    
    # 基准
    tw, tn = winrate(all_tops, 'top')
    bw, bn = winrate(all_bottoms, 'bottom')
    base_top = tw/tn*100; base_bot = bw/bn*100
    print(f"  【基准】不加条件    顶 {tw}/{tn}={base_top:.1f}%  底 {bw}/{bn}={base_bot:.1f}%")
    print()
    
    # 方案1：VWAP近N分钟斜率过滤
    print("  【方案1】VWAP近N分钟斜率太快则过滤")
    for lb in [3, 5, 8]:
        for th in [0.01, 0.02, 0.03, 0.05]:
            ft = [t for t in all_tops if check_vwap_momentum_v2(t['trends'], t['index'], 'top', lb, th)]
            fb = [b for b in all_bottoms if check_vwap_momentum_v2(b['trends'], b['index'], 'bottom', lb, th)]
            tw2, _ = winrate(ft, 'top'); bw2, _ = winrate(fb, 'bottom')
            tp = tw2/len(ft)*100 if ft else 0
            bp = bw2/len(fb)*100 if fb else 0
            imp = ((tp-base_top)+(bp-base_bot))/2
            mark = "⭐" if imp > 2 else ("✓" if imp > 0 else "")
            print(f"    近{lb}分 斜率>{th}%/min | 顶{len(ft)}({tp:.1f}%) 底{len(fb)}({bp:.1f}%) | 提升{imp:+.1f}% {mark}")
    
    print()
    # 方案2：价格-VWAP背离
    print("  【方案2】价格偏离VWAP超过历史均值2倍则过滤")
    for lb in [3, 5, 8]:
        ft = [t for t in all_tops if check_price_vwap_divergence(t['trends'], t['index'], 'top', lb)]
        fb = [b for b in all_bottoms if check_price_vwap_divergence(b['trends'], b['index'], 'bottom', lb)]
        tw2, _ = winrate(ft, 'top'); bw2, _ = winrate(fb, 'bottom')
        tp = tw2/len(ft)*100 if ft else 0
        bp = bw2/len(fb)*100 if fb else 0
        imp = ((tp-base_top)+(bp-base_bot))/2
        print(f"    窗口{lb}分 | 顶{len(ft)}({tp:.1f}%) 底{len(fb)}({bp:.1f}%) | 提升{imp:+.1f}%")
    
    print()
    # 方案3：组合
    print("  【方案3】斜率+背离 组合")
    best_combo = None
    best_imp = -999
    for lb in [3, 5]:
        for th in [0.02, 0.03]:
            ft = [t for t in all_tops if check_vwap_momentum_v2(t['trends'], t['index'], 'top', lb, th) 
                  and check_price_vwap_divergence(t['trends'], t['index'], 'top', lb)]
            fb = [b for b in all_bottoms if check_vwap_momentum_v2(b['trends'], b['index'], 'bottom', lb, th)
                  and check_price_vwap_divergence(b['trends'], b['index'], 'bottom', lb)]
            tw2, _ = winrate(ft, 'top'); bw2, _ = winrate(fb, 'bottom')
            tp = tw2/len(ft)*100 if ft else 0
            bp = bw2/len(fb)*100 if fb else 0
            imp = ((tp-base_top)+(bp-base_bot))/2
            mark = "⭐" if imp > 2 else ""
            print(f"    近{lb}分 斜率>{th}%/min + 背离 | 顶{len(ft)}({tp:.1f}%) 底{len(fb)}({bp:.1f}%) | 提升{imp:+.1f}% {mark}")
            if imp > best_imp:
                best_imp = imp
                best_combo = (lb, th)
    
    print()
    # 方案4：绝对斜率上限（user方案）
    print("  【方案4】VWAP近N分钟绝对斜率上限（不管之前多快，现在必须够平）")
    for lb in [3, 5, 8]:
        for cap in [0.01, 0.02, 0.03, 0.04]:
            def check_abs_slope(trends, idx, sig_type, lb=lb, cap=cap):
                if idx < lb: return True
                avg_prices = [t['avg_price'] for t in trends]
                slope = (avg_prices[idx] - avg_prices[idx - lb]) / avg_prices[idx - lb] * 100 / lb
                if sig_type == 'top':
                    return slope < cap   # VWAP涨速必须低于上限
                else:
                    return slope > -cap  # VWAP跌速必须低于上限
            ft = [t for t in all_tops if check_abs_slope(t['trends'], t['index'], 'top')]
            fb = [b for b in all_bottoms if check_abs_slope(b['trends'], b['index'], 'bottom')]
            tw2, _ = winrate(ft, 'top'); bw2, _ = winrate(fb, 'bottom')
            tp = tw2/len(ft)*100 if ft else 0
            bp = bw2/len(fb)*100 if fb else 0
            imp = ((tp-base_top)+(bp-base_bot))/2
            mark = "⭐" if imp > 3 else ("✓" if imp > 0 else "")
            print(f"    近{lb}分 |斜率|<{cap}%/min | 顶{len(ft)}({tp:.1f}%) 底{len(fb)}({bp:.1f}%) | 提升{imp:+.1f}% {mark}")
    
    print()
    # 方案5：绝对斜率 + 价格背离 组合
    print("  【方案5】绝对斜率上限 + 价格背离 组合")
    for lb in [5, 8]:
        for cap in [0.02, 0.03]:
            def check_combo2(trends, idx, sig_type, lb=lb, cap=cap):
                if idx < lb: return True
                avg_prices = [t['avg_price'] for t in trends]
                slope = (avg_prices[idx] - avg_prices[idx - lb]) / avg_prices[idx - lb] * 100 / lb
                slope_ok = (slope < cap) if sig_type == 'top' else (slope > -cap)
                return slope_ok and check_price_vwap_divergence(trends, idx, sig_type, lb)
            ft = [t for t in all_tops if check_combo2(t['trends'], t['index'], 'top')]
            fb = [b for b in all_bottoms if check_combo2(b['trends'], b['index'], 'bottom')]
            tw2, _ = winrate(ft, 'top'); bw2, _ = winrate(fb, 'bottom')
            tp = tw2/len(ft)*100 if ft else 0
            bp = bw2/len(fb)*100 if fb else 0
            imp = ((tp-base_top)+(bp-base_bot))/2
            mark = "⭐" if imp > 3 else ("✓" if imp > 0 else "")
            print(f"    近{lb}分 |斜率|<{cap}%/min + 背离 | 顶{len(ft)}({tp:.1f}%) 底{len(fb)}({bp:.1f}%) | 提升{imp:+.1f}% {mark}")
    
    print()
    print("=" * 70)
    if best_combo and best_imp > 0:
        print(f"  ✅ 推荐: 近{best_combo[0]}分钟 VWAP斜率>{best_combo[1]}%/min 过滤 + 价格背离过滤")
        print(f"     胜率提升: +{best_imp:.1f}%")
    else:
        print(f"  ⚠️ 所有方案均未显著提升胜率（基准顶{base_top:.1f}% 底{base_bot:.1f}%）")
        print(f"     建议优先优化量价检测层参数，而非叠加过滤条件")
    print("=" * 70)


if __name__ == '__main__':
    stocks = ['000779', '600519', '000001', '300750', '002594']
    dates = ['20260717', '20260716', '20260715', '20260714', '20260711']
    run_backtest_v2(stocks, dates)

