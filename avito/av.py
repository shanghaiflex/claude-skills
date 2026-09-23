#!/usr/bin/env python3
"""Avito through a real Chrome driven by Playwright over CDP.

Why this shape: Avito sits behind Qrator, which rate-limits by IP and answers with
"Доступ ограничен: проблема с IP" + a slider captcha. Two rules follow from that, and both are
baked in here:
  1. Never navigate to harvest. A page load is a fresh request through the edge, so it re-triggers
     the challenge. Requests go out through the tab's own context instead, reusing its cookies
     (including the Qrator pass), and the HTML is parsed with DOMParser inside that tab.
  2. The captcha is the user's to solve, in the visible window. Nothing here tries to pass it.

Since ~2026-09-22 an in-page fetch() no longer works for harvesting: Avito answers XHR with a
client-side shell (window.__staticRouterHydrationData and no markup), while the very same URL
served to a navigation still carries the full server-rendered listing. Sec-Fetch-* are forbidden
header names, so a page-side fetch() cannot dress itself up as a navigation — but Playwright's
APIRequestContext (page.context.request) can, and it shares the tab's cookie jar. Hence fetch_parse()
below. If Avito ever starts shelling that path too, the parser says {shell: true} rather than
quietly returning zero items.

The Chrome profile is persistent (PROFILE below), so a captcha the user solved once survives
across sessions together with the Qrator cookie.

  av.py launch [--query "умные весы"] [--city moskva]   # start the debuggable window
  av.py state                                          # title/url of the tab: ok | captcha | block
  av.py search "умные весы" [--city moskva] [--pages 3] [--out p.json]
  av.py card <url> [<url>...]                          # one line of JSON per card, stdout
  av.py drip urls.txt [--pause 40] [--out cards.jsonl] # slow harvest, backoff on block, resumable

Env: AV_PORT (9222), AV_PROFILE, AV_CHROME.
"""
import argparse, asyncio, json, os, subprocess, sys, time
from urllib.parse import quote_plus

PORT = int(os.environ.get("AV_PORT") or 9222)
PROFILE = os.environ.get("AV_PROFILE") or os.path.expanduser("~/.claude/tools/avito-profile")
CHROME = os.environ.get("AV_CHROME") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BLOCK_RE = r"Доступ ограничен|Ошибка 4|Captcha|Проблема с IP"

# --------------------------------------------------------------- page-side extraction
# Runs inside the tab on HTML fetched by fetch_parse(). Returns {list:[...]} for a search page,
# {card:{...}} for an item page, {blocked:true} when the edge served the challenge instead,
# {shell:true} when it served the JS shell that carries no listing.
PARSE_JS = r"""({html, u, status}) => {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const title = (doc.querySelector('title') || {}).textContent || '';
  if (/Доступ ограничен|Ошибка 4|Captcha|Проблема с IP/i.test(title) || status === 403 || status === 429)
    return {url: u, blocked: true, status, title};
  const txt = s => { const e = doc.querySelector(s); return e ? e.textContent.trim() : null; };
  if (!doc.querySelector('[data-marker="item"], [data-marker="item-view/title-info"], h1')
      && html.includes('__staticRouterHydrationData'))
    return {url: u, shell: true, status,
            note: 'client-side shell, no markup — Avito changed how this path is served'};
  const nodes = [...doc.querySelectorAll('[data-marker="item"]')];
  if (nodes.length) return {url: u, status, list: nodes.map(el => {
      const a = el.querySelector('a[data-marker="item-title"], a[itemprop="url"]');
      const p = el.querySelector('[itemprop="price"]');
      return {
        id: el.getAttribute('data-item-id'),
        title: ((el.querySelector('[itemprop="name"], a[data-marker="item-title"]') || {}).textContent || '').trim(),
        price: p ? Number(p.getAttribute('content')) : null,
        // the ?context=… tail is a tracking blob; the bare path is the shareable link
        url: a ? new URL(a.getAttribute('href'), location.origin).href.split('?')[0] : null,
        text: (el.innerText || '').replace(/\n+/g, ' | ').slice(0, 300)
      };
    })};
  // item page: the price lives in a different place than in the list, so try a chain
  let price = null;
  const pm = doc.querySelector('[itemprop="price"]');
  if (pm) price = Number(pm.getAttribute('content'));
  if (!price) { const m = html.match(/"price"\s*:\s*\{\s*"value"\s*:\s*(\d+)/); if (m) price = Number(m[1]); }
  if (!price) { const t = txt('[data-marker="item-view/item-price"]'); if (t) price = Number(t.replace(/\D+/g, '')) || null; }
  return {url: u, status, card: {
    title: txt('h1'),
    price,
    date: txt('[data-marker="item-view/item-date"]'),
    address: txt('[itemprop="address"]') || txt('[data-marker="item-view/item-address"]'),
    seller: txt('[data-marker="seller-info/name"]'),
    sellerBlock: [...doc.querySelectorAll('[data-marker^="seller-info"]')].map(e => e.textContent.trim()).slice(0, 10),
    params: [...new Set([...doc.querySelectorAll('[data-marker="item-view/item-params"] li, [data-marker="item-view/item-params"] p')].map(e => e.textContent.trim()))],
    description: txt('[data-marker="item-view/item-description"]'),
    closed: !!doc.querySelector('[data-marker="item-view/closed-warning"]')
  }};
}"""


def search_url(query, city="moskva", page=1):
    u = f"https://www.avito.ru/{city}?q={quote_plus(query)}"
    return u if page == 1 else u + f"&p={page}"


# Headers of a real navigation. Sec-Fetch-* are forbidden to page-side fetch() but fine here, and
# they are what tells the edge this is a document request rather than an XHR.
NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


_shell_seen = False  # once the cheap path is proven useless, stop paying for it on every page


async def request_parse(page, url):
    """The cheap path: a request through the tab's cookie jar, no navigation. May come back a shell."""
    try:
        r = await page.context.request.get(url, headers=NAV_HEADERS, timeout=45000)
        html, status = await r.text(), r.status
    except Exception as e:
        return {"url": url, "blocked": True, "status": None, "title": f"{type(e).__name__}: {e}"}
    return await page.evaluate(PARSE_JS, {"html": html, "u": url, "status": status})


async def nav_parse(page, url):
    """The costly path: a real navigation in the visible tab, then parse its DOM. This is a fresh
    request through the edge, so it counts against the Qrator quota — hence only as a fallback,
    and hence the pauses the callers keep between pages."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_selector('[data-marker="item"], [data-marker="item-view/title-info"]',
                                         timeout=20000)
        except Exception:
            pass  # closed listing, or the challenge — PARSE_JS decides which
        html = await page.content()
    except Exception as e:
        return {"url": url, "blocked": True, "status": None, "title": f"{type(e).__name__}: {e}"}
    r = await page.evaluate(PARSE_JS, {"html": html, "u": url, "status": 200})
    r["via"] = "navigation"
    return r


async def fetch_parse(page, url):
    """One page of Avito. Tries the cheap request first, falls back to navigating the tab when the
    edge answers with the JS shell — which is what it does for everything but navigations as of
    2026-09-22. If Avito goes back to serving markup, the cheap path starts working again by itself."""
    global _shell_seen
    if not _shell_seen:
        r = await request_parse(page, url)
        if not r.get("shell"):
            return r
        _shell_seen = True
        print("cheap path returns a JS shell — falling back to navigation", file=sys.stderr)
    return await nav_parse(page, url)


# --------------------------------------------------------------- browser plumbing
def launch(query=None, city="moskva"):
    """Start (or reuse) a debuggable Chrome on a profile of its own — never the user's."""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=3).read()
        print(f"chrome already listening on {PORT}")
        return
    except Exception:
        pass
    os.makedirs(PROFILE, exist_ok=True)
    url = search_url(query, city) if query else "https://www.avito.ru/"
    subprocess.Popen([CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                      "--no-first-run", "--no-default-browser-check", "--window-size=1440,960", url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).read()
            print(f"chrome up on {PORT}, profile {PROFILE}")
            return
        except Exception:
            continue
    sys.exit("chrome did not come up")


def new_tab(url):
    """Open a tab through the DevTools HTTP endpoint (Chrome 111+ wants PUT), then give it a moment."""
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{url}", method="PUT")
    urllib.request.urlopen(req, timeout=10).read()
    time.sleep(3)


async def tab():
    """A page sitting on avito.ru — fetch() is same-origin, so anything else (a New Tab page, a
    closed tab) makes every call fail with "Failed to fetch" / TargetClosedError. The human whose
    window this is may well close the tab or type somewhere else, so pick or restore it every time."""
    from playwright.async_api import async_playwright
    p = await async_playwright().start()
    try:
        b = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    except Exception as e:
        # Chrome alive but with every window closed has no default context, and connect_over_cdp
        # dies on Browser.setDownloadBehavior ("Browser context management is not supported").
        # Asking the browser itself for a tab over HTTP brings the context back.
        if "context management" not in str(e):
            raise
        new_tab("https://www.avito.ru/")
        b = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    if not b.contexts:
        sys.exit("no context in the debuggable chrome — run: av.py launch")
    ctx = b.contexts[0]
    page = next((pg for pg in ctx.pages if "avito.ru" in (pg.url or "")), None)
    if page is None:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        # the one navigation this tool allows itself: without the origin nothing can be fetched
        await page.goto("https://www.avito.ru/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
    return p, b, page


async def guard(page):
    """Refuse to harvest while the tab shows the challenge: fetch() would only echo it."""
    import re
    t = await page.title()
    if re.search(BLOCK_RE, t, re.I):
        print(json.dumps({"state": "captcha", "title": t,
                          "hint": "user must solve the slider in the open window; agent must not"},
                         ensure_ascii=False))
        return False
    return True


# --------------------------------------------------------------- commands
async def cmd_state():
    p, b, page = await tab()
    import re
    t = await page.title()
    state = "captcha" if re.search(BLOCK_RE, t, re.I) else "ok"
    print(json.dumps({"state": state, "title": t, "url": page.url}, ensure_ascii=False))
    await b.close(); await p.stop()


async def cmd_search(query, city, pages, out):
    p, b, page = await tab()
    if not await guard(page):
        await b.close(); await p.stop(); sys.exit(2)
    res = []
    for n in range(1, pages + 1):
        r = await fetch_parse(page, search_url(query, city, n))
        if r.get("blocked"):
            print(f"blocked on page {n} — IP quota spent, stop here", file=sys.stderr)
            break
        res.append(r)
        print(f"page {n}: {len(r.get('list') or [])} items", file=sys.stderr)
        # a navigation is a real request through the edge, so give it more room than a plain one
        await asyncio.sleep(5 if r.get("via") == "navigation" else 3)
    items = [it for r in res for it in (r.get("list") or [])]
    (open(out, "w") if out else sys.stdout).write(json.dumps(items, ensure_ascii=False, indent=1))
    await b.close(); await p.stop()


async def cmd_card(urls):
    p, b, page = await tab()
    if not await guard(page):
        await b.close(); await p.stop(); sys.exit(2)
    for u in urls:
        r = await fetch_parse(page, u)
        print(json.dumps(r, ensure_ascii=False))
        if r.get("blocked"):
            break
        await asyncio.sleep(3)
    await b.close(); await p.stop()


async def cmd_drip(path, pause, out):
    """One url at a time with a growing backoff; already-harvested urls are skipped, so the same
    file can be re-run after the IP cools down or after the user solves a captcha."""
    urls = [l.strip() for l in open(path) if l.strip() and not l.startswith("#")]
    done = set()
    if os.path.exists(out):
        done = {json.loads(l)["url"] for l in open(out)}
    p, b, page = await tab()
    if not await guard(page):
        await b.close(); await p.stop(); sys.exit(2)
    f = open(out, "a")
    for u in urls:
        if u in done:
            continue
        for attempt in range(4):
            try:
                r = await fetch_parse(page, u)
            except Exception as e:
                # tab closed or navigated away under us: take a fresh one and retry the same url
                print(f"tab lost ({type(e).__name__}), reattaching", flush=True)
                await b.close(); await p.stop()
                p, b, page = await tab()
                if not await guard(page):
                    f.close(); await b.close(); await p.stop(); sys.exit(2)
                continue
            if not r.get("blocked"):
                f.write(json.dumps(r, ensure_ascii=False) + "\n"); f.flush()
                got = r.get("card") or {}
                print("ok", got.get("price"), (got.get("title") or f"list:{len(r.get('list') or [])}")[:60], flush=True)
                break
            wait = 90 * (attempt + 1)
            print(f"blocked, backoff {wait}s {u[-32:]}", flush=True)
            await asyncio.sleep(wait)
        else:
            print("giveup " + u, flush=True)
        await asyncio.sleep(pause)
    await b.close(); await p.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("launch"); l.add_argument("--query"); l.add_argument("--city", default="moskva")
    sub.add_parser("state")
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--city", default="moskva")
    s.add_argument("--pages", type=int, default=3); s.add_argument("--out")
    c = sub.add_parser("card"); c.add_argument("urls", nargs="+")
    d = sub.add_parser("drip"); d.add_argument("file"); d.add_argument("--pause", type=int, default=40)
    d.add_argument("--out", default="cards.jsonl")
    a = ap.parse_args()
    if a.cmd == "launch":
        launch(a.query, a.city)
    elif a.cmd == "state":
        asyncio.run(cmd_state())
    elif a.cmd == "search":
        asyncio.run(cmd_search(a.query, a.city, a.pages, a.out))
    elif a.cmd == "card":
        asyncio.run(cmd_card(a.urls))
    elif a.cmd == "drip":
        asyncio.run(cmd_drip(a.file, a.pause, a.out))


if __name__ == "__main__":
    main()
