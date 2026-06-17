const DATA_URL = "data/app_data.json";

const state = {
  data: null,
  mode: "vessels",
  signalMode: "strongest",
  strongestLimit: 80,
  selectedType: "vessel",
  selectedId: null,
  query: "",
  leafletMap: null,
  leafletLayers: null,
  mapNeedsFit: true
};

const svg = document.getElementById("mapSvg");
const leafletMapElement = document.getElementById("leafletMap");
const entityList = document.getElementById("entityList");
const detailPanel = document.getElementById("detailPanel");
const stats = document.getElementById("stats");
const sourceLine = document.getElementById("sourceLine");
const searchInput = document.getElementById("searchInput");

function fmt(value, digits = 1, suffix = "") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${Number(value).toFixed(digits)}${suffix}`;
}

function clean(value) {
  return value || "-";
}

function project(lat, lon) {
  const { minLat, maxLat, minLon, maxLon } = state.data.bounds;
  const width = 1000;
  const height = 1000;
  const x = ((lon - minLon) / (maxLon - minLon)) * width;
  const y = height - ((lat - minLat) / (maxLat - minLat)) * height;
  return { x, y };
}

function vesselRadius(vessel) {
  const length = Number(vessel.maxLength || 0);
  return Math.max(5, Math.min(14, 5 + length / 28));
}

function ctdRadius(event) {
  const depth = Number(event.depthMaxM || 0);
  return Math.max(5, Math.min(11, 5 + depth / 6));
}

function finiteNumber(value, fallback = null) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function acousticScore(vessel) {
  const audio = vessel.audio || {};
  const rms = finiteNumber(audio.rmsDbfsMean, -120);
  const peak = finiteNumber(audio.peakDbfs, -120);
  const distance = finiteNumber(audio.sourceDistanceKm, finiteNumber(vessel.closestDistanceKm, 30));
  const coverage = finiteNumber(audio.coverageSeconds, 0);
  const distancePenalty = Math.log2(Math.max(1, distance + 1)) * 2.5;
  return rms + Math.max(-12, peak) * 0.08 + Math.min(coverage, 60) * 0.03 - distancePenalty;
}

function strongestVessels(vessels) {
  return vessels
    .slice()
    .sort((a, b) => acousticScore(b) - acousticScore(a))
    .slice(0, state.strongestLimit);
}

function vesselLatLng(vessel) {
  return [Number(vessel.closestLatitude), Number(vessel.closestLongitude)];
}

function ctdLatLng(event) {
  return [Number(event.latitude), Number(event.longitude)];
}

function setSelected(type, id) {
  state.selectedType = type;
  state.selectedId = id;
  render();
}

function modeAllows(type) {
  return state.mode === "all" || state.mode === type;
}

function matchesQuery(item, fields) {
  if (!state.query) return true;
  const haystack = fields.map((field) => String(item[field] || "")).join(" ").toLowerCase();
  return haystack.includes(state.query);
}

function filteredVessels() {
  const vessels = state.data.vessels.filter((vessel) => modeAllows("vessels") && matchesQuery(vessel, ["id", "name", "shipType"]));
  if (state.signalMode === "strongest") {
    return strongestVessels(vessels);
  }
  return vessels.slice().sort((a, b) => acousticScore(b) - acousticScore(a));
}

function filteredCtd() {
  return state.data.ctdEvents.filter((event) => modeAllows("ctd") && matchesQuery(event, ["station", "fileName", "startUtc"]));
}

function renderStats() {
  const metadata = state.data.metadata;
  stats.innerHTML = [
    ["Vessels", metadata.vesselCount],
    ["CTD", metadata.ctdCount],
    ["Event audio", metadata.eventAudioProfileCount || metadata.audioProfileCount]
  ].map(([label, value]) => `<div class="stat"><b>${value}</b><span>${label}</span></div>`).join("");
  const source = metadata.sources.aisCsv || "no AIS source";
  const signalText = state.signalMode === "strongest" ? `showing strongest ${filteredVessels().length}` : "showing all vessel signals";
  sourceLine.textContent = `${source} - ${signalText}`;
}

function renderList() {
  const vessels = filteredVessels();
  const ctdEvents = filteredCtd();
  const rows = [];

  for (const vessel of vessels) {
    rows.push(`
      <button class="entity-item vessel ${state.selectedType === "vessel" && state.selectedId === vessel.id ? "is-selected" : ""}" data-type="vessel" data-id="${vessel.id}">
        <span>
          <span class="entity-name">${clean(vessel.name)}</span>
          <span class="entity-meta">${vessel.id} - ${clean(vessel.shipType)} - ${fmt(vessel.audio?.rmsDbfsMean, 1, " dBFS")}</span>
        </span>
        <span class="distance-pill">${fmt(vessel.closestDistanceKm, 2, " km")}</span>
      </button>
    `);
  }

  for (const event of ctdEvents) {
    rows.push(`
      <button class="entity-item ctd ${state.selectedType === "ctd" && state.selectedId === String(event.id) ? "is-selected" : ""}" data-type="ctd" data-id="${event.id}">
        <span>
          <span class="entity-name">CTD ${clean(event.station || event.id)}</span>
          <span class="entity-meta">${clean(event.startUtc)} - ${event.recordedByHydrophone ? "recorded" : "outside audio"}</span>
        </span>
        <span class="distance-pill">${fmt(event.distanceToHydrophoneKm, 2, " km")}</span>
      </button>
    `);
  }

  entityList.innerHTML = rows.join("") || `<div class="empty">No matching rows</div>`;
  entityList.querySelectorAll(".entity-item").forEach((button) => {
    button.addEventListener("click", () => setSelected(button.dataset.type, button.dataset.id));
  });
}

function svgElement(name, attrs = {}) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attrs)) {
    element.setAttribute(key, value);
  }
  return element;
}

function pathFromGeo(points) {
  return points.map(([lat, lon]) => {
    const p = project(lat, lon);
    return `${p.x.toFixed(2)},${p.y.toFixed(2)}`;
  }).join(" ");
}

function addSvgText(text, attrs = {}) {
  const element = svgElement("text", attrs);
  element.textContent = text;
  svg.appendChild(element);
  return element;
}

function renderBasemap() {
  const b = state.data.bounds;
  svg.appendChild(svgElement("rect", { x: 0, y: 0, width: 1000, height: 1000, class: "water" }));
  const latSpan = b.maxLat - b.minLat;
  const lonSpan = b.maxLon - b.minLon;
  addSvgText("Real map tiles unavailable", { class: "place-label", x: 36, y: 58 });

  const scaleLat = b.minLat + latSpan * 0.08;
  const scaleLon = b.minLon + lonSpan * 0.10;
  const kmPerLonDegree = 111.32 * Math.cos((state.data.hydrophone.latitude * Math.PI) / 180);
  const scaleStart = project(scaleLat, scaleLon);
  const scaleEnd = project(scaleLat, scaleLon + 5 / kmPerLonDegree);
  svg.appendChild(svgElement("line", { class: "scale-bar", x1: scaleStart.x, y1: scaleStart.y, x2: scaleEnd.x, y2: scaleEnd.y }));
  addSvgText("5 km", { class: "scale-label", x: scaleStart.x, y: scaleStart.y - 8 });

  addSvgText("N", { class: "north-label", x: 960, y: 52 });
  svg.appendChild(svgElement("line", { class: "north-arrow", x1: 960, y1: 84, x2: 960, y2: 56 }));
  svg.appendChild(svgElement("path", { class: "north-arrow", d: "M960 52 L951 68 L969 68 Z" }));
}

function initializeLeafletMap() {
  if (!window.L || !leafletMapElement) return false;
  if (state.leafletMap) return true;

  svg.style.display = "none";
  leafletMapElement.style.display = "block";
  state.leafletMap = L.map(leafletMapElement, {
    preferCanvas: true,
    zoomControl: true,
    attributionControl: true
  });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(state.leafletMap);
  state.leafletLayers = L.layerGroup().addTo(state.leafletMap);
  return true;
}

function leafletStyle(kind, selected = false, muted = false) {
  const colors = {
    vessel: "#147d8c",
    ctd: muted ? "#8d99a8" : "#8060b2",
    hydrophone: "#c53b2c"
  };
  return {
    color: selected ? "#d59c22" : "#ffffff",
    fillColor: colors[kind],
    fillOpacity: kind === "hydrophone" ? 0.95 : 0.88,
    opacity: 1,
    radius: kind === "hydrophone" ? 9 : undefined,
    weight: selected ? 4 : 2
  };
}

function fitLeafletMap(vessels, ctdEvents) {
  if (!state.mapNeedsFit || !state.leafletMap) return;
  const points = [
    [state.data.hydrophone.latitude, state.data.hydrophone.longitude],
    ...vessels.map(vesselLatLng),
    ...ctdEvents.map(ctdLatLng)
  ].filter(([lat, lon]) => Number.isFinite(lat) && Number.isFinite(lon));
  if (!points.length) return;
  state.leafletMap.fitBounds(L.latLngBounds(points), { padding: [32, 32], maxZoom: 14 });
  state.mapNeedsFit = false;
}

function renderLeafletMap() {
  if (!initializeLeafletMap()) return false;
  const vessels = filteredVessels();
  const ctdEvents = filteredCtd();
  state.leafletLayers.clearLayers();

  const selected = selectedVessel();
  if (state.selectedType === "vessel" && selected && modeAllows("vessels")) {
    const selectedTrack = selected.track
      .map((point) => [Number(point.latitude), Number(point.longitude)])
      .filter(([lat, lon]) => Number.isFinite(lat) && Number.isFinite(lon));
    if (selectedTrack.length > 1) {
      L.polyline(selectedTrack, {
        color: "#0b7180",
        opacity: 0.9,
        weight: 4,
        lineCap: "round",
        lineJoin: "round"
      }).addTo(state.leafletLayers);
    }
  }

  if (modeAllows("vessels")) {
    for (const vessel of vessels) {
      const [lat, lon] = vesselLatLng(vessel);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const selectedMarker = state.selectedType === "vessel" && state.selectedId === vessel.id;
      const marker = L.circleMarker([lat, lon], {
        ...leafletStyle("vessel", selectedMarker),
        radius: vesselRadius(vessel)
      }).addTo(state.leafletLayers);
      marker.bindTooltip(`${vessel.name} ${fmt(vessel.audio?.rmsDbfsMean, 1, " dBFS")}`);
      marker.on("click", () => setSelected("vessel", vessel.id));
    }
  }

  if (modeAllows("ctd")) {
    for (const event of ctdEvents) {
      const [lat, lon] = ctdLatLng(event);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const selectedMarker = state.selectedType === "ctd" && state.selectedId === String(event.id);
      const marker = L.circleMarker([lat, lon], {
        ...leafletStyle("ctd", selectedMarker, !event.recordedByHydrophone),
        radius: ctdRadius(event)
      }).addTo(state.leafletLayers);
      marker.bindTooltip(`CTD ${event.station || event.id}`);
      marker.on("click", () => setSelected("ctd", String(event.id)));
    }
  }

  const hydro = state.data.hydrophone;
  L.circleMarker([hydro.latitude, hydro.longitude], leafletStyle("hydrophone", true))
    .bindTooltip(hydro.label)
    .addTo(state.leafletLayers);

  fitLeafletMap(vessels, ctdEvents);
  return true;
}

function renderFallbackMap() {
  if (leafletMapElement) leafletMapElement.style.display = "none";
  svg.style.display = "block";
  svg.innerHTML = "";
  svg.setAttribute("viewBox", "0 0 1000 1000");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  renderBasemap();

  if (state.selectedType === "vessel" && modeAllows("vessels")) {
    const vessel = selectedVessel();
    if (vessel && vessel.track.length) {
      const points = vessel.track
        .map((point) => {
          const p = project(point.latitude, point.longitude);
          return `${p.x.toFixed(2)},${p.y.toFixed(2)}`;
        })
        .join(" ");
      const track = svgElement("polyline", {
        class: "track is-selected",
        points
      });
      svg.appendChild(track);
    }
  }

  if (modeAllows("vessels")) {
    for (const vessel of filteredVessels()) {
      const p = project(vessel.closestLatitude, vessel.closestLongitude);
      const marker = svgElement("circle", {
        class: `marker vessel ${state.selectedType === "vessel" && state.selectedId === vessel.id ? "is-selected" : ""}`,
        cx: p.x,
        cy: p.y,
        r: vesselRadius(vessel),
        tabindex: 0
      });
      marker.addEventListener("click", () => setSelected("vessel", vessel.id));
      marker.appendChild(svgElement("title"));
      marker.querySelector("title").textContent = `${vessel.name} ${fmt(vessel.closestDistanceKm, 2, " km")}`;
      svg.appendChild(marker);
    }
  }

  if (modeAllows("ctd")) {
    for (const event of filteredCtd()) {
      const p = project(event.latitude, event.longitude);
      const marker = svgElement("circle", {
        class: `marker ctd ${event.recordedByHydrophone ? "" : "not-recorded"} ${state.selectedType === "ctd" && state.selectedId === String(event.id) ? "is-selected" : ""}`,
        cx: p.x,
        cy: p.y,
        r: ctdRadius(event),
        tabindex: 0
      });
      marker.addEventListener("click", () => setSelected("ctd", String(event.id)));
      marker.appendChild(svgElement("title"));
      marker.querySelector("title").textContent = `CTD ${event.station || event.id}`;
      svg.appendChild(marker);
    }
  }

  const hydro = state.data.hydrophone;
  const hp = project(hydro.latitude, hydro.longitude);
  const marker = svgElement("circle", {
    class: "marker hydrophone",
    cx: hp.x,
    cy: hp.y,
    r: 12
  });
  marker.appendChild(svgElement("title"));
  marker.querySelector("title").textContent = hydro.label;
  svg.appendChild(marker);
}

function renderMap() {
  if (renderLeafletMap()) return;
  renderFallbackMap();
}

function selectedVessel() {
  return state.data.vessels.find((vessel) => vessel.id === state.selectedId) || state.data.vessels[0];
}

function selectedCtd() {
  return state.data.ctdEvents.find((event) => String(event.id) === String(state.selectedId)) || state.data.ctdEvents[0];
}

function durationSeconds(startUtc, endUtc) {
  const start = Date.parse(startUtc || "");
  const end = Date.parse(endUtc || "");
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return (end - start) / 1000;
}

function audioWaveformHtml(audio) {
  const waveform = audio.waveform || {};
  const times = Array.isArray(waveform.timesSeconds) ? waveform.timesSeconds.map(Number) : [];
  const rms = Array.isArray(waveform.rmsDbfs) ? waveform.rmsDbfs.map(Number) : [];
  const peak = Array.isArray(waveform.peakDbfs) ? waveform.peakDbfs.map(Number) : [];
  const rows = times.map((time, index) => ({
    time,
    rms: rms[index],
    peak: peak[index]
  })).filter((row) => Number.isFinite(row.time) && Number.isFinite(row.rms));
  if (rows.length < 2) return "";

  const width = 320;
  const height = 118;
  const padX = 12;
  const padY = 14;
  const duration = durationSeconds(audio.startUtc, audio.endUtc) || Math.max(...rows.map((row) => row.time));
  const eventOffset = Number(audio.eventOffsetSeconds);
  const markerTime = Number.isFinite(eventOffset) ? eventOffset : duration / 2;
  const values = rows.flatMap((row) => Number.isFinite(row.peak) ? [row.rms, row.peak] : [row.rms]);
  const minDb = Math.floor(Math.min(...values) / 5) * 5;
  const maxDb = Math.ceil(Math.max(...values) / 5) * 5;
  const span = Math.max(1, maxDb - minDb);
  const xForTime = (time) => padX + (Math.max(0, Math.min(duration, time)) / duration) * (width - padX * 2);
  const yForDb = (db) => padY + (1 - ((db - minDb) / span)) * (height - padY * 2);
  const rmsPoints = rows.map((row) => `${xForTime(row.time).toFixed(1)},${yForDb(row.rms).toFixed(1)}`).join(" ");
  const peakPoints = rows
    .filter((row) => Number.isFinite(row.peak))
    .map((row) => `${xForTime(row.time).toFixed(1)},${yForDb(row.peak).toFixed(1)}`)
    .join(" ");
  const markerX = xForTime(markerTime).toFixed(1);
  const loudest = rows.reduce((best, row) => (row.rms > best.rms ? row : best), rows[0]);

  return `
    <div class="waveform-card">
      <svg class="waveform-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Audio envelope">
        <line class="waveform-grid" x1="${padX}" y1="${padY}" x2="${width - padX}" y2="${padY}"></line>
        <line class="waveform-grid" x1="${padX}" y1="${height - padY}" x2="${width - padX}" y2="${height - padY}"></line>
        <polyline class="waveform-peak" points="${peakPoints}"></polyline>
        <polyline class="waveform-rms" points="${rmsPoints}"></polyline>
        <line class="waveform-arrival" x1="${markerX}" y1="${padY}" x2="${markerX}" y2="${height - padY}"></line>
        <text class="waveform-label" x="${padX}" y="11">${maxDb} dBFS</text>
        <text class="waveform-label" x="${padX}" y="${height - 3}">${minDb} dBFS</text>
        <text class="waveform-arrival-label" x="${Math.min(width - 64, Number(markerX) + 4).toFixed(1)}" y="28">Arrival</text>
      </svg>
      <div class="waveform-meta">
        <span>Window ${fmt(duration, 1, " s")}</span>
        <span>Loudest ${fmt(loudest.time, 1, " s")} / ${fmt(loudest.rms, 1, " dBFS")}</span>
      </div>
    </div>
  `;
}

function audioProfileHtml(audio) {
  if (!audio) {
    return `<p>No audio profile linked yet</p>`;
  }
  const sourceDistance = audio.sourceDistanceKm === null || audio.sourceDistanceKm === undefined
    ? null
    : Number(audio.sourceDistanceKm);
  const delay = audio.propagationDelaySeconds === null || audio.propagationDelaySeconds === undefined
    ? null
    : Number(audio.propagationDelaySeconds);
  const timingRows = [
    audio.sourceTimeUtc ? `Source time: ${clean(audio.sourceTimeUtc)}` : "",
    audio.eventTimeUtc ? `Hydrophone arrival: ${clean(audio.eventTimeUtc)}` : "",
    Number.isFinite(sourceDistance) ? `Source distance: ${fmt(sourceDistance, 2, " km")}` : "",
    Number.isFinite(delay) ? `Sound delay: ${fmt(delay, 1, " s")}` : ""
  ].filter(Boolean).join("<br>");
  if (audio.captured === false) {
    return `
      <div class="timeline">${clean(audio.captureNote)}<br>${timingRows}<br>${clean(audio.startUtc)} to ${clean(audio.endUtc)}</div>
    `;
  }
  const bands = Object.entries(audio.bands || {});
  const values = bands.map(([, value]) => Number(value)).filter((value) => !Number.isNaN(value));
  const min = values.length ? Math.min(...values) : -80;
  const max = values.length ? Math.max(...values) : 0;
  const span = Math.max(1, max - min);
  const bandRows = bands.map(([label, value]) => {
    const width = value === null || value === undefined ? 0 : ((Number(value) - min) / span) * 100;
    return `
      <div class="profile-row">
        <span>${label}</span>
        <span class="bar-shell"><span class="bar-fill" style="width:${Math.max(5, width).toFixed(1)}%"></span></span>
        <span>${fmt(value, 1)}</span>
      </div>
    `;
  }).join("");
  return `
    <div class="detail-grid">
      <div class="metric"><span>RMS</span><b>${fmt(audio.rmsDbfsMean, 1, " dBFS")}</b></div>
      <div class="metric"><span>Peak</span><b>${fmt(audio.peakDbfs, 1, " dBFS")}</b></div>
      <div class="metric"><span>Crest</span><b>${fmt(audio.crestFactorDb, 1, " dB")}</b></div>
      <div class="metric"><span>Coverage</span><b>${fmt(audio.coverageSeconds, 1, " s")}</b></div>
    </div>
    ${audioWaveformHtml(audio)}
    ${bandRows}
    <div class="timeline">${clean(audio.captureNote)}<br>${timingRows}<br>${clean(audio.fileName)}<br>${clean(audio.startUtc)} to ${clean(audio.endUtc)}</div>
  `;
}

function renderVesselDetail(vessel) {
  detailPanel.innerHTML = `
    <div class="detail-kicker">Vessel</div>
    <h2>${clean(vessel.name)}</h2>
    <p>${vessel.id} - ${clean(vessel.shipType)} - ${clean(vessel.typeOfMobile)}</p>
    <div class="detail-grid">
      <div class="metric"><span>Closest point</span><b>${fmt(vessel.closestDistanceKm, 2, " km")}</b></div>
      <div class="metric"><span>Bearing</span><b>${fmt(vessel.closestBearingDeg, 0, " deg")}</b></div>
      <div class="metric"><span>Max speed</span><b>${fmt(vessel.maxSog, 1, " kn")}</b></div>
      <div class="metric"><span>Length</span><b>${fmt(vessel.maxLength, 0, " m")}</b></div>
      <div class="metric"><span>Draught</span><b>${fmt(vessel.maxDraught, 1, " m")}</b></div>
      <div class="metric"><span>AIS rows</span><b>${vessel.rowCount}</b></div>
    </div>
    <h3 class="section-title">Acoustic profile</h3>
    ${audioProfileHtml(vessel.audio)}
    <h3 class="section-title">AIS track</h3>
    <div class="timeline">${clean(vessel.firstTimestamp)}<br>${clean(vessel.lastTimestamp)}</div>
  `;
}

function renderCtdDetail(event) {
  detailPanel.innerHTML = `
    <div class="detail-kicker">CTD</div>
    <h2>CTD ${clean(event.station || event.id)}</h2>
    <p>${clean(event.fileName)}</p>
    <div class="detail-grid">
      <div class="metric"><span>Hydrophone distance</span><b>${fmt(event.distanceToHydrophoneKm, 2, " km")}</b></div>
      <div class="metric"><span>Audio overlap</span><b>${event.recordedByHydrophone ? "Yes" : "No"}</b></div>
      <div class="metric"><span>Depth</span><b>${fmt(event.depthMinM, 1)}-${fmt(event.depthMaxM, 1, " m")}</b></div>
      <div class="metric"><span>Temperature</span><b>${fmt(event.temperatureMinC, 1)}-${fmt(event.temperatureMaxC, 1, " C")}</b></div>
      <div class="metric"><span>Salinity</span><b>${fmt(event.salinityMinPsu, 1)}-${fmt(event.salinityMaxPsu, 1, " PSU")}</b></div>
    </div>
    <h3 class="section-title">Cast window</h3>
    <div class="timeline">${clean(event.startUtc)}<br>${clean(event.endUtc)}</div>
    <h3 class="section-title">Acoustic profile</h3>
    ${audioProfileHtml(event.audio)}
  `;
}

function renderDetail() {
  if (!state.selectedId) {
    detailPanel.innerHTML = `<div class="empty">No matching rows</div>`;
    return;
  }
  if (state.selectedType === "ctd") {
    const event = selectedCtd();
    if (event) renderCtdDetail(event);
    return;
  }
  const vessel = selectedVessel();
  if (vessel) renderVesselDetail(vessel);
}

function ensureSelection() {
  const vessels = filteredVessels();
  const ctdEvents = filteredCtd();
  const selectedVesselVisible = state.selectedType === "vessel" && vessels.some((vessel) => vessel.id === state.selectedId);
  const selectedCtdVisible = state.selectedType === "ctd" && ctdEvents.some((event) => String(event.id) === String(state.selectedId));
  if (selectedVesselVisible || selectedCtdVisible) return;

  if (vessels.length) {
    state.selectedType = "vessel";
    state.selectedId = vessels[0].id;
  } else if (ctdEvents.length) {
    state.selectedType = "ctd";
    state.selectedId = String(ctdEvents[0].id);
  } else {
    state.selectedId = null;
  }
}

function render() {
  ensureSelection();
  renderStats();
  renderList();
  renderMap();
  renderDetail();
}

document.querySelectorAll(".mode-segment").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".mode-segment").forEach((segment) => segment.classList.remove("is-active"));
    button.classList.add("is-active");
    state.mode = button.dataset.mode;
    state.selectedId = null;
    state.mapNeedsFit = true;
    render();
  });
});

document.querySelectorAll(".signal-segment").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".signal-segment").forEach((segment) => segment.classList.remove("is-active"));
    button.classList.add("is-active");
    state.signalMode = button.dataset.signalMode;
    state.selectedId = null;
    state.mapNeedsFit = true;
    render();
  });
});

searchInput.addEventListener("input", () => {
  state.query = searchInput.value.trim().toLowerCase();
  state.selectedId = null;
  state.mapNeedsFit = true;
  render();
});

function boot(data) {
  state.data = data;
  render();
}

if (window.HYDROPHONE_APP_DATA) {
  boot(window.HYDROPHONE_APP_DATA);
} else {
  fetch(DATA_URL)
    .then((response) => {
      if (!response.ok) throw new Error(`Failed to load ${DATA_URL}`);
      return response.json();
    })
    .then(boot)
    .catch((error) => {
      detailPanel.innerHTML = `<div class="empty">${error.message}</div>`;
      sourceLine.textContent = "Data file missing";
    });
}
