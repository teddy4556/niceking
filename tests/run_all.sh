#!/usr/bin/env bash
# v6.28「节点名加地区（写入实测出口国家）」功能的全套测试。
#
# 三层各自验证不同的东西，缺一层就有一类缺陷漏过：
#   1. 单元测试   —— 回写函数的纯逻辑（匹配键、幂等、备份、转义）
#   2. 端到端     —— 用**上游真实产物**（app.js 真实代码生成的 Clash YAML）
#                    跑一遍，验「真实形态下改得中、YAML 不被破坏」
#   3. 浏览器回归 —— 真浏览器点按钮下载 BAT，验参数与可见性
#
# 判据用退出码，不用 grep 中文（shell 编码不可靠）。
#
# 用法: bash tests/run_all.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
FAIL=0
PASS=0

run_case() {
  local LABEL="$1"; shift
  local OUT RC LAST
  OUT="$("$@" 2>&1)"
  RC=$?
  LAST="$(printf '%s\n' "$OUT" | tail -1)"
  if [ "$RC" -eq 0 ]; then
    printf '  [OK]   %-28s %s\n' "$LABEL" "$LAST"
    PASS=$((PASS+1))
  else
    printf '  [FAIL] %-28s %s\n' "$LABEL" "$LAST"
    printf '%s\n' "$OUT" | grep -E '✗' | head -8 | sed 's/^/         /'
    FAIL=$((FAIL+1))
  fi
}

echo "=== 1) 单元测试：节点名回写纯逻辑 ==="
run_case "rewrite-names unit" python3 "$HERE/test_rewrite_names.py"

echo
echo "=== 2) 端到端：上游真实 Clash 产物 ==="
FIXTURE="/opt/data/cache/scratch/real-clash-fixture.yaml"
# 夹具必须由上游真实代码生成 —— 手编样本可能与真实产物不一致
# （真实产物里 server 字段是带引号的：server: "162.159.198.2"）
if [ ! -f "$FIXTURE" ]; then
  echo "  [生成] 真实 Clash 夹具（上游 app.js 真实代码）…"
  node "$HERE/gen_real_clash_fixture.mjs" "$FIXTURE" >/dev/null 2>&1
fi
if [ -f "$FIXTURE" ]; then
  run_case "rewrite-names e2e" python3 "$HERE/test_e2e_real_fixture.py" "$FIXTURE"
else
  printf '  [FAIL] %-28s 无法生成真实夹具\n' "rewrite-names e2e"
  FAIL=$((FAIL+1))
fi

echo
echo "=== 3) 浏览器回归：面板可见性 + BAT 参数 ==="
run_case "browser rewrite smoke" python3 "$HERE/browser_rewrite_smoke.py"

echo
echo "=== 4) 语法检查 ==="
for f in "$REPO/pages/app.js"; do
  if node --check "$f" 2>/dev/null; then
    printf '  [OK]   %-28s %s\n' "node --check $(basename "$f")" "OK"
    PASS=$((PASS+1))
  else
    printf '  [FAIL] %-28s %s\n' "node --check $(basename "$f")" "syntax error"
    FAIL=$((FAIL+1))
  fi
done
if python3 -c "import ast,sys; ast.parse(open('$REPO/pages/warp-egress-selector.py',encoding='utf-8').read())" 2>/dev/null; then
  printf '  [OK]   %-28s %s\n' "python syntax" "warp-egress-selector.py OK"
  PASS=$((PASS+1))
else
  printf '  [FAIL] %-28s %s\n' "python syntax" "warp-egress-selector.py 有语法错"
  FAIL=$((FAIL+1))
fi

echo
echo "=== result: $PASS passed / $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
