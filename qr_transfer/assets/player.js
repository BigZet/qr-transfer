(() => {
  "use strict";
  if (window.QRTransfer) window.QRTransfer.dispose();
  const $ = id => document.getElementById(id), stage = $("stage"), canvas = $("qr"), ctx = canvas.getContext("2d");
  const config = JSON.parse($("config").textContent), started = performance.now(), listeners = [], events = [];
  const base = document.createElement("canvas"); base.width = base.height = 185;
  const baseCtx = base.getContext("2d"), pixels = baseCtx.createImageData(185, 185);
  const calibration = config.calibration_matrix ? Uint8Array.from(atob(config.calibration_matrix), c=>c.charCodeAt(0)) : null;
  let layers = 1, calibrating = false, paintMs = 0;
  $("visual").value = config.visual || "mono";
  const abort = new AbortController();
  let bytes = null, sequence = [], cursor = 0, ready = false, running = false, disposed = false;
  let raf = null, next = 0, updates = 0, cycles = 0, activeSince = null, activeMs = 0, lastShown = null;
  let intervals = [], modulePx = 0, loadMs = null, lastReport = 0, fullscreenResult = "not_tested";
  let slots = 1, shown = [[0]], baseSequence = [], nextSlot = 0, slotUpdates = [0,0];
  const gap = 8, stride = 3917, dataFrames = config.data_frames ?? config.descriptor.total;
  $("slots").value = config.slots || 1; $("mode").value = config.update_mode || "sync";
  $("interval").value = config.interval_ms;
  function on(target, name, callback) { target.addEventListener(name, callback); listeners.push(() => target.removeEventListener(name, callback)); }
  function event(type, detail = {}) { events.push({at_ms: performance.now() - started, type, ...detail}); if (events.length > 100) events.shift(); }
  function interval() { return Number($("interval").value); }
  function period() { return interval() / ($("mode").value === "staggered" ? slots : 1); }
  function layoutSchedule() {
    layers = {mono:1, rg4:2, rgb8:3}[$("visual").value];
    baseSequence = [0];
    if (layers > 1) baseSequence.unshift(-1);
    for (let i=1; i<=dataFrames; i++) {
      baseSequence.push(i);
      if (i % config.metadata_every === 0) { baseSequence.push(0); if(layers>1) baseSequence.push(-1); }
    }
    sequence = [];
    // Service QR and transport metadata occupy an entire monochrome tile.
    let group = [];
    for (const id of baseSequence) {
      if(layers>1 && id<=0) {
        if(group.length) { while(group.length<layers) group.push(0); sequence.push(group); group=[]; }
        sequence.push(Array(layers).fill(id));
      } else { group.push(id); if(group.length===layers) {sequence.push(group);group=[];} }
    }
    if(group.length) {while(group.length<layers)group.push(0);sequence.push(group);}
    if(slots===2 && sequence.length%2)sequence.push(Array(layers).fill(0));
    cursor = sequence.length ? cursor % sequence.length : 0;
    shown = Array.from({length:slots}, (_,i) => sequence[(cursor+i)%sequence.length] ?? [0]);
    nextSlot = 0;
  }
  function snapshot() {
    const rect = canvas.getBoundingClientRect(), seconds = (activeMs + (activeSince === null ? 0 : performance.now() - activeSince)) / 1000;
    return {schema:1, profile:$("visual").value, layers, calibrating, paint_ms:paintMs, transport:config.transport || "repeat", evidence_level:"browser_only", ready, running, disposed,
      transfer_id:config.transfer_id, current_packet:shown[0]?.[0] ?? null, slot_packets:shown.map(group=>group[0]), layer_packets:shown.map(group=>group.slice()), slots, requested_slots:Number($("slots").value),
      update_mode:$("mode").value, slot_updates:slotUpdates.slice(0,slots),
      slot_updates_per_second:slotUpdates.slice(0,slots).map(n=>seconds>0?n/seconds:0), cursor, cycle:cycles, updates,
      target_interval_ms:interval(), actual_updates_per_second:seconds > 0 ? updates / seconds : 0,
      active_seconds:seconds, recent_intervals_ms:intervals.slice(), load_ms:loadMs, unique_frames:dataFrames + 1,
      cycle_frames:sequence.length, cycle_seconds:sequence.length * interval() / (1000 * slots),
      visibility:document.visibilityState, fullscreen:fullscreenResult, embedded:window.self !== window.top,
      geometry:{viewport_css:[innerWidth,innerHeight], canvas_backing:[canvas.width,canvas.height],
        canvas_css:[rect.width,rect.height], canvas_origin_css:[rect.x,rect.y], device_pixel_ratio:devicePixelRatio,
        module_backing_px:modulePx, quiet_modules:4, top_inset_css:Number($("top").value),
        applied_top_inset_css:document.fullscreenElement === stage ? Number($("top").value) : 0,
        host_pixels:"not_measured"}, events:events.slice()};
  }
  function refresh() {
    const s = snapshot();
    $("stats").textContent = ready ? `${running ? "Показ" : "Пауза"} · кадр ${cursor + 1}/${sequence.length} · цикл ${cycles + 1} · ${s.actual_updates_per_second.toFixed(2)} обновл./с · ${modulePx} px/модуль · ${slots} QR` : "Ожидание данных";
    $("report").textContent = JSON.stringify(s, null, 2);
    for (const name of ["start", "pause", "step", "reset"]) $(name).disabled = !ready || modulePx < 1;
  }
  function paint() {
    if (!ready || modulePx < 1 || disposed) return;
    ctx.fillStyle = "white"; ctx.fillRect(0,0,canvas.width,canvas.height);
    const began = performance.now();
    for (let slot=0; slot<slots; slot++) {
      pixels.data.fill(255);
      const ids = calibrating ? Array(layers).fill(-1) : shown[slot];
      for (let y=0; y<177; y++) for (let x=0; x<177; x++) {
        const bit=y*177+x, pixel=((y+4)*185+x+4)*4;
        for(let layer=0;layer<layers;layer++) {
          const id=ids[layer], source=id===-1?calibration:bytes;
          const offset=id===-1?0:12+id*stride;
          const value=source && (source[offset+(bit>>3)] & (128>>(bit&7))) ? 0 : 255;
          if(layers===1) pixels.data[pixel]=pixels.data[pixel+1]=pixels.data[pixel+2]=value;
          else { pixels.data[pixel+layer]=value; if(layers===2 && ids.every(v=>v===ids[0]) && ids[0]<=0) pixels.data[pixel+2]=value; }
        }
      }
      baseCtx.putImageData(pixels,0,0); ctx.imageSmoothingEnabled=false;
      const left=slot*(185*modulePx+gap);
      ctx.drawImage(base,left,0,185*modulePx,185*modulePx);
      if(layers>1) for(let i=0;i<(1<<layers);i++) {
        ctx.fillStyle=`rgb(${i&1?0:255},${i&2?0:255},${layers===3 && i&4?0:255})`;
        ctx.fillRect(left+(4+i*22)*modulePx,187*modulePx,18*modulePx,6*modulePx);
      }
    }
    paintMs=performance.now()-began;
  }

  function resize() {
    const rect = stage.getBoundingClientRect(), dpr = devicePixelRatio;
    const inset = document.fullscreenElement === stage ? Math.ceil(Number($("top").value) * dpr) : 0;
    const width = Math.floor(rect.width * dpr), height = Math.floor(rect.height * dpr) - inset;
    const previous = slots; slots = Number($("slots").value);
    const scale = n => Math.max(0, Math.floor(Math.min((width-gap*(n-1))/(n*185), height/(layers>1?195:185))));
    if (slots === 2 && scale(2)<4 && scale(1)>=4) slots=1;
    modulePx = scale(slots);
    if (slots !== previous) { layoutSchedule(); event("layout", {slots}); next=performance.now()+period(); }
    const side = modulePx * 185;
    const totalWidth = side*slots+gap*(slots-1);
    canvas.width = Math.max(1,totalWidth); canvas.height = Math.max(1,(layers>1?195:185)*modulePx);
    canvas.style.width = `${totalWidth/dpr}px`; canvas.style.height = `${canvas.height/dpr}px`;
    canvas.style.left = `${(Math.ceil(rect.left * dpr) + Math.floor((width - totalWidth) / 2)) / dpr - rect.left}px`;
    canvas.style.top = `${(Math.ceil(rect.top * dpr) + inset + Math.floor((height - canvas.height) / 2)) / dpr - rect.top}px`;
    if (modulePx < 1) pause();
    paint(); refresh();
  }
  function advance(countUpdate = false) {
    calibrating = false;
    const staggered = $("mode").value === "staggered" && slots === 2;
    const count = staggered ? 1 : slots;
    const old = cursor;
    if (staggered) {
      shown[nextSlot] = sequence[(cursor+slots)%sequence.length];
      if (countUpdate) slotUpdates[nextSlot]++; nextSlot=(nextSlot+1)%slots;
    }
    cursor=(cursor+count)%sequence.length;
    if (old+count >= sequence.length) cycles++;
    if (!staggered) {
      shown=Array.from({length:slots}, (_,i)=>sequence[(cursor+i)%sequence.length]);
      if (countUpdate) for(let i=0;i<slots;i++) slotUpdates[i]++;
    }
    paint();
  }
  function tick(now) {
    raf = null;
    if (!running || disposed) return;
    if (now >= next) {
      advance(true); updates++;
      if (lastShown !== null) { intervals.push(now - lastShown); if (intervals.length > 120) intervals.shift(); }
      lastShown = now;
      next = performance.now() + period(); // Full hold after rendering; no catch-up burst.
    }
    if (now - lastReport >= 1000) { refresh(); lastReport = now; }
    raf = requestAnimationFrame(tick);
  }
  function start() {
    if (!ready || disposed || running || modulePx < 1 || document.hidden) return;
    calibrating=false; paint();
    running = true; activeSince = performance.now(); lastShown = null; next = activeSince + period();
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
  function reset() { if (!ready || disposed) return; pause(); cursor = cycles = updates = activeMs = 0; intervals = []; slotUpdates=[0,0]; layoutSchedule(); paint(); event("reset"); refresh(); }
  function dispose() { pause(); disposed = true; abort.abort(); listeners.forEach(remove => remove()); bytes = null; sequence = []; }
  on($("start"), "click", start); on($("pause"), "click", pause); on($("step"), "click", step); on($("reset"), "click", reset);
  on($("interval"), "change", () => {
    if (!Number.isFinite(interval()) || interval() < 50 || interval() > 10000) $("interval").value = config.interval_ms;
    next = performance.now() + period(); event("interval", {value:interval()}); refresh();
  });
  on($("visual"), "change", () => { pause(); calibrating=false; layoutSchedule(); resize(); event("visual", {value:$("visual").value}); });
  on($("calibrate"), "click", () => { if(!ready || !calibration) return; pause(); calibrating=true; paint(); refresh(); });
  on($("slots"), "change", resize);
  on($("mode"), "change", () => { layoutSchedule(); paint(); next=performance.now()+period(); event("mode", {value:$("mode").value}); refresh(); });
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
      if (config.schema !== 1 || !Number.isInteger(dataFrames) || dataFrames < 1 ||
          !Number.isInteger(config.metadata_every) || config.metadata_every < 1 || config.metadata_every > 1024 ||
          config.interval_ms < 50 || config.interval_ms > 10000 || !Number.isInteger(config.matrix_bytes) || config.matrix_bytes < 12 || config.matrix_bytes > (config.transport === "lt" ? 134217728 : 33554432)) throw Error("config");
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
          view.getUint32(8) !== dataFrames + 1 || bytes.length !== 12 + view.getUint32(8) * stride) throw Error("layout");
      layoutSchedule();
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
