/**
 * Flips a preloaded stylesheet <link> to an active one once it has finished loading, so the two
 * site stylesheets (normalize.css, styles.css) never block the initial render (Lighthouse's
 * "render-blocking requests" flag -- est. 440ms). Used by index.html and
 * common/static_pages.py's article page template, both of which mark the two stylesheet <link>s
 * `rel="preload" as="style" data-swap` instead of `rel="stylesheet"` and include a <noscript>
 * fallback for when JS is disabled.
 *
 * A tiny external file, not an inline onload="..." handler, because this site's CSP has no
 * 'unsafe-inline' for script-src (infra/modules/static-site/main.tf) -- an inline handler would
 * just be silently blocked. NOT used by about.html/error.html: both are deliberately built to
 * render correctly with zero JS dependency (see error.html's own comment), and this script
 * failing to load would leave their CSS preloaded but never activated -- worse than the plain
 * blocking <link> they keep instead.
 */
(function () {
  var links = document.querySelectorAll('link[rel="preload"][as="style"][data-swap]');
  for (var i = 0; i < links.length; i++) {
    links[i].addEventListener("load", function () {
      this.rel = "stylesheet";
    });
  }
})();
