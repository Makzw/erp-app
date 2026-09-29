#!/usr/bin/env python3
"""PMC 公式自检（只读，不写库）。

对 /api/pmc/preview_mps 做三条不变量断言，覆盖三个已修的坑：
  ① 需求按「BOM 边」分配 —— 同一料号挂多个母件时，每一处都要有各自的 父件缺口×配比
  ② 顶层要带成品自身库存 —— 子件需求 = (整单量 - 成品库存) × 配比
  ③ 在单请购 = 各仓之和（不是排序后第一行）

用法：cd /home/Mak/erp-app && python3 pmc_formula_check.py [单号数量，默认全量]
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
        prd, so_itm, qty = it['prd_no'], it['so_no_itm'], float(it['qty'])
        api = get(f"{BASE}/api/pmc/preview_mps?so_no_itm={so_itm}&prd_no={prd}&qty={qty}")['items']
        comp = M._mps_bom_tree(prd, conn, qty=qty)
        if len(comp) + 1 != len(api):
            fails.append(f'{prd}: 行数不一致 {len(api)} vs {len(comp)+1}')
            continue
        stock = M._v2_stock([prd] + [c[0] for c in comp], conn)
        det = M._v2_stock_detail([r['prd_no'] for r in api], conn)

        def sm(p):
            e = stock.get(p, {})
            return float(e.get('mat_qty', 0)) + float(e.get('prod_qty', 0))

        edges = {}
        for i, c in enumerate(comp):
            edges.setdefault(c[4], []).append(i)
        want = {}

        def walk(parent, demand, stk):
            gap = max(0.0, demand - stk)
            for i in edges.get(parent, []):
                d = gap * comp[i][6]
                want[i] = d
                walk(comp[i][0], d, sm(comp[i][0]))

        walk(prd, qty, sm(prd))
        for i, r in enumerate(api):
            rows += 1
            if abs(round(r['real_demand'] - r['total_stock'], 2) - r['gap']) > 0.01:
                fails.append(f'{prd}/{r["prd_no"]}: 缺口≠需求-合计')
            if abs(sum(x[5] for x in det.get(r['prd_no'], [])) - r['qty_on_odr']) > 0.5:
                fails.append(f'{prd}/{r["prd_no"]}: 在单请购≠各仓之和')
            if i and abs(want.get(i - 1, 0) - r['real_demand']) > 0.5:
                fails.append(f'{prd}/{r["prd_no"]} L{r["depth"]}: 需求 {r["real_demand"]:.1f} ≠ 应为 {want.get(i-1,0):.1f}')
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
