#!/usr/bin/env python3
"""
Claude Code statusLine スクリプト

画面下部のステータスラインに以下を表示する:
  - 現在の作業ディレクトリ（ホームは ~ に短縮、長い場合は中間を頭文字に省略）
  - 5h枠 / 週枠: レート制限の消費率（カラー付きバー）と「→リセット時刻」
  - サブスクプラン名 / 現在のモデル名
    / 文脈: コンテキスト窓の使用率（/以降は autocompact 発火ライン）
    / 入/出: 出力1トークンあたりに読んだ入力側トークン（300以上で「区切りどき」、500以上で「新セッション推奨」）
      サブエージェントを使ったセッションでは、その分も足した値を「(サブ込みN)」で並べる
    / 累計: セッションの API 料金換算参考コスト
    / ⚒N: 実行中のサブエージェント数（動いている時のみ表示）

表示例:
  ~/my-project │ 5h枠 ▓▓▓░░░░░ 42% → 16:00 │ 週枠 ▓▓▓▓▓▓▓░ 86% → 7/10 │ Max 20x · Opus 4.7 · 文脈 38/60% · 入/出 320 区切りどき · 累計$1.24

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

import glob
import json
import math
import os
import re
import stat
import sys
import time
import unicodedata
from datetime import datetime

BAR_WIDTH = 8       # プログレスバーの目盛り数（▓ と ░ の合計）

# 枠の使用率のしきい値。バーを出さないので、色を見落としても気づけるよう
# 記号も付ける（警告 "!" / アラート "!!"）。環境変数で動かせる。
WARN_PCT = float(os.environ.get("STATUSLINE_WARN_PCT", "80"))    # 警告
ALERT_PCT = float(os.environ.get("STATUSLINE_ALERT_PCT", "90"))  # アラート
WARN_MARK = "!"
ALERT_MARK = "!!"
DIR_MAX_LEN = 40    # 作業ディレクトリ表示がこれを超えたら中間を頭文字1字に省略
DIR_ELLIPSIS = "…"  # 末尾コンポーネントを切り詰めた際に付ける印
RESET = "\033[0m"   # 色指定を打ち消す ANSI リセット
AGENT_ACTIVE_SEC = 30  # subagent transcript の mtime がこの秒数以内なら「実行中」とみなす
CONTEXT_SCAN_MAX_BYTES = 2_000_000  # context_window が無い旧版向けフォールバックで transcript 末尾から読む上限バイト数

# ~/.claude.json は履歴等で数MBになりうるため、毎レンダリング（数百ms間隔）の
# 全パースを避けて「元ファイルの mtime + 解析結果」を小さなキャッシュに残す
PLAN_CACHE_PATH = os.path.expanduser("~/.claude/statusline-plan.cache")

# codex（OpenAI の CLI）の枠。Claude の枠は Claude Code が stdin の JSON で渡して
# くれるが、codex は別サービスの別プロセスなので誰も渡してくれない。
# ~/.codex/sessions の最新セッションに残る rate_limits から読む。
# statusLine は短い間隔で繰り返し呼ばれるため、結果を短時間キャッシュし、
# 探索も mtime の新しい数本に限る。codex を使っていなければ区画ごと出さない。
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")
CODEX_CACHE_PATH = os.path.expanduser("~/.claude/statusline-codex.cache")
CODEX_CACHE_TTL = 120   # 秒。枠は分単位でしか動かないので、これで十分新しい。
CODEX_SCAN_FILES = 10   # mtime の新しい順に見るファイル数


def num(v: object) -> float | None:
    """
    JSON 由来の値を数値として検証する。bool は int の派生なので明示的に弾く。
    NaN / ±Infinity も弾く（Python の json モジュールは既定でこれらのリテラルを
    受理してしまうため、round() 等の呼び出し側が例外を投げる前にここで遮断する。
    これにより make_bar() に限らず num() を通す全経路が守られる）。
    """
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        return f if math.isfinite(f) else None
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
    if pct >= ALERT_PCT:
        return "\033[31m"   # 赤: アラート
    elif pct >= WARN_PCT:
        return "\033[33m"   # 黄: 警告
    elif pct >= 50:
        return "\033[36m"   # シアン: 半分超え（警告ではない）
    else:
        return "\033[32m"   # 緑: 余裕


def mark_for(pct: float) -> str:
    """しきい値を超えたときに付ける記号を返す。

    色だけだと、端末の配色・色覚・視野の端で見落とす。バーを外して塗りの
    長さという手がかりも無くなったので、文字でも分かるようにする。
    """
    if pct >= ALERT_PCT:
        return ALERT_MARK
    if pct >= WARN_PCT:
        return WARN_MARK
    return ""


def format_section(label: str, pct: float | None, reset: str | None = None) -> str:
    """
    レート制限の 1 区画（"5h枠 42%→16:00" 等）を組み立てる。

    **バーは出さない。** 危険度は色（50%〜シアン / 70%〜黄 / 90%〜赤）が、量は
    数字が表しており、隣り合うバーは同じことを二度言っている。枠が3つ並ぶと
    バーだけで 24 桁、飾りの空白で 15 桁を使い、狭い端末で右端が切れる。
    周辺視野で塗りの長さを見たい場合は make_bar() を差し戻せばよい（1行）。
    % を桁揃えしていないので、9% → 10% で右が1桁ずれる。
    使用率が無くてもリセット時刻が取れていれば「N/A → 16:00」として残す
    （制限に当たっている時こそ復活時刻が要る情報のため）。
    """
    tail = f"→{reset}" if reset else ""
    if pct is None:
        return f"{label} N/A{tail}"
    return f"{label} {color_for(pct)}{pct:.0f}%{mark_for(pct)}{RESET}{tail}"


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


def _is_regional_indicator(ch: str) -> bool:
    """国旗絵文字を構成する「地域指示記号」(🇦〜🇿 相当)かどうか。"""
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _shrink_component(p: str) -> str:
    """
    中間ディレクトリ名を先頭1文字に縮める（fish 風の省略）。

    国旗絵文字（地域指示記号2つの組）等、複数コードポイントで1つの見た目を
    構成する文字の先頭1コードポイントだけを取り出すと、単体では意味をなさない
    半端な文字が残ってしまう（例: "🇯🇵🇰🇷🇺🇸-project"[:1] → "🇯" だけになる）。
    標準ライブラリだけで正確なグラフェムクラスタ境界を判定するのは困難なため、
    先頭文字が ASCII 範囲外の場合は「安全側」として省略せず元の文字列を
    そのまま残す、という実用的な対処にとどめる（完璧な絵文字対応は目指さない）。
    """
    if not p or ord(p[0]) > 127:
        return p
    return p[:1]


def _truncate_safe(s: str, budget: int) -> str:
    """
    s を最大 budget 文字に切り詰める（末尾コンポーネント強制省略用）。

    グラフェムクラスタの完全な判定はしないが、実用上よくある2パターンだけ
    追加でケアする: (1) 切断点の直後が結合文字（アクセント記号等）の場合、
    (2) 国旗絵文字の地域指示記号ペアを半分だけ残してしまう場合。
    どちらも切断点を1文字前にずらし、明らかに半端な文字が残るのを避ける。
    """
    if budget <= 0:
        return ""
    cut = s[:budget]
    if not cut:
        return cut
    rest_first = s[budget] if budget < len(s) else ""
    if rest_first and unicodedata.combining(rest_first):
        cut = cut[:-1]
    elif _is_regional_indicator(cut[-1]) and rest_first and _is_regional_indicator(rest_first):
        cut = cut[:-1]
    return cut


def format_dir(data: dict) -> str | None:
    """
    作業ディレクトリの区画を組み立てる。

    パスは workspace.current_dir（cd 追従後の現在地）を優先し、無ければ cwd。
    ホームは ~ に短縮し、DIR_MAX_LEN を超える場合は fish 風に中間ディレクトリを
    頭文字1字へ省略する（例: ~/p/my-project/src のように末尾だけ残す）。
    それでも DIR_MAX_LEN を超える場合（現実にはプロジェクト名・ブランチ名など
    末尾コンポーネントが長いケースが最頻出）は、末尾コンポーネントも "…" 付きで
    切り詰めて必ず DIR_MAX_LEN に収める。
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
        parts = [
            p if i in (0, len(parts) - 1) else _shrink_component(p)
            for i, p in enumerate(parts)
        ]
        path = "/".join(parts)
        if len(path) > DIR_MAX_LEN:
            # 中間省略だけでは収まらない（＝末尾コンポーネントが長い）。
            # 末尾も切り詰めて必ず DIR_MAX_LEN に収める。
            prefix = "/".join(parts[:-1])
            sep = 1 if prefix else 0
            budget = DIR_MAX_LEN - len(prefix) - sep - len(DIR_ELLIPSIS)
            if budget < 1:
                # 中間部だけで予算を使い切る極端なケース。全体を素朴に切り詰める
                # （体裁より「1行に必ず収める」契約を優先する）。
                path = _truncate_safe(path, max(0, DIR_MAX_LEN - len(DIR_ELLIPSIS))) + DIR_ELLIPSIS
            else:
                tail = _truncate_safe(parts[-1], budget)
                path = (prefix + "/" if prefix else "") + tail + DIR_ELLIPSIS
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

    # 通常ファイルであることを先に確認する。transcript_path に名前付きパイプ
    # (FIFO) 等が渡ると open() が書き手待ちでブロックし、タイムアウトが無いため
    # 復帰しない（5秒ごとに呼ばれるのでプロセスが積み上がる）。stat() 自体は
    # ブロックしないのでここで安全に弾ける。
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None

    try:
        # 巨大 transcript の全文走査を避けるため、末尾 CONTEXT_SCAN_MAX_BYTES
        # バイトだけを読む（この経路が探すのは「最後に現れた usage レコード」
        # なので末尾から読むのが自然）。バイナリで読んで errors="replace" で
        # デコードすることで、非UTF-8内容や途中バイトでのシーク開始があっても
        # UnicodeDecodeError で全区画が消えることがないようにする。
        with open(path, "rb") as f:
            size = st.st_size
            truncated = size > CONTEXT_SCAN_MAX_BYTES
            if truncated:
                f.seek(size - CONTEXT_SCAN_MAX_BYTES)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        if truncated:
            # シーク位置は行の途中である可能性が高く、先頭行は不完全なため捨てる
            # （直近の usage を知りたいだけなので影響しない）。
            lines = lines[1:]

        last_usage = None
        # 順に走査し、usage を持つ最後のレコードを保持する（=直近の文脈量）
        for line in lines:
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
    planDisplayName（例: "Ultra"）を優先し、無ければ枠区分から組み立てる
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
    # 表示名が取れればそれを使う。枠区分（tier）は改称に追従しない
    # （2026-10-30 に Max 20x が Ultra に改称されても default_claude_max_20x のまま）。
    display = acct.get("planDisplayName")
    if isinstance(display, str) and display.strip():
        return display.strip()
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
        # 先頭の版は parse_plan の解釈を変えたら上げる（mtime が同じ旧キャッシュを捨てるため）
        src_mtime = f"v2:{int(os.stat(src).st_mtime)}"
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


# --- セッションの効率（入/出） ------------------------------------------------
# 出力1トークンを得るために読んだ入力側（入力＋キャッシュ書き＋キャッシュ読み）のトークン数。
# 長い会話を引っ張るほど1回あたりの文脈が膨らみ、この値が上がる（枠を早く食う）。
# 作者の直近30日の実測（2026-11-18）: 全体 245、長い会話で使う Opus 5.5 が 291、主に短い文脈のサブエージェントで使う Sonnet 5.5 が 104。
# 同じ応答が記録に複数行残るので、message.id で重複を除いて数える。
#
# 速度の制約: statusLine は5秒ごとに呼ばれ、transcript は 10MB を超える。
# 読んだ位置（バイト）と途中の合計をセッションごとにキャッシュし、増えた分だけ読む。
EFF_CACHE_DIR = os.path.expanduser("~/.claude/statusline-eff")


def _env_threshold(name: str, default: float) -> float:
    """しきい値の環境変数を読む。空・数値でない・0以下・NaN/Inf は既定値に戻す（import 時に落とさない）。"""
    try:
        v = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return v if 0 < v < float("inf") else default


EFF_WARN = _env_threshold("STATUSLINE_EFF_WARN", 300.0)
EFF_ALERT = _env_threshold("STATUSLINE_EFF_ALERT", 500.0)
EFF_MIN_OUTPUT = 2000   # 出力がこれ未満のうちは比が暴れるので出さない
EFF_KEEP_IDS = 200      # 重複除去用に覚えておく直近の message.id の数


def _eff_new() -> dict:
    return {"offset": 0, "ctx": 0, "out": 0, "ids": []}


def _eff_valid(got: object) -> bool:
    """キャッシュに入っていた1ファイル分の状態が使える形か（型や範囲が違えば捨てて数え直す）。"""
    return (isinstance(got, dict)
            and all(type(got.get(k)) is int and got[k] >= 0 for k in ("offset", "ctx", "out"))
            and isinstance(got.get("ids"), list)
            and all(isinstance(i, str) for i in got["ids"]))


def _eff_advance(path: str, st: dict) -> tuple[dict, bool]:
    """transcript 1本を、前回読んだ位置から読み足す。返り値は（新しい状態, 変わったか）。読めなければ OSError。"""
    fst = os.stat(path)
    if not stat.S_ISREG(fst.st_mode):   # FIFO 等を open すると書き手待ちで固まる
        raise OSError("not a regular file")
    size = fst.st_size
    changed = False
    if size < st["offset"]:          # 作り直された等で縮んだら最初から
        st, changed = _eff_new(), True
    if size <= st["offset"]:
        return st, changed
    ids = list(st["ids"])
    seen = set(ids)
    ctx, out = st["ctx"], st["out"]
    with open(path, "rb") as fh:
        fh.seek(st["offset"])
        chunk = fh.read()
    end = chunk.rfind(b"\n") + 1   # 書きかけの最終行は次回に回す
    if not end:
        return st, changed
    for raw in chunk[:end].splitlines():
        if b'"assistant"' not in raw or b'"usage"' not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        msg = r.get("message")
        if r.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        u = msg.get("usage")
        if not isinstance(u, dict):
            continue
        mid = msg.get("id")
        if isinstance(mid, str) and mid:
            if mid in seen:
                continue
            seen.add(mid)
            ids.append(mid)
        ctx += sum(v for v in (u.get("input_tokens"), u.get("cache_creation_input_tokens"),
                               u.get("cache_read_input_tokens")) if isinstance(v, int))
        o = u.get("output_tokens")
        out += o if isinstance(o, int) else 0
    return {"offset": st["offset"] + end, "ctx": ctx, "out": out, "ids": ids[-EFF_KEEP_IDS:]}, True


def _eff_load(cache: str) -> object:
    try:
        with open(cache, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _eff_save(cache: str, obj: object) -> None:
    """session-split.py（UserPromptSubmit hook）も同じキャッシュを書くので、置き換えで書く。"""
    os.makedirs(EFF_CACHE_DIR, exist_ok=True)
    tmp = f"{cache}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, cache)


def _eff_key(tp: str) -> str:
    return re.sub(r"[^\w-]", "", os.path.basename(tp))[:80] or "session"


def _eff_main(data: dict) -> dict | None:
    """メインの transcript の合計（ctx / out）を返す。読めなければ None。"""
    tp = data.get("transcript_path")
    if not isinstance(tp, str) or not tp:
        return None
    cache = os.path.join(EFF_CACHE_DIR, _eff_key(tp) + ".json")
    got = _eff_load(cache)
    st = {k: got[k] for k in ("offset", "ctx", "out", "ids")} if _eff_valid(got) else _eff_new()
    try:
        st, changed = _eff_advance(tp, st)
        if changed:
            _eff_save(cache, st)
    except OSError:
        return None
    return st


def session_read_per_output(data: dict) -> float | None:
    """今のセッションの「入/出」（メインだけ）を返す。読めない・出力が少ないときは None。"""
    st = _eff_main(data)
    if st is None or st["out"] < EFF_MIN_OUTPUT:
        return None
    return st["ctx"] / st["out"]


def session_combined_per_output(data: dict) -> float | None:
    """
    サブエージェントの分も足した「入/出」を返す。サブエージェントの出力が無ければ None。

    仕事をサブエージェントに出しても、メインに溜まった文脈は減らない（読み直され続け、出力だけが
    サブ側へ移る）ので、メインの値は下がりにくい。出した効果が見えるよう、こちらの値を並べて見せる。
    実測 2026-11-18（直近30日・応答150〜400のセッション）: サブの出力が2割以上の4件は
    メイン 228 / サブ込み 155、サブ無しの15件は 228。件数が少ないので傾向の参考。
    """
    main = _eff_main(data)
    if main is None or main["out"] < EFF_MIN_OUTPUT:
        return None
    tp = data["transcript_path"]
    if not tp.endswith(".jsonl"):
        return None
    sub_dir = os.path.join(tp[: -len(".jsonl")], "subagents")
    cache = os.path.join(EFF_CACHE_DIR, _eff_key(tp) + ".subs.json")
    got = _eff_load(cache)
    subs = {k: v for k, v in got.items() if _eff_valid(v)} if isinstance(got, dict) else {}
    dirty = False
    try:
        with os.scandir(sub_dir) as entries:
            names = sorted(e.name for e in entries
                           if e.name.startswith("agent-") and e.name.endswith(".jsonl"))
    except OSError:
        return None
    if set(subs) - set(names):       # 消えた・名前の変わったファイルの分は合計から外す
        subs = {k: v for k, v in subs.items() if k in names}
        dirty = True
    for name in names:
        try:
            st, changed = _eff_advance(os.path.join(sub_dir, name), subs.get(name) or _eff_new())
        except OSError:
            continue
        if changed or name not in subs:
            subs[name] = st
            dirty = True
    if dirty:
        try:
            _eff_save(cache, subs)
        except OSError:
            pass
    sub_ctx = sum(v["ctx"] for v in subs.values())
    sub_out = sum(v["out"] for v in subs.values())
    if sub_out <= 0:
        return None
    return (main["ctx"] + sub_ctx) / (main["out"] + sub_out)


def format_meta(data: dict) -> str | None:
    """
    右側の付加情報「プラン · モデル名 · 文脈 NN/60% · 入/出 NNN · 累計$x.xx」を組み立てる。
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

    # セッションの効率（出力1トークンあたりに読んだ入力側トークン。小さいほど良い）
    # 数字だけだと「高いのか・何をすればいいのか」が読めないので、閾値を超えたら次の一手を添える
    ratio = session_read_per_output(data)
    if ratio is not None:
        # サブエージェントに出した分の効果はメインの値に出にくいので、サブ込みの値を括弧で並べる
        comb = session_combined_per_output(data)
        both = f"{ratio:,.0f}" + (f"(サブ込み{comb:,.0f})" if comb is not None else "")
        if ratio >= EFF_ALERT:
            parts.append(f"入/出 \033[31m{both} 新セッション推奨{RESET}")
        elif ratio >= EFF_WARN:
            parts.append(f"入/出 \033[33m{both} 区切りどき{RESET}")
        else:
            parts.append(f"入/出 {both}")

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


def codex_limits() -> dict | None:
    """codex の枠（used_percent / window_minutes / resets_at）を返す。失敗時は None。

    ~/.codex が無い環境（codex を使っていない）では毎回 None になる。
    その場合もキャッシュに None を書き、無駄な探索を繰り返さない。
    """
    now = time.time()
    try:
        with open(CODEX_CACHE_PATH, encoding="utf-8") as fh:
            c = json.load(fh)
        if isinstance(c, dict) and now - c.get("at", 0) < CODEX_CACHE_TTL:
            return c.get("rl")
    except (OSError, ValueError):
        pass

    rl = None
    try:
        files = glob.glob(os.path.join(CODEX_SESSIONS, "**", "*.jsonl"), recursive=True)
        files.sort(key=os.path.getmtime, reverse=True)
        for path in files[:CODEX_SCAN_FILES]:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"rate_limits"' not in line:
                            continue
                        try:
                            got = (json.loads(line).get("payload") or {}).get("rate_limits")
                        except ValueError:
                            continue
                        if got:
                            rl = got      # 同一ファイル内の最後＝最新
            except OSError:
                continue
            if rl:
                break
    except OSError:
        rl = None

    try:
        os.makedirs(os.path.dirname(CODEX_CACHE_PATH), exist_ok=True)
        with open(CODEX_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"at": now, "rl": rl}, fh)
    except OSError:
        pass
    return rl


def format_codex(rl: object) -> str | None:
    """codex の枠を Claude の枠と同じ見た目の1区画にする。読めなければ出さない。

    バーも配色も format_section に委ねる。codex だけ別の見た目にすると、
    同じ行に並んだときに「どちらが重いか」を目で比べられなくなる。
    codex を使っていない日も記録は残り続けるので、古い値を今の値のように
    見せないようリセット時刻を必ず添える。
    """
    if not isinstance(rl, dict):
        return None
    pr = rl.get("primary")
    if not isinstance(pr, dict):
        return None
    pct = num(pr.get("used_percent"))
    if pct is None:
        return None
    win = pr.get("window_minutes")
    label = "codex週" if win == 10080 else ("codex日" if win == 1440 else "codex")
    reset = num(pr.get("resets_at"))
    tail = None
    if reset is not None:
        try:
            d = datetime.fromtimestamp(reset)
            tail = f"{d:%-m/%-d}" if win == 10080 else f"{d:%H:%M}"
        except (OverflowError, OSError, ValueError):
            tail = None
    return format_section(label, pct, tail)


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
    # 週枠は届いているのに 5h 枠が無い＝5時間制限そのものが無いプラン
    # （アカウント確認済みの Ultra。2026-10-30〜）なので、5h 区画ごと出さない。
    # 両方とも無いとき（prepaid 組織・初回応答前）は従来どおり両方 N/A を出す。
    if rl_5h or not rl_7d:
        sections.append(format_section("5h枠", num(rl_5h.get("used_percentage")),
                                       format_reset(rl_5h.get("resets_at"))))
    sections.append(format_section("週枠", num(rl_7d.get("used_percentage")),
                                   format_reset(rl_7d.get("resets_at"), weekly=True)))
    codex_section = format_codex(codex_limits())
    if codex_section:
        sections.append(codex_section)
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
