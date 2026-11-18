#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Usque / Mihomo WARP Egress Selector v6.28 (fork: + 节点名回写)

What it does:
1. Connects to Mihomo's localhost REST API.
2. Enumerates MASQUE nodes.
3. Selects each node in the hidden "出口检测" selector.
4. Sends IP/country probes through the local mixed proxy.
5. Optionally tests ChatGPT HTTP reachability through the AI selector.
6. Ranks nodes by preferred country -> ChatGPT reachability -> latency.
7. Selects the winning node in "地区优选", "PROXY", "AI", optionally all service groups.
8. Writes JSON + CSV reports.

It DOES NOT force Cloudflare WARP to egress from a requested country.
It only selects among the real egress locations observed from your existing nodes.

9. (v6.28) Optionally rewrites the Clash config's node names to carry the
   measured egress country, e.g. "CDN | 162.159.198.2 | 443"
   -> "🇺🇸 US | CDN | 162.159.198.2 | 443".
   The rewrite matches nodes by "server:port" (the WARP ingress), NOT by
   node name -- names may be duplicated or hand-edited, but the ingress
   endpoint of a node never changes.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_GROUPS = [
    "AI","YouTube","Emby","TikTok","Netflix","Disney","Spotify","GitHub",
    "Telegram","Google","Twitter","Instagram","Facebook","Apple","Microsoft",
    "Steam","Xbox","PlayStation","Nintendo","Porn","国外网站","🐟 漏网之鱼",
]

COUNTRY_NAMES = {
    "US":"美国","SG":"新加坡","JP":"日本","HK":"香港","TW":"台湾","KR":"韩国",
    "GB":"英国","DE":"德国","FR":"法国","NL":"荷兰","CA":"加拿大","AU":"澳大利亚",
    "CH":"瑞士","SE":"瑞典","FI":"芬兰","IT":"意大利","ES":"西班牙","PL":"波兰",
    "BR":"巴西","IN":"印度","ID":"印度尼西亚","MY":"马来西亚","TH":"泰国","VN":"越南",
}

# 国旗（回写节点名时用）。只覆盖常见国家，其余回落到 🌐。
# ⚠ 用「区域指示符 + 国旗字母」构造，避免源文件里塞大量 emoji 字面量
#   （部分 Windows 终端按 GBK 解码会花屏）。
def _flag(code):
    code = (code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)

def controller_request(base, path, method="GET", data=None, secret="", timeout=8):
    url = base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if secret:
        headers["Authorization"] = "Bearer " + secret
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8", "replace"))

def select_proxy(base, group, node, secret):
    path = "/proxies/" + urllib.parse.quote(group, safe="")
    controller_request(base, path, method="PUT", data={"name": node}, secret=secret)

def delay_test(base, node, secret, timeout_ms=6000):
    path = "/proxies/{}/delay?url={}&timeout={}".format(
        urllib.parse.quote(node, safe=""),
        urllib.parse.quote("http://cp.cloudflare.com/generate_204", safe=""),
        int(timeout_ms),
    )
    try:
        data = controller_request(base, path, secret=secret, timeout=max(8, timeout_ms / 1000 + 3))
        return int((data or {}).get("delay") or 0)
    except Exception:
        return 0

def proxy_opener(proxy_url):
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )

def http_probe(opener, url, timeout=10, headers=None):
    hdr = {
        "User-Agent": "Mozilla/5.0 Usque-Egress-Selector/6.5",
        "Accept": "*/*",
    }
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, headers=hdr, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(512 * 1024).decode("utf-8", "replace")
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read(512 * 1024).decode("utf-8", "replace")
        return e.code, body, dict(e.headers)
    except Exception as e:
        return 0, str(e), {}

def parse_cf_trace(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out

def detect_location(opener):
    result = {
        "ip": "", "country": "", "region": "", "city": "", "colo": "", "org": "",
        "location_source": "",
    }

    # Cloudflare trace is lightweight and usually reliable for loc/colo/ip.
    status, body, _ = http_probe(opener, "https://www.cloudflare.com/cdn-cgi/trace", timeout=10)
    if 200 <= status < 400:
        t = parse_cf_trace(body)
        result["ip"] = t.get("ip", "")
        result["country"] = t.get("loc", "").upper()
        result["colo"] = t.get("colo", "")
        result["location_source"] = "cloudflare-trace"

    # Enrich with city/region/org if ipinfo is available.
    status2, body2, _ = http_probe(opener, "https://ipinfo.io/json", timeout=10)
    if 200 <= status2 < 400:
        try:
            j = json.loads(body2)
            result["ip"] = j.get("ip") or result["ip"]
            result["country"] = (j.get("country") or result["country"]).upper()
            result["region"] = j.get("region") or ""
            result["city"] = j.get("city") or ""
            result["org"] = j.get("org") or ""
            result["location_source"] = "ipinfo+cloudflare"
        except Exception:
            pass
    return result

def test_chatgpt(opener):
    status, body, _ = http_probe(
        opener,
        "https://chatgpt.com/",
        timeout=12,
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    low = body.lower()
    blocked_markers = [
        "unsupported_country",
        "unable to load site",
        "if you are using a vpn",
        "access denied",
    ]
    blocked = any(x in low for x in blocked_markers)
    # 403 can be a challenge/anti-bot response, so keep the raw status in report.
    ok = (200 <= status < 400) and not blocked
    return ok, status, ("blocked-marker" if blocked else "")

def get_raw_nodes(proxies):
    out = []
    for name, info in proxies.items():
        typ = str((info or {}).get("type", "")).lower()
        if typ == "masque":
            out.append(name)
    if out:
        return out

    # Fallback: use members of the hidden detection group, excluding groups/builtins.
    detector = proxies.get("出口检测") or {}
    candidates = detector.get("all") or []
    group_types = {"selector","urltest","fallback","loadbalance"}
    for name in candidates:
        info = proxies.get(name) or {}
        typ = str(info.get("type", "")).lower()
        if typ not in group_types and name not in {"DIRECT","REJECT","PASS","COMPATIBLE"}:
            out.append(name)
    return out

def rank_key(row, preferred, chatgpt_first):
    country = row.get("country", "")
    try:
        country_rank = preferred.index(country)
    except ValueError:
        country_rank = len(preferred) + (0 if not preferred else 10)

    chatgpt_rank = 0 if row.get("chatgpt_ok") else 1
    if not chatgpt_first:
        chatgpt_rank = 0

    delay = row.get("delay_ms") or 999999
    alive_rank = 0 if row.get("probe_ok") else 1
    return (alive_rank, country_rank, chatgpt_rank, delay)

def safe_select(base, proxies, group, node, secret):
    info = proxies.get(group)
    if not info:
        return False
    members = info.get("all") or []
    if node not in members:
        return False
    try:
        select_proxy(base, group, node, secret)
        return True
    except Exception:
        return False


# ── v6.28：把实测到的出口国家写回 Clash 配置的节点名 ──────────────
#
# 为什么能这么做、为什么必须这么做：
#   节点名里的 server 是 **Cloudflare 入口 IP**，不是最终出口 IP。
#   同一入口在不同网络/时间下出口国家可能不同 → 生成配置的那一刻
#   手上**没有任何**节点的出口国家信息，给名字标国家等于撒谎。
#   出口国家只能实测（本脚本逐节点探测得来），所以只能在**检测之后**回写。
#
# 为什么按 server+port 匹配，而不是按节点名：
#   节点名可能重复（不同命名模式）、被用户手改过；
#   而节点的入口端点（server:port）在配置里唯一且不变 → 用它当键。
#
# 为什么纯文本处理、不引 yaml 库：
#   本脚本只依赖标准库（用户环境不一定有 PyYAML）；
#   且只改 `- name:` 一行的值，不动结构 → 按行扫描足够且更安全。

def _yaml_scalar(text):
    """取 YAML 标量值：去掉引号。"""
    v = (text or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v.strip()


def _yaml_quote(text):
    """按 YAML 双引号规则转义（与面板 app.js 的 q() 行为一致）。"""
    out = []
    for ch in str(text):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def rewrite_names_with_country(config_path, rows, prefix_format="flag", dry_run=False):
    """把实测出口国家写回 Clash 配置的节点名。

    rows: 检测结果行，每行需含 country 与 server/port（由检测循环填入）。
    返回 (改写数量, 未命中数量, 报告行列表)。
    """
    with open(config_path, "r", encoding="utf-8") as f:
        original = f.read()
    lines = original.split("\n")

    # 建「server:port → country」索引（server/port 来自 Mihomo 运行时对象，权威）。
    by_endpoint = {}
    for row in rows:
        cc = (row.get("country") or "").strip().upper()
        server = str(row.get("server") or "").strip()
        port = str(row.get("port") or "").strip()
        if not cc or not server or not port:
            continue
        by_endpoint["%s:%s" % (server, port)] = cc

    changed = 0
    unmatched = 0
    report = []
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        stripped = line.lstrip()
        if not stripped.startswith("- name:"):
            i += 1
            continue
        # 收集这个节点的行范围（到下一个同缩进的列表项、或缩进变浅的顶层键为止）
        start = i
        indent = len(line) - len(stripped)
        j = i + 1
        server = ""
        port = ""
        while j < total:
            nxt = lines[j]
            nxt_stripped = nxt.lstrip()
            nxt_indent = len(nxt) - len(nxt_stripped)
            if nxt_stripped.startswith("- ") and nxt_indent <= indent:
                break
            if nxt_stripped and not nxt_stripped.startswith("#") and nxt_indent < indent:
                break
            if nxt_stripped.startswith("server:"):
                server = _yaml_scalar(nxt_stripped[len("server:"):])
            elif nxt_stripped.startswith("port:"):
                port = _yaml_scalar(nxt_stripped[len("port:"):])
            j += 1
        if not server or not port:
            i = j
            continue
        key = "%s:%s" % (server, port)
        cc = by_endpoint.get(key)
        old_name = _yaml_scalar(stripped[len("- name:"):])
        if not cc:
            unmatched += 1
            report.append("  [未命中] %s  (server:port=%s)" % (old_name, key))
            i = j
            continue
        if prefix_format == "flag":
            prefix = "%s %s" % (_flag(cc), cc)
        elif prefix_format == "name":
            prefix = "%s %s" % (_flag(cc), COUNTRY_NAMES.get(cc, cc))
        else:
            prefix = cc
        # 已经带国家前缀 → 跳过（幂等：重复跑不会变成 "🇺🇸 US 🇺🇸 US ..."）
        if old_name.startswith(prefix):
            i = j
            continue
        new_name = "%s | %s" % (prefix, old_name)
        lines[start] = " " * indent + "- name: " + _yaml_quote(new_name)
        changed += 1
        report.append("  %s  ->  %s" % (old_name, new_name))
        i = j

    if changed and not dry_run:
        # ⚠ 备份必须存**改写前**的原文（改写是破坏性操作，必须留退路）。
        #   顺序反了 → 备份里存的是新内容 → 改坏了也回不去。
        with open(config_path + ".bak", "w", encoding="utf-8") as f:
            f.write(original)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return changed, unmatched, report

def main():
    ap = argparse.ArgumentParser(description="Detect and select real WARP egress locations for Mihomo MASQUE nodes.")
    ap.add_argument("--controller", default="http://127.0.0.1:9090")
    ap.add_argument("--secret", default="")
    ap.add_argument("--proxy", default="http://127.0.0.1:7890")
    ap.add_argument("--prefer", default="", help="Country codes in priority order, e.g. US,SG,JP")
    ap.add_argument("--chatgpt-first", action="store_true")
    ap.add_argument("--apply-all", action="store_true")
    ap.add_argument("--no-apply", action="store_true")
    ap.add_argument("--config", default="", help="Optional JSON preferences file")
    ap.add_argument("--sleep", type=float, default=0.45, help="Pause between node switches")
    # v6.28：把实测出口国家写回 Clash 配置的节点名
    ap.add_argument("--rewrite-config", default="",
                    help="Rewrite node names in this Clash YAML with the measured egress country")
    ap.add_argument("--rewrite-format", default="flag", choices=["flag", "name", "code"],
                    help="Prefix format: flag=🇺🇸 US / name=🇺🇸 美国 / code=US")
    ap.add_argument("--rewrite-dry-run", action="store_true",
                    help="Only show what would be rewritten; do not touch the file")
    args = ap.parse_args()

    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            args.controller = cfg.get("controller") or args.controller
            args.secret = cfg.get("secret") or args.secret
            args.proxy = cfg.get("proxy") or args.proxy
            if cfg.get("preferred_countries"):
                args.prefer = ",".join(cfg["preferred_countries"])
            if cfg.get("chatgpt_first") is not None:
                args.chatgpt_first = bool(cfg["chatgpt_first"])
            if cfg.get("apply_all") is not None:
                args.apply_all = bool(cfg["apply_all"])
        except Exception as e:
            print("[WARN] 无法读取配置文件：", e)

    preferred = [x.strip().upper() for x in args.prefer.split(",") if x.strip()]
    print("=== Usque / Mihomo WARP 出口检测器 v6.28 (fork) ===")
    print("Controller:", args.controller)
    print("Local Proxy:", args.proxy)
    print("Preferred:", preferred or ["ANY"])
    print("ChatGPT first:", args.chatgpt_first)
    print()

    try:
        data = controller_request(args.controller, "/proxies", secret=args.secret)
    except Exception as e:
        print("[ERROR] 无法连接 Mihomo Controller:", e)
        print("请检查 Clash/Mihomo 是否正在运行，以及 Controller 地址/Secret 是否正确。")
        sys.exit(2)

    proxies = (data or {}).get("proxies") or {}
    nodes = get_raw_nodes(proxies)
    if not nodes:
        print("[ERROR] 没找到 MASQUE 节点。请先加载 v6.5 生成的 Clash 配置。")
        sys.exit(3)

    print(f"检测到 {len(nodes)} 个 MASQUE 节点。")
    opener = proxy_opener(args.proxy)
    rows = []

    for i, node in enumerate(nodes, 1):
        print(f"[{i}/{len(nodes)}] {node}")
        row = {
            "node": node,
            "delay_ms": 0,
            "ip": "",
            "country": "",
            "country_name": "",
            "region": "",
            "city": "",
            "colo": "",
            "org": "",
            "chatgpt_ok": False,
            "chatgpt_status": 0,
            "probe_ok": False,
            "error": "",
            # v6.28：记下节点的入口端点 —— 回写节点名时用它当匹配键
            # （节点名可能重复或被手改，server:port 不会变）。
            "server": "",
            "port": "",
        }
        try:
            # Mihomo 的 /proxies 里每个节点对象带 server / port 字段。
            node_info = proxies.get(node) or {}
            row["server"] = str(node_info.get("server") or "")
            row["port"] = str(node_info.get("port") or "")
            select_proxy(args.controller, "出口检测", node, args.secret)
            time.sleep(max(0.1, args.sleep))

            row["delay_ms"] = delay_test(args.controller, node, args.secret)

            loc = detect_location(opener)
            row.update(loc)
            row["country_name"] = COUNTRY_NAMES.get(row["country"], row["country"])
            row["probe_ok"] = bool(row.get("ip") or row.get("country"))

            # ChatGPT domain is routed to AI, so point AI directly at the same raw node for the test.
            if proxies.get("AI") and node in (proxies["AI"].get("all") or []):
                select_proxy(args.controller, "AI", node, args.secret)
                time.sleep(max(0.1, args.sleep / 2))
                ok, status, marker = test_chatgpt(opener)
                row["chatgpt_ok"] = ok
                row["chatgpt_status"] = status
                if marker:
                    row["chatgpt_note"] = marker

        except Exception as e:
            row["error"] = str(e)

        print(
            "   IP={}  {} {} {}  colo={}  delay={}ms  ChatGPT={}({})".format(
                row.get("ip") or "-",
                row.get("country") or "-",
                row.get("region") or "",
                row.get("city") or "",
                row.get("colo") or "-",
                row.get("delay_ms") or "-",
                "OK" if row.get("chatgpt_ok") else "NO",
                row.get("chatgpt_status") or "-",
            )
        )
        rows.append(row)

    rows_sorted = sorted(rows, key=lambda r: rank_key(r, preferred, args.chatgpt_first))
    best = rows_sorted[0] if rows_sorted else None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    json_name = f"warp-egress-report-{stamp}.json"
    csv_name = f"warp-egress-report-{stamp}.csv"

    with open(json_name, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "preferred_countries": preferred,
                "chatgpt_first": args.chatgpt_first,
                "best": best,
                "results": rows_sorted,
            },
            f, ensure_ascii=False, indent=2
        )

    fields = [
        "node","delay_ms","ip","country","country_name","region","city","colo","org",
        "chatgpt_ok","chatgpt_status","probe_ok","error"
    ]
    with open(csv_name, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows_sorted:
            w.writerow(row)

    print()
    print("=== 检测完成 ===")
    print("JSON:", json_name)
    print("CSV :", csv_name)

    # Country summary
    counts = {}
    for r in rows:
        c = r.get("country") or "UNKNOWN"
        counts[c] = counts.get(c, 0) + 1
    print("出口分布:", ", ".join(f"{k}:{v}" for k,v in sorted(counts.items())))

    if best:
        print()
        print("最佳候选:")
        print("  节点:", best["node"])
        print("  出口:", best.get("country"), best.get("region"), best.get("city"))
        print("  IP  :", best.get("ip"))
        print("  Colo:", best.get("colo"))
        print("  Delay:", best.get("delay_ms"), "ms")
        print("  ChatGPT:", best.get("chatgpt_ok"), "HTTP", best.get("chatgpt_status"))

    if args.rewrite_config:
        # v6.28：把实测到的出口国家写回配置文件的节点名。
        # ⚠ 放在「检测完成」之后 —— 只有实测才知道每个节点的出口国家，
        #   生成配置的那一刻是不知道的。
        print()
        print("=== 回写节点名（写入实测出口国家）===")
        try:
            changed_n, unmatched_n, report = rewrite_names_with_country(
                args.rewrite_config, rows, args.rewrite_format, args.rewrite_dry_run
            )
            for line in report[:200]:
                print(line)
            if len(report) > 200:
                print("  ...（其余 %d 行省略）" % (len(report) - 200))
            if args.rewrite_dry_run:
                print("[DRY-RUN] 已改写 %d 个节点名，未命中 %d 个；未写入文件" % (changed_n, unmatched_n))
            else:
                print("[REWRITE] 已改写 %d 个节点名，未命中 %d 个" % (changed_n, unmatched_n))
                if changed_n:
                    print("[REWRITE] 原文件已备份：%s.bak" % args.rewrite_config)
                if unmatched_n:
                    print("[REWRITE] 未命中的节点：检测时没测到出口（探测失败），名字保持原样")
        except Exception as e:
            print("[REWRITE] 失败：", e)

    if best and not args.no_apply:
        chosen = best["node"]
        # Refresh proxy state before applying.
        latest = controller_request(args.controller, "/proxies", secret=args.secret)
        pmap = (latest or {}).get("proxies") or {}

        selected = []
        for group in ["地区优选", "PROXY", "AI"]:
            if safe_select(args.controller, pmap, group, chosen, args.secret):
                selected.append(group)

        if args.apply_all:
            for group in DEFAULT_GROUPS:
                if safe_select(args.controller, pmap, group, chosen, args.secret):
                    selected.append(group)

        print()
        print("[APPLY] 已把最佳节点应用到:", ", ".join(selected) if selected else "(无可写策略组)")
        print("[APPLY] 节点:", chosen)
    else:
        print("[INFO] --no-apply 已启用，仅检测，不修改策略组。")

if __name__ == "__main__":
    main()
