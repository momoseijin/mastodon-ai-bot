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

import requests
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

# 乗り換え案内API(認証不要・読み取り専用)
TRANSIT_API_BASE = "https://api.transit.ls8h.com"
TRANSIT_API_TIMEOUT = 10
# Claudeがツールを呼び出してから最終回答を出すまでの最大往復回数
MAX_TOOL_ITERATIONS = 4

# Web検索ツール(コストが別途かかるため既定は無効。.envで有効化する)
ENABLE_WEB_SEARCH = os.environ.get("ENABLE_WEB_SEARCH", "false").lower() == "true"
# 1回の返信生成あたりの検索実行回数の上限(コスト対策)
WEB_SEARCH_MAX_USES = int(os.environ.get("WEB_SEARCH_MAX_USES", "2"))
# Web検索を使うと、検索クエリ生成や検索後の回答も出力トークンに含まれるため、
# 有効時は最低でもこの値まで出力トークン上限を引き上げる(途中打ち切りで返信が空になるのを防ぐ)
WEB_SEARCH_MIN_OUTPUT_TOKENS = 1024

SYSTEM_PROMPT = (
    "あなたはMastodonサーバ上で動作するAIアシスタントアカウントです。"
    "日本語で、簡潔かつ丁寧に返信してください。"
    "Mastodonの投稿として自然な長さ(できれば300文字程度以内)にまとめ、"
    "絵文字の多用は避けてください。"
    "電車やバスの乗り換え・経路について聞かれた場合は、"
    "search_transit_routeツールを使って実際の経路情報を調べてから回答してください。"
    "駅名があいまいで候補が複数ある場合は、ツールの結果をもとに一番自然な候補を選んで構いません。"
    + (
        "最新のニュースや現在の状況など、学習データだけでは答えられない話題を"
        "聞かれた場合は、web_searchツールを使って調べてから回答してください。"
        "一般知識や雑談など検索が不要な話題では、無駄に検索しないでください。"
        if ENABLE_WEB_SEARCH
        else ""
    )
)

TRANSIT_TOOL = {
    "name": "search_transit_route",
    "description": (
        "日本国内の駅・地名間の電車/バスの乗り換え経路を検索する。"
        "出発地と到着地の名前(駅名・停留所名・地名)を渡すと、"
        "所要時間・乗り換え回数・運賃・利用路線などを含む経路候補を返す。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "from_place": {"type": "string", "description": "出発地(駅名や地名)"},
            "to_place": {"type": "string", "description": "到着地(駅名や地名)"},
            "date": {
                "type": "string",
                "description": "検索する日付。YYYYMMDD形式。省略時は当日。",
            },
            "time": {
                "type": "string",
                "description": "検索する時刻。HH:MM形式。省略時は現在時刻。",
            },
            "search_type": {
                "type": "string",
                "enum": ["departure", "arrival", "first", "last"],
                "description": "departure=指定時刻に出発, arrival=指定時刻に到着, "
                                "first=始発, last=終電。省略時はdeparture。",
            },
        },
        "required": ["from_place", "to_place"],
    },
}

# web_searchはAnthropic側が自動で実行してくれるサーバ側ツール。
# bot.py側で実行処理を書く必要はなく、tools一覧に加えるだけでよい。
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": WEB_SEARCH_MAX_USES,
}

TOOLS = [TRANSIT_TOOL] + ([WEB_SEARCH_TOOL] if ENABLE_WEB_SEARCH else [])

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


def _format_hms(secs: float) -> str:
    """サービス日0時からの秒数を HH:MM 形式にする(24時超過・負値も許容)"""
    secs = int(secs)
    sign = "翌" if secs >= 86400 else ("前日" if secs < 0 else "")
    secs = secs % 86400
    return f"{sign}{secs // 3600:02d}:{(secs % 3600) // 60:02d}"


def _resolve_place(name: str) -> dict | None:
    """駅名・地名の文字列を、経路検索に使えるendpoint(feedId:stopId or geo:...)に解決する"""
    resp = requests.get(
        f"{TRANSIT_API_BASE}/api/v1/places/suggest",
        params={"q": name, "limit": 3},
        timeout=TRANSIT_API_TIMEOUT,
    )
    resp.raise_for_status()
    places = resp.json().get("places", [])
    return places[0] if places else None


def search_transit_route(
    from_place: str,
    to_place: str,
    date: str | None = None,
    time_: str | None = None,
    search_type: str = "departure",
) -> str:
    """Transit APIで経路検索を行い、Claudeに渡す用の要約テキストを作る"""
    try:
        origin = _resolve_place(from_place)
        if not origin:
            return f"「{from_place}」に該当する駅・地名が見つかりませんでした。"

        destination = _resolve_place(to_place)
        if not destination:
            return f"「{to_place}」に該当する駅・地名が見つかりませんでした。"

        params = {
            "from": origin["endpoint"],
            "to": destination["endpoint"],
            "fromLabel": origin["name"],
            "toLabel": destination["name"],
            "type": search_type,
            "numItineraries": 3,
        }
        if date:
            params["date"] = date
        if time_:
            params["time"] = time_

        resp = requests.get(
            f"{TRANSIT_API_BASE}/api/v1/plan",
            params=params,
            timeout=TRANSIT_API_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        journeys = data.get("journeys", [])
        if not journeys:
            return f"{origin['name']}から{destination['name']}への経路が見つかりませんでした。"

        lines = [f"{origin['name']} → {destination['name']} の経路候補:"]
        for i, journey in enumerate(journeys, start=1):
            dep = _format_hms(journey["departureSecs"])
            arr = _format_hms(journey["arrivalSecs"])
            duration_min = journey["durationSecs"] // 60
            transfers = journey["transferCount"]
            fare = journey.get("fare")
            fare_text = f", 運賃{fare['ticket']}円" if fare else ""
            route_names = [
                leg["routeName"] for leg in journey["legs"] if leg.get("kind") == "transit"
            ]
            route_text = " → ".join(route_names) if route_names else "(徒歩のみ)"
            lines.append(
                f"{i}. {dep}発 {arr}着 (所要{duration_min}分, 乗換{transfers}回{fare_text}) "
                f"利用路線: {route_text}"
            )
        return "\n".join(lines)

    except requests.RequestException as e:
        log.warning("Transit API呼び出しに失敗: %s", e)
        return "経路検索サービスへの接続に失敗しました。時間をおいて試してください。"


def _execute_tool(name: str, tool_input: dict) -> str:
    if name == "search_transit_route":
        return search_transit_route(
            from_place=tool_input["from_place"],
            to_place=tool_input["to_place"],
            date=tool_input.get("date"),
            time_=tool_input.get("time"),
            search_type=tool_input.get("search_type", "departure"),
        )
    return f"未知のツールが指定されました: {name}"


def ask_claude(prompt: str) -> str:
    messages = [{"role": "user", "content": prompt}]
    max_tokens = MAX_OUTPUT_TOKENS
    if ENABLE_WEB_SEARCH:
        max_tokens = max(max_tokens, WEB_SEARCH_MIN_OUTPUT_TOKENS)

    for _ in range(MAX_TOOL_ITERATIONS):
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # 原因調査用: 停止理由・ブロックの種類・出力トークン数を記録する
        log.info(
            "Claude応答: stop_reason=%s, blocks=%s, output_tokens=%s",
            response.stop_reason,
            [block.type for block in response.content],
            getattr(response.usage, "output_tokens", "?"),
        )

        # サーバ側ツール(Web検索)の処理が途中で一時停止した場合は、続きを再送して継続させる
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason != "tool_use":
            parts = [block.text for block in response.content if block.type == "text"]
            text = "\n".join(parts).strip()
            if not text:
                log.warning(
                    "Claudeの応答にテキストが含まれていません (stop_reason=%s)",
                    response.stop_reason,
                )
                return "うまく回答をまとめられませんでした。もう一度試してみてください。"
            return text

        # ツール呼び出しを実行し、結果を会話に追加してもう一度Claudeに投げる
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                log.info("ツール呼び出し: %s(%s)", block.name, block.input)
                result_text = _execute_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )
        messages.append({"role": "user", "content": tool_results})

    return "うまく調べられませんでした。質問を変えて試してみてください。"


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
