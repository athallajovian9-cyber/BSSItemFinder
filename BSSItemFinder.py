#!/usr/bin/env python3
"""
BSS Item Finder — real-time web searcher for Bee Swarm Simulator items.

- Pick an item (40+ popular BSS items) and a category (Fastest / Most Efficient /
  Cheapest / Wiki Sources / Reddit Tips).
- On open (and on every selection) it searches the web LIVE: DuckDuckGo HTML
  (no API key) plus the Bee Swarm Simulator Fandom wiki API for authoritative
  "how to obtain" info.
- Ranked sources (wiki > reddit > youtube > web), deduped, snippets shown.
- "Collect" saves results to collected.json; collected entries open in the 2nd tab.
- Stdlib only (urllib, tkinter) — no pip installs.

Run:  python BSSItemFinder.py
"""

from __future__ import annotations

import base64
import html
import http.client
import json
import os
import queue
import re
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTED_FILE = os.path.join(HERE, "collected.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = 12

WIKI_API = "https://bee-swarm-simulator.fandom.com/api.php"
DDG_HTML = "https://html.duckduckgo.com/html/"

# ---------------------------------------------------------------- item catalog

ITEMS = {
    "Jellies": [
        "Royal Jelly", "Star Jelly", "Silver Jelly", "Gold Jelly", "Diamond Jelly",
        "Mythic Jelly",
    ],
    "Eggs": [
        "Basic Egg", "Silver Egg", "Gold Egg", "Diamond Egg", "Mythic Egg",
        "Gummybee Egg",
    ],
    "Materials": [
        "Wax", "Glue", "Glitter", "Oil", "Enzymes", "Tape",
        "Red Extract", "Blue Extract", "White Extract",
        "Red Cannonball", "Field Dice", "Smooth Dice", "Loaded Dice",
        "Tropical Diamond", "Humongous Pollen",
    ],
    "Currency": ["Honey", "Tickets", "Bond", "Science Bear Tokens"],
    "Quest / Special": [
        "Star Treat", "Sticker Stack", "Petal Belt", "Honey Loop",
        "Cooling Drive", "Red Port-O-Hive", "Blue Port-O-Hive",
    ],
}

SEARCH_MODES = {
    "⚡ Fastest Way": "Bee Swarm Simulator fastest way to get {item} minutes hour grind",
    "♻️ Most Efficient": "Bee Swarm Simulator {item} best method most efficient per hour",
    "💰 Cheapest": "Bee Swarm Simulator cheapest way to get {item} cost price free",
    "📖 Wiki Sources": "site:bee-swarm-simulator.fandom.com {item}",
    "💬 Reddit Tips": "site:reddit.com Bee Swarm Simulator {item} how to get tips",
}

# Category signals: which sentences count as an answer for each category.
# The searcher FETCHES the top pages and keeps only sentences matching these,
# so every category produces genuinely different findings from the same sources.
CATEGORY_SIGNALS: dict[str, list[str]] = {
    "⚡ Fastest Way": [
        r"\d+\s*(?:s(?:ec(?:onds?)?)?|m(?:in(?:utes?)?)?|h(?:ours?)?)\b",
        r"fastest", r"quick", r"per hour", r"grind(?:ing)?", r"solo(?:able)?",
        r"takes?\s+\d", r"\d+\s*per (?:hour|day|run)",
    ],
    "♻️ Most Efficient": [
        r"best way", r"most efficient", r"worth it", r"per hour", r"rate",
        r"yield", r"method", r"soothing", r"convert(?:ing)?", r"return",
    ],
    "💰 Cheapest": [
        r"cheap(?:est)?", r"free", r"cost(?:s|ing)?", r"price", r"buy",
        r"purchas", r"\d[\d,]*\s*honey", r"afford", r"budget",
    ],
    "📖 Wiki Sources": [
        r"obtain(?:ed|ing)?", r"source", r"acquisition", r"can be (?:got|gained|found)",
        r"quest", r"reward", r"purchased", r"drop(?:ped|s)?", r"craft(?:ing)?",
        r"milestone", r"badge",
    ],
    "💬 Reddit Tips": [
        r"tip", r"advice", r"recommend", r"should", r"try", r"usually",
        r"always", r"farm(?:ing)?", r"i (?:would|do|use)",
    ],
}

SOURCE_RANK = [
    ("bee-swarm-simulator.fandom.com", 0, "WIKI"),
    ("reddit.com", 1, "REDDIT"),
    ("youtube.com", 2, "YOUTUBE"),
    ("tiktok.com", 3, "VIDEO"),
    ("discord.com", 4, "WEB"),
]

# --------------------------------------------------------------------- helpers


def doh_lookup(host: str) -> str | None:
    """Resolve through Cloudflare DNS-over-HTTPS — bypasses poisoned local DNS
    (duckduckgo.com is hijacked to a black-holed IP on some networks)."""
    try:
        q = urllib.parse.urlencode({"name": host, "type": "A"})
        req = urllib.request.Request(
            "https://cloudflare-dns.com/dns-query?" + q,
            headers={"User-Agent": UA, "Accept": "application/dns-json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
        return ips[-1] if ips else None
    except Exception:
        return None


def _get_via_ip(url: str, ip: str) -> str:
    """HTTPS to `ip` while keeping real Host + SNI so certificates validate."""
    u = urllib.parse.urlparse(url)
    host = u.hostname or ""
    ctx = ssl.create_default_context()
    raw = socket.create_connection((ip, 443), timeout=TIMEOUT)
    ssock = ctx.wrap_socket(raw, server_hostname=host)
    conn = http.client.HTTPSConnection(host, 443, context=ctx, timeout=TIMEOUT)
    conn.sock = ssock  # preset: http.client skips its own (poisoned) resolve
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    conn.request("GET", path, headers={"User-Agent": UA, "Accept": "*/*",
                                       "Connection": "close"})
    resp = conn.getresponse()
    body = resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace")
    conn.close()
    return body


def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, "replace")
    except urllib.error.HTTPError:
        raise  # server answered (4xx/5xx) — not a connectivity issue
    except Exception:
        # connection-level failure → retry through a DoH-resolved real IP
        host = urllib.parse.urlparse(url).hostname
        if not host:
            raise
        ip = doh_lookup(host)
        if not ip:
            raise
        return _get_via_ip(url, ip)


class _LinkParser(HTMLParser):
    """Extracts result links + snippets from DuckDuckGo's HTML endpoint."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict] = []
        self._in_title = False
        self._in_snippet = False
        self._href = ""
        self._title = ""
        self._snippet = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        cls = d.get("class") or ""
        if tag == "a" and "result__a" in cls:
            self._in_title = True
            self._href = d.get("href") or ""
            self._title = ""
        elif tag == "a" and "result__snippet" in cls:
            self._in_snippet = True
            self._snippet = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
            if self._href:
                self.results.append({"url": self._href, "title": self._title.strip()})
        elif tag == "a" and self._in_snippet:
            self._in_snippet = False
            if self.results and not self.results[-1].get("snippet"):
                self.results[-1]["snippet"] = self._snippet.strip()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title += data
        elif self._in_snippet:
            self._snippet += data


def _ddg_clean(url: str) -> str:
    """DDG html wraps targets as //duckduckgo.com/l/?uddg=<encoded>."""
    if "uddg=" in url:
        try:
            return urllib.parse.unquote(url.split("uddg=", 1)[1].split("&", 1)[0])
        except Exception:
            return url
    return url


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


def wiki_text(title: str) -> str:
    """Full page text through the MediaWiki API (article HTML 403s to urllib)."""
    q = urllib.parse.urlencode({"action": "parse", "page": title,
                                "prop": "text", "format": "json"})
    data = json.loads(http_get(f"{WIKI_API}?{q}"))
    raw = data.get("parse", {}).get("text", {}).get("*", "")
    return _strip_html(raw)


def fetch_excerpts(url: str, mode: str, limit: int = 2) -> list[str]:
    """Fetch a page and keep only the sentences that answer THIS category
    (time/grind lines for Fastest, cost/free lines for Cheapest, ...)."""
    signals = CATEGORY_SIGNALS.get(mode, [])
    if not signals:
        return []
    if "fandom.com/wiki/" in url:
        title = urllib.parse.unquote(url.split("/wiki/", 1)[1]).replace("_", " ")
        text = wiki_text(title)
    else:
        text = _strip_html(http_get(url))
    sentences = re.split(r"(?<=[.!?])\s+", text)
    scored: list[tuple[int, int, str]] = []
    for i, s in enumerate(sentences):
        s = s.strip()
        if not (50 <= len(s) <= 400):
            continue
        hits = sum(1 for sig in signals if re.search(sig, s, re.I))
        if hits:
            scored.append((hits, -i, s))
    scored.sort(reverse=True)
    return [s for _, _, s in scored[:limit]]


def _safe_excerpts(url: str, mode: str) -> list[str]:
    try:
        return fetch_excerpts(url, mode)
    except Exception:
        return []


def search_ddg(query: str, limit: int = 8) -> list[dict]:
    body = http_get(DDG_HTML + "?q=" + urllib.parse.quote(query))
    parser = _LinkParser()
    parser.feed(body)
    out = []
    for r in parser.results:
        url = _ddg_clean(r["url"])
        if not url.startswith("http"):
            continue
        out.append({"url": url, "title": r.get("title", ""), "snippet": r.get("snippet", "")})
        if len(out) >= limit:
            break
    return out


def search_bing(query: str, limit: int = 8) -> list[dict]:
    """Bing HTML fallback — used when DuckDuckGo rate-limits us."""
    body = http_get("https://www.bing.com/search?q=" + urllib.parse.quote(query) + "&count=20")
    out: list[dict] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', body, re.S):
        h2 = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not h2:
            continue
        url = html.unescape(h2.group(1))
        if "bing.com/ck/a" in url:
            # Bing wraps organic links: u=a1<base64url> is the real target
            mu = re.search(r"[?&]u=a1([^&]+)", url)
            if not mu:
                continue
            pad = "=" * (-len(mu.group(1)) % 4)
            try:
                url = base64.urlsafe_b64decode(mu.group(1) + pad).decode("utf-8", "replace")
            except Exception:
                continue
            if not url.startswith("http"):
                continue
        snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        out.append({
            "url": url,
            "title": _strip_html(h2.group(2))[:120],
            "snippet": _strip_html(snip.group(1))[:300] if snip else "",
        })
        if len(out) >= limit:
            break
    return out


def wiki_page_exists(item: str) -> str | None:
    """Ask the Fandom MediaWiki API which page title matches this item."""
    q = urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": item,
        "format": "json", "srlimit": "1", "srprop": "",
    })
    data = json.loads(http_get(f"{WIKI_API}?{q}"))
    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def wiki_extract(title: str) -> tuple[str, str]:
    """Return (page_url, obtain-section text) using the parse API."""
    q = urllib.parse.urlencode({
        "action": "parse", "page": title, "prop": "text", "format": "json",
    })
    data = json.loads(http_get(f"{WIKI_API}?{q}"))
    raw = data.get("parse", {}).get("text", {}).get("*", "")
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # grab the most relevant 'obtain' window
    m = re.search(r"(how to obtain|obtain|obtaining|ways to get|acquisition)", text, re.I)
    window = text[m.start(): m.start() + 420] if m else text[:420]
    url = "https://bee-swarm-simulator.fandom.com/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
    return url, window + ("…" if len(text) > 420 else "")


def classify(url: str) -> tuple[int, str]:
    for dom, rank, label in SOURCE_RANK:
        if dom in url:
            return rank, label
    return 5, "WEB"


BSS_HINTS = ("bee swarm", "beeswarm", "roblox", "fandom.com", "bss")


def _bss_related(r: dict) -> bool:
    hay = (str(r.get("title", "")) + " " + str(r.get("url", ""))).lower()
    return any(h in hay for h in BSS_HINTS)


def web_search(item: str, mode: str) -> list[dict]:
    """Full search: wiki authority block + ranked DDG results, deduped.
    Then fetches the top pages and extracts category-specific findings."""
    results: list[dict] = []

    # 1) Fandom wiki (authoritative for BSS)
    try:
        title = wiki_page_exists(item)
        if title:
            url, window = wiki_extract(title)
            results.append({
                "url": url,
                "title": f"WIKI: {title}",
                "snippet": window,
                "label": "WIKI",
                "rank": 0,
            })
    except Exception as e:  # wiki unreachable ≠ fatal
        results.append({
            "url": "https://bee-swarm-simulator.fandom.com/",
            "title": f"WIKI unavailable ({e.__class__.__name__})",
            "snippet": "Fandom API could not be reached — showing web results only.",
            "label": "WIKI",
            "rank": 0,
        })

    # 2) DuckDuckGo live results
    try:
        mode_query = SEARCH_MODES.get(mode, "{item}")
        web_hits = search_ddg(mode_query.format(item=item))
        if not web_hits:  # DDG rate-limited/challenged → try Bing
            web_hits = search_bing(mode_query.format(item=item))
        for r in web_hits:
            rank, label = classify(r["url"])
            if not _bss_related(r):
                rank = max(rank, 6)  # biology/mythology pages sink to the bottom
            r["rank"], r["label"] = rank, label
            results.append(r)
    except Exception as e:
        results.append({
            "url": "https://duckduckgo.com/",
            "title": f"Web search failed ({e.__class__.__name__})",
            "snippet": "Check your internet connection.",
            "label": "WEB",
            "rank": 5,
        })

    # dedupe by URL, rank-sort (stable for equal rank)
    seen: set[str] = set()
    deduped = []
    for r in sorted(results, key=lambda x: x["rank"]):
        key = r["url"].split("#", 1)[0]
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # category-specific excerpts from the top eligible pages (parallel fetch)
    eligible = [r for r in deduped
                if r.get("label") in ("WIKI", "WEB", "REDDIT") and _bss_related(r)][:3]
    if eligible:
        with ThreadPoolExecutor(max_workers=3) as pool:
            excerpts = list(pool.map(lambda r: _safe_excerpts(r["url"], mode), eligible))
        for src, ex in zip(eligible, excerpts):
            src["excerpts"] = ex
    return deduped


def load_collected() -> list[dict]:
    try:
        with open(COLLECTED_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_collected(entries: list[dict]) -> None:
    with open(COLLECTED_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


# ------------------------------------------------------------------------- GUI

BG = "#0b0f14"
PANEL = "#121822"
FG = "#dfe7f1"
ACCENT = "#00d0ff"
MUTED = "#8a97a8"
TAG_COLORS = {
    "WIKI": "#7c6cff", "REDDIT": "#ff7a45", "YOUTUBE": "#ff4d6d",
    "VIDEO": "#ff4d6d", "WEB": "#00d0ff",
}


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("BSS Item Finder — live web search")
        self.geometry("1080x720")
        self.configure(bg=BG)
        self.results: list[dict] = []
        self.ui_queue: queue.Queue = queue.Queue()
        self._search_gen = 0  # cancels stale in-flight searches visually

        self._build_widgets()
        self.after(100, self._poll_queue)
        # search immediately on open (first item of first category)
        self.after(300, self.start_search)

    # ------------------------------------------------------------- layout
    def _build_widgets(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=FG, padding=(16, 8))
        style.map("TNotebook.Tab", background=[("selected", "#1b2536")])

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=16, pady=(14, 6))

        tk.Label(top, text="BSS ITEM FINDER", bg=BG, fg=ACCENT,
                 font=("Consolas", 16, "bold")).pack(side="left")

        self.status = tk.Label(top, text="searching…", bg=BG, fg=MUTED,
                               font=("Consolas", 10))
        self.status.pack(side="right")

        sel = tk.Frame(self, bg=BG)
        sel.pack(fill="x", padx=16, pady=6)

        tk.Label(sel, text="Item:", bg=BG, fg=FG, font=("Segoe UI", 11)).pack(side="left")
        self.item_var = tk.StringVar()
        self.item_box = ttk.Combobox(sel, textvariable=self.item_var, state="readonly",
                                     width=34, font=("Segoe UI", 11))
        values: list[str] = []
        self.item_to_group: dict[str, str] = {}
        for group, items in ITEMS.items():
            for it in items:
                values.append(f"{group} › {it}")
                self.item_to_group[f"{group} › {it}"] = it
        self.item_box["values"] = values
        self.item_box.current(0)
        self.item_box.pack(side="left", padx=(8, 24))
        self.item_box.bind("<<ComboboxSelected>>", lambda e: self.start_search())

        tk.Label(sel, text="Category:", bg=BG, fg=FG, font=("Segoe UI", 11)).pack(side="left")
        self.mode_var = tk.StringVar(value=list(SEARCH_MODES)[0])
        mode_box = ttk.Combobox(sel, textvariable=self.mode_var, state="readonly",
                                width=24, font=("Segoe UI", 11))
        mode_box["values"] = list(SEARCH_MODES)
        mode_box.pack(side="left", padx=(8, 16))
        mode_box.bind("<<ComboboxSelected>>", lambda e: self.start_search())

        tk.Button(sel, text="🔎 Search", command=self.start_search, bg=ACCENT, fg="#001018",
                  activebackground="#66eaff", font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=14, pady=4).pack(side="left")

        tk.Button(sel, text="⭐ Collect", command=self.collect_current, bg="#2a3550", fg=FG,
                  activebackground="#3a4a70", font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=14, pady=4).pack(side="left", padx=(8, 0))

        # tabs: live results / collected
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=16, pady=(4, 14))

        live = tk.Frame(self.notebook, bg=PANEL)
        collected = tk.Frame(self.notebook, bg=PANEL)
        self.notebook.add(live, text="  Live Results  ")
        self.notebook.add(collected, text="  Collected  ")

        # results list (clickable rows)
        self.results_box = tk.Text(live, bg=PANEL, fg=FG, insertbackground=FG,
                                   font=("Consolas", 11), wrap="word", relief="flat",
                                   padx=14, pady=12, cursor="arrow", state="disabled")
        sb = ttk.Scrollbar(live, command=self.results_box.yview)
        self.results_box.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.results_box.pack(fill="both", expand=True)
        for tag, color in TAG_COLORS.items():
            self.results_box.tag_configure(tag, foreground=color)
        self.results_box.tag_configure("title", foreground="#ffffff", font=("Consolas", 11, "bold"))
        self.results_box.tag_configure("muted", foreground=MUTED)
        self.results_box.tag_configure("url", foreground="#4ea6ff", underline=True)
        self.results_box.tag_bind("url", "<Button-1>", self._open_under_cursor)
        self.results_box.tag_bind("title", "<Button-1>", self._open_under_cursor)

        # collected list
        self.collected_box = tk.Text(collected, bg=PANEL, fg=FG, font=("Consolas", 11),
                                     wrap="word", relief="flat", padx=14, pady=12,
                                     state="disabled")
        sb2 = ttk.Scrollbar(collected, command=self.collected_box.yview)
        self.collected_box.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y")
        self.collected_box.pack(fill="both", expand=True)
        self.collected_box.tag_configure("title", foreground=ACCENT,
                                         font=("Consolas", 12, "bold"))
        self.collected_box.tag_configure("url", foreground="#4ea6ff", underline=True)
        self.collected_box.tag_configure("muted", foreground=MUTED)
        self.refresh_collected()

    # ------------------------------------------------------------- search
    def start_search(self) -> None:
        item = self.item_to_group.get(self.item_var.get())
        if not item:
            return
        mode = self.mode_var.get()
        self._search_gen += 1
        gen = self._search_gen
        self.status.config(text=f"searching live: {item} — {mode}…")
        self._set_results_text("Searching the web… (fetching pages for category findings)", "muted")
        threading.Thread(target=self._worker, args=(gen, item, mode),
                         daemon=True).start()

    def _worker(self, gen: int, item: str, mode: str) -> None:
        try:
            results = web_search(item, mode)
        except Exception as e:  # belt & suspenders
            results = [{"url": "", "title": f"Search failed: {e}", "snippet": "",
                        "label": "WEB", "rank": 5}]
        self.ui_queue.put((gen, item, results))

    def _poll_queue(self) -> None:
        try:
            while True:
                gen, item, results = self.ui_queue.get_nowait()
                if gen != self._search_gen:
                    continue  # stale result
                self.results = results
                self._render_results(item)
                self.status.config(text=f"{len(results)} live results for {item}")
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)

    def _set_results_text(self, text: str, tag: str = "muted") -> None:
        self.results_box.config(state="normal")
        self.results_box.delete("1.0", "end")
        self.results_box.insert("end", text + "\n", tag)
        self.results_box.config(state="disabled")

    def _render_results(self, item: str) -> None:
        box = self.results_box
        box.config(state="normal")
        box.delete("1.0", "end")
        box.insert("end", f"▼ {item}  —  {self.mode_var.get()}\n\n", "title")
        if not self.results:
            box.insert("end", "No results found.\n", "muted")

        # KEY FINDINGS: the best category-matching passage from each top source
        findings: list[tuple[str, str]] = []
        for r in self.results:
            for ex in (r.get("excerpts") or [])[:1]:
                findings.append((r.get("label", "WEB"), ex))
            if len(findings) >= 3:
                break
        if findings:
            box.insert("end", "KEY FINDINGS\n", "title")
            for label, ex in findings:
                tag = label if label in TAG_COLORS else "muted"
                box.insert("end", f"  [{label}] ", tag)
                box.insert("end", f"{ex}\n", "muted")
            box.insert("end", "\n")

        for i, r in enumerate(self.results, 1):
            label = r.get("label", "WEB")
            box.insert("end", f"[{label}] ", label if label in TAG_COLORS else "muted")
            box.insert("end", f"{r['title']}\n", "title")
            excerpts = r.get("excerpts") or []
            for ex in excerpts:
                box.insert("end", f"    ✂ {ex}\n", "muted")
            snippet = (r.get("snippet") or "").strip()
            if snippet and not excerpts:
                box.insert("end", f"    {snippet}\n", "muted")
            if r.get("url"):
                box.insert("end", f"    {r['url']}\n\n", "url")
        box.config(state="disabled")

    def _open_under_cursor(self, _event) -> None:
        """Open the URL of the row under the mouse (title or url line)."""
        try:
            index = self.results_box.index(f"@{0},{0}")
            del index  # placeholder to keep linters quiet
        except Exception:
            pass
        # find nearest URL line above cursor
        pos = self.results_box.index("current")
        line = int(pos.split(".")[0])
        total = int(self.results_box.index("end-1c").split(".")[0])
        for ln in range(line, 0, -1):
            text = self.results_box.get(f"{ln}.0", f"{ln}.end")
            if text.strip().startswith("http"):
                webbrowser.open(text.strip())
                return
        del total

    # ------------------------------------------------------------- collect
    def collect_current(self) -> None:
        if not self.results:
            return
        entries = load_collected()
        item = self.item_to_group.get(self.item_var.get(), "?")
        entries.insert(0, {
            "item": item,
            "category": self.mode_var.get(),
            "top": [
                {"title": r.get("title", ""), "url": r.get("url", "")}
                for r in self.results[:5]
            ],
        })
        save_collected(entries)
        self.refresh_collected()
        self.status.config(text=f"collected {item} ({len(entries)} total)")

    def refresh_collected(self) -> None:
        box = self.collected_box
        box.config(state="normal")
        box.delete("1.0", "end")
        entries = load_collected()
        if not entries:
            box.insert("end", "Nothing collected yet — hit ⭐ Collect on a search.\n", "muted")
        for e in entries:
            box.insert("end", f"● {e.get('item', '?')}  ", "title")
            box.insert("end", f"— {e.get('category', '')}\n", "muted")
            for t in e.get("top", []):
                if t.get("url"):
                    box.insert("end", f"    {t.get('title','')}\n", "muted")
                    box.insert("end", f"    {t['url']}\n", "url")
            box.insert("end", "\n")
        box.config(state="disabled")


def main() -> None:
    if sys.version_info < (3, 10):
        print("Python 3.10+ required")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
