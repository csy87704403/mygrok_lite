# -*- coding: utf-8 -*-
"""检测 mihomo providers 里所有节点的 TCP 可达性。

用法:
    python _check_nodes.py              # 只测 TCP 可达性
    python _check_nodes.py --live       # 测真实出口 (需容器内跑, 走 mihomo 端口)

输出 name / server:port / 结果。用于节点失效后快速定位哪些还能用。
"""
import io
import os
import re
import socket
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE = os.path.dirname(os.path.abspath(__file__))
PROVIDERS = ['my_sub.yaml', 'local.yaml', 'extra_nodes.yaml']
SKIP = re.compile(r'(剩余流量|套餐到期|一毛机场|自动选择|故障转移|^DIRECT$|^REJECT$)', re.I)


def parse_nodes(path):
    """解析节点: 兼容 clash 单行 {name: x, server: y, port: z} 与多行 YAML 列表"""
    text = open(path, encoding='utf-8').read()
    out = []
    # 单行 clash 格式: - { name: xxx, type: ss, server: yyy, port: 443, ... }
    for m in re.finditer(r'-\s*\{([^}]*)\}', text):
        blk = m.group(1)

        def field(k):
            mm = re.search(rf"{k}:\s*'?\"?([^,'\"}}]+)'?\"?", blk)
            return mm.group(1).strip() if mm else ''
        nm, sv, pt = field('name'), field('server'), field('port')
        if nm and sv and pt.isdigit():
            out.append((nm, sv, int(pt)))
    # 多行 YAML 列表格式
    for m in re.finditer(r'-\s+name:\s*(.+)\n(?:.*\n)*?\s+server:\s*(.+)\n\s+port:\s*(\d+)', text):
        out.append((m.group(1).strip().strip('\'"'), m.group(2).strip().strip('\'"'), int(m.group(3))))
    return out


def tcp_ok(server, port, timeout=6):
    try:
        s = socket.create_connection((server, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def main():
    nodes, seen = [], set()
    for p in PROVIDERS:
        fp = os.path.join(BASE, 'providers', p)
        if not os.path.exists(fp):
            continue
        for nm, sv, pt in parse_nodes(fp):
            if nm in seen or SKIP.search(nm):
                continue
            seen.add(nm)
            nodes.append((nm, sv, pt))
    print(f'共 {len(nodes)} 个节点, 测试 TCP 可达性 (6s 超时):\n')
    ok = []
    for nm, sv, pt in nodes:
        good = tcp_ok(sv, pt)
        print(f'  {"可达  " if good else "不可达"} {nm}  ({sv}:{pt})')
        if good:
            ok.append((nm, sv, pt))
    print(f'\n可达 {len(ok)}/{len(nodes)}')


if __name__ == '__main__':
    main()
