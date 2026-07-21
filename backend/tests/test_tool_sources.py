"""normalize_tool_sources: extract title/url/snippet from provider payloads,
dedupe by url, and only ever emit http(s) URLs — a source feeds straight into an
<a href> in the client, so javascript:/data: must never survive."""
from app.tool_sources import (
  MAX_SOURCE_SNIPPET_CHARS,
  MAX_SOURCE_TITLE_CHARS,
  MAX_SOURCE_URL_CHARS,
  MAX_TOOL_SOURCES,
  normalize_tool_sources,
  sources_from_websearch_text,
)


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


def test_duplicate_keeps_position_but_gains_richer_metadata():
  url = "https://a.example/x"
  assert normalize_tool_sources({"results": [
    {"url": url},
    {"title": "Useful title", "url": url, "snippet": "Useful context"},
  ]}) == [{
    "title": "Useful title", "url": url, "snippet": "Useful context",
  }]


def test_source_metadata_has_small_fixed_resource_bounds():
  raw = [{
    "title": "t" * (MAX_SOURCE_TITLE_CHARS + 50),
    "url": f"https://example.com/{i}",
    "snippet": "s" * (MAX_SOURCE_SNIPPET_CHARS + 50),
  } for i in range(MAX_TOOL_SOURCES + 50)]

  sources = normalize_tool_sources(raw)

  assert len(sources) == MAX_TOOL_SOURCES
  assert all(len(source["title"]) <= MAX_SOURCE_TITLE_CHARS
             for source in sources)
  assert all(len(source["snippet"]) <= MAX_SOURCE_SNIPPET_CHARS
             for source in sources)
  assert normalize_tool_sources({
    "url": "https://example.com/" + "x" * MAX_SOURCE_URL_CHARS,
  }) == []


def test_cyclic_and_deep_payloads_cannot_recurse_forever():
  cyclic: dict = {}
  cyclic["content"] = [cyclic]
  assert normalize_tool_sources(cyclic) == []

  root: dict = {}
  current = root
  for _ in range(100):
    child: dict = {}
    current["content"] = child
    current = child
  current.update({"title": "too deep", "url": "https://deep.example"})
  assert normalize_tool_sources(root) == []


def test_drops_non_http_urls():
  raw = {"sources": [
    {"title": "x", "url": "javascript:alert(1)"},
    {"title": "y", "url": "data:text/html,<script>alert(1)</script>"},
    {"title": "z", "url": "ftp://host/file"},
    {"title": "no host", "url": "https://"},
    {"title": "bad host", "url": "https://exa mple.com/page"},
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


def test_sources_from_websearch_text_extracts_links_before_trailing_text():
  text = (
    "Web search results for query: \"the query\"\n\n"
    "Links: [{\"title\":\"A\",\"url\":\"https://a.example/x\","
    "\"snippet\":\"s\"},{\"title\":\"B\",\"url\":\"http://b.example\"},"
    "{\"title\":\"A again\",\"url\":\"https://a.example/x\"}]\n\n"
    "Free text continues after the array."
  )

  assert sources_from_websearch_text(text) == [
    {"title": "A", "url": "https://a.example/x", "snippet": "s"},
    {"title": "B", "url": "http://b.example"},
  ]


def test_sources_from_websearch_text_without_links_marker_is_empty():
  assert sources_from_websearch_text(
    "Web search results for query: \"the query\"\n\nNo links here."
  ) == []
  assert sources_from_websearch_text(None) == []


def test_sources_from_websearch_text_with_malformed_json_is_empty():
  assert sources_from_websearch_text(
    "Web search results for query: \"the query\"\n\n"
    "Links: [{\"title\":\"A\",\"url\":\"https://a.example/x\"}\n\n"
    "Free text continues after the array."
  ) == []


def test_sources_from_websearch_text_keeps_http_url_guard():
  text = (
    "Web search results for query: \"the query\"\n\n"
    "Links: [{\"title\":\"x\",\"url\":\"javascript:alert(1)\"},"
    "{\"title\":\"y\",\"url\":\"data:text/html,<script>alert(1)</script>\"},"
    "{\"title\":\"z\",\"url\":\"ftp://host/file\"},"
    "{\"title\":\"ok\",\"url\":\"https://ok.example\"}]\n\n"
    "Free text continues after the array."
  )

  assert sources_from_websearch_text(text) == [
    {"title": "ok", "url": "https://ok.example"},
  ]
