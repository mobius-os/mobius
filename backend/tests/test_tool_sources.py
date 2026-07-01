"""normalize_tool_sources: extract title/url/snippet from provider payloads,
dedupe by url, and only ever emit http(s) URLs — a source feeds straight into an
<a href> in the client, so javascript:/data: must never survive."""
from app.tool_sources import normalize_tool_sources


def test_extracts_title_url_snippet_and_dedupes():
    raw = {"results": [
        {"title": "A", "url": "https://a.example/x", "snippet": "s"},
        {"name": "B", "uri": "http://b.example"},
        {"url": "https://a.example/x"},  # duplicate url — dropped
    ]}
    assert normalize_tool_sources(raw) == [
        {"title": "A", "url": "https://a.example/x", "snippet": "s"},
        {"title": "B", "url": "http://b.example"},
    ]


def test_drops_non_http_urls():
    raw = {"sources": [
        {"title": "x", "url": "javascript:alert(1)"},
        {"title": "y", "url": "data:text/html,<script>alert(1)</script>"},
        {"title": "z", "url": "ftp://host/file"},
        {"title": "ok", "url": "https://ok.example"},
    ]}
    assert normalize_tool_sources(raw) == [
        {"title": "ok", "url": "https://ok.example"},
    ]


def test_empty_and_non_mapping_inputs_are_safe():
    assert normalize_tool_sources(None) == []
    assert normalize_tool_sources([]) == []
    assert normalize_tool_sources("nope") == []
    assert normalize_tool_sources({"results": []}) == []
