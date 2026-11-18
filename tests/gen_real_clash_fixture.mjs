// 用上游 app.js 的**真实代码**生成一份 Clash 配置，作为回写功能的端到端夹具。
//
// 为什么不用手编样本：回写逻辑是纯文本行扫描，正确性完全取决于
// 「上游真实产物行长什么样」。手编样本可能与真实产物有出入
// （缩进、字段顺序、q() 的转义）→ 测试全绿但线上改不中。
//
// 做法：把 app.js 里 makeNode / nodeName / q / connectionPairs 相关代码抠出来，
//       喂一份最小可用的 source（config.json），跑出真实 YAML。

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const 本目录 = path.dirname(fileURLToPath(import.meta.url));
const appPath = path.join(本目录, '..', 'pages', 'app.js');
const src = fs.readFileSync(appPath, 'utf8');

// —— 抠出需要的函数（按大括号配平）——
function 抠(名) {
  const m = src.match(new RegExp(`(?:async\\s+)?function\\s+${名}\\s*\\(`));
  if (!m) throw new Error('找不到 ' + 名);
  const 起 = m.index;
  let i = src.indexOf('{', m.index + m[0].length - 1);
  let 深 = 0, j = i;
  while (j < src.length) {
    const c = src[j];
    if (c === '"' || c === "'" || c === '`') {
      const q = c; j++;
      while (j < src.length) {
        if (src[j] === '\\') { j += 2; continue; }
        if (src[j] === q) { j++; break; }
        j++;
      }
      continue;
    }
    if (c === '{') 深++;
    else if (c === '}') { 深--; if (深 === 0) break; }
    j++;
  }
  return src.slice(起, j + 1);
}

// app.js 里 q() / bool 是 const 表达式赋值，不是 function 声明 —— 单独抠
const qMatch = src.match(/const\s+q\s*=\s*[^;]+;/);
if (!qMatch) throw new Error('找不到 q()');
const boolMatch = src.match(/const\s+bool\s*=\s*[^;]+;/);
if (!boolMatch) throw new Error('找不到 bool()');

// 最小可用的 source（模拟 config.json 解析结果）
const source = {
  private_key: 'FAKEPRIVATEKEYBASE64==',
  endpoint_pub_key: '-----BEGIN PUBLIC KEY-----\nFAKE\n-----END PUBLIC KEY-----\n',
  endpoint_v4: '162.159.198.2',
  endpoint_v6: '',
  endpoint_h2_v4: '162.159.198.2',
  endpoint_h2_v6: '',
  ipv4: '1.2.3.4',
  ipv6: '',
};

// 需要抠的函数：q / pemBody / nodeName / makeNode / innerIps / cidr4 / cidr6
const 需要 = ['pemBody', 'nodeName', 'makeNode', 'innerIps', 'cidr4', 'cidr6'];
const 体 = 需要.map(抠).join('\n\n');

const 沙箱 = `
${qMatch[0]}
${boolMatch[0]}
${体}
const source = ${JSON.stringify(source)};
const $ = (id) => ({
  value: id === 'nodeTag' ? 'CDN' : id === 'nodeNaming' ? 'detail' : ''
});
// nodeName 只读 nodeTag / nodeNaming
const pairs = [['162.159.198.2', 443], ['104.16.0.1', 8443], ['172.64.0.5', 2053]];
const a = ['proxies:'];
const names = [];
pairs.forEach(([server, port], i) => {
  const n = nodeName(server, port, i);
  names.push(n);
  makeNode(a, n, server, port, {
    network: 'h3', udp: true, sni: 'speed.cloudflare.com', sniList: [],
    mtu: 1400, stack: 'ipv4', cc: 'bbr', remoteDns: false, dns: [],
    outerCc: '', bbrProfile: '', handshake: 5
  }, i);
});
export const yaml = a.join('\\n');
`;

const tmp = path.join('/opt/data/cache/scratch', 'gen-clash-fixture.mjs');
fs.writeFileSync(tmp, 沙箱);
const { yaml } = await import(tmp);

const 输出 = path.join(process.argv[2] || '/opt/data/cache/scratch/real-clash-fixture.yaml');
fs.writeFileSync(输出, yaml + '\n');
console.log('已生成真实 Clash 夹具:', 输出);
console.log('--- 前 20 行 ---');
console.log(yaml.split('\n').slice(0, 20).join('\n'));
