/* Sending feedback with the token the server asked for (lambdas/common/feedback_verification.py).

   The feedback-status call hands out a one-use token, and it is only valid a moment after it was
   issued (0.5 to 2 seconds, random). When the site is busy it also needs a little proof of work:
   a number that makes SHA-256(token + ":" + number) start with `pow_bits` zero bits. None of it
   asks anything of the person (no clicking, nothing to read): the page waits the remaining
   moment, does the sum, and sends.

   submit() does the whole dance and recovers quietly:
     - too early    -> wait as long as the server said, send the same token again
     - expired, used, invalid, wrong article, no work -> fetch a fresh token and try once more
     - feedback has closed meanwhile -> resolves { closed: status } so the page can say why
   It resolves { response } (the final fetch Response) or { closed }.

   Loaded by index.html and by the static article pages before app.js / article-widgets.js, and by
   the Node test (test_frontend_verify.py). Everything it touches is injectable for that test. */
(function (root) {
  "use strict";

  var RETRYABLE = ["expired", "used", "invalid", "wrong_article", "missing", "work"];
  var YIELD_EVERY = 250; // hashes between letting the page breathe

  function leadingZeroBits(bytes) {
    var bits = 0;
    for (var i = 0; i < bytes.length; i++) {
      if (bytes[i] === 0) {
        bits += 8;
        continue;
      }
      var b = bytes[i];
      var n = 0;
      while ((b & 0x80) === 0) {
        n++;
        b = (b << 1) & 0xff;
      }
      return bits + n;
    }
    return bits;
  }

  function defaultSleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  // The smallest whole number n so SHA-256(token + ":" + n) has at least `bits` leading zero bits.
  function solveWork(token, bits, options) {
    options = options || {};
    var subtle = options.subtle || (root.crypto && root.crypto.subtle);
    var sleep = options.sleep || defaultSleep;
    if (!subtle) {
      return Promise.reject(new Error("no crypto available"));
    }
    var encoder = new TextEncoder();
    function attempt(n) {
      return subtle.digest("SHA-256", encoder.encode(token + ":" + n)).then(function (digest) {
        if (leadingZeroBits(new Uint8Array(digest)) >= bits) {
          return n;
        }
        if ((n + 1) % YIELD_EVERY === 0) {
          return sleep(0).then(function () {
            return attempt(n + 1);
          });
        }
        return attempt(n + 1);
      });
    }
    return attempt(0);
  }

  function parseJson(response) {
    return response.json().catch(function () {
      return {};
    });
  }

  function submit(config) {
    var fetchImpl = config.fetch || root.fetch.bind(root);
    var sleep = config.sleep || defaultSleep;
    var now = config.now || Date.now;
    var base = config.apiUrl + "/articles/" + encodeURIComponent(config.articleId);
    var onWorking = config.onWorking || function () {};

    // A token we already hold, with the moment it becomes valid.
    var held = config.verification
      ? { token: config.verification.token, bits: config.verification.pow_bits || 0, readyAt: config.verification.readyAt }
      : null;

    function fetchToken() {
      return fetchImpl(base + "/feedback-status")
        .then(function (response) {
          if (!response.ok) {
            throw new Error("status failed: " + response.status);
          }
          return response.json();
        })
        .then(function (status) {
          if (status.open === false) {
            return { closed: status };
          }
          var verification = status.verification;
          // Verification switched off on the server: send without a token.
          if (!verification) {
            return { held: { token: null, bits: 0, readyAt: 0 } };
          }
          return {
            held: {
              token: verification.token,
              bits: verification.pow_bits || 0,
              readyAt: now() + (verification.wait_ms || 0),
            },
          };
        });
    }

    function post(token, work) {
      var body = {};
      for (var key in config.payload) {
        if (Object.prototype.hasOwnProperty.call(config.payload, key)) {
          body[key] = config.payload[key];
        }
      }
      if (token) {
        body.token = token;
      }
      if (work !== null && work !== undefined) {
        body.work = work;
      }
      return fetchImpl(base + "/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    }

    function send(current, retriesLeft) {
      var wait = Math.max(0, (current.readyAt || 0) - now());
      return sleep(wait)
        .then(function () {
          if (current.token && current.bits > 0) {
            onWorking();
            return solveWork(current.token, current.bits, config);
          }
          return null;
        })
        .then(function (work) {
          return post(current.token, work);
        })
        .then(function (response) {
          if (response.status !== 403 || retriesLeft <= 0) {
            return { response: response };
          }
          return parseJson(response.clone ? response.clone() : response).then(function (body) {
            var verification = (body && body.verification) || {};
            if (verification.reason === "too_early") {
              // Nothing spent: wait as long as the server said and send the same token again.
              current.readyAt = now() + (verification.retry_after_ms || 0) + 50;
              return send(current, retriesLeft - 1);
            }
            if (RETRYABLE.indexOf(verification.reason) !== -1) {
              return fetchToken().then(function (result) {
                if (result.closed) {
                  return { closed: result.closed };
                }
                return send(result.held, retriesLeft - 1);
              });
            }
            return { response: response };
          });
        });
    }

    if (held) {
      return send(held, 2);
    }
    return fetchToken().then(function (result) {
      return result.closed ? { closed: result.closed } : send(result.held, 2);
    });
  }

  // Note when a token from the feedback-status call becomes valid, so submit() can wait exactly
  // as long as is left (a person who read the article has none left).
  function hold(verification, now) {
    if (!verification || !verification.token) {
      return null;
    }
    return {
      token: verification.token,
      pow_bits: verification.pow_bits || 0,
      readyAt: (now || Date.now)() + (verification.wait_ms || 0),
    };
  }

  var api = {
    submit: submit,
    hold: hold,
    solveWork: solveWork,
    leadingZeroBits: leadingZeroBits,
  };
  root.BloggerVerify = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
