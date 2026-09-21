/* Scratch BloggerBear's tummy: a small toy for the moments around feedback.

   It is decoration, never a gate. It appears under the "Thanks for your feedback!" message now and
   then (see shouldOffer), and always on the "Hold your Paws!" panel while feedback is closed. Nothing
   about it is sent anywhere or stored, and nothing about how you play is measured.

   Playing: press the bear (Enter, Space or a tap all work) or rub it with a mouse. A few scratches and
   the bear purrs and swaps to its happy picture. With reduced motion set there is no wiggle, just the
   swap and the words. The words are in a polite live region that only changes at a few points, so a
   screen reader is told "Hehe, that tickles", then "purrs", not once per click.

   The pictures are frontend/bears/tummy.svg and tummy-happy.svg: replace them with your own art
   (keep the names). The rules (game(), shouldOffer()) are pure and tested under Node
   (test_frontend_tummy.py); mount() builds the card with plain DOM calls. */
(function (root) {
  "use strict";

  var IDLE_IMAGE = "bears/tummy.svg";
  var HAPPY_IMAGE = "bears/tummy-happy.svg";
  var PURR_AT = 5; // scratches until the bear purrs
  var RUB_PIXELS_PER_SCRATCH = 40; // mouse movement that counts as one scratch
  var MAX_RUB_STEP = 80; // ignore a single jump bigger than this (a fast flick, a glitch)
  var OFFER_CHANCE = 1 / 3; // how often the thank-you offers the toy

  // What the bear says, by stage. Stage 0 is the invitation; the live region only changes when the
  // stage does.
  var WORDS = [
    "Scratch BloggerBear's tummy",
    "Hehe, that tickles.",
    "Mmm, a little more...",
    "BloggerBear purrs. Thank you!",
    "Purrrr... okay, okay, that's plenty of tummy for one day.",
  ];

  function stageOf(count) {
    if (count >= 12) {
      return 4;
    }
    if (count >= PURR_AT) {
      return 3;
    }
    if (count >= 3) {
      return 2;
    }
    return count >= 1 ? 1 : 0;
  }

  // The rules of the toy, with no DOM. scratch(n) adds n scratches; rub(distance) turns mouse
  // movement into scratches.
  function game() {
    var count = 0;
    var carried = 0;

    function snapshot() {
      var stage = stageOf(count);
      return {
        count: count,
        stage: stage,
        purring: count >= PURR_AT,
        image: count >= PURR_AT ? HAPPY_IMAGE : IDLE_IMAGE,
        words: WORDS[stage],
      };
    }

    return {
      scratch: function (times) {
        var n = typeof times === "number" && isFinite(times) && times > 0 ? Math.floor(times) : 1;
        count += n;
        return snapshot();
      },
      rub: function (distance) {
        if (typeof distance !== "number" || !isFinite(distance) || distance <= 0) {
          return snapshot();
        }
        carried += Math.min(distance, MAX_RUB_STEP);
        var scratches = Math.floor(carried / RUB_PIXELS_PER_SCRATCH);
        carried -= scratches * RUB_PIXELS_PER_SCRATCH;
        if (scratches > 0) {
          count += scratches;
        }
        return snapshot();
      },
      state: snapshot,
    };
  }

  // Should the thank-you offer the toy this time? `roll` is a number from 0 up to (not including) 1.
  function shouldOffer(roll, chance) {
    var odds = typeof chance === "number" ? chance : OFFER_CHANCE;
    return typeof roll === "number" && isFinite(roll) && roll >= 0 && roll < odds;
  }

  // Build the card inside `parent` and return it. Nothing here is stored or sent.
  function mount(parent) {
    var doc = parent.ownerDocument;
    var play = game();
    var card = doc.createElement("div");
    card.className = "tummy-game";

    var button = doc.createElement("button");
    button.type = "button";
    button.className = "tummy-button";
    button.setAttribute("aria-label", "Scratch BloggerBear's tummy");

    var bear = doc.createElement("img");
    bear.className = "tummy-bear";
    bear.setAttribute("src", IDLE_IMAGE);
    bear.setAttribute("alt", ""); // decorative: the button has its name, the words say the rest
    bear.setAttribute("width", "112");
    bear.setAttribute("height", "112");
    bear.setAttribute("draggable", "false");
    button.appendChild(bear);
    card.appendChild(button);

    // Only this one changes while you play, and only at a few points, so a screen reader is not
    // chatty. It starts as the invitation (not announced: nothing has happened yet).
    var note = doc.createElement("p");
    note.className = "tummy-note";
    note.setAttribute("role", "status");
    note.setAttribute("aria-live", "polite");
    note.textContent = WORDS[0];
    card.appendChild(note);

    var lastStage = 0;
    var wiggleTimer = null;

    function show(state) {
      if (bear.getAttribute("src") !== state.image) {
        bear.setAttribute("src", state.image);
      }
      if (state.stage !== lastStage) {
        lastStage = state.stage;
        note.textContent = state.words;
      }
      // A quick wobble. The stylesheet only animates it when the reader has not asked for reduced
      // motion; the class is harmless otherwise.
      card.classList.remove("tummy-wiggle");
      void card.offsetWidth; // restart the animation
      card.classList.add("tummy-wiggle");
      if (wiggleTimer) {
        clearTimeout(wiggleTimer);
      }
      wiggleTimer = setTimeout(function () {
        card.classList.remove("tummy-wiggle");
      }, 400);
    }

    // Enter, Space and a tap all arrive as a click.
    button.addEventListener("click", function () {
      show(play.scratch(1));
    });

    // Rubbing with a mouse (or a pen): movement over the bear while the button is held down. A
    // finger just taps: dragging would fight the page's own scrolling.
    var last = null;
    button.addEventListener("pointerdown", function (event) {
      last = event.pointerType === "touch" ? null : { x: event.clientX, y: event.clientY };
    });
    button.addEventListener("pointermove", function (event) {
      if (!last || event.buttons !== 1) {
        return;
      }
      var dx = event.clientX - last.x;
      var dy = event.clientY - last.y;
      last = { x: event.clientX, y: event.clientY };
      var before = play.state().count;
      var state = play.rub(Math.sqrt(dx * dx + dy * dy));
      if (state.count !== before) {
        show(state);
      }
    });
    button.addEventListener("pointerup", function () {
      last = null;
    });
    button.addEventListener("pointerleave", function () {
      last = null;
    });

    parent.appendChild(card);
    return card;
  }

  var api = {
    IDLE_IMAGE: IDLE_IMAGE,
    HAPPY_IMAGE: HAPPY_IMAGE,
    PURR_AT: PURR_AT,
    RUB_PIXELS_PER_SCRATCH: RUB_PIXELS_PER_SCRATCH,
    OFFER_CHANCE: OFFER_CHANCE,
    WORDS: WORDS,
    game: game,
    shouldOffer: shouldOffer,
    mount: mount,
  };
  root.BloggerTummy = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
