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
  const paged = !!config.parts && !$("data").textContent.trim();
  const partCache = new Map();
  let metadataMatrix = null, loadingParts = false, partError = false, allocatedPartBytes = 0, stepRequested = false, paintedKey = "";
  const pageStats = {requests:0, loaded_bytes:0, peak_buffer_bytes:0, waits:0};
  function bufferBytes() { return [...partCache.values()].reduce((sum,a)=>sum+a.length,0)+(metadataMatrix?.length||0)+allocatedPartBytes; }
  function trackBuffer() { pageStats.peak_buffer_bytes=Math.max(pageStats.peak_buffer_bytes,bufferBytes()); }
  function hasMatrices(ids) { return !paged || ids.every(id=>id<0 || (id===0 && metadataMatrix) || partCache.has(Math.floor(id/config.parts.frames_per_part))); }
  function matrixBytes(id) {
    if(id===-1) return calibration;
    if(!paged) return bytes.subarray(12+id*stride,12+(id+1)*stride);
    if(id===0 && metadataMatrix) return metadataMatrix;
    const part=partCache.get(Math.floor(id/config.parts.frames_per_part)), offset=(id%config.parts.frames_per_part)*stride;
    return part?.subarray(offset,offset+stride);
  }
  function validateParts() {
    const p=config.parts;
    if(p.schema!==1 || !/^parts-[a-f0-9]{32}$/.test(p.directory) || !Number.isInteger(p.frames_per_part) || p.frames_per_part<8 || p.frames_per_part>1024 ||
       !Array.isArray(p.items) || p.items.length!==Math.ceil((dataFrames+1)/p.frames_per_part) || config.matrix_bytes!==12+(dataFrames+1)*stride) throw Error("config");
    p.items.forEach((item,i)=>{
      const expected=Math.min(p.frames_per_part,dataFrames+1-i*p.frames_per_part)*stride;
      if(item.bytes!==expected || !Number.isInteger(item.crc32) || item.crc32<0 || item.crc32>0xffffffff) throw Error("config");
    });
  }
  async function loadPart(index) {
    if(partCache.has(index)) return;
    const item=config.parts.items[index], controller=new AbortController();
    const cancel=()=>controller.abort(); abort.signal.addEventListener("abort",cancel,{once:true});
    if(disposed) {abort.signal.removeEventListener("abort",cancel);return;}
    let timer=null;
    const arm=()=>{clearTimeout(timer);timer=setTimeout(cancel,60000);};
    try {
      arm();pageStats.requests++;
      $("status").textContent=`Загрузка порции ${index+1}/${config.parts.items.length}…`;
      const response=await fetch(`${config.parts.directory}/${String(index).padStart(6,"0")}.bin`,{signal:controller.signal,credentials:"same-origin",cache:"no-store"});
      if(!response.ok) throw Error(`HTTP ${response.status}`);
      const data=new Uint8Array(item.bytes), reader=response.body.getReader();
      allocatedPartBytes=data.length;trackBuffer();
      let offset=0;
      while(true) {
        const {value,done}=await reader.read();if(done)break;
        if(offset+value.length>data.length){await reader.cancel();throw Error("length");}
        data.set(value,offset);offset+=value.length;arm();
      }
      if(offset!==data.length)throw Error("length");
      if(crc32(data)!==item.crc32)throw Error("integrity");
      if(disposed)return;
      allocatedPartBytes=0;partCache.set(index,data);pageStats.loaded_bytes+=data.length;
      if(index===0)metadataMatrix=data.slice(0,stride);
      trackBuffer();
      const pinned=new Set(shown.flat().filter(id=>id>0).map(id=>Math.floor(id/config.parts.frames_per_part)));
      while(partCache.size>4) {
        const victim=[...partCache.keys()].find(i=>i!==index && !pinned.has(i));
        if(victim===undefined)throw Error("buffer");
        partCache.delete(victim);
      }
    } finally {clearTimeout(timer);allocatedPartBytes=0;abort.signal.removeEventListener("abort",cancel);}
  }
  async function ensureParts(ids) {
    for(const index of new Set(ids.filter(id=>id>=0 && !(id===0 && metadataMatrix)).map(id=>Math.floor(id/config.parts.frames_per_part)))) {
      if(disposed)return;await loadPart(index);
    }
  }
  function requestParts(ids) {
    if(!paged || loadingParts || partError || disposed || hasMatrices(ids))return;
    loadingParts=true;pageStats.waits++;
    ensureParts(ids).then(()=>{
      loadingParts=false;if(disposed)return;
      $("status").textContent=running?"Показ идёт. Порция загружена.":"Порция загружена. Старт — продолжить.";
      // Hold a newly available frame for the complete requested period.
      if(!hasMatrices(shown.flat())) {paint();return;}
      if(stepRequested)stepRequested=!advance();
      paint();refresh();
    }).catch(error=>{
      loadingParts=false;if(disposed)return;
      pause();partError=true;
      $("status").textContent=`Не удалось загрузить порцию: ${error.name==="AbortError"?"нет данных 60 секунд":error.message}. Проверьте папку порций рядом с HTML. Старт — повторить.`;
      event("part_failed",{error_name:error.name});refresh();
    });
  }
  function prefetch() {
    if(!paged || !ready || !sequence.length)return;
    const ids=[];
    for(let i=0;i<slots;i++)ids.push(...sequence[(cursor+slots+i)%sequence.length]);
    requestParts(ids);
  }
  function duration(seconds) {
    const n=Math.max(0,Math.ceil(seconds));
    return n>=3600?`${Math.floor(n/3600)} ч ${Math.floor(n%3600/60)} мин`:(n>=60?`${Math.floor(n/60)} мин ${n%60} с`:`${n} с`);
  }
  $("slots").value = config.slots || 1; $("mode").value = config.update_mode || "sync";
  $("interval").value = config.interval_ms;
  function on(target, name, callback) { target.addEventListener(name, callback); listeners.push(() => target.removeEventListener(name, callback)); }
  function event(type, detail = {}) { events.push({at_ms: performance.now() - started, type, ...detail}); if (events.length > 100) events.shift(); }
  function interval() { return Number($("interval").value); }
  function period() { return interval() / ($("mode").value === "staggered" ? slots : 1); }
  function layoutSchedule() {
    intervals=[];lastShown=null;
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
      estimated_cycle_seconds:sequence.length * Math.max(period(), intervals.length>=3?intervals.reduce((a,b)=>a+b,0)/intervals.length:period()) / (1000 * ($("mode").value==="staggered"?1:slots)),
      remaining_cycle_seconds:(sequence.length-cursor) * Math.max(period(), intervals.length>=3?intervals.reduce((a,b)=>a+b,0)/intervals.length:period()) / (1000 * ($("mode").value==="staggered"?1:slots)),
      paging:{enabled:paged, loading:loadingParts, cached_parts:partCache.size, buffer_bytes:bufferBytes(), ...pageStats},
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
    $("eta").textContent = ready ? `Ориентир передачи: ${duration(s.estimated_cycle_seconds)} на полный цикл · до конца цикла ≈ ${duration(s.remaining_cycle_seconds)}${running?"":" (без времени паузы)"}. При потерях нужны повторы; LT может завершиться раньше.` : "Оценка времени появится после загрузки первой порции.";
    $("report").textContent = JSON.stringify(s, null, 2);
    for (const name of ["start", "pause", "step", "reset"]) $(name).disabled = !ready || modulePx < 1;
  }
  function paint() {
    if (!ready || modulePx < 1 || disposed) return;
    ctx.fillStyle = "white"; ctx.fillRect(0,0,canvas.width,canvas.height);
    if(!hasMatrices((calibrating?[-1]:shown.flat()))) {requestParts(shown.flat());return;}
    const began = performance.now();
    for (let slot=0; slot<slots; slot++) {
      pixels.data.fill(255);
      const ids = calibrating ? Array(layers).fill(-1) : shown[slot];
      const planes=ids.map(matrixBytes);
      for (let y=0; y<177; y++) for (let x=0; x<177; x++) {
        const bit=y*177+x, pixel=((y+4)*185+x+4)*4;
        for(let layer=0;layer<layers;layer++) {
          const source=planes[layer];
          const offset=0;
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
    const key=JSON.stringify(calibrating?[-1]:shown);
    if(key!==paintedKey){paintedKey=key;next=performance.now()+period();}
    paintMs=performance.now()-began;
    prefetch();
  }

  function resize() {
    const rect = stage.getBoundingClientRect(), dpr = devicePixelRatio;
    const inset = document.fullscreenElement === stage ? Math.ceil(Number($("top").value) * dpr) : 0;
    const width = Math.floor(rect.width * dpr), height = Math.max(0, Math.floor(rect.height * dpr) - inset - Math.ceil(36*dpr));
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
    const old = cursor, nextCursor=(cursor+count)%sequence.length;
    const candidate=staggered?shown.slice():Array.from({length:slots},(_,i)=>sequence[(nextCursor+i)%sequence.length]);
    if(staggered)candidate[nextSlot]=sequence[(cursor+slots)%sequence.length];
    if(!hasMatrices(candidate.flat())) {requestParts(candidate.flat());return false;}
    shown=candidate;cursor=nextCursor;
    if(staggered) {if(countUpdate)slotUpdates[nextSlot]++;nextSlot=(nextSlot+1)%slots;}
    else if(countUpdate)for(let i=0;i<slots;i++)slotUpdates[i]++;
    if(old+count>=sequence.length)cycles++;
    paint();return true;
  }
  function tick(now) {
    raf = null;
    if (!running || disposed) return;
    if (now >= next && hasMatrices(shown.flat()) && advance(true)) {
      updates++;
      if (lastShown !== null) { intervals.push(now - lastShown); if (intervals.length > 120) intervals.shift(); }
      lastShown = now;
      next = performance.now() + period(); // Full hold after rendering; no catch-up burst.
    }
    if (now - lastReport >= 1000) { refresh(); lastReport = now; }
    raf = requestAnimationFrame(tick);
  }
  function start() {
    if (!ready || disposed || running || modulePx < 1 || document.hidden) return;
    stepRequested=false;partError=false;calibrating=false; paint();
    running = true; activeSince = performance.now(); lastShown = null; next = activeSince + period();
    $("status").textContent = modulePx < 4 ? "Показ идёт. QR мелкий: увеличьте область или включите полный экран." : "Показ идёт по кругу. Остановите его после завершения приёма на хосте.";
    event("start"); raf = requestAnimationFrame(tick); refresh();
  }
  function pause() {
    stepRequested=false;
    if (activeSince !== null) activeMs += performance.now() - activeSince;
    activeSince = null; running = false;
    if (raf !== null) cancelAnimationFrame(raf);
    raf = null; refresh();
    if (ready && !disposed) $("status").textContent = "Пауза. Последний кадр сохранён; Старт — продолжить.";
  }
  function step() { if (!ready || disposed) return; pause(); stepRequested=!advance(); event("step"); refresh(); }
  function reset() { if (!ready || disposed) return; pause(); cursor = cycles = updates = activeMs = 0; intervals = []; slotUpdates=[0,0]; layoutSchedule(); paint(); event("reset"); refresh(); }
  function dispose() { pause(); disposed = true; abort.abort(); listeners.forEach(remove => remove()); bytes = null; sequence = []; partCache.clear();metadataMatrix=null; }
  on($("start"), "click", start); on($("pause"), "click", pause); on($("step"), "click", step); on($("reset"), "click", reset);
  on($("interval"), "change", () => {
    if (!Number.isFinite(interval()) || interval() < 50 || interval() > 10000) $("interval").value = config.interval_ms;
    intervals=[];lastShown=null;
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
    let timeout = null;
    try {
      $("status").textContent = "JavaScript запущен. Подготовка загрузки кадров…";
      if (config.schema !== 1 || !Number.isInteger(dataFrames) || dataFrames < 1 ||
          !Number.isInteger(config.metadata_every) || config.metadata_every < 1 || config.metadata_every > 1024 ||
          config.interval_ms < 50 || config.interval_ms > 10000 || !Number.isInteger(config.matrix_bytes) || config.matrix_bytes < 12 || config.matrix_bytes > (paged ? 268435456 : (config.transport === "lt" ? 134217728 : 33554432))) throw Error("config");
      const embedded = $("data").textContent.trim();
      if (paged) {
        validateParts();layoutSchedule();
        await ensureParts([0,...shown.flat()]);
      } else if (embedded) {
        if (embedded.length !== Math.ceil(config.matrix_bytes / 3) * 4) throw Error("length");
        const raw = atob(embedded); bytes = Uint8Array.from(raw, c => c.charCodeAt(0)); $("data").remove();
      } else {
        $("status").textContent = "Загрузка frames.bin…";
        const armTimeout = () => { clearTimeout(timeout); timeout = setTimeout(() => abort.abort(), 60000); };
        armTimeout();
        const response = await fetch("frames.bin", {signal:abort.signal, credentials:"same-origin"});
        if (!response.ok) throw Error(`HTTP ${response.status}`);
        const reader = response.body.getReader(); bytes = new Uint8Array(config.matrix_bytes);
        let offset = 0;
        while (true) {
          const {value, done} = await reader.read();
          if (done) break;
          if (offset + value.length > bytes.length) { await reader.cancel(); throw Error("length"); }
          bytes.set(value, offset); offset += value.length;
          armTimeout();
          $("status").textContent = `Загрузка frames.bin: ${(offset/1048576).toFixed(1)} / ${(bytes.length/1048576).toFixed(1)} МиБ`;
        }
        if (offset !== bytes.length) throw Error("length");
      }
      clearTimeout(timeout);
      if (disposed) return;
      $("status").textContent = "Кадры загружены. Проверка целостности…";
      await new Promise(resolve => requestAnimationFrame(() => setTimeout(resolve, 0)));
      if (disposed) return;
      if (!paged) {
      if (bytes.length !== config.matrix_bytes || bytes.length < 12 || crc32(bytes) !== config.matrix_crc32) throw Error("integrity");
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      if (view.getUint32(0) !== 0x51524d31 || view.getUint16(4) !== 177 || view.getUint16(6) !== 4 ||
          view.getUint32(8) !== dataFrames + 1 || bytes.length !== 12 + view.getUint32(8) * stride) throw Error("layout");
      }
      layoutSchedule();
      ready = true; loadMs = performance.now() - started;
      $("status").textContent = (paged ? "Первая порция загружена. Остальные загрузятся по мере показа. " : "Архив загружен. ") + "Запустите приёмник на хосте, затем нажмите Старт. Space — пауза, Esc — выход из полного экрана.";
      resize(); event("ready"); refresh();
    } catch (error) {
      if (disposed) return;
      const reason = error.name === "AbortError" ? "Нет данных в течение 60 секунд" :
        (/^HTTP \d+$/.test(error.message) ? error.message : ({length:"Неверный размер файла матриц", integrity:"Не совпала контрольная сумма", layout:"Неверный формат матриц", config:"Некорректные параметры плеера"}[error.message] || "Браузер не разрешил загрузку или произошла сетевая ошибка"));
      $("status").textContent = `Не удалось загрузить кадры: ${reason}. Откройте index.html через /files/ отдельной вкладкой; Файлы матриц и папка порций должны лежать рядом с HTML.`;
      event("load_failed", {error_name:error.name}); refresh();
    } finally { clearTimeout(timeout); }
  }
  resize(); load();
})();
