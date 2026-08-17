#!/usr/bin/env python3
"""
Claude Code statusLine スクリプト

画面下部のステータスラインに以下を表示する:
  - 現在の作業ディレクトリ（ホームは ~ に短縮、長い場合は中間を頭文字に省略）
  - 5h枠 / 週枠: レート制限の消費率（カラー付きバー）と「→リセット時刻」
  - サブスクプラン名 / 現在のモデル名
    / 文脈: コンテキスト窓の使用率（/以降は autocompact 発火ライン）
    / 累計: セッションの API 料金換算参考コスト
    / ⚒N: 実行中のサブエージェント数（動いている時のみ表示）

表示例:
  ~/my-project │ 5h枠 ▓▓▓░░░░░ 42% → 16:00 │ 週枠 ▓▓▓▓▓▓▓░ 86% → 7/10 │ Max 20x · Opus 4.7 · 文脈 38/60% · 累計$1.24

Claude Code は statusLine コマンドの stdin に毎回 JSON を渡す。本スクリプトが参照する主なフィールド:
  {
    "model":          { "id": "claude-opus-4-7", "display_name": "Opus 4.7" },
    "cost":           { "total_cost_usd": 1.24, ... },
    "transcript_path":"/path/to/session.jsonl",
    "workspace":      { "current_dir": "/path/to/cwd", "project_dir": "..." },
    "cwd":            "/path/to/cwd",
    "context_window": { "used_percentage": 38, "context_window_size": 1000000, ... },
    "rate_limits":    { "five_hour": { "used_percentage": 42, "resets_at": 1783392000 },
                        "seven_day": { "used_percentage": 86, "resets_at": 1783828800 } }
  }
フィールドは存在しない・型が違うことがある前提で扱い、取れない項目は区画ごと
省略して必ず1行だけ出力する（context_window が無い旧バージョンでは transcript
走査による推定にフォールバック）。サブスクプラン名だけは stdin に来ないため
~/.claude.json の oauthAccount から読む。
"""

# macOS 標準の python3（3.9）でも def 時に型注釈が評価されて落ちないようにする
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime

BAR_WIDTH = 8       # プログレスバーの目盛り数（▓ と ░ の合計）
DIR_MAX_LEN = 40    # 作業ディレクトリ表示がこれを超えたら中間を頭文字1字に省略
RESET = "\033[0m"   # 色指定を打ち消す ANSI リセット
AGENT_ACTIVE_SEC = 30  # subagent transcript の mtime がこの秒数以内なら「実行中」とみなす

# ~/.claude.json は履歴等で数MBになりうるため、毎レンダリング（数百ms間隔）の
# 全パースを避けて「元ファイルの mtime + 解析結果」を小さなキャッシュに残す
PLAN_CACHE_PATH = os.path.expanduser("~/.claude/statusline-plan.cache")


def num(v: object) -> float | None:
    """JSON 由来の値を数値として検証する。bool は int の派生なので明示的に弾く。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def sanitize(text: str) -> str:
    """制御文字（改行・ESC 等）を除去する。1行出力の契約と ANSI 注入防止のため。"""
    return re.sub(r"[\x00-\x1f\x7f]", "", text)


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


def format_section(label: str, pct: float | None, reset: str | None = None) -> str:
    """
    レート制限の 1 区画（"5h枠: ▓▓▓░░░░░ 42% → 16:00" 等）を組み立てる。
    使用率が無くてもリセット時刻が取れていれば「N/A → 16:00」として残す
    （制限に当たっている時こそ復活時刻が要る情報のため）。
    """
    tail = f" → {reset}" if reset else ""
    if pct is None:
        return f"{label}: N/A{tail}"
    return f"{label}: {color_for(pct)}{make_bar(pct)} {pct:3.0f}%{RESET}{tail}"


def format_reset(ts: object, weekly: bool = False) -> str | None:
    """
    resets_at（unix 秒）をローカル時刻の短い表記にする。
    週枠は「M/D」。5h 枠は「HH:MM」だが、深夜など日をまたぐ場合は
    「M/D HH:MM」にして翌日であることが分かるようにする。
    値が無い・不正なら None（区画側で省略）。
    """
    ts_num = num(ts)
    if ts_num is None:
        return None
    try:
        dt = datetime.fromtimestamp(ts_num)
    except (ValueError, OSError, OverflowError):
        return None
    if weekly:
        return f"{dt.month}/{dt.day}"
    if dt.date() != datetime.now().date():
        return f"{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}"
    return f"{dt.hour:02d}:{dt.minute:02d}"


def format_dir(data: dict) -> str | None:
    """
    作業ディレクトリの区画を組み立てる。

    パスは workspace.current_dir（cd 追従後の現在地）を優先し、無ければ cwd。
    ホームは ~ に短縮し、DIR_MAX_LEN を超える場合は fish 風に中間ディレクトリを
    頭文字1字へ省略する（例: ~/p/my-project/src のように末尾だけ残す）。
    どちらのフィールドも取れなければ None を返し、区画ごと省略する。
    """
    ws = data.get("workspace")
    path = ws.get("current_dir") if isinstance(ws, dict) else None
    if not isinstance(path, str) or not path:
        path = data.get("cwd")
    if not isinstance(path, str) or not path:
        return None
    path = sanitize(path)
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


def context_pct(data: dict) -> float | None:
    """
    コンテキスト窓の使用率(%)を返す。

    v2 系の Claude Code は context_window.used_percentage を stdin で直接渡して
    くるため、あればそれをそのまま使う（窓サイズの推測が要らず正確）。
    無い旧バージョンでは transcript_path の JSONL を末尾まで読み、最後に現れた
    usage レコードのトークン数（input + cache_read + cache_creation）を窓サイズで
    割って推定する。窓はモデル ID に "[1m]" を含めば 100 万、他は 20 万とみなす。
    情報が取れない場合は None を返し、呼び出し側で区画ごと省略する。
    """
    cw = data.get("context_window")
    if isinstance(cw, dict):
        official = num(cw.get("used_percentage"))
        if official is not None:
            return official

    path = data.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    model = data.get("model")
    model_id = model.get("id") if isinstance(model, dict) else None
    window = 1_000_000 if isinstance(model_id, str) and "[1m]" in model_id else 200_000
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
                if not isinstance(rec, dict):
                    continue
                msg = rec.get("message")
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if isinstance(usage, dict):
                    last_usage = usage
        if not last_usage:
            return None
        # キャッシュ読み書きを含む全入力トークンが「窓を埋めている量」
        tokens = (
            (num(last_usage.get("input_tokens")) or 0)
            + (num(last_usage.get("cache_read_input_tokens")) or 0)
            + (num(last_usage.get("cache_creation_input_tokens")) or 0)
        )
        return min(100.0, tokens * 100.0 / window)  # 100% で頭打ち
    except OSError:
        return None  # transcript が読めない場合も静かに省略


def autocompact_pct() -> float | None:
    """
    autocompact（自動要約）の発火閾値(%)を取得する。

    Claude Code は閾値を statusLine の stdin に渡さないため、環境変数
    CLAUDE_AUTOCOMPACT_PCT_OVERRIDE（settings.json の env で設定していれば
    子プロセスにも渡る）を見て、無ければ ~/.claude/settings.json を直接読む。
    どちらにも無い・型が不正なら None（デフォルト閾値運用とみなし併記しない）。
    """
    val: object = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    if not val:
        try:
            with open(os.path.expanduser("~/.claude/settings.json")) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return None
        env = cfg.get("env") if isinstance(cfg, dict) else None
        val = env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") if isinstance(env, dict) else None
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return None
    return num(val)


def parse_plan(path: str) -> str | None:
    """
    ~/.claude.json の oauthAccount からサブスクプランの表示名を組み立てる。
    公式ドキュメントに無いフィールドなので、構造・型が想定と違ったら None
    （例: organizationRateLimitTier="default_claude_max_20x" → "Max 20x"）。
    """
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    acct = cfg.get("oauthAccount") if isinstance(cfg, dict) else None
    if not isinstance(acct, dict):
        return None
    tier = acct.get("organizationRateLimitTier") or acct.get("userRateLimitTier")
    if isinstance(tier, str):
        m = re.search(r"(pro|max)(?:_(\d+)x)?", tier)
        if m:
            name = m.group(1).capitalize()
            return f"{name} {m.group(2)}x" if m.group(2) else name
    org = acct.get("organizationType")
    if isinstance(org, str) and org.startswith("claude_"):
        return org[len("claude_"):].replace("_", " ").capitalize()
    return None


def plan_name() -> str | None:
    """
    サブスクプラン表示名を返す。~/.claude.json は大きくなりうるため、
    mtime が変わらない間は PLAN_CACHE_PATH（"mtime\\tプラン名" の1行）を使い、
    毎レンダリングの全パースを避ける。キャッシュ I/O の失敗は無視して直読み。
    """
    src = os.path.expanduser("~/.claude.json")
    try:
        src_mtime = str(int(os.stat(src).st_mtime))
    except OSError:
        return None
    try:
        with open(PLAN_CACHE_PATH) as f:
            stamp, _, cached = f.read().partition("\t")
        if stamp == src_mtime:
            return cached or None
    except OSError:
        pass
    plan = parse_plan(src)
    try:
        with open(PLAN_CACHE_PATH, "w") as f:
            f.write(f"{src_mtime}\t{plan or ''}")
    except OSError:
        pass
    return plan


def active_agents(data: dict) -> int:
    """
    実行中のサブエージェント数を数える。

    Claude Code は statusLine の stdin にサブエージェント情報を渡さないため、
    セッション transcript の隣（<transcript と同名のディレクトリ>/subagents/）に
    作られる agent-*.jsonl を見る。実行中のエージェントは数秒おきにここへ追記
    するので、mtime が AGENT_ACTIVE_SEC 以内のファイル数 ≒ 実行中の数とみなす
    （長いツール実行中は書き込みが止まり一時的に数え漏れる可能性はある）。
    ディレクトリが無い・読めない場合は 0（区画ごと省略）。
    """
    path = data.get("transcript_path")
    if not isinstance(path, str) or not path.endswith(".jsonl"):
        return 0
    subagents_dir = os.path.join(path[: -len(".jsonl")], "subagents")
    count = 0
    now = time.time()
    try:
        with os.scandir(subagents_dir) as entries:
            for e in entries:
                if not e.name.startswith("agent-") or not e.name.endswith(".jsonl"):
                    continue
                try:
                    if now - e.stat().st_mtime <= AGENT_ACTIVE_SEC:
                        count += 1
                except OSError:
                    continue
    except OSError:
        return 0
    return count


def format_meta(data: dict) -> str | None:
    """
    右側の付加情報「プラン · モデル名 · 文脈 NN/60% · 累計$x.xx」を組み立てる。
    取得できた項目だけを " · " で連結し、何も無ければ None。
    """
    parts: list[str] = []

    # サブスクプラン名（取れた時だけ）
    plan = plan_name()
    if plan:
        parts.append(sanitize(plan))

    # モデル名（display_name）
    model = data.get("model")
    name = model.get("display_name") if isinstance(model, dict) else None
    if isinstance(name, str) and name:
        parts.append(sanitize(name))

    # コンテキスト使用率。autocompact 閾値が分かる場合は「使用率/閾値%」で
    # 自動要約までの余裕を示し、色も閾値に対する比率で付ける
    # （閾値60なら57%時点でほぼ満杯＝警戒色になるべきで、絶対値の色では嘘になる）
    pct_ctx = context_pct(data)
    if pct_ctx is not None:
        limit = autocompact_pct()
        if limit is not None and limit > 0:
            color = color_for(min(100.0, pct_ctx * 100.0 / limit))
            parts.append(f"文脈 {color}{pct_ctx:3.0f}{RESET}/{limit:.0f}%")
        else:
            parts.append(f"文脈 {color_for(pct_ctx)}{pct_ctx:3.0f}%{RESET}")

    # セッション累計コスト（サブスク利用時は API 換算の参考値）
    cost_obj = data.get("cost")
    cost = num(cost_obj.get("total_cost_usd")) if isinstance(cost_obj, dict) else None
    if cost is not None:
        parts.append(f"累計${cost:.2f}")

    # 実行中サブエージェント数（0 の時は区画ごと出さない）
    agents = active_agents(data)
    if agents:
        parts.append(f"\033[35m⚒{agents}{RESET}")  # マゼンタ: 他区画と被らない配色

    return " · ".join(parts) if parts else None


def render() -> str:
    """stdin の JSON を読み、ステータスライン1行分の文字列を組み立てる。"""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return "(statusline: parse error)"
    if not isinstance(data, dict):
        return "(statusline: unexpected input)"

    # レート制限（5時間 / 7日）の使用率とリセット時刻を取り出す
    rate_limits = data.get("rate_limits")
    if not isinstance(rate_limits, dict):
        rate_limits = {}
    rl_5h = rate_limits.get("five_hour")
    rl_5h = rl_5h if isinstance(rl_5h, dict) else {}
    rl_7d = rate_limits.get("seven_day")
    rl_7d = rl_7d if isinstance(rl_7d, dict) else {}

    # 最左に作業ディレクトリ、続いてレート制限バー、右にメタ情報（あれば）
    sections = []
    cwd_section = format_dir(data)
    if cwd_section:
        sections.append(cwd_section)
    sections += [
        format_section("5h枠", num(rl_5h.get("used_percentage")),
                       format_reset(rl_5h.get("resets_at"))),
        format_section("週枠", num(rl_7d.get("used_percentage")),
                       format_reset(rl_7d.get("resets_at"), weekly=True)),
    ]
    meta = format_meta(data)
    if meta:
        sections.append(meta)
    return " │ ".join(sections)


def main() -> None:
    # 「何があっても必ず1行だけ出力する」契約の最終防衛線。
    # 個々の関数で型ガードはしているが、想定外の入力でも無表示にはしない。
    try:
        print(render())
    except Exception:  # noqa: BLE001
        print("(statusline error)")


if __name__ == "__main__":
    main()
