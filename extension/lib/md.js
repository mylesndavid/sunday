/* Tiny safe markdown renderer for Sunday's replies in the side panel.
 *
 * ALL input is HTML-escaped FIRST, then known-safe tags are built back up —
 * so page content echoed in a reply can never become live markup (no
 * innerHTML injection surface). Covers the chat subset: fenced code, inline
 * code, bold/italic/strikethrough, links (http/https only, new tab),
 * headings, lists, blockquotes, hr. Anything else stays literal text.
 *
 * Code spans/fences are stashed behind \u0000/\u0001 sentinel placeholders
 * while the rest is styled — both sentinels are stripped from the input up
 * front, so text can never forge a placeholder and pull foreign code out.
 *
 * Deliberately ~100 lines instead of a vendored parser: MV3 forbids remote
 * scripts and the extension stays no-sprawl.
 */
(() => {
  const esc = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  // Inline transforms on already-escaped text. Inline code is extracted to
  // placeholders first so its contents are never styled.
  function inline(s, codes) {
    s = s.replace(/`([^`\n]+)`/g, (_, c) => {
      codes.push(c);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>");
    s = s.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    s = s.replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
    );
    s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
    return s;
  }

  window.mdToHtml = function mdToHtml(raw) {
    // Strip our sentinel chars from input so placeholders can't be forged.
    const text = esc(String(raw || ""))
      .replace(/[\u0000\u0001]/g, "")
      .replace(/\r\n/g, "\n");

    // Pull fenced code blocks out before line parsing.
    const fences = [];
    const body = text.replace(/```[^\n`]*\n([\s\S]*?)```/g, (_, code) => {
      fences.push(code.replace(/\n$/, ""));
      return "\u0001" + (fences.length - 1) + "\u0001";
    });

    const codes = [];
    const out = [];
    let list = null; // "ul" | "ol" | null
    let para = [];

    const closeList = () => {
      if (list) { out.push(`</${list}>`); list = null; }
    };
    const flushPara = () => {
      if (para.length) { out.push(`<p>${para.join("<br>")}</p>`); para = []; }
    };

    for (const line of body.split("\n")) {
      const fence = line.match(/^\u0001(\d+)\u0001\s*$/);
      if (fence) { flushPara(); closeList(); out.push(`<pre><code>${fences[+fence[1]]}</code></pre>`); continue; }
      if (/^\s*$/.test(line)) { flushPara(); closeList(); continue; }
      const h = line.match(/^(#{1,3})\s+(.*)$/);
      if (h) { flushPara(); closeList(); const lvl = h[1].length + 2; out.push(`<h${lvl}>${inline(h[2], codes)}</h${lvl}>`); continue; }
      if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { flushPara(); closeList(); out.push("<hr>"); continue; }
      const q = line.match(/^&gt;\s?(.*)$/);
      if (q) { flushPara(); closeList(); out.push(`<blockquote>${inline(q[1], codes)}</blockquote>`); continue; }
      const ul = line.match(/^\s*[-*]\s+(.*)$/);
      if (ul) {
        flushPara();
        if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
        out.push(`<li>${inline(ul[1], codes)}</li>`);
        continue;
      }
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ol) {
        flushPara();
        if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
        out.push(`<li>${inline(ol[1], codes)}</li>`);
        continue;
      }
      closeList();
      para.push(inline(line, codes));
    }
    flushPara();
    closeList();
    return out.join("");
  };
})();
