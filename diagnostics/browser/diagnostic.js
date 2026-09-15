/* Offline diagnostic, deliberately independent of notebook/Jupyter JS APIs. */
(() => {
  "use strict";
  if (window.I00 && window.I00.dispose) window.I00.dispose();
  const $ = (id) => document.getElementById(id);
  const stage = $("stage"), canvas = $("canvas"), context = canvas.getContext("2d");
  const started = performance.now();
  const events = [], intervals = [];
  const listeners = [];
  const standalone = window.I00_STANDALONE === true;
  const features = {
    inline_script: window.I00_INLINE === true ? "ok" : "not_executed",
    main_script: "ok", main_script_source: standalone ? "inline" : "external",
    external_script: standalone ? "not_tested" : "ok",
    relative_json_fetch: standalone ? "not_tested" : "pending",
    worker: "pending", worker_source: standalone ? "blob" : "relative_file", wasm: "pending",
    fullscreen: "not_tested", canvas: context ? "ok" : "unavailable",
  };
  let running = false, frame = 0, raf = null, next = 0, lastRendered = null;
  let visibleUpdates = 0, startedAt = null, measuredSeconds = 0, lastSample = performance.now();
  let disposed = false, featureWorker = null, workerTimer = null, workerURL = null;
  let previousWidth = 0, previousHeight = 0;

  function event(type, detail = {}) {
    events.push({ at_ms: Math.round(performance.now() - started), type, ...detail });
    if (events.length > 100) events.shift();
  }
  function on(target, name, handler) {
    target.addEventListener(name, handler);
    listeners.push(() => target.removeEventListener(name, handler));
  }
  function dimensions() {
    const rect = canvas.getBoundingClientRect();
    return { css_viewport: [innerWidth, innerHeight], css_canvas: [rect.width, rect.height],
      canvas_backing: [canvas.width, canvas.height], device_pixel_ratio: devicePixelRatio,
      screen_css: [screen.width, screen.height], visual_viewport_scale: window.visualViewport?.scale ?? null,
      cell_backing_px: Number($("cell").value), host_captured_pixels: "not_measured" };
  }
  function report() {
    const now = performance.now();
    const seconds = measuredSeconds + (running && startedAt !== null ? (now - startedAt) / 1000 : 0);
    return { schema: 1, diagnostic: "i00-browser-v1", evidence_level: "browser_only",
      created_utc: new Date().toISOString(), user_agent: navigator.userAgent,
      route_declared: $("route").value, url_protocol: location.protocol,
      embedded: window.self !== window.top, secure_context: window.isSecureContext,
      cross_origin_isolated: window.crossOriginIsolated === true, visibility: document.visibilityState,
      features: { ...features }, geometry: dimensions(), playback: {
        running, frame, target_updates_per_second: Number($("rate").value),
        updates: visibleUpdates, active_seconds_including_hidden: seconds,
        actual_updates_per_second: seconds > 0 ? visibleUpdates / seconds : 0,
        recent_render_intervals_ms: intervals.slice(),
      }, events: events.slice(), limitations: [
        "No host capture or VDI color measurement is performed by this page.",
        "Reported updates are browser render calls, not confirmed VDI frames.",
        "WASM check compiles an empty module; real codec/workers require a separate test.",
        "CSP events can be incomplete; not_executed does not identify the cause by itself.",
      ] };
  }
  function list(element, data) {
    element.replaceChildren();
    for (const [key, value] of Object.entries(data)) {
      const label = document.createElement("dt"), text = document.createElement("dd");
      label.textContent = key;
      text.textContent = typeof value === "object" ? JSON.stringify(value) : String(value);
      element.append(label, text);
    }
  }
  function refresh() {
    const snapshot = report();
    list($("features"), features);
    list($("geometry"), { ...snapshot.geometry, frame, running,
      actual_updates_per_second: snapshot.playback.actual_updates_per_second.toFixed(2) });
    const text = JSON.stringify(snapshot, null, 2);
    $("report").textContent = text;
    $("report-text").value = text;
  }
  function draw() {
    if (!context) return;
    const width = canvas.width, height = canvas.height, cell = Number($("cell").value);
    context.imageSmoothingEnabled = false;
    context.fillStyle = "white";
    context.fillRect(0, 0, width, height);
    // Contrast grid: integer backing pixels, central area clear of controls.
    const margin = 20, gridWidth = Math.floor((width - margin * 2) / cell) * cell;
    const gridHeight = Math.max(0, Math.floor((height - 130) / cell) * cell);
    context.fillStyle = "black";
    for (let y = 0; y < gridHeight; y += cell) {
      for (let x = 0; x < gridWidth; x += cell) {
        if (((x / cell + y / cell + frame) & 1) === 0) context.fillRect(x + margin, y + margin, cell, cell);
      }
    }
    const colors = ["#000000", "#ff0000", "#00ff00", "#0000ff", "#00ffff", "#ff00ff", "#ffff00", "#ffffff"];
    colors.forEach((color, i) => {
      context.fillStyle = color;
      const left = margin + Math.floor(i * gridWidth / 8);
      const right = margin + Math.floor((i + 1) * gridWidth / 8);
      context.fillRect(left, height - 85, right - left, 50);
      context.strokeStyle = "#444";
      context.strokeRect(left + 0.5, height - 84.5, right - left - 1, 49);
    });
    $("overlay").textContent = `Кадр ${frame} · ${cell} px · ${running ? "показ" : "пауза"} · Space / Esc`;
  }
  function resize() {
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round(rect.width * devicePixelRatio));
    canvas.height = Math.max(1, Math.round(rect.height * devicePixelRatio));
    if (previousWidth !== canvas.width || previousHeight !== canvas.height) {
      previousWidth = canvas.width; previousHeight = canvas.height;
      event("resize", { backing: [canvas.width, canvas.height] });
    }
    draw();
  }
  function tick(now) {
    raf = null;
    if (!running || disposed) return;
    if (now >= next) {
      if (lastRendered !== null) {
        intervals.push(Math.round((now - lastRendered) * 100) / 100);
        if (intervals.length > 120) intervals.shift();
      }
      frame++; visibleUpdates++; lastRendered = now;
      // Hold one full frame after a stall; never replay an overdue burst.
      next = now + 1000 / Number($("rate").value);
      draw();
    }
    if (now - lastSample >= 1000) { refresh(); lastSample = now; }
    raf = requestAnimationFrame(tick);
  }
  function start() {
    if (running || disposed) return;
    running = true; startedAt = performance.now(); next = startedAt; lastRendered = null;
    event("start"); raf = requestAnimationFrame(tick); refresh();
  }
  function pause() {
    if (startedAt !== null) measuredSeconds += (performance.now() - startedAt) / 1000;
    startedAt = null; running = false;
    if (raf !== null) cancelAnimationFrame(raf);
    raf = null; event("pause"); draw(); refresh();
  }
  function step() { pause(); frame++; draw(); refresh(); }
  async function fullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (stage.requestFullscreen) await stage.requestFullscreen();
      else throw new Error("unsupported");
      features.fullscreen = "ok";
    } catch (error) { features.fullscreen = `failed:${error.name}`; }
    refresh();
  }
  async function testFeatures() {
    if (!standalone) {
    try {
      const response = await fetch("probe.json", { cache: "no-store", credentials: "same-origin" });
      if (!response.ok) throw new Error("http");
      const json = await response.json();
      features.relative_json_fetch = json.diagnostic === "i00-relative-resource-v1" ? "ok" : "unexpected_data";
    } catch (error) { features.relative_json_fetch = `failed:${error.name}`; }
    }
    if (disposed) return;
    try {
      const wasm = new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0]);
      await WebAssembly.instantiate(wasm);
      features.wasm = "ok";
    } catch (error) { features.wasm = `failed:${error.name}`; }
    if (disposed) return;
    features.worker = await new Promise((resolve) => {
      const done = (value) => {
        clearTimeout(workerTimer);
        if (featureWorker) featureWorker.terminate();
        if (workerURL) URL.revokeObjectURL(workerURL);
        workerURL = null;
        featureWorker = null; resolve(value);
      };
      try {
        if (standalone) workerURL = URL.createObjectURL(new Blob([window.I00_WORKER_SOURCE], { type: "text/javascript" }));
        featureWorker = new Worker(standalone ? workerURL : "worker.js");
        workerTimer = setTimeout(() => done("timeout"), 3000);
        featureWorker.onmessage = (message) => done(message.data === "i00-worker-ok" ? "ok" : "unexpected_data");
        featureWorker.onerror = () => done("failed:worker_error");
        featureWorker.postMessage("probe");
      } catch (error) { done(`failed:${error.name}`); }
    });
    if (!disposed) { event("feature_checks_complete"); refresh(); }
  }
  on($("start"), "click", start); on($("pause"), "click", pause); on($("step"), "click", step);
  on($("reset"), "click", () => { pause(); frame = 0; visibleUpdates = 0; measuredSeconds = 0; intervals.length = 0; event("reset"); draw(); refresh(); });
  on($("rate"), "change", () => { next = performance.now(); event("rate", { value: Number($("rate").value) }); refresh(); });
  on($("cell"), "change", () => { event("cell", { value: Number($("cell").value) }); draw(); refresh(); });
  on($("fullscreen"), "click", fullscreen); on($("refresh"), "click", refresh); on($("route"), "change", refresh);
  on($("download"), "click", () => {
    refresh();
    const url = URL.createObjectURL(new Blob([JSON.stringify(report(), null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "i00-browser.json";
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  on(document, "keydown", (key) => {
    if (key.code === "Space" && !["TEXTAREA", "SELECT", "INPUT", "BUTTON"].includes(document.activeElement.tagName)) {
      key.preventDefault(); if (running) pause(); else start();
    }
  });
  on(document, "visibilitychange", () => { event("visibility", { state: document.visibilityState }); refresh(); });
  on(document, "fullscreenchange", () => { event("fullscreen", { active: !!document.fullscreenElement }); resize(); refresh(); });
  on(document, "securitypolicyviolation", (violation) => {
    // No blockedURI/documentURI: either might contain a Jupyter token.
    event("csp", { directive: violation.effectiveDirective }); refresh();
  });
  on(window, "resize", resize);
  function dispose() {
    disposed = true; running = false;
    if (raf !== null) cancelAnimationFrame(raf);
    if (featureWorker) featureWorker.terminate();
    if (workerURL) URL.revokeObjectURL(workerURL);
    clearTimeout(workerTimer);
    listeners.forEach((remove) => remove());
  }
  on(window, "pagehide", dispose);
  window.I00 = { snapshot: report, start, pause, step, dispose };
  $("boot").textContent = "Основной JavaScript выполнен. Проверяйте движение счётчика и результаты ниже.";
  resize(); refresh(); testFeatures();
})();
