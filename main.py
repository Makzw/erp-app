"""
安而固 ERP — 请购单手机端
FastAPI 后端 + 响应式 SPA
"""
import pymssql
import re
import time
from datetime import datetime, timedelta
from fastapi import FastAPI, Query, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
from datetime import datetime

# ── DB 连接 ──────────────────────────────────────────────────────────────────
def get_conn(db: str = "C041"):
    return pymssql.connect(
        server="39.108.237.63:11039",
        user="Hermes",
        password="aeg123456",
        database=db,
        charset="utf8",
    )

def g(v) -> str:
    """GBK 字节 → str 或 None → ''
    坑：QTS 的 varchar 列（PRD_NO/PRD_NAME 等）存 GBK 字节，
    pymssql 无 charset 时读出为 latin-1 形态的 str（如 'Æ½Í·'）。
    必须 .encode('latin-1').decode('gbk') 转回中文。
    nvarchar 列（CUS_NAME 等）已是真 Unicode，latin-1 编码会失败 → 原样返回。
    """
    if v is None:
        return ""
    if isinstance(v, bytes):
        try:
            return v.encode("latin-1").decode("gbk", errors="replace")
        except Exception:
            return str(v)
    if hasattr(v, "strftime"):  # datetime
        return v.strftime("%Y-%m-%d")
    s = str(v)
    try:
        return s.encode("latin-1").decode("gbk", errors="replace")
    except Exception:
        return s

def col_zh(name) -> str:
    """中文字段名 → GBK bytes → decode via pymssql"""
    return name

def f(v) -> Optional[float]:
    """安全转 float"""
    try:
        return float(v)
    except Exception:
        return None


def from_hex_vw(v):
    """VW_STOCK_DETAIL2 VARBINARY: WH仓码用 GBK/Latin-1"""
    if v is None: return ''
    if isinstance(v, str): return v
    b = bytes(v) if not isinstance(v, bytes) else v
    # GBK: 中文仓码（地面/臻彩等）
    try:
        s = b.decode('gbk')
        if any(0x4e00 <= ord(c) <= 0x9fff for c in s): return s
    except: pass
    # Latin-1: ASCII 短码（D2-21/4/8等）
    try: return b.decode('latin-1')
    except: return b.decode('utf-8', errors='replace')


def from_hex_wh_name(v):
    """VW_STOCK_DETAIL2 WH_Name: UTF-16-LE优先（中文仓名），GBK次之，Latin-1兜底"""
    if v is None: return ''
    if isinstance(v, str): return v
    b = bytes(v) if not isinstance(v, bytes) else v
    if b'\x00' in b:  # UTF-16-LE 编码（有\x00字节）
        try: return b.decode('utf-16-le')
        except: pass
    # GBK: 中文仓码（原材料仓/车间仓等）
    try:
        s = b.decode('gbk')
        if any(0x4e00 <= ord(c) <= 0x9fff for c in s): return s
    except: pass
    # Latin-1: ASCII 混合文本
    try: return b.decode('latin-1')
    except: return b.decode('utf-8', errors='replace')

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="安而固 ERP — 请购单", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ── 首页 ──────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


# ── API: 未采购行列表（扁平，不分单）────────────────────────────────────────
# GET /api/qts?usr=0012&page=1
@app.get("/api/qts")
async def list_qts(
    usr: str = Query(default="0012", description="制单人员"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    filter: str = Query(default="pending", description="pending | all"),
    prd_no: str = Query(default=""),
    supplier: str = Query(default=""),
    order_no: str = Query(default=""),
):
    conn = get_conn()
    cur = conn.cursor()

    # filter=pending: UP IS NULL 且 CLS_ID=0（未确认单价 + 未结案）
    # filter=all:     不限
    up_filter = "AND (q.UP IS NULL OR q.UP = 0) AND q.CLS_ID = 0" if filter == "pending" else "AND q.CLS_ID = 0"

    # 供应商：OUTER APPLY 取该品号历史上最新的采购供应商和单价
    cur.execute(f"""
        SELECT
            q.QT_NO,
            q.ITM,
            q.QT_DD,
            q.PRD_NO,
            q.PRD_NAME,
            q.SPC,
            q.UT,
            q.QTY,
            q.UP,
            q.AMT,
            q.EST_DD,
            q.指令单号,
            q.紧急程度,
            CAST(q.CLS_ID AS INT) as CLS_ID,
            lp.CUS_NAME as last_supplier,
            lp.OS_NO as PO_NO,
            lp.UP as last_up
        FROM QTS q WITH (NOLOCK)
        OUTER APPLY (
            SELECT TOP 1 OS_NO, CUS_NAME, UP
            FROM POS WITH (NOLOCK)
            WHERE PRD_NO = q.PRD_NO
            ORDER BY OS_DD DESC
        ) lp
        WHERE q.USR = %s
          {up_filter}
          AND ISNULL(q.删除, 0) = 0
          {"AND q.PRD_NO LIKE %s" if prd_no else ""}
          {"AND q.指令单号 LIKE %s" if order_no else ""}
          {"AND EXISTS (SELECT 1 FROM POS lp2 WITH(NOLOCK) WHERE lp2.PRD_NO = q.PRD_NO AND lp2.CUS_NAME LIKE %s)" if supplier else ""}
        ORDER BY q.QT_DD DESC, q.QT_NO DESC, q.ITM
        OFFSET %s ROWS FETCH NEXT %s ROWS ONLY
    """, (usr, (page - 1) * page_size, page_size)
          + ((f"%{prd_no}%",) if prd_no else ())
          + ((f"%{order_no}%",) if order_no else ())
          + ((f"%{supplier}%",) if supplier else ()))

    rows = cur.fetchall()
    COLS = {c[0]: i for i, c in enumerate(cur.description)}

    # 总数
    cur.execute(f"""
        SELECT COUNT(*)
        FROM QTS q WITH (NOLOCK)
        WHERE q.USR = %s
          {up_filter}
          AND ISNULL(q.删除, 0) = 0
          {"AND q.PRD_NO LIKE %s" if prd_no else ""}
          {"AND q.指令单号 LIKE %s" if order_no else ""}
          {"AND EXISTS (SELECT 1 FROM POS lp3 WITH(NOLOCK) WHERE lp3.PRD_NO = q.PRD_NO AND lp3.CUS_NAME LIKE %s)" if supplier else ""}
    """, (usr,)
          + ((f"%{prd_no}%",) if prd_no else ())
          + ((f"%{order_no}%",) if order_no else ())
          + ((f"%{supplier}%",) if supplier else ()))
    total = cur.fetchone()[0]

    conn.close()

    items = []
    for r in rows:
        items.append({
            "qt_no":     g(r[COLS["QT_NO"]]),
            "itm":       r[COLS["ITM"]],
            "qt_dd":     g(r[COLS["QT_DD"]]),
            "prd_no":    g(r[COLS["PRD_NO"]]),
            "prd_name":  g(r[COLS["PRD_NAME"]]),
            "spc":       g(r[COLS["SPC"]]),
            "ut":        g(r[COLS["UT"]]),
            "qty":       f(r[COLS["QTY"]]),
            "up":        f(r[COLS["UP"]]),
            "amt":       f(r[COLS["AMT"]]),
            "est_dd":    g(r[COLS["EST_DD"]]),
            "order_no":  g(r[COLS["指令单号"]]),
            "urgency":   g(r[COLS["紧急程度"]]),
            "cls_id":    bool(r[COLS["CLS_ID"]]),
            "supplier":  g(r[COLS["last_supplier"]]),
            "po_no":     g(r[COLS["PO_NO"]]),
            "last_up":   f(r[COLS["last_up"]]),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


# ── API: 单张明细 ─────────────────────────────────────────────────────────────
# GET /api/qts/{qt_no}
@app.get("/api/qts/{qt_no}")
async def get_qts(qt_no: str):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT QT_NO, QT_ID, CUS_NO, CUS_NAME, QT_DD, SAL_NO, SAL_NAME,
               USR, CHK_MAN, REM, ACCESSORY, USABLE, CLS_ID, 删除,
               PRD_NO, PRD_NAME, SPC, UT, QTY, UP, AMT,
               EST_DD, FLD1, FLD2, FLD3, REF_ITM, SO_NO_ITM,
               指令单号, 成品编号, 紧急程度, 订单数量, ITM
        FROM QTS WITH (NOLOCK)
        WHERE QT_NO = %s
        ORDER BY ITM
    """, (qt_no,))

    rows = cur.fetchall()
    conn.close()

    if not rows:
        return {"error": "单据不存在"}, 404

    # 用普通cursor：按列名访问，避免索引错误
    def row_val(r, cols, name, idx):
        v = r[idx]
        return v

    COL = {c: i for i, c in enumerate([c[0] for c in cur.description])}

    header = {
        "qt_no":    g(rows[0][COL["QT_NO"]]),
        "qt_id":    g(rows[0][COL["QT_ID"]]),
        "cus_no":   g(rows[0][COL["CUS_NO"]]),
        "cus_name": g(rows[0][COL["CUS_NAME"]]),
        "qt_dd":    g(rows[0][COL["QT_DD"]]),
        "sal_no":   g(rows[0][COL["SAL_NO"]]),
        "sal_name": g(rows[0][COL["SAL_NAME"]]),
        "usr":      g(rows[0][COL["USR"]]),
        "chk_man":  g(rows[0][COL["CHK_MAN"]]),
        "rem":      g(rows[0][COL["REM"]]),
        "usable":   bool(rows[0][COL["USABLE"]]),
        "cls_id":   bool(rows[0][COL["CLS_ID"]]),
        "del":      bool(rows[0][COL["删除"]]) if rows[0][COL["删除"]] is not None else False,
    }

    items = []
    for r in rows:
        items.append({
            "itm":        r[COL["ITM"]],
            "prd_no":     g(r[COL["PRD_NO"]]),
            "prd_name":   g(r[COL["PRD_NAME"]]),
            "spc":        g(r[COL["SPC"]]),
            "ut":         g(r[COL["UT"]]),
            "qty":        f(r[COL["QTY"]]),
            "up":         f(r[COL["UP"]]),
            "amt":        f(r[COL["AMT"]]),
            "est_dd":     g(r[COL["EST_DD"]]),
            "fld1":       g(r[COL["FLD1"]]),
            "fld2":       g(r[COL["FLD2"]]),
            "fld3":       g(r[COL["FLD3"]]),
            "ref_itm":    g(r[COL["REF_ITM"]]),
            "so_no_itm":  g(r[COL["SO_NO_ITM"]]),
            "order_no":   g(r[COL["指令单号"]]),
            "product_no": g(r[COL["成品编号"]]),
            "urgency":    g(r[COL["紧急程度"]]),
            "order_qty":  f(r[COL["订单数量"]]),
        })

    return {**header, "items": items}


# ── API: 派工单列表（SMO）──────────────────────────────────────────────
# GET /api/smo
# ── API: 仓库列表（用于发料下拉）────────────────────────────────────────────
@app.get("/api/warehouses/all")
async def list_all_warehouses(db: str = Query(default="c041")):
    """返回所有仓库（含无库存的外发仓），供调拨弹窗 TO 下拉使用。"""
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    cur.execute("""
        SELECT WH, NAME
        FROM MY_WH WITH(NOLOCK)
        WHERE USABLE = 1
        ORDER BY WH
    """)
    rows = cur.fetchall()
    conn.close()
    return {
        "warehouses": [
            {"code": g(r[0]), "name": g(r[1]), "display": g(r[1]) + "(" + g(r[0]) + ")"}
            for r in rows
        ]
    }


@app.get("/api/warehouses")
async def list_warehouses(db: str = Query(default="c041", description="c041 | t041")):
    """
    返回有库存的仓库列表，供前端发料弹窗选择 WH1/WH2。
    只返回有实际物料库存的仓库，按库存余额排序。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    cur.execute("""
        SELECT TOP 50
            w.WH,
            w.NAME,
            ISNULL(SUM(mm.QTY - ISNULL(mm.QTYIC, 0)), 0) AS mm_stock,
            ISNULL(SUM(ic.QTY), 0) AS ic_stock
        FROM MY_WH w WITH (NOLOCK)
        LEFT JOIN MM mm WITH (NOLOCK) ON mm.WH = w.WH AND mm.USABLE = 1 AND ISNULL(mm.删除, 0) = 0
        LEFT JOIN IC ic WITH (NOLOCK) ON ic.WH2 = w.WH AND ic.USABLE = 1 AND ISNULL(ic.删除, 0) = 0
        WHERE w.USABLE = 1
        GROUP BY w.WH, w.NAME
        HAVING ISNULL(SUM(mm.QTY - ISNULL(mm.QTYIC, 0)), 0) > 0
            OR ISNULL(SUM(ic.QTY), 0) > 0
        ORDER BY mm_stock DESC, w.NAME
    """)
    rows = cur.fetchall()
    conn.close()
    return {
        "warehouses": [
            {
                "code": g(r[0]),
                "name": g(r[1]),
                "display": g(r[1]) + "(" + g(r[0]) + ")",
                "mm_stock": float(r[2] or 0),
                "ic_stock": float(r[3] or 0),
            }
            for r in rows
        ]
    }


@app.get("/api/smo")
async def list_smo(
    filter: str = Query(default="pending", description="pending | all"),
    db: str = Query(default="c041", description="c041 | t041"),
):
    """
    派工单列表：单条 CTE SQL（SMO + MOM + CUST + MOT）
    齐料：C041 用 MOT.是否齐料，T041 用 XYKC >= QTY 计算
    """
    t_all = time.time()
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    cls_cond = "s.CLS_ID = 0" if filter == "pending" else "1=1"

    # ── 步骤1: KB 查需求（SMO+MOM+CUST，0.16s）────────────────────────
    cur.execute(f"""
        SELECT
            s.SMO_NO, s.SMO_DD, s.ITM, s.REF_ITM, s.SFLD1, s.REM,
            CAST(s.CLS_ID AS INT) AS CLS_ID, CAST(s.APP_ID AS INT) AS APP_ID,
            m.FG_NO, m.FG_NAME, m.FG_QTY, m.指令单号,
            CAST(m.MO_ID AS INT) AS mo_id, m.CUS_NO,
            c.简称 AS supplier_name,
            v.项, v.材料品号, v.材料名称,
            ISNULL(v.未领料数量, 0) AS need_qty
        FROM KB_工单材料齐料 v WITH (NOLOCK)
        JOIN SMO s WITH (NOLOCK) ON s.REF_ITM = v.工单号
        JOIN MOM m WITH (NOLOCK) ON s.REF_ITM = m.MO_NO
        LEFT JOIN CUST c WITH (NOLOCK) ON c.CUS_NO = m.CUS_NO AND c.生成委外仓库 = 1
        WHERE s.USABLE = 1 AND {cls_cond}
        ORDER BY s.SMO_DD DESC, s.ITM, v.项
    """)
    rows = cur.fetchall()

    # ── 步骤2: V2 批量查库存（0.17s）────────────────────────────────
    # V2批量查库存（按品号+仓库）：
    # - KB品号是正确UTF-16-LE解码字符串（如"盘圆线"）
    # - V2 PRD_NO存GBK字节，pymssql用latin-1误解码 → bytes匹配
    # - V2 WH也存GBK字节，WH_NAME存UTF-16-LE → from_hex_vw解码
    # - 返回: {(prd_gbk, wh_gbk): (wh_name, qty)} 和 {prd_gbk: total_qty}
    prd_nos = [g(r[16]) for r in rows if r[16]]
    prd_set = set(prd_nos)
    v2_map = {}      # prd_gbk -> total_qty
    v2_wh_map = {}   # (prd_gbk, wh_gbk) -> (wh_name, qty)
    if prd_set:
        gbk_list = [p.encode('gbk') for p in prd_set]
        in_clause = ','.join(['%s'] * len(gbk_list))
        # V2查品号+仓库，WH存ASCII字符串，pymssql用latin-1误解码
        # WH_code = latin-1 decode of raw bytes = 正确字符串
        cur.execute(f"""
            SELECT CONVERT(VARBINARY(200), v.PRD_NO) AS prd_bin,
                   v.WH COLLATE Chinese_PRC_BIN AS wh_code,
                   SUM(v.QTY_WH) AS qty
            FROM VW_STOCK_DETAIL2 v WITH (NOLOCK)
            WHERE CONVERT(VARBINARY(200), v.PRD_NO) IN ({in_clause})
            GROUP BY CONVERT(VARBINARY(200), v.PRD_NO), v.WH COLLATE Chinese_PRC_BIN
        """, gbk_list)
        # WH codes去重，后面查MY_WH表转中文名
        wh_codes = set()
        for pr, wh_code, q in cur.fetchall():
            if not pr or not wh_code: continue
            prd_gbk = bytes(pr)
            qty = float(q or 0)
            wh_codes.add(wh_code)
            # 仓库名先留空，后面查MY_WH填充
            v2_wh_map[(prd_gbk, wh_code)] = ('', qty)
            v2_map[prd_gbk] = v2_map.get(prd_gbk, 0) + qty
        # 批量查MY_WH获取中文仓名（WH是varchar，直接查）
        wh_name_map = {}
        if wh_codes:
            wn_in = ','.join(['%s'] * len(wh_codes))
            cur.execute(f"SELECT WH, NAME FROM MY_WH WITH(NOLOCK) WHERE WH IN ({wn_in})", tuple(wh_codes))
            for r in cur.fetchall():
                if r[0]: wh_name_map[r[0]] = g(r[1])  # g()处理UTF-16-LE
        # 回填仓名
        for key in v2_wh_map:
            wh_code = key[1]
            if wh_code in wh_name_map:
                old_val = v2_wh_map[key]
                v2_wh_map[key] = (wh_name_map[wh_code], old_val[1])
        print(f"[perf] V2库存 {len(v2_wh_map)} 条明细, {len(v2_map)} 品号汇总, {len(wh_name_map)} 仓名")

    conn.close()
    print(f"[perf] KB {len(rows)}行 + V2 {len(v2_map)}品号, {time.time()-t_all:.3f}s")

    # ── Python 内存重组 + 齐料判断（V2库存）────────────────────────
    import re as _re
    items = []
    prev_key = None
    seen_rows = set()

    for r in rows:
        key = (g(r[3]),)  # (REF_ITM = MO_NO)
        if key != prev_key:
            mo_id = bool(r[12])
            cus_no = g(r[13]) if r[13] else ""
            is_prod = not mo_id
            sup_name = g(r[14]) if (r[14] and not is_prod) else ""

            rem = g(r[5])
            fg_qty = float(r[10]) if r[10] else 0.0
            unparsed = fg_qty
            try:
                m2 = _re.search(r'未出[：:]([\d.]+)个', rem or '')
                if m2: unparsed = float(m2.group(1))
            except: pass

            cur_item = {
                "smo_no":        g(r[0]),
                "smo_dd":        g(r[1]),
                "itm":           r[2],
                "ref_itm":       g(r[3]),
                "sfld1":         g(r[4]),
                "order_no":      g(r[11]),
                "fg_no":         g(r[8]),
                "fg_name":       g(r[9]),
                "fg_qty":        fg_qty,
                "unparsed_qty":  unparsed,
                "cls_id":        bool(r[6]),
                "app_id":        bool(r[7]),
                "is_prod":       is_prod,
                "supplier_name": sup_name,
                "supplier_no":   cus_no if not is_prod else "",
                "bom_items":     [],
            }
            items.append(cur_item)
            prev_key = key

        # 去重
        row_key = (g(r[3]), int(r[15]), g(r[16]))  # (MO_NO, 项, 料号)
        if row_key in seen_rows:
            continue
        seen_rows.add(row_key)

        # KB需求 + V2库存 = 齐料判断
        prd = g(r[16])  # v.材料品号（正确解码字符串）
        name = g(r[17])  # v.材料名称
        need_qty = float(r[18]) if r[18] is not None else 0.0  # v.未领料数量
        stock = v2_map.get(prd.encode('gbk'), 0.0)  # ← V2实时库存（bytes key）
        shortage = max(0.0, need_qty - stock)

        # 该品号的仓库明细
        prd_gbk = prd.encode('gbk')
        wh_detail = []
        for (pg, wh_code), (wn, wq) in v2_wh_map.items():
            if pg == prd_gbk:
                wh_detail.append({"wh": wh_code, "wh_name": wn, "qty": wq})

        cur_item["bom_items"].append({
            "prd_no":   prd,
            "name":     name,
            "need_qty": need_qty,
            "xykc":     stock,
            "shortage": shortage,
            "is_ready": shortage == 0.0,
            "wh_detail": wh_detail,
            "ut":       "",
        })

    for item in items:
        mats = item["bom_items"]
        shortages = [m["shortage"] for m in mats]
        item["bom_count"]     = len(mats)
        item["bom_all_ready"] = all(s == 0.0 for s in shortages) if shortages else None

    print(f"[perf] 总耗时 {time.time()-t_all:.3f}s")
    return {"items": items, "total": len(items)}



async def get_smo(smo_no: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.SMO_NO, s.SMO_DD, s.ITM, s.REF_ITM, s.SFLD1, s.REM, s.MOREM,
               CAST(s.CLS_ID AS INT) AS CLS_ID, CAST(s.APP_ID AS INT) AS APP_ID,
               m.FG_NO, m.FG_NAME, m.FG_QTY, m.指令单号, m.STA_DD, m.EST_DD
        FROM SMO s WITH (NOLOCK)
        JOIN MOM m WITH (NOLOCK) ON s.REF_ITM = m.MO_NO
        WHERE s.SMO_NO = %s
        ORDER BY s.ITM
    """, (smo_no,))
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return {"error": "派工单不存在"}, 404

    COLS = {c[0]: i for i, c in enumerate(cur.description)}
    r = rows[0]
    header = {
        "smo_no":   g(r[COLS["SMO_NO"]]),
        "smo_dd":   g(r[COLS["SMO_DD"]]),
        "itm":      r[COLS["ITM"]],
        "ref_itm":  g(r[COLS["REF_ITM"]]),
        "sfld1":    g(r[COLS["SFLD1"]]),
        "rem":      g(r[COLS["REM"]]),
        "cls_id":   bool(r[COLS["CLS_ID"]]),
        "app_id":   bool(r[COLS["APP_ID"]]),
        "fg_no":    g(r[COLS["FG_NO"]]),
        "fg_name":  g(r[COLS["FG_NAME"]]),
        "fg_qty":   f(r[COLS["FG_QTY"]]),
        "order_no": g(r[COLS["指令单号"]]),
        "sta_dd":   g(r[COLS["STA_DD"]]),
        "est_dd":   g(r[COLS["EST_DD"]]),
    }

    # BOM LEV=1
    fg, qty = header["fg_no"], header["fg_qty"] or 0
    cur.execute("""
        SELECT b.PRD_NO, b.NAME, b.QTY, b.UT
        FROM BOM b WITH (NOLOCK)
        WHERE b.UPGUID = (SELECT GUID FROM BOM WITH (NOLOCK) WHERE PRD_NO = %s AND LEV = 0)
          AND b.LEV = 1
        ORDER BY b.IDX
    """, (fg,))
    bom_rows = cur.fetchall()
    bom_items = []
    for br in bom_rows:
        prd_no = g(br[0])
        bom_qty = f(br[2]) or 0
        need_qty = qty * bom_qty
        cur.execute("""
            SELECT TOP 1 QTY FROM IC WITH (NOLOCK)
            WHERE PRD_NO = %s AND IC_KND = 13
            ORDER BY IC_DD DESC
        """, (prd_no,))
        ic_row = cur.fetchone()
        ic_qty = f(ic_row[0]) if ic_row else 0
        shortage = need_qty - ic_qty
        bom_items.append({
            "prd_no":   prd_no,
            "name":     g(br[1]),
            "bom_qty":  bom_qty,
            "ut":       g(br[3]),
            "need_qty": need_qty,
            "ic_qty":   ic_qty,
            "shortage":  shortage,
            "is_ready":  ic_qty >= need_qty,
        })
    conn.close()
    return {**header, "bom_items": bom_items}


# ── API: 盘点出库 ─────────────────────────────────────────────────────────
@app.post("/api/stock/out")
async def stock_out(
    items: List[dict] = Body(...),
    db: str = Query(default="c041"),
):
    """
    IC KND=23 其他出库。
    body: [{"prd_no": "...", "wh": "仓库代码", "qty": 数量, "rem": "备注"}]
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    today = datetime.now().strftime('%y%m')  # 2609，IC+YYMM格式
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    results = []

    # IC格式: IC + YYMM + 4位流水 = 10字符（如IC26090896，流水=0896）
    # 流水号=SUBSTRING(IC_NO,7,4)，只取今天10字符有效记录
    cur.execute(f"""
        SELECT ISNULL(MAX(TRY_CAST(SUBSTRING(IC_NO,7,4) AS INT)), 0)
        FROM IC WITH(NOLOCK)
        WHERE IC_NO LIKE %s AND LEN(IC_NO) = 10
    """, (f"IC{today}%",))
    seq = (cur.fetchone()[0] or 0)  # 从当天最大seq开始，每行+1

    seq += 1
    ic_no = f"IC{today}{seq:04d}"  # IC + YYMM + 4位序号 = 10字符
    itm = 0
    for item in items:
        prd_no = item.get("prd_no", "")
        wh2 = item.get("wh", "")
        qty = float(item.get("qty", 0))
        rem = item.get("rem", "") or ""
        ref_itm   = item.get("ref_itm", "") or ""
        cus_no    = item.get("cus_no", "") or ""
        sup_name  = item.get("sup_name", "") or ""
        ddjh      = item.get("ddjh", "") or ""
        itm += 1

        cur.execute("SELECT NAME, UT FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (prd_no,))
        pr = cur.fetchone()
        prd_name = g(pr[0]) if pr else ""
        ut = pr[1] if pr else ""

        # 查 WH2 仓库名称
        wh2_name = ""
        if wh2:
            cur.execute("SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (wh2,))
            r_wh = cur.fetchone()
            wh2_name = g(r_wh[0]) if r_wh else ""

        cur.execute("""
            INSERT INTO IC (IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH2,WH2NAME,
                            USR,USABLE,ITM,REM,
                            指令单号,客户,CUSNAME,DDJH,单重,净重)
            VALUES (%s,%s,23,%s,%s,%s,%s,%s,%s,'phone',1,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (ic_no, now_str,
              prd_no, prd_name, qty, ut, wh2, wh2_name,
              itm, rem, ref_itm, cus_no, sup_name, ddjh,
              float(item.get("danzhong", 0) or 0),
              float(item.get("jingzhong", 0) or 0)))
        results.append({"ic_no": ic_no, "prd_no": prd_no, "wh": wh2, "qty": qty, "itm": itm})

    conn.commit()
    conn.close()
    return {"ok": True, "count": len(results), "items": results}


# ── API: 盘点入库 ─────────────────────────────────────────────────────────
@app.post("/api/stock/in")
async def stock_in(
    items: List[dict] = Body(...),
    db: str = Query(default="c041"),
):
    """
    IC KND=13 其他入库。
    body: [{"prd_no": "...", "wh": "仓库代码", "qty": 数量, "rem": "备注"}]
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    today = datetime.now().strftime('%y%m')  # 2609，IC+YYMM格式
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    results = []

    # IC格式: IC + YYMM + 4位流水 = 10字符（如IC26090896，流水=0896）
    # 流水号=SUBSTRING(IC_NO,7,4)，只取今天10字符有效记录
    cur.execute(f"""
        SELECT ISNULL(MAX(TRY_CAST(SUBSTRING(IC_NO,7,4) AS INT)), 0)
        FROM IC WITH(NOLOCK)
        WHERE IC_NO LIKE %s AND LEN(IC_NO) = 10
    """, (f"IC{today}%",))
    seq = (cur.fetchone()[0] or 0)  # 从当天最大seq开始，每行+1

    seq += 1
    ic_no = f"IC{today}{seq:04d}"  # IC + YYMM + 4位序号 = 10字符
    itm = 0
    for item in items:
        prd_no = item.get("prd_no", "")
        wh1 = item.get("wh", "")
        qty = float(item.get("qty", 0))
        rem = item.get("rem", "") or ""
        ref_itm   = item.get("ref_itm", "") or ""
        cus_no    = item.get("cus_no", "") or ""
        sup_name  = item.get("sup_name", "") or ""
        ddjh      = item.get("ddjh", "") or ""
        itm += 1

        cur.execute("SELECT NAME, UT FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (prd_no,))
        pr = cur.fetchone()
        prd_name = g(pr[0]) if pr else ""
        ut = pr[1] if pr else ""

        # 查 WH1 仓库名称
        wh1_name = ""
        if wh1:
            cur.execute("SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (wh1,))
            r_wh = cur.fetchone()
            wh1_name = g(r_wh[0]) if r_wh else ""

        cur.execute("""
            INSERT INTO IC (IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH1,WH1NAME,
                            USR,USABLE,ITM,REM,
                            指令单号,客户,CUSNAME,DDJH,单重,净重)
            VALUES (%s,%s,13,%s,%s,%s,%s,%s,%s,'phone',1,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (ic_no, now_str, prd_no, prd_name, qty, ut, wh1, wh1_name,
              itm, rem, ref_itm, cus_no, sup_name, ddjh,
              float(item.get("danzhong", 0) or 0),
              float(item.get("jingzhong", 0) or 0)))
        results.append({"ic_no": ic_no, "prd_no": prd_no, "wh": wh1, "qty": qty, "itm": itm})

        conn.commit()
    conn.close()
    return {"ok": True, "count": len(results), "items": results}


# ── API: 调拨单（批量写入单张IC）─────────────────────────────────────────
@app.post("/api/stock/transfer")
async def stock_transfer(
    items: List[dict] = Body(...),
    tool_rows: List[dict] = Body(default=[]),
    db: str = Query(default="c041"),
):
    """
    IC KND=30 仓库调拨。
    INSERT 21列: IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH1,WH2,
                 WH1NAME,WH2NAME,USR,USABLE,ITM,REM,FLD1,
                 指令单号,DDJH,客户,单重,净重
    FLD1 = ic_no (完工单关联用)
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    today = datetime.now().strftime("%y%m")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        "SELECT ISNULL(MAX(TRY_CAST(SUBSTRING(IC_NO,7,4) AS INT)), 0) "
        "FROM IC WITH(NOLOCK) WHERE IC_NO LIKE %s AND LEN(IC_NO)=10",
        (f"IC{today}%",))
    seq = (cur.fetchone()[0] or 0) + 1
    ic_no = f"IC{today}{seq:04d}"

    # 工具行共用第一行 TO/FROM 仓
    batch_wh1 = items[0].get('wh1', '') if items else ''
    batch_wh2 = items[0].get('wh2', '') if items else ''
    cur.execute("SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (batch_wh1,))
    r1 = cur.fetchone()
    batch_wh1_name = r1[0] if r1 else ''
    cur.execute("SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (batch_wh2,))
    r2 = cur.fetchone()
    batch_wh2_name = r2[0] if r2 else ''

    results = []
    for i, item in enumerate(items):
        prd_no  = item.get("prd_no", "")
        wh      = item.get("wh", "")
        wh2     = item.get("wh2", "")
        qty     = float(item.get("qty", 0))
        rem     = item.get("rem", "") or ""
        ref_itm = item.get("ref_itm", "") or ""
        ddjh_v  = item.get("ddjh", "") or ""
        cus_no  = item.get("cus_no", "") or ""
        dzhw    = float(item.get("danzhong", 0) or 0)
        jzhw    = float(item.get("jingzhong", 0) or 0)

        cur.execute("SELECT NAME, UT FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (prd_no,))
        pr = cur.fetchone()
        prd_name = pr[0] if pr else ""
        ut = pr[1] if pr else ""

        cur.execute("SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (wh,))
        r = cur.fetchone()
        wh1_name = r[0] if r else ""

        cur.execute("SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (wh2,))
        r = cur.fetchone()
        wh2_name = r[0] if r else ""

        # 数据驱动INSERT
        # 产品行 KND=30（调拨），工具行 KND=30
        is_tool = bool(item.get("is_tool"))
        knd = 30
        COLS = ['IC_NO','IC_DD','IC_KND','PRD_NO','PRD_NAME','QTY','UT',
                'WH1','WH2','WH1NAME','WH2NAME',
                'USR','USABLE','ITM','REM','FLD1',
                '指令单号','DDJH','客户','单重','净重']
        VALS = [ic_no, now_str, knd, prd_no, prd_name, qty, ut,
                 wh2, wh, wh2_name, wh1_name,
                 'phone', 1, i + 1, rem, ic_no,
                 ref_itm, ddjh_v, cus_no, dzhw, jzhw]
        assert len(COLS) == len(VALS), f"列{len(COLS)}!=值{len(VALS)}"
        sql = f"INSERT INTO IC ({','.join(COLS)}) VALUES ({','.join(['%s']*len(COLS))})"
        cur.execute(sql, VALS)
        results.append({"ic_no": ic_no, "prd_no": prd_no,
                       "wh2": wh2, "wh1": wh, "qty": qty, "itm": i + 1})

    # 运输工具行：只写品号、品名、单位、数量，其余留空
    prod_count = sum(1 for item in items if float(item.get('qty') or 0) > 0)
    for trow in tool_rows:
        code = trow.get('code', ''); qty = trow.get('qty', 0)
        if not code or not qty: continue
        prod_count += 1
        cur.execute("SELECT NAME, UT FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (code,))
        r = cur.fetchone()
        prd_name = r[0] if r else ''
        ut = r[1] if r else ''
        # 工具行极简INSERT：仅品号/品名/单位/数量，其余列留空或默认值
        cur.execute(
            "INSERT INTO IC "
            "(IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,"
            "WH1,WH2,WH1NAME,WH2NAME,USR,USABLE,ITM) "
            "VALUES (%s,%s,30,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ic_no, now_str, code, prd_name, qty, ut,
             wh2, wh, wh2_name, wh1_name, "phone", 1, prod_count))
        results.append({"ic_no": ic_no, "prd_no": code,
                        "wh1": wh, "wh2": wh2,
                        "qty": qty, "itm": prod_count, "is_tool": True})

    conn.commit()
    conn.close()
    return {"ok": True, "ic_no": ic_no, "count": len(results), "items": results}

@app.get("/api/completion/transfer")
async def completion_transfer_list(
    db: str = Query(default="c041"),
    prd_no: str = Query(default=""),
    customer: str = Query(default=""),
    ddjh: str = Query(default=""),
    wh: str = Query(default=""),
):
    """
    返回 KND=30 调拨单，按(品号,收货仓WH1)分组汇总。
    用于完工单列表选择调拨单。
    返回: [{ic_no, prd_no, prd_name, wh1, wh1_name, total_qty, transfer_qty, remaining_qty}]
    transfer_qty = 已完工扣料的 QTY之和(KND=23 FLD1=本单)
    remaining_qty = total_qty - transfer_qty
    支持筛选: prd_no, customer, ddjh, wh (LIKE)
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    where = "WHERE t.IC_KND = 30 AND t.USABLE = 1 AND ISNULL(t.删除, 0) = 0 AND LEFT(t.PRD_NO, 5) <> '03002'"
    args = []
    if prd_no:
        where += " AND t.PRD_NO LIKE %s"
        args.append(f"%{prd_no}%")
    if customer:
        where += " AND t.客户 LIKE %s"
        args.append(f"%{customer}%")
    if ddjh:
        where += " AND t.指令单号 LIKE %s"
        args.append(f"%{ddjh}%")
    if wh:
        where += " AND (t.WH1 LIKE %s OR w.NAME LIKE %s)"
        args.append(f"%{wh}%")

    cur.execute(f"""
        SELECT
            t.IC_NO,
            t.PRD_NO,
            p.NAME,
            t.WH1,
            w.NAME,
            SUM(t.QTY) AS total_qty,
            ISNULL(x.done_qty, 0) AS transfer_qty,
            ISNULL(t.指令单号, '') AS ref,
            ISNULL(t.客户, '') AS cus,
            ISNULL(t.DDJH, '') AS ddjh,
            ISNULL(t.单重, 0) AS dzhw,
            ISNULL(t.净重, 0) AS jzhw
        FROM IC t WITH(NOLOCK)
        JOIN PRDT p WITH(NOLOCK) ON p.PRD_NO = t.PRD_NO
        LEFT JOIN MY_WH w WITH(NOLOCK) ON w.WH = t.WH1
        LEFT JOIN (
            SELECT FLD1, SUM(QTY) AS done_qty
            FROM IC WITH(NOLOCK)
            WHERE IC_KND = 23 AND ISNULL(FLD1, '') <> ''
            GROUP BY FLD1
        ) x ON x.FLD1 = t.IC_NO
        {where}
        GROUP BY t.IC_NO, t.PRD_NO, p.NAME, t.WH1, w.NAME, x.done_qty,
                 t.指令单号, t.客户, t.DDJH, t.单重, t.净重
        ORDER BY t.IC_NO DESC
    """, args if args else None)
    rows = cur.fetchall()
    conn.close()

    items = []
    for r in rows:
        total = float(r[5] or 0)
        done = float(r[6] or 0)
        remaining = total - done
        if remaining > 0:
            items.append({
                "ic_no":         g(r[0]),
                "prd_no":        g(r[1]),
                "prd_name":      g(r[2]),
                "wh1":           g(r[3]),
                "wh1_name":      g(r[4]),
                "total_qty":     total,
                "transfer_qty":  done,
                "remaining_qty": remaining,
                "ref":   g(r[7]),
                "cus":   g(r[8]),
                "ddjh":  g(r[9]),
                "dzhw":  r[10] or 0,
                "jzhw":  r[11] or 0,
            })
    return {"items": items}


@app.get("/api/completion/bom_match")
async def completion_bom_match(prd_no: str = Query(...), db: str = Query(default="c041")):
    """
    查哪些成品的 BOM LEV=1 直接子件包含 prd_no。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT h.PRD_NO, p.NAME, c.QTY
        FROM BOM c WITH(NOLOCK)
        JOIN BOM h WITH(NOLOCK) ON c.UPGUID = h.GUID AND h.LEV = 0
        JOIN PRDT p WITH(NOLOCK) ON p.PRD_NO = h.PRD_NO
        WHERE c.LEV = 1 AND c.PRD_NO = %s AND ISNULL(c.删除, 0) = 0
        ORDER BY p.NAME
    """, (prd_no,))
    rows = cur.fetchall()
    conn.close()
    return {
        "items": [{
            "prd_no": g(r[0]),
            "prd_name": g(r[1]) if r[1] else '',
            "qty_per_unit": float(r[2] or 1),
        } for r in rows]
    }


@app.get("/api/completion/fg_detail")
async def completion_fg_detail(fg_no: str = Query(...), db: str = Query(default="c041")):
    """
    返回成品 fg_no 的 BOM 材料详情：
    - BOM LEV=1 所有材料
    - 每材料有哪些 KND=30 调拨单、各自剩余可用量
    - 自动判断是否可完工（材料够不够）
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    # 查 BOM LEV=1 材料
    cur.execute("""
        SELECT c.PRD_NO, p.NAME, c.QTY
        FROM BOM c WITH(NOLOCK)
        JOIN BOM h WITH(NOLOCK) ON c.UPGUID = h.GUID AND h.LEV=0
        JOIN PRDT p WITH(NOLOCK) ON p.PRD_NO = c.PRD_NO
        WHERE h.PRD_NO=%s AND c.LEV=1 AND ISNULL(c.删除,0)=0
        ORDER BY c.PRD_NO
    """, (fg_no,))
    bom_rows = cur.fetchall()

    # 查所有 KND=30 调拨单及其剩余可用量（按品号分组）
    cur.execute("""
        SELECT t.PRD_NO, t.IC_NO, t.WH1, w.NAME,
               ISNULL(t.指令单号,''), ISNULL(t.客户,''),
               SUM(t.QTY) AS total_qty,
               ISNULL(SUM(y.QTY), 0) AS used_qty
        FROM IC t WITH(NOLOCK)
        LEFT JOIN MY_WH w WITH(NOLOCK) ON w.WH = t.WH1
        LEFT JOIN IC y WITH(NOLOCK) ON y.IC_KND=23 AND y.FLD1=t.IC_NO AND ISNULL(y.FLD1,'')<>''
        WHERE t.IC_KND=30 AND t.USABLE=1 AND ISNULL(t.删除,0)=0
        GROUP BY t.PRD_NO, t.IC_NO, t.WH1, w.NAME, ISNULL(t.指令单号,''), ISNULL(t.客户,'')
        ORDER BY t.PRD_NO, t.IC_NO
    """)
    transfer_by_prd = {}
    for r in cur.fetchall():
        prd = g(r[0])
        ic_no = g(r[1])
        wh1 = g(r[2]) or ''
        wh_name = g(r[3]) if r[3] else ''
        ddjh = g(r[4])
        customer = g(r[5])
        total = float(r[6] or 0)
        used = float(r[7] or 0)
        remaining = total - used
        if remaining > 0:
            if prd not in transfer_by_prd:
                transfer_by_prd[prd] = []
            transfer_by_prd[prd].append({
                "ic_no": ic_no,
                "wh1": wh1,
                "wh1_name": wh_name,
                "available": remaining,
                "ddjh": ddjh,
                "customer": customer,
            })

    materials = []
    all_fulfilled = True
    for r in bom_rows:
        prd = g(r[0])
        mat_name = g(r[1]) if r[1] else ''
        ratio = float(r[2] or 1)
        transfers = transfer_by_prd.get(prd, [])
        total_avail = sum(t["available"] for t in transfers)
        fulfilled = total_avail >= ratio
        if not fulfilled:
            all_fulfilled = False
        materials.append({
            "prd_no": prd,
            "prd_name": mat_name,
            "ratio": ratio,
            "total_available": total_avail,
            "fulfilled": fulfilled,
            "transfers": transfers,
        })

    conn.close()
    return {
        "fg_no": fg_no,
        "all_fulfilled": all_fulfilled,
        "materials": materials,
    }


@app.get("/api/smo/bom_stock")
async def smo_bom_stock(fg_no: str = Query(...), db: str = Query(default="c041"),
                        include_tree: bool = Query(default=False)):
    """
    查成品 fg_no 的 BOM 整树 + 各节点库存（V2实时）+ 缺料判断。

    查询参数:
      fg_no       成品料号
      db          c041 / t041（默认 c041）
      include_tree 是否返回完整树结构（默认 False 只返回叶子节点列表）

    返回（include_tree=False）:
      {fg_no, fg_name, items: [{prd_no, prd_name, qty_per_unit, stock, shortage, is_ready}]}

    返回（include_tree=True）:
      {fg_no, fg_name, node_count, max_depth,
       ready_count, shortage_count,
       tree: [{prd_no, name, knd, qty_per_unit, stock, shortage, is_ready, depth, path}]}

    齐料逻辑:
      - KND=4 原料: stock >= qty_per_unit * fg_qty → is_ready
      - KND=3 半成品: 递归子节点全部齐 → is_ready
      - KND=1 成品: 不参与齐料（是产出品）
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    # 确认成品存在
    cur.execute("SELECT NAME FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s AND USABLE=1", (fg_no,))
    prd_row = cur.fetchone()
    if not prd_row:
        conn.close()
        return {"fg_no": fg_no, "fg_name": "", "items": [], "tree": []}
    fg_name = g(prd_row[0])

    # ── 1. 加载全 BOM，建立 (parent_guid, child_prd_no) → row ──────────
    cur.execute("SELECT GUID, PRD_NO FROM BOM WITH(NOLOCK) WHERE LEV=0")
    prd_to_root = {r[1]: r[0] for r in cur.fetchall()}  # prd_no → LEV=0 GUID

    cur.execute("""
        SELECT c.GUID, c.PRD_NO, p.NAME, c.LEV, c.IDX, c.QTY, c.KND,
               c.UPGUID, h.PRD_NO AS parent_prd
        FROM BOM c WITH(NOLOCK)
        JOIN BOM h WITH(NOLOCK) ON c.UPGUID = h.GUID AND h.LEV=0
        LEFT JOIN PRDT p WITH(NOLOCK) ON p.PRD_NO = c.PRD_NO
        WHERE ISNULL(c.删除,0)=0
    """)
    # 结构: GUID, PRD_NO, NAME, LEV, IDX, QTY, KND, UPGUID, parent_prd
    edges = cur.fetchall()  # 所有 LEV=1 边

    # 建立 prd → (name, knd) 字典，避免递归内重复扫描 edges
    prd_info = {}   # prd_no → (name_str, knd)
    for e in edges:
        guid, prd, name, lev, idx, qty, knd, upguid, parent_prd = e
        if prd not in prd_info:
            prd_info[prd] = (g(name), knd)

    # child_map: parent → [(child_prd, qty_per_unit, knd, guid, upguid)]
    # edges 里: prd=子件, parent_prd=父件
    child_map = {}
    for e in edges:
        guid, prd, name, lev, idx, qty, knd, upguid, parent_prd = e
        child_map.setdefault(parent_prd, []).append((prd, f(qty) or 1, knd, guid, upguid))

    # 成品根不在 prd_info 里（没 JOIN），补充一下
    if fg_no not in prd_info:
        prd_info[fg_no] = (fg_name, None)

    # 成品没有 BOM 根
    if fg_no not in prd_to_root:
        conn.close()
        return {"fg_no": fg_no, "fg_name": fg_name, "items": [], "tree": [],
                "node_count": 0, "max_depth": 0, "ready_count": 0, "shortage_count": 0}

    # ── 2. 递归展开整树，记录 depth + accumulated qty ──────────────────
    visited_edges = set()
    tree_nodes = []
    max_depth = 0

    def walk(prd, depth, qty_from_parent, path, parent_guid):
        nonlocal max_depth
        if depth > 15:
            return
        key = (parent_guid, prd)
        if key in visited_edges:
            return
        visited_edges.add(key)

        info = prd_info.get(prd, ('', None))
        name_str, knd = info
        qty_this = qty_from_parent
        max_depth = max(max_depth, depth)

        tree_nodes.append({
            "prd_no":       prd,
            "name":         name_str,
            "knd":          knd,
            "qty_per_unit": qty_this,
            "depth":        depth,
            "path":         path,
        })

        # 递归子件：child_map[prd] 里存 (child_prd, qty, knd, edge_guid, edge_upguid)
        for child_prd, sub_qty, sub_knd, sub_edge_guid, sub_edge_upguid in child_map.get(prd, []):
            sub_path = f"{path} > {prd}"
            # 子件的 LEV=0 根 GUID（用 prd_to_root，不是 edge guid）
            child_root = prd_to_root.get(child_prd, sub_edge_guid)
            walk(child_prd, depth+1, qty_this * sub_qty, sub_path, child_root)

    # 从成品根开始，根的 qty_per_unit = 1（单位成品）
    root_guid = prd_to_root[fg_no]
    walk(fg_no, 0, 1.0, fg_no, root_guid)

    if not tree_nodes:
        conn.close()
        return {"fg_no": fg_no, "fg_name": fg_name, "items": [], "tree": [],
                "node_count": 0, "max_depth": 0, "ready_count": 0, "shortage_count": 0}

    # ── 3. 批量查 V2 库存（GBK bytes 匹配）────────────────────────────
    all_prds = list(set(n["prd_no"] for n in tree_nodes))
    if all_prds:
        ph = ','.join(['%s'] * len(all_prds))
        # 直接 string 查询（V2 PRD_NO 是 VARCHAR，存 ASCII 品号）
        cur.execute(f"""
            SELECT PRD_NO, SUM(QTY_WH) AS stock
            FROM VW_STOCK_DETAIL2 WITH(NOLOCK)
            WHERE PRD_NO IN ({ph})
            GROUP BY PRD_NO
        """, tuple(all_prds))
        stock_map = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
        # 各仓库存 {prd_no: {wh: qty}}
        cur.execute(f"""
            SELECT PRD_NO, WH, SUM(QTY_WH) AS qty
            FROM VW_STOCK_DETAIL2 WITH(NOLOCK)
            WHERE PRD_NO IN ({ph}) AND QTY_WH > 0
            GROUP BY PRD_NO, WH
        """, tuple(all_prds))
        wh_stock_map = {}
        for r in cur.fetchall():
            p, w, q = r[0], r[1], float(r[2] or 0)
            wh_stock_map.setdefault(p, {})[w] = q

        def get_stock(prd):
            return stock_map.get(prd, 0.0)

    else:
        wh_stock_map = {}
        get_stock = lambda p: 0.0

    # ── 4. 计算 shortage / is_ready（所有节点直接判断）───────────────
    for node in tree_nodes:
        prd = node["prd_no"]
        stock = get_stock(prd)
        qty = node["qty_per_unit"]
        node["stock"] = stock
        node["shortage"] = max(0.0, qty - stock)
        node["is_ready"] = (stock >= qty)

    # ── 5. 汇总 ───────────────────────────────────────────────────────
    ready_count    = sum(1 for n in tree_nodes if n["is_ready"])
    shortage_count = len(tree_nodes) - ready_count

    # 叶子节点（KND=4 原料层）
    leaf_nodes = [n for n in tree_nodes if str(n.get("knd")) == "4"]

    # ── 6. 构建嵌套树（每节点含 children 数组，供前端递归渲染）───────
    # 建立 prd_no → tree_node 索引
    node_map = {n["prd_no"]: n for n in tree_nodes}
    # 每个节点独立 children 列表
    for n in tree_nodes:
        n["children"] = []

    # 建立 parent_prd → [child_nodes] 反查（同一子件在不同父下出现时各有独立 entry）
    parent_children = {}   # parent_prd → [child_prd list]
    for e in edges:
        guid, prd, name, lev, idx, qty, knd, upguid, parent_prd = e
        parent_children.setdefault(parent_prd, []).append(prd)

    # 把子件挂到父节点的 children 里（同名节点在各父下独立显示）
    for e in edges:
        guid, child_prd, name, lev, idx, qty, knd, upguid, parent_prd = e
        parent_node = node_map.get(parent_prd)
        child_node  = node_map.get(child_prd)
        if parent_node and child_node:
            parent_node["children"].append(child_node)

    root_node = node_map.get(fg_no)
    if root_node:
        root_node["is_root"] = True

    def serialize(node):
        is_leaf = str(node.get("knd")) == "4"
        return {
            "prd_no":       node["prd_no"],
            "prd_name":     node["name"],
            "knd":          node["knd"],
            "qty_per_unit": round(node["qty_per_unit"], 6),
            "stock":        round(node["stock"], 4) if is_leaf else None,
            "shortage":     round(node["shortage"], 4) if is_leaf else None,
            "is_ready":     node["is_ready"] if is_leaf else None,
            "is_leaf":      is_leaf,
            "wh_stock":     wh_stock_map.get(node["prd_no"], {}) if is_leaf else {},
            "children":     [serialize(c) for c in node["children"]],
        }

    nested_tree = serialize(root_node) if root_node else {}

    # 按 depth 分层（供快速定位层级）
    by_depth = {}
    for n in tree_nodes:
        d = n["depth"]
        by_depth.setdefault(d, []).append({
            "prd_no":       n["prd_no"],
            "prd_name":     n["name"],
            "knd":          n["knd"],
            "qty_per_unit": round(n["qty_per_unit"], 6),
            "stock":        round(n["stock"], 4),
            "shortage":     round(n["shortage"], 4),
            "is_ready":     n["is_ready"],
            "is_leaf":      str(n.get("knd")) == "4",
        })

    # include_tree=False: items=叶子; include_tree=True: tree=完整树, items=[]
    source = leaf_nodes if not include_tree else tree_nodes
    result_items = []
    for n in source:
        is_leaf = str(n.get("knd")) == "4"
        result_items.append({
            "prd_no":      n["prd_no"],
            "prd_name":    n["name"],
            "qty_per_unit": round(n["qty_per_unit"], 6),
            "stock":       round(n["stock"], 4) if is_leaf else None,
            "shortage":    round(n["shortage"], 4) if is_leaf else None,
            "is_ready":    n["is_ready"] if is_leaf else None,
            "knd":         n["knd"],
            "depth":       n["depth"],
            "path":        n["path"],
        })

    conn.close()
    return {
        "fg_no":          fg_no,
        "fg_name":        fg_name,
        "node_count":     len(tree_nodes),
        "max_depth":      max_depth,
        "ready_count":    ready_count,
        "shortage_count": shortage_count,
        "by_depth":    by_depth,           # 按层级分组 {depth: [nodes...]}
        "nested_tree": nested_tree,        # 嵌套树（含 children，前端递归渲染）
        "wh_stock_map": wh_stock_map,     # {prd_no: {wh: qty}} 各仓库存
        "items":       result_items if not include_tree else [],
        "tree":        result_items if include_tree else [],
    }



@app.get("/api/bom/children")
async def bom_children(fg_no: str = Query(...), depth: int = Query(default=1), db: str = Query(default="c041")):
    """
    返回成品的 L1 BOM 子件列表（含各仓库存）。
    用于发安装 Tab：搜成品 → 展示子件 + 各仓库存 → 多选 + 各行选仓填数量。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    def gbk(v):
        if v is None: return ''
        if isinstance(v, bytes): return v.decode('gbk', errors='ignore')
        return str(v)

    # 成品基本信息
    cur.execute(
        "SELECT PRD_NO, NAME, SPC, UT FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s",
        (fg_no,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return {"error": "品号不存在"}
    fg_name = gbk(r[1])
    fg_spc  = gbk(r[2])

    # L1 子件（BOM LEV=1，UPGUID=该成品的 GUID）
    cur.execute("""
        SELECT b.PRD_NO,
               MAX(b.QTY) AS qty_per_unit,
               MAX(b.KND) AS knd
        FROM BOM b
        JOIN BOM root ON root.GUID = b.UPGUID
        WHERE root.PRD_NO = %s AND b.LEV = 1
        GROUP BY b.PRD_NO
        ORDER BY MAX(b.IDX)
    """, (fg_no,))
    children = cur.fetchall()

    child_prds = [str(c[0]) for c in children]

    # 批量查 V2 各仓库存（只取有库存的仓）
    stock_by_prd = {}
    if child_prds:
        ph = ','.join(['%s'] * len(child_prds))
        cur.execute(f"""
            SELECT PRD_NO, WH, SUM(QTY_WH) AS s
            FROM VW_STOCK_DETAIL2 WITH(NOLOCK)
            WHERE PRD_NO IN ({ph})
            GROUP BY PRD_NO, WH
            HAVING SUM(QTY_WH) > 0
        """, tuple(child_prds))
        for prd, wh, qty in cur.fetchall():
            p = str(prd)
            if p not in stock_by_prd:
                stock_by_prd[p] = {}
            stock_by_prd[p][str(wh)] = round(float(qty), 2)

    # 批量查品名
    name_map = {}
    if child_prds:
        ph = ','.join(['%s'] * len(child_prds))
        cur.execute(f"SELECT PRD_NO, NAME FROM PRDT WITH(NOLOCK) WHERE PRD_NO IN ({ph})",
                    tuple(child_prds))
        for prd, name in cur.fetchall():
            name_map[str(prd)] = gbk(name)

    conn.close()

    items = []
    for prd_no, qty_per_unit, knd in children:
        p = str(prd_no)
        items.append({
            "prd_no":      p,
            "prd_name":    name_map.get(p, p),
            "qty_per_unit": round(float(qty_per_unit), 3),
            "knd":         knd,
            "stock_by_wh": stock_by_prd.get(p, {}),  # {wh: qty}
        })

    return {
        "fg_no":    fg_no,
        "fg_name":  fg_name,
        "fg_spc":   fg_spc,
        "children": items,
    }


@app.post("/api/install/dispatch")
async def install_dispatch(
    mo:     str  = Body(...),      # 指令单号
    remark: str  = Body(default=""),
    items:  list = Body(...),      # [{prd_no, qty, wh2}]
    db:     str  = Body(default="c041"),
):
    """
    发安装：KND=30 调拨，WH1=8(车间仓)，WH2=用户选，
    每行 IC 写入一条调拨出库记录。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    def gbk(v):
        if v is None: return ''
        if isinstance(v, bytes): return v.decode('gbk', errors='ignore')
        return str(v)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # IC_NO 序号
    today = datetime.now().strftime("%y%m")
    cur.execute(
        "SELECT ISNULL(MAX(TRY_CAST(SUBSTRING(IC_NO,7,4) AS INT)), 0) "
        "FROM IC WITH(NOLOCK) WHERE IC_NO LIKE %s AND LEN(IC_NO)=10",
        (f"IC{today}%",))
    seq = (cur.fetchone()[0] or 0) + 1
    ic_no = f"IC{today}{seq:04d}"

    results = []
    for i, item in enumerate(items):
        prd_no = item.get("prd_no", "")
        qty    = float(item.get("qty", 0))
        wh2    = item.get("wh2", "")

        if qty <= 0 or not wh2:
            continue  # 前端已拦截，防御性跳过

        # WH1 固定 8（车间仓）
        wh1 = "8"

        cur.execute(
            "SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (wh1,))
        r = cur.fetchone()
        wh1_name = gbk(r[0]) if r else "车间仓"

        cur.execute(
            "SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (wh2,))
        r = cur.fetchone()
        wh2_name = gbk(r[0]) if r else wh2

        cur.execute(
            "SELECT NAME, UT FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (prd_no,))
        r = cur.fetchone()
        prd_name = gbk(r[0]) if r else prd_no
        ut = r[1] if r else ""

        cur.execute(
            "INSERT INTO IC "
            "(IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH1,WH2,"
            "WH1NAME,WH2NAME,USR,USABLE,ITM,REM,FLD1,指令单号) "
            "VALUES (%s,%s,30,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ic_no, now_str, prd_no, prd_name, qty, ut, wh1, wh2,
             wh1_name, wh2_name, "phone", 1, i + 1, remark, ic_no, mo, mo))

        results.append({
            "ic_no": ic_no, "prd_no": prd_no,
            "wh2": wh2, "wh1": wh1, "qty": qty, "itm": i + 1,
        })

    conn.commit()
    conn.close()
    return {"ok": True, "ic_no": ic_no, "count": len(results), "items": results}


@app.get("/api/completion/transfer_by_fg")
async def completion_transfer_by_fg(fg_no: str = Query(...), db: str = Query(default="c041")):
    """
    返回可用于指定成品完工的调拨单列表。
    筛选：KND=30, USABLE=1, FLD1 IS NULL（未完工），
    且品号在成品 BOM（LEV=1）之中。按(品号,收货仓)分组。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    cur.execute("""
        SELECT
            t.IC_NO,
            t.PRD_NO,
            p.NAME,
            t.WH1,
            w.NAME,
            SUM(t.QTY) AS total_qty,
            ISNULL(x.done_qty, 0) AS transfer_qty,
            ISNULL(t.指令单号, '') AS ref,
            ISNULL(t.客户, '') AS cus
        FROM IC t WITH(NOLOCK)
        JOIN PRDT p WITH(NOLOCK) ON p.PRD_NO = t.PRD_NO
        LEFT JOIN MY_WH w WITH(NOLOCK) ON w.WH = t.WH1
        LEFT JOIN (
            SELECT FLD1, SUM(QTY) AS done_qty
            FROM IC WITH(NOLOCK)
            WHERE IC_KND = 23 AND ISNULL(FLD1, '') <> ''
            GROUP BY FLD1
        ) x ON x.FLD1 = t.IC_NO
        WHERE t.IC_KND = 30
          AND t.USABLE = 1
          AND ISNULL(t.删除, 0) = 0
          AND t.PRD_NO IN (
              SELECT c2.PRD_NO FROM BOM c2 WITH(NOLOCK)
              JOIN BOM h2 WITH(NOLOCK) ON c2.UPGUID = h2.GUID AND h2.LEV = 0
              WHERE h2.PRD_NO = %s AND c2.LEV = 1
          )
        GROUP BY t.IC_NO, t.PRD_NO, p.NAME, t.WH1, w.NAME, x.done_qty,
                 t.指令单号, t.客户
        HAVING SUM(t.QTY) - ISNULL(x.done_qty, 0) > 0
        ORDER BY t.PRD_NO, t.IC_NO
    """, (fg_no,))
    rows = cur.fetchall()
    conn.close()
    items = []
    for r in rows:
        total = float(r[5] or 0)
        done = float(r[6] or 0)
        remaining = total - done
        if remaining > 0:
            items.append({
                "ic_no": g(r[0]),
                "prd_no": g(r[1]),
                "prd_name": g(r[2]),
                "wh1": g(r[3]),
                "wh1_name": g(r[4]),
                "remaining_qty": remaining,
                "ref": g(r[7]),
                "cus": g(r[8]),
            })
    return {"items": items}


@app.post("/api/completion/confirm")
async def completion_confirm(
    payload: dict = Body(...),
    db: str = Query(default="c041"),
):
    """
    完工确认。
    payload: {
      fg_no, fg_qty, fg_wh,
      danzhong, jingzhong,
      components: [{
        prd_no, qty,
        transfer_ic_no, transfer_wh1,  # 各行可来自不同调拨单
      }]
    }
    写入库单(KND=13, 1行) + 出库单(KND=23, 多行ITM)。
    """
    fg_no = payload.get("fg_no", "")
    fg_qty = float(payload.get("fg_qty", 0))
    fg_wh = payload.get("fg_wh", "G")
    components = payload.get("components", [])
    danzhong = payload.get("danzhong")
    jingzhong = payload.get("jingzhong")
    rem_in = payload.get("rem", "") or ""

    if not fg_no or fg_qty <= 0 or not components:
        return {"error": "缺少成品/数量/子件"}

    conn = get_conn()
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%y%m")

    # 查 IC 序号
    cur.execute(
        "SELECT ISNULL(MAX(TRY_CONVERT(INT, SUBSTRING(RTRIM(IC_NO),7,4))), 0) "
        "FROM IC WITH(NOLOCK) WHERE RTRIM(IC_NO) LIKE %s",
        (f"IC{today}%",))
    seq = int(cur.fetchone()[0] or 0)
    ic_in = f"IC{today}{seq + 1:04d}"
    ic_out = f"IC{today}{seq + 2:04d}"

    # 成品信息
    cur.execute('SELECT NAME, ISNULL(UT,\'\') FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s', (fg_no,))
    pr = cur.fetchone()
    fg_name = g(pr[0]) if pr else ""
    fg_ut = pr[1] if pr else ""

    cur.execute('SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s', (fg_wh,))
    wr = cur.fetchone()
    fg_wh_name = g(wr[0]) if wr else fg_wh

    # 收集所有涉及的调拨单
    transfer_nos = list(dict.fromkeys(c.get("transfer_ic_no","") for c in components if c.get("transfer_ic_no")))
    fld1_val = ",".join(transfer_nos)

    # 查第一条调拨单的补充字段（用于入库单）
    ref_info = {"ref":"","cus":"","ddjh":"","dzhw":0.0,"jzhw":0.0}
    if transfer_nos:
        cur.execute(
            "SELECT ISNULL(指令单号,''),ISNULL(客户,''),ISNULL(DDJH,''),"
            "ISNULL(单重,0),ISNULL(净重,0) FROM IC WITH(NOLOCK) WHERE IC_NO=%s",
            (transfer_nos[0],))
        r = cur.fetchone()
        if r:
           ref_info = {
               "ref": g(r[0]), "cus": g(r[1]), "ddjh": g(r[2]),
               "dzhw": float(danzhong) if danzhong is not None else float(r[3] or 0),
               "jzhw": float(jingzhong) if jingzhong is not None else float(r[4] or 0),
           }

    # ① 入库单 KND=13（1行） WH1=生产仓(增加)
    cur.execute("""
        INSERT INTO IC (IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH1,WH1NAME,WH2,
                        USR,USABLE,ITM,REM,FLD1,指令单号,DDJH,客户,单重,净重)
        VALUES (%s,%s,13,%s,%s,%s,%s,%s,%s,'',
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (ic_in, now_str, fg_no, fg_name, fg_qty, fg_ut, fg_wh, fg_wh_name,
          "phone", 1, 1, rem_in or "完工入库", fld1_val,
          ref_info["ref"], ref_info["ddjh"], ref_info["cus"],
          ref_info["dzhw"], ref_info["jzhw"], "phone"))

    # ② 出库单 KND=23（多行ITM）
    itm = 0
    for comp in components:
        prd_no = comp.get("prd_no","")
        qty = float(comp.get("qty", 0))
        tic = comp.get("transfer_ic_no","")
        if not prd_no or qty <= 0:
            continue
        itm += 1
        cur.execute('SELECT NAME FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s', (prd_no,))
        pr2 = cur.fetchone()
        prd_name = g(pr2[0]) if pr2 else ""
        # 该调拨单的收货仓作WH2（减少仓），WH1=生产仓(来源)
        twh1 = comp.get("transfer_wh1","")
        # 查子件 UT
        cur.execute('SELECT ISNULL(UT,\'\') FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s', (prd_no,))
        pr_out = cur.fetchone()
        comp_ut = pr_out[0] if pr_out else ""
        cur.execute("""
            INSERT INTO IC (IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH1,WH2,WH1NAME,WH2NAME,
                            USR,USABLE,ITM,REM,FLD1,指令单号,DDJH,客户,单重,净重)
            VALUES (%s,%s,23,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (ic_out, now_str, prd_no, prd_name, qty, comp_ut, fg_wh, twh1, fg_wh_name, "",
              "phone", 1, itm, f"完工出库({fg_no}×{fg_qty})", tic,
              ref_info["ref"], ref_info["ddjh"], ref_info["cus"],
              0.0, 0.0))

    conn.commit()
    conn.close()
    return {"ok": True, "ic_in": ic_in, "ic_out": ic_out, "ic_items": itm}


# ── API: 批量完工 ──────────────────────────────────────────────────────────
@app.post("/api/completion/batch")
async def completion_batch(
    payload: dict = Body(...),
    db: str = Query(default="c041"),
):
    """
    批量完工：多成品 → 入库单(KND=13) + 出库单(KND=23)。
    payload: {
      items: [{fg_no, qty, dzhw, jzhw, materials:[{prd_no,ratio,transfer_ic_no,transfer_wh1,ddjh,customer}]}],
      fg_wh, tool_rows: [{code, qty}]
    }
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    items = payload.get("items", [])
    fg_wh = payload.get("fg_wh", "G")
    tool_rows = payload.get("tool_rows", [])
    conn = get_conn(db=db_name)
    if not items:
        return {"error": "缺少完工成品"}
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%y%m")

    cur.execute(
        "SELECT ISNULL(MAX(TRY_CONVERT(INT, SUBSTRING(RTRIM(IC_NO),7,4))), 0) "
        "FROM IC WITH(NOLOCK) WHERE RTRIM(IC_NO) LIKE %s",
        (f"IC{today}%",))
    seq = int(cur.fetchone()[0] or 0)
    ic_in = f"IC{today}{seq + 1:04d}"
    ic_out = f"IC{today}{seq + 2:04d}"

    cur.execute("SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (fg_wh,))
    wr = cur.fetchone()
    fg_wh_name = wr[0] if wr else fg_wh

    # 收集第一条 material 的指令单号/客户作为入库单信息
    all_ddjh, all_customer = '', ''
    for item in items:
        for m in (item.get("materials") or []):
            if not all_ddjh and m.get("ddjh"):
                all_ddjh = m.get("ddjh", "")
            if not all_customer and m.get("customer"):
                all_customer = m.get("customer", "")
            if all_ddjh and all_customer:
                break
        if all_ddjh and all_customer:
            break

    # ── ① 入库单 KND=13（产品 + 工具）──────────────────────────────
    prod_itm = 0
    for item in items:
        fg_no = item.get("fg_no", "")
        if not fg_no:
            continue
        fg_qty = int(item.get("qty") or 1)
        dzhw = float(item.get("danzhong") or 0)
        jzhw = float(item.get("jingzhong") or 0)
        cur.execute("SELECT NAME, ISNULL(UT,'') FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (fg_no,))
        pr = cur.fetchone()
        fg_name_gbk = pr[0] if pr else b''
        fg_ut = pr[1] if pr else ''

        # ①a FG 成品行（WH1=生产仓(增加)，WH2=''）
        FG_IN_COLS = ['IC_NO','IC_DD','IC_KND','PRD_NO','PRD_NAME','QTY','UT','WH1','WH1NAME','WH2',
                       'USR','USABLE','ITM','REM','FLD1','指令单号','客户','单重','净重']
        FG_IN_VALS = [ic_in, now_str, 13, fg_no, fg_name_gbk, fg_qty, fg_ut, fg_wh, fg_wh_name, '',
                       'phone', 1, prod_itm + 1, '完工入库', ic_out, all_ddjh, all_customer, dzhw, jzhw]
        prod_itm += 1
        sql = f"INSERT INTO IC ({','.join(FG_IN_COLS)}) VALUES ({','.join(['%s']*len(FG_IN_COLS))})"
        cur.execute(sql, FG_IN_VALS)

    for tool in tool_rows:
        code = tool.get("code", ""); qty = int(tool.get("qty") or 1)
        if not code:
            continue
        # ①b 工具行（KND=30，WH1=生产仓(减少)，WH2=地面仓(增加)）
        T_IN_COLS = ['IC_NO','IC_DD','IC_KND','PRD_NO','QTY','WH1','WH2','WH1NAME','WH2NAME',
                      'USR','USABLE','ITM','REM']
        T_IN_VALS = [ic_in, now_str, 30, code, qty, 'G', fg_wh, '地面(临时堆放)', fg_wh_name,
                       'phone', 1, prod_itm + 1, f"完工入库；运输工具:{code}"]
        prod_itm += 1
        sql = f"INSERT INTO IC ({','.join(T_IN_COLS)}) VALUES ({','.join(['%s']*len(T_IN_COLS))})"
        cur.execute(sql, T_IN_VALS)

    # ② 出库单 KND=23（子件，不含运输工具）────────────────────
    out_itm = 0
    for item in items:
        fg_no = item.get("fg_no", "")
        if not fg_no:
            continue
        fg_qty = int(item.get("qty") or 1)
        # 读 item 层的字段（前端从调拨单带过来的）
        item_ddjh  = item.get("ddjh", "")
        item_cus   = item.get("customer", "")
        item_dz    = float(item.get("danzhong") or 0)
        item_jz    = float(item.get("jingzhong") or 0)
        for m in (item.get("materials") or []):
            prd_no = m.get("prd_no", "")
            ratio = float(m.get("ratio") or 0)
            comp_qty = ratio * fg_qty
            if not prd_no or comp_qty <= 0:
                continue
            tic = m.get("transfer_ic_no", "")
            twh1 = m.get("transfer_wh1", "")
            cur.execute("SELECT NAME FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (prd_no,))
            pr2 = cur.fetchone()
            prd_name_gbk = pr2[0] if pr2 else b''
            cur.execute("SELECT ISNULL(NAME,'') FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (twh1,))
            r_wh = cur.fetchone()
            twh1_name = r_wh[0] if r_wh else twh1

            # ②a 子件行（WH1=生产仓(减少来源)，WH2=原料仓(增加)，带调拨单字段）
            MAT_OUT_COLS = ['IC_NO','IC_DD','IC_KND','PRD_NO','PRD_NAME','QTY','UT','WH1','WH2','WH1NAME','WH2NAME',
                              'USR','USABLE','ITM','REM','FLD1','指令单号','DDJH','客户','单重','净重']
            MAT_OUT_VALS = [ic_out, now_str, 23, prd_no, prd_name_gbk, comp_qty, '',
                             fg_wh, twh1, fg_wh_name, twh1_name,
                             'phone', 1, out_itm + 1, f"完工出库({fg_no}\u00d7{fg_qty})",
                             tic, item_ddjh, '', item_cus, item_dz, item_jz]
            out_itm += 1
            sql = f"INSERT INTO IC ({','.join(MAT_OUT_COLS)}) VALUES ({','.join(['%s']*len(MAT_OUT_COLS))})"
            cur.execute(sql, MAT_OUT_VALS)

    conn.commit()
    conn.close()
    return {"ok": True, "ic_in": ic_in, "ic_out": ic_out}
@app.get("/api/product/search")
async def product_search(q: str = Query(...), db: str = Query(default="c041")):
    """搜品号/品名，返回下拉建议列表。"""
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    pat = f"%{q}%"
    cur.execute("""
        SELECT TOP 40 PRD_NO, NAME FROM PRDT WITH (NOLOCK)
        WHERE (PRD_NO LIKE %s OR ISNULL(NAME,'') LIKE %s)
          AND USABLE = 1
        ORDER BY PRD_NO
    """, (pat, pat))
    rows = cur.fetchall()
    conn.close()
    return {
        "products": [
            {"prd_no": g(r[0]), "name": g(r[1])}
            for r in rows
        ]
    }


# ── API: 盘点库存查询 ───────────────────────────────────────────────────────
@app.get("/api/stock")
async def stock_query(prd_no: str = Query(...), db: str = Query(default="c041")):
    """
    查 VW_STOCK_DETAIL2 视图，返回品号在所有仓库的库存。
    WH=存放仓库（GBK编码），WH_Name=仓库名称（UTF-16-LE编码）。
    返回字段与前端 renderStock 期望一致：found, prd_no, prd_name, warehouses, total。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    # 先确认品号是否存在
    cur.execute("""
        SELECT TOP 1 NAME FROM PRDT WITH (NOLOCK)
        WHERE PRD_NO = %s AND USABLE = 1
    """, (prd_no,))
    prd_row = cur.fetchone()
    if not prd_row:
        conn.close()
        return {"found": False, "prd_no": prd_no, "prd_name": None, "warehouses": [], "total": 0}

    prd_name = g(prd_row[0])

    # 查所有仓库（含零库存）
    # WH是varchar直接查不用转换，pymssql charset='utf8' 正确解码中文
    cur.execute("""
        SELECT WH,
               WH_Name,
               QTY_WH
        FROM VW_STOCK_DETAIL2 WITH (NOLOCK)
        WHERE PRD_NO = %s
        ORDER BY QTY_WH DESC
    """, (prd_no,))
    rows = cur.fetchall()
    conn.close()

    warehouses = []
    total = 0.0
    for r in rows:
        qty = float(r[2]) if r[2] else 0.0
        total += qty
        # WH/varchar: str → from_hex_vw 直接返回（已是正确字符串）
        # WH_Name/nvarchar: str → from_hex_wh_name 直接返回（已是正确字符串）
        warehouses.append({
            "wh":      from_hex_vw(r[0]),
            "wh_name": from_hex_wh_name(r[1]),
            "qty":     qty,
        })

    return {
        "found": True,
        "prd_no": prd_no,
        "prd_name": prd_name,
        "warehouses": warehouses,
        "total": total,
    }


# ── API: 采购未回 ─────────────────────────────────────────────────────────────
@app.get("/api/pos-unreceived")
async def list_pos_unreceived(
    q: str = Query(default=""),
    customer: str = Query(default=""),
    supplier: str = Query(default=""),
    order_no: str = Query(default=""),
    db: str = Query(default="c041"),
):
    """
    数据源：VW_POS。全数据无日期限制，无制单人过滤。
    筛选条件：PS<>1（未全回）。
    搜索 q：品号/品名/供应商/订单号/指令单号/客户代号 模糊搜索。
    三个筛选：客户代号/供应商/指令单号，AND 叠加。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    filter_params = []
    filter_conds = []
    if customer:
        filter_conds.append("AND p.客户代号 LIKE %s")
        filter_params.append("%" + customer + "%")
    if supplier:
        filter_conds.append("AND p.CUS_NAME LIKE %s")
        filter_params.append("%" + supplier + "%")
    if order_no:
        filter_conds.append("AND p.指令单号 = %s")
        filter_params.append(order_no)
    extra_filter = "\n          ".join(filter_conds)

    if q:
        sl = "%" + q + "%"
        search_cond = ("AND (p.PRD_NO LIKE %s OR p.PRD_NAME LIKE %s "
                      "OR p.CUS_NAME LIKE %s OR p.OS_NO LIKE %s "
                      "OR p.指令单号 LIKE %s OR p.客户代号 LIKE %s)")
        where = search_cond + extra_filter
        params = (sl, sl, sl, sl, sl, sl) + tuple(filter_params)
    else:
        where = extra_filter
        params = tuple(filter_params)

    cur.execute("""
        SELECT COUNT(*) FROM VW_POS p WITH (NOLOCK)
        WHERE ISNULL(p.USABLE, 1) = 1
          AND ISNULL(p.CLS_ID, 0) = 0
          AND ISNULL(p.PS, 1) <> 1
          AND p.OS_NO LIKE 'PO%'
          """ + where, params)
    total = cur.fetchone()[0]

    cur.execute("""
        SELECT p.OS_NO, p.ITM, p.OS_DD, p.CUS_NAME, p.PRD_NO, p.PRD_NAME,
               p.SPC, p.UT, p.QTY, p.UP, p.AMT, p.EST_DD, p.指令单号,
               p.请购数量, p.PS, p.PSQTY, p.PO, p.POQTY, p.客户代号,
               ISNULL(p.QTY, 0) - ISNULL(p.PSQTY, 0) AS 未回数量
        FROM VW_POS p WITH (NOLOCK)
        WHERE ISNULL(p.USABLE, 1) = 1
          AND ISNULL(p.CLS_ID, 0) = 0
          AND ISNULL(p.PS, 1) <> 1
          AND p.OS_NO LIKE 'PO%'
          """ + where + """
        ORDER BY p.OS_DD ASC, p.OS_NO ASC, p.ITM
    """, params)
    rows = cur.fetchall()
    conn.close()

    def gv(v):
        if v is None: return ""
        if isinstance(v, bytes):
            try: return v.decode("latin-1").decode("gbk", errors="ignore")
            except: return str(v)
        s = str(v)
        try: return s.encode("latin-1").decode("gbk", errors="ignore")
        except: return s

    def fv(v):
        try:
            f = float(v)
            return str(round(f, 2)) if f == int(f) else str(f)
        except: return ""

    items = [{
        "os_no":      gv(r[0]),
        "itm":        r[1],
        "os_dd":      gv(r[2]),
        "supplier":   gv(r[3]),
        "prd_no":     gv(r[4]),
        "prd_name":   gv(r[5]),
        "spc":        gv(r[6]),
        "ut":         gv(r[7]),
        "qty":        fv(r[8]),
        "up":         fv(r[9]),
        "amt":        fv(r[10]),
        "est_dd":     gv(r[11]),
        "order_no":   gv(r[12]),
        "请购数量":   fv(r[13]),
        "ps":         r[14],
        "psqty":      fv(r[15]),
        "po":         gv(r[16]),
        "poqty":      fv(r[17]),
        "customer":   gv(r[18]),
        "unreceived":  fv(r[19]),
    } for r in rows]

    return {"items": items, "total": total}


# ── API: 工单列表（已废弃，用 /api/smo）─────────────────────────────────
@app.get("/api/mom")
async def list_mom_legacy():
    return {"error": "请使用 /api/smo", "items": []}


# ── API: 单张工单明细 ─────────────────────────────────────────────────────────
# GET /api/mom/{mo_no}
@app.get("/api/mom/{mo_no}")
async def get_mom(mo_no: str):
    conn = get_conn()
    cur = conn.cursor()

    # 主表
    cur.execute("""
        SELECT MO_NO, MO_DD, FG_NO, FG_NAME, FG_SPC, FG_UT, FG_MARK, FG_QTY,
               DEP, USR, CHK_MAN, REM, APP_ID, SFFG, WH,
               SO_NO_ITM, REF_ITM, STA_DD, EST_DD, ACT_STA_DD, ACT_EST_DD,
               UP, AMT, SAL_NO, CUS_NO, CUS_NAME, 指令单号
        FROM MOM WITH (NOLOCK)
        WHERE MO_NO = %s
    """, (mo_no,))

    rows = cur.fetchall()
    if not rows:
        conn.close()
        return {"error": "工单不存在"}, 404

    COL = {c: i for i, c in enumerate([c[0] for c in cur.description])}
    r = rows[0]

    header = {
        "mo_no":      g(r[COL["MO_NO"]]),
        "mo_dd":      g(r[COL["MO_DD"]]),
        "fg_no":      g(r[COL["FG_NO"]]),
        "fg_name":    g(r[COL["FG_NAME"]]),
        "fg_spc":     g(r[COL["FG_SPC"]]),
        "fg_ut":      g(r[COL["FG_UT"]]),
        "fg_mark":    g(r[COL["FG_MARK"]]),
        "fg_qty":     f(r[COL["FG_QTY"]]),
        "dep":        g(r[COL["DEP"]]),
        "usr":        g(r[COL["USR"]]),
        "chk_man":    g(r[COL["CHK_MAN"]]),
        "rem":        g(r[COL["REM"]]),
        "app_id":     bool(r[COL["APP_ID"]]),
        "sffg":       bool(r[COL["SFFG"]]),
        "wh":         g(r[COL["WH"]]),
        "so_no_itm":  g(r[COL["SO_NO_ITM"]]),
        "ref_itm":    g(r[COL["REF_ITM"]]),
        "sta_dd":     g(r[COL["STA_DD"]]),
        "est_dd":     g(r[COL["EST_DD"]]),
        "act_sta_dd": g(r[COL["ACT_STA_DD"]]),
        "act_est_dd": g(r[COL["ACT_EST_DD"]]),
        "up":         f(r[COL["UP"]]),
        "amt":        f(r[COL["AMT"]]),
        "sal_no":     g(r[COL["SAL_NO"]]),
        "cus_no":     g(r[COL["CUS_NO"]]),
        "cus_name":   g(r[COL["CUS_NAME"]]),
        "order_no":   g(r[COL["指令单号"]]),
    }

    fg_qty_val = header["fg_qty"] or 0

    # BOM 明细：LEV=1 子件 + IC 库存 + 需求数量
    cur.execute("""
        SELECT
            b.PRD_NO,
            b.NAME,
            b.SPC,
            b.UT,
            b.QTY       AS bom_qty,
            CAST(b.QTY * %s AS DECIMAL(15,3)) AS need_qty,
            ic.QTY      AS ic_qty,
            CASE WHEN (ic.QTY >= CAST(b.QTY * %s AS DECIMAL(15,3))
                      OR CAST(b.QTY * %s AS DECIMAL(15,3)) <= 0)
                 THEN 1 ELSE 0 END AS is_ready
        FROM BOM b WITH (NOLOCK)
        OUTER APPLY (
            SELECT TOP 1 QTY
            FROM IC WITH (NOLOCK)
            WHERE PRD_NO = b.PRD_NO AND IC_KND = 13
            ORDER BY IC_DD DESC
        ) ic
        WHERE b.UPGUID = (
            SELECT GUID FROM BOM WITH (NOLOCK)
            WHERE PRD_NO = %s AND LEV = 0
        )
          AND b.LEV = 1
        ORDER BY b.IDX
    """, (fg_qty_val, fg_qty_val, fg_qty_val, header["fg_no"]))

    bom_rows = cur.fetchall()
    conn.close()

    bom_items = []
    for br in bom_rows:
        bom_items.append({
            "prd_no":    g(br[0]),
            "name":      g(br[1]),
            "spc":       g(br[2]),
            "ut":        g(br[3]),
            "bom_qty":   f(br[4]),
            "need_qty":  f(br[5]),
            "ic_qty":    f(br[6]),
            "is_ready":  bool(br[7]),
        })

    return {**header, "bom_items": bom_items}


# ── 调拨单 helpers ──────────────────────────────────────────────────────────

def next_ic_no(prefix: str = "IC", db: str = "C041") -> str:
    """生成下一个 IC 单号 IC{YYMM}{NNNN}"""
    conn = get_conn(db=db)
    cur = conn.cursor()
    today = datetime.now().strftime("%y%m")
    cur.execute(f"""
        SELECT TOP 1 IC_NO FROM IC WITH (NOLOCK)
        WHERE IC_NO LIKE %s AND LEN(IC_NO) > 10
        ORDER BY IC_NO DESC
    """, (f"{prefix}{today}%",))
    r = cur.fetchone()
    if r:
        seq = int(r[0][len(prefix) + 4:]) + 1
    else:
        seq = 1
    conn.close()
    return f"{prefix}{today}{seq:04d}"


def get_wh_stock(wh: str, prd: str) -> float:
    """查某仓库某品号的有效库存 = MM.avail + IC.net"""
    conn = get_conn()
    cur = conn.cursor()
    # MM 可用量（该仓库）
    cur.execute("""
        SELECT ISNULL(SUM(QTY - ISNULL(QTYIC, 0)), 0)
        FROM MM WITH (NOLOCK)
        WHERE PRD_NO = %s AND WH = %s AND USABLE = 1 AND ISNULL(删除, 0) = 0
    """, (prd, wh))
    mm = float(cur.fetchone()[0] or 0)
    # IC 入库 (1/5开头, 该仓库→WH1)
    cur.execute("""
        SELECT ISNULL(SUM(QTY), 0)
        FROM IC WITH (NOLOCK)
        WHERE PRD_NO = %s AND WH1 = %s AND IC_KND / 10 IN (1, 5)
          AND ISNULL(USABLE, 1) = 1 AND ISNULL(删除, 0) = 0
    """, (prd, wh))
    ic_in = float(cur.fetchone()[0] or 0)
    # IC 出库 (2/4开头, 该仓库→WH2)
    cur.execute("""
        SELECT ISNULL(SUM(QTY), 0)
        FROM IC WITH (NOLOCK)
        WHERE PRD_NO = %s AND WH2 = %s AND IC_KND / 10 IN (2, 4)
          AND ISNULL(USABLE, 1) = 1 AND ISNULL(删除, 0) = 0
    """, (prd, wh))
    ic_out = float(cur.fetchone()[0] or 0)
    conn.close()
    return mm + ic_in - ic_out


def suggest_wh2_for_subcontract(prd: str) -> str:
    """外发领料：为品号找有库存的仓库（MM>0 或 IC.net>0），按库存量倒序"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT WH, SUM(ISNULL(QTY-QTYIC, QTY)) AS stock
        FROM MM WITH (NOLOCK)
        WHERE PRD_NO = %s AND USABLE = 1 AND ISNULL(删除, 0) = 0
          AND ISNULL(QTY - ISNULL(QTYIC, 0), 0) > 0
        GROUP BY WH
        UNION ALL
        SELECT WH1 AS WH, SUM(QTY) AS stock
        FROM IC WITH (NOLOCK)
        WHERE PRD_NO = %s AND IC_KND / 10 IN (1, 5)
          AND ISNULL(USABLE, 1) = 1 AND ISNULL(删除, 0) = 0
        GROUP BY WH1
        UNION ALL
        SELECT WH2 AS WH, -SUM(QTY) AS stock
        FROM IC WITH (NOLOCK)
        WHERE PRD_NO = %s AND IC_KND / 10 IN (2, 4)
          AND ISNULL(USABLE, 1) = 1 AND ISNULL(删除, 0) = 0
        GROUP BY WH2
    """, (prd, prd, prd))
    rows = cur.fetchall()
    conn.close()
    # 按库存量倒序
    agg = {}
    for wh, stock in rows:
        if wh:
            agg[wh] = agg.get(wh, 0) + float(stock)
    if not agg:
        return "G"
    best = max(agg.items(), key=lambda x: x[1])
    return best[0] if best[1] > 0 else "G"


def suggest_wh1_for_supplier(cus_no: str) -> str:
    """
    外发领料 WH1 显示名，格式与 _wh_name 一致：简称(代码)
    如 "九盛(20390)"
    """
    if not cus_no:
        return "G"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT TOP 1 简称 FROM CUST WITH (NOLOCK)
        WHERE CUS_NO=%s AND 生成委外仓库=1
    """, (cus_no,))
    r = cur.fetchone()
    conn.close()
    if r and r[0]:
        name = g(r[0])
        return name + "(" + cus_no + ")"
    return "G"


# ── API: 发料（创建调拨单）──────────────────────────────────────────────────
# POST /api/smo/issue
@app.post("/api/smo/issue")
async def issue_transfer(
    items: List[dict] = Body(...),
    db: str = Query(default="c041"),
):
    """
    items: [{smo_no, itm, ref_itm, mo_id, prd_no, qty, wh1, wh2, cus_no, order_no, so_no_itm}]
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = []  # {"knd": 40, "ic_no": "IC26090001", "rows": 3}

    # 按 KND 分组
    prod_rows = [r for r in items if not r.get("mo_id")]   # KND=40 生产
    sub_rows  = [r for r in items if r.get("mo_id")]        # KND=41 外发

    # ── 生产领料 KND=40 ──────────────────────────────────────────────────────
    if prod_rows:
        ic_no = next_ic_no(db=db_name)
        knd = 40
        usr = "Hermes"
        for i, row in enumerate(prod_rows, start=1):
            cur.execute("""
                INSERT INTO IC (IC_NO,IC_DD,IC_KND,USR,USABLE,ITM,SO_NO_ITM,REF_ITM,
                               PRD_NO,QTY,WH1,WH1NAME,WH2,WH2NAME,SALP,UP,AMT,
                               FLD1,FLD2,FLD3,CLS_ID,REM1,EFF_DD,
                               APP_ID,APP_MAN,APP_DD,删除,指令单号)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                ic_no, today_str, knd, usr,
                1, i, row.get("so_no_itm",""), row["ref_itm"],
                row["prd_no"], float(row["qty"]),
                str(row.get("wh1","")), str(row.get("wh1_name","")),
                str(row.get("wh2","")), str(row.get("wh2_name","")),
                None, None, 0.0,
                None, None, None, 0, None, today_str,
                0, None, today_str, 0, str(row.get("order_no","")),
            ))
        conn.commit()
        result.append({"knd": knd, "ic_no": ic_no, "rows": len(prod_rows)})

    # ── 外发加工领料 KND=41（每个供应商一张单）────────────────────────────────
    if sub_rows:
        # 按 cus_no 分组
        by_supplier = {}
        for row in sub_rows:
            key = row.get("cus_no", "_none_")
            by_supplier.setdefault(key, []).append(row)

        for cus_no, rows in by_supplier.items():
            ic_no = next_ic_no(db=db_name)
            knd = 41
            usr = "Hermes"
            # 取供应商名称
            sup_name = ""
            if cus_no and cus_no != "_none_":
                conn2 = get_conn(db=db_name)
                cur2 = conn2.cursor()
                cur2.execute("""
                    SELECT TOP 1 WH1NAME, CUSNAME FROM IC WITH (NOLOCK)
                    WHERE 客户=%s AND IC_KND=41 AND WH1 IS NOT NULL
                    ORDER BY IC_DD DESC
                """, (cus_no,))
                r_sup = cur2.fetchone()
                if r_sup:
                    sup_name = g(r_sup[0]) if r_sup[0] else g(r_sup[1]) if r_sup[1] else ""
                conn2.close()

            for i, row in enumerate(rows, start=1):
                wh1_name_input = str(row.get("wh1", "G"))
                wh1_code = _wh_code(wh1_name_input)  # 名称→代码
                wh2_name_input = str(row.get("wh2", "G"))
                wh2_code = _wh_code(wh2_name_input)  # 名称→代码
                cur.execute("""
                    INSERT INTO IC (IC_NO,IC_DD,IC_KND,USR,USABLE,ITM,SO_NO_ITM,REF_ITM,
                                   PRD_NO,QTY,WH1,WH1NAME,WH2,WH2NAME,CUSNAME,SALP,UP,AMT,
                                   FLD1,FLD2,FLD3,CLS_ID,REM1,EFF_DD,
                                   APP_ID,APP_MAN,APP_DD,删除,客户,指令单号)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    ic_no, today_str, knd, usr,
                    1, i, row.get("so_no_itm",""), row["ref_itm"],
                    row["prd_no"], float(row["qty"]),
                    str(row.get("wh1","")), str(row.get("wh1_name","")),
                    str(row.get("wh2","")), str(row.get("wh2_name","")),
                    sup_name, None, None, 0.0,
                    None, None, None, 0, None, today_str,
                    0, None, today_str, 0,
                    str(cus_no) if cus_no != "_none_" else "",
                    str(row.get("order_no","")),
                ))
            conn.commit()
            result.append({"knd": knd, "ic_no": ic_no, "supplier": cus_no if cus_no != "_none_" else None, "rows": len(rows)})

    conn.close()
    return {"ok": True, "transfers": result}


# ── API: 发料弹窗数据 ────────────────────────────────────────────────────────
@app.post("/api/smo/issue_preview")
async def issue_preview(
    smo_keys: List[dict] = Body(...),
    db: str = Query(default="c041"),
):
    """
    数据来源(C041):
      - KB_工单材料齐料库存明细: 未领料数量(需求), 库存数减领料, 生产数量
      - VW_STOCK_DETAIL2: cur_stock(按料号查所有仓库QTY_WH)
      - 是否齐料/已调拨: 视图后台自动计算,写IC后触发器更新
    写IC单: KND=40生产领料 / KND=41外发加工领料
      WH1=8车间仓(入库), WH2=4原材料仓(生产)或供应商仓(外发)
      REF_ITM=MO_NO+ITM三位
    """
    conn = get_conn(db="C041")
    cur = conn.cursor()

    def g(v):
        """通用: str→直接返回, bytes→GBK解码"""
        if v is None: return ''
        if isinstance(v, bytes):
            try: return v.decode('gbk', errors='ignore')
            except: return str(v)
        return str(v) if v else ''

    def f(v):
        try: return float(str(v).strip().split('\x00')[0])
        except: return 0.0

    def from_hex(v):
        """VARBINARY字段智能解码: ASCII→UTF-16-LE(含CJK无乱码)→GBK(含CJK无乱码)→latin-1"""
        if v is None: return ''
        if isinstance(v, str): return v  # 已解码的str直接返回
        b = bytes(v) if not isinstance(v, bytes) else v
        # 纯ASCII: 直接返回
        try: return b.decode('ascii')
        except: pass
        # UTF-16-LE: 大部分KBnvarchar字段, 验证含CJK且无乱码
        try:
            s = b.decode('utf-16-le')
            has_cjk = any(19968 <= ord(c) <= 40959 for c in s)
            has_garbage = any(0x2000 <= ord(c) <= 0x2FFF for c in s)
            if has_cjk and not has_garbage: return s
        except: pass
        # GBK: 常见中文编码
        try:
            s = b.decode('gbk')
            has_cjk = any(19968 <= ord(c) <= 40959 for c in s)
            has_garbage = any(0x2000 <= ord(c) <= 0x2FFF for c in s)
            if has_cjk and not has_garbage: return s
        except: pass
        # fallback
        # fallback: 双重误解码修复
        # 场景: DB存GBK/latin-1文本, pymssql用UTF-8解码成mojibake
        # 修复: UTF-8误解码→bytes→latin-1/GBK正确解码
        s_utf8 = b.decode('utf-8', errors='ignore')
        try:
            b2 = s_utf8.encode('utf-8', errors='ignore')
            return b2.decode('gbk', errors='ignore')
        except: pass
        try:
            b2 = s_utf8.encode('utf-8', errors='ignore')
            return b2.decode('latin-1', errors='ignore')
        except: pass
        return s_utf8

    rows_out = []
    for sk in smo_keys:
        smo_no = sk["smo_no"]
        itm = sk["itm"]

        # 查KB视图: 工单+材料信息 (VARBINARY避免编码问题)
        cur.execute("""
            SELECT TOP 1
                CONVERT(VARBINARY(100), s.SFLD1) as hSFLD1,
                CONVERT(VARBINARY(100), m.指令单号) as h指令,
                CONVERT(VARBINARY(100), m.FG_NO) as hFG_NO,
                CONVERT(VARBINARY(200), m.FG_NAME) as hFG_NAME,
                m.FG_QTY,
                CONVERT(VARBINARY(100), k.工单号) as h工单号,
                CONVERT(VARBINARY(100), k.材料品号) as h品号,
                CONVERT(VARBINARY(500), k.材料名称) as h名称,
                k.未领料数量, k.总库存, k.库存数减领料, k.ITM,
                CONVERT(VARBINARY(100), k.加工商) as h加工商,
                k.生产数量
            FROM SMO s WITH(NOLOCK)
            JOIN MOM m WITH(NOLOCK) ON s.REF_ITM=m.MO_NO
            JOIN KB_工单材料齐料库存明细 k WITH(NOLOCK) ON k.工单号=m.MO_NO
            WHERE s.SMO_NO=%s AND s.ITM=%s AND s.USABLE=1 AND k.ITM=%s
        """, (smo_no, itm, itm))
        r = cur.fetchone()
        if not r:
            continue

        so_no      = from_hex(r[0])
        order_no   = from_hex(r[1])
        fg_no      = from_hex(r[2])
        fg_name    = from_hex(r[3])
        fg_qty     = f(r[4])
        mo_no      = from_hex(r[5])
        prd_no     = from_hex(r[6])
        prd_name   = from_hex(r[7])
        bom_qty    = f(r[8])
        total_stk  = f(r[9])
        stock_diff = f(r[10])
        mat_itm    = int(r[11]) if r[11] else 0
        supplier   = from_hex(r[12])
        prod_qty   = f(r[13])

        is_prod = (supplier == '')
        shortage = bom_qty
        issued_qty = prod_qty - bom_qty if bom_qty > 0 else 0.0

        # 查该料号所有仓库的库存(VW_STOCK_DETAIL2) - WH/WH_Name用VARBINARY避免编码问题
        pg = prd_no.encode('gbk')
        cur.execute("""
            SELECT CONVERT(VARBINARY(50),WH),CONVERT(VARBINARY(200),WH_Name),QTY_WH
            FROM VW_STOCK_DETAIL2
            WHERE PRD_NO=%s AND QTY_WH>0 ORDER BY QTY_WH DESC
        """, (pg,))
        stock_rows = [(from_hex(r2[0]), from_hex(r2[1]), float(r2[2]) if r2[2] else 0.0)
                      for r2 in cur.fetchall()]

        # 仓库逻辑：
        # - 生产领料(is_prod=True): WH1=车间仓(8), WH2=最大库存仓
        # - 外发加工(is_prod=False): WH1=MOT.WH(供应商仓), WH2=最大库存仓
        # WH1下拉：生产=车间仓；外发=所有ATTRIB=6外发仓(按名称排序)
        # WH2下拉：stock_rows（有库存的实际仓）
        if is_prod:
            wh1 = "8"; wh1_name = "车间仓"
            wh1_choices = [("8", "车间仓")]
            wh2 = max(stock_rows, key=lambda x: x[2])[0] if stock_rows else "8"
            wh2_name = max(stock_rows, key=lambda x: x[2])[1] if stock_rows else "车间仓"
        else:
            # 外发：WH1=MOT.WH（供应商仓=CUS_NO=MY_WH.ATTRIB=6）
            # 查MOT.WH对应的供应商名称和WH代码
            cur.execute("""
                SELECT TOP 1 mt.WH, w.NAME
                FROM MOT mt WITH(NOLOCK)
                JOIN MY_WH w WITH(NOLOCK) ON w.WH=mt.WH AND w.ATTRIB='6'
                WHERE mt.MO_NO=%s AND mt.PRD_NO=%s AND mt.USABLE=1
            """, (mo_no, prd_no.encode('gbk')))
            r3 = cur.fetchone()
            if r3:
                wh1 = from_hex(r3[0]) if r3[0] else ""
                wh1_name = from_hex(r3[1]) if r3[1] else ""
            else:
                wh1 = ""; wh1_name = ""
            # WH1下拉：所有ATTRIB=6外发仓（按名称排序）
            cur.execute("SELECT WH, NAME FROM MY_WH WITH(NOLOCK) WHERE ATTRIB='6' AND USABLE=1 ORDER BY NAME")
            wh1_choices = [(from_hex(r2[0]), from_hex(r2[1])) for r2 in cur.fetchall()]
            # WH2
            wh2 = max(stock_rows, key=lambda x: x[2])[0] if stock_rows else ""
            wh2_name = max(stock_rows, key=lambda x: x[2])[1] if stock_rows else ""

        # 计算总库存（stock_rows已汇总）
        total_stock = sum(r2[2] for r2 in stock_rows) if stock_rows else 0.0

        rows_out.append({
            "smo_no": smo_no, "itm": mat_itm,
            "ref_itm": mo_no, "so_no_itm": so_no,
            "is_prod": is_prod,
            "prd_no": prd_no, "name": prd_name,
            "fg_no": fg_no, "fg_name": fg_name,
            "bom_qty": bom_qty,
            "prod_qty": prod_qty,
            "issued_qty": issued_qty,
            "ut": "",
            "need_qty": bom_qty,
            "cur_stock": total_stock,  # ← V2汇总库存，前端显示用
            "stock_rows": stock_rows,
            "shortage": shortage,
            "qty": max(0.0, shortage),
            "wh1": wh1, "wh1_name": wh1_name,
            "wh1_choices": wh1_choices,  # 外发：所有ATTRIB=6仓
            "wh2": wh2, "wh2_name": wh2_name,
            "supplier_name": supplier,
            "order_no": order_no,
        })

    conn.close()
    return {"rows": rows_out}


def _wh_code(name: str) -> str:
    """仓库名称 → 实际存储代码（用于写库）。尝试短代码、GBK decode、双向查 MY_WH。"""
    if not name:
        return ""
    conn = get_conn()
    cur = conn.cursor()
    # 1. 直接命中
    cur.execute("SELECT TOP 1 WH FROM MY_WH WITH (NOLOCK) WHERE NAME=%s", (name,))
    r = cur.fetchone()
    if r:
        conn.close()
        return g(r[0])
    # 2. GBK decode → 再查（MY_WH 里存的可能是 GBK bytes 的 latin1 形态）
    try:
        name_gbk = name.encode('utf-8').decode('gbk', errors='ignore')
        if name_gbk != name:
            cur.execute("SELECT TOP 1 WH FROM MY_WH WITH (NOLOCK) WHERE NAME=%s", (name_gbk,))
            r = cur.fetchone()
            if r:
                conn.close()
                return g(r[0])
    except Exception:
        pass
    conn.close()
    # 3. 尝试直接作为代码
    return name


def _get_smo_supplier(mo_no: str) -> tuple:
    """
    查 SMO/工单是否外发，返回 (is_prod, supplier_name, supplier_no)
    - is_prod=True → 生产领料
    - is_prod=False → 外发，supplier_no=CUST.CUS_NO（即供应商仓库代码）
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT CAST(MO_ID AS INT) AS MO_ID
        FROM MOM WITH (NOLOCK) WHERE MO_NO=%s
    """, (mo_no,))
    r = cur.fetchone()
    mo_id = bool(r[0]) if r else False
    conn.close()
    if mo_id:
        conn2 = get_conn()
        cur2 = conn2.cursor()
        # 正确 JOIN：MOM.CUS_NO → CUST.CUS_NO，供应商仓库代码 = CUS_NO
        cur2.execute("""
            SELECT TOP 1 c.CUS_NO, c.简称
            FROM MOM mo WITH (NOLOCK)
            JOIN CUST c WITH (NOLOCK) ON c.CUS_NO = mo.CUS_NO
            WHERE mo.MO_NO=%s AND c.生成委外仓库=1
        """, (mo_no,))
        r2 = cur2.fetchone()
        if r2 and r2[0]:
            sup_no = g(r2[0])
            sup_name = g(r2[1]) if r2[1] else sup_no
            conn2.close()
            return False, sup_name, sup_no

        # 回退：从 KND=41 历史取
        cur2.execute("""
            SELECT TOP 1 客户 FROM IC WITH (NOLOCK)
            WHERE REF_ITM=%s AND IC_KND=41 AND 客户 IS NOT NULL
            ORDER BY IC_DD DESC
        """, (mo_no,))
        r3 = cur2.fetchone()
        sup_no = g(r3[0]) if r3 and r3[0] else ""
        sup_name = ""
        if sup_no:
            cur2.execute("SELECT TOP 1 简称 FROM CUST WITH (NOLOCK) WHERE CUS_NO=%s", (sup_no,))
            r4 = cur2.fetchone()
            sup_name = g(r4[0]) if r4 and r4[0] else sup_no
        conn2.close()
        return False, sup_name, sup_no
    else:
        return True, "", ""


def _wh_name(wh: str) -> str:
    """仓库代码 → 中文名。WH 可能是 GBK-latin1 乱码、纯中文名、或短代码。"""
    if not wh:
        return ""
    conn = get_conn()
    cur = conn.cursor()
    # 尝试原始值（短代码如 '4' 直接命中）
    cur.execute("SELECT NAME FROM MY_WH WITH (NOLOCK) WHERE WH=%s", (wh,))
    r = cur.fetchone()
    if r:
        conn.close()
        return g(r[0])
    # 尝试 GBK decode（WH 存的是 GBK bytes 的 latin-1 乱码）
    try:
        wh_gbk = wh.encode('latin-1').decode('gbk')
        cur.execute("SELECT NAME FROM MY_WH WITH (NOLOCK) WHERE WH=%s", (wh_gbk,))
        r = cur.fetchone()
        if r:
            conn.close()
            return g(r[0])
    except Exception:
        pass
    conn.close()
    # 回退：GBK encode 再查
    try:
        return wh.encode('latin-1').decode('gbk')
    except Exception:
        return wh


# ── API: 盘点升单（批量写入单张IC）─────────────────────────────────────────
@app.post("/api/stock/batch")
async def stock_batch(
    items: List[dict] = Body(...),
    direction: str = Query(..., description="out 或 in"),
    db: str = Query(default="c041"),
):
    """
    将 pending 列表中多个产品合并写入一张 IC 单。
    direction=out → KND=23, WH2=发货仓
    direction=in  → KND=13, WH1=收货仓
    body: [{prd_no, wh, qty, rem, ref_itm, cus_no, sup_name, ddjh}]
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    today = datetime.now().strftime('%y%m')
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 取今天最大流水，生成 IC_NO
    cur.execute(f"""
        SELECT ISNULL(MAX(TRY_CAST(SUBSTRING(IC_NO,7,4) AS INT)), 0)
        FROM IC WITH(NOLOCK)
        WHERE IC_NO LIKE %s AND LEN(IC_NO) = 10
    """, (f"IC{today}%",))
    seq = (cur.fetchone()[0] or 0) + 1
    ic_no = f"IC{today}{seq:04d}"
    knd = 23 if direction == 'out' else 13

    # WH 字段名（out 用 WH2，in 用 WH1）
    wh_col = 'WH2' if direction == 'out' else 'WH1'
    wh_name_col = 'WH2NAME' if direction == 'out' else 'WH1NAME'

    itm = 0
    results = []
    for item in items:
        prd_no = item.get("prd_no", "")
        wh = item.get("wh", "")
        qty = float(item.get("qty", 0))
        rem = item.get("rem", "") or ""
        ref_itm = item.get("ref_itm", "") or ""
        cus_no  = item.get("cus_no", "") or ""
        sup_name = item.get("sup_name", "") or ""
        ddjh   = item.get("ddjh", "") or ""
        itm += 1

        # 查品名
        cur.execute("SELECT NAME FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (prd_no,))
        pr = cur.fetchone()
        prd_name = g(pr[0]) if pr else ""

        # 查仓库名
        wh_name = ""
        if wh:
            cur.execute("SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (wh,))
            r_wh = cur.fetchone()
            wh_name = g(r_wh[0]) if r_wh else ""

        # 数据驱动INSERT
        dzhw = float(item.get("danzhong", 0) or 0)
        jzhw = float(item.get("jingzhong", 0) or 0)
        COLS = ['IC_NO','IC_DD','IC_KND','PRD_NO','PRD_NAME','QTY',wh_col,wh_name_col,
                'USR','USABLE','ITM','REM','FLD1',
                '指令单号','客户','CUSNAME','DDJH','单重','净重']
        VALS = [ic_no, now_str, knd, prd_no, prd_name, qty, wh, wh_name,
                'phone', 1, itm, rem, ic_no,
                ref_itm, cus_no, sup_name, ddjh, dzhw, jzhw]
        assert len(COLS) == len(VALS), f"列{len(COLS)}!=值{len(VALS)}"
        sql = f"INSERT INTO IC ({','.join(COLS)}) VALUES ({','.join(['%s']*len(COLS))})"
        cur.execute(sql, VALS)
        results.append({"ic_no": ic_no, "prd_no": prd_no, "wh": wh, "qty": qty, "itm": itm})

    conn.commit()
    conn.close()
    return {"ok": True, "ic_no": ic_no, "count": len(results), "items": results}


# ── API: 派工单列表（读KB表）────────────────────────────────────────────
@app.get("/api/smo/dispatch")
async def smo_dispatch_list(db: str = Query(default="c041")):
    """
    读取 KB_工单材料齐料 视图，按 SMO 单号聚合。
    齐料 = 总库存 >= 未领料数量。
    返回: [{smo_no, mo_no, cmd, fg_no, fg_name, fg_qty, wh2, materials:[], all_ready, closed}]
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    # 查 KB 视图，按工单+项聚合，再关联 SMO 信息
    cur.execute("""
        SELECT
            ISNULL(a.指令单号,'') as CMD,
            ISNULL(a.工单号,'') as MO_NO,
            ISNULL(v.SMO_NO,'') as SMO_NO,
            ISNULL(v.FG_NO,'') as FG_NO,
            ISNULL(v.FG_NAME,'') as FG_NAME,
            CAST(ISNULL(a.生产数量,0) AS FLOAT) as FG_QTY,
            a.项, a.材料品号, a.材料名称,
            CAST(ISNULL(a.未领料数量,0) AS FLOAT) as NEED_QTY,
            CAST(ISNULL(a.总库存,0) AS FLOAT) as STOCK
        FROM dbo.KB_工单材料齐料 a
        JOIN VW_MOM v ON a.工单号 = v.MO_NO
        ORDER BY a.指令单号, v.SMO_NO, a.工单号, a.项
    """)
    rows = cur.fetchall()
    conn.close()

    # 按 MO_NO 聚合（每个工单 = 一张派工单卡片）
    from collections import defaultdict
    by_mo = defaultdict(lambda: {
        'smo_no': '', 'cmd': '', 'fg_no': '', 'fg_name': '',
        'fg_qty': 0.0, 'materials': [], 'closed': False, 'all_ready': True
    })

    for r in rows:
        cmd = g(r[0]); mo = g(r[1]); smo_no = g(r[2])
        fg_no = g(r[3]); fg_name = g(r[4]); fg_qty = float(r[5])
        itm = int(r[6]); prd = g(r[7]); prd_name = g(r[8])
        need = float(r[9]); stock = float(r[10])
        ready = stock >= need
        info = by_mo[mo]
        info['smo_no'] = smo_no
        info['cmd'] = cmd
        info['fg_no'] = fg_no
        info['fg_name'] = fg_name
        info['fg_qty'] = fg_qty
        info['materials'].append({
            'itm': itm, 'prd_no': prd, 'prd_name': prd_name,
            'need_qty': need, 'stock': stock, 'is_ready': ready
        })
        if not ready:
            info['all_ready'] = False

    items = []
    for mo in sorted(by_mo.keys()):
        info = by_mo[mo]
        items.append({
            'mo_no': mo,
            'smo_no': info['smo_no'],
            'cmd': info['cmd'],
            'fg_no': info['fg_no'],
            'fg_name': info['fg_name'],
            'fg_qty': info['fg_qty'],
            'materials': info['materials'],
            'all_ready': info['all_ready'],
            'closed': False,
        })
    return {'items': items}


# ── API: 开工（生成调拨单 IC KND=30）──────────────────────────────────
@app.post("/api/smo/dispatch")
async def smo_dispatch_issue(payload: dict = Body(...), db: str = Query(default="c041")):
    """
    开工发料：生成 IC KND=30，REF_ITM=SMO单号，WH1=原材料仓，WH2=车间仓(8)。
    完工后关闭 MOT.CLS_ID=1。
    body: {smo_no: "...", mo_no: "...", cmd: "...", wh2: "...", materials:[{prd_no, need_qty, wh1, wh1_name}]}
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    now_str = datetime.now().strftime('%Y-%m-%d')
    today_ic_start = 'IC' + now_str.replace('-', '')[:8] + '001'

    cur.execute("""
        SELECT ISNULL(MAX(IC_NO),'IC00000000')
        FROM IC WITH(NOLOCK)
        WHERE IC_NO LIKE 'IC26%' AND LEN(IC_NO)=10
    """)
    last_ic = cur.fetchone()[0]
    seq = int(last_ic[2:]) + 1

    smo_no = payload.get('smo_no', '')
    mo_no = payload.get('mo_no', '')
    cmd = payload.get('cmd', '')
    wh2 = payload.get('wh2', '8')
    materials = payload.get('materials', [])
    usr = payload.get('usr', 'Hermes')

    # 查 WH2 仓库名称
    cur.execute("SELECT ISNULL(NAME,'') FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (wh2,))
    r = cur.fetchone()
    wh2_name = r[0] if r else wh2

    results = []
    itm = 1
    for mat in materials:
        prd_no = mat.get('prd_no', '')
        need_qty = float(mat.get('need_qty', 0))
        wh1 = mat.get('wh1', '4')
        wh1_name = mat.get('wh1_name', wh1)
        if need_qty <= 0 or not prd_no:
            continue
        # 生成 IC 单号
        ic_no = 'IC' + str(seq).zfill(8)
        seq += 1

        cur.execute("SELECT NAME, UT FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (prd_no,))
        r = cur.fetchone()
        prd_name_d = gbk(r[0]) if r else prd_no
        ut_d = r[1] if r else ""

        cur.execute("""
            INSERT INTO IC (IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH1,WH2,
                            WH1NAME,WH2NAME,USR,USABLE,ITM,REM,FLD1,指令单号)
            VALUES (%s,%s,30,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s)
        """, (ic_no, now_str, prd_no, prd_name_d, need_qty, ut_d,
              wh1, wh2, wh1_name, wh2_name, usr, itm, '派工开工', ic_no, smo_no))
        results.append({'ic_no': ic_no, 'prd_no': prd_no, 'wh1': wh1, 'wh2': wh2, 'qty': need_qty, 'itm': itm})
        itm += 1

    conn.commit()

    # 关闭 MOT（同一工单的所有行）
    if mo_no:
        cur.execute("""
            UPDATE MOT SET CLS_ID=1, 是否派工=1
            WHERE MO_NO=%s AND ISNULL(CLS_ID,0)=0
        """, (mo_no,))
        conn.commit()

    conn.close()
    return {'ok': True, 'smo_no': smo_no, 'mo_no': mo_no, 'ic_count': len(results), 'items': results}


# ── PMC 生产计划 ──────────────────────────────────────────────────────────────

def _mps_bom_tree(fg_no, db_conn, qty=1, depth=0, parent=None, _seen=None):
    """
    递归 BOM 展开，返回 [(prd_no, prd_name, demand_qty, knd, parent, depth, bom_ratio)]。
    demand_qty: 展开后的需求量 = qty × bom_ratio
    bom_ratio: 原始 BOM 配比 = QTY / QTY_BAS（用于 PMC 分配计算）
    depth: BOM 层级（L1=1, L2=2, ...）
    """
    cur = db_conn.cursor()
    if _seen is None: _seen = set()
    if fg_no in _seen or depth >= 8: return []
    _seen.add(fg_no)
    # 找 LEV=0 header
    cur.execute("SELECT GUID FROM BOM WITH(NOLOCK) WHERE PRD_NO=%s AND LEV=0", (fg_no,))
    hdr = cur.fetchone()
    if not hdr:
        return []
    hdr_guid = hdr[0]

    # 取 LEV=1 直接子件
    cur.execute("""
        SELECT b.PRD_NO, p.NAME, b.QTY, b.KND, b.QTY_BAS
        FROM BOM b WITH(NOLOCK)
        LEFT JOIN PRDT p WITH(NOLOCK) ON p.PRD_NO = b.PRD_NO
        WHERE b.UPGUID=%s AND b.LEV=1 AND ISNULL(b.删除,0)=0
        ORDER BY b.IDX
    """, (hdr_guid,))
    rows = cur.fetchall()

    result = []
    for r in rows:
        child_prd = g(r[0])
        child_name = g(r[1])
        child_qty_per = float(r[2] or 1)
        child_knd = str(r[3] or '')
        child_qty_bas = float(r[4] or 1)
        # 需求数量 = qty × (BOM.QTY / BOM.QTY_BAS)
        demand_qty = round(qty * child_qty_per / child_qty_bas, 4)
        # 原始 BOM 配比（用于 PMC 分配计算）
        bom_ratio = child_qty_per / child_qty_bas

        if str(child_knd) == '4':
            # KND=4 原料：直接返回
            result.append((child_prd, child_name, demand_qty, child_knd, fg_no, depth+1, bom_ratio))
        else:
            # KND=3 中间件：先加入自身，再递归展开子件
            result.append((child_prd, child_name, demand_qty, child_knd, fg_no, depth+1, bom_ratio))
            if str(child_knd) == '3':
                sub = _mps_bom_tree(child_prd, db_conn, qty=demand_qty, depth=depth+1,
                                     parent=child_prd, _seen=_seen.copy())
                if sub:
                    result.extend(sub)
    return result


_PROD_WH_CACHE = None

def _prod_wh_set(db_conn):
    """
    生产仓 WH 集合（按 MY_WH.ATTRIB）：
      ATTRIB=5 车间仓、ATTRIB=6 外发仓(含待定厂商)
    其余（ATTRIB=1 库位/地面仓、ATTRIB=2 半成品仓、ATTRIB=3 原材料仓、ATTRIB=4 辅料/不良/废/呆）为原材料仓。
    """
    global _PROD_WH_CACHE
    if _PROD_WH_CACHE is not None:
        return _PROD_WH_CACHE
    cur = db_conn.cursor()
    cur.execute("SELECT WH FROM MY_WH WITH(NOLOCK) WHERE ATTRIB IN ('5','6')")
    _PROD_WH_CACHE = {g(r[0]).strip() for r in cur.fetchall() if r[0] is not None}
    return _PROD_WH_CACHE


def _v2_stock(prd_nos, db_conn):
    """
    查 V2 实时库存，分生产仓/原材料仓。
    生产仓: MY_WH.ATTRIB in (5车间/6外发)
    原材料仓: 其他
    返回 {prd_no: {prod_wh, prod_qty, prod_av, mat_wh, mat_qty, mat_av,
                   total_qty, total_av}}  (全仓合计用于中间件判断)
    """
    if not prd_nos:
        return {}
    prod_whs = _prod_wh_set(db_conn)
    cur = db_conn.cursor()
    placeholders = ','.join(['%s'] * len(prd_nos))
    cur.execute(f"""
        SELECT PRD_NO, WH,
               QTY_WH, QTY_ON_RSV, QTY_ON_PRC,
               QTY_ON_INS, QTY_ON_SCR, QTY_ON_WAY, QTY_ON_ODR
        FROM VW_STOCK_DETAIL2 WITH(NOLOCK)
        WHERE PRD_NO IN ({placeholders})
    """, tuple(prd_nos))
    rows = cur.fetchall()
    stock = {}
    for r in rows:
        prd = g(r[0])
        wh = g(r[1])
        qty_wh = float(r[2] or 0)
        reserved = float(r[3] or 0) + float(r[4] or 0) + float(r[5] or 0) + float(r[6] or 0)
        qty_av = max(0, qty_wh - reserved)
        qty_on_way = float(r[7] or 0)
        is_prod = wh in prod_whs
        key = 'prod' if is_prod else 'mat'
        if prd not in stock:
            stock[prd] = {'prod_wh': '', 'prod_qty': 0.0, 'prod_av': 0.0, 'prod_qty_on_way': 0.0,
                           'mat_wh': '',  'mat_qty': 0.0,  'mat_av': 0.0,  'mat_qty_on_way': 0.0,
                           'total_qty': 0.0, 'total_av': 0.0, 'total_qty_on_way': 0.0}
        stock[prd]['total_qty'] += qty_wh
        stock[prd]['total_av']  += qty_av
        stock[prd]['total_qty_on_way'] += qty_on_way
        # ponytail: 累加各仓数量（不取最大，保持总和）
        stock[prd][f'{key}_qty'] += qty_wh
        stock[prd][f'{key}_av']  += qty_av
        stock[prd][f'{key}_qty_on_way'] += qty_on_way
    return stock


def _v2_stock_detail(prd_nos, db_conn):
    """
    查 V2 实时库存（逐仓明细）。
    返回 {prd_no: [(wh, wh_name, qty, av), ...]}  按 av 降序
    """
    if not prd_nos:
        return {}
    cur = db_conn.cursor()
    placeholders = ','.join(['%s'] * len(prd_nos))
    cur.execute(f"""
        SELECT WH, WH_Name, PRD_NO,
               QTY_WH, QTY_ON_RSV, QTY_ON_PRC, QTY_ON_INS, QTY_ON_SCR, QTY_ON_WAY, QTY_ON_ODR
        FROM VW_STOCK_DETAIL2 WITH(NOLOCK)
        WHERE PRD_NO IN ({placeholders})
    """, tuple(prd_nos))
    rows = cur.fetchall()
    prod_whs = _prod_wh_set(db_conn)
    result = {}
    for r in rows:
        wh = str(r[0] or '').strip()
        wh_name = str(r[1] or '').strip()
        prd_no = str(r[2] or '').strip()
        if not prd_no:
            continue
        qty_wh = float(r[3] or 0)
        qty_av = max(0, qty_wh - float(r[4] or 0) - float(r[5] or 0) - float(r[6] or 0) - float(r[7] or 0) - float(r[8] or 0) - float(r[9] or 0))
        qty_on_way = float(r[8] or 0)
        qty_on_odr = float(r[9] or 0)
        is_prod = wh in prod_whs
        if prd_no not in result:
            result[prd_no] = []
        # (wh, wh_name, qty_wh, qty_av, qty_on_way, qty_on_odr, is_prod)
        result[prd_no].append((wh, wh_name, round(qty_wh, 2), round(qty_av, 2), round(qty_on_way, 2), round(qty_on_odr, 2), is_prod))
    for prd_no in result:
        result[prd_no].sort(key=lambda x: x[3], reverse=True)
    return result


@app.get("/api/pmc/pos_unanalyzed")
async def pmc_pos_unanalyzed(
    q: str = Query(default=""),
    db: str = Query(default="c041")
):
    """
    返回 MP=0 未分析的 POS 行（按 VW_POS 视图）。
    q: 品号/品名模糊搜索。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    if q:
        cur.execute(f"""
            SELECT TOP 50 OS_NO,ITM,SO_NO_ITM,PRD_NO,PRD_NAME,SPC,QTY,CUS_NO,CUS_NAME,指令单号,EST_DD,
                   ISNULL(QTY,0)-ISNULL(QTYPS,0) AS so_remain
            FROM VW_POS WITH(NOLOCK)
            WHERE MP=0 AND USABLE=1 AND APP_ID=1 AND OS_NO LIKE 'SO%'
              AND (
                (ISNULL(指令单号,'') = '' OR ISNULL(指令单号,'') = 'TEST01')
                 OR (ISNUMERIC(指令单号)=1 AND CAST(指令单号 AS INT) >= 7721)
              )
              AND (OS_NO LIKE %s OR 指令单号 LIKE %s OR PRD_NO LIKE %s OR PRD_NAME LIKE %s)
            ORDER BY 指令单号, OS_NO, ITM
        """, (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        cur.execute("""
            SELECT TOP 50 OS_NO,ITM,SO_NO_ITM,PRD_NO,PRD_NAME,SPC,QTY,CUS_NO,CUS_NAME,指令单号,EST_DD,
                   ISNULL(QTY,0)-ISNULL(QTYPS,0) AS so_remain
            FROM VW_POS WITH(NOLOCK)
            WHERE MP=0 AND USABLE=1 AND APP_ID=1 AND OS_NO LIKE 'SO%'
              AND (
                (ISNULL(指令单号,'') = '' OR ISNULL(指令单号,'') = 'TEST01')
                 OR (ISNUMERIC(指令单号)=1 AND CAST(指令单号 AS INT) >= 7721)
              )
            ORDER BY 指令单号, OS_NO, ITM
        """)

    rows = cur.fetchall()
    conn.close()
    return {
        "items": [{
            "os_no":     g(r[0]),
            "itm":       r[1],
            "so_no_itm": g(r[2]),
            "prd_no":    g(r[3]),
            "prd_name":  g(r[4]),
            "spc":       g(r[5]),
            "qty":       float(r[6] or 0),
            "cus_no":    g(r[7]),
            "cus_name":  g(r[8]),
            "ref":       g(r[9]),
            "est_dd":    str(r[10])[:10] if r[10] else "",
            "so_remain": float(r[11] or 0),
        } for r in rows]
    }


@app.get("/api/pmc/preview_mps")
async def pmc_preview_mps(
    so_no_itm: str = Query(...),
    prd_no: str = Query(...),
    qty: float = Query(...),
    db: str = Query(default="c041")
):
    """
    返回 BOM 展开预览（不写库）。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    comp_rows = _mps_bom_tree(prd_no, conn, qty=qty)
    all_prds = [prd_no] + [r[0] for r in comp_rows]
    stock_map = _v2_stock(all_prds, conn)
    stock_detail = _v2_stock_detail(all_prds, conn)
    conn.close()

    # 库存汇总
    fg_stock = stock_map.get(prd_no, {})

    def make_row(c_prd, c_name, c_qty, c_depth, c_knd, stock_map_entry, stock_det):
        """统一公式：需求=max(0,BOM量-原材料仓-生产仓-在途)"""
        sm = stock_map_entry or {}
        mat_qty   = round(sm.get('mat_qty', 0), 2)
        prod_qty  = round(sm.get('prod_qty', 0), 2)
        mat_way   = round(sm.get('mat_qty_on_way', 0), 2)
        prod_way  = round(sm.get('prod_qty_on_way', 0), 2)
        total_way = mat_way + prod_way
        det = stock_det or []
        qty_on_odr = round(det[0][5], 2) if len(det) > 0 and len(det[0]) > 5 else 0.0
        # 在途只显示，不参与计算
        real_demand = max(0, c_qty - mat_qty - prod_qty)
        total_stock = mat_qty + prod_qty
        gap = round(real_demand - total_stock, 2)
        # wh_detail: 全部明细（兼容前端调整弹窗）
        # mat_detail / prod_detail: 原材料仓/生产仓分组（显示用）
        mat_detail = [(x[0], x[1], x[2], x[3], x[4], x[5]) for x in det if not x[6]]
        prod_detail = [(x[0], x[1], x[2], x[3], x[4], x[5]) for x in det if x[6]]
        return {
            "prd_no": c_prd, "prd_name": c_name,
            "qty": round(c_qty, 4),
            "qty_on_odr": qty_on_odr,
            "real_demand": round(real_demand, 4),
            "mat_qty": mat_qty, "prod_qty": prod_qty,
            "qty_on_way": round(total_way, 2),
            "total_stock": round(total_stock, 2),
            "gap": gap,
            "is_fg": False, "depth": c_depth,
            "knd": c_knd,
            "raw_stock": mat_qty + prod_qty,
            "contribution": 0.0,
            "wh": '',
            "qty_wh": round(mat_qty + prod_qty, 2),
            "qty_av": round(sm.get('mat_av', 0) + sm.get('prod_av', 0), 2),
            "wh_detail": stock_det,
            "mat_detail": mat_detail,
            "prod_detail": prod_detail,
        }

    # ITM=1 成品行
    det = stock_detail.get(prd_no, [])
    fg_way = round(fg_stock.get('mat_qty_on_way', 0) + fg_stock.get('prod_qty_on_way', 0), 2)
    fg_on_odr = round(det[0][5], 2) if len(det) > 0 and len(det[0]) > 5 else 0.0
    fg_raw = round(fg_stock.get('mat_qty', 0) + fg_stock.get('prod_qty', 0), 2)
    fg_real_demand = max(0, qty - fg_stock.get('mat_qty', 0) - fg_stock.get('prod_qty', 0))
    fg_total_stock = fg_raw

    os_no = so_no_itm[:-3] if len(so_no_itm) > 3 else so_no_itm
    rows = [{
        "prd_no": prd_no, "qty": round(qty, 4),
        "qty_on_odr": fg_on_odr,
        "real_demand": round(fg_real_demand, 4),
        "mat_qty": round(fg_stock.get('mat_qty', 0), 2),
        "prod_qty": round(fg_stock.get('prod_qty', 0), 2),
        "qty_on_way": fg_way,
        "total_stock": round(fg_total_stock, 2),
        "gap": round(fg_real_demand - fg_total_stock, 2),
        "is_fg": True, "depth": 0,
        "knd": None,
        "raw_stock": fg_raw,
        "contribution": 0.0,
        "wh": '', "qty_wh": fg_raw,
        "qty_av": round(fg_stock.get('mat_av', 0) + fg_stock.get('prod_av', 0), 2),
        "so_no": os_no, "so_no_itm": so_no_itm,
        "wh_detail": det,
    }]

    # BOM 子件行（comp_rows: child,name,demand_qty,knd,parent,depth,bom_ratio）
    for c_prd, c_name, c_qty, c_knd, c_parent, c_depth, c_bom_ratio in comp_rows:
        c_stock = stock_map.get(c_prd, {})
        c_det = stock_detail.get(c_prd, [])
        rows.append(make_row(c_prd, c_name, c_qty, c_depth, c_knd, c_stock, c_det))

    # -----------------------------------------------------------
    # 自顶向下 BOM 配比分需求
    #
    # 公式：子件需求 = 父件缺口 × BOM配比
    # 父件缺口 = 父件需求 - 父件库存
    # - KND=3 半成品：raw_stock = mat+prod 库存
    # - KND=4 原料：raw_stock = mat+prod 库存
    # -----------------------------------------------------------
    prd_to_idx = {}
    for i, r in enumerate(rows):
        if not r['is_fg']:
            prd_to_idx[r['prd_no']] = i

    # 建立 parent → children 映射
    parent_to_children = {}
    child_info = {}   # child_prd → (bom_ratio, parent)
    for c_prd, c_name, c_qty, c_knd, c_parent, c_depth, c_bom_ratio in comp_rows:
        if c_parent not in parent_to_children:
            parent_to_children[c_parent] = []
        parent_to_children[c_parent].append(c_prd)
        child_info[c_prd] = (c_bom_ratio, c_parent)

    # FG 真实需求 = qty（成品销售未出）
    rows[0]['real_demand'] = qty

    def allocate(parent_prd, parent_demand, parent_stock):
        """
        自顶向下分配 BOM 需求。
        公式：子件需求 = 父件缺口 × (子件用量 / 母件底数) = parent_gap × bom_ratio
        父件缺口 = max(0, 父件需求 - 父件库存)
        中间件有子件则递归继续分配；无子件则叶子，需求到此为止。
        """
        children = parent_to_children.get(parent_prd, [])
        for child_prd in children:
            cidx = prd_to_idx.get(child_prd)
            if cidx is None:
                continue
            child_row = rows[cidx]
            bom_ratio, _ = child_info[child_prd]
            # 父件缺口
            parent_gap = max(0, parent_demand - parent_stock)
            # 子件需求 = 父件缺口 × BOM配比
            child_row['real_demand'] = parent_gap * bom_ratio
            # 递归向下，把子件的需求和库存传给下一层
            allocate(child_prd, child_row['real_demand'], child_row['raw_stock'])

    allocate(prd_no, qty, 0)

    # 计算最终 gap
    for r in rows:
        r['gap'] = round(r['real_demand'] - r['total_stock'], 2)

    return {"items": rows, "so_no": os_no}


@app.post("/api/pmc/generate_mps")
async def generate_mps(
    body: dict = Body(...),
    db: str = Query(default="c041")
):
    """
    选中的多个 POS 行 → 一张 MPS 单（相同成品数量合并）。
    ITM=1~N: 顺序编号，每张 POS 一个成品 ITM + 其 BOM 子件 ITM
    """
    items = body.get("items", [])
    if not items:
        return {"error": "没有选中订单"}

    valid = [it for it in items
             if it.get("so_no_itm") and it.get("prd_no") and float(it.get("qty", 0)) > 0]
    if not valid:
        return {"error": "没有有效订单"}

    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%y%m")
    mps_date = datetime.now().strftime("%Y-%m-%d")

    # 合并：相同成品号 + 相同销售订单 才合并数量
    merged = {}   # key = (prd_no, os_no) -> {qty, so_items}
    for it in valid:
        p = it["prd_no"]
        os = it.get("so_no") or (it["so_no_itm"][:-3] if len(it["so_no_itm"]) > 3 else it["so_no_itm"])
        key = (p, os)
        if key not in merged:
            merged[key] = {"qty": 0.0, "so_items": [], "os_no": os}
        merged[key]["qty"] += float(it["qty"])
        merged[key]["so_items"].append(it["so_no_itm"])

    # 查第一条 POS 的客户/价格信息（用于 MPS 单头）
    first_so = valid[0]["so_no_itm"]
    cur.execute("""
        SELECT p.CUS_NO, p.CUS_NAME, p.指令单号,
               p.EST_DD, p.UP, p.UT, p.PRD_NAME, p.SPC
        FROM POS p WITH(NOLOCK) WHERE p.SO_NO_ITM=%s
    """, (first_so,))
    pr = cur.fetchone()
    cus_no   = g(pr[0]) if pr else ''
    cus_name = g(pr[1]) if pr else ''
    ref_no   = g(pr[2]) if pr else ''
    est_dd   = pr[3] if pr else None
    up       = float(pr[4] or 0) if pr else 0
    ut       = pr[5] or 'PCE'
    fg_name  = g(pr[6]) if pr else ''
    spc      = g(pr[7]) if pr else ''

    # MPS 序号
    cur.execute("""
        SELECT ISNULL(MAX(TRY_CAST(SUBSTRING(MPS_NO,7,4) AS INT)), 0)
        FROM MPS WITH(NOLOCK)
        WHERE MPS_NO LIKE %s AND LEN(MPS_NO)=10
    """, (f"MP{today}%",))
    seq = (cur.fetchone()[0] or 0) + 1
    mps_no = f"MP{today}{seq:04d}"

    # 收集所有成品 + 子件，查询 V2 库存
    all_fg = list(merged.keys())
    stock_map = dict(_v2_stock(all_fg, conn)) if all_fg else {}
    # _v2_stock 返回 dict，但外面会调 .get()，需保持 dict 格式

    all_mps_rows = []
    itm_counter = 0
    # {prd_no: {mat_wh, mat_qty, mat_av}}
    # _v2_stock 返回 {prd_no: {'prod_wh':...,'mat_wh':...}}
    sub_stock_cache = {}   # prd_no -> {prod_*, mat_*}

    for (prd_no, os_no), info in merged.items():
        qty = info["qty"]
        so_items = info["so_items"]

        # 成品行 ITM
        itm_counter += 1
        fg_stock = stock_map.get(prd_no, {})
        all_mps_rows.append({
            'itm': itm_counter,
            'prd_no': prd_no,
            'prd_name': fg_name,
            'spc': spc,
            'ut': ut,
            'qty': qty,
            'wh': '',
            'wh_name': '',
            'qty_wh': round(fg_stock.get('prod_qty', 0), 2),
            'qty_av': round(fg_stock.get('prod_av', 0), 2),
            'is_fg': True,
            'so_no_itm': so_items[0],
            'ref': ref_no,
        })

        # BOM 递归展开
        comp_rows = _mps_bom_tree(prd_no, conn, qty=qty)
        if not comp_rows:
            continue

        # 查子件库存
        sub_prds = [r[0] for r in comp_rows]
        sub_stock = _v2_stock(sub_prds, conn)
        sub_stock_cache.update(sub_stock)

        for c_prd, c_name, c_qty, c_knd in comp_rows:
            itm_counter += 1
            cs = sub_stock_cache.get(c_prd, {})
            all_mps_rows.append({
                'itm': itm_counter,
                'prd_no': c_prd,
                'prd_name': c_name,
                'spc': '',
                'ut': 'PCE',
                'qty': round(c_qty, 4),
                'wh': cs.get('mat_wh', ''),
                'wh_name': '',
                'qty_wh': round(cs.get('mat_qty', 0), 2),
                'qty_av': round(cs.get('mat_av', 0), 2),
                'is_fg': False,
                'so_no_itm': so_items[0],
                'ref': ref_no,
            })

    # 批量 INSERT
    for row in all_mps_rows:
        cur.execute("""
            INSERT INTO MPS (
                MPS_NO,MPS_DD,USR,USABLE,ITM,
                CUS_NO,CUS_NAME,FG_NO_SO,SO_NO_ITM,REF_ITM,
                PRD_NO,PRD_NAME,SPC,UT,
                QTY,WH,WH_NAME,
                QTY_WH,QTY_AV,
                指令单号,EST_DD,UP,
                BOM,STA_DD
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            )
        """, (
            mps_no, mps_date, 'Hermes', 1, row['itm'],
            cus_no, cus_name, prd_no, row['so_no_itm'], row['ref'],
            row['prd_no'], row['prd_name'], row['spc'], row['ut'],
            row['qty'], row['wh'], row['wh_name'],
            row['qty_wh'], row['qty_av'],
            row['ref'], est_dd, up,
            1, mps_date,
        ))

    conn.commit()
    conn.close()
    return {'ok': True, 'results': [{'mps_no': mps_no, 'total_itm': itm_counter, 'items': len(valid), 'fg_count': len(merged)}]}


# ── PMC 缺口 → MO + QD 专用 BOM 祖先链 ──────────────────────────────────────
def _pmc_bom_ancestors(fg_no, db_conn, _seen=None):
    """
    找 fg_no 的所有 BOM 祖先节点。
    返回列表 [直接父件, 祖父件, ...]，
    path_ratio: 叶子需求 × path_ratio = 该祖先的需求量。
    """
    if _seen is None:
        _seen = set()
    if fg_no in _seen:
        return []
    _seen.add(fg_no)

    cur = db_conn.cursor()
    # 查该品号在 BOM 表中的 LEV=0 header
    cur.execute("""
        SELECT GUID FROM BOM WITH(NOLOCK)
        WHERE PRD_NO=%s AND LEV=0
    """, (fg_no,))
    hdr = cur.fetchone()
    if not hdr:
        return []
    hdr_guid = hdr[0]

    # 取 LEV=1 直接父件（一品可能有多个父件）
    cur.execute("""
        SELECT b.PRD_NO, p.NAME, b.QTY, b.KND, b.QTY_BAS, b.UPGUID
        FROM BOM b WITH(NOLOCK)
        LEFT JOIN PRDT p WITH(NOLOCK) ON p.PRD_NO = b.PRD_NO
        WHERE b.UPGUID=%s AND b.LEV=1 AND ISNULL(b.删除,0)=0
    """, (hdr_guid,))
    parent_rows = cur.fetchall()

    ancestors = []
    for pr in parent_rows:
        parent_prd  = g(pr[0])
        parent_name = g(pr[1])
        parent_knd  = str(pr[3] or '')
        ratio_here  = float(pr[2] or 1) / max(float(pr[4] or 1), 1e-9)
        parent_upguid = pr[5]

        ancestors.append({
            "prd_no": parent_prd,
            "prd_name": parent_name,
            "knd": parent_knd,
            "path_ratio": ratio_here,
        })

        # 递归找祖父件
        if parent_upguid:
            cur.execute("""
                SELECT b.PRD_NO, p.NAME, b.QTY, b.KND, b.QTY_BAS, b.UPGUID
                FROM BOM b WITH(NOLOCK)
                LEFT JOIN PRDT p WITH(NOLOCK) ON p.PRD_NO = b.PRD_NO
                WHERE b.UPGUID=%s AND b.LEV=1 AND ISNULL(b.删除,0)=0
            """, (parent_upguid,))
            for gpr in cur.fetchall():
                gp_ratio = float(gpr[2] or 1) / max(float(gpr[4] or 1), 1e-9)
                gp_prd   = g(gpr[0])
                gp_name  = g(gpr[1])
                gp_knd   = str(gpr[3] or '')
                gp_upguid = gpr[5]

                ancestors.append({
                    "prd_no": gp_prd,
                    "prd_name": gp_name,
                    "knd": gp_knd,
                    "path_ratio": ratio_here * gp_ratio,
                })

                # 再递归一层（两层够用：BOM 通常 2~3 层）
                if gp_upguid:
                    cur.execute("""
                        SELECT b.PRD_NO, p.NAME, b.QTY, b.KND, b.QTY_BAS, b.UPGUID
                        FROM BOM b WITH(NOLOCK)
                        LEFT JOIN PRDT p WITH(NOLOCK) ON p.PRD_NO = b.PRD_NO
                        WHERE b.UPGUID=%s AND b.LEV=1 AND ISNULL(b.删除,0)=0
                    """, (gp_upguid,))
                    for ggpr in cur.fetchall():
                        ggp_ratio = float(ggpr[2] or 1) / max(float(ggpr[4] or 1), 1e-9)
                        ggprd = g(ggpr[0])
                        ggpn  = g(ggpr[1])
                        ggpkn = str(ggpr[3] or '')
                        ancestors.append({
                            "prd_no": ggprd,
                            "prd_name": ggpn,
                            "knd": ggpkn,
                            "path_ratio": ratio_here * gp_ratio * ggp_ratio,
                        })

    return ancestors


@app.post("/api/pmc/preview_generate")
async def pmc_preview_generate(
    body: dict = Body(...),
    db: str = Query(default="c041"),
):
    """
    预览 PMC 选中行 → 将生成的 MO 工单 + QD 请购单（不写库）。
    Input:  {items: [{so_no_itm, prd_no, qty}]}
    Output: {mo: [...], qd: [{supplier, rows: [...]}]}
    """
    items = body.get("items", [])
    if not items:
        return {"mo": [], "qd": []}

    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    # 去重：相同 (so_no_itm, prd_no) 取 qty 最大的
    seen = {}
    for it in items:
        key = (it.get("so_no_itm", ""), it.get("prd_no", ""))
        q = float(it.get("qty") or 0)
        if key not in seen or q > seen[key]["qty"]:
            seen[key] = {"so_no_itm": key[0], "prd_no": key[1], "qty": q}
    uniq = list(seen.values())

    mo_map = {}    # fg_no -> MO 信息
    qd_map = {}    # supplier -> [{prd_no, prd_name, qty, est_dd, 指令单号}]

    for it in uniq:
        so_no_itm = it["so_no_itm"]
        prd_no    = it["prd_no"]
        leaf_qty  = float(it["qty"])
        if leaf_qty <= 0:
            continue

        # POS 信息
        cur.execute("""
            SELECT p.指令单号, p.EST_DD, p.CUS_NAME, p.PRD_NAME,
                   ISNULL(v.上次购买厂商, '待定') as supplier
            FROM POS p WITH(NOLOCK)
            LEFT JOIN VW_POS v WITH(NOLOCK) ON v.SO_NO_ITM = p.SO_NO_ITM
            WHERE p.SO_NO_ITM=%s
        """, (so_no_itm,))
        pr = cur.fetchone()
        ref_no   = g(pr[0]) if pr else ''
        est_dd   = pr[1].strftime("%Y-%m-%d") if pr and pr[1] else ''
        pos_cus  = g(pr[2]) if pr else ''
        pos_name = g(pr[3]) if pr else ''
        supplier = g(pr[4]) if pr else '待定'

        # BOM 祖先链
        ancestors = _pmc_bom_ancestors(prd_no, conn)
        if not ancestors:
            # 无 BOM → 叶子本身是原料，直接请购
            cur.execute("SELECT NAME FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (prd_no,))
            nm = cur.fetchone()
            mat_name = g(nm[0]) if nm else ''
            qd_map.setdefault(supplier, []).append({
                "prd_no": prd_no, "prd_name": mat_name,
                "qty": leaf_qty, "est_dd": est_dd, "指令单号": ref_no,
            })
            continue

        # 祖先链：每个祖先生成一张 MO
        for anc in ancestors:
            anc_qty = round(leaf_qty * anc["path_ratio"], 4)
            fg = anc["prd_no"]
            if fg not in mo_map:
                cur.execute("SELECT ISNULL(p.WH,'') FROM PRDT p WITH(NOLOCK) WHERE p.PRD_NO=%s", (fg,))
                wh_row = cur.fetchone()
                wh = g(wh_row[0]) if wh_row else ''
                cur.execute("SELECT ISNULL(NAME,'') FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (wh,))
                wh_nm = cur.fetchone()
                wh_name = g(wh_nm[0]) if wh_nm else ''
                mo_map[fg] = {
                    "fg_no": fg, "fg_name": anc["prd_name"],
                    "qty": anc_qty, "wh": wh, "wh_name": wh_name,
                    "指令单号": ref_no, "so_no_itm": so_no_itm,
                    "est_dd": est_dd, "knd": anc["knd"],
                }
            else:
                mo_map[fg]["qty"] += anc_qty

        # 叶子本身请购
        cur.execute("SELECT NAME FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (prd_no,))
        nm = cur.fetchone()
        mat_name = g(nm[0]) if nm else ''
        qd_map.setdefault(supplier, []).append({
            "prd_no": prd_no, "prd_name": mat_name,
            "qty": leaf_qty, "est_dd": est_dd, "指令单号": ref_no,
        })

    conn.close()

    return {
        "mo": [{**v, "qty": round(v["qty"], 4)} for v in mo_map.values()],
        "qd": [{"supplier": sup, "rows": rows} for sup, rows in qd_map.items() if rows],
    }


@app.post("/api/pmc/generate")
async def pmc_generate(
    body: dict = Body(...),
    db: str = Query(default="c041"),
):
    """
    生成 MO 工单 + QD 请购单，同时回写 MPS.MO_NO。
    Input: {items: [{so_no_itm, prd_no, qty}]}
    Output: {ok, mo: [...], qd_no, mo_no}
    """
    items = body.get("items", [])
    if not items:
        return {"error": "没有选中订单"}

    # 去重
    seen = {}
    for it in items:
        key = (it.get("so_no_itm", ""), it.get("prd_no", ""))
        q = float(it.get("qty") or 0)
        if key not in seen or q > seen[key]["qty"]:
            seen[key] = {"so_no_itm": key[0], "prd_no": key[1], "qty": q}
    uniq = list(seen.values())

    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_yy = datetime.now().strftime("%y%m")
    today_full = datetime.now().strftime("%Y-%m-%d")
    est_dd_default = (datetime.now() + timedelta(days=15)).strftime("%Y-%m-%d")

    try:
        # 预分配单号
        def _next_no(prefix, tbl, col):
            prefix_str = f"{prefix}{today_yy}"
            cur.execute(f"""
                SELECT ISNULL(MAX(CAST(RIGHT({col},4) AS INT)), 0)
                FROM {tbl} WITH(NOLOCK)
                WHERE LEFT({col},{len(prefix_str)}) = %s AND LEN({col})={len(prefix_str)+4}
            """, (prefix_str,))
            row = cur.fetchone()
            val = row[0] if row else 0
            return f"{prefix}{today_yy}{(val or 0) + 1:04d}"

        mo_no = _next_no("MO", "MOM", "MO_NO")
        qd_no = _next_no("QD", "QTS", "QT_NO")

        mo_inserted = []
        qd_itm = 0

        for it in uniq:
            so_no_itm = it["so_no_itm"]
            prd_no    = it["prd_no"]
            leaf_qty  = float(it["qty"])
            if leaf_qty <= 0:
                continue

            # POS 信息
            cur.execute("""
                SELECT p.指令单号, p.EST_DD,
                       ISNULL(v.上次购买厂商, '待定') as supplier
                FROM POS p WITH(NOLOCK)
                LEFT JOIN VW_POS v WITH(NOLOCK) ON v.SO_NO_ITM = p.SO_NO_ITM
                WHERE p.SO_NO_ITM=%s
            """, (so_no_itm,))
            pr = cur.fetchone()
            ref_no   = g(pr[0]) if pr else ''
            est_dd   = pr[1].strftime("%Y-%m-%d") if pr and pr[1] else est_dd_default
            supplier = g(pr[2]) if pr else '待定'

            # BOM 祖先链
            ancestors = _pmc_bom_ancestors(prd_no, conn)

            # ── MO ─────────────────────────────────────────────
            seen_fg = set()
            for anc in ancestors:
                fg = anc["prd_no"]
                if fg in seen_fg:
                    continue
                seen_fg.add(fg)
                fg_qty = round(leaf_qty * anc["path_ratio"], 4)
                if fg_qty <= 0:
                    continue
                cur.execute("SELECT ISNULL(p.WH,'') FROM PRDT p WITH(NOLOCK) WHERE p.PRD_NO=%s", (fg,))
                wh_row = cur.fetchone()
                wh = g(wh_row[0]) if wh_row else ''

                cur.execute("""
                    INSERT INTO MOM (MO_NO,MO_DD,USR,USABLE,MO_ID,FG_NO,FG_QTY,
                                    WH,指令单号,SO_NO_ITM,STA_DD,EST_DD,REM)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    mo_no, today_full, 'phone', 1, 0,
                    fg, fg_qty, wh, ref_no, so_no_itm,
                    today_full, est_dd,
                    f"MPS缺口生成 ref={ref_no}",
                ))
                mo_inserted.append({"mo_no": mo_no, "fg_no": fg, "fg_qty": fg_qty})

        # ── QD：所有叶子行 ─────────────────────────────────────
        qd_itm = 0
        for it in uniq:
            leaf_qty = float(it["qty"])
            if leaf_qty <= 0:
                continue
            so_no_itm = it["so_no_itm"]
            prd_no    = it["prd_no"]

            cur.execute("""
                SELECT p.指令单号, p.EST_DD, p.CUS_NAME
                FROM POS p WITH(NOLOCK) WHERE p.SO_NO_ITM=%s
            """, (so_no_itm,))
            pr = cur.fetchone()
            ref_no   = g(pr[0]) if pr else ''
            est_dd   = pr[1].strftime("%Y-%m-%d") if pr and pr[1] else est_dd_default
            supplier = g(pr[2]) if pr else '待定'

            qd_itm += 1
            cur.execute("""
                INSERT INTO QTS (QT_NO,QT_ID,QT_DD,USR,USABLE,CUS_NO,ITM,
                                CUS_NAME,PRD_NO,PRD_NAME,QTY,UT,
                                EST_DD,指令单号,CLS_ID,AMT,UP)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                qd_no, 'QD', today_full, 'phone', 1, '20399',
                qd_itm, supplier, prd_no, '', leaf_qty, 'PCE',
                est_dd, ref_no, 0, 0, 0,
            ))

        # MPS 回写 MO_NO
        so_itms = list({it["so_no_itm"] for it in uniq})
        for so_itm in so_itms:
            cur.execute("""
                UPDATE MPS SET MO_NO=%s WHERE SO_NO_ITM=%s AND ISNULL(MO_NO,'')=''
            """, (mo_no, so_itm))

        try:
            conn.commit()
        except Exception as e:
            if e.args[0] == 2627:
                conn.rollback()
                return {"error": "单号已被占用，请刷新后重试（勿重复提交）"}
            raise

        return {
            "ok": True,
            "mo": mo_inserted,
            "qd_no": qd_no,
            "mo_no": mo_no,
            "qd_count": qd_itm,
        }

    except Exception as e:
        conn.rollback()
        return {"error": str(e)}
    finally:
        conn.close()


# 运输工具列表（PMC 调整时选用的包装/运输载具）
TRANSPORT_TOOLS = [
    ("0300202105",        "AEG周转用胶框蓝框1.05KG"),
    ("0300202135",        "AEG周转用胶框蓝框1.35KG"),
    ("030020216",         "AEG周转用胶框蓝框1.6KG"),
    ("030020220",         "AEG周转用胶框黄框2.0KG"),
    ("030020222",         "AEG周转用胶框蓝色2.2KG"),
    ("030020223",         "AEG周转用胶框蓝色3.0KG"),
    ("0300020110010011",  "AEG周转用木垫板100*100*11CM"),
    ("030020224",         "木箱"),
    ("tietong-1",         "铁桶"),
]

# ── PMC 库存调整 ─────────────────────────────────────────────────────────────
@app.post("/api/pmc/adjust_stock")
async def pmc_adjust_stock(
    body: dict = Body(...),
    db: str = Query(default="c041")
):
    """
    PMC 预览里调整材料仓库存。
    输入数量 vs 当前库存：多则13入库，少则23出库。
    body: {"prd_no": "...", "wh": "...", "input_qty": 100, "current_qty": 968,
           "transport_tool": "0300202105", "transport_qty": 50}
    IC.REM 记录：运输工具:编号 名称 数量
    """
    prd_no = body.get("prd_no", "")
    wh = body.get("wh", "")
    input_qty = float(body.get("input_qty", 0))
    current_qty = float(body.get("current_qty", 0))
    transport_tool = body.get("transport_tool", "")  # 运输工具品号
    transport_qty = float(body.get("transport_qty", 0))

    if not prd_no or not wh:
        return {"error": "品号和仓库不能为空"}
    if input_qty < 0:
        return {"error": "数量不能为负"}
    if input_qty == current_qty:
        return {"ok": True, "msg": "库存相同，无需调整"}

    diff = input_qty - current_qty
    is_in = diff > 0
    knd = 13 if is_in else 23
    qty = abs(diff)

    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()

    today = datetime.now().strftime("%y%m")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 查当天最大流水
    cur.execute("""
        SELECT ISNULL(MAX(TRY_CAST(SUBSTRING(IC_NO,7,4) AS INT)), 0)
        FROM IC WITH(NOLOCK)
        WHERE IC_NO LIKE %s AND LEN(IC_NO)=10
    """, (f"IC{today}%",))
    seq = (cur.fetchone()[0] or 0) + 1
    ic_no = f"IC{today}{seq:04d}"

    # 查品号名称
    cur.execute("SELECT NAME, UT FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (prd_no,))
    pr = cur.fetchone()
    prd_name = g(pr[0]) if pr else ""
    ut = pr[1] if pr else ""

    # 查仓库名称
    wh_name = ""
    if wh:
        cur.execute("SELECT NAME FROM MY_WH WITH(NOLOCK) WHERE WH=%s", (wh,))
        r_wh = cur.fetchone()
        wh_name = g(r_wh[0]) if r_wh else ""

    # 构建 REM 备注（含运输工具）
    rem_parts = ["PMC调整"]
    if transport_tool and transport_qty > 0:
        tool_name = next((n for c, n in TRANSPORT_TOOLS if c == transport_tool), transport_tool)
        rem_parts.append(f"运输工具:{transport_tool} {tool_name} {int(transport_qty)}件")
    rem = "；".join(rem_parts)

    # KND=13 入库写 WH1，KND=23 出库写 WH2
    if knd == 13:
        cur.execute("""
            INSERT INTO IC (IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH1,WH1NAME,
                            USR,USABLE,ITM,REM)
            VALUES (%s,%s,13,%s,%s,%s,%s,%s,%s,'Hermes',1,1,%s)
        """, (ic_no, now_str, prd_no, prd_name, qty, ut, wh, wh_name, rem))
    else:
        cur.execute("""
            INSERT INTO IC (IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH2,WH2NAME,
                            USR,USABLE,ITM,REM)
            VALUES (%s,%s,23,%s,%s,%s,%s,%s,%s,'Hermes',1,1,%s)
        """, (ic_no, now_str, prd_no, prd_name, qty, ut, wh, wh_name, rem))

    conn.commit()
    conn.close()
    return {
        "ok": True,
        "ic_no": ic_no,
        "knd": knd,
        "prd_no": prd_no,
        "wh": wh,
        "qty": qty,
        "msg": f"{'入库' if is_in else '出库'} IC={ic_no} 品号={prd_no} 仓库={wh} 数量={qty}"
    }


# ── 运行 ──────────────────────────────────────────────────────────────────────
