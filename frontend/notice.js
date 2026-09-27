/**
 * Sets the site notice's visibility as soon as its markup exists in the DOM -- before the
 * browser's first paint -- instead of waiting for app.js's whole script chain to finish and fire
 * DOMContentLoaded. That earlier toggle (unhide if not yet dismissed) was itself one of the two
 * layout shifts Lighthouse flagged on this page (~0.109 of the 0.384 total CLS): most first-time
 * visitors saw the notice pop in and push everything below it down, ~1-1.5s after first paint.
 *
 * A tiny external file, not an inline <script>, because this site's CSP has no 'unsafe-inline'
 * for script-src (infra/modules/static-site/main.tf) -- placed directly after #site-notice in
 * index.html so it runs the instant the element exists, well before the render-blocking
 * stylesheets would even allow a first paint.
 *
 * Only sets the initial visibility here; the dismiss button's own click handler is still wired
 * by app.js's initSiteNotice (DOMContentLoaded is soon enough for a user-triggered click).
 */
(function () {
  var notice = document.getElementById("site-notice");
  if (!notice) {
    return;
  }
  var dismissed = false;
  try {
    dismissed = window.localStorage.getItem("bloggerbear-site-notice-dismissed") === "1";
  } catch (err) {
    // Private browsing / storage disabled -- fall back to showing the notice, same as app.js's
    // own initSiteNotice would if it ran this check instead.
    dismissed = false;
  }
  notice.hidden = dismissed;
})();
