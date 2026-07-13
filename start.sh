#!/bin/bash
# 一键启动:自动装依赖 → 校验 Python+Go+Rust → 引导配置 → 运行
# 硬性条件: 必须同时具备 Python venv、Go workers、Rust inventory-worker
# 用法:
#   bash start.sh              # 首次会引导选模式,之后直接启动
#   bash start.sh --dashboard  # Web 控制面板
#   bash start.sh --reconfig   # 重新选择邮箱模式
#   bash start.sh --debug      # 保留完整调试面板
#   bash scripts/polyglot_gate.sh check
set -e
cd "$(dirname "$0")"

. scripts/ensure_runtime.sh
ensure_runtime

if [ "${1:-}" = "--email-service" ]; then
    shift
    if command -v flock >/dev/null 2>&1; then
        mkdir -p logs
        exec 8>logs/email-service.lock
        if ! flock -n 8; then
            echo "[!] 邮箱服务已经在运行。"
            exit 1
        fi
    fi
    echo "[*] 启动邮箱服务... (Ctrl-C 停止)"
    exec .venv/bin/python -m grok_register.email_server "$@"
fi

if [ "${1:-}" = "--turnstile-solver" ]; then
    shift
    action="${1:-start}"
    if [ "$#" -gt 0 ]; then
        shift
    fi
    case "$action" in
        start|stop|status|install)
            exec .venv/bin/python -m grok_register.turnstile_solver "$action" "$@"
            ;;
        *)
            # allow: bash start.sh --turnstile-solver  (default start)
            # or pass-through unknown as start args
            exec .venv/bin/python -m grok_register.turnstile_solver start "$action" "$@"
            ;;
    esac
fi

if [ "${1:-}" = "--scrape-proxies" ]; then
    shift
    exec .venv/bin/python -m grok_register.proxy_scraper scrape "$@"
fi

if [ "${1:-}" = "--dashboard" ]; then
    shift
    echo "[*] 启动 Web 控制面板... (默认 http://127.0.0.1:8787/)"
    exec .venv/bin/python -m grok_register.dashboard "$@"
fi

reconfig=0
register_args=()
for arg in "$@"; do
    if [ "$arg" = "--reconfig" ]; then
        reconfig=1
    else
        register_args+=("$arg")
    fi
done

# 同一工作目录只允许一个注册进程，避免重复启动同时写账号和日志。
if command -v flock >/dev/null 2>&1; then
    mkdir -p logs
    exec 9>logs/register.lock
    if ! flock -n 9; then
        echo "[!] 注册机已经在运行。"
        exit 1
    fi
fi

choose_key_export_formats() {
    echo ""
    echo "选择输出格式:"
    echo "  [1] legacy + sub2api      (默认 · keys/accounts.txt + keys/sub2api/)"
    echo "  [2] legacy + cpa          (keys/accounts.txt + keys/cpa/)"
    echo "  [3] legacy + sub2api + cpa"
    echo "  [4] legacy only           (只写 accounts.txt/grok.txt)"
    read -rp "输入 1、2、3 或 4 [1]: " export_mode || export_mode=1
    case "$export_mode" in
        2) key_export_formats="legacy,cpa" ;;
        3) key_export_formats="legacy,sub2api,cpa" ;;
        4) key_export_formats="legacy" ;;
        *) key_export_formats="legacy,sub2api" ;;
    esac
}

# 1) 配置:无 .env 或显式 --reconfig 时进入引导
if [ ! -f .env ] || [ "$reconfig" -eq 1 ]; then
    echo ""
    echo "选择邮箱模式:"
    echo "  [1] 免费临时邮箱           (默认 · 零配置 · 直接回车 · 多 provider 自动 fallback)"
    echo "  [2] 自建域名邮箱           (需 Cloudflare Email Routing + 本地 webhook)"
    echo "  [3] MoeMail OpenAPI        (需 MoeMail 地址 + API Key)"
    read -rp "输入 1、2 或 3 [1]: " mode || mode=1
    choose_key_export_formats
    if [ "$mode" = "2" ]; then
        read -rp "  你的域名 (如 example.com): " domain
        read -rp "  webhook 地址 [http://127.0.0.1:8080]: " api
        api=${api:-http://127.0.0.1:8080}
        cat > .env <<ENV
EMAIL_MODE=custom
EMAIL_DOMAIN=${domain}
EMAIL_API=${api}
KEY_EXPORT_DIR=keys
KEY_EXPORT_FORMATS=${key_export_formats}
# CSP 容量(可选,0=按 CPU/内存启动期静态派生)
# PHYSICAL_CAP=0
# PHYSICAL_PER_CPU=2
# PHYSICAL_MEM_MB=512
# MIN_FREE_MEM_MB=500
ENV
        echo ""
        echo "[!] custom 模式还需在另一终端运行收信服务:"
        echo "      bash start.sh --email-service"
        echo "    并按 README「自建邮箱模式」配置 Cloudflare Email Worker。"
    elif [ "$mode" = "3" ]; then
        read -rp "  MoeMail 地址 [https://moemail.app]: " api
        api=${api:-https://moemail.app}
        read -rsp "  MoeMail API Key: " key
        echo ""
        read -rp "  邮箱域名 (留空自动读取): " domain
        cat > .env <<ENV
EMAIL_MODE=moemail
MOEMAIL_API=${api}
MOEMAIL_API_KEY=${key}
MOEMAIL_DOMAIN=${domain}
KEY_EXPORT_DIR=keys
KEY_EXPORT_FORMATS=${key_export_formats}
# MoeMail 邮箱有效期(毫秒): 3600000=1小时,86400000=24小时,259200000=3天,0=永久
# MOEMAIL_EXPIRY_MS=3600000
# 代理池:仅用于 Grok/xAI 链路;在项目目录创建 代理.txt,一行一个 http/socks5 代理或节点分享链接
# PROXY_POOL_FILE=代理.txt
# PROXY_POOL_STRATEGY=round_robin
# 分享链接会优先交给本项目内置 sing-box relay 转成本地代理;也兼容外部 proxy-relay
# PROXY_RELAY_URL=http://127.0.0.1:18080
# PROXY_RELAY_BUILTIN_ENABLED=1
# PROXY_RELAY_AUTO_INSTALL=1
# 可选:自动拉取订阅并多线程测试可访问 xAI 的代理,默认每20分钟刷新
# PROXY_AUTO_FETCH_ENABLED=0
# PROXY_AUTO_FETCH_SOURCES_FILE=proxy-sources.txt
# PROXY_AUTO_FETCH_WORKERS=8
# PROXY_AUTO_TEST_WORKERS=16
# PROXY_AUTO_REQUIRE_ACTIVE=1
# PROXY_POOL_USE_TESTED_ONLY=1
# 可选:CF-Ares 已内置在 vendor/CF-Ares;开启后可做邮箱/xAI HTTP Cloudflare 兜底
# CF_ARES_EMAIL=fallback
# CF_ARES_XAI=fallback
# CF_ARES_PATH=
# CSP 容量(可选,0=按 CPU/内存启动期静态派生)
# PHYSICAL_CAP=0
# PHYSICAL_PER_CPU=2
# PHYSICAL_MEM_MB=512
# MIN_FREE_MEM_MB=500
ENV
    else
        cat > .env <<ENV
EMAIL_MODE=tempmail
KEY_EXPORT_DIR=keys
KEY_EXPORT_FORMATS=${key_export_formats}
ENV
    fi
    cat >> .env <<ENV
PROXY_POOL_FILE=代理.txt
PROXY_POOL_STRATEGY=random
PROXY_RELAY_ENABLED=1
PROXY_RELAY_BUILTIN_ENABLED=1
PROXY_RELAY_AUTO_INSTALL=1
PROXY_RELAY_WORK_DIR=logs/proxy-relay
PROXY_RELAY_MAX_NODES=48
PROXY_AUTO_FETCH_ENABLED=1
PROXY_AUTO_FETCH_SOURCES_FILE=proxy-sources.txt
PROXY_AUTO_FETCH_WORKERS=16
PROXY_AUTO_TEST_WORKERS=32
PROXY_AUTO_TEST_TIMEOUT=6
PROXY_AUTO_TEST_ACCEPT_STATUS=200-399
PROXY_AUTO_MAX_ACTIVE=3
PROXY_AUTO_EXPORT_FORMATS=raw,sub2api,cpa
PROXY_AUTO_REQUIRE_ACTIVE=1
PROXY_AUTO_INCLUDE_BOOTSTRAP_CANDIDATES=1
PROXY_POOL_USE_TESTED_ONLY=1
CF_ARES_EMAIL=fallback
CF_ARES_XAI=fallback
CF_ARES_BROWSER_ENGINE=auto
CF_ARES_HEADLESS=1
ENV
    echo "[*] 已写入 .env"
fi

# 2) 运行
echo "[*] 启动注册服务... (Ctrl-C 停止)"
exec .venv/bin/python -m grok_register.register "${register_args[@]}"
