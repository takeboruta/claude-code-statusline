#!/usr/bin/env python3
"""Claude Code の UserPromptSubmit hook。長くなったセッションを区切るよう、段階ごとに1回だけ促す。

判定はステータスラインの「入/出」と同じ値（出力1トークンあたりに読んだ入力側トークン）。
計算は同じフォルダに置いた statusline.py の session_read_per_output をそのまま使う（定義と閾値を二重に持たない。
増分読みのキャッシュも共有するので、毎回の入力で 10MB の記録を読み直さない）。
statusline.py を別の場所に置いているときは、環境変数 STATUSLINE_PY でパスを指定する。

  - 300 以上（区切りどき）: ユーザーに1行表示し、Claude には「報告の末尾で区切りを1行提案」を渡す
  - どちらの段階でも、以降のまとまった調査・読み込みを fork 以外のサブエージェントへ出すよう伝える
    （メインの文脈をこれ以上膨らませないため。溜まった分は減らないので、500 の引き継ぎは別に要る）
  - 500 以上（新セッション推奨）: 上に加えて、新しいセッションへ貼れる引き継ぎメモを書かせる
各段階はセッションごとに1回だけ（~/.claude/session-split/<session_id> に出した段階を記録）。
入力は止めない（区切るかどうかは人が決める）。判定できないときは何もしない。
"""
import importlib.util
import json
import os
import re
import sys

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".claude", "session-split")
STATUSLINE = os.environ.get("STATUSLINE_PY") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "statusline.py")


def load_statusline():
    spec = importlib.util.spec_from_file_location("statusline_mod", STATUSLINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    data = json.load(sys.stdin)
    sid = re.sub(r"[^\w-]", "", data.get("session_id") or "")
    if not sid:
        return
    sl = load_statusline()
    ratio = sl.session_read_per_output(data)
    if ratio is None:
        return
    level = 2 if ratio >= sl.EFF_ALERT else (1 if ratio >= sl.EFF_WARN else 0)
    if not level:
        return

    marker = os.path.join(STATE_DIR, sid)
    try:
        done = int(open(marker, encoding="utf-8").read().strip() or 0)
    except (OSError, ValueError):
        done = 0
    if level <= done:
        return

    r = f"{ratio:,.0f}"
    if level == 2:
        user_msg = f"入/出 {r}（新セッション推奨）: このセッションは1回の応答で読む量が膨らんでいます。今の依頼が済んだら新しいセッションへ。"
        ctx = (f"[session-split] このセッションの入/出は {r} で、新セッション推奨の閾値 {sl.EFF_ALERT:.0f} を超えた。"
               "今回の依頼にはいつもどおり答えたうえで、報告の末尾に「## 引き継ぎメモ」を付ける。"
               "新しいセッションの最初にそのまま貼れる形で、目的・決まったこと・未完了のタスク・触っているファイルの絶対パス・"
               "次の一手を合わせて10行以内。このセッションでしか分からない事実（測った数値、見送った理由）を優先し、"
               "リポジトリや CLAUDE.md を読めば分かることは書かない。")
    else:
        user_msg = f"入/出 {r}（区切りどき）: 話題が変わるなら新しいセッションで始めると枠の減りが緩やかになります。"
        ctx = (f"[session-split] このセッションの入/出は {r} で、区切りどきの閾値 {sl.EFF_WARN:.0f} を超えた。"
               "今回の依頼にはいつもどおり答え、報告の末尾に1行だけ「区切るなら今が目安（入/出 N）」と書く。"
               "引き継ぎメモはまだ書かない。")
    # どちらの段階でも伝える（最初の判定が 500 以上だと 300 の段階を通らないため）
    ctx += ("これ以降、まとまった調査・複数ファイルの読み込み・長いログの確認は、fork 以外のサブエージェントに"
            " model を指定して出し、メインの文脈に中身を入れない（fork は文脈ごと複製するので読む量が減らない）。"
            "判断が要る作業と、数回のツール呼び出しで済むものは出さない。サブエージェントの報告は自分の手で確かめる。")
    print(json.dumps({
        "systemMessage": user_msg,
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx},
    }, ensure_ascii=False), flush=True)
    # 出し終えてから記録する（出力に失敗したのに「通知済み」にならないように）
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(str(level))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # 判定できないときは入力を通す
    sys.exit(0)
