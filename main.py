"""
安而固 ERP — 请购单手机端
FastAPI 后端 + 响应式 SPA
"""
import os
import pymssql
import re
import time
from collections import deque
from datetime import datetime, timedelta
from fastapi import FastAPI, Query, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
from datetime import datetime

# ── DB 连接 ──────────────────────────────────────────────────────────────────
_REQUIRED_ENV = ("ERP_DB_HOST", "ERP_DB_USER", "ERP_DB_PASSWORD")

def get_conn(db: str = None):
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "缺少数据库环境变量: " + ", ".join(missing) +
            " —— 请参照 .env.example 配置 .env 后重启服务"
        )
    return pymssql.connect(
        server=os.environ["ERP_DB_HOST"],
        user=os.environ["ERP_DB_USER"],
        password=os.environ["ERP_DB_PASSWORD"],
        database=db or os.environ.get("ERP_DB_NAME") or "C041",
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
    if isinstance(v, (bytes, bytearray)):
        # varbinary 取出的字节：正常行是 nvarchar 的 UTF-16LE 字节，历史脏行是原始 GBK 字节。
        # 不能只看有没有 NUL：纯中文的 UTF-16LE 名（如「产品自粘标签」）一个 NUL 都没有。
        # 两种都试，按「控制字符少 + 可打印字符多」挑。
        b = bytes(v)
        best, best_score = None, None
        for enc in ("utf-16le", "gbk"):
            if enc == "utf-16le" and len(b) % 2:
                continue
            try:
                s = b.decode(enc).rstrip("\x00")
            except Exception:
                continue
            bad = sum(1 for ch in s if ord(ch) < 32 and ch != "\t")
            good = sum(1 for ch in s if 32 <= ord(ch) < 127 or 0x4E00 <= ord(ch) <= 0x9FFF)
            sc = (bad, -good)
            if best_score is None or sc < best_score:
                best, best_score = s, sc
        if best is not None:
            return best
        return b.decode("latin-1", errors="replace")
    if hasattr(v, "strftime"):  # datetime
        return v.strftime("%Y-%m-%d")
    s = str(v)
    try:
        return s.encode("latin-1").decode("gbk", errors="replace")
    except Exception:
        return s

def _jz_fallback(dzhw, jzhw, qty):
    """净重兜底：净重缺省/<=0 但单重>0 且数量>0 时，按 单重(g)×数量÷1000 补算（保留 3 位，与前端 calcJZ 一致）。
    其余情况一律尊重前端传入的值（包括显式 0、以及任何非 0 的怪值，绝不擅自覆盖）。
    用字符串格式化而非 round()：与 JS 的 toFixed(3) 舍入行为最接近。
    """
    try:
        d = float(dzhw or 0); j = float(jzhw or 0); q = float(qty or 0)
    except (TypeError, ValueError):
        return jzhw
    if j <= 0 and d > 0 and q > 0:
        return float(f"{d * q / 1000:.3f}")
    return j   # 返回解析后的 float：缺省/None/'' → 0.0，保持各接口原有 "(x or 0)" 的落库语义（不能落 NULL）

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
    """, (usr,)
          + ((f"%{prd_no}%",) if prd_no else ())
          + ((f"%{order_no}%",) if order_no else ())
          + ((f"%{supplier}%",) if supplier else ())
          + ((page - 1) * page_size, page_size))   # OFFSET/FETCH 在 SQL 里位于筛选条件之后

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
    """返回所有仓库（含无库存的外发仓），供调拨弹窗 TO 下拉使用。

    排除不可选的内部仓（1 辅料 / 2 成品 / 3 半成品 / 4 原材料 / 5 不良品 / 6 废品 /
    7 呆料 / 20000 测试供应商 —— MAK 2026-10-02）。
    车间仓（WH 以 8 起头，含 "8 车间仓" 这类写法）排最前，调拨/入库最常用。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    cur.execute("""
        SELECT WH, NAME
        FROM MY_WH WITH(NOLOCK)
        WHERE USABLE = 1
          AND WH NOT IN ('1', '2', '3', '4', '5', '6', '7', '20000')
        ORDER BY CASE WHEN WH LIKE '8%' THEN 0 ELSE 1 END, WH
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
    # 数量防御：qty<=0 会写出 QTY=0 的幽灵账（前端已拦，这里做后端兜底）。
    # 必须先全量校验、再写任何一行 —— /api/stock/in 的 commit 在循环内，中途 return 会留下"已提交的部分行"
    for _vi, _vit in enumerate(items, 1):
        try:
            _vq = float(_vit.get("qty") or 0)
        except (TypeError, ValueError):
            _vq = 0
        if _vq <= 0:
            return {"error": "第 %d 行数量必须大于 0（%s）" % (_vi, _vit.get("prd_no", ""))}

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
                            USR,USABLE,ITM,REM,CUSNAME,
                            指令单号,DDJH,客户,单重,净重)
            VALUES (%s,%s,23,%s,%s,%s,%s,%s,%s,'phone',1,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (ic_no, now_str,
              prd_no, prd_name, qty, ut, wh2, wh2_name,
              itm, rem, sup_name, ref_itm, ddjh, cus_no,
              float(item.get("danzhong", 0) or 0),
              _jz_fallback(item.get("danzhong", 0), item.get("jingzhong", 0), qty)))
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
    # 数量防御：qty<=0 会写出 QTY=0 的幽灵账（前端已拦，这里做后端兜底）。
    # 必须先全量校验、再写任何一行 —— /api/stock/in 的 commit 在循环内，中途 return 会留下"已提交的部分行"
    for _vi, _vit in enumerate(items, 1):
        try:
            _vq = float(_vit.get("qty") or 0)
        except (TypeError, ValueError):
            _vq = 0
        if _vq <= 0:
            return {"error": "第 %d 行数量必须大于 0（%s）" % (_vi, _vit.get("prd_no", ""))}

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
                            USR,USABLE,ITM,REM,CUSNAME,
                            指令单号,DDJH,客户,单重,净重)
            VALUES (%s,%s,13,%s,%s,%s,%s,%s,%s,'phone',1,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (ic_no, now_str, prd_no, prd_name, qty, ut, wh1, wh1_name,
              itm, rem, sup_name, ref_itm, ddjh, cus_no,
              float(item.get("danzhong", 0) or 0),
              _jz_fallback(item.get("danzhong", 0), item.get("jingzhong", 0), qty)))
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
    # 数量防御：qty<=0 会写出 QTY=0 的幽灵账（前端已拦，这里做后端兜底）。
    # 必须先全量校验、再写任何一行 —— /api/stock/in 的 commit 在循环内，中途 return 会留下"已提交的部分行"
    for _vi, _vit in enumerate(items, 1):
        try:
            _vq = float(_vit.get("qty") or 0)
        except (TypeError, ValueError):
            _vq = 0
        if _vq <= 0:
            return {"error": "第 %d 行数量必须大于 0（%s）" % (_vi, _vit.get("prd_no", ""))}

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
        jzhw    = _jz_fallback(dzhw, jzhw, qty)   # 净重兜底（<=0 且单重>0 且数量>0 → 补算）

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

    where = "WHERE t.IC_KND = 30 AND t.USABLE = 1 AND ISNULL(t.删除, 0) = 0 AND LEFT(t.PRD_NO, 5) NOT IN ('03002','03000')"
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
            CONVERT(VARCHAR(10), MIN(t.IC_DD), 120) AS ic_dd,   -- 调拨日期（KND=30 那行的单据日期）
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
            -- ⚠ 必须按品号分组：一张 KND=30 单含多个品号时，不按品号会把整单扣料算到每个品号头上
            SELECT FLD1, PRD_NO, SUM(QTY) AS done_qty
            FROM IC WITH(NOLOCK)
            WHERE IC_KND = 23 AND ISNULL(FLD1, '') <> ''
            GROUP BY FLD1, PRD_NO
        ) x ON x.FLD1 = t.IC_NO AND x.PRD_NO = t.PRD_NO
        {where}
        GROUP BY t.IC_NO, t.PRD_NO, p.NAME, t.WH1, w.NAME, x.done_qty,
                 t.指令单号, t.客户, t.DDJH, t.单重, t.净重
        ORDER BY t.IC_NO DESC
    """, args if args else None)
    rows = cur.fetchall()
    conn.close()

    items = []
    for r in rows:
        total = float(r[6] or 0)      # 索引整体后移 1（SELECT 里 IC_NO 后插了 ic_dd）
        done = float(r[7] or 0)
        remaining = total - done
        if remaining > 0:
            items.append({
                "ic_no":         g(r[0]),
                "ic_dd":         g(r[1]) or '',
                "prd_no":        g(r[2]),
                "prd_name":      g(r[3]),
                "wh1":           g(r[4]),
                "wh1_name":      g(r[5]),
                "total_qty":     total,
                "transfer_qty":  done,
                "remaining_qty": remaining,
                "ref":   g(r[8]),
                "cus":   g(r[9]),
                "ddjh":  g(r[10]),
                "dzhw":  r[11] or 0,
                "jzhw":  r[12] or 0,
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

    # 查成品名称
    cur.execute("SELECT NAME FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s AND USABLE=1", (fg_no,))
    prd_row = cur.fetchone()
    fg_name = g(prd_row[0]) if prd_row else ''

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
        LEFT JOIN IC y WITH(NOLOCK) ON y.IC_KND=23 AND y.FLD1=t.IC_NO AND y.PRD_NO=t.PRD_NO AND ISNULL(y.FLD1,'')<>''
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
        "fg_name": fg_name,
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
    # ⚠️ varchar 列（PRD_NO）读出来是 latin-1 乱码，必须过 g() 还原，
    #    否则下游 8 个 key 全部对不上（品号乱码 + 品名空 + 库存假 0 + 中文根打不开）
    prd_to_root = {g(r[1]): r[0] for r in cur.fetchall()}  # prd_no → LEV=0 GUID

    cur.execute("""
        SELECT c.GUID, c.PRD_NO, c.LEV, c.IDX, c.QTY, c.KND,
               c.UPGUID, h.PRD_NO AS parent_prd
        FROM BOM c WITH(NOLOCK)
        JOIN BOM h WITH(NOLOCK) ON c.UPGUID = h.GUID AND h.LEV=0
        WHERE ISNULL(c.删除,0)=0
    """)
    # 结构: GUID, PRD_NO, LEV, IDX, QTY, KND, UPGUID, parent_prd
    # 只在读边界还原 PRD_NO / parent_prd（GUID/UPGUID 是 ASCII UUID 不动），
    # prd_info / child_map / parent_children / node_map 全部派生于此，随之自动对齐
    edges = [(e[0], g(e[1]), e[2], e[3], e[4], e[5], e[6], g(e[7]))
             for e in cur.fetchall()]  # 所有 LEV=1 边

    # 建立 prd → (name, knd) 字典
    # 先从 BOM 的 LEV=0 根获取成品名（GUID=PRD_NO 时）
    prd_info = {}   # prd_no → (_, knd)：A 步后只需 knd，名字在建树后按 all_prds 查
    for e in edges:
        guid, prd, lev, idx, qty, knd, upguid, parent_prd = e
        if prd not in prd_info:
            prd_info[prd] = ('', knd)  # 名字待后面批量查 PRDT 填充

    # 成品根不在 prd_info 里，补充一下
    if fg_no not in prd_info:
        prd_info[fg_no] = ('', None)

    # child_map: parent → [(child_prd, qty_per_unit, knd, guid, upguid)]
    # edges 里: prd=子件, parent_prd=父件
    child_map = {}
    for e in edges:
        guid, prd, lev, idx, qty, knd, upguid, parent_prd = e
        child_map.setdefault(parent_prd, []).append((prd, f(qty) or 1, knd, guid, upguid))

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

        # A 步：只取 knd（来自 edges，不花 SQL）；名字建树后只查这棵树用到的品号
        knd = prd_info.get(prd, (None, None))[1]
        qty_this = qty_from_parent
        max_depth = max(max_depth, depth)

        tree_nodes.append({
            "prd_no":       prd,
            "name":         "",   # A 步：建树后按 all_prds 批量补名字
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

    # ── 3. 名称 + V2 库存：都只查这棵树用到的品号（all_prds）─────────────
    all_prds = list(set(n["prd_no"] for n in tree_nodes))
    if all_prds:
        ph = ','.join(['%s'] * len(all_prds))
        # A 步：名字只查这棵树用到的品号（原来查全库 7645 个 = 2.3s）
        # 逐行 fetchone 避免 fetchall 触发 UTF-8 解码错误
        cur.execute(f"SELECT PRD_NO, NAME FROM PRDT WITH(NOLOCK) WHERE PRD_NO IN ({ph})", tuple(all_prds))
        name_map = {}
        while True:
            try:
                row = cur.fetchone()
                if row is None:
                    break
                name_map[g(row[0])] = g(row[1])   # key 也过 g()，否则查不到 → 品名空白
            except UnicodeDecodeError:
                # 跳过无法解码的行（该品号名字留空）
                continue
        for n in tree_nodes:
            n["name"] = name_map.get(n["prd_no"], '')
        # V2.PRD_NO 同样是 varchar（GBK 字节），46 个品号带中文 → key 必须过 g()
        cur.execute(f"""
            SELECT PRD_NO, SUM(QTY_WH) AS stock
            FROM VW_STOCK_DETAIL2 WITH(NOLOCK)
            WHERE PRD_NO IN ({ph})
            GROUP BY PRD_NO
        """, tuple(all_prds))
        stock_map = {g(r[0]): float(r[1] or 0) for r in cur.fetchall()}   # 不过 g() → 中文品号库存恒 0
        # 各仓库存 {prd_no: {wh: qty}}
        cur.execute(f"""
            SELECT PRD_NO, WH, SUM(QTY_WH) AS qty
            FROM VW_STOCK_DETAIL2 WITH(NOLOCK)
            WHERE PRD_NO IN ({ph}) AND QTY_WH > 0
            GROUP BY PRD_NO, WH
        """, tuple(all_prds))
        wh_stock_map = {}
        for r in cur.fetchall():
            p, w, q = g(r[0]), r[1], float(r[2] or 0)   # w=仓码，纯 ASCII，不动
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
        guid, prd, lev, idx, qty, knd, upguid, parent_prd = e
        parent_children.setdefault(parent_prd, []).append(prd)

    # 把子件挂到父节点的 children 里（同名节点在各父下独立显示）
    for e in edges:
        guid, child_prd, lev, idx, qty, knd, upguid, parent_prd = e
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

    # 本地版对 str 只做 str(v)（= 不还原），PRDT.PRD_NO 是 varchar → 统一用全局 g()
    gbk = g

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

    child_prds = [g(c[0]) for c in children]   # varchar → latin-1 乱码，必须还原

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
            p = g(prd)
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
            name_map[g(prd)] = gbk(name)

    conn.close()

    items = []
    for prd_no, qty_per_unit, knd in children:
        p = g(prd_no)
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
            -- ⚠ 必须按品号分组：一张 KND=30 单含多个品号时，不按品号会把整单扣料算到每个品号头上
            SELECT FLD1, PRD_NO, SUM(QTY) AS done_qty
            FROM IC WITH(NOLOCK)
            WHERE IC_KND = 23 AND ISNULL(FLD1, '') <> ''
            GROUP BY FLD1, PRD_NO
        ) x ON x.FLD1 = t.IC_NO AND x.PRD_NO = t.PRD_NO
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


def _comp_stock_err(cur, rows, tool_rows=None):
    """完工扣料的库存校验（必须在写库同一事务内调用，INSERT 之前）。
    rows = [(prd_no, need, transfer_ic_no)]
    规则：① 调拨单必须存在（KND=30/USABLE=1/未删除）；② 该单**该品号**的剩余 ≥ 本次扣料量。
         （剩余 = 该单调拨量 − 挂该单该品号已扣的 KND=23）
    工具行（运输工具）只验品号存在，不校验库存（是搬运用具，不在 BOM 里）。
    返回 '' 或错误文案（最多列 3 条）。
    """
    shorts = []
    for prd_no, need, tic in rows:
        try:
            need = float(need or 0)
        except (TypeError, ValueError):
            need = 0.0
        if not prd_no or need <= 0:
            continue
        if not tic:
            shorts.append('%s 需 %g，未指定调拨单' % (prd_no, need))
            continue
        cur.execute("""SELECT ISNULL(SUM(QTY),0) FROM IC WITH(NOLOCK)
                       WHERE IC_KND=30 AND PRD_NO=%s AND RTRIM(IC_NO)=%s AND USABLE=1 AND ISNULL(删除,0)=0""",
                    (prd_no, tic))
        total = float(cur.fetchone()[0] or 0)
        if total <= 0:
            shorts.append('%s 需 %g，调拨单 %s 不存在或已作废' % (prd_no, need, tic))
            continue
        cur.execute("""SELECT ISNULL(SUM(QTY),0) FROM IC WITH(NOLOCK)
                       WHERE IC_KND=23 AND PRD_NO=%s AND FLD1=%s""", (prd_no, tic))
        left = total - float(cur.fetchone()[0] or 0)
        if left + 0.001 < need:
            shorts.append('%s 需 %g，%s 剩余 %g' % (prd_no, need, tic, left))
    for t in (tool_rows or []):
        code = (t.get('code') or '').strip()
        if not code:
            continue
        cur.execute('SELECT COUNT(*) FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s', (code,))
        if not cur.fetchone()[0]:
            shorts.append('工具品号 %s 不存在' % code)
    if not shorts:
        return ''
    return '材料库存不足：' + '；'.join(shorts[:3]) + ('…' if len(shorts) > 3 else '')


@app.post("/api/completion/confirm")
async def completion_confirm(
    payload: dict = Body(...),
    db: str = Query(default="c041"),
    dry: str = Query(default=""),
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

    # 库存校验：前端能被绕过，这里是最后防线（不通过 → 一行都不写）
    _rows = [(c.get("prd_no", ""), c.get("qty", 0), c.get("transfer_ic_no", "")) for c in components]
    _err = _comp_stock_err(cur, _rows)
    if _err:
        conn.rollback(); conn.close()
        return {"error": _err}
    if str(dry).lower() in ("1", "true", "yes"):
        conn.rollback(); conn.close()
        return {"dry": True, "ok": True, "checked": len(_rows)}

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
    dry: str = Query(default=""),
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
    # 数量防御：完工数量为空/0 时原来静默按 1 完工（前端 B1/B2/B3 已拦，这里做后端兜底）
    for _it in items:
        try:
            _vq = int(_it.get("qty") or 0)
        except (TypeError, ValueError):
            _vq = 0
        if _vq <= 0:
            return {"error": "成品 %s 的完工数量不能为空" % _it.get("fg_no", "")}

    conn = get_conn(db=db_name)
    if not items:
        return {"error": "缺少完工成品"}
    cur = conn.cursor()

    # 库存校验：前端能被绕过，这里是最后防线（不通过 → 一行都不写）
    _rows = []
    for _item in items:
        _fq = int(_item.get("qty") or 0)
        for _m in (_item.get("materials") or []):
            try:
                _need = float(_m.get("ratio") or 0) * _fq
            except (TypeError, ValueError):
                _need = 0.0
            if _need > 0:
                _rows.append((_m.get("prd_no", ""), _need, _m.get("transfer_ic_no", "")))
    _err = _comp_stock_err(cur, _rows, tool_rows)
    if _err:
        conn.rollback(); conn.close()
        return {"error": _err}
    if str(dry).lower() in ("1", "true", "yes"):
        conn.rollback(); conn.close()
        return {"dry": True, "ok": True, "checked": len(_rows), "items": len(items)}

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
        fg_qty = int(item.get("qty") or 0)  # 数量必填（已全量校验），不再静默按 1
        dzhw = float(item.get("danzhong") or 0)
        jzhw = float(item.get("jingzhong") or 0)
        cur.execute("SELECT NAME, ISNULL(UT,'') FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s", (fg_no,))
        pr = cur.fetchone()
        fg_name_gbk = pr[0] if pr else b''
        fg_ut = pr[1] if pr else ''

        # ①a FG 成品行（WH1=生产仓(增加)，WH2=''）
        FG_IN_COLS = ['IC_NO','IC_DD','IC_KND','PRD_NO','PRD_NAME','QTY','UT','WH1','WH1NAME','WH2',
                       'USR','USABLE','ITM','REM','FLD1','指令单号','客户','DDJH','单重','净重']
        FG_IN_VALS = [ic_in, now_str, 13, fg_no, fg_name_gbk, fg_qty, fg_ut, fg_wh, fg_wh_name, '',
                       'phone', 1, prod_itm + 1,
                       f"完工入库{'; 外发:'+all_ddjh if all_ddjh else ''}",
                       ic_out,
                       item.get("ref") or all_ddjh,   # 指令单号
                       item.get("customer") or all_customer,  # 客户
                       all_ddjh,                          # DDJH ← 外发计划
                       item.get("dzhw") or 0,           # 单重
                       _jz_fallback(item.get("dzhw"), item.get("jzhw"), fg_qty)]   # 净重兜底（<=0 且单重>0 且数量>0 → 补算）
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
        fg_qty = int(item.get("qty") or 0)  # 数量必填（已全量校验），不再静默按 1
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

            # ②a 子件行（WH2=原料仓(源仓)，WH1留空）
            MAT_OUT_COLS = ['IC_NO','IC_DD','IC_KND','PRD_NO','PRD_NAME','QTY','UT','WH2','WH2NAME',
                            'USR','USABLE','ITM','REM','FLD1','指令单号','DDJH','客户','单重','净重']
            MAT_OUT_VALS = [ic_out, now_str, 23, prd_no, prd_name_gbk, comp_qty, '',
                             twh1, twh1_name,
                             'phone', 1, out_itm + 1, f"完工出库({fg_no}×{fg_qty})",
                             tic,
                             item.get("ref") or '',     # 指令单号 ← item.ref
                             item_ddjh,                  # DDJH ← 外发计划
                             item_cus,                   # 客户
                             item.get("dzhw") or 0,     # 单重 ← item.dzhw
                             _jz_fallback(item.get("dzhw"), item.get("jzhw"), fg_qty)]   # 净重兜底（材料行沿用成品行单重/净重，故同用 fg_qty）
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
    # 找 LEV=0 header（PRD_NO 是 GBK varchar，含中文的料号必须传 GBK 字节才命中）
    prd_param = fg_no.encode("gbk", errors="replace") if isinstance(fg_no, str) else fg_no
    cur.execute("SELECT GUID FROM BOM WITH(NOLOCK) WHERE PRD_NO=%s AND LEV=0", (prd_param,))
    hdr = cur.fetchone()
    if not hdr:
        return []
    hdr_guid = hdr[0]

    # 取 LEV=1 直接子件（PRD_NO 走 varbinary，避免 GBK 字节在 fetchall 里炸 utf-8 解码）
    cur.execute("""
        SELECT CONVERT(varbinary(60), b.PRD_NO), CONVERT(varbinary(600), p.NAME), b.QTY, b.KND, b.QTY_BAS
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
            # KND=2 组件 / KND=3 中间件：先加入自身，再递归展开子件
            result.append((child_prd, child_name, demand_qty, child_knd, fg_no, depth+1, bom_ratio))
            if str(child_knd) in ('2', '3'):
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


# PMC 待分析销售订单的筛选口径（MAK 2026-09-29 定）：
#   已审核（CHK_MAN 有值）+ 销售未出>0（QTY−SAQTY）+ 指令单号>7721 + MP=0
# ⚠ 销售未出必须用 SAQTY（= VW_SO_QTY / VW_STOCK_DETAIL2.QTY_ON_ODR 的口径）；
#   VW_POS.QTYPS 在整库 850 行 SO 里全是 0/NULL，是废列 —— 用它会得到「整单量」，
#   已部分出货的单需求虚高、已出完的单还会被列进待分析（实测 QTYPS≠SAQTY 有 129 行）
# ⚠ 已系统审核 = APP_ID=1（MAK 2026-09-30 定，恢复原设计）。
#   两个状态别混：CHK_MAN = 人工审核人（851 行 SO 里 156 行有）；APP_ID=1 + APP_MAN + APP_DD
#   = 系统审核（551 行有，其中 181 行不带人）——POS 上没有任何触发器写这两列，都是桌面端按钮写的。
#   只有 APP_ID=1 的单能排产：MPS_insert 触发器 WHERE ISNULL(POS.APP_ID,0)=0 → rollback transaction。
#   （1f434b3 曾误改成 CHK_MAN<>'' → 列表里混进排不了的单，正是「能选不能生成」的第二层原因）
# ⚠ 老单（指令单号 ≤7721）不排产；空/非数字的指令单号（含 TEST01）一律不要
_PMC_POS_FILTER = """
    MP=0 AND USABLE=1 AND OS_NO LIKE 'SO%'
      AND ISNULL(APP_ID,0) = 1
      AND ISNULL(QTY,0) - ISNULL(SAQTY,0) > 0
      AND ISNUMERIC(指令单号) = 1 AND CAST(指令单号 AS INT) > 7721
"""


@app.get("/api/pmc/pos_unanalyzed")
async def pmc_pos_unanalyzed(
    q: str = Query(default=""),
    db: str = Query(default="c041")
):
    """
    返回 MP=0 未分析、且【已审核 + 销售未出>0 + 指令单号>7721】的 POS 行（按 VW_POS 视图）。
    q: 品号/品名模糊搜索。
    """
    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    # 带参数的查询要经 pymssql 的 %-插值，字面量 % 必须写成 %%（两处口径同源，别各写一份）
    flt = _PMC_POS_FILTER.replace('%', '%%') if q else _PMC_POS_FILTER

    if q:
        cur.execute(f"""
            SELECT TOP 200 OS_NO,ITM,SO_NO_ITM,PRD_NO,PRD_NAME,SPC,QTY,CUS_NO,CUS_NAME,指令单号,EST_DD,
                   ISNULL(QTY,0)-ISNULL(SAQTY,0) AS so_remain
            FROM VW_POS WITH(NOLOCK)
            WHERE {flt}
              AND (OS_NO LIKE %s OR 指令单号 LIKE %s OR PRD_NO LIKE %s OR PRD_NAME LIKE %s)
            ORDER BY 指令单号, OS_NO, ITM
        """, (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        cur.execute(f"""
            SELECT TOP 200 OS_NO,ITM,SO_NO_ITM,PRD_NO,PRD_NAME,SPC,QTY,CUS_NO,CUS_NAME,指令单号,EST_DD,
                   ISNULL(QTY,0)-ISNULL(SAQTY,0) AS so_remain
            FROM VW_POS WITH(NOLOCK)
            WHERE {flt}
            ORDER BY 指令单号, OS_NO, ITM
        """)

    rows = cur.fetchall()
    conn.close()
    return {"items": [_pos_row(r) for r in rows]}


def _pos_row(r):
    """VW_POS 待分析行的字段映射（列表 / 汇总共用，别各写一份）"""
    return {
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
    }


# ---------------------------------------------------------------------------
# 品号级汇总（跨单）：需求合计 − 挂本单供给 − 库存（库存只扣一次）= 净缺口
#
# 为什么要有它：单层「不分摊」后，单层缺口不扣公共库存，多张单用同一个料时
# 还会各扣一遍别人的料 —— 单层的数只能看「本单要多少」，**不能拿来下单**。
# 采购是按品号下的，所以净缺口在这里算：同一品号的需求合并、库存只扣一次。
# 一遍要跑所有待分析单的 BOM（几十秒）→ 后台线程算 + 内存缓存（TTL 10 分钟）。
# ---------------------------------------------------------------------------
_PRD_SUM = {"ts": 0.0, "data": None, "busy": False, "err": "", "orders": 0, "kicked": 0.0}
_PRD_SUM_TTL = 600


def _calc_prd_summary(db_name="C041"):
    """跑一遍全部待分析单的 BOM，按品号汇总。只在后台线程里调。"""
    import asyncio
    import collections
    import time as _t
    _PRD_SUM["busy"] = True
    _PRD_SUM["err"] = ""
    try:
        conn = get_conn(db=db_name)
        cur = conn.cursor()
        cur.execute(f"""
            SELECT OS_NO,ITM,SO_NO_ITM,PRD_NO,PRD_NAME,SPC,QTY,CUS_NO,CUS_NAME,指令单号,EST_DD,
                   ISNULL(QTY,0)-ISNULL(SAQTY,0) AS so_remain
            FROM VW_POS WITH(NOLOCK)
            WHERE {_PMC_POS_FILTER}
            ORDER BY 指令单号, OS_NO, ITM
        """)
        items = [_pos_row(r) for r in cur.fetchall()]
        conn.close()

        need = collections.defaultdict(float)          # 毛需求合计（跨单）
        own = collections.defaultdict(float)           # 挂本单的在途+在单请购
        pool = collections.defaultdict(float)          # 该品号全厂池（在途采购+在单请购，含别的单下的）
        names = {}
        seen = collections.defaultdict(set)            # 被哪几张单用
        for it in items:
            api = asyncio.run(pmc_preview_mps(so_no_itm=it["so_no_itm"], prd_no=it["prd_no"],
                                             qty=it["qty"], db=db_name.lower()))
            for r in api["items"]:
                p = r["prd_no"]
                # need = 毛需求合计（销售未出不扣任何料展开，与路径无关）→ 跨单加总才有意义。
                # 别用 real_demand（净需求，逐层扣过池/材料仓：跨单会重复扣，加总偏小）。
                need[p] += r["gross_demand"]
                own[p] += r["total_avail"]
                pool[p] = max(pool[p], float(r.get("pool_way") or 0) + float(r.get("pool_odr") or 0))
                # 成品行没有 prd_name 字段（只有子件有），用列表里的品名兜底
                nm = r.get("prd_name") or (it["prd_name"] if p == it["prd_no"] else "")
                if nm:
                    names[p] = nm
                seen[p].add(it["os_no"])

        conn = get_conn(db=db_name)
        stock = _v2_stock(list(need), conn)
        conn.close()

        data = {}
        for p, n in need.items():
            e = stock.get(p) or {}
            mat = float(e.get("mat_qty", 0))            # 材料仓
            prod = float(e.get("prod_qty", 0))          # 生产仓（车间仓/外发仓）
            row_gap = n - own[p]                        # 本单口径缺口加总（屏上对照用）
            # 下单口径：需求合计 − 全厂池（在途采购+在单请购）− **材料仓**。
            # 生产仓不扣（MAK 2026-09-29）：车间仓/外发仓的料已经被领去做别的单了，
            # 不能再拿来抵新单的需求。负的生产仓库存也不该吃材料仓的量。
            net = max(0.0, n - pool[p] - mat)
            data[p] = {
                "prd_no": p, "prd_name": names.get(p, ""),
                "need": round(n, 2), "own": round(own[p], 2), "pool": round(pool[p], 2),
                "mat": round(mat, 2), "prod": round(prod, 2),
                "stock": round(mat + prod, 2), "row_gap": round(row_gap, 2), "net": round(net, 2),
                "orders": len(seen[p]),
            }
        _PRD_SUM.update(ts=_t.time(), data=data, orders=len(items))
        _PRD_SUM["kicked"] = 0.0            # 算完了，允许下一次到点重算
    except Exception as e:                                # 后台线程：别把异常吞了就完了
        _PRD_SUM["err"] = f"{type(e).__name__}: {e}"
    finally:
        _PRD_SUM["busy"] = False


def _kick_prd_summary(db_name="C041"):
    """单飞：算的过程中和刚踢过的 30 秒内都不许再踢（否则每次轮询都开一个新线程一起算）"""
    import time as _t
    if _PRD_SUM["busy"] or (_t.time() - _PRD_SUM["kicked"]) < 30:
        return
    _PRD_SUM["kicked"] = _t.time()
    import threading
    threading.Thread(target=_calc_prd_summary, args=(db_name,), daemon=True).start()


@app.get("/api/pmc/prd_summary")
async def pmc_prd_summary(
    refresh: int = Query(default=0),
    db: str = Query(default="c041")
):
    """品号级汇总（净缺口）。后台算、缓存 10 分钟；初次调用返回 computing=true。"""
    import time as _t
    db_name = "T041" if db.lower() == "t041" else "C041"
    now = _t.time()
    stale = (now - _PRD_SUM["ts"]) > _PRD_SUM_TTL
    if refresh or (stale and _PRD_SUM["data"] is None):
        _kick_prd_summary(db_name)
    data = _PRD_SUM["data"] or {}
    return {
        "computing": _PRD_SUM["busy"],
        "ts": _PRD_SUM["ts"],
        "err": _PRD_SUM["err"],
        "orders": _PRD_SUM["orders"],
        "items": sorted(data.values(), key=lambda x: -x["net"]),
    }


def _v2_odr_split(prd_nos, db_conn, ref=None, fg=None, so_itm=None):
    """按品号取「本单专属」和「池子总量」两套在途/在单请购。

    ⚠ 库存视图 VW_STOCK_DETAIL2 里没有「请购在单」这一列，别搞错：
      - QTY_ON_WAY = 采购未回量（VW_PO_QTY）＝ 采购单未回 ∪ 请购单(QTS,QT_ID='QD')数量
      - QTY_ON_ODR = **销售未出货量**（VW_SO_QTY，SUM(POS.QTY-SAQTY)），不是请购。
        拿它当供给扣，等于把「需求」又减一遍，整棵 BOM 树会被扣成全 0。

    池子行（采购单 VW_POS / 请购单 QTS）上带 指令单号、成品编号、SO行 —— 也就是
    「这笔料是给哪张单下的」，数据里写着。**按这些字段挂单**：挂本单的才算本单供给；
    挂别的单、或挂不上的（「X/库存」里的库存份额、纯库存）**不分摊**，本单不扣。

    返回 {prd_no: {'po','qts','po_all','qts_all'}}
      po / qts        = 挂到本单的 采购未回 / 请购在单
      po_all / qts_all = 该品号池子总量（给屏上「池 xxx」小注用）
    """
    if not prd_nos:
        return {}
    cur = db_conn.cursor()
    ph = ','.join(['%s'] * len(prd_nos))
    args = tuple(prd_nos)
    like = f'%{ref}%' if ref else None
    out = {str(p).strip(): {'po': 0.0, 'qts': 0.0, 'po_all': 0.0, 'qts_all': 0.0, 'po_free': 0.0} for p in prd_nos}

    cur.execute(f"""
        SELECT PRD_NO, SUM(QTY - ISNULL(PSQTY,0)) FROM VW_POS WITH(NOLOCK)
        WHERE PRD_NO IN ({ph}) AND USABLE=1 AND CLS_ID=0 AND OS_ID='PO' AND QTY > ISNULL(PSQTY,0)
        GROUP BY PRD_NO
    """, args)
    for r in cur.fetchall():
        k = str(r[0]).strip()
        if k in out:
            out[k]['po_all'] = round(float(r[1] or 0), 2)
    cur.execute(f"""
        SELECT PRD_NO, SUM(QTY) FROM QTS WITH(NOLOCK)
        WHERE PRD_NO IN ({ph}) AND QT_ID='QD' AND USABLE=1 AND CLS_ID=0
        GROUP BY PRD_NO
    """, args)
    for r in cur.fetchall():
        k = str(r[0]).strip()
        if k in out:
            out[k]['qts_all'] = round(float(r[1] or 0), 2)

    if like:
        cur.execute(f"""
            SELECT PRD_NO, SUM(QTY - ISNULL(PSQTY,0)) FROM VW_POS WITH(NOLOCK)
            WHERE PRD_NO IN ({ph}) AND USABLE=1 AND CLS_ID=0 AND OS_ID='PO' AND QTY > ISNULL(PSQTY,0)
                  AND [指令单号] LIKE %s
            GROUP BY PRD_NO
        """, args + (like,))
        for r in cur.fetchall():
            k = str(r[0]).strip()
            if k in out:
                out[k]['po'] = round(float(r[1] or 0), 2)

    # 请购单挂单：指令单号命中 → 挂本单；指令单号空白时才用「成品编号」兜底
    # （成品编号只说明「为哪个成品请的」，同产品多张单时区分不出来，所以不是首选）；
    # SO行命中 → 挂本单。
    cond, cargs = [], []
    if like:
        cond.append('[指令单号] LIKE %s'); cargs.append(like)
    if fg:
        cond.append("(ISNULL(NULLIF(LTRIM(RTRIM([指令单号])),''),'')='' AND [成品编号] = %s)")
        cargs.append(fg)
    if so_itm:
        cond.append('SO_NO_ITM = %s'); cargs.append(so_itm)
    if cond:
        cur.execute(f"""
            SELECT PRD_NO, SUM(QTY) FROM QTS WITH(NOLOCK)
            WHERE PRD_NO IN ({ph}) AND QT_ID='QD' AND USABLE=1 AND CLS_ID=0
                  AND ({' OR '.join(cond)})
            GROUP BY PRD_NO
        """, args + tuple(cargs))
        for r in cur.fetchall():
            k = str(r[0]).strip()
            if k in out:
                out[k]['qts'] = round(float(r[1] or 0), 2)

    # 别人单采购「多下」的部分 = 通用料，本单可以用（MAK 2026-09-30 定）。
    # 追溯链：PO 行 REF_ITM = 源请购单号 + 3 位行号（例 PO26080054/13 → QD26080036/006）
    #   → 多下 = 采购未回量 − 源请购量，只取正数、只算还挂在途的部分（已到货的进了材料仓，不重复算）
    # 挂本单的行不算（那本来就是本单的）；别人单按请购量下的那部分也不算（是别人单的需求）
    # ⚠ VW_POS 上没有「成品编号」列（那是 QTS 的列），采购侧只能按指令单号/SO行挂
    own_cond = []
    if like:
        own_cond.append('p.[指令单号] LIKE %s')
        _po_own_args = [like]
    else:
        _po_own_args = []
    if so_itm:
        own_cond.append('p.SO_NO_ITM = %s')
        _po_own_args.append(so_itm)
    not_own = ('NOT (' + ' OR '.join(own_cond) + ')') if own_cond else '1=1'
    cur.execute(f"""
        SELECT RTRIM(p.PRD_NO),
               SUM(CASE WHEN p.QTY - ISNULL(p.PSQTY,0) - ISNULL(q.QTY,0) > 0
                        THEN p.QTY - ISNULL(p.PSQTY,0) - ISNULL(q.QTY,0) ELSE 0 END)
        FROM VW_POS p WITH(NOLOCK)
        LEFT JOIN QTS q WITH(NOLOCK)
          ON RTRIM(q.QT_NO) + RIGHT('00' + CONVERT(varchar(6), q.ITM), 3) = RTRIM(p.REF_ITM)
        WHERE p.PRD_NO IN ({ph}) AND p.USABLE=1 AND p.CLS_ID=0 AND p.OS_ID='PO'
              AND p.QTY > ISNULL(p.PSQTY,0) AND {not_own}
        GROUP BY RTRIM(p.PRD_NO)
    """, args + tuple(_po_own_args))
    for r in cur.fetchall():
        k = str(r[0]).strip()
        if k in out:
            out[k]['po_free'] = round(float(r[1] or 0), 2)
    return out


def _so_line_info(so_no_itm, db_conn):
    """该销售订单行的「销售未出」和「指令单号」。

    销售未出 = QTY − SAQTY（= VW_SO_QTY / v2 视图 QTY_ON_ODR 的口径；VW_POS.QTYPS 是废列，恒 0）。
    指令单号用来把在途/在单请购挂到单上。查不到返回 (None, '')。
    """
    cur = db_conn.cursor()
    cur.execute("""
        SELECT ISNULL(QTY,0)-ISNULL(SAQTY,0), ISNULL([指令单号],'') FROM VW_POS WITH(NOLOCK)
        WHERE SO_NO_ITM=%s
    """, (so_no_itm,))
    r = cur.fetchone()
    if not r:
        return None, ''
    return float(r[0] or 0), str(r[1] or '').strip()


def _so_line_remain(so_no_itm, db_conn):
    """只要销售未出（兼容旧调用）"""
    return _so_line_info(so_no_itm, db_conn)[0]


def _plant_unshipped(prd_no, db_conn):
    """全厂该品号的「销售未出」合计（所有未出货的销售订单行，含本单、含系统未审单）。

    口径同 ERP 的 VW_SO_QTY（USABLE=1 / CLS_ID=0 / OS_ID='SO' / WJ<>1 / QTY>SAQTY），
    但**不套 VW_SO_QTY 的 APP_ID=1 限制**：PMC 待分析单本身常是 APP_ID=0（人工已审、系统未审），
    套上去会把正在看的这张单漏掉，毛需求反而比本单需求还小。
    """
    cur = db_conn.cursor()
    cur.execute("""
        SELECT ISNULL(SUM(QTY - ISNULL(SAQTY,0)), 0) FROM VW_POS WITH(NOLOCK)
        WHERE PRD_NO=%s AND USABLE=1 AND ISNULL(CLS_ID,0)=0 AND OS_ID='SO'
          AND ISNULL(WJ,0)<>1 AND QTY > ISNULL(SAQTY,0)
    """, (prd_no.encode("gbk"),))
    r = cur.fetchone()
    return round(float(r[0] or 0), 4)


class _StockAlloc:
    """跨单共享的材料仓 / 「别人多下」分配器（P0 修复，MAK 2026-09-30）。

    以前每张单各自把同一批材料仓扣一遍 —— 3 个成品一起预览时逐行缺口相加 = −78,665，
    去重后却是 53,432（差 13 万），屏上的数加不起来。现在按「选中的单顺序 → 单内树行
    顺序」累计抵冲、扣完为止，跟 generate_mps 落库用的 stock_left 同一套规则。
    """

    def __init__(self):
        self.mat = {}   # 品号 -> 还没被抵掉的材料仓
        self.sur = {}   # 品号 -> 还没被抵掉的「别人单采购多下」在途

    def take(self, prd, need, mat_qty, po_free):
        """按顺序取：材料仓 → 别人多下，扣完为止。返回 (用了材料仓, 用了多下)。"""
        if not need or need <= 0:
            return 0.0, 0.0
        # 负库存夹到 0：库里现存为负的品号（89 行）不该把缺口撑大
        m = max(0.0, self.mat.setdefault(prd, float(mat_qty or 0)))
        s = max(0.0, self.sur.setdefault(prd, float(po_free or 0)))
        um = min(m, need)
        m -= um
        us = min(s, need - um)
        s -= us
        self.mat[prd], self.sur[prd] = m, s
        return um, us


def _pmc_preview_one(conn, so_no_itm, prd_no, qty, alloc=None):
    """单个成品的 BOM 展开预览（不写库）。传 alloc 则与别的单共享同一批库存。"""
    if alloc is None:
        alloc = _StockAlloc()
    # 需求基准 = 销售未出（QTY-SAQTY）；指令单号用来把在途/在单请购挂到本单
    demand_qty, ref = _so_line_info(so_no_itm, conn)
    if demand_qty is None:
        demand_qty = qty
        ref = ref or ''
    # 全厂销售未出（含本单）：屏上「毛需求」列的根，见文件末尾 allocate 处的说明
    plant_unshipped = _plant_unshipped(prd_no, conn)
    comp_rows = _mps_bom_tree(prd_no, conn, qty=demand_qty)
    all_prds = [prd_no] + [r[0] for r in comp_rows]
    stock_map = _v2_stock(all_prds, conn)
    stock_detail = _v2_stock_detail(all_prds, conn)
    odr_split = _v2_odr_split(all_prds, conn, ref=ref, fg=prd_no, so_itm=so_no_itm)

    # 库存汇总
    fg_stock = stock_map.get(prd_no, {})

    def make_row(c_prd, c_name, c_qty, c_depth, c_knd, stock_map_entry, stock_det):
        """需求 = 父件缺口 × BOM配比（毛需求，不看公共库存）。

        不分摊（MAK 2026-09-29 定）：公共库存不进「合计」，单层缺口只扣
        「挂到本单」的在途采购 / 在单请购 —— 按指令单号/成品编号/SO行挂。
        净缺口看品号汇总（库存只在品号层扣一次）。"""
        sm = stock_map_entry or {}
        mat_qty   = round(sm.get('mat_qty', 0), 2)
        prod_qty  = round(sm.get('prod_qty', 0), 2)
        det = stock_det or []
        sp = odr_split.get(c_prd) or {}
        qty_on_way = round(float(sp.get('po', 0)), 2)     # 挂本单的采购未回
        qty_on_odr = round(float(sp.get('qts', 0)), 2)    # 挂本单的请购在单
        pool_way = round(float(sp.get('po_all', 0)), 2)   # 该品号池子总量（显示用）
        pool_odr = round(float(sp.get('qts_all', 0)), 2)
        po_free = round(float(sp.get('po_free', 0)), 2)   # 别人单采购多下的、本单可用的在途
        way_free = round(qty_on_way + po_free, 2)         # 本单可用在途 = 挂本单未回 + 别人多下的
        way_others = round(max(0.0, pool_way - way_free), 2)  # 全厂在途里属于别人单的部分
        total_stock = mat_qty + prod_qty
        real_demand = c_qty                                # 毛需求
        total_avail = round(way_free + qty_on_odr, 2)      # 本单可用供给（不含公共库存）
        # 缺口 = 需求 − 本单可用(在途/请购) − 材料仓（与 allocate 的父件缺口同一算式）
        gap = round(real_demand - total_avail - mat_qty, 2)
        # wh_detail: 全部明细（兼容前端调整弹窗）
        # mat_detail / prod_detail: 原材料仓/生产仓分组（显示用）
        mat_detail = [(x[0], x[1], x[2], x[3], x[4], x[5]) for x in det if not x[6]]
        prod_detail = [(x[0], x[1], x[2], x[3], x[4], x[5]) for x in det if x[6]]
        return {
            "prd_no": c_prd, "prd_name": c_name,
            "qty": round(c_qty, 4),
            "qty_on_odr": qty_on_odr,
            "real_demand": round(real_demand, 4),
            "gross_demand": round(c_qty, 4),    # 本单毛需求：由 allocate 填（父件毛需求×配比）；成品行 = 本单销售未出
            "gross_plant": round(c_qty, 4),     # 全厂毛需求：全厂销售未出展开（屏上「毛需求」列用它）
            "mat_qty": mat_qty, "prod_qty": prod_qty,
            "qty_on_way": qty_on_way,
            "way_free": way_free, "way_others": way_others, "po_free": po_free,
            "total_stock": round(total_stock, 2),
            "total_avail": total_avail,
            "pool_way": pool_way, "pool_odr": pool_odr,
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
    fg_sp = odr_split.get(prd_no) or {}
    fg_way = round(float(fg_sp.get('po', 0)), 2)      # 挂本单的采购未回
    fg_on_odr = round(float(fg_sp.get('qts', 0)), 2)  # 挂本单的请购在单
    fg_pool_way = round(float(fg_sp.get('po_all', 0)), 2)
    fg_pool_odr = round(float(fg_sp.get('qts_all', 0)), 2)
    fg_po_free = round(float(fg_sp.get('po_free', 0)), 2)
    fg_way_free = round(fg_way + fg_po_free, 2)               # 本单可用在途
    fg_way_others = round(max(0.0, fg_pool_way - fg_way_free), 2)
    fg_raw = round(fg_stock.get('mat_qty', 0) + fg_stock.get('prod_qty', 0), 2)
    fg_real_demand = demand_qty                       # 毛需求（库存不分摊）
    fg_total_stock = fg_raw
    fg_total_avail = round(fg_way_free + fg_on_odr, 2)   # 本单可用供给

    os_no = so_no_itm[:-3] if len(so_no_itm) > 3 else so_no_itm
    rows = [{
        "prd_no": prd_no, "qty": round(demand_qty, 4),
        "qty_on_odr": fg_on_odr,
        "so_remain": round(demand_qty, 4),   # 销售未出 = QTY-SAQTY
        "real_demand": round(fg_real_demand, 4),
        "gross_demand": round(demand_qty, 4),        # 本单口径毛需求（品号汇总用）
        "gross_plant": round(plant_unshipped, 4),    # 全厂口径毛需求（屏上「毛需求」列用）
        "mat_qty": round(fg_stock.get('mat_qty', 0), 2),
        "prod_qty": round(fg_stock.get('prod_qty', 0), 2),
        "qty_on_way": fg_way,
        "way_free": fg_way_free, "way_others": fg_way_others, "po_free": fg_po_free,
        "total_stock": round(fg_total_stock, 2),
        "total_avail": fg_total_avail,
        "pool_way": fg_pool_way, "pool_odr": fg_pool_odr,
        "gap": round(fg_real_demand - fg_total_avail - float(fg_stock.get('mat_qty', 0)), 2),
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
    # 自顶向下 BOM 配比分需求（MRP 净需求展开）
    #
    # 公式：子件需求 = 父件缺口 × BOM配比 = parent_gap × bom_ratio
    # 父件缺口 = max(0, 父件需求 − 父件池(在途+在单请购) − 父件材料仓)
    #            ← 与屏上「缺口」列同一算式，所以「子件需求 = 父件缺口 × 配比」恒成立
    # 生产仓已被领走，不参与扣减（MAK 2026-09-29）
    # - KND=2 组件 / KND=3 半成品 / KND=4 原料 都用同一个口径
    # - 每层都扣该层自己的库存/池 → 中间件有货就不往下要料
    #   （例：振光件库存够 → 黑坯和钢带都不用做/买）
    # ponytail: 按「BOM 边（母件实例→子件实例）」分配，不按品号索引。
    # 同一料号会挂在多个母件下（50 单里有 54 个这样的料号），按品号索引
    # 只会命中最后一行，前一行留着「展开量-库存」的错值。
    # rows[i+1] 与 comp_rows[i] 一一对应（上面就是按序遍历生成的）；
    # comp_rows 是 DFS 前序，所以某行的母件 = 它前面最近的 depth-1 那一行。
    # -----------------------------------------------------------
    edges = {}    # 母件实例下标（-1 = 成品行）→ [comp_rows 下标]
    stack = {}
    for i, crow in enumerate(comp_rows):
        edges.setdefault(stack.get(crow[5] - 1, -1), []).append(i)
        stack[crow[5]] = i

    def net_avail(row, demand):
        """参与扣减的供给 = 挂本单在途 + 挂本单在单请购 + 本次实际取到的公共库存。

        ⚠ 公共库存（材料仓、别人多下）由 alloc 跨单共享、扣完为止 —— 同一批库存只抵
          一次（P0 修复：以前每张单各抵一遍，批量预览相加毫无意义）。
          先扣挂本单的池，不够才动公共库存。
        第二遍（全厂毛需求）不重复消耗：结果按行缓存。
        """
        if row.get('mat_used') is None:
            way_own = float(row.get('qty_on_way') or 0)
            odr = float(row.get('qty_on_odr') or 0)
            need_left = max(0.0, float(demand or 0) - way_own - odr)
            um, us = alloc.take(row['prd_no'], need_left, row.get('mat_qty'), row.get('po_free'))
            row['mat_used'], row['sur_used'] = round(um, 2), round(us, 2)
        return (float(row.get('qty_on_way') or 0) + float(row.get('qty_on_odr') or 0)
                + float(row.get('mat_used') or 0) + float(row.get('sur_used') or 0))

    def allocate(parent_row, parent_idx, parent_demand, parent_gross, gkey='gross_demand'):
        """自顶向下分配 BOM 需求；父件缺口与屏上「缺口」列同口径。
        parent_gross = 毛需求（从销售未出不扣任何库存/在途展开）。
        gkey = 写哪个毛需求字段：gross_demand 本单口径（品号汇总用）/ gross_plant 全厂口径（只上屏）。
        两遍分发 real_demand 结果相同（只依赖父件缺口），所以可按字段跑两遍。"""
        parent_gap = max(0.0, parent_demand - net_avail(parent_row, parent_demand))
        for i in edges.get(parent_idx, []):
            child_row = rows[i + 1]
            ratio = comp_rows[i][6]
            # 子件需求 = 父件缺口 × BOM配比；毛需求 = 父件毛需求 × BOM配比
            child_row['real_demand'] = round(parent_gap * ratio, 4)
            child_row[gkey] = round(parent_gross * ratio, 4)
            allocate(child_row, i, child_row['real_demand'], child_row[gkey], gkey)

    # FG 真实需求 = 销售未出；成品自身也先扣库存/池（货够就不用做，子件也不用要料）
    # 毛需求起点 = 销售未出（不扣任何料）
    rows[0]['real_demand'] = demand_qty
    rows[0]['gross_demand'] = demand_qty
    rows[0]['gross_plant'] = plant_unshipped
    allocate(rows[0], -1, demand_qty, demand_qty)
    # 屏上「毛需求」= 全厂该品号销售未出展开（前面单没出的 + 本单）→ MAK 2026-09-29 定
    # ⚠ gross_demand 保持本单口径不动：品号汇总 _calc_prd_summary 靠跨单加总它，
    #    这里若换成全厂数，汇总会把同一个全厂数按单数重复计一遍。
    if abs(plant_unshipped - demand_qty) > 1e-6:
        allocate(rows[0], -1, demand_qty, plant_unshipped, gkey='gross_plant')

    # 计算最终 gap（④ 屏上「缺口」= 需求 − 本单可用(在途+请购) − 材料仓）
    # ⚠ 这里是缺口唯一的落地点：make_row 里算过也没用，会被这个循环覆盖（踩过）。
    #   与 allocate 的父件缺口 max(0, 父件需求 − net_avail(父件)) 同一算式，所以
    #   「子件需求 = 父件缺口 × 配比」在屏上恒成立。
    # 生产仓不扣：车间仓/外发仓的料已经被领去做别的单了（MAK 2026-09-29）。
    # 同品号多行或多单时以品号汇总为准（那里跨单合并需求、池子和材料仓各只扣一次）。
    for r in rows:
        # 合计 = 实际能被扣到的（挂本单在途 + 挂本单请购 + 本次取到的别人多下）；
        # 缺口 = 需求 − 合计 − 本次取到的材料仓 → 逐行缺口相加 = 本次合计缺口
        r['total_avail'] = round(float(r.get('qty_on_way') or 0) + float(r.get('qty_on_odr') or 0)
                                 + float(r.get('sur_used') or 0), 2)
        r['gap'] = round(r['real_demand'] - r['total_avail'] - float(r.get('mat_used') or 0), 2)
        r['pool_total'] = round((r.get('pool_way') or 0) + (r.get('pool_odr') or 0), 2)
        r['net_gap'] = round(max(0.0, r['real_demand'] - r['pool_total'] - (r.get('mat_qty') or 0)), 2)
        # 品号级净缺口（跨单，库存只扣一次）—— 缓存热了才有；冷的时候前端不显示
        s = (_PRD_SUM['data'] or {}).get(r['prd_no'])
        if s:
            r['prd_net'] = s['net']
            r['prd_need'] = s['need']
            r['prd_pool'] = s.get('pool', 0)
            r['prd_mat'] = s.get('mat', 0)
            r['prd_stock'] = s['stock']
            r['prd_orders'] = s['orders']

    return {"items": rows, "so_no": os_no}


# ── 全局净需求（毛需求 − 供给）───────────────────────────────────────────────────
# 只读聚合：所有成品的销售未出 → BOM 展开加总 → 减 材料仓/生产仓/在途/请购。
# 口径（MAK 2026-09-30 定）：
#   · 毛需求 = 成品未出（与库存视图 QTY_ON_ODR 同源：USABLE=1 + CLS_ID=0 + APP_ID=1）
#             × BOM 配比（QTY/QTY_BAS），按品号加总
#   · 生产仓（MY_WH.ATTRIB 5/6）的料是「某母件的在途」：MOT 里唯一母件 → 自动归并抵该母件；
#     多母件/查不到 → 不参与计算、只报警（算法不猜数量）
#   · 生产仓归属后多出的 → 回全局池
#   · 负账面夹 0 并报警；JDPE03C01200003 材料仓 100 亿脏数据不夹、单独报警
#   · 无 BOM：TYPE=20000/30000（成品/半成品）→ 报警且不计入需求；其余（外购件/包材等）
#     未出直接当该品号自己的毛需求（本身就是要买的东西，不展开）
import threading
_GNET = {"ts": 0.0, "data": None, "err": None}
# 「全厂毛需求」来源包（反向图 + 不滚算毛需求 + 成品种子），只驻内存，
# 供 /api/pmc/flat_src 的 hover 明细用 —— 不进 global_net 的响应体。
_FLATSRC = {"ts": 0.0, "data": None}
_GNET_TTL = 600
_GNET_LOCK = threading.Lock()

# 写库后让全厂净需求缓存失效：任何 POST/PUT/PATCH/DELETE 成功 → 下次读重算。
# 不逐个接口加（30+ 个写接口，漏一个就又是「刚建的请购单不减需求」），一处兜住全部。
@app.middleware("http")
async def _gnet_invalidate_on_write(request, call_next):
    resp = await call_next(request)
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and resp.status_code < 400:
        _GNET["ts"] = 0.0
    return resp
_GNET_DIRTY_PRD = "JDPE03C01200003"
_GNET_MADE_TYPES = ("20000", "30000")   # 成品/半成品：没 BOM 就是缺 BOM


_GNET_PRDT = {"ts": 0.0, "data": None}


def _calc_global_net(db_name="C041", refresh=0):
    import time as _t
    t0 = _t.time()
    conn = get_conn(db_name)
    cur = conn.cursor()

    def q(sql, args=None):
        cur.execute(sql, args or ())
        return cur.fetchall()

    # ① 成品未出（VW_SO_QTY 同源）
    fg = {}
    for prd_b, qty in q("""
        SELECT CONVERT(varbinary(60), PRD_NO), SUM(QTY - ISNULL(SAQTY,0))
        FROM VW_POS WITH(NOLOCK)
        WHERE USABLE = 1 AND CLS_ID = 0 AND OS_ID = 'SO' AND ISNULL(WJ,0) <> 1
          AND QTY > ISNULL(SAQTY,0) AND APP_ID = 1
        GROUP BY CONVERT(varbinary(60), PRD_NO)
    """):
        fg[g(prd_b)] = float(qty or 0)

    # ② BOM 全表 → 内存索引（18,658 行，一次载入）
    # node: GUID → (品号, 层, 父GUID) —— 生产仓归属的 BOM 兜底要顺着 UPGUID 往上爬到 LEV=0
    hdr, kids, node = {}, {}, {}
    for prd_b, guid_b, up_b, lev, cq, cbas, cknd in q("""
        SELECT CONVERT(varbinary(60), PRD_NO), CONVERT(varchar(60), GUID), CONVERT(varchar(60), UPGUID),
               LEV, QTY, ISNULL(QTY_BAS,0), ISNULL(KND,0)
        FROM BOM WITH(NOLOCK) WHERE ISNULL(删除,0) = 0
    """):
        p = g(prd_b)
        node[g(guid_b)] = (p, int(lev or 0), g(up_b))
        if lev == 0:
            hdr[p] = g(guid_b)
        else:
            kids.setdefault(g(up_b), []).append((p, float(cq or 0), float(cbas or 0), str(cknd or '')))

    # ③ 品号档案（长缓存 1 小时：品名很少变，全表读是最大开销）
    global _GNET_PRDT
    import time as _t2
    if not _GNET_PRDT["data"] or (_t2.time() - _GNET_PRDT["ts"]) > 3600 or refresh:
        d = {}
        for prd_b, nm_b, spc_b, ut, typ, knd in q("""
            SELECT CONVERT(varbinary(60), PRD_NO), CONVERT(varbinary(400), NAME), CONVERT(varbinary(300), SPC),
                   CONVERT(varchar(20), ISNULL(UT,'')), ISNULL(TYPE,0), ISNULL(KND,0)
            FROM PRDT WITH(NOLOCK)
        """):
            d[g(prd_b)] = (g(nm_b), g(spc_b), g(ut), str(typ), str(knd))
        _GNET_PRDT["data"], _GNET_PRDT["ts"] = d, _t2.time()
    prdt = _GNET_PRDT["data"]

    # ④ 库存：按 品号 × 仓类（ATTRIB 5/6 = 生产仓；其余 = 原材料仓）
    mat_stock, prod_stock = {}, {}
    for prd_b, attrib, qty in q("""
        SELECT CONVERT(varbinary(60), s.PRD_NO), ISNULL(CONVERT(varchar(20), w.ATTRIB), ''), SUM(s.QTY_WH)
        FROM VW_STOCK_DETAIL2 s WITH(NOLOCK)
        LEFT JOIN MY_WH w WITH(NOLOCK) ON w.WH = s.WH
        WHERE ISNULL(s.QTY_WH,0) <> 0
        GROUP BY CONVERT(varbinary(60), s.PRD_NO), ISNULL(CONVERT(varchar(20), w.ATTRIB), '')
    """):
        p, v = g(prd_b), float(qty or 0)
        if attrib in ('5', '6'):
            prod_stock[p] = prod_stock.get(p, 0.0) + v
        else:
            mat_stock[p] = mat_stock.get(p, 0.0) + v

    # ⑤ 生产仓归属：MOT（工单）里这个料被哪些母件用到
    own = {}
    for prd_b, fg_b in q("""
        SELECT CONVERT(varbinary(60), PRD_NO), CONVERT(varbinary(60), FG_NO)
        FROM MOT WITH(NOLOCK)
        GROUP BY CONVERT(varbinary(60), PRD_NO), CONVERT(varbinary(60), FG_NO)
    """):
        own.setdefault(g(prd_b), set()).add(g(fg_b))

    # ⑤b BOM 兜底：MOT（工单）里查不到的料，去 BOM 查「被哪些顶层成品（LEV=0 祖先）用到」
    # · 唯一 → 自动归属（料物理上还在车间，母件不要时按规则回全局池）
    # · 多个 → 仍按多母件冲突全部报警（算法不猜）
    # · 一个都没有 → 继续报警（天生没 BOM 的外购件/包材）
    _root_memo, _kid_guids, _bom_own_memo = {}, None, {}

    def _root_of(gd):
        """顺着 UPGUID 爬到 LEV=0，返回顶层成品品号（爬不到返回 None）"""
        path, cur, r = [], gd, None
        while True:
            if cur in _root_memo:
                r = _root_memo[cur]
                break
            if cur not in node or len(path) > 30:
                r = None
                break
            p0, lev0, up0 = node[cur]
            if lev0 == 0:
                r = p0
                break
            path.append(cur)
            cur = up0
        for x in path:
            _root_memo[x] = r
        return r

    def bom_owners(p):
        """BOM 里这个料被哪些顶层成品用到（不含它自己）"""
        nonlocal _kid_guids
        if p in _bom_own_memo:
            return _bom_own_memo[p]
        if _kid_guids is None:
            _kid_guids = {}
            for _gd, (_p0, _lev0, _up0) in node.items():
                if _lev0 > 0:
                    _kid_guids.setdefault(_p0, []).append(_gd)
        out = set()
        for _gd in _kid_guids.get(p, ()):
            _r = _root_of(_gd)
            if _r and _r != p:
                out.add(_r)
        _bom_own_memo[p] = out
        return out

    # ⑥ 在途（采购未回）/ 请购（未转采购）
    onway, qts = {}, {}
    for prd_b, qty in q("""
        SELECT CONVERT(varbinary(60), PRD_NO), SUM(QTY - ISNULL(PSQTY,0))
        FROM VW_POS WITH(NOLOCK)
        WHERE USABLE = 1 AND CLS_ID = 0 AND OS_ID = 'PO' AND QTY > ISNULL(PSQTY,0)
        GROUP BY CONVERT(varbinary(60), PRD_NO)
    """):
        onway[g(prd_b)] = float(qty or 0)
    for prd_b, qty in q("""
        SELECT CONVERT(varbinary(60), PRD_NO), SUM(QTY)
        FROM QTS WITH(NOLOCK)
        WHERE QT_ID = 'QD' AND ISNULL(USABLE,0) = 1 AND ISNULL(CLS_ID,0) = 0
        GROUP BY CONVERT(varbinary(60), PRD_NO)
    """):
        qts[g(prd_b)] = float(qty or 0)

    # ⑦ 内存展开：毛需求 + 每个母件自己的材料需求（生产仓抵冲要用）
    def expand(prd, qty, depth=0, seen=None):
        if seen is None:
            seen = set()
        if prd in seen or depth >= 8:
            return {}
        guid = hdr.get(prd)
        if not guid:
            return {}
        seen.add(prd)
        out = {}
        for cprd, cq, cbas, cknd in kids.get(guid, []):
            ratio = cq / cbas if cbas else cq
            need = qty * ratio
            out[cprd] = out.get(cprd, 0.0) + need
            if cknd in ('2', '3'):
                for k, v in expand(cprd, need, depth + 1, seen.copy()).items():
                    out[k] = out.get(k, 0.0) + v
        return out

    gross, by_fg, no_bom = {}, {}, []
    for fgp, fgq in fg.items():
        d = expand(fgp, fgq)
        if not d:
            no_bom.append((fgp, fgq))
            continue
        by_fg[fgp] = d
        for k, v in d.items():
            gross[k] = gross.get(k, 0.0) + v

    # 无 BOM：成品/半成品报警且不计；其余（外购件/包材）未出直接当自己的毛需求
    no_bom_missing, no_bom_direct = [], []
    for fgp, fgq in no_bom:
        typ = (prdt.get(fgp) or ('', '', '', '0', '0'))[3]
        if typ in _GNET_MADE_TYPES:
            no_bom_missing.append({"prd": fgp, "qty": fgq, "type": typ})
        else:
            gross[fgp] = gross.get(fgp, 0.0) + fgq
            no_bom_direct.append({"prd": fgp, "qty": fgq, "type": typ})

    # ⑦.5 滚算（方案 B，MAK 2026-09-30 拍板）
    #   毛需求不再"Σ成品未出×系数一次展开"，改成逐层净算：
    #     每层 需求 − 可用(材料仓+生产仓+在途+请购) = 要生产 → ×系数推给子件 → 逐层往下
    #   母件有货 → 子件不再做（滚算核心）。共用件库存进全局池，不分（总量口径不需要分配规则）。
    #   展开规则与 ⑦ 的 expand() 完全一致：系数=QTY/QTY_BAS，只有子件 KND∈{2,3} 才继续往下；
    #   深度 ≤8 由 expand() 保证，环内品号由拓扑序天然排除（另报 bom_cycles）。
    #   ⚠ gross 换血是唯一入口改动 → ⑨ 的 net 算式一个字不改。
    _knds, _mg = {}, {}
    for _guid, _lst in kids.items():
        _pu = node.get(_guid, (None,))[0]
        if not _pu:
            continue
        for _cprd, _cq, _cbas, _cknd in _lst:
            # ⚠ kids 里存的是 (品号, QTY, QTY_BAS, KND)，不是算好的系数 —— 必须现场除
            _ratio = (_cq / _cbas) if _cbas else _cq
            _knds.setdefault(_cprd, set()).add(_cknd)      # 该品号作为子件出现过的所有 KND
            _mg.setdefault(_pu, {})[_cprd] = max(_mg.get(_pu, {}).get(_cprd, 0.0), _ratio)

    def _can_expand(p):
        # 成品根总能往下展开；其余只有 组件/中间件(KND 2/3) 才继续。
        # 判据用**确定性并集**：「任一 BOM 行里作为子件出现过 KND∈{2,3}」或「PRDT.KND∈{2,3}」。
        # ⚠ 不能取"最后一次出现的 KND"（依赖查询行序）；也不能用"有 BOM 树"当判据
        #    —— 有 BOM 树的外购件（WX-/LH- 系列）不该往下推，否则子件会多算。
        if p in fg:
            return True
        if any(k in ('2', '3') for k in _knds.get(p, ())):
            return True
        return (prdt.get(p) or ('', '', '', '0', '0'))[4] in ('2', '3')

    def _avail(p):
        # 与 ⑨ 同口径：各分量先夹 0（负账面不放大需求），再求和
        return max(0.0, mat_stock.get(p, 0.0)) + max(0.0, prod_stock.get(p, 0.0)) \
            + max(0.0, onway.get(p, 0.0)) + max(0.0, qts.get(p, 0.0))

    # 种子：成品全厂未出 + 无 BOM 的外购件/包材（与上面同规则）
    dem = {}
    for _fgp, _fgq in fg.items():
        dem[_fgp] = dem.get(_fgp, 0.0) + _fgq
    for _fgp, _fgq in no_bom:
        if (prdt.get(_fgp) or ('', '', '', '0', '0'))[3] not in _GNET_MADE_TYPES:
            dem[_fgp] = dem.get(_fgp, 0.0) + _fgq

    # 拓扑排序（Kahn），父 → 子
    _indeg = {}
    for _pu, _cs in _mg.items():
        for _c in _cs:
            _indeg[_c] = _indeg.get(_c, 0) + 1
    _dq = deque([p for p in _mg if _indeg.get(p, 0) == 0])
    _order, _seen = [], set()
    while _dq:
        _p = _dq.popleft()
        if _p in _seen:
            continue
        _seen.add(_p)
        _order.append(_p)
        for _c in _mg.get(_p, ()):
            _indeg[_c] -= 1
            if _indeg[_c] <= 0:
                _dq.append(_c)
    bom_cycles = sorted(p for p in _indeg if p not in _seen)

    # ⚠ _order 只含 _mg 图里的品号；种子直接给需求的「无 BOM 外购件/包材」不在图里 → 补到末尾。
    #   它们没有子件，push 循环天然空转（不改任何数），只是让它们的「本层自身被可用抵」也被记下来，
    #   否则「不滚算 − 上游抵 − 本层抵 = 净需求」在这些品号上摆不平（如 SC0081 请购 4 万抵了 4 千需求）。
    for _p in list(dem):
        if _p not in _seen:
            _seen.add(_p)
            _order.append(_p)

    # ★ 被上游可用抵（MAK 2026-10-02）：
    #   _cut[p] = 上游各层被可用（材料仓+生产仓+在途+请购）抵掉的量，按系数累计到 p；
    #   _src[p] = {来源品号: 量}，含 p 自己那一份（键为 p，展示时要区分开）。
    #   ⚠ 抵扣的传递与需求的推送是两件事：一个品号自己滚算需求=0（被上游抵光）时，
    #     仍必须把累计抵扣传给子件，否则子件的「被上游抵」会凭空少一块。
    # ★ 不滚算毛需求（MAK 2026-10-02）：同一次滚算的**同一张 _mg 图** + 同一批种子，只做纯展开，
    #   一分库存都不扣。用途：让「不滚算毛需求 − 被上游可用抵 − 本层自身可用抵 = 全厂净需求」
    #   这条式子成立（MAK 要的就是这个读法）。
    #   ⚠ 必须用 _mg 图：⑦ 的 expand() 是另一套读法（单根 GUID/深度≤8/同边相加），实测有 370 个
    #   品号两套结果不同 → 拿 ⑦ 的数来摆这条式子会摆不平。
    _flat = dict(dem)
    for _p in _order:
        _n = _flat.get(_p, 0.0)
        if _n <= 0 or not _can_expand(_p):
            continue
        for _c, _r in _mg.get(_p, {}).items():
            _flat[_c] = _flat.get(_c, 0.0) + _n * _r

    _cut, _src = {}, {}
    #   _kx[p][o] = 来源 o 到本料 p 的链上配比和（Σ 各条路径上的系数连乘），
    #   供弹窗显示「左列 × k = 右列」——与 _src 同步累加，恒等式 _src[p][o] == own_cut[o] * _kx[p][o] 成立。
    _kx = {}
    for _p in _order:
        _need = dem.get(_p, 0.0)
        _own = min(_need, _avail(_p)) if _need > 0 else 0.0   # 本层被可用抵掉的量（原本算完即弃）
        if _own > 0:
            _src.setdefault(_p, {})[_p] = _src.get(_p, {}).get(_p, 0.0) + _own
            _kx.setdefault(_p, {})[_p] = 1.0                  # 自己对自己：配比 1
        _tot = _cut.get(_p, 0.0) + _own                       # 本层累计抵扣 = 继承的 + 自身的
        if not _can_expand(_p):
            continue                                          # 不可展开：不推需求、也不传抵扣
        for _c, _r in _mg.get(_p, {}).items():
            if _need > _own:                                  # 等价于原来的 _rem > 0
                dem[_c] = dem.get(_c, 0.0) + (_need - _own) * _r
            if _tot > 0:                                      # ⚠ need=0 也要传
                _cut[_c] = _cut.get(_c, 0.0) + _tot * _r
                _s = _src.setdefault(_c, {})
                _x = _kx.setdefault(_c, {})
                for _o, _a in _src.get(_p, {}).items():
                    _s[_o] = _s.get(_o, 0.0) + _a * _r
                    _x[_o] = _x.get(_o, 0.0) + _kx.get(_p, {}).get(_o, 0.0) * _r

    gross = {p: v for p, v in dem.items() if v > 0}     # ★ 毛需求 = 滚算后的本层需求

    # ⑧ 生产仓归属判定（**只作展示/报警**，不再影响净需求算式 —— MAK 2026-09-30）
    #   滚算里生产仓按 _avail() 直接算该层可用；归属信息保留给 PMC 看"这批给谁做的"。
    owners, prod_used, prod_pool = {}, {}, {}
    multi_owner, unknown_owner, neg_clamped, bom_owned = [], [], [], []
    # ★ 保险（2026-10-02 MAK）：MOT 唯一 X 且 BOM 唯一 Y 且 X≠Y → 报警。
    #   归属结果不变（仍按 MOT），只把「MOT 可能过时」从静默相信变成会自己喊的报警。
    owner_mismatch = []
    for p, s in prod_stock.items():
        if s <= 0:
            if s < 0:
                neg_clamped.append({"prd": p, "kind": "生产仓", "qty": s})
            continue
        os_ = own.get(p) or set()
        if len(os_) == 1:
            f = next(iter(os_))
            # ★ A（2026-10-02）：抵扣量改用「本料本层需求 gross[p]」。
            #   原来取 by_fg.get(母件)——by_fg 只装成品(KND=2)，而归属到的母件大多是
            #   半成品(KND=3) → 查不到 → need=0 → 抵 0、在制全额记成「回池」（实测 84 个
            #   已归属料里只有 1 个真抵过）。gross[p] 是滚算后的本层需求=所有母件合计要它
            #   多少，单母件时用它做上限不会抵过头。
            need = max(0.0, gross.get(p, 0.0))
            x = min(s, need)
            owners[p] = f
            prod_used[p] = x
            prod_pool[p] = s - x
            _bs1 = bom_owners(p)
            if len(_bs1) == 1 and next(iter(_bs1)) != f:
                owner_mismatch.append({"prd": p, "qty": s, "mot": f, "bom": next(iter(_bs1))})
        elif len(os_) > 1:
            multi_owner.append({"prd": p, "qty": s, "owners": sorted(os_), "src": "MOT"})
        else:
            # MOT 查不到 → 去 BOM 查（顶层成品口径）
            bs = sorted(bom_owners(p))
            if len(bs) == 1:
                f = bs[0]
                need = max(0.0, gross.get(p, 0.0))
                x = min(s, need)
                owners[p] = f
                prod_used[p] = x
                prod_pool[p] = s - x
                bom_owned.append({"prd": p, "qty": s, "owner": f, "used": round(x, 3)})
            elif len(bs) > 1:
                multi_owner.append({"prd": p, "qty": s, "owners": bs, "src": "BOM"})
            else:
                unknown_owner.append({"prd": p, "qty": s})

    # ★ C（2026-10-02）：母件被归属了多少在制（反查汇总，纯展示）——
    #   给 PMC 树里的母件行显示「其中 X 在外发/车间在制」，不动任何净需求
    child_inproc = {}
    for _cp, _cf in owners.items():
        _cu = prod_used.get(_cp, 0.0)
        if _cu > 0:
            child_inproc[_cf] = child_inproc.get(_cf, 0.0) + _cu

    # ⑨ 逐品号算净需求
    used_by = {}
    for fgp, d in by_fg.items():
        for k in d:
            used_by.setdefault(k, []).append(fgp)
    keys = set(gross)
    for d in (mat_stock, prod_stock, onway, qts):
        keys |= set(d)
    items = []
    for p in keys:
        gq = gross.get(p, 0.0)
        ms = mat_stock.get(p, 0.0)
        dirty = (p == _GNET_DIRTY_PRD and ms > 0)
        if ms < 0 and not dirty:
            neg_clamped.append({"prd": p, "kind": "材料仓", "qty": ms})
            ms = 0.0
        ps_all = prod_stock.get(p, 0.0)
        # 生产仓全部算可用（含外发仓，MAK 2026-09-30）；归属只作展示，不再影响算式
        # 负账面夹 0（与滚算 _avail() 同口径，不放大需求）
        ps_use = max(0.0, ps_all)
        ow, qt = max(0.0, onway.get(p, 0.0)), max(0.0, qts.get(p, 0.0))
        net = gq - (ms + ps_use + ow + qt)
        if net < 0:
            net = 0.0
        if gq <= 0 and net <= 0:
            continue
        nm, spc, ut, typ, knd = prdt.get(p) or ('', '', '', '0', '0')
        fl = []
        if dirty:
            fl.append("库存脏数据")
        if typ not in _GNET_MADE_TYPES and p in hdr:
            fl.append("外购件直算")
        ub = used_by.get(p, [])
        items.append({
            "prd": p, "name": nm, "spc": spc, "ut": ut, "type": typ, "knd": knd,
            "gross": round(gq, 3), "gross_flat": round(_flat.get(p, 0.0), 3),
            "flat_gap": round(gq - _flat.get(p, 0.0), 3), "mat_stock": round(ms, 3),
            "prod_stock": round(ps_all, 3), "prod_used": round(prod_used.get(p, 0.0), 3),
            "prod_pool": round(prod_pool.get(p, 0.0), 3),
            "onway": round(ow, 3), "qts": round(qt, 3), "net": round(net, 3),
            "owner": owners.get(p, ""), "fg_count": len(ub), "fg_top": ub[:3],
            "child_inproc": round(child_inproc.get(p, 0.0), 3),
            # 被上游可用抵（MAK 2026-10-02）：cut = 上游各层累计抵掉的（不含自身）；
            # own_cut = 本层自己那一份（只在推给子件时生效，母件行小字用）；
            # cut_top = 上游来源 Top5（不含自己）
            "cut": round(_cut.get(p, 0.0), 3),
            "own_cut": round(_src.get(p, {}).get(p, 0.0), 3),
            "cut_top": [{"prd": _k, "name": (prdt.get(_k) or ('',))[0], "qty": round(_v, 3),
                         "k": round(_kx.get(p, {}).get(_k, 0.0), 6)}
                        for _k, _v in
                        sorted(((k2, v2) for k2, v2 in _src.get(p, {}).items() if k2 != p),
                               key=lambda kv: -kv[1])[:5]],
            "flags": fl,
        })
    items.sort(key=lambda x: (-x["net"], -x["gross"], x["prd"]))

    # ★ 「全厂毛需求」来源包（MAK 2026-10-02 hover 需求）：
    #   反向图 _rvg[子] = [(母, 系数)] + 可展开母件集 _exp + 不滚算毛需求 _flat + 成品种子 fg。
    #   前端从任意品号往上爬，累乘路径系数，就能列出「所有相关成品 × 未出 × 累计用量」。
    #   ⚠ 与 _flat 必须同一张 _mg 图、同一套 max 系数、同一批种子，否则合计数对不上。
    try:
        _rvg = {}
        for _pu, _cs in _mg.items():
            for _c2, _r2 in _cs.items():
                _rvg.setdefault(_c2, []).append((_pu, _r2))
        _FLATSRC["data"] = {
            "rvg": _rvg,
            "exp": set(_p for _p in _mg if _can_expand(_p)),
            "flat": _flat,
            "fg": fg,
            "prdt": prdt,
            "hdr": hdr,
            "seeds_no_bom": {d["prd"]: d["qty"] for d in no_bom_direct},
        }
        _FLATSRC["ts"] = _t2.time()
    except Exception:
        _FLATSRC["data"] = None

    # ⑩ 成品自己有货清单（只列出给人看，**不参与任何抵冲、不进净需求算式**）
    # 这些品号是成品/半成品，没被别的品号当材料用到，所以不出现在上面的 items 里；
    # 它们自己的库存（材料仓/生产仓）不属于「材料供给」，只在提示块里展示，由人判断要不要买。
    fg_self = []
    for fgp, fgq in fg.items():
        # ⚠ 滚算后成品根也在 gross 里，不能再拿 "fgp in gross" 当判据（会把成品自有货全过滤掉）
        # 真正要排除的是「被别的品号当材料用到」的品号 → 用 ⑨ 建的 used_by
        if fgp in used_by:
            continue
        ms = mat_stock.get(fgp, 0.0)
        ps = prod_stock.get(fgp, 0.0)
        if ms < 0:
            ms = 0.0
        if ps < 0:
            ps = 0.0
        if ms <= 0 and ps <= 0:
            continue
        fg_self.append({
            "prd": fgp, "name": (prdt.get(fgp) or ('', '', '', '0', '0'))[0],
            "qty": round(fgq, 3), "mat": round(ms, 3), "prod": round(ps, 3),
            "onway": round(onway.get(fgp, 0.0), 3), "qts": round(qts.get(fgp, 0.0), 3),
        })
    fg_self.sort(key=lambda x: -(x["mat"] + x["prod"]))
    conn.close()

    unowned = sum(x["qty"] for x in multi_owner) + sum(x["qty"] for x in unknown_owner)
    return {
        "ts": _t.time(),
        "cost": round(_t.time() - t0, 2),
        "fg_count": len(fg),
        "material_count": len(gross),
        "gross_total": round(sum(gross.values()), 3),
        "net_total": round(sum(x["net"] for x in items), 3),
        "prod_total": round(sum(prod_stock.values()), 3),
        "prod_used_total": round(sum(prod_used.values()), 3),
        "bom_owned_total": round(sum(x["qty"] for x in bom_owned), 3),
        "bom_owned": sorted(bom_owned, key=lambda x: -x["qty"]),
        "prod_pool_total": round(sum(prod_pool.values()), 3),
        "unowned_prod_total": round(unowned, 3),
        "fg_self_total": round(sum(x["mat"] + x["prod"] for x in fg_self), 3),
        "fg_self": fg_self,
        "items": items,
        "alerts": {
            "bom_cycles": bom_cycles,
            "multi_owner": sorted(multi_owner, key=lambda x: -x["qty"]),
            "owner_mismatch": sorted(owner_mismatch, key=lambda x: -x["qty"]),
            "unknown_owner": sorted(unknown_owner, key=lambda x: -x["qty"]),
            "no_bom_missing": sorted(no_bom_missing, key=lambda x: -x["qty"]),
            "no_bom_direct": sorted(no_bom_direct, key=lambda x: -x["qty"]),
            "neg_clamped": neg_clamped,
            "dirty": [{"prd": _GNET_DIRTY_PRD, "qty": mat_stock.get(_GNET_DIRTY_PRD, 0.0)}]
                     if _GNET_DIRTY_PRD in mat_stock else [],
        },
    }


@app.get("/api/pmc/global_net")
async def pmc_global_net(
    refresh: int = Query(default=0),
    slim: int = Query(default=0),
    db: str = Query(default="c041")
):
    """全局净需求（只读）。缓存 10 分钟；refresh=1 强制重算。

    slim=1 只回 {品号: [毛需求, 材料仓, 生产仓, 在途, 请购, 净需求, 母件在制, 被上游可用抵, 本层自身被抵]}，
    供 PMC 预览的 BOM 树按品号 join（复用同一个 _GNET 缓存，不重算、不影响下面原样返回）。
    """
    import time as _t
    db_name = "T041" if db.lower() == "t041" else "C041"
    stale = (_t.time() - _GNET["ts"]) > _GNET_TTL
    if refresh or _GNET["data"] is None or stale:
        with _GNET_LOCK:
            if refresh or _GNET["data"] is None or (_t.time() - _GNET["ts"]) > _GNET_TTL:
                try:
                    _GNET["data"] = _calc_global_net(db_name, refresh=1 if refresh else 0)
                    _GNET["ts"] = _GNET["data"].get("ts", _t.time())
                    _GNET["err"] = None
                except Exception as e:
                    _GNET["err"] = f"{type(e).__name__}: {e}"
    d = dict(_GNET["data"] or {})
    d["err"] = _GNET["err"]
    if slim and d.get("items") and not d.get("err"):
        return {
            "ts": d.get("ts"), "cost": d.get("cost"), "slim": 1,
            "map": {i["prd"]: [i["gross"], i["mat_stock"], i["prod_stock"],
                               i["onway"], i["qts"], i["net"],
                               i.get("child_inproc", 0), i.get("cut", 0),
                               i.get("own_cut", 0), i.get("gross_flat", 0)] for i in d["items"]},
        }
    return d


@app.get("/api/pmc/flat_src")
async def pmc_flat_src(prd_no: str = Query(...), db: str = Query(default="c041")):
    """「全厂毛需求」的来源明细（只读，前端 hover 用）：
    所有相关成品 × 未出 × 累计用量 = 本料毛需求（不滚算口径）。

    数据取自上一次 _calc_global_net 存下的内存包（不重算），包过期就先重算一次。
    ⚠ 往上爬必须遵守「只有可展开的母件才把量推给子件」——与 _flat 的纯展开同规则，
      否则合计对不上不滚算毛需求（响应里给 diff 自检，正常应为 0）。
    """
    import time as _t
    db_name = "T041" if db.lower() == "t041" else "C041"
    if _FLATSRC["data"] is None or (_t.time() - _FLATSRC["ts"]) > _GNET_TTL:
        if _GNET["data"] is None or (_t.time() - _GNET["ts"]) > _GNET_TTL:
            with _GNET_LOCK:
                if _GNET["data"] is None or (_t.time() - _GNET["ts"]) > _GNET_TTL:
                    _GNET["data"] = _calc_global_net(db_name)
                    _GNET["ts"] = _GNET["data"].get("ts", _t.time())
    b = _FLATSRC["data"] or {}
    rvg = b.get("rvg") or {}
    exp = b.get("exp") or set()
    flat, fg = b.get("flat") or {}, b.get("fg") or {}
    prdt, hdr = b.get("prdt") or {}, b.get("hdr") or {}
    seeds = b.get("seeds_no_bom") or {}
    nm, spc, ut, typ, knd = (prdt.get(prd_no) or ('', '', '', '0', '0'))

    # 往上爬：k = 各条路径系数连乘之和（同一张 _mg 图、同一套 max 系数）
    contrib, tops = {}, set()
    stack, guard = [(prd_no, 1.0, 0)], 0
    while stack:
        p, k, dep = stack.pop()
        guard += 1
        if guard > 300000 or dep > 40:
            continue
        if p in fg or p in seeds:          # 种子（成品未出 / 无 BOM 直采件）：记一份贡献
            contrib[p] = contrib.get(p, 0.0) + k
            # ⚠ 但不能就此停下：它自己可能同时还是别人的子件（量会继续往上来自更高的成品，
            #   后端 _flat 也是「种子值 + 继承来的值」一起再往下推）。停在它会漏掉那部分 →
            #   全库核对会冒出一两个 diff≠0 的品号（实测 03002011200800144 差 0.065）。
        par = rvg.get(p) or []
        if not par:
            if p in hdr:
                tops.add(p)                # 爬到顶、但这次没有未出订单的成品
            continue
        for pp, r in par:
            if pp in exp:                  # 只有能往下展开的母件才把量推给子件
                stack.append((pp, k * r, dep + 1))

    meta = {}
    if contrib:
        conn = get_conn(db=db_name)
        try:
            cur = conn.cursor()
            keys = list(contrib.keys())
            ph = ','.join(['%s'] * len(keys))
            cur.execute(
                "SELECT CONVERT(varbinary(60), PRD_NO), CONVERT(varbinary(60), ISNULL(指令单号,'')), "
                "       CONVERT(varbinary(60), ISNULL(OS_NO,'')), SUM(QTY - ISNULL(SAQTY,0)) "
                "FROM VW_POS WITH(NOLOCK) "
                "WHERE USABLE = 1 AND CLS_ID = 0 AND OS_ID = 'SO' AND ISNULL(WJ,0) <> 1 "
                "  AND QTY > ISNULL(SAQTY,0) AND APP_ID = 1 AND PRD_NO IN (" + ph + ") "
                "GROUP BY CONVERT(varbinary(60), PRD_NO), CONVERT(varbinary(60), ISNULL(指令单号,'')), "
                "         CONVERT(varbinary(60), ISNULL(OS_NO,''))",
                tuple(x.encode('gbk') for x in keys))
            for bp, bd, bs2, qv in cur.fetchall():
                e = meta.setdefault(g(bp), {"ddjh": [], "so": [], "qty": 0.0})
                d2, s2 = g(bd).strip(), g(bs2).strip()
                if d2 and d2 not in e["ddjh"]:
                    e["ddjh"].append(d2)
                if s2 and s2 not in e["so"]:
                    e["so"].append(s2)
                e["qty"] += float(qv or 0)
        finally:
            conn.close()

    rows, tot = [], 0.0
    for p, k in contrib.items():
        # ⚠ 与后端种子完全一致：dem = 成品未出(fg) + 无 BOM 直采件(no_bom_direct)。
        #   两者都可能命中同一个品号（如 SC0081：既是外购件又有销售未出）→ 后端是相加，
        #   这里也必须相加，否则 diff 不为 0。（那个「重复计入」本身另议，见下。）
        base = fg.get(p, 0.0) + seeds.get(p, 0.0)
        need = base * k
        tot += need
        m = meta.get(p) or {}
        rows.append({
            "fg": p, "fg_name": (prdt.get(p) or ('',))[0],
            "ddjh": sorted(m.get("ddjh") or []), "so": sorted(m.get("so") or []),
            "qty": round(fg.get(p, 0.0), 3), "extra": round(seeds.get(p, 0.0), 3),
            "k": round(k, 6), "need": round(need, 3),
            "is_self": p == prd_no, "no_bom": p in seeds,
        })
    rows.sort(key=lambda r: -r["need"])
    gf = flat.get(prd_no, 0.0)
    return {
        "prd_no": prd_no, "name": nm, "spc": spc, "ut": ut, "knd": knd,
        "gross_flat": round(gf, 3),
        "total": round(tot, 3),
        "diff": round(tot - gf, 3),
        "is_direct": prd_no in seeds,
        "has_bom": prd_no in hdr,
        "rows": rows,
        # ⚠ 必须排掉已经出现在明细里的成品（它们爬到顶了但也确实有未出订单），
        #   否则「另有 N 个没有未出订单」会把有单的也算进去（实测铁杆：19+34 而不是 19+15）
        "no_order": [{"fg": p, "fg_name": (prdt.get(p) or ('',))[0]} for p in sorted(tops) if p not in contrib],
    }


@app.get("/api/pmc/preview_mps")
async def pmc_preview_mps(
    so_no_itm: str = Query(...),
    prd_no: str = Query(...),
    qty: float = Query(...),
    db: str = Query(default="c041")
):
    """单张单预览（前端批量预览走 /api/pmc/preview_batch）。"""
    conn = get_conn(db="T041" if db.lower() == "t041" else "C041")
    try:
        return _pmc_preview_one(conn, so_no_itm, prd_no, qty)
    finally:
        conn.close()


@app.post("/api/pmc/preview_batch")
async def pmc_preview_batch(payload: dict = Body(...), db: str = Query(default="c041")):
    """批量预览：几个成品合在一起跑需求（不写库）。

    材料仓 + 别人多下由 _StockAlloc 跨单共享，按 items 的顺序扣完为止 —— 所以
    逐行缺口相加 = 本次合计缺口（跟生成 MPS 落库同一套规则）。
    """
    conn = get_conn(db="T041" if db.lower() == "t041" else "C041")
    alloc = _StockAlloc()
    all_rows, groups = [], []
    try:
        for it in (payload.get("items") or []):
            so_no_itm = str(it.get("so_no_itm") or "")
            prd_no = str(it.get("prd_no") or "")
            if not so_no_itm or not prd_no:
                continue
            d = _pmc_preview_one(conn, so_no_itm, prd_no, float(it.get("qty") or 0), alloc)
            all_rows.extend(d["items"])
            groups.append({"so_no_itm": so_no_itm, "prd_no": prd_no, "count": len(d["items"])})
    finally:
        conn.close()
    return {"items": all_rows, "groups": groups, "batch": True}


def _prd_meta(prd_nos, db_conn):
    """品号 → {prd_no: {'name','ut','spc','has_bom'}}（批量，别逐行查）。

    PRD_NAME 是 nvarchar 且历史上有脏行 → 走 CONVERT(varbinary)+g() 解码，不直接读。
    """
    prd_nos = [p for p in dict.fromkeys(prd_nos) if p]
    if not prd_nos:
        return {}
    cur = db_conn.cursor()
    params = tuple(p.encode("gbk") for p in prd_nos)
    ph = ','.join(['%s'] * len(prd_nos))
    out = {}
    cur.execute(f"""
        SELECT CONVERT(varbinary(60), PRD_NO), CONVERT(varbinary(600), NAME), UT, SPC
        FROM PRDT WITH(NOLOCK) WHERE PRD_NO IN ({ph})
    """, params)
    for r in cur.fetchall():
        out[g(r[0])] = {'name': g(r[1]), 'ut': g(r[2]), 'spc': g(r[3]), 'has_bom': False}
    cur.execute(f"""
        SELECT DISTINCT CONVERT(varbinary(60), PRD_NO) FROM BOM WITH(NOLOCK)
        WHERE LEV=0 AND PRD_NO IN ({ph})
    """, params)
    for r in cur.fetchall():
        k = g(r[0])
        if k in out:
            out[k]['has_bom'] = True
    return out


@app.post("/api/pmc/generate_mps")
async def generate_mps(
    body: dict = Body(...),
    db: str = Query(default="c041")
):
    """
    选中的多个 POS 行 → 一张 MPS 单（每个销售订单行一个成品 ITM + 其 BOM 子件 ITM）。

    ⚠ ERP 硬规则（MPS_insert 触发器）：引用的销售订单行必须 APP_ID=1（系统核准），
      否则 print '引用订单未审批' 并 rollback transaction → 整张单打回。
    字段口径照真单 MP26090046：
      REF_ITM 只有成品行填（= 本 POS 行的 SO_NO_ITM），子件行留空 —— VW_POS 的 MPQTY/MP
        就是按 REF_ITM = OS_NO+3位ITM 统计 QTY_SO 的，填错/漏填这张单不会从待分析列表走掉
      QTY = 净需求 = max(0, 毛需求 − 本单可用池(挂本单在途/在单请购/别人多下) − 该品号材料仓)
       —— 与屏上「缺口」同一算式（MAK 2026-09-30「要扣」）；QTY_SO = 毛需求；QTY_AV = 材料仓 − 毛需求
      UT/品名/规格 取 PRDT；USR = 0014（PMC）；BOM = 该品号有没有 BOM 头
    body: {items:[{so_no_itm, prd_no, qty}], dry:1 → 校验+INSERT 后 ROLLBACK}
    """
    dry = str(body.get("dry") or "") in ("1", "true", "True")
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

    # 按「销售订单行」逐个出成品行（不跨行合并）：真单 MP26090046 就是一个 SO 行一个成品行，
    # VW_POS 的 MPQTY 只认 REF_ITM = 本 SO 行 → 合并了那张单就不会从待分析列表走掉
    merged = {}             # so_no_itm -> {prd_no, qty}
    for it in valid:
        soi = it["so_no_itm"]
        grp = merged.setdefault(soi, {"prd_no": it["prd_no"], "qty": 0.0})
        grp["qty"] += float(it["qty"])
        grp["prd_no"] = it["prd_no"]

    # 每个 SO 行各取各的挂靠信息（客户/指令单号/交期/单价），一条 IN 查询
    so_info = {}
    so_list = list(merged.keys())
    ph = ','.join(['%s'] * len(so_list))
    cur.execute(f"""
        SELECT SO_NO_ITM, CUS_NO, CUS_NAME, 指令单号, EST_DD, UP
        FROM POS WITH(NOLOCK) WHERE SO_NO_ITM IN ({ph})
    """, tuple(so_list))
    for r in cur.fetchall():
        so_info[g(r[0])] = {'cus_no': g(r[1]), 'cus_name': g(r[2]), 'ref': g(r[3]),
                            'est_dd': r[4], 'up': float(r[5] or 0)}

    # MPS 序号
    cur.execute("""
        SELECT ISNULL(MAX(TRY_CAST(SUBSTRING(MPS_NO,7,4) AS INT)), 0)
        FROM MPS WITH(NOLOCK)
        WHERE MPS_NO LIKE %s AND LEN(MPS_NO)=10
    """, (f"MP{today}%",))
    seq = (cur.fetchone()[0] or 0) + 1
    mps_no = f"MP{today}{seq:04d}"

    # 成品库存（_v2_stock 返回 {prd_no: {'prod_wh','mat_wh','mat_qty','mat_av',...}}）
    all_fg = [v["prd_no"] for v in merged.values()]
    stock_map = dict(_v2_stock(all_fg, conn)) if all_fg else {}

    all_mps_rows = []
    itm_counter = 0

    # 同品号库存按行顺序累计抵冲，扣完为止（照真单口径）
    #   MP26070015/H20554-01-05：库存 2,600 → 头两行各 1,002 扣成 0、第三行 1,002−596=406，其余全额
    #   MP26080031/P20365-01-03：库存 12,800 → 首行 27,200−12,800=14,400，其余全额
    #   QTY_AV = 该行扣减前的剩余库存 − 本行毛需求（两例逐行都对得上）
    # ⚠ 不做累计就会重复扣：同一个料挂在两个成品下时每行各扣一遍库存 → 净需求偏小
    # ponytail: 内存字典足够（一张 MPS 单最多几百行）；跨单共用料不在这里扣，那是品号汇总的活
    stock_left = {}

    def _take_stock(prd, mat_qty, need):
        """(本行净需求, 本行扣减前的剩余库存, 本行抵冲掉的库存)"""
        left = max(0.0, stock_left.setdefault(prd, float(mat_qty or 0)))   # 负库存夹 0
        take = min(left, need)
        stock_left[prd] = round(left - take, 4)
        return round(max(0.0, need - take), 4), round(left, 4), round(take, 4)

    # 本单可用池也按行顺序累计抵冲、扣完为止（MAK 2026-09-30「要扣」）：
    #   挂本单在途 + 挂本单在单请购（按单持有，别人挂着的不算我的）+ 别人采购「多下」（全厂共享）
    # ⚠ 与屏上「缺口」同一口径：净需求 = 毛需求 − 池抵冲 − 材料仓抵冲
    pool_left = {}    # (so_no_itm, 品号) -> [挂本单在途, 挂本单在单请购]
    free_left = {}    # 品号 -> 别人多下（跨单共享，先到先得）

    def _take_pool(so_no_itm, prd, sp, need):
        """返回 (扣完池之后还要多少, 池抵冲了多少)"""
        need = max(0.0, float(need or 0))
        e = pool_left.setdefault((so_no_itm, prd),
                                 [float(sp.get('po') or 0), float(sp.get('qts') or 0)])
        free = max(0.0, free_left.setdefault(prd, float(sp.get('po_free') or 0)))
        take = 0.0
        for k in range(2):
            if need <= 0:
                break
            t = min(e[k], need)
            e[k] -= t
            need -= t
            take += t
        if need > 0 and free > 0:
            t = min(free, need)
            free -= t
            need -= t
            take += t
        free_left[prd] = free
        return round(need, 4), round(take, 4)

    for so_no_itm, info in merged.items():
        prd_no = info["prd_no"]
        qty = info["qty"]
        si = so_info.get(so_no_itm, {})
        fg_stock = stock_map.get(prd_no, {})
        fg_meta = _prd_meta([prd_no], conn).get(prd_no, {})
        fg_mat = round(fg_stock.get('mat_qty', 0), 2)

        # 成品行 ITM：QTY_SO = 毛需求（本单销售未出）、QTY = 净需求、REF_ITM = 本 SO 行
        fg_odr = _v2_odr_split([prd_no], conn, ref=si.get('ref'), fg=prd_no, so_itm=so_no_itm).get(prd_no) or {}
        fg_need, fg_ptake = _take_pool(so_no_itm, prd_no, fg_odr, qty)
        fg_net, fg_left, fg_take = _take_stock(prd_no, fg_mat, fg_need)
        itm_counter += 1
        all_mps_rows.append({
            'itm': itm_counter,
            'prd_no': prd_no,
            'fg_no': prd_no,
            'prd_name': fg_meta.get('name') or '',
            'spc': fg_meta.get('spc') or '',
            'ut': fg_meta.get('ut') or '',
            'qty_so': round(qty, 4),
            'qty': fg_net,
            'take': fg_take,
            'pool_take': fg_ptake,
            'wh': fg_stock.get('prod_wh', '') or fg_stock.get('mat_wh', ''),
            'wh_name': '',
            'qty_wh': fg_mat,
            'qty_av': round(fg_left - qty, 2),
            'ref_itm': so_no_itm,
            'bom': 1 if fg_meta.get('has_bom') else 0,
            'so_no_itm': so_no_itm,
            'cus_no': si.get('cus_no', ''), 'cus_name': si.get('cus_name', ''),
            'ref': si.get('ref', ''), 'est_dd': si.get('est_dd'), 'up': si.get('up', 0),
            'is_fg': True,
        })

        # BOM 递归展开
        comp_rows = _mps_bom_tree(prd_no, conn, qty=qty)
        if not comp_rows:
            continue

        # 子件库存 + 品号资料（各一条批量查询）
        sub_prds = [r[0] for r in comp_rows]
        sub_stock = _v2_stock(sub_prds, conn)
        sub_meta = _prd_meta(sub_prds, conn)
        sub_odr = _v2_odr_split(sub_prds, conn, ref=si.get('ref'), fg=prd_no, so_itm=so_no_itm)

        for c_prd, c_name, c_qty, c_knd, c_parent, c_depth, c_ratio in comp_rows:
            itm_counter += 1
            cs = sub_stock.get(c_prd, {})
            cm = sub_meta.get(c_prd, {})
            c_mat = round(cs.get('mat_qty', 0), 2)
            c_need, c_ptake = _take_pool(so_no_itm, c_prd, sub_odr.get(c_prd) or {}, c_qty)
            c_net, c_left, c_take = _take_stock(c_prd, c_mat, c_need)
            all_mps_rows.append({
                'itm': itm_counter,
                'prd_no': c_prd,
                'fg_no': prd_no,
                'prd_name': cm.get('name') or c_name,
                'spc': cm.get('spc') or '',
                'ut': cm.get('ut') or '',
                'qty_so': round(c_qty, 4),
                'qty': c_net,
                'take': c_take,
                'pool_take': c_ptake,
                'wh': cs.get('mat_wh', ''),
                'wh_name': '',
                'qty_wh': c_mat,
                'qty_av': round(c_left - c_qty, 2),
                'ref_itm': None,          # 子件行不填（同真单 MP26090046）
                'bom': 1 if cm.get('has_bom') else 0,
                'so_no_itm': so_no_itm,
                'cus_no': si.get('cus_no', ''), 'cus_name': si.get('cus_name', ''),
                'ref': si.get('ref', ''), 'est_dd': si.get('est_dd'), 'up': 0,
                'is_fg': False,
            })

    if not all_mps_rows:
        conn.rollback()
        conn.close()
        return {"error": "这些单没有可排产的 BOM 子件（成品没建 BOM？）"}

    # 批量 INSERT（一张 MPS 单多行，ITM 顺序 = 成品行 + 它的子件）
    try:
        for row in all_mps_rows:
            cur.execute("""
                INSERT INTO MPS (
                    MPS_NO,MPS_DD,USR,USABLE,ITM,
                    CUS_NO,CUS_NAME,FG_NO_SO,SO_NO_ITM,REF_ITM,
                    PRD_NO,PRD_NAME,SPC,UT,
                    QTY,QTY_SO,WH,WH_NAME,
                    QTY_WH,QTY_AV,
                    指令单号,EST_DD,UP,
                    BOM,STA_DD
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
            """, (
                mps_no, mps_date, '0014', 1, row['itm'],
                row['cus_no'], row['cus_name'], row['fg_no'], row['so_no_itm'], row['ref_itm'],
                row['prd_no'], row['prd_name'], row['spc'], row['ut'],
                row['qty'], row['qty_so'], row['wh'], row['wh_name'],
                row['qty_wh'], row['qty_av'],
                row['ref'], row['est_dd'], row['up'],
                row['bom'], None,
            ))
        if dry:
            conn.rollback()
            return {'ok': True, 'dry': True, 'mps_no': mps_no, 'total_itm': len(all_mps_rows),
                    'fg_count': len(merged), 'items': len(valid), 'rows': all_mps_rows}
        conn.commit()
    except Exception as e:
        conn.rollback()
        msg = str(e)
        # MPS_insert 触发器：引用未核准(APP_ID=0)的销售订单会 rollback transaction
        if '未审批' in msg or '3609' in msg or 'transaction' in msg.lower():
            return {"error": "ERP 拒绝排产：选中的销售订单里有没「系统核准」(APP_ID=1) 的行。"
                             "MPS_insert 触发器要求先核准才能排产（桌面 ERP → 销售订单 → 核准）。"}
        return {"error": msg}
    finally:
        conn.close()

    return {'ok': True, 'dry': False, 'mps_no': mps_no, 'total_itm': len(all_mps_rows),
            'fg_count': len(merged), 'items': len(valid),
            'results': [{'mps_no': mps_no, 'total_itm': len(all_mps_rows),
                         'items': len(valid), 'fg_count': len(merged)}],
            'rows': all_mps_rows}


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
        mo_no = _next_serial(cur, "MO", "MOM", "MO_NO")
        qd_no = _next_serial(cur, "QD", "QTS", "QT_NO")

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


# ── PMC 一键生成请购单（QD / QTS）─────────────────────────────────────────────
def _next_serial(cur, prefix, tbl, col, width=4):
    """当月流水号：QD26090040 / MO2609xxxx（YYMM + width 位流水）。"""
    head = f"{prefix}{datetime.now().strftime('%y%m')}"
    cur.execute(f"""
        SELECT ISNULL(MAX(CAST(RIGHT(RTRIM({col}),{width}) AS INT)), 0)
        FROM {tbl} WITH(NOLOCK)
        WHERE LEFT(RTRIM({col}),{len(head)}) = %s
          AND LEN(RTRIM({col})) = {len(head)+width}
          AND RIGHT(RTRIM({col}),{width}) NOT LIKE '%[^0-9]%'
    """, (head,))
    r = cur.fetchone()
    return f"{head}{(int(r[0]) if r and r[0] else 0) + 1:0{width}d}"


@app.post("/api/pmc/make_qd")
async def pmc_make_qd(
    body: dict = Body(...),
    db: str = Query(default="c041"),
):
    """PMC 在 MPS 树里勾原材料 + 填数量 → 生成一张请购单（QTS，QT_ID='QD'）。

    body: {so_no_itm, fg_no, est_dd, dry, items: [{prd_no, qty, rem}]}
      est_dd  交期（手填，必填）
      dry=1   走完所有校验+INSERT 后 ROLLBACK（验证用，不留单）

    写库口径（对齐桌面端真单 QD26090038/39，字段全部按真单形态）：
      USR='0014'（PMC）· APP_ID=0（待审）· CLS_ID=0 · USABLE=1 · 删除=0
      CUS_NO='20399' / CUS_NAME='待定'（采购审完才定厂）
      PRD_NAME/UT 取 PRDT · AMT=0 / UP 留空（＝未确认单价，采购才认领）· 删除 不写（＝NULL，同真单）
      指令单号/客户代号/成品编号/订单数量/SO_NO_ITM 挂本单；REF_ITM 留空（app 不建派工单）
    """
    # 数量先解析成数字再筛（防手工构造的 body 送非数字把 float() 炸到 500）
    items = []
    for it in (body.get("items") or []):
        try:
            q = float(it.get("qty") or 0)
        except (TypeError, ValueError):
            return {"error": f"数量不是数字：{it.get('prd_no')}"}
        if q > 0 and it.get("prd_no"):
            it = dict(it); it["qty"] = q
            items.append(it)
    if not items:
        return {"error": "没有勾选请购的原材料（数量要大于 0）"}
    est_dd = (body.get("est_dd") or "").strip()
    if not est_dd:
        return {"error": "请填写交期"}
    try:
        datetime.strptime(est_dd, "%Y-%m-%d")
    except ValueError:
        return {"error": f"交期格式不对（要 YYYY-MM-DD）：{est_dd}"}
    so_no_itm = (body.get("so_no_itm") or "").strip()
    fg_no = (body.get("fg_no") or "").strip()
    dry = str(body.get("dry") or "") in ("1", "true", "True")
    # 批量预览（合并树）里每行挂它自己那张单：行级字段优先，body 级只是缺省值。
    # 单张单的树不带行级字段 → 行为跟以前完全一样。
    for it in items:
        it["so_no_itm"] = (str(it.get("so_no_itm") or "").strip() or so_no_itm)
        it["fg_no"] = (str(it.get("fg_no") or "").strip() or fg_no)
    if not all(it["so_no_itm"] for it in items):
        return {"error": "缺少 SO 行号（请从某张待分析单的 BOM 树里生成）"}

    db_name = "T041" if db.lower() == "t041" else "C041"
    conn = get_conn(db=db_name)
    cur = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # 挂靠字段（订单行 → 指令单号 / 客户代号 / 订单数量）
        # 客户代号：POS.客户代号 多数为空，真值是 POS.CUS_NAME（如 10003→KH003，历史 QD 全是 KH0xx 格式）
        # 每张单只查一次（合并批量预览会挂多张单）
        refs = {}

        def _order_ref(si):
            if si not in refs:
                cur.execute("""
                    SELECT ISNULL(指令单号,''), ISNULL(NULLIF(客户代号,''), ISNULL(CUS_NAME,'')), ISNULL(QTY,0)
                    FROM POS WITH(NOLOCK) WHERE SO_NO_ITM=%s
                """, (si,))
                pr = cur.fetchone()
                refs[si] = None if not pr else (g(pr[0]), g(pr[1]), float(pr[2] or 0))
            return refs[si]

        uniq_so = list(dict.fromkeys(it["so_no_itm"] for it in items))
        missing = [si for si in uniq_so if _order_ref(si) is None]
        if missing:
            conn.rollback()
            return {"error": "POS 里找不到销售订单行 " + "、".join(missing) + "（请从待分析单的 BOM 树里生成）"}

        qd_no = _next_serial(cur, "QD", "QTS", "QT_NO")

        # ponytail: 每行一次 PRDT 查询 + 一次 INSERT。实测量级：300 行 ≈ 25s，
        # 常规请购（几十行）几秒，够用。真嫌慢就把 PRDT 查询并成一条 IN (...) 批量取。

        lines, itm = [], 0
        for it in items:
            prd_no = str(it["prd_no"]).strip()
            qty = round(float(it.get("qty") or 0), 4)
            cur.execute("SELECT CONVERT(varbinary(600), NAME), UT FROM PRDT WITH(NOLOCK) WHERE PRD_NO=%s",
                        (prd_no.encode("gbk"),))
            prow = cur.fetchone()
            if not prow:
                conn.rollback()
                return {"error": f"PRDT 里没有品号 {prd_no}"}
            prd_name = g(prow[0])
            ut = g(prow[1])
            itm += 1
            so_si = it["so_no_itm"]
            order_ref, cus_ref, order_qty = refs[so_si]
            line_fg = it["fg_no"]
            rem1 = f"{line_fg} PMC请购" if line_fg else "PMC请购"
            cur.execute("""
                INSERT INTO QTS (QT_NO,QT_ID,QT_DD,USR,USABLE,CUS_NO,CUS_NAME,ITM,PRD_NO,PRD_NAME,UT,
                                 QTY,EST_DD,CLS_ID,SO_NO_ITM,指令单号,客户代号,成品编号,订单数量,
                                 REM1,EFF_DD,APP_ID,AMT)
                VALUES (%s,'QD',%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s,%s,0,0)
            """, (
                qd_no, now_str, "0014", "20399", "待定", itm, prd_no, prd_name, ut,
                qty, est_dd, so_si, order_ref, cus_ref, line_fg, order_qty,
                rem1, now_str,
            ))
            lines.append({"itm": itm, "prd_no": prd_no, "prd_name": prd_name, "ut": ut, "qty": qty,
                          "so_no_itm": so_si, "指令单号": order_ref, "成品编号": line_fg,
                          "merges": int(it.get("merges") or 1)})

        # 每张单挂的成品（同一 SO 行只对应一个成品；取该单第一行的 fg_no）
        fg_by_so = {}
        for _it in items:
            fg_by_so.setdefault(_it["so_no_itm"], _it["fg_no"])

        def _one(si):
            r_ = refs.get(si) or ("", "", 0.0)
            return {"so_no_itm": si, "指令单号": r_[0], "客户代号": r_[1], "订单数量": r_[2],
                    "成品编号": fg_by_so.get(si, "")}

        if dry:
            conn.rollback()
            head = _one(uniq_so[0]) if len(uniq_so) == 1 else {"so_no_itm": "", "指令单号": "", "客户代号": "", "订单数量": 0.0}
            return {"ok": True, "dry": True, "qd_no": qd_no, "itm_count": len(lines),
                    "so_no_itm": head["so_no_itm"], "指令单号": head["指令单号"], "客户代号": head["客户代号"],
                    "成品编号": fg_no if len(uniq_so) == 1 else "", "订单数量": head["订单数量"],
                    "est_dd": est_dd, "lines": lines, "挂靠": [_one(si) for si in uniq_so]}
        conn.commit()
    except Exception as e:
        conn.rollback()
        if getattr(e, "args", None) and e.args and e.args[0] == 2627:
            return {"error": "单号已被占用，请重试（勿重复提交）"}
        return {"error": str(e)}
    finally:
        conn.close()

    # 回读校验（写入后立刻按单号读回，逐行核对品号/数量/交期）
    try:
        c2 = get_conn(db=db_name)
        cur2 = c2.cursor()
        cur2.execute("""
            SELECT ISNULL(ITM,0), PRD_NO, QTY, UT, CONVERT(varchar(10),EST_DD,120), USR, CAST(APP_ID AS INT)
            FROM QTS WITH(NOLOCK) WHERE QT_NO=%s ORDER BY ITM
        """, (qd_no.encode("gbk"),))
        back = [{"itm": r[0], "prd_no": g(r[1]), "qty": float(r[2] or 0), "ut": g(r[3]),
                 "est_dd": r[4], "usr": g(r[5]), "app": r[6]} for r in cur2.fetchall()]
        c2.close()
    except Exception as e:
        back = [{"error": str(e)}]

    head = _one(uniq_so[0]) if len(uniq_so) == 1 else {"so_no_itm": "", "指令单号": "", "客户代号": "", "订单数量": 0.0}
    return {"ok": True, "dry": False, "qd_no": qd_no, "itm_count": len(lines),
            "so_no_itm": head["so_no_itm"], "指令单号": head["指令单号"], "客户代号": head["客户代号"],
            "成品编号": fg_no if len(uniq_so) == 1 else "", "订单数量": head["订单数量"], "est_dd": est_dd,
            "lines": lines, "回读": back, "挂靠": [_one(si) for si in uniq_so]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)

