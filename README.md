# BSS Item Finder

Live web searcher for Bee Swarm Simulator items.

## Use
- **Run `BSSItemFinder.exe`** (or `python BSSItemFinder.py` with Python 3.10+)
- Pick an **item** (38 popular BSS items) and a **category**:
  - ⚡ Fastest Way / ♻️ Most Efficient / 💰 Cheapest / 📖 Wiki Sources / 💬 Reddit Tips
- Searches the live web **on open and on every selection**:
  - Bee Swarm Simulator Fandom wiki API (authoritative "how to obtain")
  - DuckDuckGo live results, ranked: WIKI > Reddit > YouTube > Web
- Click any result row/title to open it in the browser
- **⭐ Collect** saves the top results to `collected.json` (2nd tab)

## Network notes
- No API keys, stdlib only (urllib + tkinter).
- Handles DNS-poisoned networks: on connection failure it resolves the real IP
  via Cloudflare DNS-over-HTTPS and connects with correct SNI (fixes
  duckduckgo.com being hijacked to a black-holed IP on some ISPs).
