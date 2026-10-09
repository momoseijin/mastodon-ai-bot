# Mastodon AI 返信ボット (Claude API版)

自サーバ(例: `your-domain.example`)のアカウントからのメンションにのみ、
Claude APIを使って日本語で返信するボットです。
電車・バスの乗り換え案内と、Web検索(任意)にも対応しています。

## 構成

- `bot.py` : 本体スクリプト
- `requirements.txt` : 依存パッケージ
- `.env.example`(ダウンロード後 `env.example.txt` → `.env.example` に
  リネームしてください) : 設定ファイルの雛形
- `mastodon-ai-bot.service` : systemdサービス定義
- `.gitignore`(ダウンロード後 `gitignore.txt` → `.gitignore` にリネーム)

## 機能

- **ローカル限定**: 自サーバのアカウントからのメンションにのみ反応し、
  他サーバのユーザーからのメンションは無視します。
- **電車・バスの乗り換え案内**: 「渋谷から新宿までの行き方は?」のように聞くと、
  無料・認証不要の乗り換え案内API(`api.transit.ls8h.com`)を使って実際の経路・
  所要時間・運賃を調べて回答します。追加の契約や課金は不要です。
- **Web検索(任意・既定オフ)**: 最新のニュースなど、学習データだけでは
  答えられない話題について、Claudeが必要と判断した場合にWeb検索を行ってから
  回答します。検索実行ごとに別途課金が発生するため、`.env`で明示的に
  有効化した場合のみ使われます。
- **コスト・スパム対策**: 1時間あたりの返信数上限、1返信あたりの出力トークン数
  上限、Web検索の実行回数上限を、それぞれ`.env`で設定できます。
- **自サーバの一時的な応答遅延への耐性**: 投稿リクエストがタイムアウトした場合、
  短い間隔で数回リトライします。

## セットアップ手順

1. **Botアカウントの作成**

   自サーバ上に専用のアカウント(例: `@assistant`)を新規作成する。

2. **アプリ登録とアクセストークン発行**

   そのアカウントでログインした状態で、設定 → 開発 → 新規アプリ から
   スコープ `read` `write` を持つアプリを作成し、アクセストークンを控える。

3. **サーバへ配置(Gitを使う場合)**

   先にGitHub等にリポジトリを作成し、`bot.py` / `requirements.txt` /
   `.env.example` / `mastodon-ai-bot.service` / `.gitignore` をpushしておく
   (`.env`自体はコミットしないこと)。

   ```bash
   sudo mkdir -p /opt/mastodon-ai-bot
   sudo chown "$USER":"$USER" /opt/mastodon-ai-bot
   git clone <あなたのリポジトリURL> /opt/mastodon-ai-bot
   cd /opt/mastodon-ai-bot

   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   cp .env.example .env
   ```

   Privateリポジトリの場合は、事前にSSH鍵またはPersonal Access Tokenの設定が
   必要です。

4. **`.env` を編集**

   `MASTODON_ACCESS_TOKEN` / `MASTODON_API_BASE_URL` / `MASTODON_DOMAIN` /
   `ANTHROPIC_API_KEY` を実際の値に書き換える。Web検索を使いたい場合は
   `ENABLE_WEB_SEARCH=true` に変更する(詳細は後述)。

5. **専用ユーザーの作成(任意だが推奨)**

   ```bash
   sudo useradd -r -s /usr/sbin/nologin mastodon-bot
   sudo chown -R mastodon-bot:mastodon-bot /opt/mastodon-ai-bot
   ```

   以降、このユーザーでgit操作を行う場合は、Privateリポジトリ用の認証情報
   (SSH鍵やトークン)も`mastodon-bot`ユーザーから使える場所に配置してください。

6. **systemdサービスとして登録**

   ```bash
   sudo cp mastodon-ai-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now mastodon-ai-bot
   sudo journalctl -u mastodon-ai-bot -f  # 任意: 正常に起動したか確認したい場合のみ。Ctrl+Cで終了
   ```

## 設定項目(`.env`)

| 変数名 | 必須 | 既定値 | 説明 |
|---|---|---|---|
| `MASTODON_ACCESS_TOKEN` | ○ | - | Bot用アカウントのアクセストークン |
| `MASTODON_API_BASE_URL` | ○ | - | 自サーバのベースURL |
| `MASTODON_DOMAIN` | ○ | - | 自サーバのドメイン(ローカルアカウント判定用) |
| `ANTHROPIC_API_KEY` | ○ | - | Anthropic ConsoleのAPIキー |
| `CLAUDE_MODEL` | - | `claude-haiku-5-5` | 使用するモデル |
| `MAX_REPLIES_PER_HOUR` | - | `30` | 1時間あたりの最大返信数 |
| `MAX_OUTPUT_TOKENS` | - | `400` | 1返信あたりの最大出力トークン数 |
| `ENABLE_WEB_SEARCH` | - | `false` | Web検索ツールを有効にするか |
| `WEB_SEARCH_MAX_USES` | - | `2` | Web検索を有効にした場合の、1返信あたりの検索回数上限 |

## Web検索について

- Web検索を使うには別契約は不要です。同じAnthropic APIキーのまま、
  `.env`で`ENABLE_WEB_SEARCH=true`に設定するだけで有効になります。
- 通常のトークン課金に加えて、**検索を実行した回数分だけ追加の課金**が
  発生します。`WEB_SEARCH_MAX_USES`で1回の返信あたりの検索回数に
  上限をかけられますが、最新の単価は念のため以下で確認してください。
  `https://platform.claude.com/docs/en/about-claude/pricing`
- Web検索を有効にすると、検索の途中で出力が打ち切られて返信が空になるのを防ぐため、
  出力トークン上限は自動的に最低1024まで引き上げられます(返信自体は短くまとめる
  よう指示しているため、通常は課金は大きく増えません)。
- 雑談や一般知識の質問では検索は使われず、Claudeが「最新情報が必要」と
  判断した場合のみ発動するため、常に課金されるわけではありません。
- コストが気になる場合は、既定の`false`のままで問題ありません(乗り換え案内
  機能はWeb検索とは別物で、こちらは追加課金なしで常に利用できます)。

## 更新手順

コードを修正した場合は、手元でコミット・プッシュしたうえで、サーバ側でpullして
反映します。

```bash
sudo systemctl stop mastodon-ai-bot

cd /opt/mastodon-ai-bot
sudo -u mastodon-bot git pull

# requirements.txt に変更があった場合は依存関係も更新
sudo -u mastodon-bot ./venv/bin/pip install -r requirements.txt

sudo systemctl start mastodon-ai-bot
sudo journalctl -u mastodon-ai-bot -f  # 任意: 反映後の動作確認用。Ctrl+Cで終了
```

- `journalctl -f` は反映後にログをリアルタイムで確認したい場合だけの任意の手順です。
  実行しなくても起動・反映自体には影響しません。省略する場合は代わりに
  `sudo systemctl status mastodon-ai-bot` で `active (running)` になっているかだけ
  確認すると手軽です。
- `.env` は `.gitignore` で除外されているため、`git pull` しても上書きされません。
- `.env`に新しい設定項目(`ENABLE_WEB_SEARCH`など)を追加した場合は、
  既存の`.env`にも手動で追記してください(`git pull`では更新されません)。
- `mastodon-ai-bot.service` を変更した場合は、`pip install` の代わりに
  以下を実行してください。
  ```bash
  sudo cp mastodon-ai-bot.service /etc/systemd/system/
  sudo systemctl daemon-reload
  ```
- 反映後は `sudo -u mastodon-bot git log -1` で、意図した変更が取り込まれて
  いるか確認すると安心です。

## 注意点

- 返信の公開範囲(visibility)はデフォルトで `unlisted` にしています
  (元の投稿が `public` の場合)。全体公開のタイムラインを荒らさないための配慮です。
- Claude APIの料金や利用可能なモデル名は変更される可能性があるため、
  導入前に Anthropic の公式ドキュメント(`platform.claude.com/docs`)で
  最新情報を確認してください。
- 乗り換え案内APIは非公式・読み取り専用のサービスです。運行情報の変更や
  遅延などリアルタイム性が必要な場面では、あくまで参考情報として扱ってください。
