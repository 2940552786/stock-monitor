"""
顶底信号回测脚本
分析当前检测逻辑的胜率：顶信号卖一手、底信号买一手的收益情况
"""
import json
import time
import sys
import requests

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

# ============ 预测版参数（纯左侧，实时判断） ============
W = 12       # 左看窗口
VR = 2.5     # 量比阈值
DEV = 0.25   # 偏离均价阈值(%)

# ============ 自选股列表（与前端默认一致） ============
WATCHLIST = [
    {'code': '600519', 'market': 'sh', 'name': '贵州茅台'},
    {'code': '000001', 'market': 'sz', 'name': '平安银行'},
    {'code': '300750', 'market': 'sz', 'name': '宁德时代'},
]

def fetch_trend(code, market):
    """获取分时数据"""
    tcode = f"{market}{code}"
    url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={tcode}"
    try:
        resp = requests.get(url, headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=10)
        text = resp.text
        json_str = text.replace('min_data=', '', 1).strip().rstrip(';')
        data = json.loads(json_str)

        if data.get('code') != 0 or not data.get('data'):
            return None

        stock_section = data['data'].get(tcode)
        if not stock_section or not isinstance(stock_section, dict):
            return None

        inner = stock_section.get('data') or stock_section
        raw_mins = inner.get('data', [])
        qt = stock_section.get('qt', {})
        qt_arr = qt.get(tcode, [])
        pre_close = float(qt_arr[4]) if len(qt_arr) > 4 else 0

        trends = []
        prev_cum_vol = 0
        for m in raw_mins:
            parts = m.split(' ')
            if len(parts) >= 3:
                time_str = parts[0]
                cum_vol = int(float(parts[2]))
                per_vol = cum_vol - prev_cum_vol
                if per_vol < 0:
                    per_vol = 0
                prev_cum_vol = cum_vol
                trends.append({
                    'time': f"{time_str[:2]}:{time_str[2:4]}",
                    'price': float(parts[1]),
                    'volume': per_vol,
                })

        # 计算均价 (累计成交额/累计成交量)，预测版需要
        cum_amount, cum_vol = 0, 0
        for t in trends:
            cum_vol += t['volume']
            cum_amount += t['volume'] * t['price'] * 100
            t['avg_price'] = round(cum_amount / (cum_vol * 100), 2) if cum_vol > 0 else t['price']

        return {'code': code, 'pre_close': pre_close, 'trends': trends}
    except Exception as e:
        print(f"  ⚠ 获取 {code} 数据失败: {e}")
        return None


def detect_signals(trends):
    """
    预测版：纯左侧，实时判断。只看左边W根K线 + 放量 + 超涨/超跌
    返回 (tops, bottoms)
    """
    if not trends or len(trends) < 10:
        return [], []

    prices = [t['price'] for t in trends]
    volumes = [t['volume'] for t in trends]
    avg_prices = [t.get('avg_price', t['price']) for t in trends]
    n = len(prices)
    tops, bottoms = [], []

    for i in range(W, n):
        cp, cv, ap = prices[i], volumes[i], avg_prices[i]
        if cv <= 0:
            continue

        # ── 只看左边: [i-W, i] ──
        avgV = sum(volumes[i-W:i+1]) / (W + 1)

        isLeftMax = all(prices[j] < cp for j in range(i-W, i))
        isLeftMin = all(prices[j] > cp for j in range(i-W, i))

        dev_pct = (cp - ap) / ap * 100  # 偏离均价百分比

        # 顶预测: 左边最高 + 放量 + 价格明显高于均价（超涨）
        if isLeftMax and cv > avgV * VR and dev_pct > DEV:
            tops.append({
                'index': i, 'price': cp, 'time': trends[i]['time'],
                'volume': cv, 'avg_volume': round(avgV, 0),
                'dev_pct': round(dev_pct, 3),
            })

        # 底预测: 左边最低 + 放量 + 价格明显低于均价（超跌）
        if isLeftMin and cv > avgV * VR and dev_pct < -DEV:
            bottoms.append({
                'index': i, 'price': cp, 'time': trends[i]['time'],
                'volume': cv, 'avg_volume': round(avgV, 0),
                'dev_pct': round(dev_pct, 3),
            })

    return tops, bottoms


def backtest_signal_accuracy(trends, tops, bottoms):
    """
    预测版回测：信号发出后，后续价格是否按预测方向走
    - 顶信号 → 预测跌，胜 = 后续价格 < 信号价
    - 底信号 → 预测涨，胜 = 后续价格 > 信号价
    """
    prices = [t['price'] for t in trends]
    n = len(prices)
    
    top_results = []
    for t in tops:
        sig_idx = t['index']
        signal_price = t['price']
        remaining = n - sig_idx - 1
        if remaining < 3:
            continue
        
        after_1 = prices[min(sig_idx + 1, n-1)]
        after_3 = prices[min(sig_idx + 3, n-1)]
        after_5 = prices[min(sig_idx + 5, n-1)]
        close_price = prices[-1]
        
        win_1 = after_1 < signal_price
        win_3 = after_3 < signal_price
        win_5 = after_5 < signal_price
        win_close = close_price < signal_price
        
        post_prices = prices[sig_idx:]
        min_after = min(post_prices)
        max_drop = (signal_price - min_after) / signal_price * 100
        
        top_results.append({
            'time': t['time'], 'price': signal_price,
            'dev_pct': t.get('dev_pct', 0),
            'after_1': round(after_1, 2), 'after_3': round(after_3, 2),
            'after_5': round(after_5, 2), 'close': round(close_price, 2),
            'win_1': win_1, 'win_3': win_3, 'win_5': win_5, 'win_close': win_close,
            'max_drop_pct': round(max_drop, 3), 'remaining_bars': remaining,
        })
    
    bottom_results = []
    for b in bottoms:
        sig_idx = b['index']
        signal_price = b['price']
        remaining = n - sig_idx - 1
        if remaining < 3:
            continue
        
        after_1 = prices[min(sig_idx + 1, n-1)]
        after_3 = prices[min(sig_idx + 3, n-1)]
        after_5 = prices[min(sig_idx + 5, n-1)]
        close_price = prices[-1]
        
        win_1 = after_1 > signal_price
        win_3 = after_3 > signal_price
        win_5 = after_5 > signal_price
        win_close = close_price > signal_price
        
        post_prices = prices[sig_idx:]
        max_after = max(post_prices)
        max_rise = (max_after - signal_price) / signal_price * 100
        
        bottom_results.append({
            'time': b['time'], 'price': signal_price,
            'dev_pct': b.get('dev_pct', 0),
            'after_1': round(after_1, 2), 'after_3': round(after_3, 2),
            'after_5': round(after_5, 2), 'close': round(close_price, 2),
            'win_1': win_1, 'win_3': win_3, 'win_5': win_5, 'win_close': win_close,
            'max_rise_pct': round(max_rise, 3), 'remaining_bars': remaining,
        })
    
    return top_results, bottom_results


def backtest_pair_trading(trends, tops, bottoms):
    """
    预测版配对交易：信号出现即入场（当下成交），不等确认
    """
    prices = [t['price'] for t in trends]
    n = len(prices)
    
    all_signals = []
    for t in tops:
        all_signals.append({'type': 'top', 'index': t['index'], 'price': prices[t['index']],
                           'signal_price': t['price'], 'time': t['time']})
    for b in bottoms:
        all_signals.append({'type': 'bottom', 'index': b['index'], 'price': prices[b['index']],
                           'signal_price': b['price'], 'time': b['time']})
    
    all_signals.sort(key=lambda x: x['index'])
    
    trades = []
    position = None
    entry_signal = None
    
    for sig in all_signals:
        if position is None:
            if sig['type'] == 'top':
                position = 'short'
                entry_signal = sig
        elif position == 'short':
            if sig['type'] == 'bottom':
                entry_price = entry_signal['price']
                exit_price = sig['price']
                pnl = entry_price - exit_price
                pnl_pct = pnl / entry_price * 100
                trades.append({
                    'entry_type': '顶卖', 'entry_time': entry_signal['time'],
                    'entry_price': entry_price,
                    'exit_type': '底买', 'exit_time': sig['time'],
                    'exit_price': exit_price,
                    'pnl': round(pnl, 3), 'pnl_pct': round(pnl_pct, 3),
                    'win': pnl > 0,
                })
                position = None
                entry_signal = None
            elif sig['type'] == 'top':
                entry_signal = sig
    
    if position == 'short' and entry_signal:
        exit_price = prices[-1]
        entry_price = entry_signal['price']
        pnl = entry_price - exit_price
        pnl_pct = pnl / entry_price * 100
        trades.append({
            'entry_type': '顶卖', 'entry_time': entry_signal['time'],
            'entry_price': entry_price,
            'exit_type': '强制平仓(收盘)', 'exit_time': trends[-1]['time'],
            'exit_price': exit_price,
            'pnl': round(pnl, 3), 'pnl_pct': round(pnl_pct, 3),
            'win': pnl > 0,
        })
    
    return trades


def highlight(text, color='w'):
    """终端颜色"""
    colors = {'r': '\033[91m', 'g': '\033[92m', 'y': '\033[93m', 'b': '\033[94m', 'w': '\033[0m'}
    return f"{colors.get(color, '')}{text}{colors['w']}"


def main():
    print("=" * 70)
    print("   📊 顶底信号回测分析")
    print("=" * 70)
    print(f"   预测版参数: 左看W={W}, VR={VR}, 偏离均价≥{DEV}%")
    print(f"   特点: 纯左侧(无未来数据), 当下实时判断")
    print(f"   评估时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    all_top_results = []
    all_bottom_results = []
    all_trades = []
    stock_details = {}
    
    for stock in WATCHLIST:
        code = stock['code']
        print(f"  [{code} {stock['name']}] 获取数据...", end=' ', flush=True)
        
        data = fetch_trend(code, stock['market'])
        if not data or not data['trends']:
            print(highlight("数据为空，跳过", 'y'))
            continue
        
        trends = data['trends']
        pre_close = data['pre_close']
        print(f"获取到 {len(trends)} 根K线, 昨收={pre_close}")
        
        # 检测信号
        tops, bottoms = detect_signals(trends)
        print(f"    顶信号: {len(tops)}个, 底信号: {len(bottoms)}个")
        
        if not tops and not bottoms:
            print(f"    → 无信号，跳过回测")
            continue
        
        # 信号准确率回测
        top_res, bottom_res = backtest_signal_accuracy(trends, tops, bottoms)
        all_top_results.extend(top_res)
        all_bottom_results.extend(bottom_res)
        
        # 配对交易回测
        trades = backtest_pair_trading(trends, tops, bottoms)
        all_trades.extend([{**t, 'code': code, 'name': stock['name']} for t in trades])
        
        # 详细信息
        stock_details[code] = {
            'name': stock['name'],
            'trends_count': len(trends),
            'trends': trends,
            'tops': tops,
            'bottoms': bottoms,
            'top_results': top_res,
            'bottom_results': bottom_res,
            'trades': trades,
            'pre_close': pre_close,
            'first_price': trends[0]['price'],
            'last_price': trends[-1]['price'],
        }
    
    # ============ 汇总报告 ============
    print()
    print("=" * 70)
    print("   📈 回测结果汇总")
    print("=" * 70)
    
    # 顶信号胜率
    if all_top_results:
        print()
        print(f"  🔴 顶信号（卖空信号）准确率 - 共 {len(all_top_results)} 个信号：")
        print(f"  {'─' * 50}")
        for horizon, key in [('1分钟后', 'win_1'), ('3分钟后', 'win_3'), ('5分钟后', 'win_5'), ('至收盘', 'win_close')]:
            wins = sum(1 for r in all_top_results if r[key])
            rate = wins / len(all_top_results) * 100
            avg_drop = sum(r['max_drop_pct'] for r in all_top_results) / len(all_top_results)
            bar = '█' * int(rate / 5) + '░' * (20 - int(rate / 5))
            print(f"     {horizon:8s}: {wins}/{len(all_top_results)} = {rate:5.1f}% {bar}  平均最大跌幅: {avg_drop:.3f}%")
    
    # 底信号胜率
    if all_bottom_results:
        print()
        print(f"  🟢 底信号（买入信号）准确率 - 共 {len(all_bottom_results)} 个信号：")
        print(f"  {'─' * 50}")
        for horizon, key in [('1分钟后', 'win_1'), ('3分钟后', 'win_3'), ('5分钟后', 'win_5'), ('至收盘', 'win_close')]:
            wins = sum(1 for r in all_bottom_results if r[key])
            rate = wins / len(all_bottom_results) * 100
            avg_rise = sum(r['max_rise_pct'] for r in all_bottom_results) / len(all_bottom_results)
            bar = '█' * int(rate / 5) + '░' * (20 - int(rate / 5))
            print(f"     {horizon:8s}: {wins}/{len(all_bottom_results)} = {rate:5.1f}% {bar}  平均最大涨幅: {avg_rise:.3f}%")
    
    # 配对交易结果
    if all_trades:
        print()
        print(f"  💰 配对交易（顶卖→底买）回测 - 共 {len(all_trades)} 笔：")
        print(f"  {'─' * 50}")
        win_trades = [t for t in all_trades if t['win']]
        lose_trades = [t for t in all_trades if not t['win']]
        win_rate = len(win_trades) / len(all_trades) * 100
        total_pnl = sum(t['pnl'] for t in all_trades)
        avg_win = sum(t['pnl'] for t in win_trades) / len(win_trades) if win_trades else 0
        avg_loss = sum(t['pnl'] for t in lose_trades) / len(lose_trades) if lose_trades else 0
        
        print(f"     总交易笔数: {len(all_trades)}")
        print(f"     盈利笔数:   {highlight(str(len(win_trades)), 'g')}")
        print(f"     亏损笔数:   {highlight(str(len(lose_trades)), 'r')}")
        print(f"     胜率:       {highlight(f'{win_rate:.1f}%', 'g' if win_rate >= 50 else 'r')}")
        print(f"     总盈亏:     {highlight(f'{total_pnl:+.3f}元/股', 'g' if total_pnl > 0 else 'r')}")
        print(f"     平均盈利:   {avg_win:+.3f}元/股  |  平均亏损: {avg_loss:+.3f}元/股")
        if avg_win > 0 and avg_loss < 0:
            print(f"     盈亏比:     {abs(avg_win/avg_loss):.2f}:1")
        
        print()
        print(f"  📋 逐笔明细：")
        print(f"  {'─' * 70}")
        for i, t in enumerate(all_trades):
            mark = highlight('✓ 胜', 'g') if t['win'] else highlight('✗ 败', 'r')
            pnl_val = t['pnl']
            print(f"  {i+1:2d}. [{t['code']} {t['name']}] {t['entry_time']} {t['entry_type']}@{t['entry_price']:.2f} → "
                  f"{t['exit_time']} {t['exit_type']}@{t['exit_price']:.2f} | "
                  f"盈亏: {highlight(f'{pnl_val:+.3f}', 'g' if t['pnl']>0 else 'r')} "
                  f"({t['pnl_pct']:+.3f}%) {mark}")
    
    # ============ 每只股票详情 ============
    print()
    print("=" * 70)
    print("   📋 各股票信号详情")
    print("=" * 70)
    
    for code, detail in stock_details.items():
        print(f"\n  ┌─ [{code}] {detail['name']} ─────────────────────")
        print(f"  │ 数据: {detail['trends_count']}根K线, "
              f"昨收={detail['pre_close']:.2f}, "
              f"首价={detail['first_price']:.2f}, "
              f"末价={detail['last_price']:.2f}, "
              f"日涨跌={(detail['last_price']/detail['pre_close']-1)*100:+.2f}%")
        
        if detail['tops']:
            print(f"  │ 🔴 顶信号 ({len(detail['tops'])}个):")
            for t in detail['tops']:
                print(f"  │    {t['time']} ¥{t['price']:.2f} "
                      f"量={t['volume']}(均{t['avg_volume']:.0f}) "
                      f"偏离均价+{t.get('dev_pct',0):.2f}%")
        
        if detail['bottoms']:
            print(f"  │ 🟢 底信号 ({len(detail['bottoms'])}个):")
            for b in detail['bottoms']:
                print(f"  │    {b['time']} ¥{b['price']:.2f} "
                      f"量={b['volume']}(均{b['avg_volume']:.0f}) "
                      f"偏离均价{b.get('dev_pct',0):.2f}%")
        
        if detail['trades']:
            print(f"  │ 💰 配对交易 ({len(detail['trades'])}笔):")
            for t in detail['trades']:
                mark = '✓' if t['win'] else '✗'
                print(f"  │    {mark} {t['entry_time']} {t['entry_type']}@{t['entry_price']:.2f} -> "
                      f"{t['exit_time']} {t['exit_type']}@{t['exit_price']:.2f} "
                      f"盈亏: {t['pnl']:+.3f} ({t['pnl_pct']:+.3f}%)")
        else:
            print(f"  │ ⚠ 无配对交易（信号不足）")
    
    # ============ 算法说明 ============
    print()
    print("=" * 70)
    print("   🔬 预测版算法说明")
    print("=" * 70)
    print(f"""
    核心逻辑（纯左侧，零未来数据）：
    
    1. 【左看窗口】只看过去 {W} 根K线，判断当前是否为局部极值
       → 无需等待右侧K线，信号当下立刻发出
    
    2. 【放量确认】当前成交量 > 左边 {W}+1 根均量的 {VR} 倍
       → 有量才可能有转折，无量不报
    
    3. 【超涨超跌】偏离均价线 ≥ {DEV}% 才发信号
       → 顶信号：价格高于均价（超涨，有回落需求）
       → 底信号：价格低于均价（超跌，有反弹需求）
    
    与旧版的核心区别：
    ┌──────────────┬─────────────────┬─────────────────┐
    │              │  旧版（确认型）   │  新版（预测型）   │
    ├──────────────┼─────────────────┼─────────────────┤
    │ 窗口         │ ±4（左右各4根）  │ 左5（只看历史）  │
    │ 信号时机     │ 滞后5~8分钟     │ 当下立刻         │
    │ 假信号       │ 少              │ 较多            │
    │ 适用场景     │ 事后复盘        │ 实时交易         │
    └──────────────┴─────────────────┴─────────────────┘
    """)
    
    print("=" * 70)
    print("   回测完成")
    print("=" * 70)


if __name__ == '__main__':
    main()
