/**
 * Interactive widgets for static article pages (docs/project-plan.md §11,
 * "Static article publishing"). A static article page (rendered server-side
 * by lambdas/common/static_pages.py and served directly from S3/CloudFront)
 * has no build-time knowledge of the API's own domain, and the CloudFront
 * distribution's CSP is script-src 'self' with no unsafe-inline (see
 * infra/modules/static-site/main.tf) -- so this must be an external file,
 * not an inline <script> block, and it reads window.PUBLIC_API_URL from
 * config.js exactly the way frontend/app.js does, rather than inventing a
 * second config mechanism.
 */

(function () {
  "use strict";

  function apiUrl(path) {
    return window.PUBLIC_API_URL + path;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var container = document.getElementById("content");
    if (!container) {
      return;
    }
    var articleId = container.getAttribute("data-article-id");
    if (!articleId) {
      return;
    }

    // Bump the view counter once per page load and reflect the server's
    // authoritative new count -- the number rendered server-side is only
    // accurate as of publish time.
    var viewCountEl = container.querySelector('[data-role="view-count"]');
    fetch(apiUrl("/articles/" + encodeURIComponent(articleId) + "/view"), { method: "POST" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("request failed: " + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        if (viewCountEl && typeof data.view_count === "number") {
          viewCountEl.textContent = data.view_count + " views";
        }
      })
      .catch(function () {
        // Silently ignore -- the server-rendered as-of-publish count is
        // already visible; a failed increment shouldn't disrupt reading.
      });

    var statusEl = container.querySelector('[data-role="feedback-status"]');
    var upButton = container.querySelector('[data-role="upvote"]');
    var downButton = container.querySelector('[data-role="downvote"]');

    function setDisabled(disabled) {
      if (upButton) upButton.disabled = disabled;
      if (downButton) downButton.disabled = disabled;
    }

    function submitVote(vote) {
      setDisabled(true);
      if (statusEl) statusEl.textContent = "Submitting...";

      fetch(apiUrl("/articles/" + encodeURIComponent(articleId) + "/feedback"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ vote: vote, comment: null }),
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("request failed: " + response.status);
          }
          if (statusEl) statusEl.textContent = "Thanks for your feedback!";
        })
        .catch(function () {
          if (statusEl) {
            statusEl.textContent = "Could not submit feedback right now. Please try again.";
          }
          setDisabled(false);
        });
    }

    if (upButton) {
      upButton.addEventListener("click", function () {
        submitVote("up");
      });
    }
    if (downButton) {
      downButton.addEventListener("click", function () {
        submitVote("down");
      });
    }
  });
})();
