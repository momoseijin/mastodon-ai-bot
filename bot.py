#!/usr/bin/env python3
"""
Mastodon AI 返信ボット
- 自分のMastodonサーバのローカルアカウントからのメンションにのみ反応
- 返信生成にはAnthropic Claude APIを利用
"""

import os
import re
import time
import logging
from collections import deque

from dotenv import load_dotenv
from mastodon import Mastodon, StreamListener
from mastodon.errors import MastodonNetworkError
from anthropic import Anthropic

load_dotenv()

# ---- 設定(.env から読み込み) ----
MASTODON_API_BASE_URL = os.environ["MASTODON_API_BASE_URL"]  # 例: https://your-domain.example
MASTODON_ACCESS_TOKEN = os.environ["MASTODON_ACCESS_TOKEN"]
MASTODON_DOMAIN = os.environ["MASTODON_DOMAIN"]  # 例: your-domain.example (acctのドメイン比較用)
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# コスト・スパム対策: 1時間あたりの最大返信数
MAX_REPLIES_PER_HOUR = int(os.environ.get("MAX_REPLIES_PER_HOUR", "30"))
# 1返信あたりの最大出力トークン数(短めに抑えてコスト削減)
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "400"))

SYSTEM_PROMPT = (
    "あなたはMastodonサーバ上で動作するAIアシスタントアカウントです。"
    "日本語で、簡潔かつ丁寧に返信してください。"
    "Mastodonの投稿として自然な長さ(できれば300文字程度以内)にまとめ、"
    "絵文字の多用は避けてください。"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mastodon-ai-bot")

mastodon = Mastodon(
    access_token=MASTODON_ACCESS_TOKEN,
    api_base_url=MASTODON_API_BASE_URL,
    request_timeout=20,  # 既定300秒だと自サーバ側が重い時にプロセス全体が長時間ブロックされるため短縮
)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

# 直近の返信時刻を保持し、レート制限に使う
_reply_timestamps = deque()


def is_local_account(acct: str) -> bool:
    """
    acctが自分のサーバのローカルアカウントか判定する。
    ローカルアカウントは "username" 形式、
    リモートは "username@remote-domain" 形式になる。
    念のためドメインが自分のものと一致するかも確認する。
    """
    if "@" not in acct:
        return True
    _, domain = acct.split("@", 1)
    return domain.lower() == MASTODON_DOMAIN.lower()


def within_rate_limit() -> bool:
    now = time.time()
    while _reply_timestamps and now - _reply_timestamps[0] > 3600:
        _reply_timestamps.popleft()
    if len(_reply_timestamps) >= MAX_REPLIES_PER_HOUR:
        return False
    _reply_timestamps.append(now)
    return True


def strip_html_and_mentions(html: str) -> str:
    """投稿本文のHTMLタグと先頭のメンション(@user)を除去して素のテキストにする"""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"@\w+(@[\w.-]+)?", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def ask_claude(prompt: str) -> str:
    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(parts).strip()


class MentionListener(StreamListener):
    def on_notification(self, notification):
        try:
            if notification["type"] != "mention":
                return

            account = notification["account"]
            acct = account["acct"]

            if not is_local_account(acct):
                log.info("無視: リモートアカウントからのメンション (%s)", acct)
                return

            if not within_rate_limit():
                log.warning("レート制限に達したため返信をスキップ (%s)", acct)
                return

            status = notification["status"]
            text = strip_html_and_mentions(status["content"])
            if not text:
                return

            log.info("メンション受信 from @%s: %s", acct, text[:80])

            reply_text = ask_claude(text)
            if not reply_text:
                return

            mention = f"@{acct} "
            visibility = status.get("visibility", "unlisted")
            if visibility == "public":
                visibility = "unlisted"  # 返信は公開タイムラインを荒らさないよう控えめに

            # 自サーバの一時的な遅延・タイムアウトに備えて数回だけ短くリトライする
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    mastodon.status_post(
                        mention + reply_text,
                        in_reply_to_id=status["id"],
                        visibility=visibility,
                    )
                    log.info("返信投稿完了 (in_reply_to=%s)", status["id"])
                    break
                except MastodonNetworkError:
                    if attempt == max_attempts:
                        log.error(
                            "投稿に%d回失敗したため諦めます (in_reply_to=%s, 宛先=@%s)",
                            max_attempts, status["id"], acct,
                        )
                    else:
                        log.warning(
                            "投稿失敗、再試行します (%d/%d, in_reply_to=%s)",
                            attempt, max_attempts, status["id"],
                        )
                        time.sleep(3)

        except Exception:
            log.exception("通知処理中にエラーが発生しました")


def main():
    log.info("Mastodon AI ボットを起動します (%s)", MASTODON_API_BASE_URL)
    while True:
        try:
            mastodon.stream_user(MentionListener(), run_async=False, reconnect_async=True)
        except Exception:
            log.exception("ストリーム接続が切断されました。5秒後に再接続します")
            time.sleep(5)


if __name__ == "__main__":
    main()
