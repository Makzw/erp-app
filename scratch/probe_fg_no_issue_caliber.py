"""t_d339165a 只读对账：「入库未领料」口径 vs 真库 vs 接口返回值。

三件事，全 SELECT：
  ① 从 IC 自己重算一遍：入库(13)规范指令单号集合、领料(40/41/23)规范指令单号集合、差集
  ② 校验接口 /api/fg-no-issue 返回的桶与 ① 逐项一致（这才是硬不变式：口径一致）
  ③ 打印与 2026-10-12 基线（32 / 35 / 5）的对照 —— 数据每天在长（完工扣料 knd=23 由 app 写），
     数字漂了要看集合差在哪，不当失败

用法: python3 scratch/probe_fg_no_issue_caliber.py [--db c041] [--api http://127.0.0.1:8001]
"""
import json
import os
import sys
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")

BASELINE = {"in_orders": 32, "issue_orders": 35, "no_issue": ["1443", "1543", "1634", "7727", "7745"],
            "abnormal_in_rows": 387, "blank_in_rows": 5862}

NORM = ("LTRIM(RTRIM(ISNULL(指令单号,'')))<>'' AND 指令单号 NOT LIKE '%[^0-9]%' "
        "AND LEN(LTRIM(RTRIM(指令单号))) BETWEEN 4 AND 5")
ABN = ("LTRIM(RTRIM(ISNULL(指令单号,'')))<>'' AND (指令单号 LIKE '%[^0-9]%' "
       "OR LEN(LTRIM(RTRIM(指令单号))) NOT BETWEEN 4 AND 5)")


def load_env(path=ENV_PATH):
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main():
    db = "C041"
    api_base = "http://127.0.0.1:8001"
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--db":
            db = args[i + 1]
        if a == "--api":
            api_base = args[i + 1].rstrip("/")

    env = load_env()
    import pymssql
    conn = pymssql.connect(server=env["ERP_DB_HOST"], user=env["ERP_DB_USER"],
                           password=env["ERP_DB_PASSWORD"], database=db, charset="utf8")
    cur = conn.cursor()

    def q(sql):
        cur.execute(sql)
        return cur.fetchall() or []

    def orders(knds, cond):
        k = ",".join(str(x) for x in knds)
        return {str(r[0]).strip() for r in q(
            f"SELECT DISTINCT LTRIM(RTRIM(指令单号)) FROM IC WITH(NOLOCK) "
            f"WHERE IC_KND IN ({k}) AND {cond}")}

    fg = orders((13,), NORM)
    issue = orders((40, 41, 23), NORM)
    no_issue_sql = sorted(fg - issue)
    abn_in = q(f"SELECT COUNT(*) FROM IC WITH(NOLOCK) WHERE IC_KND=13 AND {ABN}")[0][0]
    blank_in = q("SELECT COUNT(*) FROM IC WITH(NOLOCK) WHERE IC_KND=13 "
                 "AND LTRIM(RTRIM(ISNULL(指令单号,'')))=''")[0][0]
    conn.close()

    fails, lines = [], []

    def check(cond, ok_text, fail_text):
        lines.append(("PASS " if cond else "FAIL ") + (ok_text if cond else fail_text))
        if not cond:
            fails.append(fail_text)

    # ── ① 重算 ────────────────────────────────────────────────────────────
    check(len(fg) > 0 and len(issue) > 0,
          f"① 重算完成：入库规范单号 {len(fg)} 个 / 领料规范单号 {len(issue)} 个 / 差集 {len(no_issue_sql)} 个",
          "① 重算异常：两侧至少有一侧为空")

    # ── ② 接口 vs SQL ─────────────────────────────────────────────────────
    try:
        with urllib.request.urlopen(f"{api_base}/api/fg-no-issue", timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        api_no_issue = sorted(x["order_no"] for x in data["no_issue"])
        api_done = sorted(x["order_no"] for x in data["done"])
        c = data["counts"]
        check(api_no_issue == no_issue_sql,
              f"② 接口「没领料」== SQL 差集（{len(api_no_issue)} 个）",
              f"② 接口与 SQL 不一致：接口 {api_no_issue} vs SQL {no_issue_sql}")
        check(set(api_no_issue) | set(api_done) == fg and not (set(api_no_issue) & set(api_done)),
              f"② 「没领料」+「已领料」= 入库规范单号全集（{len(fg)} 个），两桶不重叠",
              "② 两桶并集 ≠ 入库规范单号全集（有漏或重叠）")
        check(all(x["issue_rows"] == 0 for x in data["no_issue"])
              and all(x["issue_rows"] > 0 for x in data["done"]),
              "② 没领料的订单领料行数全为 0；已领料的全 > 0",
              "② 桶归类与行数矛盾（有订单领料行数对不上）")
        check(all(x["fg_rows"] > 0 for x in data["no_issue"] + data["done"]),
              "② 每个订单都带回入库行（可下钻）",
              "② 有订单没有入库行，下钻会空")
        check(c["in_orders"] == len(fg) and c["issue_orders"] == len(issue),
              f"② 基数一致：入库 {c['in_orders']} / 领料 {c['issue_orders']}",
              f"② 基数不一致：接口 {c['in_orders']}/{c['issue_orders']} vs SQL {len(fg)}/{len(issue)}")
        check(c["abnormal_in_rows"] == abn_in and c["blank_in_rows"] == blank_in,
              f"② 异常桶 {c['abnormal_in_rows']} 行 / 未填桶 {c['blank_in_rows']} 行 == SQL",
              f"② 异常/未填桶对不上：接口 {c['abnormal_in_rows']}/{c['blank_in_rows']} vs SQL {abn_in}/{blank_in}")
        check(len(data["abnormal"]) > 0 and all(a["rows"] > 0 for a in data["abnormal"]),
              f"② 异常桶明细 {len(data['abnormal'])} 组，行数合计 "
              f"{sum(a['rows'] for a in data['abnormal'])}",
              "② 异常桶明细为空或含 0 行组")
    except Exception as exc:                                    # noqa: BLE001
        fails.append(f"② 取接口失败：{exc!r}")
        lines.append("FAIL ② 取接口失败：" + repr(exc))

    # ── ③ 与基线对照（数据每天在长，漂移只提示不判红）────────────────────
    lines.append("")
    lines.append(f"③ 与 2026-10-12 基线对照：入库 {len(fg)}/{BASELINE['in_orders']} · "
                 f"领料 {len(issue)}/{BASELINE['issue_orders']} · "
                 f"没领料 {len(no_issue_sql)}/{len(BASELINE['no_issue'])} · "
                 f"异常 {abn_in}/{BASELINE['abnormal_in_rows']} 行 · "
                 f"未填 {blank_in}/{BASELINE['blank_in_rows']} 行")
    if no_issue_sql == BASELINE["no_issue"]:
        lines.append("   没领料订单与基线逐字一致：" + "、".join(no_issue_sql))
    else:
        gone = [x for x in BASELINE["no_issue"] if x not in no_issue_sql]
        new = [x for x in no_issue_sql if x not in BASELINE["no_issue"]]
        lines.append(f"   相对基线有漂移（数据在长，不算失败）：少 {gone} 多 {new}")
    if abn_in != BASELINE["abnormal_in_rows"] or blank_in != BASELINE["blank_in_rows"]:
        lines.append("   ⚠ 异常/未填行数也漂了 —— 口径没动，是新录入的行")
    else:
        lines.append("   异常桶 387 行 / 未填桶 5862 行 与基线一致")

    print("\n".join(lines))
    print("")
    print(f"rc={'1' if fails else '0'}  fails={len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
