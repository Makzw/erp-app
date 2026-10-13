"""t_d339165a 真机走查：「入库未领料」页在真 Chromium 里对不对（只读，不改数据）。

看四件事：
  ① tab 点得开、没有 console 报错
  ② 顶部数字 = 接口数字（没领料 / 已领料 / 入库单 / 领料覆盖 / 异常 / 未填）
  ③ 「没领料」节里的订单数 == 接口的没领料数，且每个都能下钻（≥1 入库行 + 明说领料「无」）
  ④ 「已领料」节里每个订单都能展开出领料行（带 KND 标签）
最后按手机宽度 375×667 截一张图留证。

用法: python3 scratch/verify_fg_no_issue_page.py [--url http://127.0.0.1:8001] [--shot x.png]
"""
import json
import os
import sys
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SHOT = os.path.join(BASE_DIR, "scratch", "fg_no_issue_375x667.png")


def main():
    url = "http://127.0.0.1:8001"
    shot = DEFAULT_SHOT
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--url":
            url = args[i + 1].rstrip("/")
        if a == "--shot":
            shot = args[i + 1]

    with urllib.request.urlopen(f"{url}/api/fg-no-issue", timeout=60) as r:
        api = json.loads(r.read().decode("utf-8"))
    c = api["counts"]
    want_no_issue = sorted(x["order_no"] for x in api["no_issue"])

    from playwright.sync_api import sync_playwright

    fails, lines = [], []

    def check(cond, ok, bad):
        lines.append(("PASS " if cond else "FAIL ") + (ok if cond else bad))
        if not cond:
            fails.append(bad)

    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        pg = br.new_page(viewport={"width": 375, "height": 667})
        errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(url + "/", wait_until="load")
        pg.click('.tab-btn[data-tab="fg-no-issue"]')
        pg.wait_for_selector(".fni-chip", timeout=30000)

        chips = [e.strip() for e in pg.eval_on_selector_all(".fni-chip", "els=>els.map(e=>e.textContent)")]
        heads = [e.strip() for e in pg.eval_on_selector_all(".fni-sec-head", "els=>els.map(e=>e.textContent)")]
        blocks = pg.eval_on_selector_all(".fni-order", """els => els.map(e => ({
            order_no: e.querySelector('.fni-order-no').textContent.trim(),
            tags: [...e.querySelectorAll('summary .fni-tag')].map(t=>t.textContent.trim()),
            fg_lines: [...e.querySelectorAll('.fni-drill')].flatMap(d =>
                [...d.children].length ? (() => {
                    const out=[]; let inFg=false, inIssue=false;
                    for (const ch of d.children) {
                        if (ch.classList.contains('fni-drill-title')) {
                            inFg = ch.textContent.includes('成品入库单');
                            inIssue = ch.textContent.includes('领料记录');
                            continue;
                        }
                        if (ch.classList.contains('fni-line')) { if (inFg) out.push({k:'fg', t:ch.textContent.trim()}); }
                    }
                    return out;
                })() : []
            ),
            issue_lines: [...e.querySelectorAll('.fni-line')].map(x=>x.textContent.trim()),
            none_text: [...e.querySelectorAll('.fni-none')].map(x=>x.textContent.trim()),
        }))""")

        # 分节归属：按 DOM 顺序把 .fni-order 归到最近的 .fni-sec-head
        sec_of = pg.evaluate("""() => {
            const out=[]; let cur=null;
            for (const el of document.getElementById('content').children) {
                if (el.classList.contains('fni-sec-head')) cur = el.textContent.trim();
                else if (el.classList.contains('fni-order')) out.push([el.querySelector('.fni-order-no').textContent.trim(), cur]);
            }
            const m={}; for (const [n,s] of out) { (m[s]=m[s]||[]).push(n); }
            return m;
        }""")
        abn = pg.eval_on_selector_all(".fni-abn", "els=>els.map(e=>e.textContent.trim())")
        note = pg.eval_on_selector(".fni-note", "e=>e.textContent") if pg.query_selector(".fni-note") else ""
        pg.screenshot(path=shot, full_page=True)
        br.close()

    no_issue_sec = next((k for k in sec_of if k.startswith("没领料")), None)
    done_sec = next((k for k in sec_of if k.startswith("已领料")), None)
    page_no_issue = sorted(sec_of.get(no_issue_sec, []))
    page_done = sorted(sec_of.get(done_sec, []))

    check(not errs, "① 无 console / page 报错", "① 有报错：" + "; ".join(errs[:3]))
    check(any(h.startswith("没领料") for h in heads),
          "① 三个分节都在：" + " | ".join(heads), "① 分节标题缺失：" + str(heads))
    # ② 顶部数字
    def chip_has(text):
        return any(text in x for x in chips)

    check(chip_has(f"没领料 {c['no_issue_orders']} 个订单"),
          f"② 顶部「没领料 {c['no_issue_orders']} 个订单」", "② 顶部没领料数与接口不符：" + str(chips))
    check(chip_has(f"已领料 {c['done_orders']} 个订单"),
          f"② 顶部「已领料 {c['done_orders']} 个订单」", "② 顶部已领料数与接口不符")
    check(chip_has(f"成品入库单 {c['in_orders']} 个"),
          f"② 顶部「成品入库单 {c['in_orders']} 个 / {c['in_rows']} 行」", "② 顶部入库单基数不符")
    check(chip_has(f"领料覆盖 {c['issue_orders']} 个"),
          f"② 顶部「领料覆盖 {c['issue_orders']} 个 / {c['issue_rows']} 行」", "② 顶部领料覆盖不符")
    check(chip_has(f"指令单号异常 {c['abnormal_rows']} 行"),
          f"② 顶部「指令单号异常 {c['abnormal_rows']} 行」", "② 顶部异常行数不符")
    check(chip_has(f"未填指令单号 {c['blank_in_rows']} 行（无法判定）"),
          f"② 顶部「未填指令单号 {c['blank_in_rows']} 行（无法判定）」", "② 顶部未填行数不符")
    # ③ 没领料节
    check(page_no_issue == want_no_issue,
          f"③ 「没领料」节 {len(page_no_issue)} 个订单，逐字等于接口：{'、'.join(page_no_issue)}",
          f"③ 没领料节与接口不一致：页面 {page_no_issue} vs 接口 {want_no_issue}")
    by_no = {b["order_no"]: b for b in blocks}
    bad_drill = [o for o in page_no_issue
                 if not by_no.get(o) or not by_no[o]["fg_lines"]
                 or not any("无" in t for t in by_no[o]["none_text"])]
    check(not bad_drill,
          "③ 每个没领料订单都能下钻：≥1 条入库行 + 明说领料「无（40/41/23 里一条都没找到）」",
          f"③ 这些订单下钻不全：{bad_drill}")
    check(all(any("领料：无" in t for t in by_no.get(o, {}).get("tags", [])) for o in page_no_issue),
          "③ 每个没领料订单的摘要都标「领料：无」", "③ 摘要标签缺「领料：无」")
    # ④ 已领料节
    bad_done = [o for o in page_done
                if not by_no.get(o) or not any("[" in t for t in by_no[o]["issue_lines"])]
    check(not bad_done,
          f"④ 「已领料」节 {len(page_done)} 个订单，每个都能展开出带 KND 标签的领料行",
          f"④ 这些已领料订单没展开出领料行：{bad_done}")
    # ⑤ 异常桶 + 说明
    check(len(abn) > 0 and any("V2负库存清零" in x for x in abn),
          f"⑤ 异常桶列出 {len(abn)} 组（含历史盘点单 V2负库存清零）", "⑤ 异常桶没列出来")
    check("不进「没领料」计数" in note and "5862" in note,
          "⑤ 页面明说未填指令单号 5862 行「不进没领料计数」", "⑤ 页面缺「未填不进计数」的说明")

    print("\n".join(lines))
    print(f"\n截图: {shot}")
    print(f"rc={'1' if fails else '0'}  fails={len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
