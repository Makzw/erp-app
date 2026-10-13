"""验收脚本：采购「到交期未到」看板 GET /api/po-late（只读，走真 HTTP）。

用法（app 在跑的时候）：
    cd ~/erp-app && python3 check_po_late.py                       # 默认 http://127.0.0.1:8001
    python3 check_po_late.py --base http://127.0.0.1:8011

验的是**口径**（数字漂了 = 口径被改坏），不是实现细节：
  · 卡面基线（基准日 2026-10-12）：176 行 / 未到 307,522 件，未结案采购行 1150
  · 未到齐含未到期 330 行；分档合计 = 逾期行数（176）
  · 未到期行 late_days ≤ 0、逾期行 ≥ 1；分档边界 1-7 / 8-30 / >30
  · 卡面 Top4 逐行对账（单号 / 行号 / 品号 / 订购 / 已到 / 未到 / 交期 / 逾期天数）
  · 基准日参数校验：非法值不许 500，要明说错误

口径（MAK 2026-10-12 拍板，kanban t_5f8ef09b）见 main.py 的 list_po_late 注释：
已入库 = PSS(PS_ID='PS'、USABLE=1、CLS_ID=0、入库日<基准日) 按 REF_ITM 求和；
未到 = 订购 − 已入库（只列 >0）；到交期 = 交期<基准日；逾期天数 = 基准日 − 交期。
"""
import argparse
import json
import sys
import urllib.request

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""))
    if not cond:
        FAILS.append(name)


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=60) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    st, d = get(base, "/api/po-late?as_of=2026-10-12")
    check("卡面基线：到交期未到 = 176 行", d["late"]["rows"] == 176, str(d["late"]))
    check("卡面基线：未到合计 = 307,522 件", d["late"]["qty"] == 307522.0, str(d["late"]["qty"]))
    check("未结案采购行 = 1150", d["po_lines"] == 1150, str(d["po_lines"]))
    check("未到齐（含未到期）= 330 行", len(d["items"]) == 330, str(len(d["items"])))

    late = [i for i in d["items"] if i["bucket"].startswith("late_")]
    check("逐行 late_days 统计 = 逾期行数", len(late) == 176, "len=%d" % len(late))
    check("未到合计 = 各行 todo_qty 之和",
          round(sum(i["todo_qty"] for i in late), 3) == 307522.0)
    check("分档合计 = 逾期行数",
          sum(b["rows"] for b in d["buckets"] if b["key"].startswith("late_")) == 176,
          str([(b["key"], b["rows"]) for b in d["buckets"]]))
    check("未到期行 late_days ≤ 0", all(i["late_days"] <= 0 for i in d["items"] if i["bucket"] == "not_due"))
    check("逾期行 late_days ≥ 1", all(i["late_days"] >= 1 for i in late))
    check("分档边界 1-7 / 8-30 / >30",
          all(1 <= i["late_days"] <= 7 for i in d["items"] if i["bucket"] == "late_1_7")
          and all(8 <= i["late_days"] <= 30 for i in d["items"] if i["bucket"] == "late_8_30")
          and all(i["late_days"] > 30 for i in d["items"] if i["bucket"] == "late_31p"))

    idx = {(i["os_no"], i["itm"]): i for i in d["items"]}
    for k, w in {("PO26070010", 8): ("WB-11392-TM", 250.0, 52.0, 198.0, "2026-07-20", 84),
                 ("PO26070010", 25): ("WB-11440-TM", 334.0, 84.0, 250.0, "2026-07-20", 84),
                 ("PO26070022", 1): ("M20336-01-03", 5000.0, 4830.0, 170.0, "2026-07-23", 81),
                 ("PO26070028", 6): ("RM01C98.51.8", 88.0, 86.0, 2.0, "2026-07-18", 86)}.items():
        it = idx.get(k)
        got = (it["prd_no"], it["qty"], it["in_qty"], it["todo_qty"], it["est_dd"], it["late_days"]) if it else None
        check("卡面 Top 对账 %s 行%s" % k, got == w, "want=%s got=%s" % (w, got))

    for bad in ("2026-10-99", "2026-13-01", "abc", "2026/10/12"):
        st, e = get(base, "/api/po-late?as_of=" + bad)
        check("非法基准日 %s → 明说错误（不 500）" % bad,
              st == 200 and isinstance(e.get("error"), str), str(e.get("error"))[:50])

    st, t = get(base, "/api/po-late")
    check("不传基准日 → 用今天", bool(t.get("as_of")) and t["late"]["rows"] >= 0, "as_of=%s" % t.get("as_of"))

    print("\n%s（失败 %d）" % ("全部通过" if not FAILS else "有失败：" + ", ".join(FAILS), len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
