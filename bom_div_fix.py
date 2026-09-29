#!/usr/bin/env python3
"""BOM 用量纠偏：把「照抄 Excel 原值（相对成品）」的用量改成「除以上一级用量」。

规则（MAK 2026-09-29 定，0073AL7011 实测验证）：
    每件用量(应写) =  Excel用量 ÷ 基数 ÷ (上一级 Excel用量 ÷ 上一级基数)
    写库：QTY = 每件用量 × 当前 QTY_BAS （基数不动，保持 Excel F）

只动满足全部条件的行（宁少勿错）：
    1) 库里 QTY/QTY_BAS 与 Excel 原值一致（= 照抄未除）
    2) 该(母件,子件)在多个成品块里除出来的值一致（Excel 无自相矛盾）
    3) 改完确有变化
跳过：其他不一致（库里被人工改过 / Excel 基准混用）、同边多值、DB 无此边。

用法：
    python3 bom_div_fix.py            # dry-run（打印 + 写备份 CSV，不写库）
    python3 bom_div_fix.py --apply    # 建备份表 + 写库 + 逐行回读校验
"""
import os
import sys
import csv
from collections import defaultdict

import openpyxl
import pymssql

sys.path.insert(0, '/home/Mak/erp-app')
for line in open('/home/Mak/erp-app/.env', encoding='utf-8'):
    l = line.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

APPLY = '--apply' in sys.argv
TS = '20260929'
XLSX = '/vol00/WDC WD20EJRX-89G3VY0/BOM_2026.9.24_.xlsx'
CSV_BACKUP = '/home/Mak/erp-app/backups/bom_div_fix_%s.csv' % TS
BAK_TABLE = 'BOM_BAK_%s_DIV' % TS


def close(a, b):
    return abs(a - b) <= max(1e-6, abs(b) * 1e-4)


# ---------- 1) Excel ----------
ws = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)['BOM 2026.9.24']
targets = {}
blockrows = []
multi = set()
odd_bas = 0
root = None
stack = {}
for i, r in enumerate(ws.iter_rows(min_row=1, max_row=22464, max_col=9, values_only=True), start=1):
    A, B, D, E, F = [(None if v is None else str(v).strip()) for v in (r[0], r[1], r[3], r[4], r[5])]
    if not B:
        continue
    if not A:
        root, stack = B, {0: (B, 1.0, 1.0)}
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
        odd_bas += 1
    stack = {k: v for k, v in stack.items() if k < lev}
    par = stack.get(lev - 1)
    if not par:
        continue
    pu = (par[1] / par[2]) if par[2] else par[1]
    unit = (e / f) / (pu if pu else 1.0)
    key = (par[0], B)
    row = {'unit': unit, 'e': e, 'f': f, 'root': root, 'lev': lev, '单位': D, 'row': i}
    if key in targets:
        if not close(targets[key]['unit'], unit):
            multi.add(key)
    else:
        targets[key] = row
    blockrows.append((key, row))
    stack[lev] = (B, e, f)
print('Excel 边键 %d；同边多值 %d；基数非数字(按1算) %d' % (len(targets), len(multi), odd_bas))

# ---------- 2) DB ----------
conn = pymssql.connect(server=os.environ['ERP_DB_HOST'], user=os.environ['ERP_DB_USER'],
                       password=os.environ['ERP_DB_PASSWORD'], database='C041', autocommit=True)
cur = conn.cursor()


def txt(b):
    if b is None:
        return ''
    if isinstance(b, str):
        b = b.encode('latin-1', 'ignore')
    try:
        return b.decode('gbk')
    except Exception:
        return b.decode('latin-1', 'ignore')


cur.execute("""SELECT CONVERT(varbinary(100), GUID), CONVERT(varbinary(100), UPGUID),
                      CONVERT(varbinary(50), PRD_NO), CAST(LEV AS INT), CAST(IDX AS INT),
                      CAST(QTY AS FLOAT), CAST(ISNULL(QTY_BAS,1) AS FLOAT)
               FROM BOM WITH (NOLOCK) WHERE LEV > 0""")
db = {}
dup_keys = []
for g_b, up_b, pn_b, lev, idx, q, qb in cur.fetchall():
    k = (txt(up_b), txt(pn_b))
    if k in db:
        dup_keys.append(k)
    db[k] = {'g': g_b, 'lev': lev, 'idx': idx, 'q': float(q or 0),
             'qb': float(qb or 1), 'up': txt(up_b), 'pn': txt(pn_b)}
print('DB LEV>0 唯一键 %d，解码后同名键 %d（同名=按字节不同的重复行）' % (len(db), len(dup_keys)))
for k in dup_keys[:6]:
    print('   同名:', repr(k))

# ---------- 3) 选目标 ----------
plan, skip = [], defaultdict(int)
cand = {}
for key, t in blockrows:
    if key in cand or key in multi:
        continue
    d = db.get(key)
    if not d:
        continue
    cur_unit = d['q'] / d['qb'] if d['qb'] else d['q']
    if close(cur_unit, t['unit']):
        continue
    if close(d['q'], t['e']) and close(d['qb'], t['f']):
        cand[key] = t
miss = [k for k in cand if not close(cand[k]['unit'], targets[k]['unit'])]
for k in miss:
    cand.pop(k)
print('按块判定：候选边 %d（剔除各块除后值不一致 %d）' % (len(cand), len(miss)))
for key, t in cand.items():
    d = db.get(key)
    if not d:
        skip['DB 无此边'] += 1
        continue
    cur_unit = d['q'] / d['qb'] if d['qb'] else d['q']
    if close(cur_unit, t['unit']):
        continue
    if key in multi:
        skip['同边多值(Excel 自相矛盾)'] += 1
        continue
    new_q = round(t['unit'] * d['qb'], 8)
    if close(new_q, d['q']):
        continue
    plan.append({'母件': key[0], '子件': key[1], '成品': t['root'], '层级': t['lev'],
                 'GUID_hex': d['g'].hex(), 'IDX': d['idx'],
                 'QTY_old': d['q'], 'QTY_BAS': d['qb'], 'QTY_new': new_q,
                 '倍数_old/new': round(d['q'] / new_q, 4) if new_q else None})

print('\n待改 %d 行（涉及 (母件,子件) 边 %d 条）' % (len(plan), len({(p['母件'], p['子件']) for p in plan})))
print('跳过：', dict(skip))
byprod = defaultdict(int)
for p in plan:
    byprod[p['成品']] += 1
print('涉及成品块 %d 个；改得最多的 10 个：%s' % (len(byprod),
      ', '.join('%s(%d)' % (k, v) for k, v in sorted(byprod.items(), key=lambda x: -x[1])[:10])))

with open(CSV_BACKUP, 'w', newline='', encoding='utf-8-sig') as fh:
    w = csv.DictWriter(fh, fieldnames=list(plan[0].keys()) if plan else ['空'])
    w.writeheader()
    w.writerows(plan)
print('备份 CSV（旧值/新值/GUID 十六进制）：%s' % CSV_BACKUP)

print('\n样例 15 条：')
for p in plan[:15]:
    print('  %-22s -> %-24s 原 %-10.6g 基数 %-6g → 新 %-10.6g (%sx)' % (
        p['母件'][:22], p['子件'][:24], p['QTY_old'], p['QTY_BAS'], p['QTY_new'], p['倍数_old/new']))

if not APPLY:
    print('\n[DRY-RUN] 未写库。加 --apply 执行。')
    raise SystemExit(0)

# ---------- 4) 备份整表 + 写库 ----------
cur.execute("SELECT COUNT(*) FROM sys.tables WHERE name=%s", (BAK_TABLE,))
if cur.fetchone()[0]:
    print('\n备份表 %s 已存在，改用追加时间戳名' % BAK_TABLE)
    BAK_TABLE = BAK_TABLE + '_B'
cur.execute('SELECT * INTO %s FROM BOM' % BAK_TABLE)
cur.execute('SELECT COUNT(*) FROM %s' % BAK_TABLE)
print('\n已备份整表 → %s（%d 行）' % (BAK_TABLE, cur.fetchone()[0]))

ok = bad = 0
for p in plan:
    # 旧值守卫：同事若同时改了这行，QTY 已变 → rowcount=0，跳过不覆盖
    cur.execute('UPDATE BOM SET QTY=%s WHERE CONVERT(varbinary(100), GUID)=%s AND CAST(QTY AS FLOAT)=%s',
                (p['QTY_new'], bytes.fromhex(p['GUID_hex']), p['QTY_old']))
    if cur.rowcount != 1:
        bad += 1
        print('  ⚠️ rowcount=%s：%s -> %s' % (cur.rowcount, p['母件'], p['子件']))
    else:
        ok += 1
print('写入：成功 %d / 异常 %d' % (ok, bad))

# ---------- 5) 回读校验 ----------
err = 0
for p in plan:
    cur.execute('SELECT CAST(QTY AS FLOAT) FROM BOM WITH (NOLOCK) WHERE CONVERT(varbinary(100), GUID)=%s',
                (bytes.fromhex(p['GUID_hex']),))
    got = cur.fetchone()
    if not got or not close(float(got[0]), p['QTY_new']):
        err += 1
        if err <= 5:
            print('  ❌ %s -> %s 期望 %s 实得 %s' % (p['母件'], p['子件'], p['QTY_new'], got))
print('回读校验：%d 行，%d 行不合格' % (len(plan), err))
print('\n下一步：重跑 bom_usage_audit3.py 对账（未除应降到 0）。')
