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
    for no in RETURNED_NOS:
        if no:
            cur.execute("DELETE FROM IC WHERE IC_NO = %s", (no,))
            n += cur.rowcount
    # 兜底：本测试单产生的退料单（哪怕没被记下来）也一起清（只动 T041）
    cur.execute("DELETE FROM IC WHERE IC_KND = 13 AND REM LIKE N'生产退料%' AND FLD1 = %s", (T30,))
    n += cur.rowcount
    for no in (CONFIRM_IN, CONFIRM_OUT, BATCH_IN):
        if no:
            cur.execute("DELETE FROM IC WHERE IC_NO = %s", (no,))
            n += cur.rowcount
    cur.execute("DELETE FROM IC WHERE IC_NO LIKE 'ICTESTR%'")
    n += cur.rowcount
    tc.commit()
    return n


RETURNED_NOS = []      # 按行退：一次测试可能写出多张退料单，逐个记着清
CONFIRM_IN = CONFIRM_OUT = None
BATCH_IN = None
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
    print("① 退料预览（按行：每行给已退量 / 可退量）")
    pv = call("GET", f"/return_preview?db=t041&ic_no={T30}")
    chk(pv.get("found") is True, "找得到该调拨单")
    chk(len(pv.get("items") or []) == 2, f"列出 2 个品号（实际 {len(pv.get('items') or [])}）")
    qs = sorted(round(i["qty"], 3) for i in pv.get("items") or [])
    chk(qs == [7, 11], f"扣料量 = 7 / 11（实际 {qs}）")
    chk(round(pv.get("total_qty") or 0, 3) == 18, f"扣料合计 18（实际 {pv.get('total_qty')}）")
    rem = sorted(round(i["remaining"], 3) for i in pv.get("items") or [])
    chk(rem == [7, 11], f"可退量 = 扣料 − 已退(0) = 7 / 11（实际 {rem}）")
    chk(all(round(i["returned_qty"], 3) == 0 for i in pv.get("items") or []), "每行已退量都是 0")
    chk(round(pv.get("remaining_total") or 0, 3) == 18, f"可退合计 18（实际 {pv.get('remaining_total')}）")
    chk(pv.get("default_wh") == wh_from, f"默认退回仓 = 原发出仓（调拨单 WH2）{wh_from}（实际 {pv.get('default_wh')}）")
    chk(pv.get("from_wh") == wh_from, f"原发出仓 = 调拨单 WH2 {wh_from}（实际 {pv.get('from_wh')}）")
    chk((pv.get("cus"), pv.get("ref"), pv.get("ddjh")) == ("TESTCUS", "9999", "TESTPLAN"), "客户/指令单/外发计划带过来")
    chk(pv.get("returned") is False and pv.get("partial") is False, "尚未退料（returned=False / partial=False）")

    print("② dry=1 不写库（按行：只退一行；不传 items = 还能退的全退）")
    cur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)"); n_before_dry = cur.fetchone()[0]
    d1 = call("POST", "/return_material?db=t041&dry=1", {"ic_no": T30, "wh": wh_dst,
                                                         "items": [{"prd_no": p1, "qty": 7}]})
    chk(d1.get("dry") is True and d1.get("rows") == 1 and round(d1.get("total_qty") or 0, 3) == 7,
        f"按行 dry：1 行 / 合计 7（实际 {d1.get('rows')} / {d1.get('total_qty')}）")
    d2 = call("POST", "/return_material?db=t041&dry=1", {"ic_no": T30, "wh": wh_dst})
    chk(d2.get("rows") == 2 and round(d2.get("total_qty") or 0, 3) == 18, "不传 items = 还能退的全退（2 行 / 18）")
    cur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)"); n_after_dry = cur.fetchone()[0]
    chk(n_before_dry == n_after_dry, f"dry 一行没写（{n_before_dry} → {n_after_dry}）")

    print("③ 护栏：数量 0 / 超可退量 / 陌生品号 / 空 items（全部不写库）")
    e0 = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst, "items": [{"prd_no": p1, "qty": 0}]})
    chk("必须大于 0" in (e0.get("error") or ""), f"数量 0 被拒：{e0.get('error')}")
    e_over = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst, "items": [{"prd_no": p1, "qty": 8}]})
    chk("超出可退量" in (e_over.get("error") or ""), f"超可退量被拒：{e_over.get('error')}")
    e_bad = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst, "items": [{"prd_no": "NOSUCH-01", "qty": 1}]})
    chk("不在该单据的完工扣料记录里" in (e_bad.get("error") or ""), f"陌生品号被拒：{e_bad.get('error')}")
    e_none = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst, "items": []})
    chk("没有勾选任何要退的行" in (e_none.get("error") or ""), f"空 items 被拒：{e_none.get('error')}")
    e_type = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst, "items": {"prd_no": p1}})
    chk("必须是数组" in (e_type.get("error") or ""), f"items 非数组被拒：{e_type.get('error')}")
    cur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)"); n_after_guards = cur.fetchone()[0]
    chk(n_before_dry == n_after_guards, f"五条护栏一行没写（{n_before_dry} → {n_after_guards}）")

    print("④ 按行退：只勾 p1 → 只写这一行")
    res = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst,
                                                    "items": [{"prd_no": p1, "qty": 7}]})
    chk(res.get("ok") is True, "写入成功")
    rno1 = res.get("ic_no")
    RETURNED_NOS.append(rno1)
    chk(bool(rno1), f"返回入库单号 {rno1}")
    chk(res.get("items") == 1 and round(res.get("total_qty") or 0, 3) == 7,
        f"1 行 / 合计 7（实际 {res.get('items')} / {res.get('total_qty')}）")

    print("⑤ 落库内容逐项核对（这批只该有 p1 一行）")
    cur.execute("""SELECT IC_NO,IC_KND,PRD_NO,QTY,UT,ISNULL(WH1,''),ISNULL(WH1NAME,''),ISNULL(WH2,''),
                          ISNULL(FLD1,''),ISNULL(REM,''),ITM,USABLE,ISNULL(指令单号,''),ISNULL(DDJH,''),ISNULL(客户,'')
                   FROM IC WITH(NOLOCK) WHERE IC_NO=%s ORDER BY ITM""", (rno1,))
    rows = cur.fetchall()
    chk(len(rows) == 1, f"入库单 {len(rows)} 行（只退勾中的那一行，不是整单）")
    chk(all(r[1] == 13 for r in rows), "KND=13（一张单一种 KND）")
    chk([int(r[10]) for r in rows] == [1], f"ITM 从 1 连续（实际 {[int(r[10]) for r in rows]}）")
    chk(bool(rows) and g(rows[0][2]) == p1 and float(rows[0][3]) == 7,
        f"只退了 p1 的 7（实际 {(g(rows[0][2]), float(rows[0][3])) if rows else '无行'}）")
    chk(all(g(r[5]) == wh_dst for r in rows), f"WH1 = 输入的退回仓 {wh_dst}")
    chk(all(g(r[7]) == "" for r in rows), "WH2 留空（纯入库单：不能同时带出库方向）")
    chk(all(g(r[8]) == T30 for r in rows), f"FLD1 = 调拨单号 {T30}（可反查）")
    chk(all(g(r[9]) == f"生产退料({T30})" for r in rows), "REM 标了生产退料+单号")
    chk(all(r[11] == 1 for r in rows), "USABLE=1")
    chk({(g(r[12]), g(r[13]), g(r[14])) for r in rows} == {("9999", "TESTPLAN", "TESTCUS")}, "客户/指令单/外发计划随单带过来")

    print("⑥ 部分退料：标记对得上 + 还能继续退剩下的行")
    pv2 = call("GET", f"/return_preview?db=t041&ic_no={T30}")
    chk(pv2.get("partial") is True and pv2.get("returned") is False, "预览：partial=True / returned=False（没全退完）")
    left = {i["prd_no"]: round(i["remaining"], 3) for i in pv2.get("items") or []}
    chk(left.get(p1) == 0 and left.get(p2) == 11, f"可退量：p1 已退完 0、p2 还剩 11（实际 {left}）")
    chk(round(pv2.get("remaining_total") or 0, 3) == 11, f"可退合计 11（实际 {pv2.get('remaining_total')}）")
    lst = call("GET", f"/transfer?db=t041&prd_no={p1}")
    hit = [i for i in (lst.get("items") or []) if i.get("ic_no") == T30]
    if not hit:
        print("     ↳ 调试：列表返回 =", json.dumps(lst, ensure_ascii=False)[:600])
    chk(bool(hit) and hit[0].get("returned") is False and round(hit[0].get("ret_qty") or 0, 3) == 7,
        "完工列表：returned=False、ret_qty=7（前端显示「部分退 7」+ 退料按钮）")
    res2 = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst, "items": [{"prd_no": p2, "qty": 5}]})
    chk(res2.get("ok") is True and round(res2.get("total_qty") or 0, 3) == 5, "第二批：只退 p2 的 5")
    RETURNED_NOS.append(res2.get("ic_no"))
    res3 = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst, "items": [{"prd_no": p2, "qty": 6}]})
    chk(res3.get("ok") is True and round(res3.get("total_qty") or 0, 3) == 6, "第三批：退 p2 剩下的 6")
    RETURNED_NOS.append(res3.get("ic_no"))
    pv3 = call("GET", f"/return_preview?db=t041&ic_no={T30}")
    chk(pv3.get("returned") is True and round(pv3.get("remaining_total") or 0, 3) == 0, "全部退完：returned=True / 可退 0")
    dup = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": wh_dst})
    chk("已全部退料" in (dup.get("error") or ""), f"退完后再退被拒：{dup.get('error')}")
    lst2 = call("GET", f"/transfer?db=t041&prd_no={p1}")
    hit2 = [i for i in (lst2.get("items") or []) if i.get("ic_no") == T30]
    chk(bool(hit2) and hit2[0].get("returned") is True, "完工列表：returned=True（前端显示「已退料」并锁按钮）")

    print("⑦ 护栏：找不到单 / 没有扣料记录 / 空仓")
    bad = call("POST", "/return_material?db=t041", {"ic_no": "ICNOPE9999", "wh": wh_dst})
    chk("找不到调拨单" in (bad.get("error") or ""), f"不存在的单被拒：{bad.get('error')}")
    no23 = call("POST", "/return_material?db=t041", {"ic_no": T31, "wh": wh_dst})
    chk("没有完工扣料记录" in (no23.get("error") or ""), f"无扣料记录被拒：{no23.get('error')}")
    nowh = call("POST", "/return_material?db=t041", {"ic_no": T30, "wh": "  "})
    chk("缺少调拨单号或退回仓库" in (nowh.get("error") or ""), f"空仓被拒：{nowh.get('error')}")

    print("⑨ 单笔完工的扣料行只写 WH2（一张单不能同时出入库）")
    cf = call("POST", "/confirm?db=t041", {
        "fg_no": p2, "fg_qty": 1, "fg_wh": wh_dst,
        "components": [{"prd_no": p1, "qty": 5, "transfer_ic_no": T30, "transfer_wh1": wh_from}]})
    if not cf.get("ic_out"):
        print("     ↳ 调试：confirm 返回 =", json.dumps(cf, ensure_ascii=False)[:500])
    if cf.get("error"):
        chk(False, f"单笔完工被拒：{cf.get('error')}")
    else:
        CONFIRM_IN, CONFIRM_OUT = cf.get("ic_in"), cf.get("ic_out")
        cur.execute("SELECT ISNULL(WH1,''), ISNULL(WH2,''), IC_KND FROM IC WITH(NOLOCK) WHERE IC_NO=%s", (CONFIRM_OUT,))
        out_rows = cur.fetchall()
        chk(bool(out_rows), f"写出出库单 {CONFIRM_OUT}")
        chk(all(int(r[2]) == 23 for r in out_rows), "出库单全是 KND=23")
        chk(all(g(r[0]) == "" for r in out_rows), f"WH1 留空（实际 {[g(r[0]) for r in out_rows]}）")
        chk(all(g(r[1]) == wh_from for r in out_rows), f"WH2 = 来源仓 {wh_from}")

    print("⑩ 批量完工的工具行不带 WH2（一张单不能同时出入库）")
    cur.execute("SELECT TOP 1 PRD_NO FROM PRDT WITH(NOLOCK) WHERE PRD_NO LIKE '03000%' ORDER BY PRD_NO")
    tr = cur.fetchone()
    tool_code = g(tr[0]) if tr else p1
    bt = call("POST", "/batch?db=t041", {
        "items": [{"fg_no": p2, "qty": 1, "fg_wh": wh_dst, "materials": []}],
        "tool_rows": [{"code": tool_code, "qty": 1}]})
    if bt.get("ic_in"):
        BATCH_IN = bt["ic_in"]
        cur.execute("""SELECT IC_KND, ISNULL(WH1,''), ISNULL(WH2,''), ISNULL(WH1NAME,''), ISNULL(WH2NAME,''), PRD_NO
                       FROM IC WITH(NOLOCK) WHERE IC_NO=%s AND IC_KND=30""", (BATCH_IN,))
        trows = cur.fetchall()
        chk(bool(trows), f"写出工具行（单 {BATCH_IN}，工具 {tool_code}）")
        chk(all(int(r[0]) == 30 for r in trows), "工具行 KND=30")
        chk(all(g(r[1]) == "G" for r in trows), f"工具行 WH1=G（实际 {[g(r[1]) for r in trows]}）")
        chk(all(g(r[2]) == "" for r in trows), f"工具行 WH2 留空（实际 {[g(r[2]) for r in trows]}）")
        chk(all(g(r[4]) == "" for r in trows), "工具行 WH2NAME 也留空")
    else:
        chk(False, f"批量完工被拒：{bt.get('error') or bt}")

    print("⑦ C041 一行没动")
    ccur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)")
    chk(ccur.fetchone()[0] == c041_before, f"C041 IC 行数不变（{c041_before}）")
finally:
    n = cleanup()
    _ph = ",".join(["%s"] * max(len(RETURNED_NOS), 1))
    cur.execute(f"SELECT COUNT(*) FROM IC WITH(NOLOCK) WHERE IC_NO LIKE 'ICTESTR%%' OR IC_NO IN ({_ph})",
                tuple(RETURNED_NOS or [""]))
    left = cur.fetchone()[0]
    print("⑧ 清理")
    chk(left == 0, f"测试数据 0 残留（清了 {n} 行）")

print(f"\n===== 结果：{len(OK)} 通过 / {len(BAD)} 失败 =====")
for b in BAD:
    print("  ❌ " + b)
raise SystemExit(1 if BAD else 0)
