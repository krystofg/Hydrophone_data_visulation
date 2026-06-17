const DATA_URL = "data/app_data.json";

const state = {
  data: null,
  mode: "vessels",
  selectedType: "vessel",
  selectedId: null,
  query: ""
};

const svg = document.getElementById("mapSvg");
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
  return state.data.vessels.filter((vessel) => modeAllows("vessels") && matchesQuery(vessel, ["id", "name", "shipType"]));
}

function filteredCtd() {
  return state.data.ctdEvents.filter((event) => modeAllows("ctd") && matchesQuery(event, ["station", "fileName", "startUtc"]));
}

function renderStats() {
  const metadata = state.data.metadata;
  stats.innerHTML = [
    ["Vessels", metadata.vesselCount],
    ["CTD", metadata.ctdCount],
    ["Audio profiles", metadata.audioProfileCount]
  ].map(([label, value]) => `<div class="stat"><b>${value}</b><span>${label}</span></div>`).join("");
  const source = metadata.sources.aisCsv || "no AIS source";
  sourceLine.textContent = source;
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
          <span class="entity-meta">${vessel.id} · ${clean(vessel.shipType)} · ${fmt(vessel.maxSog, 1, " kn")}</span>
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
          <span class="entity-meta">${clean(event.startUtc)} · ${event.recordedByHydrophone ? "recorded" : "outside audio"}</span>
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

function renderMap() {
  svg.innerHTML = "";
  svg.setAttribute("viewBox", "0 0 1000 1000");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const water = svgElement("rect", { x: 0, y: 0, width: 1000, height: 1000, fill: "transparent" });
  svg.appendChild(water);

  if (modeAllows("vessels")) {
    for (const vessel of filteredVessels()) {
      if (!vessel.track.length) continue;
      const points = vessel.track.map((point) => {
        const p = project(point.latitude, point.longitude);
        return `${p.x.toFixed(2)},${p.y.toFixed(2)}`;
      }).join(" ");
      const track = svgElement("polyline", {
        class: `track ${state.selectedType === "vessel" && state.selectedId === vessel.id ? "is-selected" : ""}`,
        points
      });
      svg.appendChild(track);
    }

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

function selectedVessel() {
  return state.data.vessels.find((vessel) => vessel.id === state.selectedId) || state.data.vessels[0];
}

function selectedCtd() {
  return state.data.ctdEvents.find((event) => String(event.id) === String(state.selectedId)) || state.data.ctdEvents[0];
}

function audioProfileHtml(audio) {
  if (!audio) {
    return `<p>No audio profile linked yet</p>`;
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
      <div class="metric"><span>Stereo corr.</span><b>${fmt(audio.stereoCorrelation, 2)}</b></div>
    </div>
    ${bandRows}
    <div class="timeline">${clean(audio.fileName)}<br>${clean(audio.startUtc)} to ${clean(audio.endUtc)}</div>
  `;
}

function renderVesselDetail(vessel) {
  detailPanel.innerHTML = `
    <div class="detail-kicker">Vessel</div>
    <h2>${clean(vessel.name)}</h2>
    <p>${vessel.id} · ${clean(vessel.shipType)} · ${clean(vessel.typeOfMobile)}</p>
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
  `;
}

function renderDetail() {
  if (state.selectedType === "ctd") {
    const event = selectedCtd();
    if (event) renderCtdDetail(event);
    return;
  }
  const vessel = selectedVessel();
  if (vessel) renderVesselDetail(vessel);
}

function ensureSelection() {
  if (state.selectedId) return;
  if (state.data.vessels.length) {
    state.selectedType = "vessel";
    state.selectedId = state.data.vessels[0].id;
  } else if (state.data.ctdEvents.length) {
    state.selectedType = "ctd";
    state.selectedId = String(state.data.ctdEvents[0].id);
  }
}

function render() {
  ensureSelection();
  renderStats();
  renderList();
  renderMap();
  renderDetail();
}

document.querySelectorAll(".segment").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".segment").forEach((segment) => segment.classList.remove("is-active"));
    button.classList.add("is-active");
    state.mode = button.dataset.mode;
    if (state.mode === "ctd" && state.data.ctdEvents.length) {
      state.selectedType = "ctd";
      state.selectedId = String(state.data.ctdEvents[0].id);
    }
    if (state.mode === "vessels" && state.data.vessels.length) {
      state.selectedType = "vessel";
      state.selectedId = state.data.vessels[0].id;
    }
    render();
  });
});

searchInput.addEventListener("input", () => {
  state.query = searchInput.value.trim().toLowerCase();
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
