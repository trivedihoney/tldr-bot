import asyncio
import contextlib
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import discord
import yaml
from dotenv import load_dotenv


load_dotenv()

DEFAULT_FEED_URLS = (
    "TLDR AI|https://bullrich.dev/tldr-rss/ai.rss,"
    "TLDR Data|https://bullrich.dev/tldr-rss/data.rss"
)
CONFIG_FILE = Path(os.getenv("TLDR_BOT_CONFIG", "config.yml"))
FEED_URLS = os.getenv("TLDR_RSS_FEED_URLS") or os.getenv("FEED_URLS") or DEFAULT_FEED_URLS
NEWS_CHANNEL_ID = os.getenv("DISCORD_NEWS_CHANNEL_ID")
NEWS_CHANNEL_NAME = os.getenv("DISCORD_NEWS_CHANNEL_NAME", "news-channel")
DEFAULT_POLL_MINUTES = int(os.getenv("TLDR_POLL_MINUTES", "360"))
DEFAULT_FIRST_RUN_LIMIT = int(os.getenv("TLDR_FIRST_RUN_LIMIT", "5"))
DEFAULT_SEEN_FILE = Path(os.getenv("TLDR_SEEN_FILE", ".tldr_seen.json"))
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096


@dataclass(frozen=True)
class Feed:
    name: str
    url: str


@dataclass(frozen=True)
class FeedConfig(Feed):
    channel_id: int | None
    channel_name: str | None
    poll_minutes: int
    first_run_limit: int

    @property
    def tracking_key(self) -> str:
        channel_key = str(self.channel_id) if self.channel_id is not None else self.channel_name
        return f"{self.name}|{self.url}|{channel_key or ''}"


@dataclass(frozen=True)
class BotConfig:
    feeds: list[FeedConfig]
    seen_file: Path


@dataclass(frozen=True)
class NewsItem:
    feed_name: str
    title: str
    link: str
    summary: str
    published: datetime
    entry_id: str
    legacy_entry_id: str


class SeenEntries:
    def __init__(
        self,
        feed_entries: dict[str, set[str]] | None = None,
        legacy_entries: set[str] | None = None,
    ) -> None:
        self.feed_entries = feed_entries or {}
        self.legacy_entries = legacy_entries or set()

    @classmethod
    def load(cls, path: Path) -> "SeenEntries":
        if not path.exists():
            return cls()

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()

        if isinstance(data, list):
            return cls(legacy_entries={entry for entry in data if isinstance(entry, str)})

        if not isinstance(data, dict):
            return cls()

        feeds_data = data.get("feeds", {})
        if not isinstance(feeds_data, dict):
            feeds_data = {}

        legacy_data = data.get("legacy", [])
        if not isinstance(legacy_data, list):
            legacy_data = []

        feed_entries: dict[str, set[str]] = {}
        for key, entries in feeds_data.items():
            if isinstance(key, str) and isinstance(entries, list):
                feed_entries[key] = {entry for entry in entries if isinstance(entry, str)}

        legacy_entries = {entry for entry in legacy_data if isinstance(entry, str)}
        return cls(feed_entries=feed_entries, legacy_entries=legacy_entries)

    def save(self, path: Path) -> None:
        payload = {
            "feeds": {
                key: sorted(entries)
                for key, entries in sorted(self.feed_entries.items())
                if entries
            },
            "legacy": sorted(self.legacy_entries),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _entries_for(self, feed: FeedConfig) -> set[str]:
        return self.feed_entries.setdefault(feed.tracking_key, set())

    def has_seen_feed_entries(self, feed: FeedConfig, items: list[NewsItem]) -> bool:
        return any(self.is_seen(feed, item) for item in items)

    def is_seen(self, feed: FeedConfig, item: NewsItem) -> bool:
        feed_entries = self._entries_for(feed)
        return (
            item.entry_id in feed_entries
            or item.legacy_entry_id in feed_entries
            or item.entry_id in self.legacy_entries
            or item.legacy_entry_id in self.legacy_entries
        )

    def mark_seen(self, feed: FeedConfig, item: NewsItem) -> bool:
        feed_entries = self._entries_for(feed)
        before_count = len(feed_entries)
        feed_entries.add(item.entry_id)
        return len(feed_entries) != before_count

    def mark_all_seen(self, feed: FeedConfig, items: list[NewsItem]) -> bool:
        feed_entries = self._entries_for(feed)
        before_count = len(feed_entries)
        feed_entries.update(item.entry_id for item in items)
        return len(feed_entries) != before_count


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


def optional_text(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def coerce_int(value: Any, field_name: str, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc

    if number < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}.")

    return number


def coerce_optional_int(value: Any, field_name: str) -> int | None:
    if value is None or value == "":
        return None

    return coerce_int(value, field_name, minimum=1)


def mapping_value(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a mapping.")
    return value


def parse_feed_config(
    feed_data: dict[str, Any],
    index: int,
    defaults: dict[str, Any],
) -> FeedConfig:
    default_channel = mapping_value(defaults, "channel")
    feed_channel = mapping_value(feed_data, "channel")

    name = optional_text(feed_data.get("name")) or f"RSS Feed {index}"
    url = optional_text(feed_data.get("url"))
    if url is None:
        raise ValueError(f"feeds[{index}].url is required.")

    default_channel_id = coerce_optional_int(
        defaults.get("channel_id", default_channel.get("id", NEWS_CHANNEL_ID)),
        "defaults.channel_id",
    )
    default_channel_name = optional_text(
        defaults.get("channel_name", default_channel.get("name", NEWS_CHANNEL_NAME))
    )

    channel_id = coerce_optional_int(
        feed_data.get("channel_id", feed_channel.get("id", default_channel_id)),
        f"feeds[{index}].channel_id",
    )
    channel_name = optional_text(
        feed_data.get("channel_name", feed_channel.get("name", default_channel_name))
    )

    if channel_id is None and channel_name is None:
        raise ValueError(
            f"feeds[{index}] must define channel_id or channel_name, "
            "or provide a default channel."
        )

    poll_minutes = coerce_int(
        feed_data.get("poll_minutes", defaults.get("poll_minutes", DEFAULT_POLL_MINUTES)),
        f"feeds[{index}].poll_minutes",
        minimum=1,
    )
    first_run_limit = coerce_int(
        feed_data.get(
            "first_run_limit",
            defaults.get("first_run_limit", DEFAULT_FIRST_RUN_LIMIT),
        ),
        f"feeds[{index}].first_run_limit",
        minimum=0,
    )

    return FeedConfig(
        name=name,
        url=url,
        channel_id=channel_id,
        channel_name=channel_name,
        poll_minutes=poll_minutes,
        first_run_limit=first_run_limit,
    )


def parse_config_data(data: Any, path: Path) -> BotConfig:
    if data is None:
        data = {}

    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")

    defaults = mapping_value(data, "defaults")
    feeds_data = data.get("feeds", [])
    if not isinstance(feeds_data, list):
        raise ValueError("feeds must be a list.")

    feeds: list[FeedConfig] = []
    for index, feed_data in enumerate(feeds_data, start=1):
        if not isinstance(feed_data, dict):
            raise ValueError(f"feeds[{index}] must be a mapping.")
        feeds.append(parse_feed_config(feed_data, index, defaults))

    seen_file = Path(optional_text(data.get("seen_file")) or DEFAULT_SEEN_FILE)
    return BotConfig(feeds=feeds, seen_file=seen_file)


def load_config(path: Path = CONFIG_FILE) -> BotConfig:
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"Could not parse {path}: {exc}") from exc
        return parse_config_data(data, path)

    if "TLDR_BOT_CONFIG" in os.environ:
        raise FileNotFoundError(f"Config file not found: {path}")

    fallback_feeds = [
        FeedConfig(
            name=feed.name,
            url=feed.url,
            channel_id=coerce_optional_int(NEWS_CHANNEL_ID, "DISCORD_NEWS_CHANNEL_ID"),
            channel_name=NEWS_CHANNEL_NAME,
            poll_minutes=DEFAULT_POLL_MINUTES,
            first_run_limit=DEFAULT_FIRST_RUN_LIMIT,
        )
        for feed in parse_feed_urls(FEED_URLS)
    ]
    return BotConfig(feeds=fallback_feeds, seen_file=DEFAULT_SEEN_FILE)


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


async def find_news_channel(
    client: discord.Client,
    feed: FeedConfig,
) -> discord.abc.Messageable | None:
    if feed.channel_id is not None:
        channel = client.get_channel(feed.channel_id)
        if channel is None:
            channel = await client.fetch_channel(feed.channel_id)
        return channel if isinstance(channel, discord.abc.Messageable) else None

    if feed.channel_name is None:
        return None

    for guild in client.guilds:
        channel = discord.utils.get(guild.text_channels, name=feed.channel_name)
        if channel is not None:
            return channel

    return None


class TLDRBot(discord.Client):
    def __init__(self, config: BotConfig | None = None) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.config = config or load_config()
        self.seen_entries = SeenEntries.load(self.config.seen_file)
        self.seen_entries_lock = asyncio.Lock()
        self.poll_tasks: list[asyncio.Task[None]] = []

    async def setup_hook(self) -> None:
        for feed in self.config.feeds:
            task = asyncio.create_task(
                self.poll_feed_forever(feed),
                name=f"tldr-poll-{feed.name}",
            )
            self.poll_tasks.append(task)

    async def close(self) -> None:
        for task in self.poll_tasks:
            task.cancel()

        for task in self.poll_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        await super().close()

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user}")
        print(f"Polling {len(self.config.feeds)} TLDR RSS feed(s).")

    async def poll_feed_forever(self, feed: FeedConfig) -> None:
        await self.wait_until_ready()

        while not self.is_closed():
            try:
                await self.poll_feed(feed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Unexpected error while polling {feed.name}: {exc}")

            await asyncio.sleep(feed.poll_minutes * 60)

    async def poll_feed(self, feed: FeedConfig) -> None:
        try:
            channel = await find_news_channel(self, feed)
        except discord.HTTPException as exc:
            print(f"Could not fetch Discord channel for {feed.name}: {exc}")
            return

        if channel is None:
            channel_target = (
                f"ID {feed.channel_id}"
                if feed.channel_id is not None
                else f"#{feed.channel_name}"
            )
            print(f"Could not find Discord channel {channel_target} for {feed.name}.")
            return

        try:
            items = await asyncio.to_thread(fetch_feed_items, feed)
        except (ET.ParseError, TimeoutError, URLError, OSError) as exc:
            print(f"Failed to fetch {feed.name} TLDR RSS feed: {exc}")
            return

        async with self.seen_entries_lock:
            is_first_run = not self.seen_entries.has_seen_feed_entries(feed, items)
            unseen_items = [
                item for item in items if not self.seen_entries.is_seen(feed, item)
            ]

        if is_first_run:
            unseen_items = unseen_items[-feed.first_run_limit :] if feed.first_run_limit else []

        if not unseen_items:
            print(f"No new {feed.name} items found.")
        else:
            posted_count = 0
            for item in unseen_items:
                try:
                    await channel.send(embed=format_news_item(item))
                except discord.HTTPException as exc:
                    print(f"Failed to post {item.title!r} from {feed.name}: {exc}")
                    continue

                posted_count += 1
                async with self.seen_entries_lock:
                    if self.seen_entries.mark_seen(feed, item):
                        self.seen_entries.save(self.config.seen_file)

            print(f"Posted {posted_count} {feed.name} item(s).")

        if is_first_run:
            async with self.seen_entries_lock:
                if self.seen_entries.mark_all_seen(feed, items):
                    self.seen_entries.save(self.config.seen_file)


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set.")

    bot = TLDRBot()
    bot.run(token)
