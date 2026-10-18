import os
import re
import sys
import time
import logging
from html import unescape

import feedparser
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("patchnote-to-discord")

STEAM_APPID = os.environ.get("STEAM_APPID", None)
STATE_FILE = "last_guid.txt"
RSS_URL = f"https://store.steampowered.com/feeds/news/app/{STEAM_APPID}/"
COLOR = int(os.environ.get("COLOR", 0x1B2838), 16)
EMBED_DESC_LIMIT = int(os.environ.get("EMBED_DESC_LIMIT", 4000))

TAG_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
TAG_P_OPEN = re.compile(r"<p[^>]*>", re.IGNORECASE)
TAG_P_CLOSE = re.compile(r"</p>", re.IGNORECASE)
TAG_BOLD = re.compile(r"<b>(.*?)</b>", re.IGNORECASE | re.DOTALL)
TAG_ANY = re.compile(r"<[^>]+>")
MULTI_NEWLINE = re.compile(r"\n{3,}")

FORUM_ERROR_CODE = 220001
forum_urls: set[str] = set()  # 이미 포럼으로 확인된 URL은 처음부터 thread_name 포함


def html_to_discord_text(html: str, link: str) -> str:
    text = html or ""
    text = TAG_BR.sub("\n", text)
    text = TAG_P_OPEN.sub("\n", text)
    text = TAG_P_CLOSE.sub("", text)
    text = TAG_BOLD.sub(r"**\1**", text)
    text = TAG_ANY.sub("", text)
    text = unescape(text)
    text = MULTI_NEWLINE.sub("\n\n", text).strip()

    if len(text) > EMBED_DESC_LIMIT:
        text = text[:EMBED_DESC_LIMIT].rstrip() + f"...([Read more]({link}))"
    return text


def load_last_guid() -> str | None:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        value = f.read().strip()
    return value or None


def save_last_guid(guid: str) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(guid)


def build_embed(entry) -> dict:
    link = entry.get("link", "")
    return {
        "title": (entry.get("title") or "(untitled)").strip()[:256],
        "url": link,
        "description": html_to_discord_text(entry.get("description", ""), link),
        "color": COLOR,
    }

def post_webhook(url: str, payload: dict) -> requests.Response:
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code == 429:
        retry_after = resp.json().get("retry_after", 1)
        log.warning("Ratelimit, waited for %s seconds", retry_after)
        time.sleep(float(retry_after) + 0.5)
        resp = requests.post(url, json=payload, timeout=15)
    return resp


def is_forum_error(resp: requests.Response) -> bool:
    if resp.status_code != 400:
        return False
    try:
        return resp.json().get("code") == FORUM_ERROR_CODE
    except ValueError:
        return "thread_name" in resp.text


def send_to_webhooks(webhooks: list[str], embed: dict) -> None:
    base_payload = {"embeds": [embed]}
    thread_name = embed["title"][:100]

    for url in webhooks:
        url = url.strip()
        if not url:
            continue

        payload = base_payload
        if url in forum_urls:
            payload = {**base_payload, "thread_name": thread_name}

        resp = post_webhook(url, payload)

        if is_forum_error(resp):
            log.info("Forum channel detected, retrying with thread_name.")
            forum_urls.add(url)
            resp = post_webhook(url, {**base_payload, "thread_name": thread_name})

        if resp.status_code >= 300:
            log.error("Failed to send webhooks: %s %s", resp.status_code, resp.text)
        else:
            log.info("Successful sending webhooks.")
        time.sleep(1)


def main() -> int:
    if not STEAM_APPID:
        log.error("STEAM_APPID env variable is empty.")
        return 1
    raw_webhooks = os.environ.get("DISCORD_WEBHOOKS", "")
    webhooks = [w for w in raw_webhooks.split(",") if w.strip()]
    if not webhooks:
        log.error("DISCORD_WEBHOOKS env secret is empty.")
        return 1

    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        log.warning("RSS has no item.: %s", RSS_URL)
        return 0

    last_guid = load_last_guid()

    if last_guid is None: #최초실행
        newest = feed.entries[0].guid
        save_last_guid(newest)
        log.info("Setting initial guid as %s and sending latest announcement.", newest)
        send_to_webhooks(webhooks, build_embed(feed.entries[0]))
        return 0

    new_entries = []
    for entry in feed.entries:
        guid = entry.guid
        if guid == last_guid:
            break
        new_entries.append(entry)
    new_entries.reverse()  # 글 오래된 순으로 전송

    if not new_entries:
        log.info("No new updates.")
        return 0

    for entry in new_entries:
        send_to_webhooks(webhooks, build_embed(entry))

    newest_guid = feed.entries[0].guid
    save_last_guid(newest_guid)
    log.info("Successful sending new %d announcements.", len(new_entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
