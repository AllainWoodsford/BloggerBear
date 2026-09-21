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

  // The same "Hold your Paws!" panel the SPA shows (frontend/app.js) when BloggerBear isn't
  // taking feedback: the form is replaced by the reason. The API decides; this only shows it.
  function formatRetry(isoString) {
    var then = new Date(isoString).getTime();
    if (!isoString || isNaN(then)) {
      return "";
    }
    var minutes = Math.ceil((then - Date.now()) / 60000);
    if (minutes <= 1) {
      return "Try again in a moment.";
    }
    if (minutes < 60) {
      return "Try again in about " + minutes + " minutes.";
    }
    var hours = Math.round(minutes / 60);
    return "Try again in about " + hours + (hours === 1 ? " hour." : " hours.");
  }

  function paragraph(text, className) {
    var p = document.createElement("p");
    if (className) {
      p.className = className;
    }
    p.textContent = text;
    return p;
  }

  function closedPanel(status) {
    var box = document.createElement("div");
    box.className = "feedback-closed";
    box.setAttribute("role", "status");
    var headline = paragraph("Hold your Paws! ", "feedback-closed-headline");
    var paw = document.createElement("span");
    paw.setAttribute("aria-hidden", "true");
    paw.textContent = "\uD83D\uDC3E";
    headline.appendChild(paw);
    box.appendChild(headline);
    box.appendChild(paragraph("BloggerBear is not taking feedback right now."));
    box.appendChild(
      paragraph(
        "Reason: " + ((status && status.label) || "Feedback is unavailable right now"),
        "feedback-closed-reason"
      )
    );
    var retry = status ? formatRetry(status.retry_at) : "";
    if (retry) {
      box.appendChild(paragraph(retry, "feedback-closed-retry"));
    }
    return box;
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

    var buttonsEl = container.querySelector(".feedback-buttons");
    var feedbackSection = container.querySelector('[data-role="feedback"]');

    function setDisabled(disabled) {
      if (upButton) upButton.disabled = disabled;
      if (downButton) downButton.disabled = disabled;
    }

    // Replace the buttons and status with the reason feedback is closed.
    function showClosed(status) {
      if (!feedbackSection) return;
      if (buttonsEl) buttonsEl.remove();
      if (statusEl) statusEl.remove();
      feedbackSection.appendChild(closedPanel(status));
    }

    // The buttons start hidden (see common/static_pages.py) so a closed article never flashes
    // them; show them once the API says feedback is open, or if it can't be asked.
    function showButtons() {
      if (buttonsEl) buttonsEl.hidden = false;
    }

    fetch(apiUrl("/articles/" + encodeURIComponent(articleId) + "/feedback-status"))
      .then(function (response) {
        if (!response.ok) {
          throw new Error("request failed: " + response.status);
        }
        return response.json();
      })
      .then(function (status) {
        if (status && status.open === false) {
          showClosed(status);
        } else {
          showButtons();
        }
      })
      .catch(function () {
        showButtons();
      });

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
            // 423 / 429 / 503 come with the reason: show it rather than inviting a retry.
            return response
              .json()
              .catch(function () {
                return {};
              })
              .then(function (body) {
                if (body && body.feedback && body.feedback.open === false) {
                  showClosed(body.feedback);
                  return;
                }
                throw new Error("request failed: " + response.status);
              });
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
