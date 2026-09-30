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
  ④ 本单口径 gap = 需求 − 本单可用（在途=挂本单未回+别人采购多下；+挂本单请购）− 材料仓
     —— 屏上「缺口」列用它（MAK 2026-09-30：别人的请购不能把本单需求抹成 0）
  ⑤ 品号净缺 (net_gap) = 需求 − 全厂池 − 材料仓 —— 跨单去重口径，生产仓不参与；
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

        # 独立参考实现（照文档口径自己走一遍：池只认挂本单的；材料仓/多下按顺序消耗）
        left = {}

        def ref_avail(p, demand):
            """返回 (挂本单池合计, 材料仓抵冲, 多下抵冲, 合计供给)。"""
            sp = split.get(p) or {}
            st = stock.get(p) or {}
            own = float(sp.get('po', 0)) + float(sp.get('qts', 0))
            e = left.setdefault(p, [max(0.0, float(st.get('mat_qty', 0))),
                                    max(0.0, float(sp.get('po_free', 0)))])
            need = max(0.0, demand - own)
            um = min(e[0], need)
            e[0] -= um
            us = min(e[1], need - um)
            e[1] -= us
            return own, um, us

        # 母件「实例」连线：comp 是 DFS 前序，某行的母件 = 前面最近的 depth-1 那一行（-1=成品）
        edges, stack = {}, {}
        for i, c in enumerate(comp):
            edges.setdefault(stack.get(c[5] - 1, -1), []).append(i)
            stack[c[5]] = i
        want, want_gross = {}, {}
        want_row = {}          # comp 下标 -> (需求, 合计, 缺口, 抵冲材料仓, 抵冲多下)
        fg_own, fg_um, fg_us = ref_avail(prd, remain)
        fg_avail = fg_own + fg_us          # 屏上「合计」不含材料仓（材料仓单独一列、在缺口里扣）
        fg_gap = max(0.0, remain - fg_own - fg_um - fg_us)

        def walk(parent_idx, parent_gap, gross):
            for i in edges.get(parent_idx, []):
                d = parent_gap * comp[i][6]
                g = gross * comp[i][6]
                own, um, us = ref_avail(comp[i][0], d)
                avail = own + us               # 合计不含材料仓
                g2 = max(0.0, d - own - um - us)
                want[i] = d
                want_gross[i] = g
                want_row[i] = (d, avail, g2, um, us)
                walk(i, g2, g)

        walk(-1, fg_gap, remain)

        for i, r in enumerate(api):
            rows += 1
            tag = f'{prd}/{r["prd_no"]} L{r["depth"]}'
            # 合计 = 挂本单在途 + 挂本单在单请购 + 本次抵到的别人多下
            if abs((r['qty_on_way'] + r['qty_on_odr'] + (r.get('sur_used') or 0)) - r['total_avail']) > 0.01:
                fails.append(f'{tag}: 合计 {r["total_avail"]:.1f} ≠ 挂本单在途{r["qty_on_way"]:.1f}+在单{r["qty_on_odr"]:.0f}+多下抵冲{r.get("sur_used", 0):.1f}')
            # way_free = 挂本单未回 + 别人多下（显示用）；way_others + way_free 应等于全厂在途
            way_free = r.get('way_free', r['qty_on_way'])
            if abs((r['qty_on_way'] + r.get('po_free', 0)) - way_free) > 0.01:
                fails.append(f'{tag}: 本单可用在途 {way_free:.1f} ≠ 挂本单{r["qty_on_way"]:.1f}+多下{r.get("po_free", 0):.1f}')
            if abs((r.get('way_others', 0) + way_free) - r.get('pool_way', 0)) > 0.5:
                fails.append(f'{tag}: 别人{r.get("way_others",0):.1f}+本单可用{way_free:.1f} ≠ 全厂在途{r.get("pool_way",0):.1f}')
            # 挂本单的量不能超过池子总量
            if r['qty_on_way'] > r.get('pool_way', 0) + 0.5 or r['qty_on_odr'] > r.get('pool_odr', 0) + 0.5:
                fails.append(f'{tag}: 挂本单的量超过池子总量')
            # 缺口 = 需求 − 合计 − 抵冲（抵冲 = 本次实际扣到的材料仓）
            mat_used = r.get('mat_used')
            if mat_used is None:
                mat_used = r['mat_qty']
            if abs(round(r['real_demand'] - r['total_avail'] - mat_used, 2) - r['gap']) > 0.01:
                fails.append(f'{tag}: 缺口 {r["gap"]:.1f} ≠ 需求{r["real_demand"]:.1f}-合计{r["total_avail"]:.1f}-抵冲{mat_used:.1f}')
            if mat_used > max(0.0, r['mat_qty']) + 0.01:
                fails.append(f'{tag}: {r["prd_no"]} 抵冲 {mat_used:.1f} 超过材料仓 {r["mat_qty"]:.1f}')
            # ⑤ 屏上下单口径：net_gap = 需求 − 全厂池 − 库存，且 pool_total = 池合计
            if abs(round((r.get('pool_way') or 0) + (r.get('pool_odr') or 0), 2) - r['pool_total']) > 0.01:
                fails.append(f'{tag}: 合计(池) {r["pool_total"]:.1f} ≠ 在途{r.get("pool_way")}+在单{r.get("pool_odr")}')
            want_net = max(0.0, round(r['real_demand'] - r['pool_total'] - r['mat_qty'], 2))
            if abs(want_net - r['net_gap']) > 0.01:
                fails.append(f'{tag}: 净缺口 {r["net_gap"]:.1f} ≠ 需求{r["real_demand"]:.1f}-池{r["pool_total"]:.1f}-材料仓{r["mat_qty"]:.1f}={want_net:.1f}')
            # 需求基准 = 销售未出；成品行与参考实现逐项对齐
            if r['is_fg']:
                if abs(r['real_demand'] - remain) > 0.01:
                    fails.append(f'{tag}: 成品需求 {r["real_demand"]:.1f} ≠ 销售未出 {remain:.1f}')
                if abs(r['total_avail'] - fg_avail) > 0.5:
                    fails.append(f'{tag}: 成品合计 {r["total_avail"]:.1f} ≠ 参考 {fg_avail:.1f}')
                if abs(r['gap'] - fg_gap) > 0.5:
                    fails.append(f'{tag}: 成品缺口 {r["gap"]:.1f} ≠ 参考 {fg_gap:.1f}')
            # ①② 需求 = 父件缺口×配比；毛需求 = 父件毛需求×配比；合计/缺口/抵冲 = 参考实现
            if i:
                w = want_row.get(i - 1)
                if w:
                    if abs(r['real_demand'] - w[0]) > 0.5:
                        fails.append(f'{tag}: 需求 {r["real_demand"]:.1f} ≠ 应为 {w[0]:.1f}')
                    if abs(r['total_avail'] - w[1]) > 0.5:
                        fails.append(f'{tag}: 合计 {r["total_avail"]:.1f} ≠ 应为 {w[1]:.1f}')
                    if abs(r['gap'] - w[2]) > 0.5:
                        fails.append(f'{tag}: 缺口 {r["gap"]:.1f} ≠ 应为 {w[2]:.1f}')
                    if abs((r.get('mat_used') or 0) - w[3]) > 0.5:
                        fails.append(f'{tag}: 抵冲材料仓 {r.get("mat_used", 0):.1f} ≠ 应为 {w[3]:.1f}')
                if abs(r['gross_demand'] - want_gross.get(i - 1, 0)) > 0.5:
                    fails.append(f'{tag}: 毛需求 {r["gross_demand"]:.1f} ≠ 应为 {want_gross.get(i-1,0):.1f}')
            elif abs(r['gross_demand'] - remain) > 0.01:
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
