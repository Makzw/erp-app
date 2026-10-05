"""静态自检：main.py 里每处 INSERT INTO IC 的「列数」必须等于「values 个数」。
这类错编译不报、只有真调用才炸（2026-10-05 单笔完工入库就是 20 列 21 值 → 接口恒 500）。

跑法：cd /home/Mak/erp-app && python3.11 check_ic_insert_counts.py
"""
import re
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "main.py"


def split_top(s):
    """按顶层逗号切分（跳过括号内的逗号，如 GETDATE() ）。"""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


def values_block(src, start):
    """从 start（VALUES 后的 '(' ）取出配平的括号内容。"""
    depth, i, begin = 0, start, 0
    while i < len(src):
        if src[i] == "(":
            depth += 1
            if depth == 1:
                begin = i + 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[begin:i]
        i += 1
    return ""


def main():
    src = open(SRC, encoding="utf-8").read()
    bad = 0
    checked = 0
    for m in re.finditer(r"INSERT\s+INTO\s+IC\s*\(", src, re.I):
        cols_open = m.end() - 1
        cols = values_block(src, cols_open)
        vm = re.search(r"VALUES\s*\(", src[cols_open:], re.I)
        if not vm:
            continue                      # INSERT INTO IC (...) SELECT ... 之类，跳过
        vals_start = cols_open + vm.end() - 1
        vals = values_block(src, vals_start)
        ncols, nvals = len(split_top(cols)), len(split_top(vals))
        line = src[:m.start()].count("\n") + 1
        ok = ncols == nvals
        checked += 1
        bad += 0 if ok else 1
        print(f"  {'✅' if ok else '❌'} main.py:{line:<6} 列 {ncols:>2} / 值 {nvals:>2}")
    print(f"\n===== 检查 {checked} 处 INSERT INTO IC：{bad} 处不匹配 =====")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
