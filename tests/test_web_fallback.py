"""The zero-config web fallback must fill Hermes' gaps WITHOUT clobbering a
backend the user actually configured.

Hermes ships a multi-backend web system (firecrawl/tavily/exa/ddgs/…). Our
crawl4ai/httpx fallback (teams_server.web_crawl4ai) should only override a
capability when Hermes has no provider for it — otherwise a user who configured
(and pays for) firecrawl would silently get downgraded to a raw HTTP fetch.

These tests steer a fake ``agent.web_search_registry`` to reproduce the three
deployment cases the design targets:

  1a. user configured a real backend (firecrawl) -> we leave both alone
  1b/2. zero-config (ddgs search only, no extract) -> we override extract only
  registry unavailable (older Hermes) -> fail open and override both
"""
import sys
import types

import pytest

import teams_server.web_crawl4ai as wc


class _FakeProvider:
    def __init__(self, search: bool, extract: bool, available: bool = True):
        self._search, self._extract, self._available = search, extract, available

    name = "fake"

    def supports_search(self) -> bool:
        return self._search

    def supports_extract(self) -> bool:
        return self._extract

    def is_available(self) -> bool:
        return self._available


@pytest.fixture
def fake_registry(monkeypatch):
    """Install a steerable fake ``agent.web_search_registry``. Yields a dict the
    test sets to control what Hermes' resolver returns for each capability."""
    state = {"search": None, "extract": None}

    fake = types.ModuleType("agent.web_search_registry")
    fake.get_active_search_provider = lambda: state["search"]
    fake.get_active_extract_provider = lambda: state["extract"]
    agentmod = types.ModuleType("agent")
    agentmod.web_search_registry = fake

    monkeypatch.setitem(sys.modules, "agent", agentmod)
    monkeypatch.setitem(sys.modules, "agent.web_search_registry", fake)
    # Each test starts with a clean "what have we already overridden" set.
    monkeypatch.setattr(wc, "_OVERRIDDEN", set())
    return state


def test_configured_backend_is_not_clobbered(fake_registry):
    """Case 1a: a configured firecrawl (search+extract) must be left in place."""
    fake_registry["search"] = _FakeProvider(search=True, extract=True)
    fake_registry["extract"] = _FakeProvider(search=True, extract=True)

    assert wc._hermes_has_provider("search") is True
    assert wc._hermes_has_provider("extract") is True


def test_zero_config_overrides_extract_only(fake_registry):
    """Case 1b/2: ddgs gives zero-config search but there is no extract backend.

    We must leave search to ddgs and override only extract (the real gap)."""
    fake_registry["search"] = _FakeProvider(search=True, extract=False)  # ddgs
    fake_registry["extract"] = None  # nothing extract-capable configured

    assert wc._hermes_has_provider("search") is True      # -> don't override search
    assert wc._hermes_has_provider("extract") is False     # -> override extract


def test_registry_unavailable_fails_open(monkeypatch):
    """Older/newer Hermes without the provider registry: fail open to override so
    a fresh install still gets a working web fallback rather than nothing."""
    # Ensure the import inside _hermes_has_provider raises.
    monkeypatch.setitem(sys.modules, "agent.web_search_registry", None)
    assert wc._hermes_has_provider("extract") is False
    assert wc._hermes_has_provider("search") is False


def test_installer_skips_configured_capability(fake_registry):
    """End to end: with firecrawl configured, the installer registers NOTHING."""
    fake_registry["search"] = _FakeProvider(search=True, extract=True)
    fake_registry["extract"] = _FakeProvider(search=True, extract=True)

    registered = []

    class _Reg:
        def get_schema(self, name):
            return {"name": name, "description": "d", "parameters": {}}

        def register(self, **kw):
            registered.append(kw["name"])

    wc.install_crawl4ai_web_tools(_Reg())
    assert registered == []
    assert wc._OVERRIDDEN == set()


def test_installer_overrides_only_the_gap(fake_registry):
    """End to end: zero-config -> installer overrides web_extract but not web_search."""
    fake_registry["search"] = _FakeProvider(search=True, extract=False)  # ddgs
    fake_registry["extract"] = None

    registered = []

    class _Reg:
        def get_schema(self, name):
            return {"name": name, "description": "d", "parameters": {}}

        def register(self, **kw):
            registered.append(kw["name"])

    wc.install_crawl4ai_web_tools(_Reg())
    assert registered == ["web_extract"]
    assert wc._OVERRIDDEN == {"web_extract"}


def test_installer_is_idempotent(fake_registry):
    """Per-agent setup calls the installer repeatedly; a tool is registered once."""
    fake_registry["search"] = _FakeProvider(search=True, extract=False)
    fake_registry["extract"] = None

    calls = []

    class _Reg:
        def get_schema(self, name):
            return {"name": name, "description": "d", "parameters": {}}

        def register(self, **kw):
            calls.append(kw["name"])

    reg = _Reg()
    wc.install_crawl4ai_web_tools(reg)
    wc.install_crawl4ai_web_tools(reg)
    wc.install_crawl4ai_web_tools(reg)
    assert calls == ["web_extract"]  # registered exactly once across 3 calls


def test_httpx_fallback_returns_content():
    """The httpx fetch path (used when crawl4ai isn't installed) must return clean
    text, so a fresh `pip install` without the [web] extra still extracts pages.
    Patches tools.web_tools-independent httpx via the module's lazy import."""
    import teams_server.web_crawl4ai as mod

    class _Resp:
        text = "<html><head><style>x{}</style></head><body>Hello <b>World</b></body></html>"

        def raise_for_status(self):
            return None

    class _FakeHttpx:
        @staticmethod
        def get(url, **kw):
            return _Resp()

    sys.modules["httpx"] = _FakeHttpx  # _httpx_fetch does `import httpx` lazily
    try:
        out = mod._httpx_fetch("https://example.com")
    finally:
        # restore the real module so other tests/imports are unaffected
        import importlib
        sys.modules.pop("httpx", None)
        importlib.import_module("httpx")
    assert "Hello World" in out
    assert "<b>" not in out and "style" not in out
