#!/usr/bin/env python3
"""按 BOM Excel 的「叙述」更新 PRDT.NAME（只改 NAME，同料号取最长叙述）。

规则（MAK 2026-09-29 定）：
  - 基准 = BOM_2026.9.24_.xlsx C 列「叙述」；同一料号出现多次 → 取**最长**那条
  - 只改 PRDT.NAME，其他字段一律不碰
  - 库中无该料号 → 跳过（实测 0 条）

存储姿势（实测，很关键）：
  PRDT.NAME 是 nvarchar(500)：
    ✅ 参数传 Python str         → 落库为 UTF-16LE（跟正常行一致，桌面端/App 都正常）
    ❌ 参数传 bytes(gbk/utf-8)   → 原始字节塞进 nvarchar（历史脏行就是这么来的，桌面端乱码）
  读取：用 CONVERT(varbinary(600), NAME) 再自己判编码（直读/CAST 对脏行会抛 UnicodeDecodeError）。

用法：
  python3 prdt_name_sync.py            # dry-run（打印 + 写备份 CSV，不写库）
  python3 prdt_name_sync.py --apply    # 备份整表 + 写库 + 逐行回读校验
"""
import os
import sys
import csv
from collections import defaultdict

import openpyxl
import pymssql

for line in open('/home/Mak/erp-app/.env', encoding='utf-8'):
    l = line.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

APPLY = '--apply' in sys.argv
TS = os.popen('date +%Y%m%d_%H%M%S').read().strip()
XLSX = '/vol00/WDC WD20EJRX-89G3VY0/BOM_2026.9.24_.xlsx'
CSV_BACKUP = '/home/Mak/erp-app/backups/prdt_name_sync_%s.csv' % TS
BAK_TABLE = 'PRDT_BAK_%s_NAME' % TS


def dec(raw):
    """nvarchar 列取值：正常行 UTF-16LE（含 NUL），历史脏行是原始 GBK 字节。"""
    b = bytes(raw or b'')
    if b'\x00' in b:
        try:
            return b.decode('utf-16le').rstrip('\x00')
        except Exception:
            pass
    try:
        return b.decode('gbk')
    except Exception:
        return b.decode('latin-1', 'replace')


def is_utf16(raw):
    return b'\x00' in bytes(raw or b'')


# ---------- 1) Excel：同料号取最长叙述 ----------
ws = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)['BOM 2026.9.24']
names = defaultdict(list)
roots = set()
for i, r in enumerate(ws.iter_rows(min_row=1, max_row=22464, max_col=3, values_only=True), start=1):
    A, B, C = [(None if v is None else str(v).strip()) for v in r]
    if not B or i == 1:
        continue
    if not A:
        roots.add(B)
    if C:
        names[B].append(C)
target = {code: max(lst, key=len) for code, lst in names.items()}
print('Excel：有叙述的料号 %d（多条不同叙述取最长 %d），成品根 %d'
      % (len(target), sum(1 for v in names.values() if len(set(v)) > 1), len(roots)))

# ---------- 2) PRDT ----------
conn = pymssql.connect(server=os.environ['ERP_DB_HOST'], user=os.environ['ERP_DB_USER'],
                       password=os.environ['ERP_DB_PASSWORD'], database='C041', autocommit=True)
cur = conn.cursor()
cur.execute("SELECT CONVERT(varbinary(50), PRD_NO), CONVERT(varbinary(600), NAME) FROM PRDT WITH(NOLOCK)")
prdt = {}
for pn, nm in cur.fetchall():
    if pn is None:
        continue
    prdt[bytes(pn).decode('gbk', 'replace')] = (pn, bytes(nm or b''))
print('PRDT：%d 行（其中 nvarchar 正常存储 %d / 历史脏存储 %d）'
      % (len(prdt), sum(1 for _, (_, raw) in prdt.items() if is_utf16(raw)),
         sum(1 for _, (_, raw) in prdt.items() if not is_utf16(raw))))

# ---------- 3) 选目标 ----------
plan, skipped = [], defaultdict(int)
for code, new_name in target.items():
    p = prdt.get(code)
    if not p:
        skipped['PRDT 无此料号'] += 1
        continue
    pn_b, raw = p
    old = dec(raw)
    kind = None
    if old.strip() != new_name.strip():
        kind = '改名字'
    elif not is_utf16(raw):
        kind = '仅修存储形态(文字相同)'
    if not kind:
        continue
    plan.append({'料号': code, 'PRD_NO_hex': bytes(pn_b).hex(), '类型': kind,
                 '旧名': old, '新名': new_name, '旧长': len(old), '新长': len(new_name),
                 '是成品根': code in roots})
kinds = defaultdict(int)
for x in plan:
    kinds[x['类型']] += 1
print('\n待改 %d 行：%s（其中成品根 %d）' % (len(plan), dict(kinds), sum(1 for x in plan if x['是成品根'])))
print('跳过：', dict(skipped))
print('  新名比旧名长 %d / 短 %d' % (sum(1 for x in plan if x['新长'] > x['旧长']),
                                sum(1 for x in plan if x['新长'] < x['旧长'])))

with open(CSV_BACKUP, 'w', newline='', encoding='utf-8-sig') as fh:
    w = csv.DictWriter(fh, fieldnames=list(plan[0].keys()) if plan else ['空'])
    w.writeheader()
    w.writerows(plan)
print('备份 CSV：%s' % CSV_BACKUP)
print('\n样例 12 条：')
for x in plan[:12]:
    print('  [%s] %-22s\n     旧(%d): %s\n     新(%d): %s'
          % (x['类型'], x['料号'][:22], x['旧长'], x['旧名'][:70], x['新长'], x['新名'][:70]))

if not APPLY:
    print('\n[DRY-RUN] 未写库。加 --apply 执行。')
    raise SystemExit(0)

# ---------- 4) 备份 + 写库 ----------
cur.execute("SELECT COUNT(*) FROM sys.tables WHERE name=%s", (BAK_TABLE,))
if cur.fetchone()[0]:
    BAK_TABLE += '_B'
cur.execute('SELECT * INTO %s FROM PRDT' % BAK_TABLE)
cur.execute('SELECT COUNT(*) FROM %s' % BAK_TABLE)
print('\n已备份整表 → %s（%d 行）' % (BAK_TABLE, cur.fetchone()[0]))

ok = bad = 0
for x in plan:
    # 只改 NAME；参数传 str（nvarchar 正确存储）
    cur.execute('UPDATE PRDT SET NAME=%s WHERE CONVERT(varbinary(50), PRD_NO)=%s',
                (x['新名'], bytes.fromhex(x['PRD_NO_hex'])))
    if cur.rowcount != 1:
        bad += 1
        if bad <= 10:
            print('  ⚠️ rowcount=%s：%s' % (cur.rowcount, x['料号']))
    else:
        ok += 1
print('写入：成功 %d / 异常 %d' % (ok, bad))

err = 0
for x in plan:
    cur.execute('SELECT CONVERT(varbinary(600), NAME) FROM PRDT WITH(NOLOCK) WHERE CONVERT(varbinary(50), PRD_NO)=%s',
                (bytes.fromhex(x['PRD_NO_hex']),))
    got = cur.fetchone()
    raw = bytes(got[0]) if got and got[0] else b''
    if dec(raw).strip() != x['新名'].strip() or not is_utf16(raw):
        err += 1
        if err <= 5:
            print('  ❌ %s 期望「%s」实得「%s」(UTF16=%s)' % (x['料号'], x['新名'][:40], dec(raw)[:40], is_utf16(raw)))
print('回读校验：%d 行，%d 行不合格' % (len(plan), err))
print('\n下一步：重跑 prdt_vs_bomxl.py，名称不符应为 0。')
