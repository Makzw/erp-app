#!/usr/bin/env python3
"""PRDT.NAME 字节级验收：目标名(Excel 最长叙述) 是否已按 UTF-16LE 落库。只读。

判定不靠猜编码，直接比字节：
    CONVERT(varbinary(600), NAME) == 目标名.encode('utf-16le')
"""
import os
import sys
from collections import defaultdict

import openpyxl
import pymssql

for line in open('/home/Mak/erp-app/.env', encoding='utf-8'):
    l = line.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

XLSX = '/vol00/WDC WD20EJRX-89G3VY0/BOM_2026.9.24_.xlsx'
ws = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)['BOM 2026.9.24']
names = defaultdict(list)
for i, r in enumerate(ws.iter_rows(min_row=1, max_row=22464, max_col=3, values_only=True), start=1):
    A, B, C = [(None if v is None else str(v).strip()) for v in r]
    if not B or i == 1:
        continue
    if C:
        names[B].append(C)
target = {c: max(v, key=len) for c, v in names.items()}

conn = pymssql.connect(server=os.environ['ERP_DB_HOST'], user=os.environ['ERP_DB_USER'],
                       password=os.environ['ERP_DB_PASSWORD'], database='C041')
cur = conn.cursor()
cur.execute("SELECT CONVERT(varbinary(50), PRD_NO), CONVERT(varbinary(600), NAME) FROM PRDT WITH(NOLOCK)")
prdt = {bytes(pn).decode('gbk', 'replace'): bytes(nm or b'') for pn, nm in cur.fetchall() if pn}

ok = bad = missing = utf16 = dirty = 0
bad_list = []
for code, want in target.items():
    raw = prdt.get(code)
    if raw is None:
        missing += 1
        continue
    if raw == want.encode('utf-16le'):
        ok += 1
        utf16 += 1
    else:
        bad += 1
        if len(bad_list) < 25:
            bad_list.append((code, raw[:60].hex(), want))
        if b'\x00' in raw:
            utf16 += 1
        else:
            dirty += 1
print('Excel 料号 %d（成品根 %d）' % (len(target), len(names)))
print('字节级验收：符合 %d / 不符 %d / PRDT 无此料号 %d' % (ok, bad, missing))
print('  其中存储形态：UTF-16LE %d，非 UTF-16LE(脏) %d' % (utf16, dirty))
if bad_list:
    print('\n不符样例：')
    for code, hexs, want in bad_list:
        print('   %-24s raw=%s…  目标=%s' % (code[:24], hexs, want[:40]))
sys.exit(0 if bad == 0 else 2)
