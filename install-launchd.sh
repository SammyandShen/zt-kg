#!/bin/bash
# install-launchd.sh — 安装/卸载/测试 zt-kg 每日涨停抓取定时任务
# 用法：bash install-launchd.sh [install|test|status|remove]
set -e

PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LABEL="com.user.zt-kg-daily"
PLIST_SOURCE="$PROJECT_DIR/launchd/$LABEL.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

ACTION="${1:-install}"
case "$ACTION" in
    install)
        mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"
        sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
            -e "s|__HOME__|$HOME|g" \
            "$PLIST_SOURCE" > "$PLIST_DEST"
        chmod +x "$PROJECT_DIR/run-daily.sh"
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
        echo "✅ 已加载：工作日 17:00 自动抓当日涨停池并重建网页"
        echo "   测试：bash install-launchd.sh test"
        ;;
    remove)
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "✅ 已卸载" || echo "ℹ️ 任务未在运行"
        rm -f "$PLIST_DEST"
        ;;
    test)
        cd "$PROJECT_DIR" && bash run-daily.sh
        ;;
    status)
        launchctl list | grep "$LABEL" && echo "✅ 已加载" || echo "❌ 未加载"
        echo "--- 最近日志 ---"
        tail -5 "$PROJECT_DIR/logs/daily-stdout.log" 2>/dev/null || echo "（还没有日志）"
        ;;
    *)
        echo "用法: bash install-launchd.sh [install|test|status|remove]"; exit 1 ;;
esac
