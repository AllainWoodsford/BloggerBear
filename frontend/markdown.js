/* A small, safe Markdown parser for article bodies.

   Pure: text in, a tree of plain objects out. It never touches the DOM and never produces
   HTML, so nothing an article contains can become markup: the caller (app.js) builds
   elements from the tree with createElement/textContent, which keeps the site's strict
   CSP (script-src 'self', no inline anything) and makes injection impossible by construction.
   Raw HTML in the source is just text. Links are kept only if they are http(s).

   Supported: headings, paragraphs, **bold**, *italic*, `code`, [links](https://...), bullet and
   numbered lists (nested), block quotes, horizontal rules, fenced code, and pipe tables.
   That is what the drafts actually use; anything else falls through as plain text.

   Headings are shifted down by `options.headingOffset` (the page already has one <h1>, the
   article title, so a body "#" must not become a second one -- screen-reader users navigate
   by heading level).

   Loaded by index.html before app.js, and by the Node test (test_frontend_markdown.py). */
(function (root) {
  "use strict";

  var MAX_HEADING = 6;
  var ESCAPABLE = "\\`*_{}[]()#+-.!|>~";

  function isSafeUrl(url) {
    return /^https?:\/\/[^\s]+$/i.test(url);
  }

  // ---- inline ------------------------------------------------------------------------

  // The index of the closing `mark` for an emphasis that opened just before `from`, or -1.
  function findClose(src, from, mark) {
    for (var j = from; j < src.length; j++) {
      var c = src.charAt(j);
      if (c === "\\") {
        j++;
        continue;
      }
      if (c === "`") {
        var end = src.indexOf("`", j + 1);
        if (end !== -1) {
          j = end;
          continue;
        }
      }
      if (src.substr(j, mark.length) === mark && j > from && !/\s/.test(src.charAt(j - 1))) {
        if (mark.length === 1 && src.charAt(j + 1) === mark) {
          j++; // part of a ** run: not the close of a single
          continue;
        }
        if (mark.charAt(0) === "_" && /[A-Za-z0-9]/.test(src.charAt(j + mark.length))) {
          continue; // snake_case_word: not an emphasis close
        }
        return j;
      }
    }
    return -1;
  }

  // The index of the "]" matching the "[" at `open`, or -1.
  function findBracket(src, open) {
    var depth = 0;
    for (var j = open; j < src.length; j++) {
      var c = src.charAt(j);
      if (c === "\\") {
        j++;
      } else if (c === "[") {
        depth++;
      } else if (c === "]") {
        depth--;
        if (depth === 0) {
          return j;
        }
      }
    }
    return -1;
  }

  // The index of the ")" matching the "(" at `open`, or -1. Balanced, so a target like
  // https://en.wikipedia.org/wiki/Foo_(bar) or a hostile "javascript:alert(1)" is read whole.
  function findParen(src, open) {
    var depth = 0;
    for (var j = open; j < src.length; j++) {
      var c = src.charAt(j);
      if (c === "\\") {
        j++;
      } else if (c === "(") {
        depth++;
      } else if (c === ")") {
        depth--;
        if (depth === 0) {
          return j;
        }
      }
    }
    return -1;
  }

  function parseInline(src) {
    var nodes = [];
    var buf = "";
    var i = 0;

    function flush() {
      if (buf) {
        nodes.push({ type: "text", text: buf });
        buf = "";
      }
    }

    while (i < src.length) {
      var ch = src.charAt(i);

      if (ch === "\\" && i + 1 < src.length && ESCAPABLE.indexOf(src.charAt(i + 1)) !== -1) {
        buf += src.charAt(i + 1);
        i += 2;
        continue;
      }

      if (ch === "`") {
        var endTick = src.indexOf("`", i + 1);
        if (endTick > i) {
          flush();
          nodes.push({ type: "code", text: src.slice(i + 1, endTick) });
          i = endTick + 1;
          continue;
        }
      }

      if (ch === "*" || ch === "_") {
        var isDouble = src.charAt(i + 1) === ch;
        var mark = isDouble ? ch + ch : ch;
        var after = src.charAt(i + mark.length);
        var before = i > 0 ? src.charAt(i - 1) : "";
        var opensHere =
          after !== "" && !/\s/.test(after) && (ch === "*" || !/[A-Za-z0-9]/.test(before));
        if (opensHere) {
          var close = findClose(src, i + mark.length, mark);
          if (close !== -1) {
            flush();
            nodes.push({
              type: isDouble ? "strong" : "em",
              children: parseInline(src.slice(i + mark.length, close)),
            });
            i = close + mark.length;
            continue;
          }
        }
      }

      if (ch === "[") {
        var closeBracket = findBracket(src, i);
        if (closeBracket !== -1 && src.charAt(closeBracket + 1) === "(") {
          var closeParen = findParen(src, closeBracket + 1);
          if (closeParen !== -1) {
            var target = src.slice(closeBracket + 2, closeParen).trim().split(/\s+/)[0] || "";
            var kids = parseInline(src.slice(i + 1, closeBracket));
            flush();
            if (isSafeUrl(target)) {
              nodes.push({ type: "link", href: target, children: kids });
            } else {
              // Not a link we will follow (javascript:, data:, relative...): keep the words.
              kids.forEach(function (kid) {
                nodes.push(kid);
              });
            }
            i = closeParen + 1;
            continue;
          }
        }
      }

      buf += ch;
      i++;
    }
    flush();
    return nodes;
  }

  // ---- blocks -------------------------------------------------------------------------

  var LIST_RE = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  var HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
  var HR_RE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
  var FENCE_RE = /^\s*(```|~~~)/;

  function isBlank(line) {
    return /^\s*$/.test(line);
  }

  function indentOf(line) {
    return /^\s*/.exec(line)[0].length;
  }

  function splitRow(line) {
    var text = line.trim();
    if (text.charAt(0) === "|") {
      text = text.slice(1);
    }
    if (text.charAt(text.length - 1) === "|" && text.charAt(text.length - 2) !== "\\") {
      text = text.slice(0, -1);
    }
    var cells = [];
    var cell = "";
    for (var j = 0; j < text.length; j++) {
      var c = text.charAt(j);
      if (c === "\\" && text.charAt(j + 1) === "|") {
        cell += "|";
        j++;
      } else if (c === "|") {
        cells.push(cell.trim());
        cell = "";
      } else {
        cell += c;
      }
    }
    cells.push(cell.trim());
    return cells;
  }

  function isDelimiterRow(line) {
    if (line === undefined || line.indexOf("|") === -1 || line.indexOf("-") === -1) {
      return false;
    }
    return splitRow(line).every(function (cell) {
      return /^:?-+:?$/.test(cell);
    });
  }

  function alignOf(cell) {
    var left = cell.charAt(0) === ":";
    var right = cell.charAt(cell.length - 1) === ":";
    if (left && right) {
      return "center";
    }
    return right ? "right" : left ? "left" : null;
  }

  // Does `line` begin some block (so a paragraph must stop before it)?
  function startsBlock(line, next) {
    return (
      FENCE_RE.test(line) ||
      HEADING_RE.test(line) ||
      HR_RE.test(line) ||
      /^\s{0,3}>/.test(line) ||
      LIST_RE.test(line) ||
      (line.indexOf("|") !== -1 && isDelimiterRow(next))
    );
  }

  function dedent(lines) {
    var min = Infinity;
    lines.forEach(function (line) {
      if (!isBlank(line)) {
        min = Math.min(min, indentOf(line));
      }
    });
    if (min === Infinity || min === 0) {
      return lines;
    }
    return lines.map(function (line) {
      return line.slice(Math.min(min, indentOf(line)));
    });
  }

  function parseList(lines, start, offset) {
    var first = LIST_RE.exec(lines[start]);
    var baseIndent = first[1].length;
    var ordered = /\d/.test(first[2]);
    var startNumber = ordered ? parseInt(first[2], 10) : null;
    var items = [];
    var i = start;

    while (i < lines.length) {
      var line = lines[i];

      if (isBlank(line)) {
        var j = i;
        while (j < lines.length && isBlank(lines[j])) {
          j++;
        }
        var next = j < lines.length ? LIST_RE.exec(lines[j]) : null;
        var continues =
          j < lines.length &&
          (next
            ? next[1].length > baseIndent ||
              (next[1].length === baseIndent && /\d/.test(next[2]) === ordered)
            : indentOf(lines[j]) > baseIndent);
        if (!continues) {
          break;
        }
        i = j;
        continue;
      }

      var marker = LIST_RE.exec(line);
      if (marker && marker[1].length < baseIndent) {
        break; // belongs to an outer list
      }
      if (marker && marker[1].length === baseIndent) {
        if (/\d/.test(marker[2]) !== ordered) {
          break; // a bullet list turning into a numbered one, or the reverse: a new list
        }
        items.push([marker[3]]);
        i++;
        continue;
      }
      if (items.length === 0) {
        break;
      }
      if (marker || indentOf(line) > baseIndent) {
        items[items.length - 1].push(line); // nested list or an indented continuation
        i++;
        continue;
      }
      if (startsBlock(line, lines[i + 1])) {
        break;
      }
      items[items.length - 1].push(line); // a lazy continuation of the item's text
      i++;
    }

    var nodeItems = items.map(function (itemLines) {
      var head = [itemLines[0]];
      var rest = [];
      for (var k = 1; k < itemLines.length; k++) {
        if (rest.length === 0 && !LIST_RE.test(itemLines[k]) && !isBlank(itemLines[k])) {
          head.push(itemLines[k].trim());
        } else {
          rest.push(itemLines[k]);
        }
      }
      return {
        inline: parseInline(head.join(" ")),
        blocks: rest.length ? parseBlocks(dedent(rest), offset) : [],
      };
    });

    return {
      node: { type: "list", ordered: ordered, start: startNumber, items: nodeItems },
      next: i,
    };
  }

  function parseBlocks(lines, offset) {
    var blocks = [];
    var i = 0;

    while (i < lines.length) {
      var line = lines[i];

      if (isBlank(line)) {
        i++;
        continue;
      }

      var fence = FENCE_RE.exec(line);
      if (fence) {
        var code = [];
        i++;
        while (i < lines.length && lines[i].trim().indexOf(fence[1]) !== 0) {
          code.push(lines[i]);
          i++;
        }
        i++; // the closing fence
        blocks.push({ type: "code", text: code.join("\n") });
        continue;
      }

      var heading = HEADING_RE.exec(line);
      if (heading) {
        blocks.push({
          type: "heading",
          level: Math.min(MAX_HEADING, heading[1].length + offset),
          children: parseInline(heading[2]),
        });
        i++;
        continue;
      }

      if (HR_RE.test(line)) {
        blocks.push({ type: "hr" });
        i++;
        continue;
      }

      if (line.indexOf("|") !== -1 && isDelimiterRow(lines[i + 1])) {
        var head = splitRow(line);
        var aligns = splitRow(lines[i + 1]).map(alignOf);
        var rows = [];
        i += 2;
        while (i < lines.length && !isBlank(lines[i]) && lines[i].indexOf("|") !== -1) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        blocks.push({
          type: "table",
          aligns: head.map(function (_, index) {
            return aligns[index] || null;
          }),
          head: head.map(parseInline),
          rows: rows.map(function (row) {
            return head.map(function (_, index) {
              return parseInline(row[index] || "");
            });
          }),
        });
        continue;
      }

      if (/^\s{0,3}>/.test(line)) {
        var quoted = [];
        while (i < lines.length && /^\s{0,3}>/.test(lines[i])) {
          quoted.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
          i++;
        }
        blocks.push({ type: "blockquote", children: parseBlocks(quoted, offset) });
        continue;
      }

      if (LIST_RE.test(line)) {
        var parsed = parseList(lines, i, offset);
        blocks.push(parsed.node);
        i = parsed.next;
        continue;
      }

      var paragraph = [line.trim()];
      i++;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i], lines[i + 1])) {
        paragraph.push(lines[i].trim());
        i++;
      }
      blocks.push({ type: "paragraph", children: parseInline(paragraph.join(" ")) });
    }
    return blocks;
  }

  function parse(markdown, options) {
    var offset = options && typeof options.headingOffset === "number" ? options.headingOffset : 0;
    var lines = String(markdown === undefined || markdown === null ? "" : markdown)
      .replace(/\r\n?/g, "\n")
      .split("\n");
    return parseBlocks(lines, offset);
  }

  var api = { parse: parse, parseInline: parseInline, isSafeUrl: isSafeUrl };
  root.BloggerMarkdown = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
