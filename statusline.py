#!/usr/bin/env python3
"""
Claude Code statusLine スクリプト

画面下部のステータスラインに以下を表示する:
  - 現在の作業ディレクトリ（ホームは ~ に短縮、長い場合は中間を頭文字に省略）
  - 5h枠 / 週枠: レート制限の消費率（カラー付きプログレスバー）
  - 現在のモデル名 / 文脈: コンテキスト窓の使用率（/以降は autocompact 発火ライン）
    / 累計: セッションの API 料金換算参考コスト

表示例:
  ~/my-project │ 5h枠 ▓▓▓░░░░░ 42% │ 週枠 ▓▓▓▓▓▓▓░ 86% │ Opus 4.7 · 文脈 38/60% · 累計$1.24

Claude Code は statusLine コマンドの stdin に毎回 JSON を渡す。本スクリプトが参照する主なフィールド:
  {
    "model":          { "id": "claude-opus-4-7[1m]", "display_name": "Opus 4.7" },
    "cost":           { "total_cost_usd": 1.24, ... },
    "transcript_path":"/path/to/session.jsonl",
    "workspace":      { "current_dir": "/path/to/cwd", "project_dir": "..." },
    "cwd":            "/path/to/cwd",
    "rate_limits":    { "five_hour": { "used_percentage": 42 },
                        "seven_day": { "used_percentage": 86 } }
  }
存在しないフィールドはすべて省略して描画するため、古い/別系統の入力でも落ちない。
"""

import json
import sys

BAR_WIDTH = 8  # プログレスバーの目盛り数（▓ と ░ の合計）


def make_bar(pct: float) -> str:
    """使用率(0-100)を BAR_WIDTH 目盛りの ▓/░ バー文字列に変換する。"""
    filled = max(0, min(BAR_WIDTH, round(pct * BAR_WIDTH / 100)))
    return "▓" * filled + "░" * (BAR_WIDTH - filled)


def color_for(pct: float) -> str:
    """使用率に応じた ANSI カラーのエスケープを返す（高いほど警戒色）。"""
    if pct >= 90:
        return "\033[31m"   # 赤: 危険域
    elif pct >= 70:
        return "\033[33m"   # 黄: 注意
    elif pct >= 50:
        return "\033[36m"   # シアン: 中程度
    else:
        return "\033[32m"   # 緑: 余裕


RESET = "\033[0m"  # 色指定を打ち消す ANSI リセット


def format_section(label: str, pct: float | None) -> str:
    """レート制限の 1 区画（"5h: ▓▓▓░░░░░ 42%" 等）を組み立てる。値が無ければ N/A。"""
    if pct is None:
        return f"{label}: N/A"
    return f"{label}: {color_for(pct)}{make_bar(pct)} {pct:3.0f}%{RESET}"


def context_pct(data: dict) -> float | None:
    """
    transcript の最終 usage からコンテキスト窓の使用率(%)を推定する。

    Claude Code は stdin にコンテキスト使用率を直接渡してくれないため、
    transcript_path の JSONL を末尾まで読み、最後に現れた usage レコードの
    トークン数（input + cache_read + cache_creation）をコンテキスト窓で割って算出する。
    窓サイズはモデル ID に "[1m]" を含めば 100 万、それ以外は 20 万トークンとみなす。
    情報が取れない場合は None を返し、呼び出し側で区画ごと省略する。
    """
    path = data.get("transcript_path")
    if not path:
        return None
    model_id = (data.get("model") or {}).get("id") or ""
    window = 1_000_000 if "[1m]" in model_id else 200_000
    try:
        last_usage = None
        # JSONL を順に走査し、usage を持つ最後のレコードを保持する（=直近の文脈量）
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue  # 壊れた行はスキップ
                usage = (rec.get("message") or {}).get("usage")
                if usage:
                    last_usage = usage
        if not last_usage:
            return None
        # キャッシュ読み書きを含む全入力トークンが「窓を埋めている量」
        tokens = (
            (last_usage.get("input_tokens") or 0)
            + (last_usage.get("cache_read_input_tokens") or 0)
            + (last_usage.get("cache_creation_input_tokens") or 0)
        )
        return min(100.0, tokens * 100.0 / window)  # 100% で頭打ち
    except OSError:
        return None  # transcript が読めない場合も静かに省略


DIR_MAX_LEN = 40  # これを超えたら中間ディレクトリを頭文字1字に省略する


def format_dir(data: dict) -> str | None:
    """
    作業ディレクトリの区画を組み立てる。

    パスは workspace.current_dir（cd 追従後の現在地）を優先し、無ければ cwd。
    ホームは ~ に短縮し、DIR_MAX_LEN を超える場合は fish 風に中間ディレクトリを
    頭文字1字へ省略する（例: ~/p/srb-copilot/src → 最後の要素だけ残す）。
    どちらのフィールドも無ければ None を返し、区画ごと省略する。
    """
    import os

    path = (data.get("workspace") or {}).get("current_dir") or data.get("cwd")
    if not path:
        return None
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + "/"):
        path = "~" + path[len(home):]
    if len(path) > DIR_MAX_LEN:
        parts = path.split("/")
        # 先頭（~ や空文字）と末尾は残し、中間だけ頭文字に縮める
        path = "/".join(
            p if i in (0, len(parts) - 1) else (p[:1] or p)
            for i, p in enumerate(parts)
        )
    return f"\033[34m{path}{RESET}"  # 青: 使用率バーの警戒色と混ざらない配色


def autocompact_pct() -> float | None:
    """
    autocompact（自動要約）の発火閾値(%)を取得する。

    Claude Code は閾値を statusLine の stdin に渡さないため、環境変数
    CLAUDE_AUTOCOMPACT_PCT_OVERRIDE（settings.json の env で設定していれば
    子プロセスにも渡る）を見て、無ければ ~/.claude/settings.json を直接読む。
    どちらにも無ければ None（デフォルト閾値運用とみなし併記しない）。
    """
    import os

    val = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    if not val:
        try:
            with open(os.path.expanduser("~/.claude/settings.json")) as f:
                val = (json.load(f).get("env") or {}).get(
                    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
                )
        except (OSError, json.JSONDecodeError, ValueError):
            return None
    try:
        return float(val) if val else None
    except (TypeError, ValueError):
        return None


def format_meta(data: dict) -> str | None:
    """
    右側の付加情報「モデル名 · 文脈 NN/60% · 累計$x.xx」を組み立てる。
    取得できた項目だけを " · " で連結し、何も無ければ None。
    """
    parts: list[str] = []

    # モデル名（display_name）
    name = (data.get("model") or {}).get("display_name")
    if name:
        parts.append(name)

    # コンテキスト使用率（バーと同じ配色で着色）。
    # autocompact 閾値が分かる場合は「使用率/閾値%」で自動要約までの余裕を示す
    pct_ctx = context_pct(data)
    if pct_ctx is not None:
        limit = autocompact_pct()
        if limit is not None:
            parts.append(
                f"文脈 {color_for(pct_ctx)}{pct_ctx:3.0f}{RESET}/{limit:.0f}%"
            )
        else:
            parts.append(f"文脈 {color_for(pct_ctx)}{pct_ctx:3.0f}%{RESET}")

    # セッション累計コスト（サブスク利用時は API 換算の参考値）
    cost = (data.get("cost") or {}).get("total_cost_usd")
    if isinstance(cost, (int, float)):
        parts.append(f"累計${cost:.2f}")

    return " · ".join(parts) if parts else None


def main() -> None:
    # Claude Code から渡される JSON を読む。壊れていればその旨だけ出して終了。
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("(rate limit: parse error)")
        return

    # レート制限（5時間 / 7日）の使用率を取り出す
    rate_limits = data.get("rate_limits") or {}
    pct_5h = (rate_limits.get("five_hour") or {}).get("used_percentage")
    pct_7d = (rate_limits.get("seven_day") or {}).get("used_percentage")

    # 最左に作業ディレクトリ、続いてレート制限バー、右にメタ情報（あれば）を 1 行で出力
    sections = []
    cwd_section = format_dir(data)
    if cwd_section:
        sections.append(cwd_section)
    sections += [format_section("5h枠", pct_5h), format_section("週枠", pct_7d)]
    meta = format_meta(data)
    if meta:
        sections.append(meta)
    print(" │ ".join(sections))


if __name__ == "__main__":
    main()
