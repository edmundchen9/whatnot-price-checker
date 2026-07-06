// Whatnot Price Checker — content script.
//
// Press "W" while a Whatnot stream's <video> is visible: grabs the current
// frame straight off the video element (no OS screen capture, no DPI
// ambiguity — this is the same approach validated by the extension-spike),
// sends it to the background service worker (which forwards it to the
// local Python server), and renders the result in a Slabbr-style overlay
// injected into a Shadow DOM so Whatnot's page styles can't touch it.

(() => {
  "use strict";

  const CONDITION_ORDER = ["NM", "LP", "MP", "HP", "DM"];
  const CONDITION_CAPTIONS = {
    NM: "NEAR MINT",
    LP: "LIGHTLY PLAYED",
    MP: "MODERATELY PLAYED",
    HP: "HEAVILY PLAYED",
    DM: "DAMAGED",
  };
  const STATUS_DOT_COLORS = {
    ok: "#3ddc84",
    warn: "#e0c34d",
    idle: "#5a5a68",
    error: "#e0744d",
  };
  const STATUS_DEFAULT_MESSAGES = {
    idle: 'Press "W" to capture the stream and scan the card.',
    capturing: "Capturing frame and reading card…",
    error: "Unknown error.",
  };
  const CONFIDENCE_BG = {
    "#3ddc84": "#1f3d2c",
    "#e0c34d": "#3d3520",
    "#e0744d": "#3d2820",
    "#8a8a98": "#2a2a34",
  };
  const POSITION_STORAGE_KEY = "wpcOverlayPosition";

  const state = {
    prefersFoil: false,
    selectedCondition: "NM",
    latestPayload: { status: "idle" },
    viewUrl: "",
    scanCount: 0,
    scanning: false,
    dragging: false,
    dragOffset: { x: 0, y: 0 },
  };

  let els = {};

  // --- DOM construction -----------------------------------------------

  function buildPanelHtml() {
    const conditionBtns = CONDITION_ORDER.map(
      (code) =>
        `<button class="wpc-condition-btn" data-code="${code}">${code}\n—</button>`
    ).join("");

    return `
      <div class="wpc-panel" id="wpc-panel">
        <div class="wpc-header" id="wpc-header">
          <span class="wpc-status-dot" id="wpc-status-dot">●</span>
          <span class="wpc-title">WHATNOT PRICE CHECKER</span>
          <span class="wpc-spacer"></span>
          <button class="wpc-close-btn" id="wpc-close-btn">×</button>
        </div>
        <div class="wpc-toolbar">
          <button class="wpc-foil-btn" id="wpc-foil-btn">Normal</button>
          <button class="wpc-scan-btn" id="wpc-scan-btn">Scan (W)</button>
        </div>
        <div class="wpc-tabs">
          <span class="wpc-tab wpc-tab-active">SCANNER</span>
          <span class="wpc-tab wpc-tab-disabled" title="Coming soon">COLLECTION</span>
        </div>
        <div class="wpc-status-label" id="wpc-status-label"></div>
        <div class="wpc-card-body" id="wpc-card-body" style="display: none;">
          <div class="wpc-top-row">
            <img class="wpc-thumb" id="wpc-thumb" alt="" />
            <div class="wpc-price-col">
              <span class="wpc-condition-caption" id="wpc-condition-caption">NEAR MINT</span>
              <span class="wpc-price" id="wpc-price">—</span>
              <span class="wpc-source-caption" id="wpc-source-caption" style="display: none;"></span>
              <span class="wpc-change-label" id="wpc-change-label" style="display: none;"></span>
            </div>
          </div>
          <div class="wpc-name" id="wpc-name">—</div>
          <div class="wpc-set" id="wpc-set">—</div>
          <div class="wpc-number" id="wpc-number"></div>
          <div class="wpc-tag-row">
            <span class="wpc-pill" id="wpc-confidence-tag"></span>
            <span class="wpc-pill" id="wpc-tier-tag"></span>
          </div>
          <div class="wpc-conditions-caption" id="wpc-conditions-caption">CONDITIONS</div>
          <div class="wpc-conditions-row" id="wpc-conditions-row">${conditionBtns}</div>
          <button class="wpc-view-btn" id="wpc-view-btn" disabled>VIEW ON TCGPLAYER &#8203;›</button>
          <div class="wpc-footer" id="wpc-footer"></div>
        </div>
      </div>
    `;
  }

  async function loadOverlayCss() {
    try {
      const res = await fetch(chrome.runtime.getURL("overlay.css"));
      return await res.text();
    } catch (e) {
      console.warn("[WPC] Failed to load overlay.css", e);
      return "";
    }
  }

  async function init() {
    if (document.getElementById("wpc-root-host")) return;

    const host = document.createElement("div");
    host.id = "wpc-root-host";
    document.documentElement.appendChild(host);
    const shadow = host.attachShadow({ mode: "open" });

    const styleEl = document.createElement("style");
    styleEl.textContent = await loadOverlayCss();
    shadow.appendChild(styleEl);

    const container = document.createElement("div");
    container.innerHTML = buildPanelHtml();
    shadow.appendChild(container);

    els = {
      panel: shadow.getElementById("wpc-panel"),
      header: shadow.getElementById("wpc-header"),
      statusDot: shadow.getElementById("wpc-status-dot"),
      closeBtn: shadow.getElementById("wpc-close-btn"),
      foilBtn: shadow.getElementById("wpc-foil-btn"),
      scanBtn: shadow.getElementById("wpc-scan-btn"),
      statusLabel: shadow.getElementById("wpc-status-label"),
      cardBody: shadow.getElementById("wpc-card-body"),
      thumb: shadow.getElementById("wpc-thumb"),
      conditionCaption: shadow.getElementById("wpc-condition-caption"),
      price: shadow.getElementById("wpc-price"),
      sourceCaption: shadow.getElementById("wpc-source-caption"),
      changeLabel: shadow.getElementById("wpc-change-label"),
      name: shadow.getElementById("wpc-name"),
      set: shadow.getElementById("wpc-set"),
      number: shadow.getElementById("wpc-number"),
      confidenceTag: shadow.getElementById("wpc-confidence-tag"),
      tierTag: shadow.getElementById("wpc-tier-tag"),
      conditionsCaption: shadow.getElementById("wpc-conditions-caption"),
      conditionsRow: shadow.getElementById("wpc-conditions-row"),
      conditionBtns: {},
      viewBtn: shadow.getElementById("wpc-view-btn"),
      footer: shadow.getElementById("wpc-footer"),
    };
    shadow.querySelectorAll(".wpc-condition-btn").forEach((btn) => {
      els.conditionBtns[btn.dataset.code] = btn;
      btn.addEventListener("click", () => onConditionClick(btn.dataset.code));
    });

    els.closeBtn.addEventListener("click", () => els.panel.classList.add("wpc-hidden"));
    els.foilBtn.addEventListener("click", onToggleFoil);
    els.scanBtn.addEventListener("click", triggerScan);
    els.viewBtn.addEventListener("click", () => {
      if (state.viewUrl) window.open(state.viewUrl, "_blank", "noopener");
    });

    setupDragging();
    await restorePosition();

    renderBody({ status: "idle" });

    document.addEventListener("keydown", onKeyDown, true);
  }

  // --- Dragging ----------------------------------------------------------

  function setupDragging() {
    els.header.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      state.dragging = true;
      const rect = els.panel.getBoundingClientRect();
      state.dragOffset = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!state.dragging) return;
      const left = e.clientX - state.dragOffset.x;
      const top = e.clientY - state.dragOffset.y;
      els.panel.style.left = `${Math.max(0, left)}px`;
      els.panel.style.top = `${Math.max(0, top)}px`;
      els.panel.style.right = "auto";
    });
    document.addEventListener("mouseup", () => {
      if (!state.dragging) return;
      state.dragging = false;
      savePosition();
    });
  }

  function savePosition() {
    const rect = els.panel.getBoundingClientRect();
    try {
      chrome.storage.local.set({
        [POSITION_STORAGE_KEY]: { left: rect.left, top: rect.top },
      });
    } catch (e) {
      /* storage unavailable — non-fatal */
    }
  }

  async function restorePosition() {
    try {
      const stored = await chrome.storage.local.get(POSITION_STORAGE_KEY);
      const pos = stored?.[POSITION_STORAGE_KEY];
      if (pos && typeof pos.left === "number" && typeof pos.top === "number") {
        els.panel.style.left = `${pos.left}px`;
        els.panel.style.top = `${pos.top}px`;
        els.panel.style.right = "auto";
      }
    } catch (e) {
      /* ignore */
    }
  }

  // --- Capture -------------------------------------------------------------

  function pickMainVideo() {
    const videos = Array.from(document.querySelectorAll("video"));
    const candidates = videos.filter((v) => v.readyState >= 2 && v.videoWidth > 0);
    if (candidates.length === 0) return null;
    candidates.sort((a, b) => b.videoWidth * b.videoHeight - a.videoWidth * a.videoHeight);
    return candidates[0];
  }

  function captureFrameDataUrl(video) {
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/png");
  }

  function sendToBackground(type, payload) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ type, ...payload }, (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        if (!response) {
          reject(new Error("No response from background script."));
          return;
        }
        if (!response.ok) {
          reject(new Error(response.error || "Request failed."));
          return;
        }
        resolve(response.data);
      });
    });
  }

  async function triggerScan() {
    if (state.scanning) return;
    els.panel.classList.remove("wpc-hidden");

    const video = pickMainVideo();
    if (!video) {
      state.latestPayload = {
        status: "error",
        detail: "No live video found on this page. Open a stream and try again.",
      };
      renderBody(state.latestPayload);
      return;
    }

    state.scanning = true;
    state.scanCount += 1;
    els.scanBtn.disabled = true;
    state.selectedCondition = "NM";
    renderBody({ status: "capturing" });

    try {
      const image = captureFrameDataUrl(video);
      const data = await sendToBackground("WPC_SCAN", {
        image,
        preferFoil: state.prefersFoil,
      });
      state.latestPayload = data;
      renderBody(data);
    } catch (err) {
      state.latestPayload = {
        status: "error",
        detail: `Couldn't reach the local server: ${err.message}. Is "python -m whatnot_price_checker.server" running?`,
      };
      renderBody(state.latestPayload);
    } finally {
      state.scanning = false;
      els.scanBtn.disabled = false;
    }
  }

  function onKeyDown(e) {
    if (e.key.toLowerCase() !== "w") return;
    const active = document.activeElement;
    const tag = active?.tagName?.toLowerCase();
    if (tag === "input" || tag === "textarea" || active?.isContentEditable) return;
    triggerScan();
  }

  // --- Interaction handlers -----------------------------------------------

  function onToggleFoil() {
    state.prefersFoil = !state.prefersFoil;
    els.foilBtn.textContent = state.prefersFoil ? "Foil" : "Normal";
    els.foilBtn.classList.toggle("wpc-foil-active", state.prefersFoil);
  }

  function onConditionClick(code) {
    if (els.conditionBtns[code]?.disabled) return;
    state.selectedCondition = code;
    renderBody(state.latestPayload);
  }

  // --- Rendering (ported 1:1 from app.py OverlayWindow._render_body) ------

  function setStatusDot(kind) {
    els.statusDot.style.color = STATUS_DOT_COLORS[kind] || STATUS_DOT_COLORS.idle;
  }

  function priceTierLabel(market) {
    const val = parseFloat(market);
    if (Number.isNaN(val)) return "";
    if (val < 5) return "Bulk <$5";
    if (val < 25) return "Mid $5–$25";
    return "Chase $25+";
  }

  function normalizeCollector(value) {
    const prefix = (value || "").split("/")[0] || "";
    const digits = prefix
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "")
      .replace(/^0+/, "");
    return digits;
  }

  function collectorMatches(a, b) {
    const na = normalizeCollector(a);
    const nb = normalizeCollector(b);
    return Boolean(na) && na === nb;
  }

  function confidenceLabel(data) {
    if (!(data.tcg_name || "").trim()) return ["NO MATCH", "#8a8a98"];
    const collector = (data.collector_number || "").trim();
    const cardNumber = (data.card_number || "").trim();
    const detail = (data.detail || "").trim();
    if (collector && cardNumber && collectorMatches(collector, cardNumber) && !detail) {
      return ["HIGH CONFIDENCE", "#3ddc84"];
    }
    if (collector && cardNumber && !collectorMatches(collector, cardNumber)) {
      return ["LOW CONFIDENCE", "#e0744d"];
    }
    return ["MEDIUM CONFIDENCE", "#e0c34d"];
  }

  function setPill(el, text, fg) {
    if (!text) {
      el.classList.remove("wpc-pill-visible");
      return;
    }
    el.textContent = text;
    el.style.color = fg;
    el.style.background = CONFIDENCE_BG[fg] || "#2a2a34";
    el.classList.add("wpc-pill-visible");
  }

  function truncateLine(s, maxLen = 56) {
    const t = String(s || "").trim();
    if (t.length <= maxLen) return t;
    return t.slice(0, maxLen - 1) + "…";
  }

  function renderBody(data) {
    const status = data?.status || "";
    if (!status) return;

    if (status !== "card") {
      els.cardBody.style.display = "none";
      els.statusLabel.style.display = "block";
      els.statusLabel.textContent =
        data.detail || STATUS_DEFAULT_MESSAGES[status] || "Unknown status.";
      setStatusDot(status === "error" ? "error" : "idle");
      return;
    }

    els.statusLabel.style.display = "none";
    els.cardBody.style.display = "flex";

    const tcgName = (data.tcg_name || "").trim();
    const ocrName = (data.ocr_name || "").trim();
    const collector = (data.collector_number || "").trim();
    const cardNumber = (data.card_number || "").trim();
    const displayNumber = cardNumber || collector || "";
    const rarity = (data.rarity || "").trim();
    const market = (data.market || "").trim();

    setStatusDot(tcgName ? "ok" : "warn");

    if (data.warp_thumb) {
      els.thumb.src = data.warp_thumb;
      els.thumb.style.visibility = "visible";
    }

    const conditions = data.condition_prices || {};
    for (const code of CONDITION_ORDER) {
      const price = conditions[code];
      const btn = els.conditionBtns[code];
      btn.textContent = `${code}\n${price != null ? "$" + price.toFixed(2) : "—"}`;
      btn.disabled = price == null;
      btn.classList.toggle("wpc-condition-selected", code === state.selectedCondition);
    }

    if (conditions[state.selectedCondition] == null) {
      const fallback = CONDITION_ORDER.find((c) => conditions[c] != null);
      if (fallback) {
        state.selectedCondition = fallback;
        for (const code of CONDITION_ORDER) {
          els.conditionBtns[code].classList.toggle("wpc-condition-selected", code === fallback);
        }
      }
    }

    const selectedPrice = conditions[state.selectedCondition];
    let priceText;
    if (selectedPrice != null) {
      priceText = `$${selectedPrice.toFixed(2)}`;
    } else if (market && market !== "—") {
      priceText = market.startsWith("$") ? market : `$${market}`;
    } else {
      priceText = "—";
    }
    els.price.textContent = priceText;
    els.price.classList.toggle("wpc-price-set", priceText !== "—");
    els.conditionCaption.textContent = CONDITION_CAPTIONS[state.selectedCondition] || "NEAR MINT";

    const source = (data.price_source || "").trim();
    const lookupMs = data.lookup_ms || 0;
    if (source) {
      els.sourceCaption.textContent = `via ${source} · ${lookupMs}ms`;
      els.sourceCaption.style.display = "block";
    } else {
      els.sourceCaption.style.display = "none";
    }

    const change = data.price_change_24h;
    if (typeof change === "number") {
      let arrow, color;
      if (change > 0) {
        arrow = "▲";
        color = "#3ddc84";
      } else if (change < 0) {
        arrow = "▼";
        color = "#e0744d";
      } else {
        arrow = "■";
        color = "#8a8a98";
      }
      els.changeLabel.textContent = `${arrow} ${Math.abs(change).toFixed(1)}% 24h`;
      els.changeLabel.style.color = color;
      els.changeLabel.style.display = "block";
    } else {
      els.changeLabel.style.display = "none";
    }

    els.name.textContent = tcgName || ocrName || "—";
    els.set.textContent = (data.set_name || "").trim() || "—";
    const bits = [];
    if (displayNumber) bits.push(`#${displayNumber}`);
    if (rarity) bits.push(rarity);
    els.number.textContent = bits.join(" · ");

    const [confLabel, confColor] = confidenceLabel(data);
    setPill(els.confidenceTag, confLabel, confColor);

    const tierLabel = priceTierLabel(market);
    setPill(els.tierTag, tierLabel ? `● ${tierLabel}` : "", "#6ab0f5");

    const printing = (data.printing || data.foil_guess || "Normal").trim();
    els.conditionsCaption.textContent = `CONDITIONS · ${printing.toUpperCase()}`;

    const tcgplayerId = (data.tcgplayer_id || "").trim();
    let url = tcgplayerId ? `https://www.tcgplayer.com/product/${tcgplayerId}` : "";
    state.viewUrl = url;
    els.viewBtn.disabled = !url;

    const detail = (data.detail || "").trim();
    const stage = (data.lookup_stage || "").trim();
    const note = detail || (!tcgName ? stage : "");
    if (note) {
      els.footer.textContent = truncateLine(note, 56);
    } else {
      const scans = state.scanCount;
      const tier = source === "JustTCG" ? "JUSTTCG" : "BASIC";
      els.footer.textContent = `${scans} SCAN${scans !== 1 ? "S" : ""} THIS SESSION  ·  ${tier}`;
    }
  }

  init();
})();
