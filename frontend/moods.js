/* BloggerBear's moods, and the bear that goes with each (the Musings page).

   A musing carries a mood word (lambdas/common/musings.py: proud, thoughtful, pleased, reflective,
   curious, excited, shocked; a loot drop is always excited, a draft turned away at review always
   shocked). Each has a picture of the bear feeling it, frontend/bears/<mood>.svg, and a line of grey
   text under the musing: "BloggerBear was feeling <mood>". To use your own art, replace the SVG
   with the same name; the pictures are square and shown at about 56px.

   A mood with no picture of its own (a new one added on the backend before its art exists, or an
   older musing with no mood) gets bears/default.svg. A mood word that isn't plain letters is never
   shown as text. Pure: no DOM, so it is tested under Node (test_frontend_moods.py).

   A musing written by another character carries `character`. Today that is Professor Grizzlewick,
   the old reviewer bear (lambdas/common/reviewer.py): he has one picture whatever his mood,
   bears/grizzlewick.svg, and his own name in the grey line. BloggerBear's moods and pictures are
   untouched by him.

   Keep MOODS in step with lambdas/common/musings.py: a test fails if they drift. */
(function (root) {
  "use strict";

  var MOODS = ["proud", "thoughtful", "pleased", "reflective", "curious", "excited", "shocked"];
  var FALLBACK_IMAGE = "bears/default.svg";
  var PLAIN_WORD = /^[A-Za-z][A-Za-z -]{0,29}$/;
  var DEFAULT_NAME = "BloggerBear";
  var CHARACTERS = {
    grizzlewick: { name: "Professor Grizzlewick", image: "bears/grizzlewick.svg" },
  };

  function describe(mood, character) {
    var text = typeof mood === "string" ? mood.trim().toLowerCase() : "";
    var known = MOODS.indexOf(text) !== -1;
    var who = Object.prototype.hasOwnProperty.call(CHARACTERS, character) ? CHARACTERS[character] : null;
    return {
      // The word to show after "<name> was feeling", or null if there is nothing to say.
      label: PLAIN_WORD.test(text) ? text : null,
      image: who ? who.image : known ? "bears/" + text + ".svg" : FALLBACK_IMAGE,
      known: who ? true : known,
      name: who ? who.name : DEFAULT_NAME,
    };
  }

  var api = {
    MOODS: MOODS,
    CHARACTERS: CHARACTERS,
    FALLBACK_IMAGE: FALLBACK_IMAGE,
    describe: describe,
  };
  root.BloggerMoods = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
