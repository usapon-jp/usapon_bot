# USAPONIA Cloud Deploy (Railway)

## 重要な前提
- クラウド上のBotは、あなたのMacローカルファイル（例: `~/Desktop`）にはアクセスできません。
- そのため「デスクトップのスクショをゴミ箱へ」はクラウド版では実行できません。
- 相談チャットBotとしての常時稼働には向いています。

## 1. GitHubへ配置
1. `usapon_bot` をGitHubリポジトリにpushする
2. `.env` はpushしない（秘密情報）

## 2. Railwayで新規プロジェクト
1. Railwayで `New Project` -> `Deploy from GitHub Repo`
2. 対象リポジトリを選択
3. Start Command を `python usaponia.py` に設定

## 3. Railway環境変数
以下を Railway の Variables に設定:
- `DISCORD_TOKEN`
- `GEMINI_API_KEY`
- `USAPONIA_AUTO_CHANNEL_IDS`
- `USAPONIA_MODEL=gemini-2.5-flash-lite`
- `USAPONIA_LLM_BACKEND=gemini`
- `USAPONIA_PROGRESS_NOTIFY=false`
- `USAPONIA_ENABLE_COMMAND_EXECUTION=false`  # クラウドではまずOFF推奨
- `USAPONIA_MEMORY_FILE=/data/memory.txt`     # Volume利用時
- `USAPONIA_REMINDERS_FILE=/data/reminders.json`
- `USAPONIA_HANDOFF_PROVIDER=github`
- `USAPONIA_HANDOFF_REPO=owner/repo`
- `USAPONIA_HANDOFF_TOKEN=github_pat_xxx`
- `USAPONIA_HANDOFF_LABEL=usapon-handoff`

## 4. 永続化（推奨）
- Railway Volume を追加し `/data` にマウント
- `USAPONIA_MEMORY_FILE=/data/memory.txt` を設定
- `USAPONIA_REMINDERS_FILE=/data/reminders.json` を設定

## 5. デプロイ確認
- Logs で `ログインしました` が出れば成功
- Discordで指定チャンネルに投稿して応答確認

## 6. ローカル版との使い分け
- ローカル版: Mac操作コマンド向け（スクショ整理など）
- クラウド版: 常時チャット応答向け

## 7. handoff運用（クラウド -> ローカル）
- クラウド版で `handoff create <内容>` を実行してIssue化
- ローカル版で `handoff pull` を実行して未処理一覧を取得
- ローカル作業後に `handoff done <番号> <完了メモ>` を実行して完了
