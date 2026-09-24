"""
0073 BOM 更新脚本
用法: python3 bom_0073_dryrun.py [--live]

铁律（skill erp-bom-update）:
  - BOM 表没有 ITM 列，只有 IDX
  - UPGUID = 短格式（父料号），不用全路径
  - KND: 自制件=3, 采购件=4, 不从 PRDT 取
  - 根行(lev=None) 不能 continue，必须处理
  - NAME 用 str.encode('gbk') 传 bytes
  - 批量写入必须 autocommit=True，否则 EOF 后整批回滚
  - 写前 dry-run，--live 才真写入
"""
import openpyxl, pymssql, sys

# ── 连接 ─────────────────────────────────────────────────────────────────
DB_HOST = '39.108.237.63:11039'  # 与 main.py 保持一致
DB_USER = 'Hermes'
DB_PASS = 'aeg123456'
DB_NAME = 'C041'
EXCEL   = '/vol00/WDC WD20EJRX-89G3VY0/bom/修改关联BOM_已修改QTY_修正RM.xlsx'

# ── 辅助 ──────────────────────────────────────────────────────────────────
def g(v):
    """MSSQL GBK → Python str"""
    if v is None: return ''
    if isinstance(v, bytes):
        return v.encode('latin-1').decode('gbk', errors='replace')
    return str(v)

def gbk(s):
    """Python str → GBK bytes"""
    return s.encode('gbk')

DEPT_KND3 = {'安而固', '冲压车间', '安装车间', '托外加工'}

# ── 1. 解析 Excel ──────────────────────────────────────────────────────────
def parse_excel():
    """
    Excel 列结构：
      col[1] = 料号（含根行 BOM 代号）；子件行 col[1]=子件 PRD_NO
      col[8] = BOM 代号（子件行）；根行 col[8]=BOM 代号
      col[0] = 阶数（None=根行，1/2/3...=子件行）

    解析规则：
      - 顶层根行：lev=None 且 col[1] 以 '0073' 开头
        → 切换 cur_bom，重置 cur_root/cur_items
      - 嵌套根行（lev=None 但 col[1] 不是 0073 开头）：不切换 cur_bom，
        直接当子件处理（cur_root 已被顶层根设置，不覆盖）
      - 子件行：lev 有值，col[8] 有 BOM 代号 → 归属到 cur_bom
      - 漏行：col[8] 有 0073 代号但 lev 有值 → 归属到 cur_bom

    嵌套 BOM 场景：0073AL7230 里包含 0069HWG-HNG733307-B 的子 BOM，
    0069HWG 的子件行 col[8]=0069HWG-HNG733307-B（非 0073），这些行
    属于 0073AL7230（因为它们的 col[8] 非 0073 → active_bom 继承 cur_bom），
    但实际它们在 Excel 里是嵌套子 BOM 的内容。
    解决方案：active_bom 永远从 col[8] 读，col[8] 非 0073 则继承 cur_bom，
    cur_bom 只在顶层根行切换。
    """
    wb = openpyxl.load_workbook(EXCEL, data_only=True)
    ws = wb['Sheet1']

    bom_map = {}    # bom_no -> {'root': {}, 'items': []}
    cur_bom = None
    cur_items = []
    cur_root = None

    for row in ws.iter_rows(values_only=True):
        col1     = str(row[1] or '').strip()
        col8     = str(row[8] or '').strip()
        lev_s    = row[0]
        name     = str(row[2] or '').strip()
        qty      = row[4]
        qty_bas  = row[5]
        dept     = str(row[6] or '').strip()

        # 全空行 → 跳过
        if not col1 and not col8:
            continue

        # 判断是否是顶层根行（lev=None 且 col[1] 是 0073 开头）
        is_root_row = (lev_s is None) and col1.startswith('0073')

        # active_bom：子件行永远用 col[8]（子件行的 BOM 代号列）
        # col[8] 非 0073 → 继承 cur_bom（嵌套子 BOM 的子件行）
        if col8.startswith('0073'):
            active_bom = col8
        else:
            active_bom = cur_bom   # 继承

        # ── 顶层根行：切换 BOM ──────────────────────────────────────
        if is_root_row:
            # commit 前的 BOM
            if cur_bom and cur_items:
                bom_map[cur_bom] = {'root': cur_root, 'items': cur_items}
            cur_bom   = col1          # col[1] = BOM 代号 = 根 PRD_NO
            cur_items = []
            cur_root  = {'prd': col1, 'name': name, 'qty': qty, 'qty_bas': qty_bas}
            continue                  # 根行不追加到 items

        # ── 非根行：收集子件 ────────────────────────────────────────
        # prd = col[1]（子件料号）
        if cur_bom and col1:
            # 判断层级（lev_s 有值时用 lev_s，否则默认 1）
            try:
                lev = int(lev_s) if lev_s else 1
            except:
                lev = 1
            cur_items.append({
                'lev': lev, 'prd': col1, 'name': name,
                'qty': float(qty) if qty else 1.0,
                'qty_bas': float(qty_bas) if qty_bas else 1.0,
                'dept': dept,
            })

    # 最后 commit
    if cur_bom and cur_items:
        bom_map[cur_bom] = {'root': cur_root, 'items': cur_items}

    return bom_map

# ── 2. 构建 INSERT ────────────────────────────────────────────────────────
def build_inserts(bom_map):
    """
    模型 (A): LEV=0 GUID=PRD_NO, LEV=1 GUID=PARENT->CHILD
    IDX: 每个 BOM 内从 1 开始
    KND: 自制件(安而固/冲压车间/安装车间/托外加工)=3, 采购件=4
    """
    rows = []
    for bom_no in sorted(bom_map.keys()):
        data  = bom_map[bom_no]
        root  = data['root']
        items = data['items']
        root_prd = root['prd']

        # 根行（LEV=0）
        rows.append({
            'bom': bom_no, 'lev': 0, 'prd': root_prd,
            'guid': root_prd, 'upguid': '',
            'name': root['name'] or root_prd,
            'qty':    float(root['qty'])     if root['qty']     else 1.0,
            'qty_bas': float(root['qty_bas']) if root['qty_bas'] else 1.0,
            'knd': '3', 'idx': 0, 'dept': '',
        })

        # 子件行（LEV=1，Excel 里的 lev_s 值保留到 DB）
        for idx, item in enumerate(items, start=1):
            prd   = item['prd']
            dept  = item['dept']
            knd   = '3' if dept in DEPT_KND3 else '4'
            rows.append({
                'bom': bom_no, 'lev': item['lev'], 'prd': prd,
                'guid': f"{root_prd}->{prd}",
                'upguid': root_prd,
                'name': item['name'] or prd,
                'qty': item['qty'], 'qty_bas': item['qty_bas'],
                'knd': knd, 'idx': idx, 'dept': dept,
            })

    return rows

# ── 3. DB 写入 ──────────────────────────────────────────────────────────
def write(rows, live=False):
    conn = pymssql.connect(DB_HOST, DB_USER, DB_PASS, DB_NAME, charset='utf8')
    cur = conn.cursor()
    if live:
        conn.autocommit(False)   # 每条独立事务，避免 trigger ROLLBACK 影响后续
    else:
        print("[DRYRUN] 不写入数据库")

    ok, skip, err = 0, 0, []
    for r in rows:
        name_bytes = gbk(r['name'])
        sql = """
            INSERT INTO BOM (GUID,PRD_NO,NAME,UPGUID,LEV,KND,QTY,QTY_BAS,IDX,CON,SPC,REM)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        try:
            if live:
                cur.execute(sql, (
                    r['guid'], r['prd'], name_bytes,
                    r['upguid'] if r['upguid'] else None,
                    r['lev'], r['knd'],
                    r['qty'], r['qty_bas'],
                    r['idx'] or 0, 0, '', '',
                ))
                conn.commit()
            print(f"  {'✅' if live else '📝'} L{r['lev']} {r['prd'][:28]:28s}"
                  f" QTY={r['qty']:8.4f}  DEPT={r['dept'][:8]}")
            ok += 1
        except pymssql.IntegrityError:
            print(f"  ⏭  跳过(已存在) L{r['lev']} {r['prd'][:28]}")
            skip += 1
            if live:
                conn.rollback()
        except Exception as e:
            if live:
                conn.rollback()
            err.append(f"  ❌ L{r['lev']} {r['prd'][:28]}: {e}")

    print(f"\n写入: {ok}  跳过: {skip}  错误: {len(err)}")
    for e in err:
        print(e)

    if not live:
        conn.rollback()
        print("[ROLLBACK — dry run]")
    conn.close()

# ── 主 ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    live = '--live' in sys.argv
    print(f"{'='*60}")
    print(f"{'[LIVE]' if live else '[DRYRUN]'} 0073 BOM 更新")
    print(f"{'='*60}")

    bom_map = parse_excel()
    print(f"\n共 {len(bom_map)} 个 BOM")
    for k, v in sorted(bom_map.items()):
        print(f"\n{'─'*40}")
        print(f"BOM: {k}  ROOT: {v['root']['prd']}")
        for x in v['items']:
            print(f"  L{x['lev']:2d} {x['prd'][:28]:28s}"
                  f" QTY={x['qty']:8.4f}  DEPT={x['dept'][:8]}")

    rows = build_inserts(bom_map)
    total_items = sum(len(v['items']) for v in bom_map.values())
    print(f"\n{'═'*60}")
    print(f"总计: {len(bom_map)} BOM / {total_items} 子件行 / {len(rows)} INSERT(含根)")
    print(f"\n{'═'*60}")
    print(f"{'[LIVE] 即将写入！' if live else '[DRYRUN] 预览，加 --live 执行写入'}")
    write(rows, live=live)
