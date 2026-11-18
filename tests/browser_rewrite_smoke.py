#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器回归：v6.28「节点名加地区」功能的可见区块。

为什么必须真浏览器：node --check 只验语法，验不了运行时顺序。
这个面板是**单文件前端**（app.js 整个包在 IIFE 里）——
顶层 const 声明在使用之后会抛 TDZ，整个脚本在那里就死了，
表现为「点任何按钮都没反应」，而语法检查完全无感。

★ 两个必须用真浏览器才能验到的点：
  1. app.js 整个包在 IIFE 里 → 内部函数**不在全局**，
     page.evaluate("buildRewriteBat()") 拿不到 —— 必须走真实点击下载。
  2. 上游 v6.27 把「出口检测」区块标了 legacy-hidden（默认隐藏），
     本功能必须**自带可见入口** —— 判据是「不展开任何折叠块也能看到它」。

跑法：python3 tests/browser_rewrite_smoke.py
"""
import os
import sys
import threading
import http.server
import socketserver

本目录 = os.path.dirname(os.path.abspath(__file__))
PAGES = os.path.join(本目录, "..", "pages")

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


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=PAGES, **kw)

    def log_message(self, *a):
        pass


def 起服务():
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [SKIP] playwright 未安装")
        return 0

    httpd, port = 起服务()
    url = "http://127.0.0.1:%d/index.html" % port
    print("\n=== 浏览器回归：节点名加地区（可见区块） ===\n")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        错误 = []
        页面错误 = []
        page.on("console", lambda m: 错误.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: 页面错误.append(str(e)))
        page.goto(url, wait_until="load")
        page.wait_for_timeout(900)

        # ① 页面无脚本错误（外部资源拉不到不算 —— 沙箱网络问题）
        本页错误 = [t for t in 错误 if "ERR_CONNECTION" not in t and "Failed to load resource" not in t]
        断言(not 页面错误, "页面无 pageerror（TDZ / 顶层崩溃会在这里现形）", 页面错误[:3])
        断言(not 本页错误, "页面无本脚本的 console error", 本页错误[:5])
        if 错误:
            print("  [提示] %d 条外部资源加载失败（沙箱网络，不计失败）" % len(错误))

        # ② 面板初始化跑完了（用可见状态判定，不用 typeof —— IIFE 里取不到）
        状态文本 = page.evaluate(
            "() => { const el=document.getElementById('settingsStatusText');"
            " return el?el.textContent.trim():''; }")
        断言(bool(状态文本), "★ 面板初始化跑完（设置状态已渲染 → 顶层代码没崩）", 状态文本)

        # ③ ★ 新区块**默认可见**（这是 C 方案的核心要求）
        区块可见 = page.evaluate("""() => {
          const box = document.getElementById('rewriteNamesBox');
          if (!box) return '(无此区块)';
          const cs = getComputedStyle(box);
          const r = box.getBoundingClientRect();
          return { display: cs.display, visible: r.width > 0 && r.height > 0 };
        }""")
        断言(isinstance(区块可见, dict), "找到 #rewriteNamesBox 区块", 区块可见)
        if isinstance(区块可见, dict):
            断言(区块可见.get("display") != "none",
                  "★ 新区块默认可见（不是 legacy-hidden）", 区块可见)
            断言(区块可见.get("visible"), "★ 新区块有实际尺寸（用户能滚到、能看见）", 区块可见)

        # ④ 隐藏的上游出口检测区块仍保持隐藏（不推翻上游产品决策）
        上游隐藏 = page.evaluate("""() => {
          const d = document.querySelector('details.egress-advanced');
          return d ? getComputedStyle(d).display : '(无此区块)';
        }""")
        断言(上游隐藏 == "none",
              "★ 上游的「出口检测」区块仍保持 legacy-hidden（不动上游决策）", 上游隐藏)

        # ⑤ 控件存在 + 默认值
        for 控件 in ["egressRewriteConfig", "egressRewriteFormat",
                     "egressRewriteController", "egressRewriteProxy",
                     "downloadRewriteScanner", "downloadRewriteBat"]:
            存在 = page.query_selector("#" + 控件) is not None
            断言(存在, "控件 #%s 存在" % 控件)

        默认格式 = page.eval_on_selector("#egressRewriteFormat", "e => e.value")
        断言(默认格式 == "flag", "前缀格式默认 flag（🇺🇸 US）", 默认格式)

        # ⑥ 新区块本身是 <details>（折叠式），填控件前要展开 ——
        #   折叠状态下 Playwright 的 fill 会等到超时（元素不可见）。
        #   这不是 bug：折叠是刻意的 UI 设计（默认收起，不打扰用户）。
        展开后 = page.evaluate("""() => {
          const box = document.getElementById('rewriteNamesBox');
          box.open = true;
          return box.open;
        }""")
        断言(展开后 is True, "新区块可展开（折叠式设计）")
        page.wait_for_timeout(250)
        输入可见 = page.query_selector("#egressRewriteConfig") is not None and \
            page.eval_on_selector("#egressRewriteConfig", "e => !!e.offsetParent")
        断言(输入可见, "★ 展开后输入框可见（用户真能填）")

        # ⑦ 路径留空 → 点下载**不生成文件**，而是提示（不生成坏命令）
        提示数 = []
        page.on("dialog", lambda d: (提示数.append(d.message), d.dismiss()))
        page.fill("#egressRewriteConfig", "")
        生成空 = True
        try:
            with page.expect_download(timeout=3000):
                page.click("#downloadRewriteBat")
        except Exception:
            生成空 = False
        断言(not 生成空, "★ 路径留空时点下载不会生成文件（避免生成坏脚本）")

        # ⑦ 填路径 → 点下载 → BAT 内容正确
        def 下载BAT():
            with page.expect_download() as 信息:
                page.click("#downloadRewriteBat")
            d = 信息.value
            with open(d.path(), "rb") as f:
                原文 = f.read()
            try:
                return 原文.decode("utf-8")
            except UnicodeDecodeError:
                return 原文.decode("gbk", errors="replace")

        路径 = r"D:\clash\masque-clash-smart.yaml"
        page.fill("#egressRewriteConfig", 路径)
        page.fill("#egressRewriteController", "http://127.0.0.1:9090")
        page.fill("#egressRewriteProxy", "http://127.0.0.1:7890")
        bat = 下载BAT()

        断言("--rewrite-config" in bat, "★ BAT 含 --rewrite-config")
        断言('--rewrite-config "D:\\clash\\masque-clash-smart.yaml"' in bat,
              "★ BAT 里路径正确且被引号包住", bat[-500:])
        断言('--rewrite-format "flag"' in bat, "BAT 带前缀格式参数")
        断言("--no-apply" in bat, "★ BAT 带 --no-apply（只检测改名字，不动用户的代理选择）")
        断言("--apply-all" not in bat, "★ BAT 不带 --apply-all")
        断言("重新导入" in bat, "★ BAT 提示了「要重新导入配置」（否则用户看不到新名字）")
        断言(".bak" in bat, "BAT 提示了备份")
        断言("--controller" in bat and "--proxy" in bat, "BAT 带 controller/proxy 参数")

        # ⑧ 路径含引号 → 必须转义（否则 BAT 命令断）
        page.fill("#egressRewriteConfig", 'D:\\a"b\\c.yaml')
        bat2 = 下载BAT()
        断言('a""b' in bat2, "★ 路径里的引号被转义成 \"\"（否则 BAT 命令会断）", bat2[-400:])

        # ⑨ 切换前缀格式
        page.select_option("#egressRewriteFormat", "name")
        bat3 = 下载BAT()
        断言('--rewrite-format "name"' in bat3, "切换格式后 BAT 参数跟着变")

        # ⑩ 既有功能没被弄坏
        主输出存在 = page.query_selector("#mainOutput") is not None
        断言(主输出存在, "主输出区仍在（既有功能没被弄坏）")

        browser.close()

    httpd.shutdown()
    print("\n=== 结果：%d 通过 / %d 失败 ===\n" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
