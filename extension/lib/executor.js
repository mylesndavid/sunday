/* Browser tool executors — the extension's hands.
 * Ported from Cockpit's Agent class with the LLM loop removed: each method
 * executes one browser action and returns a plain JSON-able result object.
 * Sunday's daemon is the brain; it calls these over the WebSocket bridge
 * (see background.js) and formats results for its own model.
 *
 * Results convention: methods return the result payload for {id, result}.
 * Failures throw Error(message), which the bridge turns into {id, error}.
 */

export class Executor {
  constructor() {
    this.targetTabId = null;
    // Survive service-worker restarts: the working tab is the only state.
    this._restored = chrome.storage.session
      .get("targetTabId")
      .then((r) => {
        if (typeof r.targetTabId === "number" && this.targetTabId == null) {
          this.targetTabId = r.targetTabId;
        }
      })
      .catch(() => {});
  }

  _persistTarget() {
    chrome.storage.session.set({ targetTabId: this.targetTabId }).catch(() => {});
  }

  // ---------------------------------------------------------------------------
  // Tools (one method per protocol method)
  // ---------------------------------------------------------------------------

  async read_page() {
    const tabId = await this.getTargetTab();
    const snap = await this.snapshot(tabId);
    if (snap.error) throw new Error(snap.error);
    return snap;
  }

  async screenshot() {
    const tabId = await this.getTargetTab();
    const tab = await chrome.tabs.get(tabId);
    if (!tab.active) await chrome.tabs.update(tabId, { active: true });
    const image = await chrome.tabs.captureVisibleTab(tab.windowId, {
      format: "jpeg",
      quality: 70,
    });
    return { image };
  }

  async navigate({ url }) {
    const tabId = await this.getTargetTab();
    url = normalizeUrl(url);
    const loaded = this.waitForLoad(tabId);
    await chrome.tabs.update(tabId, { url });
    await loaded;
    await wait(400);
    const snap = await this.snapshot(tabId);
    if (snap.error) throw new Error(snap.error);
    return { navigated: url, page: snap };
  }

  async click({ index }) {
    const tabId = await this.getTargetTab();
    await this.requireContentScript(tabId);
    const res = await this.tabMessage(tabId, { type: "CLICK", index });
    if (!res || !res.ok) throw new Error(`Click failed: ${res?.error || "unknown"}`);
    const page = await this.settleAndSnapshot(tabId);
    return { clicked: index, label: res.label || "", page };
  }

  async fill({ index, value, submit }) {
    const tabId = await this.getTargetTab();
    await this.requireContentScript(tabId);
    const res = await this.tabMessage(tabId, {
      type: "FILL",
      index,
      value,
      submit: !!submit,
    });
    if (!res || !res.ok) throw new Error(`Fill failed: ${res?.error || "unknown"}`);
    const page = await this.settleAndSnapshot(tabId);
    return { filled: index, page };
  }

  async press_key({ key, index }) {
    const tabId = await this.getTargetTab();
    await this.requireContentScript(tabId);
    const res = await this.tabMessage(tabId, { type: "PRESS_KEY", key, index });
    if (!res || !res.ok) throw new Error(`Key press failed: ${res?.error || "unknown"}`);
    const page = await this.settleAndSnapshot(tabId);
    return { pressed: key, page };
  }

  async scroll({ to, index }) {
    const tabId = await this.getTargetTab();
    await this.requireContentScript(tabId);
    const target = typeof index === "number" ? index : to || "down";
    const res = await this.tabMessage(tabId, { type: "SCROLL", target });
    if (res && res.ok === false) throw new Error(res.error || "Scroll failed");
    const snap = await this.snapshot(tabId);
    if (snap.error) throw new Error(snap.error);
    return { scrolled: true, page: snap };
  }

  async highlight({ indexes, note }) {
    const tabId = await this.getTargetTab();
    await this.requireContentScript(tabId);
    const res = await this.tabMessage(tabId, {
      type: "HIGHLIGHT",
      indexes: indexes || [],
      note,
    });
    if (!res || !res.ok) throw new Error("Highlight failed");
    return { highlighted: res.highlighted };
  }

  async list_tabs() {
    const tabs = await chrome.tabs.query({ windowType: "normal" });
    return {
      tabs: tabs.map((t) => ({
        id: t.id,
        title: t.title || "",
        url: t.url || "",
        active: !!t.active,
        working: t.id === this.targetTabId,
      })),
    };
  }

  async open_tab({ url, focus }) {
    url = normalizeUrl(url);
    const makeWorking = focus !== false;
    const t = await chrome.tabs.create({ url, active: makeWorking });
    if (makeWorking) {
      this.targetTabId = t.id;
      this._persistTarget();
    }
    await this.waitForLoad(t.id);
    await wait(400);
    const snap = await this.snapshot(t.id);
    return { tab_id: t.id, working: makeWorking, page: snap.error ? null : snap };
  }

  async switch_tab({ tab_id }) {
    const t = await chrome.tabs.get(tab_id); // throws if gone
    await chrome.tabs.update(tab_id, { active: true });
    if (t.windowId != null) {
      await chrome.windows.update(t.windowId, { focused: true }).catch(() => {});
    }
    this.targetTabId = tab_id;
    this._persistTarget();
    const snap = await this.snapshot(tab_id);
    return { switched: tab_id, page: snap.error ? null : snap };
  }

  async close_tab({ tab_id }) {
    await chrome.tabs.remove(tab_id);
    if (tab_id === this.targetTabId) {
      this.targetTabId = null;
      this._persistTarget();
    }
    return { closed: tab_id };
  }

  /* Shows the on-page "your turn" callout and highlights the element(s).
   * Does NOT wait — the bridge holds the request open and resolves it with
   * {acknowledged: true} when the content script reports the user confirmed.
   * requestId is threaded through so the confirmation maps back to the
   * pending daemon request. */
  async instruct_user({ message, indexes }, requestId) {
    const tabId = await this.getTargetTab();
    await this.requireContentScript(tabId);
    await this.tabMessage(tabId, {
      type: "INSTRUCT",
      indexes: indexes || [],
      message,
      requestId,
    });
    return { shown: true };
  }

  // ---------------------------------------------------------------------------
  // Tab + content-script plumbing (unchanged from Cockpit)
  // ---------------------------------------------------------------------------

  async getTargetTab() {
    await this._restored;
    if (this.targetTabId != null) {
      try {
        const t = await chrome.tabs.get(this.targetTabId);
        if (t && !t.discarded) return this.targetTabId;
      } catch (_) {}
    }
    let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!tabs.length) tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tabs.length) tabs = await chrome.tabs.query({ active: true });
    if (!tabs.length) throw new Error("No active tab found.");
    this.targetTabId = tabs[0].id;
    this._persistTarget();
    return this.targetTabId;
  }

  tabMessage(tabId, msg) {
    return new Promise((resolve) => {
      chrome.tabs.sendMessage(tabId, msg, (resp) => {
        if (chrome.runtime.lastError) {
          resolve({ __noReceiver: true, error: chrome.runtime.lastError.message });
        } else {
          resolve(resp);
        }
      });
    });
  }

  async ensureContentScript(tabId) {
    const ping = await this.tabMessage(tabId, { type: "PING" });
    if (ping && ping.ok) return true;
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
      await chrome.scripting.insertCSS({ target: { tabId }, files: ["content.css"] });
    } catch (e) {
      return {
        error:
          "Can't operate on this page (it may be a chrome:// page, the Chrome Web Store, a PDF, or a browser-internal page). " +
          `Details: ${e.message}`,
      };
    }
    const ping2 = await this.tabMessage(tabId, { type: "PING" });
    return ping2 && ping2.ok ? true : { error: "Content script did not load on this page." };
  }

  async requireContentScript(tabId) {
    const ensured = await this.ensureContentScript(tabId);
    if (ensured !== true) throw new Error(ensured.error);
  }

  async snapshot(tabId) {
    const ensured = await this.ensureContentScript(tabId);
    if (ensured !== true) return { error: ensured.error };
    const snap = await this.tabMessage(tabId, { type: "GET_STATE" });
    if (!snap || snap.__noReceiver) return { error: "Could not read the page." };
    return snap;
  }

  async settleAndSnapshot(tabId) {
    await wait(700);
    const snap = await this.snapshot(tabId);
    return snap.error
      ? { url: "", title: "", elements: "", pageText: "", elementCount: 0, note: snap.error }
      : snap;
  }

  waitForLoad(tabId, timeout = 30000) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      };
      const listener = (id, info) => {
        if (id === tabId && info.status === "complete") finish();
      };
      chrome.tabs.onUpdated.addListener(listener);
      setTimeout(finish, timeout);
    });
  }
}

function normalizeUrl(url) {
  url = (url || "").trim();
  if (/^https?:\/\//i.test(url)) return url;
  if (/^[\w.-]+\.[a-z]{2,}([/:?#]|$)/i.test(url)) return "https://" + url;
  return "https://www.google.com/search?q=" + encodeURIComponent(url);
}

function wait(ms) {
  return new Promise((r) => setTimeout(r, ms));
}
