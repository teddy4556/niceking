#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证：用**上游真实产物**（app.js 真实代码生成的 Clash YAML）跑回写。

为什么必须这一层：单元测试用的是手编夹具，可能与上游真实产物有出入。
上游真实产物里有个关键差异 —— `server` 字段是**带引号**的：
    server: "162.159.198.2"
而手编夹具往往写成 `server: 162.159.198.2`。
匹配逻辑若没处理引号，在真实产物上就改不中。

跑法：python3 tests/test_e2e_real_fixture.py <真实夹具.yaml>
"""
import importlib.util
import os
import subprocess
import sys
import tempfile

本目录 = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(本目录, "..", "pages", "warp-egress-selector.py")
spec = importlib.util.spec_from_file_location("wes", SCRIPT)
wes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wes)

PASS = 0
FAIL = 0


def 断言(cond, 说明, 详情=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ " + 说明)
    else:
        FAIL += 1
        print("  ✗ " + 说明 + ("" if 详情 is None else "  → " + repr(详情)))


夹具 = sys.argv[1] if len(sys.argv) > 1 else "/opt/data/cache/scratch/real-clash-fixture.yaml"
if not os.path.exists(夹具):
    print("缺少真实夹具 %s —— 先跑 node tests/gen_real_clash_fixture.mjs" % 夹具)
    sys.exit(2)

原文 = open(夹具, encoding="utf-8").read()

print("\n=== 端到端：真实 Clash 产物上的节点名回写 ===\n")

# 夹具自检：确认它真的是上游形态（server 带引号）
断言('server: "162.159.198.2"' in 原文,
      "夹具是上游真实形态（server 字段带引号）")
断言('  - name: "CDN | 162.159.198.2 | 443"' in 原文,
      "夹具节点名是上游 detail 模式形态")

# 造检测结果（模拟脚本实测出来的 rows）
rows = [
    {"node": "CDN | 162.159.198.2 | 443", "country": "US",
     "server": "162.159.198.2", "port": 443, "ip": "1.2.3.4"},
    {"node": "CDN | 104.16.0.1 | 8443", "country": "JP",
     "server": "104.16.0.1", "port": 8443, "ip": "5.6.7.8"},
    {"node": "CDN | 172.64.0.5 | 2053", "country": "",
     "server": "172.64.0.5", "port": 2053, "ip": ""},
]

fd, path = tempfile.mkstemp(suffix=".yaml")
os.close(fd)
open(path, "w", encoding="utf-8").write(原文)

changed, unmatched, report = wes.rewrite_names_with_country(path, rows)
新 = open(path, encoding="utf-8").read()

断言(changed == 2, "★ 真实产物上改写了 2 个节点（第三个探测失败不改）", changed)
断言(unmatched == 1, "探测失败的那个计为未命中", unmatched)
断言('  - name: "🇺🇸 US | CDN | 162.159.198.2 | 443"' in 新,
      "★ US 节点名正确回写（server 带引号也能匹配）")
断言('  - name: "🇯🇵 JP | CDN | 104.16.0.1 | 8443"' in 新,
      "★ JP 节点名正确回写")
断言('  - name: "CDN | 172.64.0.5 | 2053"' in 新,
      "★ 探测失败的节点名保持原样")
断言('    server: "162.159.198.2"' in 新, "server 字段（含引号）没被改")
断言('    private-key: "FAKEPRIVATEKEYBASE64=="' in 新, "private-key 没被改")
断言('    sni: "speed.cloudflare.com"' in 新, "sni 没被改")
断言('      mode: ipv4' in 新, "嵌套字段没被改（缩进层级判断正确）")

# 备份正确性
备份 = open(path + ".bak", encoding="utf-8").read()
断言(备份 == 原文, "★ 备份与原文逐字节一致")

# 回写后的 YAML 仍是合法 YAML（用 python yaml 验；没有 yaml 模块就跳过）
try:
    import yaml as _yaml
    解析 = _yaml.safe_load(新)
    断言(isinstance(解析, dict) and "proxies" in 解析, "★ 回写后 YAML 仍可被解析")
    代理 = 解析.get("proxies") or []
    断言(len(代理) == 3, "proxies 仍是 3 个节点", len(代理))
    断言(代理[0]["name"].startswith("🇺🇸 US"), "解析回来的节点名带国家", 代理[0].get("name"))
    断言(代理[0]["server"] == "162.159.198.2", "server 值正确", 代理[0].get("server"))
    断言(代理[0]["port"] == 443, "port 是数字", 代理[0].get("port"))
except ImportError:
    print("  [跳过] 本机没有 PyYAML，跳过 YAML 解析校验")

os.unlink(path)
os.unlink(path + ".bak")

print("\n=== 结果：%d 通过 / %d 失败 ===\n" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
