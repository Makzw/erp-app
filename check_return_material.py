"""退料功能真机验证（只写 T041 测试库，C041 一行不碰，测完按单号精确清理）。

跑法：cd /home/Mak/erp-app && set -a && . ./.env && set +a && python3.11 check_return_material.py
"""
import json
import os
import urllib.error
import urllib.request

import pymssql

HOST, USER, PWD = os.environ["ERP_DB_HOST"], os.environ["ERP_DB_USER"], os.environ["ERP_DB_PASSWORD"]
BASE = "http://127.0.0.1:8001/api/completion"
OK, BAD = [], []

# 测试单号（全部带 TEST 前缀，便于精确清理）
T30, T23, T31 = "ICTESTR30", "ICTESTR23", "ICTESTR31"


def chk(cond, msg):
    (OK if cond else BAD).append(msg)
    print(("  ✅ " if cond else "  ❌ ") + msg)


def conn(db):
    return pymssql.connect(server=HOST, user=USER, password=PWD, database=db, charset="utf8")


def g(s):
    if isinstance(s, bytes):
        for e in ("gbk", "utf8"):
            try:
                return s.decode(e)
            except Exception:
                pass
        return s.decode("latin1", "replace")
    return s


def call(method, path, body=None):
    url = BASE + path
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=180).read().decode())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode()[:300]}


tc = conn("T041")
cur = tc.cursor()
cc = conn("C041")
ccur = cc.cursor()
ccur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)")
c041_before = ccur.fetchone()[0]


def cleanup():
    """按单号精确清理（含本次写出的退料单）+ T041 里可能残留的同名前缀单。"""
    cur.execute("DELETE FROM IC WHERE IC_NO IN (%s,%s,%s)", (T30, T23, T31))
    n = cur.rowcount
    if RETURNED_NO:
        cur.execute("DELETE FROM IC WHERE IC_NO = %s", (RETURNED_NO,))
        n += cur.rowcount
    cur.execute("DELETE FROM IC WHERE IC_NO LIKE 'ICTESTR%'")
    n += cur.rowcount
    tc.commit()
    return n


RETURNED_NO = None
print("── 准备：从 T041 现成料/仓里取样 + 造测试调拨单与完工扣料行 ──")
cur.execute("SELECT TOP 2 PRD_NO, NAME, ISNULL(UT,'PCE') FROM PRDT WITH(NOLOCK) WHERE LEN(PRD_NO)>4 ORDER BY PRD_NO")
prds = cur.fetchall()
cur.execute("SELECT TOP 3 WH, NAME FROM MY_WH WITH(NOLOCK) ORDER BY WH")
whs = cur.fetchall()
if len(prds) < 2 or len(whs) < 3:
    print("  ⚠ T041 里料/仓不足，无法测试"); raise SystemExit(1)
(p1, n1, ut1), (p2, n2, ut2) = prds[0], prds[1]
wh_from, wh_src, wh_dst = whs[0][0], whs[1][0], whs[2][0]   # 原发出仓 / 料所在仓 / 退回到
print(f"  料: {p1} / {p2}；仓: 原发出={wh_from} 料所在={wh_src} 退回={wh_dst}")

cleanup()   # 先清可能的残留
# 调拨单（KND=30，总数 50 保证列表里 remaining>0 可见）
cur.execute("""INSERT INTO IC (IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH1,WH2,USR,USABLE,ITM,
                               REM,FLD1,指令单号,DDJH,客户,单重,净重)
               VALUES (%s,GETDATE(),30,%s,%s,50,%s,%s,%s,'phone',1,1,'',%s,'9999','TESTPLAN','TESTCUS',1.5,3.0)""",
            (T30, p1, n1, ut1, wh_src, wh_from, T30))
# 完工扣料行（KND=23，两个品号，数量故意不等：7 / 11）
for i, (prd, nm, ut, q) in enumerate([(p1, n1, ut1, 7), (p2, n2, ut2, 11)], start=1):
    cur.execute("""INSERT INTO IC (IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH1,WH2,WH2NAME,USR,USABLE,ITM,
                                   REM,FLD1,指令单号,DDJH,客户,单重,净重)
                   VALUES (%s,GETDATE(),23,%s,%s,%s,%s,'',%s,'',%s,1,%s,%s,%s,'9999','TESTPLAN','TESTCUS',0,0)""",
                (T23, prd, nm, q, ut, wh_src, "phone", i, f"完工出库(TEST×10)", T30))
# 另一个调拨单：没有任何完工扣料行（验「无需退料」护栏）
cur.execute("""INSERT INTO IC (IC_NO,IC_DD,IC_KND,PRD_NO,PRD_NAME,QTY,UT,WH1,WH2,USR,USABLE,ITM,REM,FLD1)
               VALUES (%s,GETDATE(),30,%s,%s,50,%s,%s,%s,'phone',1,1,'','')""",
            (T31, p1, n1, ut1, wh_src, wh_from))
tc.commit()
print("  测试数据已写入 T041（单号 ICTESTR30 / ICTESTR23 / ICTESTR31）")

try:
    print("① 退料预览")
    pv = call("GET", f"/return_preview?db=t041&ic_no={T30}")
    chk(pv.get("found") is True, "找得到该调拨单")
    chk(len(pv.get("items") or []) == 2, f"列出 2 个品号（实际 {len(pv.get('items') or [])}）")
    qs = sorted(round(i["qty"], 3) for i in pv.get("items") or [])
    chk(qs == [7, 11], f"退料量 = 完工扣料量（7 / 11，实际 {qs}）")
    chk(round(pv.get("total_qty") or 0, 3) == 18, f"合计 18（实际 {pv.get('total_qty')}）")
    chk(pv.get("default_wh") == wh_src, f"默认退回仓 = 扣料行 WH2 料所在仓 {wh_src}（实际 {pv.get('default_wh')}）")
    chk(pv.get("from_wh") == wh_from, f"原发出仓 = 调拨单 WH2 {wh_from}（实际 {pv.get('from_wh')}）")
    chk((pv.get("cus"), pv.get("ref"), pv.get("ddjh")) == ("TESTCUS", "9999", "TESTPLAN"), "客户/指令单/外发计划带过来")
    chk(pv.get("returned") is False, "尚未退料（returned=False）")

    print("② dry=1 不写库")
    cur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)"); n_before_dry = cur.fetchone()[0]
    d = call("POST", "/return_material?db=t041&dry=1", {"ic_no": T30, "wh": wh_dst})
    cur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)"); n_after_dry = cur.fetchone()[0]
    chk(d.get("dry") is True and d.get("total_qty") == 18, "dry 返回合计 18")
    chk(n_before_dry == n_after_dry, f"dry 一行没写（{n_before_dry} → {n_after_dry}）")

    print("③ 正式退料（数量框故意传错，验「不管输入多少都全退」）")
    res = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst, "qty": 3})
    chk(res.get("ok") is True, "写入成功")
    RETURNED_NO = res.get("ic_no")
    chk(bool(RETURNED_NO), f"返回入库单号 {RETURNED_NO}")
    chk(res.get("items") == 2 and round(res.get("total_qty") or 0, 3) == 18, f"2 行 / 合计 18（实际 {res.get('items')} / {res.get('total_qty')}）")

    print("④ 落库内容逐项核对")
    cur.execute("""SELECT IC_NO,IC_KND,PRD_NO,QTY,UT,ISNULL(WH1,''),ISNULL(WH1NAME,''),ISNULL(WH2,''),
                          ISNULL(FLD1,''),ISNULL(REM,''),ITM,USABLE,ISNULL(指令单号,''),ISNULL(DDJH,''),ISNULL(客户,'')
                   FROM IC WITH(NOLOCK) WHERE IC_NO=%s ORDER BY ITM""", (RETURNED_NO,))
    rows = cur.fetchall()
    chk(len(rows) == 2, f"入库单 {len(rows)} 行")
    chk(all(r[1] == 13 for r in rows), "全部 KND=13（一张单一种 KND）")
    chk([int(r[10]) for r in rows] == [1, 2], f"ITM 从 1 连续（实际 {[int(r[10]) for r in rows]}）")
    got = {g(r[2]): float(r[3]) for r in rows}
    chk(got.get(p1) == 7 and got.get(p2) == 11, f"数量 = 全退量 7/11，不是手填的 3（实际 {got}）")
    chk(all(g(r[5]) == wh_dst for r in rows), f"WH1 = 输入的退回仓 {wh_dst}")
    chk(all(g(r[7]) == wh_src for r in rows), f"WH2 = 料所在仓 {wh_src}")
    chk(all(g(r[8]) == T30 for r in rows), f"FLD1 = 调拨单号 {T30}（可反查）")
    chk(all(g(r[9]) == f"生产退料({T30})" for r in rows), "REM 标了生产退料+单号")
    chk(all(r[11] == 1 for r in rows), "USABLE=1")
    chk({(g(r[12]), g(r[13]), g(r[14])) for r in rows} == {("9999", "TESTPLAN", "TESTCUS")}, "客户/指令单/外发计划随单带过来")

    print("⑤ 防重复退 + 列表标记")
    pv2 = call("GET", f"/return_preview?db=t041&ic_no={T30}")
    chk(pv2.get("returned") is True, "预览：returned=True")
    dup = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst})
    chk("已退过料" in (dup.get("error") or ""), f"再退被拒：{dup.get('error')}")
    lst = call("GET", f"/transfer?db=t041&prd_no={p1}")
    hit = [i for i in (lst.get("items") or []) if i.get("ic_no") == T30]
    if not hit:
        print("     ↳ 调试：列表返回 =", json.dumps(lst, ensure_ascii=False)[:600])
        lst_all = call("GET", "/transfer?db=t041")
        print("     ↳ 调试：不带筛选 =", json.dumps(lst_all, ensure_ascii=False)[:400])
    chk(bool(hit) and hit[0].get("returned") is True, "完工列表里该单 returned=True（前端会显示「已退料」并禁按钮）")

    print("⑥ 护栏：找不到单 / 没有扣料记录")
    bad = call("POST", "/return_material?db=t041", {"ic_no": "ICNOPE9999", "wh": wh_dst})
    chk("找不到调拨单" in (bad.get("error") or ""), f"不存在的单被拒：{bad.get('error')}")
    no23 = call("POST", "/return_material?db=t041", {"ic_no": T31, "wh": wh_dst})
    chk("没有完工扣料记录" in (no23.get("error") or ""), f"无扣料记录被拒：{no23.get('error')}")
    nowh = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": "  "})
    chk("缺少调拨单号或退回仓库" in (nowh.get("error") or ""), f"空仓被拒：{nowh.get('error')}")

    print("⑦ C041 一行没动")
    ccur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)")
    chk(ccur.fetchone()[0] == c041_before, f"C041 IC 行数不变（{c041_before}）")
finally:
    n = cleanup()
    cur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK) WHERE IC_NO LIKE 'ICTESTR%%' OR IC_NO=%s", (RETURNED_NO or "",))
    left = cur.fetchone()[0]
    print("⑧ 清理")
    chk(left == 0, f"测试数据 0 残留（清了 {n} 行）")

print(f"\n===== 结果：{len(OK)} 通过 / {len(BAD)} 失败 =====")
for b in BAD:
    print("  ❌ " + b)
raise SystemExit(1 if BAD else 0)
