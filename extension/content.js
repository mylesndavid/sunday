/* Sunday Cockpit — content script.
 * Sunday's eyes and hands on the page:
 *  - indexes interactive elements and returns a compact perception snapshot
 *  - highlights elements, draws callouts, fills fields, clicks, scrolls
 *  - hands control to the user for sensitive steps (instruct_user) and
 *    reports their confirmation back to the bridge
 * Runs in an isolated world but shares the DOM. Module-level state persists
 * until the page is reloaded/navigated.
 */
(() => {
  if (window.__copilotAgentInjected) return;
  window.__copilotAgentInjected = true;

  /** index -> element map for the current snapshot */
  let elementMap = [];
  /** overlay container element */
  let overlayRoot = null;
  /** records of active overlays so we can reposition on scroll/resize */
  let activeOverlays = [];
  let repositionScheduled = false;

  const MAX_TEXT = 7000;
  const MAX_LABEL = 120;

  // Inline line icons (Lucide, ISC) — content scripts can't import modules.
  const CPI = {
    hand: '<path d="M18 11V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2"/><path d="M14 10V4a2 2 0 0 0-2-2a2 2 0 0 0-2 2v2"/><path d="M10 10.5V6a2 2 0 0 0-2-2a2 2 0 0 0-2 2v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    bulb: '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
  };
  function svgIcon(name, size) {
    const p = CPI[name];
    return p ? `<svg xmlns="http://www.w3.org/2000/svg" width="${size || 14}" height="${size || 14}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:block">${p}</svg>` : "";
  }

  // ---------------------------------------------------------------------------
  // Visibility + perception
  // ---------------------------------------------------------------------------

  function isVisible(el) {
    if (!(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    // must intersect (loosely) the viewport-extended document area
    if (rect.bottom < -2000 || rect.top > window.innerHeight + 6000) return false;
    return true;
  }

  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();
    if (["a", "button", "input", "textarea", "select", "summary", "label"].includes(tag)) return true;
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (["button", "link", "checkbox", "radio", "tab", "menuitem", "switch", "option", "combobox", "textbox", "searchbox"].includes(role)) return true;
    if (el.hasAttribute("onclick")) return true;
    if (el.isContentEditable) return true;
    const ti = el.getAttribute("tabindex");
    if (ti !== null && ti !== "-1") return true;
    return false;
  }

  function labelFor(el) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const parts = [];
    const aria = el.getAttribute("aria-label");
    const placeholder = el.getAttribute("placeholder");
    const title = el.getAttribute("title");
    const alt = el.getAttribute("alt");
    const name = el.getAttribute("name");
    // Only surface a control's live value when it is the visible button label.
    // Never leak typed text from inputs/textareas (could be a password, OTP,
    // card number, etc.) into the snapshot sent to the model.
    const buttonish = tag === "input" && ["submit", "button", "reset"].includes(type);
    const value = buttonish ? el.value : "";
    let text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    if (aria) parts.push(aria);
    else if (text) parts.push(text);
    else if (placeholder) parts.push(placeholder);
    else if (alt) parts.push(alt);
    else if (title) parts.push(title);
    else if (value) parts.push(value);
    else if (name) parts.push(name);
    let label = parts.join(" ").trim();
    if (label.length > MAX_LABEL) label = label.slice(0, MAX_LABEL) + "…";
    return label;
  }

  function descriptor(el) {
    const tag = el.tagName.toLowerCase();
    let d = tag;
    if (tag === "input") d += `:${el.getAttribute("type") || "text"}`;
    const role = el.getAttribute("role");
    if (role && !["input", "button", "a", "textarea", "select"].includes(tag)) d += ` role=${role}`;
    return d;
  }

  function buildSnapshot() {
    // reset prior index attributes
    document.querySelectorAll("[data-agent-index]").forEach((e) => e.removeAttribute("data-agent-index"));
    elementMap = [];
    const seen = new Set();
    const all = document.querySelectorAll(
      "a, button, input, textarea, select, summary, label, [role], [onclick], [tabindex], [contenteditable]"
    );
    const lines = [];
    all.forEach((el) => {
      if (seen.has(el)) return;
      if (!isInteractive(el)) return;
      if (!isVisible(el)) return;
      // skip if an ancestor is already indexed and is itself a control (avoid label+input dupes)
      seen.add(el);
      const idx = elementMap.length;
      elementMap.push(el);
      el.setAttribute("data-agent-index", String(idx));
      const rect = el.getBoundingClientRect();
      const inView = rect.bottom > 0 && rect.top < window.innerHeight;
      const label = labelFor(el);
      const state = [];
      if (el.disabled) state.push("disabled");
      if (el.checked) state.push("checked");
      if (el.getAttribute("aria-expanded")) state.push(`expanded=${el.getAttribute("aria-expanded")}`);
      if (!inView) state.push("offscreen");
      const stateStr = state.length ? ` (${state.join(", ")})` : "";
      lines.push(`[${idx}] <${descriptor(el)}> ${label ? `"${label}"` : "(no label)"}${stateStr}`);
    });

    let bodyText = "";
    try {
      bodyText = (document.body?.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
    } catch (_) {}
    if (bodyText.length > MAX_TEXT) bodyText = bodyText.slice(0, MAX_TEXT) + "\n…[truncated]";

    const scrollY = window.scrollY;
    const maxScroll = Math.max(0, document.body.scrollHeight - window.innerHeight);
    return {
      url: location.href,
      title: document.title,
      elementCount: elementMap.length,
      elements: lines.join("\n"),
      pageText: bodyText,
      scroll: {
        y: Math.round(scrollY),
        max: Math.round(maxScroll),
        atTop: scrollY <= 4,
        atBottom: scrollY >= maxScroll - 4,
      },
    };
  }

  // ---------------------------------------------------------------------------
  // Overlays (highlight + callouts)
  // ---------------------------------------------------------------------------

  function ensureOverlayRoot() {
    if (overlayRoot && document.body.contains(overlayRoot)) return overlayRoot;
    overlayRoot = document.createElement("div");
    overlayRoot.id = "__copilot_agent_overlay_root";
    overlayRoot.style.cssText =
      "position:fixed;inset:0;z-index:2147483646;pointer-events:none;";
    document.documentElement.appendChild(overlayRoot);
    window.addEventListener("scroll", scheduleReposition, true);
    window.addEventListener("resize", scheduleReposition, true);
    return overlayRoot;
  }

  function scheduleReposition() {
    if (repositionScheduled) return;
    repositionScheduled = true;
    requestAnimationFrame(() => {
      repositionScheduled = false;
      repositionOverlays();
    });
  }

  function repositionOverlays() {
    activeOverlays.forEach((o) => {
      if (!o.el || !document.contains(o.el)) return;
      const rect = o.el.getBoundingClientRect();
      if (o.box) {
        o.box.style.left = `${rect.left - 4}px`;
        o.box.style.top = `${rect.top - 4}px`;
        o.box.style.width = `${rect.width + 8}px`;
        o.box.style.height = `${rect.height + 8}px`;
      }
      if (o.badge) {
        o.badge.style.left = `${rect.left - 4}px`;
        o.badge.style.top = `${Math.max(2, rect.top - 22)}px`;
      }
      if (o.bubble) {
        positionBubble(o.bubble, rect);
      }
    });
  }

  function positionBubble(bubble, rect) {
    const margin = 10;
    const bw = bubble.offsetWidth || 280;
    const bh = bubble.offsetHeight || 80;
    let top = rect.bottom + margin;
    if (top + bh > window.innerHeight - 8) top = Math.max(8, rect.top - bh - margin);
    let left = rect.left;
    if (left + bw > window.innerWidth - 8) left = Math.max(8, window.innerWidth - bw - 8);
    bubble.style.left = `${left}px`;
    bubble.style.top = `${top}px`;
  }

  function clearOverlays() {
    activeOverlays = [];
    if (overlayRoot) overlayRoot.innerHTML = "";
  }

  function makeBox(rect, color, badgeText) {
    const root = ensureOverlayRoot();
    const box = document.createElement("div");
    box.className = "__copilot_box";
    box.style.cssText = `position:fixed;left:${rect.left - 4}px;top:${rect.top - 4}px;width:${rect.width + 8}px;height:${rect.height + 8}px;border:2px solid ${color};border-radius:6px;box-shadow:0 0 0 3px ${color}33, 0 0 14px ${color}66;pointer-events:none;transition:all .12s ease;`;
    root.appendChild(box);
    let badge = null;
    if (badgeText != null) {
      badge = document.createElement("div");
      badge.textContent = badgeText;
      badge.style.cssText = `position:fixed;left:${rect.left - 4}px;top:${Math.max(2, rect.top - 22)}px;background:${color};color:#fff;font:600 12px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;padding:1px 7px;border-radius:5px;pointer-events:none;white-space:nowrap;`;
      root.appendChild(badge);
    }
    return { box, badge };
  }

  function highlight(indexes, note, color) {
    color = color || "#ff007a";
    let first = null;
    indexes.forEach((idx, i) => {
      const el = elementMap[idx];
      if (!el || !document.contains(el)) return;
      if (i === 0) first = el;
      const rect = el.getBoundingClientRect();
      const { box, badge } = makeBox(rect, color, String(idx));
      activeOverlays.push({ el, box, badge });
    });
    if (note && first) {
      showBubble(first, note, color);
    }
    if (first) {
      first.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
      setTimeout(scheduleReposition, 350);
    }
    return { ok: true, highlighted: indexes.filter((i) => elementMap[i]) };
  }

  function showBubble(el, message, color, requestId) {
    const root = ensureOverlayRoot();
    const rect = el.getBoundingClientRect();
    const bubble = document.createElement("div");
    bubble.className = "__copilot_bubble";
    bubble.style.cssText = `position:fixed;max-width:320px;background:#1b1b22;color:#fff;border:1px solid ${color};border-left:4px solid ${color};border-radius:10px;padding:10px 12px;font:13px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.4);pointer-events:auto;z-index:2147483647;`;
    const head = document.createElement("div");
    head.style.cssText = `font-weight:700;font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:${color};margin-bottom:4px;display:flex;justify-content:space-between;align-items:center;gap:8px;`;
    head.innerHTML = `<span style="display:inline-flex;align-items:center;gap:6px;">${svgIcon("hand", 13)} Your turn</span>`;
    const close = document.createElement("span");
    close.innerHTML = svgIcon("x", 13);
    close.style.cssText = "cursor:pointer;opacity:.6;display:inline-flex;";
    close.onclick = () => bubble.remove();
    head.appendChild(close);
    const body = document.createElement("div");
    body.textContent = message;
    bubble.appendChild(head);
    bubble.appendChild(body);
    if (requestId != null) appendConfirm(bubble, color, requestId);
    root.appendChild(bubble);
    positionBubble(bubble, rect);
    activeOverlays.push({ el, bubble });
    return bubble;
  }

  // "Done" button for instruct_user — tells the bridge the user finished the
  // manual step, which resolves the daemon's pending request.
  function appendConfirm(bubble, color, requestId) {
    const btn = document.createElement("button");
    btn.textContent = "Done — continue";
    btn.style.cssText = `margin-top:8px;display:block;background:${color};color:#1b1b22;border:none;border-radius:7px;padding:6px 12px;font:600 12px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;cursor:pointer;`;
    btn.onclick = () => {
      chrome.runtime.sendMessage({ type: "INSTRUCT_DONE", requestId });
      clearOverlays();
    };
    bubble.appendChild(btn);
  }

  // Floating callout not tied to a specific element (general instruction)
  function showFloatingNote(message, color, requestId) {
    color = color || "#ffb020";
    const root = ensureOverlayRoot();
    const bubble = document.createElement("div");
    bubble.style.cssText = `position:fixed;right:16px;bottom:16px;max-width:340px;background:#1b1b22;color:#fff;border:1px solid ${color};border-left:4px solid ${color};border-radius:10px;padding:12px 14px;font:13px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.45);pointer-events:auto;z-index:2147483647;`;
    bubble.innerHTML = `<div style="font-weight:700;font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:${color};margin-bottom:4px;display:flex;align-items:center;gap:6px;">${svgIcon("bulb", 13)} Note from Sunday</div>`;
    const body = document.createElement("div");
    body.textContent = message;
    bubble.appendChild(body);
    if (requestId != null) appendConfirm(bubble, color, requestId);
    root.appendChild(bubble);
    activeOverlays.push({ el: document.body, bubble });
    return { ok: true };
  }

  // ---------------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------------

  function resolveEl(index) {
    const el = elementMap[index];
    if (!el) return { error: `No element with index ${index}. Call read_page to refresh indexes.` };
    if (!document.contains(el)) return { error: `Element ${index} is no longer on the page. Call read_page to refresh.` };
    return { el };
  }

  function clickIndex(index) {
    const { el, error } = resolveEl(index);
    if (error) return { ok: false, error };
    el.scrollIntoView({ block: "center", inline: "center" });
    flash(el, "#22c55e");
    try {
      el.focus({ preventScroll: true });
    } catch (_) {}
    el.click();
    return { ok: true, clicked: index, label: labelFor(el) };
  }

  // Fields the agent must never type into — the human enters these via
  // instruct_user. Enforced here (not just in the daemon's prompt) so the
  // safety handoff survives any upstream mistake.
  function isProtectedField(el) {
    const type = ((el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
    if (type === "password") return true;
    const ac = ((el.getAttribute && el.getAttribute("autocomplete")) || "").toLowerCase();
    return ["one-time-code", "current-password", "new-password", "cc-number", "cc-csc"].includes(ac);
  }

  function fillIndex(index, value, submit) {
    const { el, error } = resolveEl(index);
    if (error) return { ok: false, error };
    if (isProtectedField(el)) {
      return {
        ok: false,
        error:
          "Refusing to fill a password/one-time-code/payment field. Use instruct_user — the user must enter this themselves.",
      };
    }
    el.scrollIntoView({ block: "center", inline: "center" });
    flash(el, "#3b82f6");
    const tag = el.tagName.toLowerCase();
    try {
      el.focus({ preventScroll: true });
    } catch (_) {}
    if (tag === "select") {
      const opt = Array.from(el.options).find(
        (o) => o.value === value || o.text.trim().toLowerCase() === String(value).trim().toLowerCase()
      );
      if (!opt) return { ok: false, error: `No <option> matching "${value}" in select ${index}.` };
      el.value = opt.value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, filled: index, value: opt.value };
    }
    if (el.isContentEditable) {
      el.textContent = value;
      el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    } else if (el.type === "checkbox" || el.type === "radio") {
      const want = value === true || value === "true" || value === "on" || value === "1";
      if (el.checked !== want) el.click();
      return { ok: true, filled: index, checked: el.checked };
    } else {
      setNativeValue(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    if (submit) {
      el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
      el.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
      if (el.form) {
        try { el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit(); } catch (_) {}
      }
    }
    return { ok: true, filled: index, value: String(value).slice(0, 60) };
  }

  // React-friendly value setter
  function setNativeValue(el, value) {
    const proto = el.tagName.toLowerCase() === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
  }

  function flash(el, color) {
    const rect = el.getBoundingClientRect();
    const { box } = makeBox(rect, color, null);
    setTimeout(() => box.remove(), 700);
  }

  const KEY_CODES = {
    Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46, ArrowUp: 38,
    ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39, PageUp: 33, PageDown: 34,
    Home: 36, End: 35, " ": 32, Space: 32,
  };

  function pressKey(key, index) {
    let el = document.activeElement;
    if (typeof index === "number") {
      const r = resolveEl(index);
      if (r.error) return { ok: false, error: r.error };
      el = r.el;
      try { el.focus({ preventScroll: true }); } catch (_) {}
      flash(el, "#a855f7");
    }
    el = el || document.body;
    const k = key === "Space" ? " " : key;
    const code = KEY_CODES[key] || 0;
    const init = { key: k, code: key, keyCode: code, which: code, bubbles: true, cancelable: true };
    el.dispatchEvent(new KeyboardEvent("keydown", init));
    el.dispatchEvent(new KeyboardEvent("keypress", init));
    el.dispatchEvent(new KeyboardEvent("keyup", init));
    if (k === "Enter" && el.form) {
      try { el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit(); } catch (_) {}
    }
    return { ok: true, pressed: key };
  }

  function scrollAction(target) {
    if (typeof target === "number") {
      const { el, error } = resolveEl(target);
      if (error) return { ok: false, error };
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      return { ok: true };
    }
    const t = String(target || "down").toLowerCase();
    if (t === "top") window.scrollTo({ top: 0, behavior: "smooth" });
    else if (t === "bottom") window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    else if (t === "up") window.scrollBy({ top: -Math.round(window.innerHeight * 0.8), behavior: "smooth" });
    else window.scrollBy({ top: Math.round(window.innerHeight * 0.8), behavior: "smooth" });
    return { ok: true };
  }

  // ---------------------------------------------------------------------------
  // Message handling
  // ---------------------------------------------------------------------------

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    try {
      switch (msg.type) {
        case "PING":
          sendResponse({ ok: true });
          return;
        case "GET_STATE":
          sendResponse(buildSnapshot());
          return;
        case "HIGHLIGHT":
          sendResponse(highlight(msg.indexes || [], msg.note, msg.color));
          return;
        case "INSTRUCT": {
          // highlight (optional) + on-page callout for a manual user step;
          // requestId threads through to the Done button so the bridge can
          // resolve the daemon's pending instruct_user request.
          const el = Array.isArray(msg.indexes) && msg.indexes.length
            ? (highlight(msg.indexes, null, "#ffb020"), elementMap[msg.indexes[0]])
            : null;
          if (el) showBubble(el, msg.message, "#ffb020", msg.requestId);
          else showFloatingNote(msg.message, "#ffb020", msg.requestId);
          sendResponse({ ok: true });
          return;
        }
        case "NOTE":
          sendResponse(showFloatingNote(msg.message, msg.color));
          return;
        case "CLICK":
          sendResponse(clickIndex(msg.index));
          return;
        case "FILL":
          sendResponse(fillIndex(msg.index, msg.value, msg.submit));
          return;
        case "SCROLL":
          sendResponse(scrollAction(msg.target));
          return;
        case "PRESS_KEY":
          sendResponse(pressKey(msg.key, msg.index));
          return;
        case "CLEAR":
          clearOverlays();
          sendResponse({ ok: true });
          return;
        default:
          sendResponse({ ok: false, error: `Unknown message type ${msg.type}` });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e && e.message ? e.message : e) });
    }
    return true;
  });
})();
