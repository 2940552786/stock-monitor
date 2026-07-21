"""回测不同VWAP斜率窗口的胜率 - 精细版"""
import requests, json

stocks = ['600519', '000779', '002881', '600795', '300750']
dates = ['20260718', '20260717', '20260716', '20260715', '20260711']

def get_data(code, date):
    mkt = 'sh' if code[0] in '69' else 'sz'
    tc = f'{mkt}{code}'
    url = f'https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={tc}&date={date}'
    r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'}, timeout=10)
    j = r.text.replace('min_data=', '').rstrip(';')
    d = json.loads(j)
    stock = d['data'].get(tc, {})
    raw = stock.get('data', {}).get('data', [])
    if not raw: return None
    trends = []; pv = 0; pa = 0
    for m in raw:
        p = m.split(' ')
        cv = int(float(p[2])); ca = float(p[3]) if len(p) >= 4 else 0
        v = cv - pv; a = ca - pa
        if v < 0: v = 0
        if a < 0: a = 0
        pv = cv; pa = ca
        trends.append({'price': float(p[1]), 'vol': v, 'amt': a})
    cv2 = 0; ca2 = 0
    for t in trends:
        cv2 += t['vol']; ca2 += t['amt']
        t['ap'] = round(ca2 / (cv2 * 100), 2) if cv2 > 0 else t['price']
    return trends

all_days = []
for code in stocks:
    for date in dates:
        trends = get_data(code, date)
        if trends:
            all_days.append({'code': code, 'date': date, 'trends': trends})

def detect_and_eval(trends, slope_win):
    prices = [t['price'] for t in trends]
    volumes = [t['vol'] for t in trends]
    avgs = [t['ap'] for t in trends]
    MAX_W = 12
    tops, bottoms = [], []
    
    for i in range(6, len(prices)):
        cp, cv, ap = prices[i], volumes[i], avgs[i]
        if cv <= 0: continue
        effW = min(MAX_W, max(3, i // 2))
        window_vols = volumes[i-effW:i+1]
        sv = sorted(window_vols)
        p75 = sv[int(len(sv)*0.75)]
        vol_ok = cv > p75 or (i>0 and volumes[i-1] > p75)
        isMax = all(prices[j] < cp for j in range(i-effW, i))
        isMin = all(prices[j] > cp for j in range(i-effW, i))
        dev = (cp-ap)/ap*100
        
        dyn_dev = 0.25
        if i >= 20:
            pds = [abs(prices[j]-avgs[j])/avgs[j]*100 for j in range(1,i+1) if avgs[j]>0]
            if pds: pds.sort(); dyn_dev = max(pds[int(len(pds)*0.8)], 0.15)
        
        vwap_slope = 0
        if i >= slope_win:
            vwap_slope = (avgs[i]-avgs[i-slope_win])/avgs[i-slope_win]*100/slope_win
        slope_limit = 0.03
        tick_slope = 0.01/ap*100/slope_win*1.5 if ap>0 else 0
        slope_limit = max(slope_limit, tick_slope)
        if i >= 20:
            chs = [abs(avgs[j]-avgs[j-1])/avgs[j-1]*100 for j in range(1,i+1) if avgs[j-1]>0]
            if chs: chs.sort(); slope_limit = max(chs[int(len(chs)*0.8)]*2, 0.015, tick_slope)
        top_slope_ok = vwap_slope < slope_limit
        bot_slope_ok = vwap_slope > -slope_limit
        
        div_ok = True
        if i >= 8:
            cd = abs(cp-ap)/ap*100
            pds2 = [abs(prices[j]-avgs[j])/avgs[j]*100 for j in range(i-8,i)]
            div_ok = cd < (sum(pds2)/len(pds2))*2.0
        
        if isMax and vol_ok and dev > dyn_dev and top_slope_ok and div_ok:
            tops.append({'index': i, 'price': cp})
        if isMin and vol_ok and dev < -dyn_dev and bot_slope_ok and div_ok:
            bottoms.append({'index': i, 'price': cp})
    
    # 直接评估
    prices2 = prices
    tw = 0
    for s in tops:
        idx = s['index']; sp = s['price']
        end = min(idx+10, len(prices2)-1)
        fm = min(prices2[idx+1:end+1]) if idx+1<=end else sp
        if (sp-fm)/sp*100 >= 0.5: tw += 1
    bw = 0
    for s in bottoms:
        idx = s['index']; sp = s['price']
        end = min(idx+10, len(prices2)-1)
        fm = max(prices2[idx+1:end+1]) if idx+1<=end else sp
        if (fm-sp)/sp*100 >= 0.5: bw += 1
    
    return len(tops), tw, len(bottoms), bw

# 跑所有天、所有窗口
results = {}
for sw in [3, 4, 5, 6, 8]:
    total_t, total_tw, total_b, total_bw = 0, 0, 0, 0
    for day in all_days:
        tn, tw, bn, bw = detect_and_eval(day['trends'], sw)
        total_t += tn; total_tw += tw; total_b += bn; total_bw += bw
    tp = total_tw/total_t*100 if total_t else 0
    bp = total_bw/total_b*100 if total_b else 0
    results[sw] = (total_t, tp, total_b, bp)

print("VWAP斜率窗口精细回测（5股×5天，未来10分钟反转>0.5%）")
print(f"{'窗口':>6} {'顶信号':>6} {'顶胜率':>8} {'底信号':>6} {'底胜率':>8} {'平均胜率':>8}")
print("-"*52)
best_sw, best_avg = 0, 0
for sw in sorted(results.keys()):
    tn, tp, bn, bp = results[sw]
    avg = (tp+bp)/2
    mark = "⭐" if avg > best_avg else ""
    print(f"{sw:>6}分 {tn:>6} {tp:>7.1f}% {bn:>6} {bp:>7.1f}% {avg:>7.1f}% {mark}")
    if avg > best_avg: best_avg = avg; best_sw = sw

# 基准：无斜率条件
total_t, total_tw, total_b, total_bw = 0, 0, 0, 0
for day in all_days:
    tn, tw, bn, bw = detect_and_eval(day['trends'], 9999)
    total_t += tn; total_tw += tw; total_b += bn; total_bw += bw
tp = total_tw/total_t*100 if total_t else 0
bp = total_bw/total_b*100 if total_b else 0
print(f"{'无斜率':>6} {total_t:>6} {tp:>7.1f}% {total_b:>6} {bp:>7.1f}% {(tp+bp)/2:>7.1f}%")
print()
print(f"✅ 最优窗口: {best_sw}分钟  平均胜率: {best_avg:.1f}%")

