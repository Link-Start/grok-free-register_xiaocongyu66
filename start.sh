#!/bin/bash
# 一键启动:自动装依赖 → 引导配置 → 运行
# 用法:
#   bash start.sh              # 首次会引导选模式,之后直接启动
#   bash start.sh --reconfig   # 重新选择邮箱模式
#   bash start.sh --debug      # 保留完整调试面板
set -e
cd "$(dirname "$0")"

reconfig=0
register_args=()
for arg in "$@"; do
    if [ "$arg" = "--reconfig" ]; then
        reconfig=1
    else
        register_args+=("$arg")
    fi
done

# 同一工作目录只允许一个注册进程，避免多次 nohup 同时写账号和日志。
if command -v flock >/dev/null 2>&1; then
    mkdir -p logs
    exec 9>logs/register.lock
    if ! flock -n 9; then
        echo "[!] 注册机已经在运行。"
        exit 1
    fi
fi

# 1) 依赖:没有 venv 就自动安装
if [ ! -d .venv ]; then
    echo "[*] 首次运行,安装依赖..."
    bash setup.sh
fi

# 2) 配置:无 .env 或显式 --reconfig 时进入引导
if [ ! -f .env ] || [ "$reconfig" -eq 1 ]; then
    echo ""
    echo "选择邮箱模式:"
    echo "  [1] 免费临时邮箱           (默认 · 零配置 · 直接回车 · 多 provider 自动 fallback)"
    echo "  [2] 自建域名邮箱           (需 Cloudflare Email Routing + 本地 webhook)"
    read -rp "输入 1 或 2 [1]: " mode || mode=1
    if [ "$mode" = "2" ]; then
        read -rp "  你的域名 (如 example.com): " domain
        read -rp "  webhook 地址 [http://127.0.0.1:8080]: " api
        api=${api:-http://127.0.0.1:8080}
        cat > .env <<ENV
EMAIL_MODE=custom
EMAIL_DOMAIN=${domain}
EMAIL_API=${api}
# CSP 容量(可选,0=按 CPU/内存启动期静态派生)
# PHYSICAL_CAP=0
# PHYSICAL_PER_CPU=2
# PHYSICAL_MEM_MB=512
# MIN_FREE_MEM_MB=500
ENV
        echo ""
        echo "[!] custom 模式还需在另一终端运行收信服务:"
        echo "      .venv/bin/python email_server.py"
        echo "    并按 README「自建邮箱模式」配置 Cloudflare Email Worker。"
    else
        echo "EMAIL_MODE=tempmail" > .env
    fi
    echo "[*] 已写入 .env"
fi

# 3) 运行
echo "[*] 启动注册服务... (Ctrl-C 停止)"
exec .venv/bin/python register.py "${register_args[@]}"
