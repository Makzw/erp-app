"""形态转换「一张单一种 KND」真机验证（只写 T041 测试库，C041 一行不碰，测完按单号精确清理）。

跑法：cd /home/Mak/erp-app && set -a && . ./.env && set +a && python3.11 <本文件>
"""
import json
import os
import urllib.request

import pymssql

HOST, USER, PWD = os.environ["ERP_DB_HOST"], os.environ["ERP_DB_USER"], os.environ["ERP_DB_PASSWORD"]
API = "http://127.0.0.1:8001/api/stock/shape_convert?db=t041"
OK, BAD = [], []


def chk(cond, msg):
    (OK if cond else BAD).append(msg)
    print(("  ✅ " if cond else "  ❌ ") + msg)


def conn(db):
    return pymssql.connect(server=HOST, user=USER, password=PWD, database=db, charset="utf8")


# ── 取测试料：T041.PRDT 里现成的品号 + 一个现成仓库 ─────────────────────────
tc = conn("T041")
cur = tc.cursor()
cur.execute("SELECT TOP 2 PRD_NO, NAME, ISNULL(UT,'PCE') FROM PRDT WITH(NOLOCK) WHERE LEN(PRD_NO)>4 ORDER BY PRD_NO")
prds = cur.fetchall()
cur.execute("SELECT TOP 1 WH, NAME FROM MY_WH WITH(NOLOCK) ORDER BY WH")
wh_row = cur.fetchone()
if len(prds) < 2 or not wh_row:
    print("  ⚠ T041 里料/仓不足，测试无法进行"); raise SystemExit(1)
(src1, n1, ut1), (tgt1, n2, ut2) = prds[0], prds[1]
wh = wh_row[0]
print(f"  测试料：源 {src1} / 目标 {tgt1}；仓库 {wh}")

cc = conn("C041")
ccur = cc.cursor()
ccur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)")
c041_before = ccur.fetchone()[0]

# ── 提交：2 出 + 1 入（数量手填、故意不等）───────────────────────────────
body = {"sources": [{"prd_no": src1, "wh": wh, "qty": 7}, {"prd_no": tgt1, "wh": wh, "qty": 3}],
        "targets": [{"prd_no": tgt1, "wh": wh, "qty": 9}, {"prd_no": src1, "wh": wh, "qty": 4}],
        "rem": "__SHAPETEST__ 两张单验证"}
req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
res = json.loads(urllib.request.urlopen(req, timeout=180).read().decode())

print("① 返回两张单号")
print("   ", {k: res.get(k) for k in ("ok", "ic_no_out", "ic_no_in", "count")})
out_no, in_no = res.get("ic_no_out"), res.get("ic_no_in")
chk(res.get("ok") is True, "提交成功")
chk(bool(out_no) and bool(in_no) and out_no != in_no, f"出库单/入库单是两个不同单号：{out_no} / {in_no}")

print("② 每张单只有一种 KND")
cur.execute("""SELECT IC_NO, COUNT(DISTINCT IC_KND) k, MIN(IC_KND) mk, COUNT(*) n
               FROM IC WITH(NOLOCK) WHERE IC_NO IN (%s,%s) GROUP BY IC_NO ORDER BY IC_NO""", (out_no, in_no))
doc_rows = cur.fetchall()
for r in doc_rows:
    print(f"    {r[0]}  KND种数={r[1]}  KND={r[2]}  行数={r[3]}")
chk(len(doc_rows) == 2, "两张单都写出来了")
chk(all(r[1] == 1 for r in doc_rows), "每张单 KND 种数都是 1（一张单一种 KND）")
knd_by_no = {r[0]: r[2] for r in doc_rows}
chk(knd_by_no.get(out_no) == 23, f"出库单 {out_no} 是 KND=23")
chk(knd_by_no.get(in_no) == 13, f"入库单 {in_no} 是 KND=13")

print("③ 行归属 / 仓库 / 数量 / 交叉关联")
cur.execute("""SELECT IC_NO, IC_KND, PRD_NO, QTY, ISNULL(WH1,'') , ISNULL(WH2,''), ISNULL(FLD1,''),
                      ITM, ISNULL(USR,''), ISNULL(REM,'')
               FROM IC WITH(NOLOCK) WHERE IC_NO IN (%s,%s) ORDER BY IC_NO, ITM""", (out_no, in_no))
rows = cur.fetchall()
for r in rows:
    print(f"    {r[0]} KND={r[1]} {r[2]:<16} qty={float(r[3]):>6.0f} WH1={r[4]:<4} WH2={r[5]:<4} FLD1={r[6]} ITM={r[7]}")
out_rows = [r for r in rows if r[0] == out_no]
in_rows = [r for r in rows if r[0] == in_no]
chk(len(out_rows) == 2 and len(in_rows) == 2, f"出库单 {len(out_rows)} 行 / 入库单 {len(in_rows)} 行（=提交的 2 出 2 入）")
chk(all(r[5] == wh and r[4] == "" for r in out_rows), "出库行：WH2=源仓、WH1 空")
chk(all(r[4] == wh and r[5] == "" for r in in_rows), "入库行：WH1=目标仓、WH2 空")
chk(sorted(float(r[3]) for r in out_rows) == [3.0, 7.0], "出库数量按手填原样写入（3 / 7，不互相抵消）")
chk([r[7] for r in out_rows] == [1, 2], "出库单 ITM 从 1 连续")
chk([r[7] for r in in_rows] == [1, 2], "入库单 ITM 也从 1 连续（不是接着出库单往下排）")
chk(all(r[6] == in_no for r in out_rows), f"出库行 FLD1 = 入库单号（{in_no}）")
chk(all(r[6] == out_no for r in in_rows), f"入库行 FLD1 = 出库单号（{out_no}）")
chk(all(r[9] == body["rem"] for r in rows), "备注按提交写入")

print("④ C041 一行没动")
ccur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)")
c041_after = ccur.fetchone()[0]
chk(c041_before == c041_after, f"C041.IC 行数 {c041_before} → {c041_after}（不变）")

print("⑤ 按单号精确清理 T041")
cur.execute("DELETE FROM IC WHERE IC_NO IN (%s,%s)", (out_no, in_no))
tc.commit()
cur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK) WHERE IC_NO IN (%s,%s)", (out_no, in_no))
chk(cur.fetchone()[0] == 0, f"清理后 {out_no} / {in_no} 在 T041 里 0 行")
ccur.execute("SELECT COUNT(*) FROM IC WITH(NOLOCK)")
chk(ccur.fetchone()[0] == c041_before, "清理动作没碰 C041")

tc.close(); cc.close()
print(f"\n通过 {len(OK)} / 失败 {len(BAD)}")
raise SystemExit(1 if BAD else 0)
