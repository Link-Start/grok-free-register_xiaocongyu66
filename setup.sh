#!/bin/bash
# Grok Free Register — 一键安装 (硬性要求: Python + Go inventory [+ hybrid Rust/C++])
# 用法: bash setup.sh

set -e
cd "$(dirname "$0")"

echo "=== Grok Free Register 安装 ==="
echo "    硬性栈: Python (编排) + Go (高并发 I/O) + Rust (库存/成品)"
echo ""

# 工具链预检
missing_tc=0
if ! command -v python3 >/dev/null 2>&1; then
    echo "[✗] 缺少 python3" >&2
    missing_tc=1
fi
if ! command -v go >/dev/null 2>&1; then
    echo "[✗] 缺少 go (https://go.dev/dl/ 或 apt install golang-go)" >&2
    missing_tc=1
fi
if ! command -v cargo >/dev/null 2>&1 || ! command -v rustc >/dev/null 2>&1; then
    echo "[✗] 缺少 rustc/cargo (https://rustup.rs/)" >&2
    missing_tc=1
fi
if [ "$missing_tc" -ne 0 ]; then
    echo "" >&2
    echo "本项目强制启用 Python + Go inventory（hybrid 另需 rustc/g++），缺少工具链时拒绝安装。" >&2
    exit 1
fi
echo "[0/6] 工具链 OK · python3=$(python3 -c 'import sys;print(sys.version.split()[0])') · go=$(go version | awk "{print \$3}") · rustc=$(rustc --version | awk "{print \$2}")"

# 检测系统
if [ -f /etc/debian_version ]; then
    echo "[1/6] 安装系统依赖 (Debian/Ubuntu)..."
    sudo apt update -qq
    sudo apt install -y -qq \
        python3 python3-pip python3-venv \
        libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2t64 libnspr4 libnss3 libxshmfence1 \
        2>/dev/null || true
    sudo apt install -y -qq libatk1.0-0 libatk-bridge2.0-0 libcups2 libasound2 2>/dev/null || true
elif [ -f /etc/redhat-release ]; then
    echo "[1/6] 安装系统依赖 (RHEL/CentOS)..."
    sudo yum install -y -q \
        python3 python3-pip \
        atk cups-libs libdrm libXcomposite libXdamage libXfixes libXrandr \
        mesa-libgbm pango cairo alsa-lib nspr nss libxshmfence \
        2>/dev/null || true
else
    echo "[1/6] 未知系统，跳过系统依赖（如 Chrome 启动失败请手动安装）"
fi

# Python 虚拟环境
echo "[2/6] 创建 Python 环境..."
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum requirements.txt | awk '{print $1}' > .venv/.requirements.sha256
elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 requirements.txt | awk '{print $1}' > .venv/.requirements.sha256
fi

# CloakBrowser Chromium
echo "[3/6] 下载 CloakBrowser Chromium..."
.venv/bin/python -m cloakbrowser install

# Turnstile Solver browsers
echo "[4/6] 准备内置 Turnstile Solver 浏览器(patchright)..."
.venv/bin/python -m patchright install chromium 2>/dev/null \
  || echo "    [!] patchright chromium 安装失败,可稍后执行: .venv/bin/python -m grok_register.turnstile_solver install"

# Go + Rust native (mandatory)
echo "[5/6] 编译原生组件 (Go proxy/register/inventory + hybrid)..."
bash scripts/build-native.sh

# 创建输出目录
mkdir -p keys logs/turnstile-solver

# Final gate
echo "[6/6] 校验多语言栈..."
# shellcheck source=scripts/polyglot_gate.sh
source scripts/polyglot_gate.sh
if ! require_polyglot_stack; then
    exit 1
fi

echo ""
echo "安装完成！"
echo ""
echo "  运行注册:     bash start.sh"
echo "  控制面板:     bash start.sh --dashboard"
echo "  邮箱服务:     bash start.sh --email-service"
echo "  认证服务:     bash auth-service.sh"
echo "  代理爬取:     bash start.sh --scrape-proxies"
echo "  重建原生:     bash scripts/build-native.sh"
echo "  栈自检:       bash scripts/polyglot_gate.sh check"
echo ""
echo "  分工: Python=编排/浏览器/面板 · Go=代理测活/HTTP注册 · Rust=账号库存/CPA·sub2api"
