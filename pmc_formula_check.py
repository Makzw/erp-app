#!/usr/bin/env python3
"""PMC 公式自检（只读，不写库）。

对 /api/pmc/preview_mps 做逐行断言，覆盖已修的坑：
  需求基准 = 销售未出（VW_POS.QTY − SAQTY，= v2 视图 QTY_ON_ODR 的口径），不是订单原始数量
  ① 需求按「BOM 边（母件实例→子件实例）」分配 —— 同一料号挂多个母件时各自算
  ② MRP 净需求展开：子件需求 = 父件缺口 × 配比，且父件缺口 = 屏上同一个数
     毛需求（gross_demand）= 父件毛需求 × 配比，起点 = 销售未出（不扣任何料）
     （逐层扣该层自己的池+材料仓 → 中间件有货就不往下要料）
  ③ 在途采购 / 在单请购的**全厂池**（po_all/qts_all）参与扣减；挂本单的量(po/qts)
     只用于屏上 tooltip，且 ≤ 池子总量
     ⚠ QTY_ON_ODR 是「销售未出货量」(VW_SO_QTY)，不是请购，别拿来当供给扣
  ④ 合计(池) = pool_way + pool_odr；本单口径 gap = 需求 − 挂本单的料（只作对照）
  ⑤ 屏上「缺口」(net_gap) = 需求 − 全厂池 − 材料仓 —— 下单口径，生产仓不参与；
     就是「昨天买了 7000，今天只该买 3000」；品号汇总的净缺口用同一算式

用法：cd /home/Mak/erp-app && python3 pmc_formula_check.py [单数，默认全量]
退出码 0 = 全过；1 = 有断言失败。
"""
import os, sys, json, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for line in open('.env', encoding='utf-8'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")
import main as M

BASE = 'http://127.0.0.1:8001'
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 0


def get(url):
    return json.loads(urllib.request.urlopen(url, timeout=180).read())


def main():
    items = get(f'{BASE}/api/pmc/pos_unanalyzed')['items']
    if LIMIT:
        items = items[:LIMIT]
    conn = M.get_conn()
    fails = []
    rows = 0
    for it in items:
        prd, so_itm = it['prd_no'], it['so_no_itm']
        qty = float(it['qty'])
        remain, ref = M._so_line_info(so_itm, conn)
        if remain is None:
            remain = qty
        api = get(f"{BASE}/api/pmc/preview_mps?so_no_itm={so_itm}&prd_no={prd}&qty={qty}")['items']
        comp = M._mps_bom_tree(prd, conn, qty=remain)
        if len(comp) + 1 != len(api):
            fails.append(f'{prd}: 行数不一致 {len(api)} vs {len(comp)+1}')
            continue
        stock = M._v2_stock([prd] + [c[0] for c in comp], conn)
        det = M._v2_stock_detail([r['prd_no'] for r in api], conn)
        split = M._v2_odr_split([r['prd_no'] for r in api], conn, ref=ref, fg=prd, so_itm=so_itm)

        def net_avail(p):
            """独立参考实现：参与扣减的供给 = 全厂池(在途采购 po_all + 在单请购 qts_all) + 材料仓。
            生产仓(prod_qty)已被领走，不参与；本单专属量(po/qts)只用于屏上 tooltip。"""
            sp = split.get(p) or {}
            st = stock.get(p) or {}
            return (float(sp.get('po_all', 0)) + float(sp.get('qts_all', 0))
                    + float(st.get('mat_qty', 0)))

        # 母件「实例」连线：comp 是 DFS 前序，某行的母件 = 前面最近的 depth-1 那一行（-1=成品）
        edges, stack = {}, {}
        for i, c in enumerate(comp):
            edges.setdefault(stack.get(c[5] - 1, -1), []).append(i)
            stack[c[5]] = i
        want, want_gross = {}, {}

        def walk(parent_idx, demand, av, gross):
            gap = max(0.0, demand - av)
            for i in edges.get(parent_idx, []):
                d = gap * comp[i][6]
                g = gross * comp[i][6]
                want[i] = d
                want_gross[i] = g
                walk(i, d, net_avail(comp[i][0]), g)

        walk(-1, remain, net_avail(prd), remain)
        for i, r in enumerate(api):
            rows += 1
            tag = f'{prd}/{r["prd_no"]} L{r["depth"]}'
            # 合计 = 挂本单的在途 + 在单请购（库存不分摊，不进合计）
            if abs((r['qty_on_way'] + r['qty_on_odr']) - r['total_avail']) > 0.01:
                fails.append(f'{tag}: 合计 {r["total_avail"]:.1f} ≠ 挂本单在途{r["qty_on_way"]:.0f}+在单{r["qty_on_odr"]:.0f}')
            # 挂本单的量不能超过池子总量
            if r['qty_on_way'] > r.get('pool_way', 0) + 0.5 or r['qty_on_odr'] > r.get('pool_odr', 0) + 0.5:
                fails.append(f'{tag}: 挂本单的量超过池子总量')
            # 缺口 = 需求 − 合计（本单口径，保留给屏上对照）
            if abs(round(r['real_demand'] - r['total_avail'], 2) - r['gap']) > 0.01:
                fails.append(f'{tag}: 缺口 {r["gap"]:.1f} ≠ 需求-合计 {r["real_demand"]-r["total_avail"]:.1f}')
            # ⑤ 屏上下单口径：net_gap = 需求 − 全厂池 − 库存，且 pool_total = 池合计
            if abs(round((r.get('pool_way') or 0) + (r.get('pool_odr') or 0), 2) - r['pool_total']) > 0.01:
                fails.append(f'{tag}: 合计(池) {r["pool_total"]:.1f} ≠ 在途{r.get("pool_way")}+在单{r.get("pool_odr")}')
            want_net = max(0.0, round(r['real_demand'] - r['pool_total'] - r['mat_qty'], 2))
            if abs(want_net - r['net_gap']) > 0.01:
                fails.append(f'{tag}: 净缺口 {r["net_gap"]:.1f} ≠ 需求{r["real_demand"]:.1f}-池{r["pool_total"]:.1f}-材料仓{r["mat_qty"]:.1f}={want_net:.1f}')
            # 需求基准 = 销售未出
            if r['is_fg'] and abs(r['real_demand'] - remain) > 0.01:
                fails.append(f'{tag}: 成品需求 {r["real_demand"]:.1f} ≠ 销售未出 {remain:.1f}')
            # ①② 需求(净) = 父件缺口×配比；毛需求 = 父件毛需求×配比（独立参考实现按边重算）
            if i and abs(want.get(i - 1, 0) - r['real_demand']) > 0.5:
                fails.append(f'{tag}: 需求 {r["real_demand"]:.1f} ≠ 应为 {want.get(i-1,0):.1f}')
            if i and abs(want_gross.get(i - 1, 0) - r['gross_demand']) > 0.5:
                fails.append(f'{tag}: 毛需求 {r["gross_demand"]:.1f} ≠ 应为 {want_gross.get(i-1,0):.1f}')
            if not i and abs(r['gross_demand'] - remain) > 0.01:
                fails.append(f'{tag}: 成品毛需求 {r["gross_demand"]:.1f} ≠ 销售未出 {remain:.1f}')
    conn.close()
    # ⑥ 品号汇总自洽：net = max(0, need − pool − stock)
    try:
        summ = get(f'{BASE}/api/pmc/prd_summary')
        for r in (summ.get('items') or []):
            want_net = max(0.0, round(r['need'] - r.get('pool', 0) - r.get('mat', 0), 2))
            if abs(want_net - r['net']) > 0.01:
                fails.append(f"汇总 {r['prd_no']}: 净缺口 {r['net']:.1f} ≠ 需求{r['need']:.1f}-池{r.get('pool',0):.1f}-材料仓{r.get('mat',0):.1f}={want_net:.1f}")
    except Exception as e:
        print('（品号汇总自洽检查跳过：%s）' % e)
    print(f'检查 {len(items)} 单 / {rows} 行，失败 {len(fails)} 条')
    for f in fails[:20]:
        print('  ✗', f)
    if len(fails) > 20:
        print(f'  …其余 {len(fails)-20} 条')
    print('RESULT:', 'PASS' if not fails else 'FAIL')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
