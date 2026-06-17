const DATA_URL = "data/app_data.json";
const MAP_TILE_PROVIDERS = [
  {
    name: "CARTO Voyager",
    url: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    attribution: "&copy; OpenStreetMap contributors &copy; CARTO"
  },
  {
    name: "Esri World Street Map",
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    attribution: "Tiles &copy; Esri"
  }
];

const state = {
  data: null,
  mode: "vessels",
  signalMode: "strongest",
  strongestLimit: 40,
  strongestMaxDistanceKm: 2.5,
  closeCandidateDistanceKm: 0.35,
  selectedType: "vessel",
  selectedId: null,
  query: "",
  leafletMap: null,
  leafletLayers: null,
  tileLayer: null,
  tileProviderIndex: 0,
  mapNeedsFit: true
};

const svg = document.getElementById("mapSvg");
const leafletMapElement = document.getElementById("leafletMap");
const entityList = document.getElementById("entityList");
const detailPanel = document.getElementById("detailPanel");
const soundTrackPanel = document.getElementById("soundTrackPanel");
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
  if (value === null || value === undefined || value === "") return fallback;
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function vesselEventDistanceKm(vessel) {
  return finiteNumber(vessel.audio?.sourceDistanceKm, finiteNumber(vessel.closestDistanceKm, 999));
}

function ctdDetected(event) {
  if (event.detectedByHydrophone !== undefined) return event.detectedByHydrophone === true;
  return event.audio?.captured === true;
}

function beamErrorDeg(audio) {
  return finiteNumber(audio?.beam?.bearingErrorDeg);
}

function beamConfidence(audio) {
  return finiteNumber(audio?.beam?.confidence);
}

function beamHasCalibratedBearing(audio) {
  const beam = audio?.beam || {};
  return (
    Array.isArray(beam.bearingCandidatesDeg) &&
    beam.bearingCandidatesDeg.length > 0 &&
    finiteNumber(beam.bestBearingDeg) !== null &&
    finiteNumber(beam.bearingErrorDeg) !== null
  );
}

function beamStronglyDisagrees(audio) {
  if (!beamHasCalibratedBearing(audio)) return false;
  const error = beamErrorDeg(audio);
  const confidence = beamConfidence(audio);
  return error !== null && confidence !== null && confidence >= 0.12 && error > 75;
}

function beamSupportsSource(audio) {
  if (!beamHasCalibratedBearing(audio)) return false;
  const error = beamErrorDeg(audio);
  const confidence = beamConfidence(audio);
  return error !== null && confidence !== null && confidence >= 0.12 && error <= 35;
}

function acousticScore(vessel) {
  const audio = vessel.audio || {};
  const rms = finiteNumber(audio.rmsDbfsMean, -120);
  const peak = finiteNumber(audio.peakDbfs, -120);
  const distance = vesselEventDistanceKm(vessel);
  const coverage = finiteNumber(audio.coverageSeconds, 0);
  const distancePenalty = Math.max(0, distance) * 5.5;
  const directionBonus = beamSupportsSource(audio) ? 2 : 0;
  const directionPenalty = beamStronglyDisagrees(audio) ? 8 : 0;
  return rms + Math.max(-30, peak) * 0.12 + Math.min(coverage, 60) * 0.03 - distancePenalty + directionBonus - directionPenalty;
}

function strongestVessels(vessels) {
  const isolated = vessels
    .filter((vessel) => vesselEventDistanceKm(vessel) <= state.strongestMaxDistanceKm)
    .filter((vessel) => isDefaultLikelyCandidate(vessel))
    .slice()
    .sort((a, b) => acousticScore(b) - acousticScore(a))
    .slice(0, state.strongestLimit);
  if (isolated.length) return isolated;
  return vessels
    .filter((vessel) => vesselEventDistanceKm(vessel) <= 0.5)
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

function soundTracks() {
  return Array.isArray(state.data?.soundTracks) ? state.data.soundTracks : [];
}

function soundTrackForVessel(vesselId) {
  return soundTracks().find((track) => String(track.vesselId) === String(vesselId)) || null;
}

function selectedSoundTrack() {
  if (state.selectedType !== "vessel" || !state.selectedId) return null;
  return soundTrackForVessel(state.selectedId);
}

function soundTrackPoints(track) {
  return Array.isArray(track?.points)
    ? track.points.filter((point) => Number.isFinite(Number(point.latitude)) && Number.isFinite(Number(point.longitude)))
    : [];
}

function primarySoundPoint(track) {
  const points = soundTrackPoints(track);
  if (!points.length) return null;
  return points.reduce((best, point) => {
    const bestScore = finiteNumber(best.beamConfidence, 0) * 2 + finiteNumber(best.rmsDbfs, -120) / 60;
    const pointScore = finiteNumber(point.beamConfidence, 0) * 2 + finiteNumber(point.rmsDbfs, -120) / 60;
    return pointScore > bestScore ? point : best;
  }, points[0]);
}

function soundTrackSummary(track) {
  const points = soundTrackPoints(track);
  if (!points.length) return null;
  const ranges = points.map((point) => finiteNumber(point.rangeEstimateKm)).filter((value) => value !== null);
  const bearings = points.map((point) => finiteNumber(point.bearingDeg)).filter((value) => value !== null);
  const confidences = points.map((point) => finiteNumber(point.beamConfidence)).filter((value) => value !== null);
  return {
    pointCount: points.length,
    minRangeKm: ranges.length ? Math.min(...ranges) : null,
    maxRangeKm: ranges.length ? Math.max(...ranges) : null,
    minBearingDeg: bearings.length ? Math.min(...bearings) : null,
    maxBearingDeg: bearings.length ? Math.max(...bearings) : null,
    meanConfidence: confidences.length ? confidences.reduce((sum, value) => sum + value, 0) / confidences.length : null
  };
}

function parseMillis(value) {
  const millis = Date.parse(value || "");
  return Number.isFinite(millis) ? millis : null;
}

function windowsOverlapSeconds(a, b) {
  const aStart = parseMillis(a?.startUtc);
  const aEnd = parseMillis(a?.endUtc);
  const bStart = parseMillis(b?.startUtc);
  const bEnd = parseMillis(b?.endUtc);
  if (aStart === null || aEnd === null || bStart === null || bEnd === null) return 0;
  return Math.max(0, Math.min(aEnd, bEnd) - Math.max(aStart, bStart)) / 1000;
}

function audioRows(audio) {
  const waveform = audio?.waveform || {};
  const times = Array.isArray(waveform.timesSeconds) ? waveform.timesSeconds.map(Number) : [];
  const rms = Array.isArray(waveform.rmsDbfs) ? waveform.rmsDbfs.map(Number) : [];
  return times.map((time, index) => ({ time, rms: rms[index] }))
    .filter((row) => Number.isFinite(row.time) && Number.isFinite(row.rms));
}

function loudestOffsetSeconds(audio) {
  const rows = audioRows(audio);
  if (!rows.length) return null;
  return rows.reduce((best, row) => (row.rms > best.rms ? row : best), rows[0]).time;
}

function loudestArrivalDeltaSeconds(audio) {
  const loudest = loudestOffsetSeconds(audio);
  const eventOffset = finiteNumber(audio?.eventOffsetSeconds);
  if (loudest === null || eventOffset === null) return null;
  return Math.abs(loudest - eventOffset);
}

function sameAudioWindowCandidates(vessel) {
  const audio = vessel.audio || {};
  const selectedEvent = parseMillis(audio.eventTimeUtc);
  return state.data.vessels
    .filter((candidate) => candidate.id !== vessel.id && candidate.audio?.captured === true)
    .map((candidate) => {
      const candidateEvent = parseMillis(candidate.audio?.eventTimeUtc);
      const eventDelta = selectedEvent !== null && candidateEvent !== null
        ? Math.abs(candidateEvent - selectedEvent) / 1000
        : null;
      return {
        vessel: candidate,
        windowOverlap: windowsOverlapSeconds(audio, candidate.audio),
        eventDelta,
        distanceKm: vesselEventDistanceKm(candidate)
      };
    })
    .filter((candidate) => candidate.windowOverlap > 0 || (candidate.eventDelta !== null && candidate.eventDelta <= 60))
    .sort((a, b) => {
      const overlapDelta = b.windowOverlap - a.windowOverlap;
      if (Math.abs(overlapDelta) > 0.001) return overlapDelta;
      return (a.eventDelta ?? 9999) - (b.eventDelta ?? 9999);
    });
}

function sharedWindowSeconds(vessel) {
  const candidates = sameAudioWindowCandidates(vessel);
  return candidates.length ? candidates[0].windowOverlap : 0;
}

function isIsolatedCandidate(vessel) {
  const audio = vessel.audio || {};
  const distance = vesselEventDistanceKm(vessel);
  const loudestDelta = loudestArrivalDeltaSeconds(audio);
  const overlap = sharedWindowSeconds(vessel);
  const duration = durationSeconds(audio.startUtc, audio.endUtc) || finiteNumber(audio.coverageSeconds, 45) || 45;
  const hasSharedAudioWindow = overlap >= Math.min(15, duration * 0.35);
  const peak = finiteNumber(audio.peakDbfs, -120);

  return (
    distance <= state.strongestMaxDistanceKm &&
    !hasSharedAudioWindow &&
    peak > -35 &&
    (loudestDelta === null || loudestDelta <= 10)
  );
}

function isCloseCandidate(vessel) {
  const peak = finiteNumber(vessel.audio?.peakDbfs, -120);
  return vesselEventDistanceKm(vessel) <= state.closeCandidateDistanceKm && peak > -45;
}

function isDefaultLikelyCandidate(vessel) {
  return (isCloseCandidate(vessel) || isIsolatedCandidate(vessel)) && !beamStronglyDisagrees(vessel.audio);
}

function associationStatus(vessel) {
  const distance = vesselEventDistanceKm(vessel);
  const competitors = sameAudioWindowCandidates(vessel);
  const loudestDelta = loudestArrivalDeltaSeconds(vessel.audio);
  if (beamStronglyDisagrees(vessel.audio)) return "Direction mismatch";
  if (beamSupportsSource(vessel.audio) && distance <= 2.5 && competitors.length === 0) return "Direction supported";
  if (isCloseCandidate(vessel)) return competitors.length ? "Close shared candidate" : "Likely close candidate";
  if (!isIsolatedCandidate(vessel)) return "Ambiguous";
  if (distance <= 1.5 && (loudestDelta === null || loudestDelta <= 8) && competitors.length === 0) return "Likely candidate";
  if (distance > 5 || competitors.length >= 1) return "Ambiguous";
  return "Candidate";
}

function formatDegreeList(values) {
  if (!Array.isArray(values)) return "-";
  const degrees = values
    .map(Number)
    .filter((value) => Number.isFinite(value))
    .map((value) => `${value.toFixed(0)} deg`);
  return degrees.length ? degrees.join(" / ") : "-";
}

function beamDirectionHtml(audio) {
  const beam = audio?.beam || {};
  const angleCandidates = Array.isArray(beam.angleCandidatesDeg) ? beam.angleCandidatesDeg : [];
  const bearingCandidates = Array.isArray(beam.bearingCandidatesDeg) ? beam.bearingCandidatesDeg : [];
  const hasBeam = angleCandidates.length || Boolean(beam.note) || (beam.delaySeconds !== null && beam.delaySeconds !== undefined);
  if (!hasBeam) return "";

  const bearingHelp = bearingCandidates.length
    ? formatDegreeList(bearingCandidates)
    : "Set array heading to convert relative angles";
  const rows = [
    ["AIS bearing", fmt(audio.sourceBearingDeg, 0, " deg")],
    ["Array angles", formatDegreeList(angleCandidates)],
    ["Beam bearings", bearingHelp],
    ["Best match", fmt(beam.bestBearingDeg, 0, " deg")],
    ["Bearing error", fmt(beam.bearingErrorDeg, 1, " deg")],
    ["Confidence", fmt(beam.confidence, 2)],
    ["Band", `${fmt(beam.frequencyMinHz, 0, " Hz")} - ${fmt(beam.frequencyMaxHz, 0, " Hz")}`]
  ];

  return `
    <div class="beam-card">
      <div class="beam-grid">
        ${rows.map(([label, value]) => `
          <div>
            <span>${label}</span>
            <b>${value}</b>
          </div>
        `).join("")}
      </div>
      ${beam.note ? `<div class="beam-note">${beam.note}</div>` : ""}
    </div>
  `;
}

function associationContextHtml(vessel) {
  const competitors = sameAudioWindowCandidates(vessel);
  const loudestDelta = loudestArrivalDeltaSeconds(vessel.audio);
  const beamError = beamErrorDeg(vessel.audio);
  const beamConfidenceValue = beamConfidence(vessel.audio);
  const rows = competitors.slice(0, 5).map((item) => `
    <div class="candidate-row">
      <span>${clean(item.vessel.name)}</span>
      <span>${fmt(item.eventDelta, 1, " s")}</span>
      <span>${fmt(item.distanceKm, 2, " km")}</span>
      <span>${fmt(item.vessel.audio?.rmsDbfsMean, 1, " dBFS")}</span>
    </div>
  `).join("");
  return `
    <div class="association-box ${associationStatus(vessel).toLowerCase().replace(" ", "-")}">
      <b>${associationStatus(vessel)}</b>
      <span>Single-hydrophone AIS/audio association, not source-separated proof.</span>
      <span>Shared audio overlap: ${fmt(sharedWindowSeconds(vessel), 1, " s")}. Repeated waveforms mean ambiguous evidence.</span>
      <span>Peak vs AIS arrival delta: ${fmt(loudestDelta, 1, " s")}. Smaller is better; large means the loudest sound did not occur at the estimated arrival.</span>
      <span>Beam/AIS bearing error: ${fmt(beamError, 1, " deg")} at confidence ${fmt(beamConfidenceValue, 2)}.</span>
      <span>${competitors.length} other captured vessel candidates overlap this acoustic window or arrival time.</span>
      ${rows ? `<div class="candidate-table"><div class="candidate-head"><span>Candidate</span><span>dt</span><span>dist</span><span>RMS</span></div>${rows}</div>` : ""}
    </div>
  `;
}

function soundTrackDetailHtml(track) {
  if (!track) {
    return `
      <div class="timeline">
        No repeated acoustic-only sound track for this vessel yet.
      </div>
    `;
  }
  const summary = soundTrackSummary(track);
  const primary = primarySoundPoint(track);
  return `
    <div class="sound-track-detail">
      <div class="detail-grid">
        <div class="metric"><span>Windows</span><b>${summary?.pointCount || 0}</b></div>
        <div class="metric"><span>Range model</span><b>Level</b></div>
        <div class="metric"><span>Active bearing</span><b>${fmt(primary?.bearingDeg, 0, " deg")}</b></div>
        <div class="metric"><span>Active range</span><b>${fmt(primary?.rangeEstimateKm, 2, " km")}</b></div>
        <div class="metric"><span>Beam confidence</span><b>${fmt(summary?.meanConfidence, 2)}</b></div>
        <div class="metric"><span>Calibration</span><b>${clean(track.calibrationStatus)}</b></div>
      </div>
      <div class="timeline">${clean(track.note)}</div>
    </div>
  `;
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
  let signalText = `${filteredCtd().length} CTD casts`;
  if (modeAllows("vessels")) {
    const vesselCount = filteredVessels().length;
    const soundTrackText = metadata.soundTrackCount ? `, ${metadata.soundTrackCount} sound tracks` : "";
    signalText = state.signalMode === "strongest"
      ? `${vesselCount} isolated vessel candidates within ${state.strongestMaxDistanceKm} km${soundTrackText}`
      : `${vesselCount} captured vessel signals${soundTrackText}`;
  }
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
          <span class="entity-meta">${clean(event.startUtc)} - ${ctdDetected(event) ? "detected" : "not detected"}</span>
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

function renderSoundTrackPanel() {
  if (!soundTrackPanel) return;
  const track = selectedSoundTrack();
  const vessel = state.selectedType === "vessel" ? selectedVessel() : null;
  if (!track || !vessel) {
    const trackCount = finiteNumber(state.data?.metadata?.soundTrackCount, 0);
    soundTrackPanel.innerHTML = `
      <div class="sound-track-card is-empty">
        <div>
          <span class="sound-track-kicker">Sound Track</span>
          <b>No acoustic-only track for this selection</b>
        </div>
        <span>${trackCount} processed target tracks</span>
      </div>
    `;
    return;
  }

  const summary = soundTrackSummary(track);
  const primary = primarySoundPoint(track);
  soundTrackPanel.innerHTML = `
    <div class="sound-track-card">
      <div>
        <span class="sound-track-kicker">Sound Track</span>
        <b>${clean(track.vesselName || vessel.name)}</b>
      </div>
      <div class="sound-track-metrics">
        <span><b>${summary?.pointCount || 0}</b> windows</span>
        <span><b>${fmt(summary?.minRangeKm, 2)}-${fmt(summary?.maxRangeKm, 2, " km")}</b> level range</span>
        <span><b>${fmt(primary?.bearingDeg, 0, " deg")}</b> active bearing</span>
        <span><b>${fmt(summary?.meanConfidence, 2)}</b> beam conf</span>
      </div>
      <span class="sound-track-note">Acoustic-only: beam bearing + relative sound level, no AIS coordinates.</span>
    </div>
  `;
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
  setTileProvider(0);
  state.leafletLayers = L.layerGroup().addTo(state.leafletMap);
  return true;
}

function setTileProvider(index) {
  if (!state.leafletMap || !window.L) return;
  const provider = MAP_TILE_PROVIDERS[index];
  if (!provider) return;
  if (state.tileLayer) {
    state.leafletMap.removeLayer(state.tileLayer);
  }
  state.tileProviderIndex = index;
  state.tileLayer = L.tileLayer(provider.url, {
    maxZoom: 19,
    subdomains: "abcd",
    attribution: provider.attribution,
    crossOrigin: true
  });
  state.tileLayer.on("tileerror", () => {
    const nextIndex = state.tileProviderIndex + 1;
    if (MAP_TILE_PROVIDERS[nextIndex]) {
      setTileProvider(nextIndex);
    }
  });
  state.tileLayer.addTo(state.leafletMap);
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
    ...ctdEvents.map(ctdLatLng),
    ...soundTracks().flatMap((track) => soundTrackPoints(track).map(soundPointLatLng))
  ].filter(([lat, lon]) => Number.isFinite(lat) && Number.isFinite(lon));
  if (!points.length) return;
  state.leafletMap.fitBounds(L.latLngBounds(points), { padding: [32, 32], maxZoom: 14 });
  state.mapNeedsFit = false;
}

function selectedCtdEvent() {
  return state.data.ctdEvents.find((event) => String(event.id) === String(state.selectedId));
}

function addLeafletRadialGeometry(targetLatLng, distanceKm, color) {
  const hydro = state.data.hydrophone;
  const hydroLatLng = [hydro.latitude, hydro.longitude];
  if (!targetLatLng || !Number.isFinite(targetLatLng[0]) || !Number.isFinite(targetLatLng[1])) return;
  L.circle(hydroLatLng, {
    radius: Math.max(1, distanceKm) * 1000,
    color,
    dashArray: "8 8",
    fillColor: color,
    fillOpacity: 0.045,
    opacity: 0.75,
    weight: 2
  }).addTo(state.leafletLayers);
  L.polyline([hydroLatLng, targetLatLng], {
    color,
    dashArray: "8 8",
    opacity: 0.9,
    weight: 2
  }).addTo(state.leafletLayers);
}

function destinationLatLng(lat, lon, bearingDeg, distanceKm) {
  const radiusKm = 6371.0088;
  const angularDistance = distanceKm / radiusKm;
  const bearing = (bearingDeg * Math.PI) / 180;
  const lat1 = (lat * Math.PI) / 180;
  const lon1 = (lon * Math.PI) / 180;
  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(angularDistance) +
    Math.cos(lat1) * Math.sin(angularDistance) * Math.cos(bearing)
  );
  const lon2 = lon1 + Math.atan2(
    Math.sin(bearing) * Math.sin(angularDistance) * Math.cos(lat1),
    Math.cos(angularDistance) - Math.sin(lat1) * Math.sin(lat2)
  );
  return [(lat2 * 180) / Math.PI, (((lon2 * 180) / Math.PI + 540) % 360) - 180];
}

function beamBearingCandidates(audio) {
  const candidates = audio?.beam?.bearingCandidatesDeg;
  if (Array.isArray(candidates) && candidates.length) {
    return candidates.map(Number).filter((value) => Number.isFinite(value));
  }
  const best = finiteNumber(audio?.beam?.bestBearingDeg);
  return best === null ? [] : [best];
}

function addLeafletBeamGeometry(audio, fallbackDistanceKm) {
  const bearings = beamBearingCandidates(audio);
  if (!bearings.length) return;
  const hydro = state.data.hydrophone;
  const hydroLatLng = [hydro.latitude, hydro.longitude];
  const distanceKm = Math.max(0.5, finiteNumber(audio?.sourceDistanceKm, fallbackDistanceKm) || fallbackDistanceKm || 1);
  for (const bearing of bearings) {
    const end = destinationLatLng(hydro.latitude, hydro.longitude, bearing, distanceKm);
    L.polyline([hydroLatLng, end], {
      color: "#c53b2c",
      dashArray: "3 7",
      opacity: 0.9,
      weight: 2
    }).bindTooltip(`Beam ${fmt(bearing, 0, " deg")}`).addTo(state.leafletLayers);
  }
}

function soundPointLatLng(point) {
  return [Number(point.latitude), Number(point.longitude)];
}

function addLeafletSoundTrack(track) {
  const points = soundTrackPoints(track);
  if (!points.length) return;
  const latLngs = points.map(soundPointLatLng);
  if (latLngs.length > 1) {
    L.polyline(latLngs, {
      color: "#d24b26",
      opacity: 0.95,
      weight: 4,
      lineCap: "round",
      lineJoin: "round"
    }).bindTooltip(`${track.vesselName} sound-only track`).addTo(state.leafletLayers);
  }
  for (const point of points) {
    const confidence = finiteNumber(point.beamConfidence, 0);
    const marker = L.circleMarker(soundPointLatLng(point), {
      color: "#ffffff",
      fillColor: "#d24b26",
      fillOpacity: 0.86,
      opacity: 1,
      radius: 5 + Math.max(0, Math.min(5, confidence * 8)),
      weight: 2
    }).bindTooltip(
      `Sound ${fmt(point.bearingDeg, 0, " deg")} / ${fmt(point.rangeEstimateKm, 2, " km")} / ${fmt(point.rmsDbfs, 1, " dBFS")}`
    );
    marker.addTo(state.leafletLayers);
  }
}

function renderLeafletMap() {
  if (!initializeLeafletMap()) return false;
  const vessels = filteredVessels();
  const ctdEvents = filteredCtd();
  state.leafletLayers.clearLayers();

  const selected = selectedVessel();
  const selectedTrack = selectedSoundTrack();
  if (state.selectedType === "vessel" && selected && modeAllows("vessels")) {
    const soundPoint = primarySoundPoint(selectedTrack);
    if (selectedTrack && soundPoint) {
      addLeafletRadialGeometry(soundPointLatLng(soundPoint), finiteNumber(soundPoint.rangeEstimateKm, 1), "#d24b26");
      addLeafletSoundTrack(selectedTrack);
    } else {
      addLeafletRadialGeometry(vesselLatLng(selected), vesselEventDistanceKm(selected), "#d59c22");
      addLeafletBeamGeometry(selected.audio, vesselEventDistanceKm(selected));
      const selectedAisTrack = selected.track
        .map((point) => [Number(point.latitude), Number(point.longitude)])
        .filter(([lat, lon]) => Number.isFinite(lat) && Number.isFinite(lon));
      if (selectedAisTrack.length > 1) {
        L.polyline(selectedAisTrack, {
          color: "#0b7180",
          opacity: 0.9,
          weight: 4,
          lineCap: "round",
          lineJoin: "round"
        }).addTo(state.leafletLayers);
      }
    }
  }

  const selectedCtd = selectedCtdEvent();
  if (state.selectedType === "ctd" && selectedCtd && modeAllows("ctd")) {
    addLeafletRadialGeometry(ctdLatLng(selectedCtd), finiteNumber(selectedCtd.distanceToHydrophoneKm, 1), "#8060b2");
    addLeafletBeamGeometry(selectedCtd.audio, finiteNumber(selectedCtd.distanceToHydrophoneKm, 1));
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
        ...leafletStyle("ctd", selectedMarker, !ctdDetected(event)),
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
    const track = selectedSoundTrack();
    const soundPoint = primarySoundPoint(track);
    if (track && soundPoint) {
      addFallbackRadialGeometry(soundPoint.latitude, soundPoint.longitude);
    } else if (vessel) {
      addFallbackRadialGeometry(vessel.closestLatitude, vessel.closestLongitude);
    }
  }
  if (state.selectedType === "ctd" && modeAllows("ctd")) {
    const event = selectedCtdEvent();
    if (event) {
      addFallbackRadialGeometry(event.latitude, event.longitude);
    }
  }

  if (state.selectedType === "vessel" && modeAllows("vessels")) {
    const vessel = selectedVessel();
    const track = selectedSoundTrack();
    const soundPoints = soundTrackPoints(track);
    if (soundPoints.length) {
      const points = soundPoints
        .map((point) => {
          const p = project(point.latitude, point.longitude);
          return `${p.x.toFixed(2)},${p.y.toFixed(2)}`;
        })
        .join(" ");
      const soundTrack = svgElement("polyline", {
        class: "track sound-track is-selected",
        points
      });
      svg.appendChild(soundTrack);
      for (const point of soundPoints) {
        const p = project(point.latitude, point.longitude);
        svg.appendChild(svgElement("circle", {
          class: "marker sound-point",
          cx: p.x,
          cy: p.y,
          r: 6
        }));
      }
    } else if (vessel && vessel.track.length) {
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

function addFallbackRadialGeometry(lat, lon) {
  const hydro = state.data.hydrophone;
  const start = project(hydro.latitude, hydro.longitude);
  const end = project(Number(lat), Number(lon));
  if (!Number.isFinite(end.x) || !Number.isFinite(end.y)) return;
  svg.appendChild(svgElement("line", {
    class: "radial-line",
    x1: start.x,
    y1: start.y,
    x2: end.x,
    y2: end.y
  }));
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
  const loudestX = xForTime(loudest.time).toFixed(1);

  return `
    <div class="waveform-card">
      <svg class="waveform-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Audio envelope">
        <line class="waveform-grid" x1="${padX}" y1="${padY}" x2="${width - padX}" y2="${padY}"></line>
        <line class="waveform-grid" x1="${padX}" y1="${height - padY}" x2="${width - padX}" y2="${height - padY}"></line>
        <polyline class="waveform-peak" points="${peakPoints}"></polyline>
        <polyline class="waveform-rms" points="${rmsPoints}"></polyline>
        <line class="waveform-arrival" x1="${markerX}" y1="${padY}" x2="${markerX}" y2="${height - padY}"></line>
        <line class="waveform-loudest" x1="${loudestX}" y1="${padY}" x2="${loudestX}" y2="${height - padY}"></line>
        <text class="waveform-label" x="${padX}" y="11">${maxDb} dBFS</text>
        <text class="waveform-label" x="${padX}" y="${height - 3}">${minDb} dBFS</text>
        <text class="waveform-arrival-label" x="${Math.min(width - 72, Number(markerX) + 4).toFixed(1)}" y="28">AIS arrival</text>
        <text class="waveform-loudest-label" x="${Math.min(width - 44, Number(loudestX) + 4).toFixed(1)}" y="44">Peak</text>
      </svg>
      <div class="waveform-meta">
        <span>Window ${fmt(duration, 1, " s")}</span>
        <span>Peak ${fmt(loudest.time, 1, " s")} / ${fmt(loudest.rms, 1, " dBFS")}</span>
      </div>
    </div>
  `;
}

function interpolateChannel(a, b, t) {
  return Math.round(a + (b - a) * t);
}

function interpolateColor(from, to, t) {
  const start = from.match(/\w\w/g).map((value) => parseInt(value, 16));
  const end = to.match(/\w\w/g).map((value) => parseInt(value, 16));
  return `#${start.map((channel, index) => interpolateChannel(channel, end[index], t).toString(16).padStart(2, "0")).join("")}`;
}

function heatColor(value, min, max) {
  const t = Math.max(0, Math.min(1, (value - min) / Math.max(1, max - min)));
  if (t < 0.45) return interpolateColor("#234a78", "#168f9f", t / 0.45);
  if (t < 0.78) return interpolateColor("#168f9f", "#e2b33b", (t - 0.45) / 0.33);
  return interpolateColor("#e2b33b", "#c53b2c", (t - 0.78) / 0.22);
}

function audioGradientHtml(audio) {
  const waveform = audio.waveform || {};
  const values = Array.isArray(waveform.rmsDbfs)
    ? waveform.rmsDbfs.map(Number).filter((value) => Number.isFinite(value))
    : [];
  if (values.length < 2) return "";
  const min = Math.floor(Math.min(...values) / 5) * 5;
  const max = Math.ceil(Math.max(...values) / 5) * 5;
  const stops = values.map((value, index) => {
    const pct = values.length === 1 ? 0 : (index / (values.length - 1)) * 100;
    return `${heatColor(value, min, max)} ${pct.toFixed(1)}%`;
  }).join(", ");
  const markerPct = (() => {
    const duration = durationSeconds(audio.startUtc, audio.endUtc);
    const eventOffset = Number(audio.eventOffsetSeconds);
    if (!duration || !Number.isFinite(eventOffset)) return 50;
    return Math.max(0, Math.min(100, (eventOffset / duration) * 100));
  })();
  const peakPct = (() => {
    const duration = durationSeconds(audio.startUtc, audio.endUtc);
    const peakOffset = loudestOffsetSeconds(audio);
    if (!duration || peakOffset === null) return null;
    return Math.max(0, Math.min(100, (peakOffset / duration) * 100));
  })();
  return `
    <div class="sound-gradient-card">
      <div class="sound-gradient" style="background: linear-gradient(90deg, ${stops})">
        <span class="sound-gradient-marker" style="left:${markerPct.toFixed(1)}%"></span>
        ${peakPct === null ? "" : `<span class="sound-gradient-peak" style="left:${peakPct.toFixed(1)}%"></span>`}
      </div>
      <div class="waveform-meta">
        <span>${fmt(min, 0, " dBFS")}</span>
        <span>AIS arrival / peak</span>
        <span>${fmt(max, 0, " dBFS")}</span>
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
  const beamBlock = beamDirectionHtml(audio);
  return `
    <div class="detail-grid">
      <div class="metric"><span>RMS</span><b>${fmt(audio.rmsDbfsMean, 1, " dBFS")}</b></div>
      <div class="metric"><span>Peak</span><b>${fmt(audio.peakDbfs, 1, " dBFS")}</b></div>
      <div class="metric"><span>Crest</span><b>${fmt(audio.crestFactorDb, 1, " dB")}</b></div>
      <div class="metric"><span>Coverage</span><b>${fmt(audio.coverageSeconds, 1, " s")}</b></div>
    </div>
    <h4 class="mini-title">Hydrophone waveform</h4>
    ${audioWaveformHtml(audio)}
    <h4 class="mini-title">Sound intensity gradient</h4>
    ${audioGradientHtml(audio)}
    ${beamBlock ? `<h4 class="mini-title">Direction check</h4>${beamBlock}` : ""}
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
      <div class="metric"><span>Audio source</span><b>${fmt(vesselEventDistanceKm(vessel), 2, " km")}</b></div>
      <div class="metric"><span>Bearing</span><b>${fmt(vessel.closestBearingDeg, 0, " deg")}</b></div>
      <div class="metric"><span>Association</span><b>${associationStatus(vessel)}</b></div>
      <div class="metric"><span>Beam error</span><b>${fmt(vessel.audio?.beam?.bearingErrorDeg, 0, " deg")}</b></div>
      <div class="metric"><span>Beam confidence</span><b>${fmt(vessel.audio?.beam?.confidence, 2)}</b></div>
      <div class="metric"><span>Max speed</span><b>${fmt(vessel.maxSog, 1, " kn")}</b></div>
      <div class="metric"><span>Length</span><b>${fmt(vessel.maxLength, 0, " m")}</b></div>
      <div class="metric"><span>Draught</span><b>${fmt(vessel.maxDraught, 1, " m")}</b></div>
      <div class="metric"><span>AIS rows</span><b>${vessel.rowCount}</b></div>
    </div>
    ${associationContextHtml(vessel)}
    <h3 class="section-title">Sound-only track</h3>
    ${soundTrackDetailHtml(soundTrackForVessel(vessel.id))}
    <h3 class="section-title">Acoustic profile</h3>
    ${audioProfileHtml(vessel.audio)}
    <h3 class="section-title">AIS track</h3>
    <div class="timeline">${clean(vessel.firstTimestamp)}<br>${clean(vessel.lastTimestamp)}</div>
  `;
}

function renderCtdDetail(event) {
  const audio = event.audio || {};
  detailPanel.innerHTML = `
    <div class="detail-kicker">CTD</div>
    <h2>CTD ${clean(event.station || event.id)}</h2>
    <p>${clean(event.fileName)}</p>
    <div class="detail-grid">
      <div class="metric"><span>Hydrophone distance</span><b>${fmt(event.distanceToHydrophoneKm, 2, " km")}</b></div>
      <div class="metric"><span>Detection</span><b>${ctdDetected(event) ? "Yes" : "No"}</b></div>
      <div class="metric"><span>Depth</span><b>${fmt(event.depthMinM, 1)}-${fmt(event.depthMaxM, 1, " m")}</b></div>
      <div class="metric"><span>Temperature</span><b>${fmt(event.temperatureMinC, 1)}-${fmt(event.temperatureMaxC, 1, " C")}</b></div>
      <div class="metric"><span>Salinity</span><b>${fmt(event.salinityMinPsu, 1)}-${fmt(event.salinityMaxPsu, 1, " PSU")}</b></div>
      <div class="metric"><span>CTD bearing</span><b>${fmt(audio.sourceBearingDeg, 0, " deg")}</b></div>
      <div class="metric"><span>Beam error</span><b>${fmt(audio.beam?.bearingErrorDeg, 0, " deg")}</b></div>
    </div>
    <h3 class="section-title">Cast window</h3>
    <div class="timeline">${clean(event.startUtc)}<br>${clean(event.endUtc)}</div>
    <h3 class="section-title">Detection geometry</h3>
    <div class="timeline">CTD position: ${fmt(event.latitude, 5)}, ${fmt(event.longitude, 5)}<br>Hydrophone arrival: ${clean(audio.eventTimeUtc)}<br>Sound delay: ${fmt(audio.propagationDelaySeconds, 1, " s")}</div>
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
  renderSoundTrackPanel();
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
