#!/usr/bin/env python3
"""BOM 用量审计 v3（只读，不写库）—— 先在 Excel 里除出「每件用量」，再与库对账。

规则（MAK 2026-09-29 定，已由 0073AL7011 同事修复后的数据验证）：
    某边「每件用量」= (用量 ÷ 基数) ÷ (上一级用量 ÷ 上一级基数)
Excel 一条链按「相对成品」写（例：0073AL7011 = 4 / 4 / 4 / 0.0388 → 正确值 4 / 1 / 1 / 0.0097）。

可信度：同一条边(母件,子件)在多个成品块里出现，各块除完后应当得到同一个数；
        多个块算出不一致 → 说明 Excel 本身基准混用，列为「需人工确认」，不自动判对错。
输出：xlsx（3 个 sheet）+ csv（差异明细）
"""
import os
import sys
import csv
from collections import defaultdict

import openpyxl
import pymssql
from openpyxl.styles import Font, PatternFill

sys.path.insert(0, '/home/Mak/erp-app')
for line in open('/home/Mak/erp-app/.env', encoding='utf-8'):
    l = line.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

XLSX_IN = '/vol00/WDC WD20EJRX-89G3VY0/BOM_2026.9.24_.xlsx'
XLSX_OUT = '/home/Mak/erp-app/backups/BOM用量_除上一级_对照_20260929.xlsx'
CSV_OUT = '/home/Mak/erp-app/backups/bom_usage_audit_20260929.csv'


def close(a, b):
    return abs(a - b) <= max(1e-6, abs(b) * 1e-4)


# ---------- 1) Excel 解析 ----------
ws = openpyxl.load_workbook(XLSX_IN, read_only=True, data_only=True)['BOM 2026.9.24']
edges = []          # 每条边一行
by_key = defaultdict(set)   # (母件,子件) -> {除后每件用量}
blocks = 0
root = None
stack = {}
for i, r in enumerate(ws.iter_rows(min_row=1, max_row=22464, max_col=9, values_only=True), start=1):
    A, B, C, D, E, F = [(None if v is None else str(v).strip()) for v in r[:6]]
    if not B:
        continue
    if not A:
        root, blocks = B, blocks + 1
        stack = {0: (B, 1.0, 1.0)}
        continue
    try:
        lev = int(float(A))
    except Exception:
        continue
    if not root:
        continue
    try:
        e = float(E) if E else 0.0
    except Exception:
        e = 0.0
    try:
        f = float(F) if F else 1.0
    except Exception:
        f = 1.0
    stack = {k: v for k, v in stack.items() if k < lev}
    par = stack.get(lev - 1)
    if not par:
        continue
    pu = (par[1] / par[2]) if par[2] else par[1]
    unit = (e / f) / (pu if pu else 1.0)
    edges.append({'成品': root, '层级': lev, '母件': par[0], '子件': B, '单位': D,
                  '原用量': e, '基数': f, '每件用量': unit, '母件每件': pu, '行': i,
                  '同边多值': False})
    by_key[(par[0], B)].add(round(unit, 8))
    stack[lev] = (B, e, f)
print('Excel：块 %d，边 %d，边键 %d' % (blocks, len(edges), len(by_key)))

# ---------- 2) DB ----------
conn = pymssql.connect(server=os.environ['ERP_DB_HOST'], user=os.environ['ERP_DB_USER'],
                       password=os.environ['ERP_DB_PASSWORD'], database='C041')
cur = conn.cursor()
cur.execute("""SELECT CONVERT(varbinary(120), UPGUID), CONVERT(varbinary(120), PRD_NO),
                      CAST(QTY AS FLOAT), CAST(QTY_BAS AS FLOAT) FROM BOM WITH(NOLOCK) WHERE LEV>0""")
db = {}
for up_b, pn_b, q, qb in cur.fetchall():
    try:
        db[(up_b.decode('gbk'), pn_b.decode('gbk'))] = (float(q or 0), float(qb or 0))
    except Exception:
        pass
print('DB：边 %d' % len(db))

# ---------- 3) 判定 ----------
stat = defaultdict(int)
bad = defaultdict(list)
diff_rows = []
for r in edges:
    key = (r['母件'], r['子件'])
    r['同边多值'] = len(by_key[key]) > 1
    d = db.get(key)
    if not d:
        r['判定'] = 'DB 无此边'
        r['库每件'] = None
        stat['DB 无此边'] += 1
        continue
    q, qb = d
    r['库量'], r['库基数'] = q, qb
    r['库每件'] = (q / qb) if qb else q
    if close(r['库每件'], r['每件用量']):
        r['判定'] = '一致'
        stat['一致'] += 1
    elif close(r['库量'], r['原用量']) and close(r['库基数'], r['基数']):
        r['判定'] = '未除上一级' if not r['同边多值'] else '未除上一级(同边多值)'
        stat[r['判定']] += 1
    else:
        r['判定'] = '其他不一致'
        stat['其他不一致'] += 1
    if r['判定'] != '一致':
        bad[r['成品']].append(r)
        diff_rows.append(r)
print('\n=== 判定 ===')
for k in sorted(stat):
    print('  %-22s %d' % (k, stat[k]))
print('  受影响成品数：%d' % len(bad))
diff_rows.sort(key=lambda r: (r['判定'], r['成品'], r['层级']))

# ---------- 4) 输出 ----------
wb = openpyxl.Workbook()
hdr = ['成品', '层级', '母件', '子件', '单位', 'Excel原用量', 'Excel基数', '修正后每件用量(除上一级)',
       '库用量', '库基数', '库每件', '倍数(库/修正)', '判定', 'Excel行']
sh = wb.active
sh.title = '全部边-修正后对照'
sh.append(hdr)
for r in sorted(edges, key=lambda x: (x['成品'], x['层级'], x['母件'], x['子件'])):
    mult = (r['库每件'] / r['每件用量']) if (r.get('库每件') is not None and r['每件用量']) else None
    sh.append([r['成品'], r['层级'], r['母件'], r['子件'], r['单位'], r['原用量'], r['基数'],
               round(r['每件用量'], 6), r.get('库量'), r.get('库基数'),
               None if r.get('库每件') is None else round(r['库每件'], 6),
               None if mult is None else round(mult, 4), r['判定'], r['行']])
sh2 = wb.create_sheet('需改-差异明细')
sh2.append(hdr)
for r in diff_rows:
    mult = (r.get('库每件') / r['每件用量']) if (r.get('库每件') is not None and r['每件用量']) else None
    sh2.append([r['成品'], r['层级'], r['母件'], r['子件'], r['单位'], r['原用量'], r['基数'],
                round(r['每件用量'], 6), r.get('库量'), r.get('库基数'),
                None if r.get('库每件') is None else round(r['库每件'], 6),
                None if mult is None else round(mult, 4), r['判定'], r['行']])
sh3 = wb.create_sheet('按成品汇总')
sh3.append(['成品', '差异边数', '其中未除', '其中其他', '最大倍数'])
for code, items in sorted(bad.items()):
    und = sum(1 for r in items if r['判定'].startswith('未除'))
    oth = sum(1 for r in items if r['判定'] == '其他不一致')
    worst = max([abs(r['库每件'] / r['每件用量']) for r in items
                 if r.get('库每件') is not None and r['每件用量']] or [0])
    sh3.append([code, len(items), und, oth, round(worst, 3)])
for s in (sh, sh2, sh3):
    for c in s[1]:
        c.font = Font(bold=True)
    s.freeze_panes = 'A2'
red = PatternFill('solid', fgColor='F8D7DA')
for row in sh2.iter_rows(min_row=2):
    if str(row[12].value).startswith('未除'):
        for c in row:
            c.fill = red
wb.save(XLSX_OUT)

with open(CSV_OUT, 'w', newline='', encoding='utf-8-sig') as fh:
    w = csv.writer(fh)
    w.writerow(['成品', '层级', '母件', '子件', '单位', 'Excel原用量', 'Excel基数', '修正后每件用量',
                '库用量', '库基数', '库每件', '倍数(库/修正)', '判定'])
    for r in diff_rows:
        mult = (r.get('库每件') / r['每件用量']) if (r.get('库每件') is not None and r['每件用量']) else None
        w.writerow([r['成品'], r['层级'], r['母件'], r['子件'], r['单位'], r['原用量'], r['基数'],
                    round(r['每件用量'], 6), r.get('库量'), r.get('库基数'),
                    None if r.get('库每件') is None else round(r['库每件'], 6),
                    None if mult is None else round(mult, 4), r['判定']])
print('\n对照表：%s' % XLSX_OUT)
print('差异明细：%s（%d 行）' % (CSV_OUT, len(diff_rows)))

print('\n=== 偏差最大的 12 个成品 ===')
ranked = []
for code, items in bad.items():
    worst = max([abs(r['库每件'] / r['每件用量']) for r in items
                 if r.get('库每件') is not None and r['每件用量']] or [0])
    ranked.append((worst, code, items))
ranked.sort(reverse=True)
for worst, code, items in ranked[:12]:
    print('\n● %-22s 最大 %7.3gx  %d 条' % (code, worst, len(items)))
    for r in items[:4]:
        print('    L%d %-22s -> %-24s 库每件=%-9.4g 应为=%-9.4g %s' % (
            r['层级'], r['母件'][:22], r['子件'][:24],
            -1 if r.get('库每件') is None else r['库每件'], r['每件用量'], r['判定']))
