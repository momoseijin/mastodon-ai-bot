# Mastodon AI 返信ボット (Claude API版)

自サーバ(your-domain.example)のアカウントからのメンションにのみ、
Claude APIを使って日本語で返信するボットです。

## 構成
- `bot.py` : 本体スクリプト
- `requirements.txt` : 依存パッケージ
- `.env.example` : 設定ファイルの雛形
- `mastodon-ai-bot.service` : systemdサービス定義

## セットアップ手順

1. **Botアカウントの作成**
   自サーバ上に専用のアカウント(例: `@assistant`)を新規作成する。

2. **アプリ登録とアクセストークン発行**
   そのアカウントでログインした状態で、設定 → 開発 → 新規アプリ から
   スコープ `read` `write` を持つアプリを作成し、アクセストークンを控える。

3. **サーバへ配置**
   ```bash
   sudo mkdir -p /opt/mastodon-ai-bot
   sudo cp bot.py requirements.txt .env.example /opt/mastodon-ai-bot/
   cd /opt/mastodon-ai-bot
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   cp .env.example .env
   ```

4. **`.env` を編集**
   `MASTODON_ACCESS_TOKEN` / `MASTODON_API_BASE_URL` / `MASTODON_DOMAIN` /
   `ANTHROPIC_API_KEY` を実際の値に書き換える。

5. **専用ユーザーの作成(任意だが推奨)**
   ```bash
   sudo useradd -r -s /usr/sbin/nologin mastodon-bot
   sudo chown -R mastodon-bot:mastodon-bot /opt/mastodon-ai-bot
   ```

6. **systemdサービスとして登録**
   ```bash
   sudo cp mastodon-ai-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now mastodon-ai-bot
   sudo journalctl -u mastodon-ai-bot -f
   ```

## 仕組み
- Mastodonのユーザーストリーム(`stream_user`)でメンション通知を常時受信します。
- 通知元アカウントの `acct` にドメインが含まれない、または
  `MASTODON_DOMAIN` と一致する場合のみ「ローカルアカウント」と判定し処理します
  (他サーバのユーザーからのメンションは無視されます)。
- 本文からHTMLタグとメンション部分を除去し、Claude APIに渡します。
- 生成された返信を、元の投稿への返信(`in_reply_to_id`)として投稿します。

## コストを抑えるための工夫
- デフォルトモデルは `claude-haiku-4-5-20251001`(2026年9月時点で
  入力$1/出力$5 per 1M tokens と、Claudeのモデルの中で最も低コスト)。
- `MAX_REPLIES_PER_HOUR` で1時間あたりの返信数に上限を設定(既定30件)。
- `MAX_OUTPUT_TOKENS` で1回の返信の最大トークン数を抑制(既定400)。
- ローカルアカウント限定にすることで、不特定多数からの呼び出しを防止。
- 必要に応じて、Anthropic ConsoleでこのAPIキーに月間利用上限(spending limit)を
  設定しておくと、想定外の高額請求を防げます。

## 注意点
- `MAX_OUTPUT_TOKENS` や `MAX_REPLIES_PER_HOUR` は運用しながら調整してください。
- 返信の公開範囲(visibility)はデフォルトで `unlisted` にしています
  (元の投稿が `public` の場合)。全体公開のタイムラインを荒らさないための配慮です。
  必要であれば `bot.py` 内の該当箇所を変更してください。
- Claude APIの料金や利用可能なモデル名は変更される可能性があるため、
  実際に導入する前に Anthropic の公式ドキュメント(platform.claude.com/docs)で
  最新情報を確認してください。
