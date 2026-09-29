#!/usr/bin/env python3
"""PMC 公式自检（只读，不写库）。

对 /api/pmc/preview_mps 做逐行断言，覆盖已修的坑：
  需求基准 = 销售未出（POS.QTY-QTYPS），不是订单原始数量
  ① 需求按「BOM 边」分配 —— 同一料号挂多个母件时，每一处都有各自的 父件缺口×配比
  ② 顶层带成品自身合计 —— 子件需求 = (销售未出 − 成品合计) × 配比
  ③ 在途采购 + 在单请购 = 视图 QTY_ON_WAY（VW_PO_QTY 的两个来源拆开算，和必须对得上）
     ⚠ QTY_ON_ODR 是「销售未出货量」(VW_SO_QTY)，不是请购，别拿来当供给扣
  ④ 缺口 = 需求 − 合计（合计已含在途 + 在单请购）
  ⑥ 合计 = 原材料仓 + 生产仓 + 在途采购 + 在单请购

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
        remain = M._so_line_remain(so_itm, conn)   # 销售未出
        if remain is None:
            remain = qty
        api = get(f"{BASE}/api/pmc/preview_mps?so_no_itm={so_itm}&prd_no={prd}&qty={qty}")['items']
        comp = M._mps_bom_tree(prd, conn, qty=remain)
        if len(comp) + 1 != len(api):
            fails.append(f'{prd}: 行数不一致 {len(api)} vs {len(comp)+1}')
            continue
        stock = M._v2_stock([prd] + [c[0] for c in comp], conn)
        det = M._v2_stock_detail([r['prd_no'] for r in api], conn)
        split = M._v2_odr_split([r['prd_no'] for r in api], conn)

        def avail(p):
            """独立参考实现：合计 = 原材料仓+生产仓+在途采购+在单请购（后两者按品号取）"""
            e = stock.get(p, {})
            po, qts = split.get(p, (0.0, 0.0))
            return (float(e.get('mat_qty', 0)) + float(e.get('prod_qty', 0)) + po + qts)

        edges = {}
        for i, c in enumerate(comp):
            edges.setdefault(c[4], []).append(i)
        want = {}

        def walk(parent, demand, av):
            gap = max(0.0, demand - av)
            for i in edges.get(parent, []):
                d = gap * comp[i][6]
                want[i] = d
                walk(comp[i][0], d, avail(comp[i][0]))

        walk(prd, remain, avail(prd))
        for i, r in enumerate(api):
            rows += 1
            tag = f'{prd}/{r["prd_no"]} L{r["depth"]}'
            # ⑥ 合计构成
            s = r['mat_qty'] + r['prod_qty'] + r['qty_on_way'] + r['qty_on_odr']
            if abs(s - r['total_avail']) > 0.01:
                fails.append(f'{tag}: 合计 {r["total_avail"]:.1f} ≠ 四列之和 {s:.1f}')
            # ④ 缺口 = 需求 − 合计
            if abs(round(r['real_demand'] - r['total_avail'], 2) - r['gap']) > 0.01:
                fails.append(f'{tag}: 缺口 {r["gap"]:.1f} ≠ 需求-合计 {r["real_demand"]-r["total_avail"]:.1f}')
            # 在途采购 + 在单请购 == 库存视图的 QTY_ON_WAY（拆开算的两半必须等于 ERP 原值）
            e = stock.get(r['prd_no'], {})
            view_way = float(e.get('mat_qty_on_way', 0)) + float(e.get('prod_qty_on_way', 0))
            if abs(r['qty_on_way'] + r['qty_on_odr'] - view_way) > 0.5:
                fails.append(f'{tag}: 在途{r["qty_on_way"]:.0f}+在单请购{r["qty_on_odr"]:.0f} '
                             f'≠ 视图 QTY_ON_WAY {view_way:.0f}')
            # 需求基准 = 销售未出
            if r['is_fg'] and abs(r['real_demand'] - remain) > 0.01:
                fails.append(f'{tag}: 成品需求 {r["real_demand"]:.1f} ≠ 销售未出 {remain:.1f}')
            # ①② 需求 = 父件缺口×配比（按边算的独立参考实现）
            if i and abs(want.get(i - 1, 0) - r['real_demand']) > 0.5:
                fails.append(f'{tag}: 需求 {r["real_demand"]:.1f} ≠ 应为 {want.get(i-1,0):.1f}')
    conn.close()
    print(f'检查 {len(items)} 单 / {rows} 行，失败 {len(fails)} 条')
    for f in fails[:20]:
        print('  ✗', f)
    if len(fails) > 20:
        print(f'  …其余 {len(fails)-20} 条')
    print('RESULT:', 'PASS' if not fails else 'FAIL')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
