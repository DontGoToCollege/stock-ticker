"""Stock Ticker — local server.

Serves the UI and proxies Yahoo Finance (no API key needed):
  /api/stock?symbol=AAPL&range=1d   quote + candles for the chart
  /api/news?symbol=AAPL             recent news articles
  /api/search?q=apple               ticker autocomplete
  /api/article?url=...              server-side article text extraction (built-in reader)
  /api/open?url=...                 open a URL in the user's default browser

Run:  python server.py   (then it opens http://127.0.0.1:8750 in your browser)
"""
import gzip
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0.0"
HOST, PORT = "127.0.0.1", 8750
ROOT = Path(__file__).resolve().parent
SSL_CTX = ssl.create_default_context()

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

RANGE_INTERVALS = {"1d": "1m", "5d": "5m", "1mo": "30m", "6mo": "1d", "1y": "1d", "5y": "1wk"}


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        charset = resp.headers.get_content_charset() or "utf-8"
        return body, charset, resp.geturl()


def fetch_json(url, timeout=15):
    body, charset, _ = fetch(url, timeout)
    return json.loads(body.decode(charset, errors="replace"))


# ---------------------------------------------------------------- article extraction

class ArticleExtractor(HTMLParser):
    """Readability-lite: collect paragraph/heading text, skipping page chrome."""
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "form",
            "button", "select", "nav", "footer", "aside", "figure", "video", "audio"}
    BLOCK = {"p", "h1", "h2", "h3", "h4", "li", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.block_stack = []
        self.blocks = []          # (tag, text) in document order
        self.title_parts = []
        self.in_title = False
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            a = dict(attrs)
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
            return
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
        elif tag in self.BLOCK:
            self.block_stack.append((tag, []))

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = False
        elif self.block_stack and tag == self.block_stack[-1][0]:
            btag, parts = self.block_stack.pop()
            text = re.sub(r"\s+", " ", "".join(parts)).strip()
            if text:
                if self.block_stack:            # e.g. <p> inside <li>
                    self.block_stack[-1][1].append(" " + text)
                else:
                    self.blocks.append((btag, text))

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        elif self.block_stack:
            self.block_stack[-1][1].append(data)


def extract_article(url):
    try:
        body, charset, final_url = fetch(url, timeout=20)
    except urllib.error.HTTPError as e:
        return {"ok": False, "reason": f"The site refused the request (HTTP {e.code})."}
    except Exception as e:
        return {"ok": False, "reason": f"Could not reach the site ({e.__class__.__name__})."}

    html = body.decode(charset, errors="replace")
    m = re.search(r'<meta[^>]+charset=["\']?([\w-]+)', html[:2048], re.I)
    if m and m.group(1).lower() not in (charset.lower(), "utf-8"):
        try:
            html = body.decode(m.group(1), errors="replace")
        except LookupError:
            pass

    parser = ArticleExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass

    # keep headings, and paragraphs long enough to be prose
    content = []
    for tag, text in parser.blocks:
        if tag in ("h1", "h2", "h3", "h4"):
            content.append({"tag": "h", "text": text})
        elif len(text) >= 60 or (len(text) >= 25 and text.endswith((".", "!", "?", '"', "”"))):
            content.append({"tag": "p", "text": text})
    prose_len = sum(len(b["text"]) for b in content if b["tag"] == "p")
    if prose_len < 500:
        return {"ok": False,
                "reason": "The article text couldn't be extracted — the site likely "
                          "requires JavaScript, a login, or blocks readers."}

    title = (parser.meta.get("og:title") or "".join(parser.title_parts).strip()
             or (content[0]["text"] if content and content[0]["tag"] == "h" else "Article"))
    return {
        "ok": True,
        "title": re.sub(r"\s+", " ", title).strip(),
        "site": parser.meta.get("og:site_name", ""),
        "description": parser.meta.get("og:description", ""),
        "url": final_url,
        "blocks": content[:400],
    }


# ---------------------------------------------------------------- yahoo endpoints

def get_stock(symbol, rng):
    rng = rng if rng in RANGE_INTERVALS else "1d"
    interval = RANGE_INTERVALS[rng]
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?interval={interval}&range={rng}"
           "&includePrePost=false&events=div%2Csplit")
    data = fetch_json(url)
    result = (data.get("chart", {}).get("result") or [None])[0]
    err = data.get("chart", {}).get("error")
    if not result:
        msg = (err or {}).get("description") or f'No data found for "{symbol}".'
        return {"ok": False, "reason": msg}

    meta = result.get("meta", {})
    ts = result.get("timestamp") or []
    ind = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = ind.get("close") or []
    opens = ind.get("open") or []
    candles = [{"t": t, "c": c} for t, c in zip(ts, closes) if c is not None]
    first_open = next((o for o in opens if o is not None), None)

    prev_close = meta.get("chartPreviousClose") if rng == "1d" else None
    return {
        "ok": True,
        "symbol": meta.get("symbol", symbol.upper()),
        "name": meta.get("longName") or meta.get("shortName") or symbol.upper(),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName", ""),
        "currency": meta.get("currency", "USD"),
        "price": meta.get("regularMarketPrice"),
        "previousClose": meta.get("previousClose") or meta.get("chartPreviousClose"),
        "chartPreviousClose": prev_close,
        "open": first_open,
        "dayHigh": meta.get("regularMarketDayHigh"),
        "dayLow": meta.get("regularMarketDayLow"),
        "volume": meta.get("regularMarketVolume"),
        "week52High": meta.get("fiftyTwoWeekHigh"),
        "week52Low": meta.get("fiftyTwoWeekLow"),
        "marketTime": meta.get("regularMarketTime"),
        "timezone": meta.get("exchangeTimezoneName", ""),
        "instrument": meta.get("instrumentType", ""),
        "range": rng,
        "candles": candles,
    }


def get_news(symbol):
    url = ("https://query1.finance.yahoo.com/v1/finance/search?"
           + urllib.parse.urlencode({"q": symbol, "quotesCount": 0, "newsCount": 15,
                                     "enableFuzzyQuery": "false"}))
    data = fetch_json(url)
    items = []
    for n in data.get("news", []):
        thumb = ""
        for res in (n.get("thumbnail", {}) or {}).get("resolutions", []):
            if res.get("url"):
                thumb = res["url"]
                if res.get("width", 999) <= 400:
                    break
        items.append({
            "title": n.get("title", ""),
            "publisher": n.get("publisher", ""),
            "link": n.get("link", ""),
            "time": n.get("providerPublishTime", 0),
            "thumbnail": thumb,
        })
    return {"ok": True, "items": [i for i in items if i["title"] and i["link"]]}


def search_symbols(query):
    url = ("https://query1.finance.yahoo.com/v1/finance/search?"
           + urllib.parse.urlencode({"q": query, "quotesCount": 8, "newsCount": 0}))
    data = fetch_json(url)
    out = []
    for q in data.get("quotes", []):
        if not q.get("symbol"):
            continue
        out.append({"symbol": q["symbol"],
                    "name": q.get("shortname") or q.get("longname") or "",
                    "exchange": q.get("exchDisp", ""),
                    "type": q.get("typeDisp", "")})
    return {"ok": True, "items": out[:8]}


# ---------------------------------------------------------------- http server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        arg = lambda k, d="": (qs.get(k) or [d])[0].strip()
        try:
            if parsed.path == "/" or parsed.path == "/index.html":
                body = (ROOT / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/stock":
                sym = arg("symbol")
                if not sym:
                    return self.send_json({"ok": False, "reason": "Missing symbol."}, 400)
                self.send_json(get_stock(sym, arg("range", "1d")))
            elif parsed.path == "/api/news":
                sym = arg("symbol")
                if not sym:
                    return self.send_json({"ok": False, "reason": "Missing symbol."}, 400)
                self.send_json(get_news(sym))
            elif parsed.path == "/api/search":
                q = arg("q")
                self.send_json(search_symbols(q) if q else {"ok": True, "items": []})
            elif parsed.path == "/api/article":
                url = arg("url")
                if not re.match(r"^https?://", url):
                    return self.send_json({"ok": False, "reason": "Invalid URL."}, 400)
                self.send_json(extract_article(url))
            elif parsed.path == "/api/open":
                url = arg("url")
                if not re.match(r"^https?://", url):
                    return self.send_json({"ok": False, "reason": "Invalid URL."}, 400)
                webbrowser.open(url)
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "reason": "Not found."}, 404)
        except urllib.error.HTTPError as e:
            self.send_json({"ok": False, "reason": f"Upstream error (HTTP {e.code})."}, 502)
        except Exception as e:
            self.send_json({"ok": False, "reason": f"{e.__class__.__name__}: {e}"}, 500)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Stock Ticker {VERSION} running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
