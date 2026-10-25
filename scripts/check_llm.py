#!/usr/bin/env python3
"""check_llm.py — LLM 层健康哨兵（2026-10-25 设）。

背景：claude CLI 的 OAuth 会话 2026-08-22 起过期，summarize_news / classify_tags /
judge_attributions / discover_news_terms 连续两个月每日失败，而它们都包在
`|| echo 跳过` 里——日志只有一行警告，确定性工序照常发布，没人察觉。

零人工干预的边界：OAuth 续期必须人做（`claude auth login`），系统能做的是
**不再沉默**：班次开头探测一次 CLI，结果落 data/llm_status.json（build_site
导出→网页状态条红牌），离线时日志大声报警并给出修复命令。

用法：python3 scripts/check_llm.py   （退出码恒为 0，不阻断班次）
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date

import common
from summarize_news import find_claude

STATUS_PATH = common.REPO_ROOT / "data" / "llm_status.json"
PROBE_MODEL = "claude-sonnet-5"      # 与 judge/classify 同款，最具代表性
TIMEOUT_SEC = 90


def probe() -> tuple[bool, str]:
    try:
        claude_bin = find_claude()
    except Exception as e:                      # noqa: BLE001
        return False, f"找不到 claude CLI：{e}"
    try:
        r = subprocess.run([claude_bin, "-p", "--model", PROBE_MODEL, "只回复两个字母：OK"],
                           capture_output=True, text=True, timeout=TIMEOUT_SEC,
                           stdin=subprocess.DEVNULL)   # 不给 stdin 否则 CLI 等 3s 并刷警告
    except subprocess.TimeoutExpired:
        return False, f"探测超时 {TIMEOUT_SEC}s"
    if r.returncode == 0 and "OK" in r.stdout.upper():
        return True, ""
    lines = [ln.strip() for ln in (r.stderr + "\n" + r.stdout).splitlines()
             if ln.strip() and "no stdin data" not in ln]
    err = lines[-1] if lines else f"退出码 {r.returncode}"
    return False, err[:200]


def first_gap_date() -> str | None:
    """首次发现离线时回溯起点：briefs 最后有数据之后的第一个涨停交易日。
    （哨兵 2026-10-25 才上线，但事故从 08-22 开始——不能把起点记成上线日）"""
    try:
        conn = common.open_db()
        row = conn.execute(
            "SELECT MIN(trade_date) FROM limit_up_events WHERE pool='zt' "
            "AND trade_date > (SELECT COALESCE(MAX(trade_date),'') FROM briefs)").fetchone()
        return row[0] if row and row[0] else None
    except Exception:                           # noqa: BLE001
        return None


def main() -> int:
    prev = {}
    if STATUS_PATH.exists():
        try:
            prev = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except ValueError:
            prev = {}
    ok, err = probe()
    now = common.now_iso()
    down_since = None if ok else (prev.get("down_since") or first_gap_date() or now)
    status = {"ok": ok, "checked_at": now, "error": err,
              "down_since": down_since, "probe_model": PROBE_MODEL}
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
    if ok:
        if prev.get("ok") is False:
            print(f"✅ LLM 层恢复在线（此前离线自 {str(prev.get('down_since'))[:10]}）")
        else:
            print("✅ LLM 层在线")
        return 0
    days = (date.today() - date.fromisoformat(down_since[:10])).days
    print("🚨" * 12)
    print(f"🚨 LLM 层离线 {days} 天（自 {down_since[:10]}）：{err}")
    print("🚨 影响：一句话归因 / 标签分型 / judge 判决 / 雷达提词 全部停摆；"
          "抓取、归因候选、轮次、龙头评分、发布等确定性工序照常")
    print("🚨 修复：终端执行  claude auth login  完成登录，"
          "随后  python3 scripts/summarize_news.py --days N  回补一句话归因")
    print("🚨" * 12)
    return 0


if __name__ == "__main__":
    sys.exit(main())
