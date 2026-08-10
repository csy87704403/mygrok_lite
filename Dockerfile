# Grok 账号管理平台 - Docker 镜像
# 特性: 运行时下载 Chromium(镜像小), 不含 Mihomo(宿主自备节点), 数据全走挂载卷
FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.description="Grok 账号管理平台 (OpenAI 兼容 API 网关 + 批量账号注册/续期/降级)"
LABEL org.opencontainers.image.source="https://github.com/csy87704403/mygrok_lite"

# 系统依赖: Xvfb(虚拟显示) + xdotool(物理点击兜底) + 浏览器运行库 + 中文字体
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb xdotool imagemagick \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 libx11-xcb1 \
    fonts-wqy-microhei fonts-wqy-zenhei fonts-dejavu-core \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
# playwright 用于 cloakbrowser 的 Chromium 基础 (cloakbrowser 会运行时下载自己的补丁版 Chromium)
RUN pip install --no-cache-dir \
    fastapi uvicorn curl_cffi pydantic \
    playwright websocket-client \
    cloakbrowser==0.5.4 \
    && rm -rf ~/.cache/pip

# 复制平台代码
WORKDIR /app
COPY main.py config.py db.py auto_refresh.py start.sh ./
COPY services/ ./services/
COPY static/ ./static/
# 脚本直接 COPY 到 /app/scripts (不经过根目录 mv)
COPY scripts/ ./scripts/

# 数据目录 (挂载卷)
RUN mkdir -p /app/data /root/grok_accounts/cpa

# Xvfb 虚拟显示
ENV DISPLAY=:1

# 平台配置 (默认值, 部署时用环境变量覆盖)
ENV GROK_PLATFORM_PORT=18080 \
    GROK_ACCOUNTS_DIR=/root/grok_accounts \
    GROK_REGISTER_SCRIPT=/app/scripts/grok_auto_v7.py \
    GROK_REFRESH_SCRIPT=/app/scripts/refresh_cpa.py \
    GROK_DEGRADE_SCRIPT=/app/scripts/grok_login_sso.py

# 健康检查
EXPOSE 18080

# 启动: Xvfb + 平台 (xdotool 需要真实显示, Xvfb :1)
CMD ["sh", "-c", "Xvfb :1 -screen 0 1280x800x24 & sleep 1 && exec python3 main.py"]
