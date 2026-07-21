"""
趋势衰竭诊断：对比有无加速度过滤的信号差异
用法：python diag_exhaustion.py [股票代码] [日期]
"""
import requests, json, sys

UA = 'Mozilla/5.0'
code = sys.argv[1] if len(sys.argv) > 1 else '002881'
date = sys.argv[2] if len(sys.argv) > 2 else '20260718'
market = 'sh' if code.startswith(('6','9')) else 'sz'
tcode = f'{market}{code}'

url = f'https://ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={tcode}&date={date}'
r = requests.get(url, headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com/'}, timeout=10)
j = r.text.replace('min_data=', '').rstrip(';')
data = json.loads(j)
stock = data['data'].get(tcode, {})
raw = stock.get('data', {}).get('data', [])
if not raw:
    print('无数据')
    sys.exit(1)

trends = []
pv = pa = 0
for m in raw:
    p = m.split(' ')
    cv = int(float(p[2])); ca = float(p[3]) if len(p) >= 4 else 0
    v = cv - pv; a = ca - pa
    if v < 0: v = 0
    if a < 0: a = 0
    pv = cv; pa = ca
    trends.append({'time': p[0], 'price': float(p[1]), 'vol': v, 'amt': a})

cv2 = ca2 = 0
for t in trends:
    cv2 += t['vol']; ca2 += t['amt']
    t['ap'] = round(ca2 / (cv2 * 100), 2) if cv2 > 0 else t['price']

prices = [t['price'] for t in trends]
volumes = [t['vol'] for t in trends]
avgs = [t['ap'] for t in trends]
n = len(prices)
MAX_W = 12

print(f"{code} {date}  共{n}条K线")
print(f"{'时间':<8} {'价格':<8} {'VWAP':<8} {'偏离%':<8} {'VWAP斜率':<10} {'加速度':<10} {'old底':<6} {'new底':<6} {'被过滤原因'}")
print("-" * 90)

ACCEL_THRESHOLD = 0.005

for i in range(6, n):
    cp, cv, ap = prices[i], volumes[i], avgs[i]
    if cv <= 0: continue

    effW = min(MAX_W, max(3, i // 2))
    isMin = all(prices[j] > cp for j in range(i - effW, i))
    if not isMin:
        continue

    dev = (cp - ap) / ap * 100
    
    # dyn_dev
    dyn_dev = 0.6
    if i >= 15:
        pds = [abs(prices[j] - avgs[j]) / avgs[j] * 100 for j in range(1, i+1) if avgs[j] > 0]
        if pds: pds.sort(); dyn_dev = max(pds[int(len(pds) * 0.85)], 0.25)

    if dev > -dyn_dev:
        continue  # 不满足超跌

    # VWAP slope
    vwap_slope = 0
    if i >= 8:
        seg = avgs[i-7:i+1]
        nv = len(seg)
        xm = (nv-1)/2.0; ym = sum(seg)/nv
        num = sum((j-xm)*(seg[j]-ym) for j in range(nv))
        den = sum((j-xm)**2 for j in range(nv))
        if den != 0: vwap_slope = (num/den)/ym*100

    slope_limit = 0.03
    tick_slope = 0.01/ap*100/8*1.5 if ap>0 else 0
    slope_limit = max(slope_limit, tick_slope)
    if i >= 20:
        chs = [abs(avgs[j]-avgs[j-1])/avgs[j-1]*100 for j in range(1,i+1) if avgs[j-1]>0]
        if chs: chs.sort(); slope_limit = max(chs[int(len(chs)*0.8)]*2, 0.015, tick_slope)
    bot_slope_ok = vwap_slope > -slope_limit

    # divergence
    div_ok = True
    if i >= 8:
        cd = abs(cp-ap)/ap*100
        pds2 = [abs(prices[j]-avgs[j])/avgs[j]*100 for j in range(i-8,i)]
        div_ok = cd < (sum(pds2)/len(pds2))*2.5

    # acceleration
    vwap_accel = 0
    accel_info = ''
    if i >= 10 and avgs[i-4]>0 and avgs[i-8]>0:
        ss = (avgs[i]-avgs[i-4])/avgs[i-4]*100/4
        sl = (avgs[i-4]-avgs[i-8])/avgs[i-8]*100/4
        vwap_accel = ss - sl
        if vwap_accel < -ACCEL_THRESHOLD:
            accel_info = f'跌速加快({vwap_accel:.4f} < {-ACCEL_THRESHOLD})'

    old_bottom = bot_slope_ok and div_ok
    new_bottom = old_bottom and not (vwap_accel < -ACCEL_THRESHOLD)

    if old_bottom:  # 只要旧逻辑认为可能是底就展示
        mark_old = '✅' if old_bottom else '--'
        mark_new = '✅' if new_bottom else '❌过滤'
        reason = accel_info if old_bottom and not new_bottom else ''
        print(f"{trends[i]['time']:<8} ¥{cp:<7.2f} ¥{ap:<7.2f} {dev:>7.2f}% {vwap_slope:>9.4f}  {vwap_accel:>9.4f}  {mark_old:<6} {mark_new:<6} {reason}")
