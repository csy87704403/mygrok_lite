# Grok 账号管理平台

开箱即用的 Grok **批量账号管理平台**：OpenAI 兼容 API 网关 + 账号注册 / 自动续期 / 降级重登 / 自动补号 / 额度监控。

Docker 化部署，代理节点（Mihomo）与临时邮箱服务**部署后自行配置**，镜像不包含任何私有订阅或账号数据。

---

## 功能

- **OpenAI 兼容 API** (`/v1/chat/completions`) — 多账号自动调度、负载均衡、busy 锁并发保护
- **批量注册** — 领邮箱 → 注册 → SSO → OAuth → CPA 入库全自动（需自备临时邮箱服务）
- **自动续期** — 精确调度：各账号失效前 30 分钟 RT 续期
- **降级重登** — RT 被吊销时浏览器自动重新登录恢复
- **自动补号** — 可调度账号 ≤ 5 时自动注册补至 ≥ 30
- **额度监控** — 实时显示每个账号剩余额度
- **Web 管理面板** — 账号/节点/Key/用量/设置管理

---

## 架构

```
[你的应用] → 平台容器(18080) → 按账号调度 → 通过宿主 Mihomo 节点 → Grok API
                           └── 注册/重登: Xvfb + CloakBrowser (运行时下载 Chromium)
```

- 平台容器用 `network_mode: host`，**直连宿主的 Mihomo 节点**（127.0.0.1:8001-8092）
- 平台**不内置 Mihomo**，代理节点完全由你宿主机自备
- Chrome 二进制**运行时首次下载**（镜像小），注册/重登才会触发，首次等待约 1-2 分钟

---

## 前提条件

1. **Docker + Docker Compose** 已安装
2. **宿主机已跑 Mihomo**，代理节点监听 `127.0.0.1:8001-8092`（可通过环境变量调整）
3. **临时邮箱服务**（注册功能需要，若只做 API 网关/续期可跳过）
4. **至少 1 个可用的 Grok 账号**（通过导入 CPA 或自动注册）

---

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/YOUR_USERNAME/grok-platform.git
cd grok-platform
```

### 2. 配置环境变量

```bash
cp .env.example .env
vi .env   # 填 GROK_PLATFORM_ADMIN_PASSWORD 等
```

### 3. 构建并启动

```bash
docker compose up -d --build
docker compose logs -f   # 查看启动日志
```

### 4. 访问

- 管理面板: `http://SERVER_IP:18080/`
- 健康检查: `http://SERVER_IP:18080/health`
- OpenAI 兼容 API: `http://SERVER_IP:18080/v1/chat/completions`

---

## 环境变量说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `GROK_PLATFORM_ADMIN_PASSWORD` | ✅ | 管理密码 |
| `GROK_ACCOUNTS_DIR` | | CPA/账号目录 (挂载卷) |
| `GROK_PUBLIC_BASE_URL` | | 公网域名 (如 `https://grok.example.com/v1`) |
| `TEMP_MAIL_API` | 注册用 | 临时邮箱 worker 地址 |
| `TEMP_MAIL_ADMIN` | 注册用 | 邮箱 admin 密码 |
| `TEMP_MAIL_DOMAINS` | 注册用 | 邮箱域名池 |
| `GROK_MAIL_CONFIG` | 注册用 | 注册脚本邮箱配置 JSON |

---

## 导入已有账号 (CPA)

把已有账号的 CPA JSON 文件放进 `./grok_accounts/cpa/`，在管理面板设置里导入即可。

---

## 代理节点 (Mihomo) 配置

平台通过 `--network=host` 直连宿主的 Mihomo。**你需要自己搭好 Mihomo** 并在节点端口提供 socks5/http 代理出口：

```
127.0.0.1:8001 ~ 8092  (或你自定义的端口)
```

部署时把可用节点端口通过后台「节点管理」页面添加即可。

---

## 数据持久化

- `./data/` → 平台 SQLite 数据库、状态文件
- `./grok_accounts/` → 账号 CPA 产出物

两个目录都挂载为卷，容器重建不丢数据。

---

## 常见问题

**Q: 容器能起，但注册/重登失败？**
A: 检查宿主 Mihomo 节点是否可达（容器内 `curl -x http://127.0.0.1:8001` 测试）。首次用浏览器会等 Chromium 下载。

**Q: 需要公网访问 API？**
A: 设置 `GROK_PUBLIC_BASE_URL` 指向你的域名/隧道。

---

## License

仅供学习与账号管理自动化使用。请遵守目标服务 (xAI/Grok) 的服务条款。
