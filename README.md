# claude-code-statusline

[Claude Code](https://claude.com/claude-code) のステータスライン用スクリプト。画面下部に次を1行で常時表示します。

```
~/my-project │ 5h枠 ▓▓░░░░░░ 26% │ 週枠 ▓░░░░░░░ 14% │ Opus 4.7 · 文脈 57/60% · 累計$7.47
```

| 区画 | 意味 |
|---|---|
| `~/my-project` | 現在の作業ディレクトリ。ホームは `~` に短縮、40文字を超える場合は中間ディレクトリを頭文字1字に省略（`~/p/s/src` のように末尾だけ残す） |
| `5h枠` | 5時間ローリングウィンドウのレート制限消費率（カラー付きバー。50%〜シアン / 70%〜黄 / 90%〜赤） |
| `週枠` | 7日ウィンドウのレート制限消費率 |
| `Opus 4.7` | 現在使用中のモデル名 |
| `文脈 57/60%` | コンテキスト窓の使用率。`/60%` は autocompact（自動要約）の発火ライン（`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 設定時のみ表示） |
| `累計$7.47` | セッション累計コストの API 料金換算参考値（サブスクリプション利用時は実請求ではない） |

取得できない項目は区画ごと省略されるため、古いバージョンの Claude Code でも動作します。

## 必要環境

- Python 3.10+（標準ライブラリのみ。追加インストール不要）

## セットアップ

```bash
cp statusline.py ~/.claude/statusline.py
```

`~/.claude/settings.json` に追記:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude/statusline.py"
  }
}
```

autocompact の発火ラインを併記したい場合（任意）:

```json
{
  "env": {
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "60"
  }
}
```

## 実装メモ

- Claude Code は statusLine コマンドの stdin に毎回セッション情報の JSON を渡す。本スクリプトは `workspace.current_dir` / `model` / `cost.total_cost_usd` / `transcript_path` / `rate_limits` を参照する
- コンテキスト使用率は stdin では渡されないため、`transcript_path` の JSONL を走査して直近の usage レコード（input + cache_read + cache_creation トークン）から推定する。窓サイズはモデル ID に `[1m]` を含めば 100 万、それ以外は 20 万トークン
- autocompact 閾値は環境変数 → `~/.claude/settings.json` の `env` の順で読む

## License

MIT
