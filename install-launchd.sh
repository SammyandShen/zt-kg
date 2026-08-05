#!/bin/bash
# install-launchd.sh — 安装/卸载/测试 zt-kg 定时任务（双班次）
#   com.user.zt-kg-morning  工作日 07:40  补齐昨日涨停信息（run-morning.sh）
#   com.user.zt-kg-daily    工作日 17:00  抓当日涨停池+重建+慢速富化（run-daily.sh）
# 用法：bash install-launchd.sh [install|test|test-morning|status|remove]
set -e

PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LABELS=(com.user.zt-kg-morning com.user.zt-kg-daily)

ACTION="${1:-install}"
case "$ACTION" in
    install)
        mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"
        chmod +x "$PROJECT_DIR/run-daily.sh" "$PROJECT_DIR/run-morning.sh"
        for LABEL in "${LABELS[@]}"; do
            PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
            sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
                -e "s|__HOME__|$HOME|g" \
                "$PROJECT_DIR/launchd/$LABEL.plist" > "$PLIST_DEST"
            launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
            launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
        done
        echo "✅ 已加载双班次：工作日 07:40 补齐昨日信息；17:00 抓当日涨停池并重建网页"
        echo "   测试：bash install-launchd.sh test / test-morning"
        ;;
    remove)
        for LABEL in "${LABELS[@]}"; do
            launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "✅ 已卸载 $LABEL" || echo "ℹ️ $LABEL 未在运行"
            rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
        done
        ;;
    test)
        cd "$PROJECT_DIR" && bash run-daily.sh
        ;;
    test-morning)
        cd "$PROJECT_DIR" && bash run-morning.sh
        ;;
    status)
        for LABEL in "${LABELS[@]}"; do
            launchctl list | grep "$LABEL" && echo "✅ $LABEL 已加载" || echo "❌ $LABEL 未加载"
        done
        echo "--- 最近日志（早班） ---"
        tail -5 "$PROJECT_DIR/logs/morning-stdout.log" 2>/dev/null || echo "（还没有日志）"
        echo "--- 最近日志（晚班） ---"
        tail -5 "$PROJECT_DIR/logs/daily-stdout.log" 2>/dev/null || echo "（还没有日志）"
        ;;
    *)
        echo "用法: bash install-launchd.sh [install|test|test-morning|status|remove]"; exit 1 ;;
esac
