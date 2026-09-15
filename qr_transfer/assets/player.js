(() => {
  "use strict";
  if (window.QRTransfer) window.QRTransfer.dispose();
  const $ = id => document.getElementById(id), stage = $("stage"), canvas = $("qr"), ctx = canvas.getContext("2d");
  const config = JSON.parse($("config").textContent), started = performance.now(), listeners = [], events = [];
  const base = document.createElement("canvas"); base.width = base.height = 185;
  const baseCtx = base.getContext("2d"), pixels = baseCtx.createImageData(185, 185);
  const abort = new AbortController();
  let bytes = null, sequence = [], cursor = 0, ready = false, running = false, disposed = false;
  let raf = null, next = 0, updates = 0, cycles = 0, activeSince = null, activeMs = 0, lastShown = null;
  let intervals = [], modulePx = 0, loadMs = null, lastReport = 0, fullscreenResult = "not_tested";
  const stride = 3917;
  $("interval").value = config.interval_ms;
  function on(target, name, callback) { target.addEventListener(name, callback); listeners.push(() => target.removeEventListener(name, callback)); }
  function event(type, detail = {}) { events.push({at_ms: performance.now() - started, type, ...detail}); if (events.length > 100) events.shift(); }
  function interval() { return Number($("interval").value); }
  function snapshot() {
    const rect = canvas.getBoundingClientRect(), seconds = (activeMs + (activeSince === null ? 0 : performance.now() - activeSince)) / 1000;
    return {schema:1, profile:"mono-safe", evidence_level:"browser_only", ready, running, disposed,
      transfer_id:config.transfer_id, current_packet:sequence[cursor] ?? null, cursor, cycle:cycles, updates,
      target_interval_ms:interval(), actual_updates_per_second:seconds > 0 ? updates / seconds : 0,
      active_seconds:seconds, recent_intervals_ms:intervals.slice(), load_ms:loadMs, unique_frames:config.descriptor.total + 1,
      cycle_frames:sequence.length, cycle_seconds:sequence.length * interval() / 1000,
      visibility:document.visibilityState, fullscreen:fullscreenResult, embedded:window.self !== window.top,
      geometry:{viewport_css:[innerWidth,innerHeight], canvas_backing:[canvas.width,canvas.height],
        canvas_css:[rect.width,rect.height], canvas_origin_css:[rect.x,rect.y], device_pixel_ratio:devicePixelRatio,
        module_backing_px:modulePx, quiet_modules:4, top_inset_css:Number($("top").value),
        applied_top_inset_css:document.fullscreenElement === stage ? Number($("top").value) : 0,
        host_pixels:"not_measured"}, events:events.slice()};
  }
  function refresh() {
    const s = snapshot();
    $("stats").textContent = ready ? `${running ? "Показ" : "Пауза"} · кадр ${cursor + 1}/${sequence.length} · цикл ${cycles + 1} · ${s.actual_updates_per_second.toFixed(2)} обновл./с · ${modulePx} px/модуль` : "Ожидание данных";
    $("report").textContent = JSON.stringify(s, null, 2);
    for (const name of ["start", "pause", "step", "reset"]) $(name).disabled = !ready || modulePx < 1;
  }
  function paint() {
    if (!ready || modulePx < 1 || disposed) return;
    pixels.data.fill(255);
    const offset = 12 + sequence[cursor] * stride;
    for (let y = 0; y < 177; y++) for (let x = 0; x < 177; x++) {
      const bit = y * 177 + x;
      if (bytes[offset + (bit >> 3)] & (128 >> (bit & 7))) {
        const pixel = ((y + 4) * 185 + x + 4) * 4;
        pixels.data[pixel] = pixels.data[pixel + 1] = pixels.data[pixel + 2] = 0;
      }
    }
    baseCtx.putImageData(pixels, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(base, 0, 0, canvas.width, canvas.height);
  }
  function resize() {
    const rect = stage.getBoundingClientRect(), dpr = devicePixelRatio;
    const inset = document.fullscreenElement === stage ? Math.ceil(Number($("top").value) * dpr) : 0;
    const width = Math.floor(rect.width * dpr), height = Math.floor(rect.height * dpr) - inset;
    modulePx = Math.max(0, Math.floor(Math.min(width, height) / 185));
    const side = modulePx * 185;
    canvas.width = canvas.height = Math.max(1, side);
    canvas.style.width = canvas.style.height = `${side / dpr}px`;
    canvas.style.left = `${(Math.ceil(rect.left * dpr) + Math.floor((width - side) / 2)) / dpr - rect.left}px`;
    canvas.style.top = `${(Math.ceil(rect.top * dpr) + inset + Math.floor((height - side) / 2)) / dpr - rect.top}px`;
    if (modulePx < 1) pause();
    paint(); refresh();
  }
  function advance() { cursor = (cursor + 1) % sequence.length; if (!cursor) cycles++; paint(); }
  function tick(now) {
    raf = null;
    if (!running || disposed) return;
    if (now >= next) {
      advance(); updates++;
      if (lastShown !== null) { intervals.push(now - lastShown); if (intervals.length > 120) intervals.shift(); }
      lastShown = now;
      next = performance.now() + interval(); // Full hold after rendering; no catch-up burst.
    }
    if (now - lastReport >= 1000) { refresh(); lastReport = now; }
    raf = requestAnimationFrame(tick);
  }
  function start() {
    if (!ready || disposed || running || modulePx < 1 || document.hidden) return;
    running = true; activeSince = performance.now(); lastShown = null; next = activeSince + interval();
    $("status").textContent = modulePx < 4 ? "Показ идёт. QR мелкий: увеличьте область или включите полный экран." : "Показ идёт по кругу. Остановите его после завершения приёма на хосте.";
    event("start"); raf = requestAnimationFrame(tick); refresh();
  }
  function pause() {
    if (activeSince !== null) activeMs += performance.now() - activeSince;
    activeSince = null; running = false;
    if (raf !== null) cancelAnimationFrame(raf);
    raf = null; refresh();
    if (ready && !disposed) $("status").textContent = "Пауза. Последний кадр сохранён; Старт — продолжить.";
  }
  function step() { if (!ready || disposed) return; pause(); advance(); event("step"); refresh(); }
  function reset() { if (!ready || disposed) return; pause(); cursor = cycles = updates = activeMs = 0; intervals = []; paint(); event("reset"); refresh(); }
  function dispose() { pause(); disposed = true; abort.abort(); listeners.forEach(remove => remove()); bytes = null; sequence = []; }
  on($("start"), "click", start); on($("pause"), "click", pause); on($("step"), "click", step); on($("reset"), "click", reset);
  on($("interval"), "change", () => {
    if (!Number.isFinite(interval()) || interval() < 50 || interval() > 10000) $("interval").value = config.interval_ms;
    next = performance.now() + interval(); event("interval", {value:interval()}); refresh();
  });
  on($("top"), "change", resize); on(window, "resize", resize);
  on($("fullscreen"), "click", async () => {
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await stage.requestFullscreen(); fullscreenResult = "ok"; }
    catch (error) { fullscreenResult = `failed:${error.name}`; $("status").textContent = "Полный экран недоступен. Откройте страницу отдельной вкладкой браузера."; }
    resize();
  });
  on(document, "fullscreenchange", resize);
  on(document, "visibilitychange", () => { if (document.hidden) { pause(); event("hidden_pause"); $("status").textContent = "Показ приостановлен: вкладка была скрыта. Нажмите Старт для продолжения."; } });
  on(document, "keydown", key => { if (key.code === "Space" && (document.fullscreenElement === stage || !["BUTTON","INPUT","SELECT","TEXTAREA"].includes(document.activeElement.tagName))) { key.preventDefault(); running ? pause() : start(); } });
  on($("refresh"), "click", refresh);
  on($("download"), "click", () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot(), null, 2)], {type:"application/json"}));
    const link = document.createElement("a"); link.href = url; link.download = "player-report.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  on(window, "pagehide", dispose);
  window.QRTransfer = {snapshot, start, pause, step, reset, dispose};
  function crc32(data) {
    let crc = 0xffffffff;
    for (const value of data) { crc ^= value; for (let i = 0; i < 8; i++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0); }
    return (crc ^ 0xffffffff) >>> 0;
  }
  async function load() {
    try {
      if (config.schema !== 1 || !Number.isInteger(config.descriptor.total) || config.descriptor.total < 1 ||
          !Number.isInteger(config.metadata_every) || config.metadata_every < 1 || config.metadata_every > 1024 ||
          config.interval_ms < 50 || config.interval_ms > 10000 || !Number.isInteger(config.matrix_bytes) || config.matrix_bytes < 12 || config.matrix_bytes > 33554432) throw Error("config");
      const embedded = $("data").textContent.trim();
      if (embedded) {
        if (embedded.length !== Math.ceil(config.matrix_bytes / 3) * 4) throw Error("length");
        const raw = atob(embedded); bytes = Uint8Array.from(raw, c => c.charCodeAt(0)); $("data").remove();
      } else {
        const response = await fetch("frames.bin", {signal:abort.signal, credentials:"same-origin"});
        if (!response.ok) throw Error("fetch");
        const reader = response.body.getReader(); bytes = new Uint8Array(config.matrix_bytes);
        let offset = 0;
        while (true) {
          const {value, done} = await reader.read();
          if (done) break;
          if (offset + value.length > bytes.length) { await reader.cancel(); throw Error("length"); }
          bytes.set(value, offset); offset += value.length;
        }
        if (offset !== bytes.length) throw Error("length");
      }
      if (disposed) return;
      if (bytes.length !== config.matrix_bytes || bytes.length < 12 || crc32(bytes) !== config.matrix_crc32) throw Error("integrity");
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      if (view.getUint32(0) !== 0x51524d31 || view.getUint16(4) !== 177 || view.getUint16(6) !== 4 ||
          view.getUint32(8) !== config.descriptor.total + 1 || bytes.length !== 12 + view.getUint32(8) * stride) throw Error("layout");
      sequence.push(0);
      for (let i = 1; i <= config.descriptor.total; i++) { sequence.push(i); if (i % config.metadata_every === 0) sequence.push(0); }
      ready = true; loadMs = performance.now() - started;
      $("status").textContent = "Архив загружен. Запустите приёмник на хосте, затем нажмите Старт. Space — пауза, Esc — выход из полного экрана.";
      resize(); event("ready"); refresh();
    } catch (error) {
      if (disposed) return;
      $("status").textContent = "Не удалось загрузить кадры. Откройте standalone.html или проверьте наличие полного комплекта файлов.";
      event("load_failed", {error_name:error.name}); refresh();
    }
  }
  resize(); load();
})();
