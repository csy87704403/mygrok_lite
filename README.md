# Grok 账号管理平台 (mygrok_lite)

开箱即用的 Grok **批量账号管理平台**：OpenAI 兼容 API 网关 + 账号注册 / 自动续期 / 降级重登 / 自动补号 / 额度监控。

Docker 化部署，支持 **Linux 服务器** 与 **Windows (Docker Desktop / WSL2)** 双环境。

---

## 功能

- **OpenAI 兼容 API** (`/v1/chat/completions`) — 多账号自动调度、负载均衡、busy 锁并发保护，支持流式输出 (`stream: true`)
- **批量注册** — 领邮箱 → 注册 → SSO → OAuth → CPA 入库全自动（需自备临时邮箱服务）
- **自动续期** — 精确调度：各账号失效前 30 分钟 RT 续期
- **降级重登** — RT 被吊销时浏览器自动重新登录恢复
- **自动补号** — 可调度账号 ≤ 5 时自动注册补至 ≥ 30（**设置页勾选开关才启用**）
- **额度监控** — 实时显示每个账号剩余额度（HTTP 200 才更新，403/缺头保留旧值不误报）
- **Web 管理面板** — 账号 / 节点 / Key / 用量 / 设置 / 补号开关管理

---

## 架构

```
[你的应用] → 平台容器(18080) → 按账号调度 → 通过 Mihomo 节点 → Grok API
                           └── 注册/重登: Xvfb + CloakBrowser (运行时下载 Chromium)
```

**两种部署模式**（同一个仓库，配置文件区分）：

| | Linux 服务器 | Windows / Docker Desktop |
|---|---|---|
| 网络 | `host`（直连宿主机 Mihomo） | `bridge` + mihomo 容器同网络 |
| 节点地址 | `127.0.0.1:8001-8092` | `mihomo:8001/8002` + 8100+ 真实节点 |
| 数据卷 | `./data`、`./grok_accounts` 目录 | named volumes (WSL2 ext4) |
| compose 文件 | `docker-compose.yml` | 加 `-f docker-compose.docker-desktop.yml` |

---

## 快速开始

### Linux 服务器

```bash
git clone https://github.com/csy87704403/mygrok_lite.git
cd mygrok_lite

cp .env.example .env
vi .env            # 填 GROK_PLATFORM_ADMIN_PASSWORD

# 宿主机需已跑 Mihomo (监听 127.0.0.1:8001-8092)
docker compose up -d --build
```

### Windows (Docker Desktop)

双击 `start.bat` 即可全自动：拉起 Docker Desktop → 等引擎就绪 → 启动容器 → 打开管理面板。

或手动：

```bash
cp .env.example .env
# .env 里设置 GROK_DEFAULT_NODES=mihomo:8001,mihomo:8002
docker compose -f docker-compose.yml -f docker-compose.docker-desktop.yml up -d --build
```

### 访问

- 管理面板: `http://SERVER_IP:18080/`（登录密码 = `GROK_PLATFORM_ADMIN_PASSWORD`）
- 健康检查: `http://SERVER_IP:18080/health`
- OpenAI 兼容 API: `http://SERVER_IP:18080/v1/chat/completions`

---

## 环境变量 (.env)

| 变量 | 必填 | 说明 |
|------|------|------|
| `GROK_PLATFORM_ADMIN_PASSWORD` | ✅ | 管理密码 (登录面板 + 管理 API Bearer token) |
| `GROK_PLATFORM_PORT` | | 平台端口 (默认 18080) |
| `GROK_ACCOUNTS_DIR` | | CPA/账号目录 (挂载卷) |
| `GROK_PUBLIC_BASE_URL` | | 公网域名 (如 `https://grok.example.com/v1`) |
| `TEMP_MAIL_API` | 注册用 | 临时邮箱 worker 地址 |
| `TEMP_MAIL_ADMIN` | 注册用 | 邮箱 admin 密码 |
| `TEMP_MAIL_DOMAINS` | 注册用 | 邮箱域名池，格式 `domain1\|base_url1\|admin1,domain2\|...` |
| `GROK_MAIL_CONFIG` | 注册用 | 注册脚本邮箱配置 JSON: `[{"base_url":"...","domain":"..."}]` |
| `GROK_DEFAULT_NODES` | | 默认节点池 (逗号分隔)。Linux 留空用内置 31 端口；Docker Desktop 设 `mihomo:8001,mihomo:8002` |

---

## 代理节点 (Mihomo) 配置

### 方案 A：宿主机 Mihomo（Linux 默认）

平台通过 `--network=host` 直连宿主 Mihomo，节点端口提供 socks5/http 代理：

```
127.0.0.1:8001 ~ 8092
```

部署后在后台「节点管理」页面添加可用节点端口。

### 方案 B：mihomo 容器（Docker Desktop 默认，推荐通用）

仓库提供 `mihomo/` 配置目录（挂载到容器 `/root/.config/mihomo`）：

```
mihomo/
├── config.example.yaml          # 脱敏模板 (复制为 config.yaml 填写)
└── providers/
    └── local.example.yaml       # 本地节点模板 (复制为 local.yaml 填写)
```

**每个真实节点一个独立端口 (8100+)**，平台节点池直接看到真实节点（如 🇺🇸美国专线01），注册时一个账号锁定一个出口 IP，避免同一账号流程中途换 IP 触发风控。

`config.yaml` 结构要点：
- `proxy-providers.my_sub` — 你的订阅 URL（填真实 token）
- `proxy-providers.local_nodes` — 本地节点文件
- `listeners` — 每节点独立端口段（可用 `rebuild_config.py` 一键重建）
- `rules` — 只代理 Grok / 邮箱 / CF 相关域名，其余 REJECT

> ⚠️ `config.yaml` / `providers/*.yaml` 含订阅 token 与节点密码，**已加入 .gitignore 不会推送到 GitHub**。他人 clone 后按 `.example` 模板填写自己的订阅即可。

### 重建节点端口

订阅节点变化时（新增/失效），重新生成监听端口：

```bash
python mihomo/rebuild_config.py   # 幂等重建 config.yaml (自动分配 8100+ 端口)
docker compose -f docker-compose.yml -f docker-compose.docker-desktop.yml restart mihomo
```

---

## 导入已有账号 (CPA)

把已有账号的 CPA JSON 文件放进 CPA 目录（Linux: `./grok_accounts/cpa/`；Docker Desktop: 卷内 `/root/grok_accounts/cpa/`），在管理面板「设置 → CPA」里导入即可（支持本地目录 / 远程 WebDAV 两种模式，自动去重）。

---

## 自动补号开关

**默认关闭**。在管理面板「设置」页勾选「启用自动补号」后才生效（状态持久化，重启不丢）：

- 触发条件: 可调度账号 ≤ 5
- 补号目标: 达到 30 个
- 连续失败 5 个自动停止

---

## 数据持久化

| 环境 | 路径 |
|------|------|
| Linux | `./data/` (SQLite) + `./grok_accounts/` (CPA) |
| Docker Desktop | named volumes `mygrok_lite_platform_data` + `mygrok_lite_grok_accounts` (WSL2 ext4) |

容器重建 / 重启不丢数据。

---

## 本次改动 (相对原版)

### 1. 双环境部署支持
- `docker-compose.yml` 保持原版 host 网络（Linux 直接用）
- 新增 `docker-compose.docker-desktop.yml` override（bridge + mihomo 容器 + named volumes）
- `db.py` 节点池改为 `GROK_DEFAULT_NODES` 环境变量控制

### 2. 每真实节点独立出口 (新功能)
- mihomo 多 listener：每个真实节点独立端口 (8100+)，平台节点池直接可见
- 注册流程内账号锁定固定出口 IP（原版 REG_POOL 每连接轮询保留为兜底）

### 3. 稳定性修复
- **全站卡死根因**：`async def` 路由调同步阻塞函数（额度探测 34 次串行 510s）→ 改同步 `def` + `get_quota` 并行化 (8 线程 15s 上限)
- **额度误报**：HTTP 403 / 响应头缺额度不再写死 `remaining=0`，保留旧值
- **路径硬编码**：`/usr/bin/python3.11` → `/usr/local/bin/python3.11`（镜像实际位置）
- **代理格式兼容**：注册/重登脚本兼容 `127.0.0.1:port` 与 `mihomo:port` 两种节点格式

### 4. 新功能 / UI
- 设置页新增「自动补号」复选框（勾选才启用，持久化）
- 新增 `POST /api/auto-fill/set-enabled` API

### 5. 安全
- `.gitignore` 排除 mihomo 真实配置、本机脚本，只推脱敏示例

---

## API 使用

```bash
curl http://localhost:18080/v1/chat/completions \
  -H "Authorization: Bearer sk-grok-你的APIKEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.5","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

API Key 在管理面板「Keys」页创建。流式 (`stream: true`) 与一次性响应均支持。

---

## 常见问题

**Q: 容器能起，但注册/重登失败？**
A: 检查 Mihomo 节点是否可达。Linux: 容器内 `curl -x http://127.0.0.1:8001 https://cli-chat-proxy.grok.com/v1/models`；Docker Desktop: `docker exec grok-platform curl -x http://mihomo:8100 ...`。首次用浏览器会等 Chromium 下载 (1-2 分钟)。

**Q: Docker Desktop 下 start.bat 卡在"等待 Docker 引擎就绪"？**
A: Docker Desktop 冷启动需 1-2 分钟，耐心等待；超过 3 分钟说明引擎卡住，重启 Docker Desktop 再试。

**Q: 需要公网访问 API？**
A: 设置 `GROK_PUBLIC_BASE_URL` 指向你的域名/隧道。

**Q: 改了代码不生效？**
A: 需要重新构建镜像: `docker compose build && docker compose up -d`（或 Docker Desktop 版加 `-f docker-compose.docker-desktop.yml`）。

---

## License

仅供学习与账号管理自动化使用。请遵守目标服务 (xAI/Grok) 的服务条款。
