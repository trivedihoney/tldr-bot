import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import discord
from discord.ext import tasks
from dotenv import load_dotenv


load_dotenv()

DEFAULT_FEED_URLS = (
    "TLDR AI|https://bullrich.dev/tldr-rss/ai.rss,"
    "TLDR Data|https://bullrich.dev/tldr-rss/data.rss"
)
FEED_URLS = os.getenv("TLDR_RSS_FEED_URLS") or os.getenv("FEED_URLS") or DEFAULT_FEED_URLS
NEWS_CHANNEL_ID = os.getenv("DISCORD_NEWS_CHANNEL_ID")
NEWS_CHANNEL_NAME = os.getenv("DISCORD_NEWS_CHANNEL_NAME", "news-channel")
POLL_MINUTES = int(os.getenv("TLDR_POLL_MINUTES", "360"))
FIRST_RUN_LIMIT = int(os.getenv("TLDR_FIRST_RUN_LIMIT", "5"))
SEEN_FILE = Path(os.getenv("TLDR_SEEN_FILE", ".tldr_seen.json"))
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096


@dataclass(frozen=True)
class Feed:
    name: str
    url: str


@dataclass(frozen=True)
class NewsItem:
    feed_name: str
    title: str
    link: str
    summary: str
    published: datetime
    entry_id: str
    legacy_entry_id: str


def parse_feed_urls(value: str) -> list[Feed]:
    feeds: list[Feed] = []

    for index, feed_config in enumerate(value.split(","), start=1):
        feed_config = feed_config.strip()
        if not feed_config:
            continue

        if "|" in feed_config:
            name, url = feed_config.split("|", 1)
            name = name.strip() or f"TLDR Feed {index}"
            url = url.strip()
        else:
            url = feed_config
            name = f"TLDR Feed {index}"

        if url:
            feeds.append(Feed(name=name, url=url))

    return feeds


def clean_text(value: str | None) -> str:
    if not value:
        return ""

    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def item_text(item: ET.Element, tag: str) -> str:
    child = item.find(tag)
    return child.text.strip() if child is not None and child.text else ""


def first_link_from_html(value: str) -> str:
    if not value:
        return ""

    match = re.search(r'href=["\']([^"\']+)["\']', value, flags=re.IGNORECASE)
    return unescape(match.group(1).strip()) if match else ""


def item_guid(item: ET.Element) -> tuple[str, bool]:
    guid = item.find("guid")
    if guid is None or not guid.text:
        return "", False

    return guid.text.strip(), guid.attrib.get("isPermaLink", "").lower() == "true"


def is_sponsor_title(title: str) -> bool:
    return title.strip().lower().endswith("(sponsor)")


def fetch_feed_items(feed: Feed) -> list[NewsItem]:
    request = Request(
        feed.url,
        headers={"User-Agent": "tldr-discord-bot/1.0"},
    )

    with urlopen(request, timeout=20) as response:
        feed_xml = response.read()

    root = ET.fromstring(feed_xml)
    items: list[NewsItem] = []

    for item in root.findall("./channel/item"):
        title = clean_text(item_text(item, "title"))
        if not title or is_sponsor_title(title):
            continue

        raw_description = item_text(item, "description")
        guid, guid_is_permalink = item_guid(item)
        link = item_text(item, "link") or first_link_from_html(raw_description)
        if not link and guid_is_permalink:
            link = guid
        summary = clean_text(raw_description)
        published = parse_datetime(item_text(item, "pubDate"))
        legacy_entry_id = guid or link or f"{title}-{published.isoformat()}"
        entry_id = f"{feed.url}::{legacy_entry_id}"

        items.append(
            NewsItem(
                feed_name=feed.name,
                title=title,
                link=link,
                summary=summary,
                published=published,
                entry_id=entry_id,
                legacy_entry_id=legacy_entry_id,
            )
        )

    return sorted(items, key=lambda news_item: news_item.published)


def load_seen_entries() -> set[str]:
    if not SEEN_FILE.exists():
        return set()

    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    if not isinstance(data, list):
        return set()

    return {entry for entry in data if isinstance(entry, str)}


def save_seen_entries(seen_entries: set[str]) -> None:
    SEEN_FILE.write_text(
        json.dumps(sorted(seen_entries), indent=2),
        encoding="utf-8",
    )


def trim_to_limit(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    return text[: limit - 3].rstrip() + "..."


def discord_timestamp(value: datetime) -> str:
    timestamp = int(value.timestamp())
    return f"<t:{timestamp}:F> (<t:{timestamp}:R>)"


def format_news_item(item: NewsItem) -> discord.Embed:
    description = trim_to_limit(
        item.summary or "No summary was included in the feed.",
        1400,
    )

    embed = discord.Embed(
        title=trim_to_limit(item.title, DISCORD_EMBED_TITLE_LIMIT),
        url=item.link or None,
        description=trim_to_limit(description, DISCORD_EMBED_DESCRIPTION_LIMIT),
        color=discord.Color.from_rgb(32, 139, 214),
        timestamp=item.published,
    )

    embed.add_field(
        name="Published",
        value=discord_timestamp(item.published),
        inline=False,
    )

    if item.link:
        embed.add_field(
            name="Article",
            value=f"[Open the full story]({item.link})",
            inline=False,
        )

    embed.set_author(name=item.feed_name)
    embed.set_footer(text=f"{item.feed_name} RSS")

    return embed


def has_seen_feed_entries(seen_entries: set[str], items: list[NewsItem]) -> bool:
    return any(
        item.entry_id in seen_entries or item.legacy_entry_id in seen_entries
        for item in items
    )


def is_seen(seen_entries: set[str], item: NewsItem) -> bool:
    return item.entry_id in seen_entries or item.legacy_entry_id in seen_entries


async def find_news_channel(client: discord.Client) -> discord.abc.Messageable | None:
    if NEWS_CHANNEL_ID:
        channel = client.get_channel(int(NEWS_CHANNEL_ID))
        if channel is None:
            channel = await client.fetch_channel(int(NEWS_CHANNEL_ID))
        return channel if isinstance(channel, discord.abc.Messageable) else None

    for guild in client.guilds:
        channel = discord.utils.get(guild.text_channels, name=NEWS_CHANNEL_NAME)
        if channel is not None:
            return channel

    return None


class TLDRBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.seen_entries = load_seen_entries()
        self.feeds = parse_feed_urls(FEED_URLS)

    async def setup_hook(self) -> None:
        self.post_tldr_news.start()

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user}")

    @tasks.loop(minutes=POLL_MINUTES)
    async def post_tldr_news(self) -> None:
        channel = await find_news_channel(self)
        if channel is None:
            print(
                "Could not find a Discord channel. Set DISCORD_NEWS_CHANNEL_ID "
                f"or create a #{NEWS_CHANNEL_NAME} text channel."
            )
            return

        if not self.feeds:
            print("No TLDR RSS feeds configured.")
            return

        posted_count = 0

        for feed in self.feeds:
            try:
                items = await asyncio.to_thread(fetch_feed_items, feed)
            except (ET.ParseError, TimeoutError, URLError, OSError) as exc:
                print(f"Failed to fetch {feed.name} RSS feed: {exc}")
                continue

            is_first_run = not has_seen_feed_entries(self.seen_entries, items)
            unseen_items = [item for item in items if not is_seen(self.seen_entries, item)]
            if is_first_run:
                unseen_items = unseen_items[-FIRST_RUN_LIMIT:]

            if not unseen_items:
                print(f"No new {feed.name} items found.")
                continue

            for item in unseen_items:
                await channel.send(embed=format_news_item(item))
                self.seen_entries.add(item.entry_id)
                posted_count += 1

            if is_first_run:
                self.seen_entries.update(item.entry_id for item in items)

            print(f"Posted {len(unseen_items)} {feed.name} item(s).")

        if posted_count:
            save_seen_entries(self.seen_entries)

    @post_tldr_news.before_loop
    async def before_post_tldr_news(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set.")

    bot = TLDRBot()
    bot.run(token)


if __name__ == "__main__":
    main()
