from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser


class _Strip(HTMLParser):
    BLOCK = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def html_to_text(s: str) -> str:
    """Job APIs return HTML, sometimes escaped twice (Greenhouse). Unescape, strip tags, tidy."""
    if not s:
        return ""
    if "&lt;" in s:
        s = html.unescape(s)
    p = _Strip()
    p.feed(s)
    text = html.unescape("".join(p.out)).replace("\xa0", " ")
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


@dataclass
class Job:
    ats: str  # greenhouse | lever | ashby | workable | rss | hn
    company: str
    title: str
    url: str
    description: str = ""
    location: str = ""
    remote: bool | None = None
    posted: datetime | None = None
    salary_min: int | None = None  # yearly, USD-ish; None = not stated
    salary_max: int | None = None
    board: str = ""  # ATS board slug
    ats_id: str = ""  # ATS job id
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        key = f"{self.ats}|{self.board}|{self.ats_id}" if self.ats_id else f"{self.ats}|{norm(self.company)}|{norm(self.title)}|{self.url}"
        return hashlib.sha1(key.encode()).hexdigest()[:10]
