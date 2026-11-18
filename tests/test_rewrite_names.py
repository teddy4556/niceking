#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""warp-egress-selector.py 的「节点名回写」功能单测。

判据来源：面板 app.js 的 makeNode() 真实生成的 YAML 形态（不是手编样本）——
  - name: "CDN | 162.159.198.2 | 443"
  type: masque
  server: 162.159.198.2
  port: 443

为什么用真实形态做夹具：
  回写逻辑是**纯文本行扫描**，它的正确性完全取决于「行长什么样」。
  手编的样本可能和上游真实产物不一致 → 测试全绿但线上改不中。

跑法：python3 tests/test_rewrite_names.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pages"))
import importlib.util

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pages", "warp-egress-selector.py")
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


# ── 夹具：照上游 app.js makeNode() 的真实输出形态 ──────────────────
# app.js 生成的是：
#   proxies:
#     - name: "CDN | 162.159.198.2 | 443"
#       type: masque
#       server: 162.159.198.2
#       port: 443
#       private-key: "xxx"
#       ...
def 造配置(节点列表, 额外头部="proxies:\n"):
    out = [额外头部]
    for name, server, port in 节点列表:
        out.append('  - name: "%s"' % name)
        out.append("    type: masque")
        out.append("    server: %s" % server)
        out.append("    port: %d" % port)
        out.append('    private-key: "FAKEKEY"')
        out.append("    sni: speed.cloudflare.com")
    return "\n".join(out) + "\n"


def 写临时(内容):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(内容)
    return path


print("\n=== warp-egress-selector 回写节点名 单测 ===\n")

# ── 1. 基本回写 ──────────────────────────────────────────────
def test_1():
        cfg = 造配置([
            ("CDN | 162.159.198.2 | 443", "162.159.198.2", 443),
            ("CDN | 104.16.0.1 | 8443", "104.16.0.1", 8443),
        ])
        path = 写临时(cfg)
        rows = [
            {"node": "CDN | 162.159.198.2 | 443", "country": "US", "server": "162.159.198.2", "port": 443},
            {"node": "CDN | 104.16.0.1 | 8443", "country": "JP", "server": "104.16.0.1", "port": 8443},
        ]
        changed, unmatched, report = wes.rewrite_names_with_country(path, rows)
        内容 = open(path, encoding="utf-8").read()
        断言(changed == 2, "两个节点都改写了", changed)
        断言(unmatched == 0, "没有未命中", unmatched)
        断言("🇺🇸 US | CDN | 162.159.198.2 | 443" in 内容, "US 节点名带上了国旗+国家码")
        断言("🇯🇵 JP | CDN | 104.16.0.1 | 8443" in 内容, "JP 节点名带上了国旗+国家码")
        断言("server: 162.159.198.2" in 内容, "server 字段没被改动")
        断言("port: 443" in 内容, "port 字段没被改动")
        断言("private-key: \"FAKEKEY\"" in 内容, "其它字段（private-key）没被改动")
        os.unlink(path)
        if os.path.exists(path + ".bak"):
            os.unlink(path + ".bak")


# ── 2. 备份存的是**改写前**的内容 ──────────────────────────────
def test_2():
        cfg = 造配置([("CDN | 162.159.198.2 | 443", "162.159.198.2", 443)])
        path = 写临时(cfg)
        rows = [{"node": "x", "country": "US", "server": "162.159.198.2", "port": 443}]
        wes.rewrite_names_with_country(path, rows)
        断言(os.path.exists(path + ".bak"), "改写后生成了 .bak 备份")
        备份 = open(path + ".bak", encoding="utf-8").read()
        断言("🇺🇸 US" not in 备份, "★ 备份里是**改写前**的内容（顺序反了 = 备份没意义）")
        断言("CDN | 162.159.198.2 | 443" in 备份, "备份里有原始节点名")
        os.unlink(path)
        os.unlink(path + ".bak")


# ── 3. 幂等：重复跑不会变成 "🇺🇸 US 🇺🇸 US ..." ───────────────
def test_3():
        cfg = 造配置([("CDN | 162.159.198.2 | 443", "162.159.198.2", 443)])
        path = 写临时(cfg)
        rows = [{"node": "x", "country": "US", "server": "162.159.198.2", "port": 443}]
        wes.rewrite_names_with_country(path, rows)
        第二次 = open(path, encoding="utf-8").read()
        changed2, _, _ = wes.rewrite_names_with_country(path, rows)
        内容2 = open(path, encoding="utf-8").read()
        断言(changed2 == 0, "★ 第二次跑不改写（幂等）", changed2)
        断言(内容2 == 第二次, "★ 第二次跑内容不变（不会叠加前缀）")
        断言(内容2.count("🇺🇸 US") == 1, "★ 国旗前缀只出现一次", 内容2.count("🇺🇸 US"))
        os.unlink(path)
        os.unlink(path + ".bak")


# ── 4. 未命中：检测失败的节点保持原样 ──────────────────────────
def test_4():
        cfg = 造配置([
            ("CDN | 162.159.198.2 | 443", "162.159.198.2", 443),
            ("CDN | 1.2.3.4 | 443", "1.2.3.4", 443),
        ])
        path = 写临时(cfg)
        rows = [{"node": "x", "country": "US", "server": "162.159.198.2", "port": 443}]
        changed, unmatched, report = wes.rewrite_names_with_country(path, rows)
        内容 = open(path, encoding="utf-8").read()
        断言(changed == 1, "只改写命中的那个", changed)
        断言(unmatched == 1, "未命中的被计数", unmatched)
        断言('  - name: "CDN | 1.2.3.4 | 443"' in 内容, "★ 未命中节点的**原名保持不变**（不能瞎标国家）")
        os.unlink(path)
        os.unlink(path + ".bak")


# ── 5. 空 country 不写（探测失败的国家是空串）─────────────────
def test_5():
        cfg = 造配置([("CDN | 162.159.198.2 | 443", "162.159.198.2", 443)])
        path = 写临时(cfg)
        rows = [{"node": "x", "country": "", "server": "162.159.198.2", "port": 443}]
        changed, unmatched, _ = wes.rewrite_names_with_country(path, rows)
        内容 = open(path, encoding="utf-8").read()
        断言(changed == 0, "country 为空 → 不改写", changed)
        断言("🌐" not in 内容, "★ 不会写入 🌐 假国家")
        os.unlink(path)


# ── 6. 按 server:port 匹配，不按节点名 ─────────────────────────
def test_6():
        # 用户手改过节点名 → 名字对不上，但 server:port 没变 → 仍要改对
        cfg = 造配置([("我改过的名字", "162.159.198.2", 443)])
        path = 写临时(cfg)
        rows = [{"node": "CDN | 162.159.198.2 | 443", "country": "US", "server": "162.159.198.2", "port": 443}]
        changed, _, _ = wes.rewrite_names_with_country(path, rows)
        内容 = open(path, encoding="utf-8").read()
        断言(changed == 1, "★ 节点名被手改过，仍按 server:port 命中", changed)
        断言("🇺🇸 US | 我改过的名字" in 内容, "改的是手改后的那个名字")
        os.unlink(path)
        os.unlink(path + ".bak")


# ── 7. dry-run 不写文件 ──────────────────────────────────────
def test_7():
        cfg = 造配置([("CDN | 162.159.198.2 | 443", "162.159.198.2", 443)])
        path = 写临时(cfg)
        rows = [{"node": "x", "country": "US", "server": "162.159.198.2", "port": 443}]
        changed, _, _ = wes.rewrite_names_with_country(path, rows, dry_run=True)
        内容 = open(path, encoding="utf-8").read()
        断言(changed == 1, "dry-run 也报告改写数量", changed)
        断言(内容 == cfg, "★ dry-run 不动文件")
        断言(not os.path.exists(path + ".bak"), "dry-run 不生成备份")
        os.unlink(path)


# ── 8. 前缀格式三种 ──────────────────────────────────────────
def test_8():
        for fmt, 期望 in [("flag", "🇺🇸 US | "), ("name", "🇺🇸 美国 | "), ("code", "US | ")]:
            cfg = 造配置([("CDN | 162.159.198.2 | 443", "162.159.198.2", 443)])
            path = 写临时(cfg)
            rows = [{"node": "x", "country": "US", "server": "162.159.198.2", "port": 443}]
            wes.rewrite_names_with_country(path, rows, prefix_format=fmt)
            内容 = open(path, encoding="utf-8").read()
            断言(期望 in 内容, "前缀格式 %s 生效" % fmt, 内容)
            os.unlink(path)
            os.unlink(path + ".bak")


# ── 9. 配置里没有 name 的节点（策略组等）不受影响 ──────────────
def test_9():
        cfg = ("proxies:\n"
               "  - name: \"CDN | 162.159.198.2 | 443\"\n"
               "    type: masque\n"
               "    server: 162.159.198.2\n"
               "    port: 443\n"
               "proxy-groups:\n"
               "  - name: \"🚀 WARP自动\"\n"
               "    type: url-test\n"
               "    proxies:\n"
               "      - \"CDN | 162.159.198.2 | 443\"\n")
        path = 写临时(cfg)
        rows = [{"node": "x", "country": "US", "server": "162.159.198.2", "port": 443}]
        changed, _, _ = wes.rewrite_names_with_country(path, rows)
        内容 = open(path, encoding="utf-8").read()
        断言(changed == 1, "只改了节点，没改策略组", changed)
        断言('- name: "🚀 WARP自动"' in 内容, "★ 策略组名没被改（它没有 server/port）")
        os.unlink(path)
        os.unlink(path + ".bak")


# ── 10. YAML 引号转义正确 ────────────────────────────────────
def test_10():
        cfg = 造配置([('CDN | "带引号" | 443', "162.159.198.2", 443)])
        path = 写临时(cfg)
        rows = [{"node": "x", "country": "US", "server": "162.159.198.2", "port": 443}]
        wes.rewrite_names_with_country(path, rows)
        内容 = open(path, encoding="utf-8").read()
        断言('\\"' in 内容, "名字里的引号被正确转义（不能破坏 YAML）", 内容)
        # 关键：转义后的行仍然是合法 YAML 标量
        行 = [l for l in 内容.split("\n") if l.strip().startswith("- name:")][0]
        值 = wes._yaml_scalar(行.strip()[len("- name:"):])
        断言(值.startswith("🇺🇸 US | "), "转义后仍能正确解析回来", 值)
        os.unlink(path)
        os.unlink(path + ".bak")


# ── 11. _flag 构造正确 ───────────────────────────────────────
def test_11():
        断言(wes._flag("US") == "\U0001F1FA\U0001F1F8", "US 国旗正确", wes._flag("US"))
        断言(wes._flag("JP") == "\U0001F1EF\U0001F1F5", "JP 国旗正确", wes._flag("JP"))
        断言(wes._flag("") == "🌐", "空国家码回落到 🌐")
        断言(wes._flag("XYZ") == "🌐", "非 2 位代码回落到 🌐")
        断言(wes._flag("us") == "\U0001F1FA\U0001F1F8", "小写也认")


# ── 12. CLI 参数存在 ─────────────────────────────────────────
def test_12():
        import subprocess
        r = subprocess.run([sys.executable, SCRIPT, "--help"], capture_output=True, text=True)
        断言(r.returncode == 0, "--help 正常退出", r.returncode)
        断言("--rewrite-config" in r.stdout, "有 --rewrite-config 参数")
        断言("--rewrite-format" in r.stdout, "有 --rewrite-format 参数")
        断言("--rewrite-dry-run" in r.stdout, "有 --rewrite-dry-run 参数")


test_1()
test_2()
test_3()
test_4()
test_5()
test_6()
test_7()
test_8()
test_9()
test_10()
test_11()
test_12()

print("\n=== 结果：%d 通过 / %d 失败 ===\n" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
