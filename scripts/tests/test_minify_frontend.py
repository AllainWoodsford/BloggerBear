"""scripts/minify_frontend.py: mirrors a source directory with .js/.css minified, everything
else copied as-is."""

from __future__ import annotations

from pathlib import Path

import pytest

from minify_frontend import minify_frontend


@pytest.fixture
def source_dir(tmp_path):
    src = tmp_path / "frontend"
    src.mkdir()
    (src / "app.js").write_text(
        "// a comment\nfunction greet() {\n  return 'hi';   \n}\n", encoding="utf-8"
    )
    (src / "styles.css").write_text(
        "/* a comment */\nbody {\n  color: red;   \n}\n", encoding="utf-8"
    )
    (src / "index.html").write_text("<html><!-- untouched --></html>", encoding="utf-8")
    (src / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")
    sub = src / "bears"
    sub.mkdir()
    (sub / "logo.svg").write_bytes(b"<svg></svg>")
    return src


def test_js_and_css_are_minified(source_dir, tmp_path):
    dest = tmp_path / "dist"
    results = minify_frontend(source_dir, dest)

    js_after = (dest / "app.js").read_text(encoding="utf-8")
    css_after = (dest / "styles.css").read_text(encoding="utf-8")
    assert "// a comment" not in js_after and "greet" in js_after
    assert "/* a comment */" not in css_after and "color:red" in css_after.replace(" ", "")
    # Always forward slashes, regardless of host OS -- see minify_frontend's own comment on why.
    assert {name for name, before, after in results} == {
        "app.js",
        "styles.css",
        "index.html",
        "robots.txt",
        "bears/logo.svg",
    }


def test_everything_else_is_copied_byte_for_byte(source_dir, tmp_path):
    dest = tmp_path / "dist"
    minify_frontend(source_dir, dest)

    assert (dest / "index.html").read_bytes() == (source_dir / "index.html").read_bytes()
    assert (dest / "robots.txt").read_bytes() == (source_dir / "robots.txt").read_bytes()
    assert (dest / "bears" / "logo.svg").read_bytes() == (source_dir / "bears" / "logo.svg").read_bytes()


def test_minified_files_are_smaller_or_equal_never_larger(source_dir, tmp_path):
    dest = tmp_path / "dist"
    results = minify_frontend(source_dir, dest)

    for name, before, after in results:
        assert after <= before, name


def test_a_stale_file_removed_from_the_source_does_not_linger_in_the_dest(source_dir, tmp_path):
    dest = tmp_path / "dist"
    minify_frontend(source_dir, dest)
    (dest / "leftover.js").write_text("stale", encoding="utf-8")

    minify_frontend(source_dir, dest)

    assert not (dest / "leftover.js").exists()


def test_a_missing_source_directory_raises(tmp_path):
    with pytest.raises(SystemExit):
        minify_frontend(tmp_path / "does-not-exist", tmp_path / "dist")


def test_minify_frontend_is_importable_as_a_module_for_tests():
    # Guards against the script losing its `if __name__ == "__main__":` guard and running its
    # CLI (which would try to read the real frontend/ directory) just from being imported here.
    import minify_frontend as module

    assert callable(module.minify_frontend)


def test_the_real_frontend_directory_minifies_without_error(tmp_path):
    """Not a golden-master test of exact output -- just that the real, current frontend/ survives
    a real run without the empty-output guard or a minifier itself raising. Destination is a
    tmp_path, never the real repo's frontend-dist/ -- this must not depend on, or leave behind,
    anything outside pytest's own sandbox."""
    root = Path(__file__).resolve().parents[2]
    source = root / "frontend"
    if not source.is_dir():
        pytest.skip("frontend/ not present in this checkout")

    results = minify_frontend(source, tmp_path / "frontend-dist")

    assert len(results) > 0
    assert all(after > 0 for _, before, after in results if before > 0)
