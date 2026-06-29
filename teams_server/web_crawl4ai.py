"""Zero-config web_search + web_extract fallback (crawl4ai → ddgs / httpx).

Hermes ships a full multi-backend web system (firecrawl, parallel, tavily, exa,
searxng, brave-free, ddgs, …) selected from the user's API keys / config.yaml.
We deliberately do NOT replace a backend the user has actually configured — that
would silently downgrade a paid/JS-rendering provider (e.g. firecrawl) to a raw
HTTP fetch. Instead this module fills the *gap*:

  * Hermes' zero-config **search** default is ddgs and it works — so we leave it.
  * Hermes has **no** zero-config **extract** backend: with no provider keys,
    ``get_active_extract_provider()`` returns None and ``web_extract`` fails with
    a "configure a provider" error. That's the gap we close.

``install_crawl4ai_web_tools()`` therefore overrides a capability ONLY when
Hermes has no available provider for it (asking Hermes' own resolver, so we never
duplicate its selection logic). When it does override, it uses crawl4ai as the
primary engine if that optional package is installed (JS rendering via the
``[web]`` extra), and falls back to ddgs (search) / httpx (fetch) otherwise —
both of which are core dependencies, so the fallback always works.

Called from teams_server.tools._register_custom_tools() (team agents) and the
Architect setup in master.py; re-registers the existing tool names with
registry.register(..., override=True) so the schema agents see is unchanged.

Handlers are SYNC and run the async crawl4ai code in a FRESH event loop on a
dedicated thread, so they work no matter what loop/thread context Hermes invokes
them from (verified: Playwright launches fine off the main thread with a fresh
asyncio.run loop).
"""
import asyncio
import contextlib
import io
import json
import logging
import re
import threading
import urllib.parse

log = logging.getLogger(__name__)

_FETCH_TIMEOUT = 60
_SEARCH_TIMEOUT = 45
_MAX_CONTENT_CHARS = 100_000


def _run_async(coro, timeout):
    """Run a coroutine to completion in a fresh event loop on a dedicated thread.

    Robust against being called from a thread that already owns a running loop
    (which would make asyncio.run raise). crawl4ai/Playwright tolerate a fresh
    loop on a non-main thread.
    """
    box = {}

    def _runner():
        try:
            # crawl4ai is chatty on stdout; keep the server log clean.
            with contextlib.redirect_stdout(io.StringIO()):
                box["value"] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001 - surfaced to caller
            box["error"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"crawl4ai operation exceeded {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# --------------------------------------------------------------------------- #
# crawl4ai primitives
# --------------------------------------------------------------------------- #
async def _c4a_fetch(url: str) -> str:
    from crawl4ai import AsyncWebCrawler

    async with AsyncWebCrawler(verbose=False) as crawler:
        res = await crawler.arun(url=url)
        if not res.success:
            raise RuntimeError(
                f"crawl4ai fetch failed: {getattr(res, 'error_message', 'unknown')}"
            )
        return res.markdown or ""


# Each organic result lives in its own <div class="result ..."> block; split on
# the block boundary so a result's link, title and snippet are parsed from the
# SAME element (ads are then dropped per-block, keeping everything aligned).
_RESULT_BLOCK_RE = re.compile(r'<div[^>]*\bclass="[^"]*\bresult\b[^"]*"', re.S)
_RESULT_A_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip(html: str) -> str:
    return _TAG_RE.sub("", html or "").strip()


async def _c4a_search(query: str, limit: int):
    """Primary search: fetch the DuckDuckGo HTML SERP via crawl4ai and parse
    organic result anchors (decoding DDG's uddg redirect, dropping ads)."""
    from crawl4ai import AsyncWebCrawler

    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    async with AsyncWebCrawler(verbose=False) as crawler:
        res = await crawler.arun(url=url)
    html = res.html or ""

    # Split the SERP into per-result blocks so each link is paired with the
    # snippet from its own block; ad blocks are skipped wholesale, so a leading
    # ad can never shift snippets onto the wrong organic result.
    bounds = [m.start() for m in _RESULT_BLOCK_RE.finditer(html)]
    if bounds:
        blocks = [html[bounds[i]:(bounds[i + 1] if i + 1 < len(bounds) else len(html))]
                  for i in range(len(bounds))]
    else:
        blocks = [html]  # fallback: treat the whole page as one block

    results = []
    for block in blocks:
        m = _RESULT_A_RE.search(block)
        if not m:
            continue
        href, title = m.group(1), _strip(m.group(2))
        parsed = urllib.parse.urlparse(href if href.startswith("http") else "https:" + href)
        uddg = urllib.parse.parse_qs(parsed.query).get("uddg", [None])[0]
        real = uddg or href
        if not real:
            continue
        if "duckduckgo.com/y.js" in real or "ad_provider=" in real:  # paid ad slot
            continue
        item = {"title": title, "url": real}
        sm = _SNIPPET_RE.search(block)
        if sm:
            item["snippet"] = _strip(sm.group(1))[:300]
        results.append(item)
        if len(results) >= limit:
            break
    return results


# --------------------------------------------------------------------------- #
# fallbacks
# --------------------------------------------------------------------------- #
def _ddgs_search(query: str, limit: int):
    from ddgs import DDGS

    out = []
    for r in DDGS().text(query, max_results=limit):
        out.append(
            {
                "title": r.get("title", ""),
                "url": r.get("href", "") or r.get("url", ""),
                "snippet": (r.get("body", "") or "")[:300],
            }
        )
    return out


def _httpx_fetch(url: str) -> str:
    import httpx

    r = httpx.get(
        url,
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; TeamsBot/1.0)"},
    )
    r.raise_for_status()
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip()


# --------------------------------------------------------------------------- #
# tool handlers  (sync, return JSON string)
# --------------------------------------------------------------------------- #
def web_search_handler(args: dict, **kwargs) -> str:
    query = (args.get("query") or "").strip()
    try:
        limit = max(1, min(int(args.get("limit") or 5), 15))
    except (TypeError, ValueError):
        limit = 5
    if not query:
        return json.dumps({"error": "query is required"})

    provider, results = "crawl4ai", []
    try:
        results = _run_async(_c4a_search(query, limit), _SEARCH_TIMEOUT) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("[web_search] crawl4ai failed (%s) — falling back to ddgs", exc)

    if not results:
        try:
            results = _ddgs_search(query, limit)
            provider = "ddgs(backup)"
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {"error": f"crawl4ai and ddgs both failed: {exc}", "query": query}
            )

    return json.dumps(
        {"provider": provider, "query": query, "count": len(results), "results": results},
        ensure_ascii=False,
    )


def web_extract_handler(args: dict, **kwargs) -> str:
    urls = args.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    urls = [u for u in (urls or []) if u][:5]
    if not urls:
        return json.dumps({"error": "urls (a list of URLs) is required"})

    out = []
    for url in urls:
        provider, content, err = "crawl4ai", None, None
        try:
            content = _run_async(_c4a_fetch(url), _FETCH_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            log.warning("[web_extract] crawl4ai failed for %s (%s) — httpx fallback", url, exc)
            try:
                content = _httpx_fetch(url)
                provider = "httpx(backup)"
            except Exception as exc2:  # noqa: BLE001
                err = f"crawl4ai: {exc} | httpx: {exc2}"
        item = {"url": url, "provider": provider}
        if err:
            item["error"] = err
        else:
            item["content"] = (content or "")[:_MAX_CONTENT_CHARS]
        out.append(item)

    return json.dumps({"results": out}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# installer — called from _register_custom_tools()
# --------------------------------------------------------------------------- #
# Tools we have already overridden in this process (avoids noisy re-logging when
# every agent's setup calls the installer; registry.register is idempotent).
_OVERRIDDEN: set = set()


def _hermes_has_provider(capability: str) -> bool:
    """True when Hermes already has a web provider for ``capability`` ("search"
    or "extract") that we must not clobber — either one the user explicitly
    configured (config.yaml ``web.backend`` / ``web.{cap}_backend``) or one that
    is available from their API keys. False when none is configured/available
    (the gap we fill).

    Reuses Hermes' own resolver so our gate never drifts from its selection
    logic. ``get_active_*_provider`` returns an explicitly-configured provider
    even without credentials (so the user gets Hermes' precise "add your key"
    error rather than a silent swap to our HTTP fetch), and otherwise returns the
    highest-priority provider the user actually has credentials for — or None.
    Fails open to False (override) if Hermes' registry can't be queried, so a
    fresh install still gets a working fallback."""
    try:
        from agent.web_search_registry import (
            get_active_search_provider,
            get_active_extract_provider,
        )
    except Exception:  # noqa: BLE001 — older/newer Hermes without this registry
        return False

    getter = get_active_search_provider if capability == "search" else get_active_extract_provider
    try:
        provider = getter()
        if provider is None:
            return False
        supports = provider.supports_search() if capability == "search" else provider.supports_extract()
        return bool(supports)
    except Exception as exc:  # noqa: BLE001
        log.debug("[web_crawl4ai] provider probe for '%s' failed: %s", capability, exc)
        return False


def install_crawl4ai_web_tools(registry) -> bool:
    """Install our zero-config web fallback, but ONLY for capabilities Hermes has
    no provider for (see module docstring + ``_hermes_has_provider``). Safe to
    call repeatedly (per-agent setup does). Returns True if any override is now
    in place from this process."""
    overrides = (
        ("web_search", web_search_handler, "search"),
        ("web_extract", web_extract_handler, "extract"),
    )
    for name, handler, capability in overrides:
        if name in _OVERRIDDEN:
            continue
        if _hermes_has_provider(capability):
            log.info("[web_crawl4ai] Hermes has a configured '%s' provider — "
                     "leaving built-in '%s' in place.", capability, name)
            continue
        schema = registry.get_schema(name)
        if not schema:
            # Built-in web toolset not present in this build; define a minimal schema.
            if name == "web_search":
                schema = {
                    "name": "web_search",
                    "description": "Search the web and return ranked results (title, url, snippet).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "The search query."},
                            "limit": {"type": "integer", "description": "Max results (default 5)."},
                        },
                        "required": ["query"],
                    },
                }
            else:
                schema = {
                    "name": "web_extract",
                    "description": "Fetch one or more URLs and return their content as clean markdown.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "urls": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "URLs to fetch (max 5).",
                            }
                        },
                        "required": ["urls"],
                    },
                }
        try:
            registry.register(
                name=name,
                toolset="web",
                schema=schema,
                handler=handler,
                is_async=False,
                override=True,
                max_result_size_chars=_MAX_CONTENT_CHARS,
                description=schema.get("description", ""),
            )
            _OVERRIDDEN.add(name)
            log.info("[web_crawl4ai] No Hermes '%s' provider — installed crawl4ai/"
                     "httpx fallback for '%s'.", capability, name)
        except Exception as exc:  # noqa: BLE001
            log.error("[web_crawl4ai] Failed to override '%s': %s", name, exc)
    return bool(_OVERRIDDEN)
