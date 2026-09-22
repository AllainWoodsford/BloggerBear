/* BloggerBear's moods, and the bear that goes with each (the Musings page).

   A musing carries a mood word (lambdas/common/musings.py: proud, thoughtful, pleased, reflective,
   curious, excited; a loot drop is always excited). Each has a picture of the bear feeling it, frontend/bears/<mood>.svg, and a line of grey
   text under the musing: "BloggerBear was feeling <mood>". To use your own art, replace the SVG
   with the same name; the pictures are square and shown at about 56px.

   A mood with no picture of its own (a new one added on the backend before its art exists, or an
   older musing with no mood) gets bears/default.svg. A mood word that isn't plain letters is never
   shown as text. Pure: no DOM, so it is tested under Node (test_frontend_moods.py).

   Keep MOODS in step with lambdas/common/musings.py: a test fails if they drift. */
(function (root) {
  "use strict";

  var MOODS = ["proud", "thoughtful", "pleased", "reflective", "curious", "excited"];
  var FALLBACK_IMAGE = "bears/default.svg";
  var PLAIN_WORD = /^[A-Za-z][A-Za-z -]{0,29}$/;

  function describe(mood) {
    var text = typeof mood === "string" ? mood.trim().toLowerCase() : "";
    var known = MOODS.indexOf(text) !== -1;
    return {
      // The word to show after "BloggerBear was feeling", or null if there is nothing to say.
      label: PLAIN_WORD.test(text) ? text : null,
      image: known ? "bears/" + text + ".svg" : FALLBACK_IMAGE,
      known: known,
    };
  }

  var api = { MOODS: MOODS, FALLBACK_IMAGE: FALLBACK_IMAGE, describe: describe };
  root.BloggerMoods = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
