"""自检：完工 tab 的 wh 参数 + 「查询途中连接被掐断」的重试策略。

单独跑（不需要 pytest）：    python3.11 check_db_blip_retry.py

覆盖 2026-10-05 修的两件事：
① /api/completion/transfer 带 wh 参数（修前：两个占位符只绑一个值 → 必 500）
② 客户库链路偶发「Unexpected EOF from the server」(pymssql 20017) → GET 重试一次；
   写接口（POST）绝不重试，真错误照旧 500。
全程只读 + 打桩：写接口那条分支在 get_conn 就抛，不会落任何数据。
"""
import sys

sys.path.insert(0, "/home/Mak/erp-app")

from starlette.testclient import TestClient  # noqa: E402

import main  # noqa: E402

OK, BAD = [], []


def chk(cond, msg):
    (OK if cond else BAD).append(msg)
    print(("  ✅ " if cond else "  ❌ ") + msg)


BLIP = "(20017, b'DB-Lib error message 20017, severity 9:\\nUnexpected EOF from the server\\n')"

real_get_conn = main.get_conn
client = TestClient(main.app, raise_server_exceptions=False)

print("① wh 参数（修前必 500）")
r = client.get("/api/completion/transfer?wh=8")
chk(r.status_code == 200, f"?wh=8 → HTTP {r.status_code}")
items = r.json().get("items", []) if r.status_code == 200 else []
chk(bool(items) and all(
    "8" in str(i.get("wh1", "")) or "8" in str(i.get("wh1_name", "")) for i in items
), f"wh=8 的 {len(items)} 行确实只属于该仓（真过滤，不只是不报错）")

r0 = client.get("/api/completion/transfer")
n0 = len(r0.json().get("items", [])) if r0.status_code == 200 else -1
chk(r0.status_code == 200 and n0 >= len(items), f"不带参数仍 200 且行数 {n0} ≥ {len(items)}（老行为不变）")

print("② 连接被掐断 → GET 重试一次")
calls = {"n": 0}


def flaky(*a, **k):
    calls["n"] += 1
    if calls["n"] == 1:
        raise RuntimeError(BLIP)
    return real_get_conn(*a, **k)


main.get_conn = flaky
r = client.get("/api/completion/transfer")
chk(r.status_code == 200, f"首次掐断后重试 → HTTP {r.status_code}")
chk(calls["n"] == 2, f"get_conn 被调用 {calls['n']} 次（应为 2：原请求 + 重试一次）")

print("③ 真错误：不重试、照旧 500")
calls["n"] = 0


def hard(*a, **k):
    calls["n"] += 1
    raise RuntimeError("列名 '简称' 无效")


main.get_conn = hard
r = client.get("/api/completion/transfer")
chk(r.status_code == 500, f"真错误 → HTTP {r.status_code}（应为 500）")
chk(calls["n"] == 1, f"只调用 {calls['n']} 次（不重试）")

print("④ 写接口绝不重试（同样的掐断）")
calls["n"] = 0


def blip_post(*a, **k):
    calls["n"] += 1
    raise RuntimeError(BLIP)


main.get_conn = blip_post
r = client.post("/api/stock/in", json=[{"prd_no": "__SELFCHECK__", "wh": "G", "qty": 1}])
chk(r.status_code == 500, f"POST + 掐断 → HTTP {r.status_code}（应为 500）")
chk(calls["n"] == 1, f"写接口只调用 {calls['n']} 次（绝不重试，避免重复写库）")

main.get_conn = real_get_conn
print(f"\n通过 {len(OK)} / 失败 {len(BAD)}")
sys.exit(1 if BAD else 0)
