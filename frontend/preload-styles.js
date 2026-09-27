/**
 * LEGACY SHIM -- nothing new references this file. Article pages published while PR #125's
 * preload+swap experiment was live are static HTML in S3 that mark their two stylesheet <link>s
 * `rel="preload" as="style" data-swap` and load this script to activate them; it has to stay
 * deployed or those pages render unstyled. index.html and common/static_pages.py's article
 * template are back to plain `rel="stylesheet"` links.
 *
 * The original version waited for each link's `load` event before flipping `rel`, which raced:
 * a preload that finished before this script ran (a cached stylesheet, or this file arriving
 * late) had already fired its `load`, so the listener never ran and the page stayed unstyled
 * ("preloaded but not used" in DevTools). Flipping immediately has no such window -- a
 * rel="stylesheet" link reuses the in-flight/finished preload rather than fetching again.
 *
 * Safe to delete once every page carrying `data-swap` has been re-rendered.
 */
(function () {
  var links = document.querySelectorAll('link[rel="preload"][as="style"][data-swap]');
  for (var i = 0; i < links.length; i++) {
    links[i].rel = "stylesheet";
  }
})();
