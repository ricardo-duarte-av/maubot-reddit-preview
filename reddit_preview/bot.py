"""Maubot plugin that previews Reddit posts/comments linked in Matrix rooms.

Only uses libraries bundled with maubot (aiohttp, yarl, mautrix, commonmark).
"""
from __future__ import annotations

import asyncio
import html
import re
import time
from typing import Any, NamedTuple, Optional, Type

import aiohttp
from yarl import URL

from maubot import MessageEvent, Plugin
from maubot.handlers import event
from mautrix.types import (
    EventType,
    Format,
    ImageInfo,
    MediaMessageEventContent,
    MessageType,
    TextMessageEventContent,
)
from mautrix.util import markdown
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

# Any URL pointing at reddit or redd.it
URL_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)?(?:reddit\.com|redd\.it)/[^\s<>\"'\)\]]*",
    re.IGNORECASE,
)
COMMENTS_RE = re.compile(
    r"/comments/(?P<post>[a-z0-9]+)(?:/[^/]*/(?P<comment>[a-z0-9]+))?", re.IGNORECASE
)
SHARE_RE = re.compile(r"/r/[^/]+/s/[A-Za-z0-9]+", re.IGNORECASE)
SHORT_RE = re.compile(r"^/(?P<post>[a-z0-9]+)/?$", re.IGNORECASE)
IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp)(?:\?.*)?$", re.IGNORECASE)
# Any http(s) URL; filtered down to git hosts by _is_git_host
ANY_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
# Upper bound on "Repo:" lines per preview, so link-dump posts don't flood the room
MAX_GIT_LINKS = 10


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        for key in (
            "reddit.client_id",
            "reddit.client_secret",
            "reddit.username",
            "reddit.password",
            "reddit.user_agent",
            "max_links_per_message",
            "max_body_length",
            "show_top_comments",
            "show_git_links",
            "comment_count",
            "max_comment_length",
            "upload_images",
            "max_image_size",
            "max_images",
            "nsfw_media",
            "use_notice",
            "reply",
            "ignore_users",
            "allowed_rooms",
        ):
            helper.copy(key)


class Target(NamedTuple):
    post_id: str
    comment_id: Optional[str]


class RedditError(Exception):
    pass


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip() + "…"


def _md(text: str) -> str:
    """Render Reddit markdown to (escaped) HTML."""
    return markdown.render(text, allow_html=False).strip()


def _num(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 10_000:
        return f"{n / 1000:.1f}k"
    return str(n)


# Well-known forges whose hostnames don't contain a "git"-like label.
# Subdomains match too (e.g. git.sr.ht, hg.sr.ht).
KNOWN_FORGES = (
    "codeberg.org",
    "bitbucket.org",
    "sr.ht",
    "framagit.org",
    "notabug.org",
    "pagure.io",
    "salsa.debian.org",
    "invent.kde.org",
    "code.launchpad.net",
    "launchpad.net",
    "sourceforge.net",
    "src.fedoraproject.org",
)
# Hostname labels that indicate a self-hosted forge: github, gitlab, gitea, git.example.com,
# forgejo.example.org, gogs.example.org, ...
FORGE_LABEL_PREFIXES = ("git", "forgejo", "gogs")
# Static-site hosts: <owner>.<pages host>/<repo>/... -> <forge>/<owner>/<repo>
PAGES_HOSTS = {
    "github.io": "https://github.com",
    "gitlab.io": "https://gitlab.com",
    "codeberg.page": "https://codeberg.org",
}


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _canonical_git_link(url: URL) -> Optional[str]:
    """Return the repo link for a git-hosting URL, or None if it's not one.

    Raw-file, gist and Pages hosts are mapped back to the repo they belong to.
    """
    host = (url.host or "").lower()
    parts = [p for p in url.path.split("/") if p]

    if host == "raw.githubusercontent.com":
        # /<owner>/<repo>/<ref>/<path>
        return f"https://github.com/{parts[0]}/{parts[1]}" if len(parts) >= 2 else None
    if host == "gist.githubusercontent.com":
        # /<owner>/<gist id>/raw/...
        return f"https://gist.github.com/{parts[0]}/{parts[1]}" if len(parts) >= 2 else None
    if _host_matches(host, "githubusercontent.com"):
        return None  # user-images, avatars, objects: no repo info

    for pages, forge in PAGES_HOSTS.items():
        if host.endswith("." + pages):
            owner = host[: -len(pages) - 1]
            if "." in owner:
                return None
            # owner.github.io/repo/... is a project site; bare owner.github.io is the
            # user site, which lives in the <owner>.github.io repo.
            repo = parts[0] if parts else host
            return f"{forge}/{owner}/{repo}"

    labels = host.split(".")
    if len(labels) < 2:
        return None
    if any(_host_matches(host, d) for d in KNOWN_FORGES) or any(
        lbl.startswith(FORGE_LABEL_PREFIXES) for lbl in labels[:-1]
    ):
        return str(url)
    return None


def _find_git_links(texts: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        # Reddit markdown escapes underscores etc. inside URLs ("foo\\_bar")
        for match in ANY_URL_RE.finditer(text.replace("\\", "")):
            link = match.group(0).rstrip(".,;:!?*")
            try:
                link = _canonical_git_link(URL(link))
            except ValueError:
                continue
            if not link:
                continue
            key = link.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            found.append(link)
            if len(found) >= MAX_GIT_LINKS:
                return found
    return found


class RedditPreviewBot(Plugin):
    _token: Optional[str]
    _token_expiry: float
    _token_lock: asyncio.Lock

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    async def start(self) -> None:
        self.config.load_and_update()
        self._token = None
        self._token_expiry = 0
        self._token_lock = asyncio.Lock()

    def on_external_config_update(self) -> None:
        self.config.load_and_update()
        self._token = None
        self._token_expiry = 0

    # ------------------------------------------------------------------ helpers

    @property
    def _user_agent(self) -> str:
        ua = (self.config["reddit.user_agent"] or "").strip()
        if ua:
            return ua
        user = (self.config["reddit.username"] or "").strip()
        by = f" (by /u/{user})" if user else ""
        return f"matrix:maubot-reddit-preview:v{self.loader.meta.version}{by}"

    @property
    def _has_app_creds(self) -> bool:
        return bool(self.config["reddit.client_id"] and self.config["reddit.client_secret"])

    async def _get_token(self, force: bool = False) -> Optional[str]:
        if not self._has_app_creds:
            return None
        async with self._token_lock:
            if not force and self._token and time.time() < self._token_expiry - 60:
                return self._token
            username = self.config["reddit.username"]
            password = self.config["reddit.password"]
            if username and password:
                data = {"grant_type": "password", "username": username, "password": password}
            else:
                data = {"grant_type": "client_credentials"}
            auth = aiohttp.BasicAuth(
                self.config["reddit.client_id"], self.config["reddit.client_secret"]
            )
            async with self.http.post(
                "https://www.reddit.com/api/v1/access_token",
                data=data,
                auth=auth,
                headers={"User-Agent": self._user_agent},
            ) as resp:
                body = await resp.json(content_type=None)
            if resp.status != 200 or "access_token" not in body:
                self._token = None
                raise RedditError(f"Reddit auth failed ({resp.status}): {body.get('error', body)}")
            self._token = body["access_token"]
            self._token_expiry = time.time() + float(body.get("expires_in", 3600))
            self.log.debug("Obtained new Reddit access token")
            return self._token

    async def _api_get(self, path: str, params: dict[str, str]) -> Any:
        params = {**params, "raw_json": "1"}
        headers = {"User-Agent": self._user_agent}
        token = await self._get_token()
        for attempt in range(2):
            if token:
                url = URL("https://oauth.reddit.com") / path.lstrip("/")
                headers["Authorization"] = f"Bearer {token}"
            else:
                url = URL("https://www.reddit.com") / (path.lstrip("/") + ".json")
            async with self.http.get(url.with_query(params), headers=headers) as resp:
                if resp.status == 401 and token and attempt == 0:
                    token = await self._get_token(force=True)
                    continue
                if resp.status != 200:
                    raise RedditError(f"Reddit API returned HTTP {resp.status} for {path}")
                return await resp.json(content_type=None)
        raise RedditError("Reddit API authentication failed")

    async def _resolve_share_link(self, url: str) -> Optional[str]:
        """Resolve /r/<sub>/s/<code> share links to the canonical post URL."""
        headers = {"User-Agent": self._user_agent}
        try:
            async with self.http.get(
                URL(url).with_host("www.reddit.com"),
                headers=headers,
                allow_redirects=False,
            ) as resp:
                return resp.headers.get("Location")
        except aiohttp.ClientError as e:
            self.log.warning(f"Failed to resolve share link {url}: {e}")
            return None

    async def _parse_target(self, raw_url: str) -> Optional[Target]:
        try:
            url = URL(raw_url)
        except ValueError:
            return None
        host = (url.host or "").lower()
        if host == "redd.it" or host.endswith(".redd.it"):
            if host != "redd.it":  # i.redd.it / v.redd.it are media, not posts
                return None
            m = SHORT_RE.match(url.path)
            return Target(m.group("post").lower(), None) if m else None
        if SHARE_RE.search(url.path):
            location = await self._resolve_share_link(raw_url)
            if not location:
                return None
            url = URL(location)
        m = COMMENTS_RE.search(url.path)
        if not m:
            return None
        comment = m.group("comment")
        return Target(m.group("post").lower(), comment.lower() if comment else None)

    # -------------------------------------------------------------- formatting

    def _post_images(self, post: dict) -> list[str]:
        meta = post.get("media_metadata") or {}

        def meta_url(media_id: str) -> Optional[str]:
            entry = meta.get(media_id) or {}
            src = entry.get("s") or {}
            if entry.get("e") == "Image" and src.get("u"):
                return src["u"]
            return None

        urls: list[str] = []
        gallery = post.get("gallery_data")
        if post.get("is_gallery") and meta and gallery:
            for item in gallery.get("items", []):
                u = meta_url(item.get("media_id", ""))
                if u:
                    urls.append(u)
            if urls:
                return urls
        if meta:
            # Text posts with inline images: keep the order they appear in the body,
            # then any remaining ones.
            body = post.get("selftext") or ""
            ids = sorted(meta, key=lambda k: (body.find(k) < 0, body.find(k)))
            urls = [u for u in (meta_url(k) for k in ids) if u]
            if urls:
                return urls
        url = post.get("url_overridden_by_dest") or post.get("url") or ""
        if IMAGE_EXT_RE.search(url):
            return [url]
        if post.get("post_hint") not in ("image", "link", "rich:video", "hosted:video"):
            return []
        try:
            return [post["preview"]["images"][0]["source"]["url"]]
        except (KeyError, IndexError, TypeError):
            return []

    def _format(
        self, post: dict, comments: list[dict], focused: bool
    ) -> tuple[str, str]:
        title = post.get("title", "")
        permalink = "https://www.reddit.com" + post.get("permalink", "")
        sub = post.get("subreddit_name_prefixed") or f"r/{post.get('subreddit', '?')}"
        author = post.get("author", "[deleted]")
        flags = []
        if post.get("over_18"):
            flags.append("NSFW")
        if post.get("spoiler"):
            flags.append("Spoiler")
        flag_txt = "".join(f"[{f}] " for f in flags)

        meta_txt = (
            f"{sub} · u/{author} · ⬆ {_num(post.get('score'))} · "
            f"💬 {_num(post.get('num_comments'))}"
        )

        plain = [f"{flag_txt}{title}", meta_txt]
        out = [
            "<blockquote>",
            f"<p><b>{html.escape(flag_txt)}<a href=\"{html.escape(permalink)}\">"
            f"{html.escape(title)}</a></b><br>",
            f"<sub>{html.escape(meta_txt)}</sub></p>",
        ]

        # Crossposts: show the original content
        source = post
        if post.get("crosspost_parent_list"):
            source = post["crosspost_parent_list"][0]
            xp = source.get("subreddit_name_prefixed", "")
            plain.append(f"Crossposted from {xp}")
            out.append(f"<p><i>Crossposted from {html.escape(xp)}</i></p>")

        if not source.get("is_self"):
            link = source.get("url_overridden_by_dest") or source.get("url")
            if link and "reddit.com" not in link:
                plain.append(f"🔗 {link}")
                out.append(
                    f"<p>🔗 <a href=\"{html.escape(link)}\">{html.escape(link)}</a></p>"
                )
            video = ((source.get("secure_media") or source.get("media") or {}).get(
                "reddit_video"
            ) or {}).get("fallback_url")
            if video:
                plain.append(f"🎞 {video}")
                out.append(f"<p>🎞 <a href=\"{html.escape(video)}\">video</a></p>")
            if source.get("is_gallery"):
                n = len((source.get("gallery_data") or {}).get("items", []))
                plain.append(f"🖼 Gallery with {n} items")
                out.append(f"<p>🖼 Gallery with {n} items</p>")

        body_limit = self.config["max_body_length"]
        selftext = source.get("selftext") or ""
        if selftext and body_limit > 0 and not (focused and comments):
            text = _truncate(selftext, body_limit)
            plain.append("")
            plain.append(text)
            out.append(_md(text))
        out.append("</blockquote>")

        if comments:
            header = "Linked comment" if focused else "Top comments"
            plain.append("")
            plain.append(f"{header}:")
            out.append(f"<p><b>{header}:</b></p>")
            limit = self.config["max_comment_length"]
            for c in comments:
                c_author = c.get("author", "[deleted]")
                c_meta = f"u/{c_author} · ⬆ {_num(c.get('score'))}"
                c_body = _truncate(c.get("body", ""), limit)
                plain.append(f"• {c_meta}: {c_body}")
                out.append(
                    f"<blockquote><p><b>{html.escape(c_meta)}</b></p>{_md(c_body)}</blockquote>"
                )

        if self.config["show_git_links"]:
            # Search the full (untruncated) text, so repos are listed even if cut from the preview
            texts = [
                post.get("title", ""),
                source.get("url_overridden_by_dest") or source.get("url") or "",
                source.get("selftext") or "",
                *(c.get("body", "") for c in comments),
            ]
            repos = _find_git_links(texts)
            if repos:
                plain.append("")
                out.append("<p>")
                for i, repo in enumerate(repos):
                    plain.append(f"Repo: {repo}")
                    br = "<br>" if i < len(repos) - 1 else ""
                    out.append(
                        f"<b>Repo:</b> <a href=\"{html.escape(repo)}\">{html.escape(repo)}</a>{br}"
                    )
                out.append("</p>")

        return "\n".join(plain), "\n".join(out)

    # ----------------------------------------------------------------- sending

    async def _fetch(self, target: Target) -> tuple[dict, list[dict]]:
        count = max(0, int(self.config["comment_count"]))
        if not self.config["show_top_comments"]:
            count = 0
        params = {"sort": "top", "depth": "1", "limit": str(max(count, 1))}
        if target.comment_id:
            params["comment"] = target.comment_id
            params["context"] = "0"
        data = await self._api_get(f"/comments/{target.post_id}", params)
        try:
            post = data[0]["data"]["children"][0]["data"]
        except (KeyError, IndexError, TypeError) as e:
            raise RedditError("Unexpected Reddit response") from e
        comments: list[dict] = []
        if target.comment_id or count > 0:
            for child in data[1]["data"]["children"] if len(data) > 1 else []:
                if child.get("kind") != "t1":
                    continue
                c = child["data"]
                if c.get("stickied") and not target.comment_id:
                    continue
                comments.append(c)
                if target.comment_id or len(comments) >= count:
                    break
        return post, comments

    async def _send_image(self, evt: MessageEvent, url: str) -> None:
        max_size = int(self.config["max_image_size"])
        async with self.http.get(url, headers={"User-Agent": self._user_agent}) as resp:
            if resp.status != 200:
                raise RedditError(f"Image download failed with HTTP {resp.status}")
            if resp.content_length and resp.content_length > max_size:
                return
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                buf.extend(chunk)
                if len(buf) > max_size:
                    return
            data = bytes(buf)
            mime = resp.content_type or "image/jpeg"
        if not mime.startswith("image/"):
            return
        filename = URL(url).name or "image"
        mxc = await self.client.upload_media(data, mime_type=mime, filename=filename, size=len(data))
        content = MediaMessageEventContent(
            msgtype=MessageType.IMAGE,
            body=filename,
            url=mxc,
            info=ImageInfo(mimetype=mime, size=len(data)),
        )
        await self.client.send_message(evt.room_id, content)

    async def _preview(self, evt: MessageEvent, target: Target) -> None:
        post, comments = await self._fetch(target)
        plain, formatted = self._format(post, comments, focused=bool(target.comment_id))
        content = TextMessageEventContent(
            msgtype=MessageType.NOTICE if self.config["use_notice"] else MessageType.TEXT,
            body=plain,
            format=Format.HTML,
            formatted_body=formatted,
        )
        if self.config["reply"]:
            content.set_reply(evt)
        await self.client.send_message(evt.room_id, content)

        if self.config["upload_images"]:
            source = (post.get("crosspost_parent_list") or [post])[0]
            if (source.get("over_18") or source.get("spoiler") or post.get("over_18")) and not \
                    self.config["nsfw_media"]:
                return
            limit = max(0, int(self.config["max_images"]))
            for img in self._post_images(source)[:limit]:
                try:
                    await self._send_image(evt, img)
                except Exception as e:
                    self.log.warning(f"Failed to send image {img}: {e}")

    @event.on(EventType.ROOM_MESSAGE)
    async def on_message(self, evt: MessageEvent) -> None:
        if evt.sender == self.client.mxid or evt.sender in (self.config["ignore_users"] or []):
            return
        allowed = self.config["allowed_rooms"] or []
        if allowed and evt.room_id not in allowed:
            return
        if evt.content.msgtype not in (MessageType.TEXT, MessageType.EMOTE):
            return
        body = evt.content.body or ""
        # Skip reply fallbacks ("> <@user> ...") so quoted links aren't re-previewed
        body = "\n".join(line for line in body.splitlines() if not line.startswith(">"))

        targets: list[Target] = []
        for match in URL_RE.finditer(body):
            try:
                target = await self._parse_target(match.group(0))
            except Exception as e:
                self.log.warning(f"Failed to parse {match.group(0)}: {e}")
                continue
            if target and target not in targets:
                targets.append(target)
            if len(targets) >= int(self.config["max_links_per_message"]):
                break

        for target in targets:
            try:
                await self._preview(evt, target)
            except Exception as e:
                self.log.warning(f"Failed to preview reddit post {target}: {e}")
