# -*- coding: utf-8 -*-
"""mygrok_lite: 幂等重建 mihomo/config.yaml.
从 providers 提取所有真实节点, 每个节点生成单节点组(NODE_x) + 独立 mixed listener(8100+).
完全覆盖写入, 可反复运行不产生重复。
"""
import re, io, sys, os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# BASE 自动检测: 脚本位于 <project>/mihomo/ 目录, 兼容 Linux/Windows
BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, 'config.yaml')
SUB = os.path.join(BASE, 'providers', 'my_sub.yaml')
LOCAL = os.path.join(BASE, 'providers', 'local.yaml')
# 订阅 URL: 从环境变量 SUB_URL 读取 (未设置则用占位符, 需手动替换)
SUB_URL = os.environ.get('SUB_URL', 'https://your-subscribe-provider.com/api/v1/client/subscribe?token=YOUR_TOKEN')
START_PORT = 8100
SKIP_NAMES = re.compile(r'(剩余流量|套餐到期|一毛机场|自动选择|故障转移|^DIRECT$|^REJECT$)', re.I)

def extract_names(path):
    text = open(path, encoding='utf-8').read()
    names = []
    for m in re.finditer(r'name:\s*[\'"]?([^\r\n\'",}]+)[\'"]?\s*[,}]', text):
        n = m.group(1).strip()
        if n and not SKIP_NAMES.search(n):
            names.append(n)
    for m in re.finditer(r'^\s*-\s+name:\s*([^\r\n]+)\r?$', text, re.M):
        n = m.group(1).strip().strip('"\'').strip()
        if n and not SKIP_NAMES.search(n):
            names.append(n)
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n); out.append(n)
    return out

def main():
    sub_names = extract_names(SUB)
    local_names = extract_names(LOCAL)
    all_names, seen = [], set()
    for n in sub_names + local_names:
        if n not in seen:
            seen.add(n); all_names.append(n)

    # 生成单节点组 + listener
    groups, listeners = [], []
    for i, name in enumerate(all_names):
        port = START_PORT + i
        gname = f'NODE_{port}'
        esc = re.escape(name).replace(r'\ ', r'\s+').replace("'", r"\'")
        groups.append(f'''  - name: {gname}
    type: select
    use:
      - my_sub
      - local_nodes
    filter: '(?i){esc}' ''')
        listeners.append(f'''  - name: L{port}
    type: mixed
    port: {port}
    listen: 0.0.0.0
    proxy: {gname}''')

    cfg = f'''# Mihomo 配置 - mygrok_lite 平台专用 (自动生成, 幂等重建)
# 出口: 8100+ 每个真实节点一个独立端口 (平台节点池直接使用)
# 兜底: 8001 mixed / 8002 http -> REG_POOL round-robin 全池轮询

mixed-port: 8001
port: 8002
socks-port: 8003
allow-lan: true
bind-address: "*"
mode: rule
log-level: info
ipv6: false
external-controller: 0.0.0.0:9090
secret: "mygrok-lite-mihomo-secret"

# 节点来源
proxy-providers:
  my_sub:
    type: http
    url: "{SUB_URL}"
    path: ./providers/my_sub.yaml
    interval: 3600
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
      lazy: true
  local_nodes:
    type: file
    path: ./providers/local.yaml
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
      lazy: true

proxy-groups:
  # 全池轮询兜底
  - name: REG_POOL
    type: load-balance
    use:
      - my_sub
      - local_nodes
    url: https://www.gstatic.com/generate_204
    interval: 120
    lazy: false
    strategy: round-robin

  - name: FALLBACK
    type: select
    proxies:
      - REG_POOL
      - DIRECT

  # === 单节点固定出口 (自动生成) ===
{chr(10).join(groups)}
listeners:
  # === 每节点独立端口 (自动生成) ===
{chr(10).join(listeners)}

rules:
  # 订阅/节点域名直连 (拉取节点不能被 REJECT)
  - DOMAIN-SUFFIX,your-subscribe-provider.com,DIRECT
  - DOMAIN-SUFFIX,your-node-provider.info,DIRECT
  - DOMAIN-SUFFIX,xn--mirrors-oj8km52txc7d.com,DIRECT
  - DOMAIN-SUFFIX,x.ai,REG_POOL
  - DOMAIN-SUFFIX,grok.com,REG_POOL
  - DOMAIN-SUFFIX,grokusercontent.com,REG_POOL
  - DOMAIN-SUFFIX,grokipedia.com,REG_POOL
  - DOMAIN-SUFFIX,auth.x.ai,REG_POOL
  - DOMAIN-KEYWORD,turnstile,REG_POOL
  - DOMAIN-SUFFIX,cloudflare.com,REG_POOL
  - DOMAIN-SUFFIX,cloudflareinsights.com,REG_POOL
  - DOMAIN-SUFFIX,challenges.cloudflare.com,REG_POOL
  - DOMAIN-SUFFIX,215.im,REG_POOL
  - DOMAIN-SUFFIX,chatgpt.org.uk,REG_POOL
  - DOMAIN-SUFFIX,tempmail.lol,REG_POOL
  - DOMAIN-SUFFIX,770440.xyz,REG_POOL
  - DOMAIN-SUFFIX,770770.xyz,REG_POOL
  - DOMAIN-SUFFIX,icodetensor.com,REG_POOL
  - DOMAIN-SUFFIX,airfryersbg.com,REG_POOL
  - MATCH,REJECT
'''
    with open(CFG, 'w', encoding='utf-8') as f:
        f.write(cfg)
    print(f'✅ 重建完成: {len(all_names)} 节点 -> 端口 {START_PORT}~{START_PORT+len(all_names)-1}')
    for i in range(min(8, len(all_names))):
        print(f'  {START_PORT+i} -> {all_names[i]}')

if __name__ == '__main__':
    main()
