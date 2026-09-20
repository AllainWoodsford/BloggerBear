from common.source_refs import dedupe_source_refs


def test_dedupe_source_refs_collapses_trailing_slash_url_variants():
    assert dedupe_source_refs(
        [
            {"url": "https://example.com/path", "title": "Example"},
            {"url": "https://example.com/path/", "title": "Example"},
        ]
    ) == [{"url": "https://example.com/path", "title": "Example"}]


def test_dedupe_source_refs_fills_missing_metadata_from_later_duplicate():
    assert dedupe_source_refs(
        [
            {"url": "https://example.com/path"},
            {
                "url": "https://example.com/path/",
                "title": "Example",
                "accessed_at": "2026-09-20T00:00:00+00:00",
            },
        ]
    ) == [
        {
            "url": "https://example.com/path",
            "title": "Example",
            "accessed_at": "2026-09-20T00:00:00+00:00",
        }
    ]


def test_dedupe_source_refs_preserves_distinct_titles_for_same_url():
    assert dedupe_source_refs(
        [
            {"url": "https://example.com/path", "title": "Example"},
            {"url": "https://example.com/path/", "title": "Example mirror"},
        ]
    ) == [
        {"url": "https://example.com/path", "title": "Example"},
        {"url": "https://example.com/path/", "title": "Example mirror"},
    ]
