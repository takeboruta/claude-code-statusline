# claude-code-statusline

[Claude Code](https://claude.com/claude-code) のステータスライン用スクリプト。画面下部に次を1行で常時表示します。

```
~/my-project │ 5h枠 ▓▓░░░░░░ 26% → 16:00 │ 週枠 ▓░░░░░░░ 14% → 7/10 │ Max 20x · Opus 4.7 · 文脈 15/60% · 累計$7.47 · ⚒2
```

| 区画 | 意味 |
|---|---|
| `~/my-project` | 現在の作業ディレクトリ。ホームは `~` に短縮、40文字を超える場合は中間ディレクトリを頭文字1字に省略（`~/p/s/src` のように末尾だけ残す） |
| `5h枠` | 5時間ローリングウィンドウのレート制限消費率（カラー付きバー。50%〜シアン / 70%〜黄 / 90%〜赤）。`→ 16:00` はリセット時刻 |
| `週枠` | 7日ウィンドウのレート制限消費率。`→ 7/10` はリセット日 |
| `Max 20x` | サブスクプラン名（Pro / Max 5x / Max 20x 等。取得できた時のみ） |
| `Opus 4.7` | 現在使用中のモデル名 |
| `文脈 15/60%` | コンテキスト窓の使用率。`/60%` は autocompact（自動要約）の発火ライン（`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 設定時のみ表示） |
| `累計$7.47` | セッション累計コストの API 料金換算参考値（サブスクリプション利用時は実請求ではない） |
| `⚒2` | **実行中のサブエージェント数**（1件以上あるときだけ表示）。表示には後述の `refreshInterval` 設定が必要 |

取得できない項目は区画ごと省略されるため、古いバージョンの Claude Code でも動作します。

## 必要環境

- Python 3.9+（標準ライブラリのみ。追加インストール不要。macOS 標準の python3 で動作）

## セットアップ

```bash
cp statusline.py ~/.claude/statusline.py
```

`~/.claude/settings.json` に追記:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude/statusline.py",
    "refreshInterval": 5
  }
}
```

`refreshInterval: 5` は、メインがアイドルでバックグラウンドのサブエージェントだけが動いている間もステータスラインを再描画させるための設定です。**これが無いと `⚒N` の点灯・消灯やリセット時刻がイベント発生時までしか更新されません。** `⚒` 表示が不要なら省略して構いません。

autocompact の発火ラインを併記したい場合（任意）:

```json
{
  "env": {
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "60"
  }
}
```

## 実装メモ

- Claude Code は statusLine コマンドの stdin に毎回セッション情報の JSON を渡す。本スクリプトは `workspace.current_dir` / `model` / `cost.total_cost_usd` / `context_window` / `rate_limits`（`used_percentage` と `resets_at`）を参照する
- コンテキスト使用率は `context_window.used_percentage`（v2 系で提供）をそのまま使う。無い旧バージョンでは `transcript_path` の JSONL を走査して直近の usage レコードから推定にフォールバック
- サブスクプラン名だけは stdin に来ないため、`~/.claude.json` の `oauthAccount`（ログイン時にキャッシュされる非公開フィールド）から読む。構造が変わって取れない場合は黙って省略
- autocompact 閾値は環境変数 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` → `~/.claude/settings.json` の `env` の順で読む。「文脈」の色は閾値に対する比率で付く（閾値60で57%ならほぼ満杯＝赤）
- `~/.claude.json` は履歴等で数MBになりうるため、プラン名の解析結果を `~/.claude/statusline-plan.cache` に元ファイルの mtime 付きでキャッシュし、毎レンダリングの全パースを避けている
- 取得できない項目は区画ごと省略する方針のため、フィールド構成が違う環境でも1行は必ず出る（想定外の入力・型でも落ちないよう型検証と最終フォールバックを備える）
- サブエージェント数は stdin に来ないため、セッション transcript の隣にできる `<transcript と同名のディレクトリ>/subagents/agent-*.jsonl` のうち mtime が直近30秒以内のものを数える近似。実行中のエージェントは数秒おきに追記するため実用上は一致するが、長いツール実行中は書き込みが止まり一時的に数え漏れることがある

## License

MIT
