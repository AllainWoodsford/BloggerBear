/* BloggerBear's gear, as the Stats page shows it: what each piece is called, how rare it is, how worn it
   is, and the little picture that goes on its slot.

   The backend (lambdas/common/gear.py, public_view; GET /equipment) sends each worn piece as
   {name, rarity, slot, description, topic_name, durability_percent, ...}. This turns that into
   something safe to draw: an unknown rarity becomes "common", a percentage is clamped, text is
   trimmed, and nothing is trusted to be present. Pure: no DOM, so it is tested under Node
   (test_frontend_gear.py). app.js does the drawing.

   Pictures are described as plain data ({tag, attrs}), never markup, so nothing here can carry a
   style attribute, a script or a link (the site's CSP forbids inline styles). Every colour comes from
   styles.css classes (rarity-*, dur-*), so light and dark mode are inherited.

   Rarity colours follow the usual game convention: grey common, green uncommon, blue rare, purple
   epic, orange legendary. Colour is never the only signal: the rarity and the condition are always
   written out too.

   Keep RARITIES and the slot names in step with lambdas/common/gear.py and equipment.py: a test fails
   if they drift. */
(function (root) {
  "use strict";

  var RARITIES = ["common", "uncommon", "rare", "epic", "legendary"];
  var RARITY_LABELS = {
    common: "Common",
    uncommon: "Uncommon",
    rare: "Rare",
    epic: "Epic",
    legendary: "Legendary",
  };
  var ARMOR_SLOTS = ["helmet", "chest", "gloves", "boots", "sword", "shield"];
  var SLOT_LABELS = {
    helmet: "Helmet",
    chest: "Chest",
    gloves: "Gloves",
    boots: "Boots",
    sword: "Sword",
    shield: "Shield",
    ring: "Ring",
  };
  var MAX_NAME = 80;
  var MAX_DESCRIPTION = 400;

  // How worn a piece is, from its durability as a percentage of its maximum. Lower is worse, and the
  // page colours it green, yellow, orange and red to match.
  var TIERS = [
    { min: 75, key: "good", label: "Good condition" },
    { min: 50, key: "fair", label: "Showing wear" },
    { min: 25, key: "worn", label: "Badly worn" },
    { min: 0, key: "critical", label: "About to break" },
  ];

  function clean(value, limit) {
    var text = typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
    return text.length > limit ? text.slice(0, limit - 1).trimEnd() + "…" : text;
  }

  function rarityOf(value) {
    var text = typeof value === "string" ? value.trim().toLowerCase() : "";
    return RARITIES.indexOf(text) !== -1 ? text : "common";
  }

  // Durability as {percent, tier, label, text}; percent is null when the piece has none to show.
  function durability(percent) {
    var number = typeof percent === "number" && isFinite(percent) ? Math.round(percent) : null;
    if (number === null) {
      return { percent: null, tier: "unknown", label: "Condition unknown", text: "n/a" };
    }
    number = Math.max(0, Math.min(100, number));
    var tier = TIERS.filter(function (candidate) {
      return number >= candidate.min;
    })[0];
    return { percent: number, tier: tier.key, label: tier.label, text: number + "%" };
  }

  // One worn piece, ready to draw. `slot` is where it is (the API's own key for it) when the piece
  // itself does not say.
  function describe(item, slot) {
    item = item && typeof item === "object" ? item : {};
    var slotKey = typeof item.slot === "string" && SLOT_LABELS[item.slot] ? item.slot : slot;
    var rarity = rarityOf(item.rarity);
    var condition = durability(item.durability_percent);
    var name = clean(item.name, MAX_NAME) || "Unnamed gear";
    var topic = clean(item.topic_name, MAX_NAME) || null;
    return {
      name: name,
      rarity: rarity,
      rarityLabel: RARITY_LABELS[rarity],
      slot: slotKey,
      slotLabel: SLOT_LABELS[slotKey] || "Gear",
      description: clean(item.description, MAX_DESCRIPTION) || "No description.",
      topic: topic,
      scope: topic ? "Topic: " + topic : "Every topic",
      durability: condition,
      // What a screen reader hears for the slot's button.
      accessibleName:
        (SLOT_LABELS[slotKey] || "Gear") + ": " + name + ", " + RARITY_LABELS[rarity] + ", " +
        (condition.percent === null ? "condition unknown" : "durability " + condition.text),
    };
  }

  // "3 items in the backpack", "1 item", "the backpack is empty". A count that is not a whole
  // number of at least zero is treated as none.
  function backpackText(count) {
    var number = typeof count === "number" && isFinite(count) && count > 0 ? Math.floor(count) : 0;
    if (number === 0) {
      return "The backpack is empty";
    }
    return number + (number === 1 ? " item" : " items") + " in the backpack";
  }

  // --- the pictures -------------------------------------------------------------------------
  //
  // 48 x 48. Each slot has a plain silhouette; rarity adds to it (nothing for common, then a gem, a
  // ring around it, sparkles, and for legendary a burst of rays and a double ring) so the better the
  // gear the more it shines, whatever the colour looks like to the person.

  var BASE = {
    helmet: [
      { tag: "path", attrs: { class: "gear-fill", d: "M10 30 C10 16 17 9 24 9 C31 9 38 16 38 30 L38 36 L30 36 L30 30 L18 30 L18 36 L10 36 Z" } },
      { tag: "rect", attrs: { class: "gear-cut", x: 17, y: 22, width: 14, height: 3 } },
      { tag: "path", attrs: { class: "gear-line", d: "M24 9 L24 4" } },
    ],
    chest: [
      { tag: "path", attrs: { class: "gear-fill", d: "M15 9 L21 7 L27 7 L33 9 L41 15 L37 22 L34 20 L34 41 L14 41 L14 20 L11 22 L7 15 Z" } },
      { tag: "path", attrs: { class: "gear-line", d: "M24 12 L24 41 M17 24 H31" } },
    ],
    gloves: [
      { tag: "rect", attrs: { class: "gear-fill", x: 13, y: 22, width: 22, height: 14, rx: 3 } },
      { tag: "rect", attrs: { class: "gear-fill", x: 13, y: 8, width: 5, height: 16, rx: 2 } },
      { tag: "rect", attrs: { class: "gear-fill", x: 19, y: 6, width: 5, height: 18, rx: 2 } },
      { tag: "rect", attrs: { class: "gear-fill", x: 25, y: 8, width: 5, height: 16, rx: 2 } },
      { tag: "rect", attrs: { class: "gear-fill", x: 31, y: 12, width: 4, height: 12, rx: 2 } },
      { tag: "rect", attrs: { class: "gear-cut", x: 11, y: 36, width: 26, height: 6, rx: 2 } },
    ],
    boots: [
      { tag: "path", attrs: { class: "gear-fill", d: "M14 7 H27 V26 L38 30 C41 31 41 38 37 38 H14 Z" } },
      { tag: "rect", attrs: { class: "gear-cut", x: 12, y: 38, width: 28, height: 4, rx: 2 } },
      { tag: "path", attrs: { class: "gear-line", d: "M14 14 H27" } },
    ],
    sword: [
      { tag: "path", attrs: { class: "gear-fill", d: "M24 3 L28 9 V30 H20 V9 Z" } },
      { tag: "rect", attrs: { class: "gear-fill", x: 11, y: 30, width: 26, height: 4, rx: 1 } },
      { tag: "rect", attrs: { class: "gear-cut", x: 22, y: 34, width: 4, height: 8, rx: 1 } },
      { tag: "circle", attrs: { class: "gear-fill", cx: 24, cy: 44, r: 2.5 } },
    ],
    shield: [
      { tag: "path", attrs: { class: "gear-fill", d: "M24 5 L38 10 V24 C38 33 31 39 24 43 C17 39 10 33 10 24 V10 Z" } },
      { tag: "path", attrs: { class: "gear-line", d: "M24 10 V38 M14 20 H34" } },
    ],
    ring: [
      { tag: "circle", attrs: { class: "gear-band", cx: 24, cy: 28, r: 11 } },
      { tag: "path", attrs: { class: "gear-fill", d: "M24 6 L30 13 L24 20 L18 13 Z" } },
    ],
  };

  // Corner sparkles: four little diamonds.
  var SPARKLES = [
    { tag: "path", attrs: { class: "gear-spark", d: "M6 4 L7.5 7 L6 10 L4.5 7 Z" } },
    { tag: "path", attrs: { class: "gear-spark", d: "M42 4 L43.5 7 L42 10 L40.5 7 Z" } },
    { tag: "path", attrs: { class: "gear-spark", d: "M6 38 L7.5 41 L6 44 L4.5 41 Z" } },
    { tag: "path", attrs: { class: "gear-spark", d: "M42 38 L43.5 41 L42 44 L40.5 41 Z" } },
  ];

  // Eight rays behind a legendary piece.
  var RAYS = [
    "M24 0 V5", "M24 43 V48", "M0 24 H5", "M43 24 H48",
    "M7 7 L10 10", "M41 7 L38 10", "M7 41 L10 38", "M41 41 L38 38",
  ].map(function (d) {
    return { tag: "path", attrs: { class: "gear-ray", d: d } };
  });

  function decorations(rarity) {
    var extra = [];
    if (rarity === "legendary") {
      extra = extra.concat(RAYS);
    }
    if (rarity === "rare" || rarity === "epic" || rarity === "legendary") {
      extra.push({ tag: "circle", attrs: { class: "gear-halo", cx: 24, cy: 24, r: 22.5 } });
    }
    if (rarity === "legendary") {
      extra.push({ tag: "circle", attrs: { class: "gear-halo", cx: 24, cy: 24, r: 19.5 } });
    }
    if (rarity === "epic" || rarity === "legendary") {
      extra = extra.concat(SPARKLES);
    }
    if (rarity === "uncommon" || rarity === "rare") {
      extra.push({ tag: "circle", attrs: { class: "gear-spark", cx: 40, cy: 8, r: 3 } });
    }
    return extra;
  }

  // The picture for a slot: {viewBox, nodes: [{tag, attrs}]}. Pass a null rarity for an empty slot: just
  // the silhouette, which the page draws dashed.
  function iconSpec(slot, rarity) {
    var base = BASE[slot] || BASE.ring;
    var nodes = rarity ? decorations(rarityOf(rarity)).concat(base) : base;
    return { viewBox: "0 0 48 48", nodes: nodes };
  }

  // The bag beside the count: a plain sack.
  var BAG = {
    viewBox: "0 0 24 24",
    nodes: [
      { tag: "path", attrs: { class: "gear-fill", d: "M8 7 C8 4 16 4 16 7 L18 9 C21 12 21 20 17 21 H7 C3 20 3 12 6 9 Z" } },
      { tag: "path", attrs: { class: "gear-line", d: "M8 7 H16" } },
    ],
  };

  var api = {
    RARITIES: RARITIES,
    RARITY_LABELS: RARITY_LABELS,
    ARMOR_SLOTS: ARMOR_SLOTS,
    SLOT_LABELS: SLOT_LABELS,
    TIERS: TIERS,
    BAG: BAG,
    rarityOf: rarityOf,
    durability: durability,
    describe: describe,
    backpackText: backpackText,
    iconSpec: iconSpec,
  };
  root.BloggerGear = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
