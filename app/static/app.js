/* CampgroundFinder — map + list.
   Reads only our own server; never contacts a camping website directly.
   The one exception is the basemap tile server, which the browser fetches
   directly and which is configured in config.yaml, not here (§8h). */

const LABELS = {
  available: "Open now",
  full: "Full",
  unknown: "Not checked yet",
  stale: "Couldn't reach the website",
  closed: "Closed",
};

const QUALITY_NOTE = {
  measured: null,   // believable figures; nothing to warn about
  default: "Site lengths here look like a form default, not a measurement.",
  unknown: "Too few sites to judge the length data.",
};

let STATE = {
  campgrounds: [], states: [], counts: {}, last_checked: null,
  unlocated: 0, map: null,
};
let activeStates = new Set();
let query = "";
let view = "map";

let map = null;
let cluster = null;
let tilesEverLoaded = false;
let needsFit = false;

/* Tile bookkeeping. A missing tile is a blank square on a map, which is the
   worst possible failure mode here: it reads as "there is nothing in this
   area". Observed live on 2026-07-29 — OpenTopoMap dropped roughly half the
   tiles in one view and the page said nothing, because the old code only ever
   warned when the *first* tile failed. So failures are now counted while they
   are on screen, retried, and said out loud. */
const TILE_RETRIES = 2;
const tileTries = new Map();      // "z/x/y" -> attempts so far
const tileFailed = new Set();     // "z/x/y" currently on screen and blank

// Basemaps. "auto" swaps at topoFromZoom; the other two modes pin a choice.
const basemaps = { topo: null, street: null };
let basemapMode = "auto";
let shownBasemap = null;
let topoFromZoom = 12;

function tileKey(c) {
  return `${c.z}/${c.x}/${c.y}`;
}

function setStatus(text, kind) {
  const box = document.getElementById("status");
  document.getElementById("status-text").textContent = text;
  box.className = "status" + (kind ? " " + kind : "");
}

function ago(iso) {
  if (!iso) return null;
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (!isFinite(mins) || mins < 0) return null;
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} hr ago`;
  return `${Math.floor(hrs / 24)} days ago`;
}

/* Which campgrounds the current search/region scoping shows.
   Note what this does NOT do: it never drops a campground for being unknown,
   stale, closed, or unlocated. Missing data is a thing to display, not a
   reason to disappear (§8k). */
function visible() {
  const q = query.trim().toLowerCase();
  return STATE.campgrounds.filter((c) => {
    if (activeStates.size && !activeStates.has(c.state)) return false;
    if (q && !(c.name || "").toLowerCase().includes(q)) return false;
    return true;
  });
}

/* --- map ---------------------------------------------------------------- */

/* One basemap layer, wired for retry-and-admit-it. */
function makeBasemap(tiles) {
  const layer = L.tileLayer(tiles.url, {
    attribution: tiles.attribution || "",
    subdomains: tiles.subdomains || "abc",
    maxZoom: tiles.max_zoom || 17,
    // Above this, Leaflet upscales the last zoom the server actually renders
    // instead of requesting tiles that come back blank. Blurry beats empty.
    maxNativeZoom: tiles.max_native_zoom || tiles.max_zoom || 17,
  });

  layer.on("tileload", (e) => {
    tilesEverLoaded = true;
    // A retry that succeeded stops being a failure.
    tileFailed.delete(tileKey(e.coords));
    updateTileWarning();
  });

  /* The tile server here is a volunteer one that rate-limits and renders on
     demand, so a failure is usually transient. Retry a couple of times with a
     little backoff before giving up and admitting to the gap. The query string
     is there to get past the browser's cache of the failed response — without
     it Safari serves the error back instantly and the retry is worthless. */
  layer.on("tileerror", (e) => {
    const key = tileKey(e.coords);
    const tries = (tileTries.get(key) || 0) + 1;
    tileTries.set(key, tries);
    if (tries <= TILE_RETRIES && e.tile) {
      const url = layer.getTileUrl(e.coords);
      setTimeout(() => {
        e.tile.src = url + (url.includes("?") ? "&" : "?") + "retry=" + tries;
      }, 500 * tries);
      return;
    }
    tileFailed.add(key);
    updateTileWarning();
  });

  // Panning away discards a tile; a gap that is no longer on screen should
  // stop being reported, so the count always describes what's actually visible.
  layer.on("tileunload", (e) => {
    const key = tileKey(e.coords);
    tileFailed.delete(key);
    tileTries.delete(key);
    updateTileWarning();
  });

  return layer;
}

/* Which basemap should be showing: the pinned one, or — on auto — whichever
   suits the zoom. */
function wantedBasemap() {
  if (basemapMode !== "auto") return basemapMode;
  return map.getZoom() >= topoFromZoom ? "topo" : "street";
}

function applyBasemap() {
  // The buttons exist before the state arrives, so a click during loading
  // must be a no-op rather than a crash that kills the rest of the page.
  if (!map) return;
  const want = wantedBasemap();
  if (want !== shownBasemap) {
    if (basemaps[shownBasemap]) map.removeLayer(basemaps[shownBasemap]);
    basemaps[want].addTo(map);
    basemaps[want].bringToBack();
    shownBasemap = want;
    // Those failures belonged to the layer just dropped; carrying the count
    // over would report gaps that are no longer on screen.
    tileFailed.clear();
    tileTries.clear();
    updateTileWarning();
  }
  document.getElementById("basemap-note").textContent =
    `zoom ${map.getZoom()} · ` +
    `${shownBasemap === "topo" ? "topographic" : "street"} tiles` +
    (basemapMode === "auto" ? ` · topo from zoom ${topoFromZoom}` : " · pinned");
}

function setBasemapMode(mode) {
  basemapMode = mode;
  for (const [id, name] of [["base-auto", "auto"], ["base-topo", "topo"],
                            ["base-street", "street"]]) {
    const btn = document.getElementById(id);
    btn.classList.toggle("on", mode === name);
    btn.setAttribute("aria-pressed", String(mode === name));
  }
  applyBasemap();
}

function initMap() {
  const settings = STATE.map || {};

  map = L.map("map", { preferCanvas: true });
  if (settings.center) {
    map.setView(settings.center, settings.zoom || 7);
  } else {
    // No home_base and no configured centre. Rather than invent a coordinate,
    // frame whatever we actually have once the pins are drawn.
    map.setView([45.5, -122.5], 5);
    needsFit = true;
  }

  topoFromZoom = settings.topo_from_zoom || 12;
  basemaps.topo = makeBasemap(settings.tiles || {});
  // Falls back to the topo layer's own settings rather than to nothing: a
  // config without street tiles should lose the swap, not the background.
  basemaps.street = makeBasemap(settings.street_tiles || settings.tiles || {});
  applyBasemap();
  map.on("zoomend", applyBasemap);

  cluster = L.markerClusterGroup({
    // 774 pins at low zoom is a wall of dots; cluster until it means something.
    maxClusterRadius: 45,
    showCoverageOnHover: false,
  });
  map.addLayer(cluster);
}

/* Says out loud what the blank squares are. Two different messages, because
   they are two different problems: no tiles at all is a connection, and some
   tiles missing is a tile server dropping requests. Neither may be left for
   the eye to interpret as empty country. */
function updateTileWarning() {
  const el = document.getElementById("tile-warning");
  if (!tilesEverLoaded) {
    el.hidden = false;
    el.textContent =
      "Couldn't load the map background — check the internet connection. " +
      "The campground pins are still accurate.";
    return;
  }
  const n = tileFailed.size;
  el.hidden = n === 0;
  el.textContent = n
    ? `${n} map background tile${n === 1 ? "" : "s"} didn't load. The blank ` +
      `square${n === 1 ? " is" : "s are"} missing background, not empty ` +
      `country — every campground we know about is still pinned.`
    : "";
}

/* Every status gets its OWN SHAPE, not just its own colour.

   Scott is colourblind and reported on 2026-07-29 that the five coloured dots
   were indistinguishable to him — which made the map's entire status layer
   decorative. Colour is now the secondary cue and silhouette is the primary
   one, so the map works desaturated. The five are deliberately different at a
   glance rather than subtly different: filled circle, filled square, hollow
   circle, triangle, cross.

   Drawn as SVG rather than a styled <span>, because a triangle and a cross in
   CSS at 16px are borrow-a-border tricks that break at the sizes we need. */
const PIN_SHAPE = {
  // Open now — a solid disc, the heaviest mark on the map, because it is the
  // one thing someone is actually looking for.
  available: (s, f) =>
    `<circle cx="${s / 2}" cy="${s / 2}" r="${s / 2 - 2}" fill="${f}"/>`,
  // Full — solid, but square: same visual weight, unmistakably not a circle.
  full: (s, f) =>
    `<rect x="2" y="2" width="${s - 4}" height="${s - 4}" fill="${f}"/>`,
  // Not checked yet — hollow. Emptiness is the point: we have nothing to say.
  unknown: (s, f) =>
    `<circle cx="${s / 2}" cy="${s / 2}" r="${s / 2 - 2.5}" fill="${f}"
             fill-opacity="0.55"/>`,
  // Couldn't reach the website — a warning triangle, the one shape everyone
  // already reads as "something is wrong".
  stale: (s, f) =>
    `<polygon points="${s / 2},2 ${s - 2},${s - 3} 2,${s - 3}" fill="${f}"/>`,
  // Closed — struck through.
  closed: (s, f) =>
    `<rect x="2" y="2" width="${s - 4}" height="${s - 4}" rx="2" fill="${f}"/>` +
    `<path d="M${s * 0.3},${s * 0.3} L${s * 0.7},${s * 0.7} ` +
    `M${s * 0.7},${s * 0.3} L${s * 0.3},${s * 0.7}" ` +
    `stroke="#fbfaf7" stroke-width="2" stroke-linecap="round" fill="none"/>`,
};

const PIN_FILL = {
  available: "#0b5cab",
  full: "#e08214",
  unknown: "#d6d1c6",
  stale: "#7b52ab",
  closed: "#1b1a17",
};

function pinIcon(c) {
  const open = c.open_sites || 0;
  // Bigger than the first cut across the board — 12px dots were hard to pick
  // out of a topo background. Open campgrounds still grow with how much is
  // open, so the map's loudest marks are its most useful ones.
  const size = c.status === "available"
    ? (open > 20 ? 28 : open > 5 ? 24 : 20)
    : 18;
  // An unrecognised status must still draw something: a pin that vanishes
  // because we added a status and forgot the map is exactly the silent
  // disappearance this project keeps banning (§8k).
  const shape = PIN_SHAPE[c.status] || PIN_SHAPE.unknown;
  const fill = PIN_FILL[c.status] || PIN_FILL.unknown;
  const svg =
    `<svg class="pin" width="${size}" height="${size}" ` +
    `viewBox="0 0 ${size} ${size}" aria-hidden="true">` +
    `<g stroke="#1b1a17" stroke-width="2">${shape(size, fill)}</g></svg>`;
  return L.divIcon({
    html: svg,
    className: "pin-wrap",
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

/* Built with textContent throughout: campground names come from provider
   scrapes, and must never be interpolated into HTML. */
function popupFor(c) {
  const box = document.createElement("div");
  box.className = "popup";

  const h = document.createElement("strong");
  h.textContent = c.name;
  box.appendChild(h);

  const where = document.createElement("div");
  where.className = "popup-where";
  where.textContent = [c.state, c.provider].filter(Boolean).join(" · ");
  box.appendChild(where);

  const status = document.createElement("div");
  status.className = "badge " + c.status;
  const dot = document.createElement("b");
  status.append(dot, document.createTextNode(LABELS[c.status] || c.status));
  box.appendChild(status);

  const lines = [];
  if (c.status_reason) lines.push(c.status_reason);
  if (c.status === "available" && c.open_sites > 0) {
    lines.push(`${c.open_sites} opening${c.open_sites === 1 ? "" : "s"} in the window we've scanned`);
  }
  // Straight from the server, which is where the honest phrasing lives.
  if (c.booking_label) lines.push(c.booking_label);
  // State-park providers publish one coordinate for the whole park. Cape
  // Lookout's is a kilometre from its campground; drawing that pin without
  // saying so claims a precision we don't have.
  if (c.coord_precision === "park") {
    lines.push("Pin marks the park, not the campground — the sites can be a mile away.");
  }
  const note = QUALITY_NOTE[c.length_data_quality];
  if (note) lines.push(note);

  for (const text of lines) {
    const p = document.createElement("div");
    p.className = "popup-note";
    p.textContent = text;
    box.appendChild(p);
  }
  return box;
}

function renderMap() {
  if (!map) return;
  const located = visible().filter((c) => c.located && c.latitude != null);
  cluster.clearLayers();
  cluster.addLayers(
    located.map((c) =>
      L.marker([c.latitude, c.longitude], { icon: pinIcon(c), title: c.name })
        .bindPopup(() => popupFor(c))
    )
  );

  // Frame the catalog once, on first draw only — refitting on every keystroke
  // would yank the map around while someone is typing.
  if (needsFit && located.length) {
    needsFit = false;
    map.fitBounds(cluster.getBounds(), { padding: [20, 20] });
  }

  // Unlocated campgrounds cannot be drawn, so they get said out loud instead
  // of quietly vanishing between the catalog count and the map.
  const hidden = visible().length - located.length;
  const el = document.getElementById("unlocated");
  el.hidden = hidden === 0;
  el.textContent = hidden
    ? `${hidden} campground${hidden === 1 ? " has" : "s have"} no location and ` +
      `can't be drawn on the map — they're in the list view.`
    : "";
}

/* --- list --------------------------------------------------------------- */

function renderList() {
  const rows = document.getElementById("rows");
  const list = visible();

  rows.replaceChildren();
  for (const c of list) {
    const tr = document.createElement("tr");

    const name = document.createElement("td");
    name.className = "name";
    name.textContent = c.name;
    if (!c.located) {
      const note = document.createElement("span");
      note.className = "reason";
      note.textContent = "location unknown";
      name.appendChild(note);
    }

    const where = document.createElement("td");
    where.className = "where";
    where.textContent = c.state || "—";

    const status = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge " + c.status;
    const dot = document.createElement("b");
    badge.append(dot, document.createTextNode(LABELS[c.status] || c.status));
    status.appendChild(badge);
    if (c.status_reason) {
      const why = document.createElement("span");
      why.className = "reason";
      why.textContent = c.status_reason;
      status.appendChild(why);
    }

    const open = document.createElement("td");
    open.textContent = c.open_sites > 0 ? c.open_sites : "—";

    tr.append(name, where, status, open);
    rows.appendChild(tr);
  }

  document.getElementById("empty").hidden = list.length > 0;
}

function render() {
  renderList();
  renderMap();
  const checked = ago(STATE.last_checked);
  document.getElementById("foot").textContent =
    `Showing ${visible().length} of ${STATE.total} campgrounds` +
    (checked ? ` · last checked ${checked}` : "");
}

function renderRegions() {
  const box = document.getElementById("regions");
  box.replaceChildren();
  for (const s of STATE.states) {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = activeStates.has(s);
    cb.addEventListener("change", () => {
      cb.checked ? activeStates.add(s) : activeStates.delete(s);
      render();
    });
    label.append(cb, document.createTextNode(s));
    box.appendChild(label);
  }
}

function setView(next) {
  view = next;
  document.getElementById("map-wrap").hidden = next !== "map";
  document.getElementById("list-wrap").hidden = next !== "list";
  for (const [id, name] of [["view-map", "map"], ["view-list", "list"]]) {
    const btn = document.getElementById(id);
    btn.classList.toggle("on", next === name);
    btn.setAttribute("aria-pressed", String(next === name));
  }
  // Leaflet measures the container on creation; if it was hidden then, it
  // thinks the map is 0x0 and draws one tile in the corner.
  if (next === "map" && map) map.invalidateSize();
}

async function load() {
  setStatus("Loading campgrounds…");
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(res.statusText);
    STATE = await res.json();
    activeStates = new Set(STATE.states);
    if (!map) initMap();
    renderRegions();
    render();

    const stale = STATE.counts.stale || 0;
    if (stale > 0) {
      setStatus(
        `${stale} campground${stale === 1 ? "" : "s"} couldn't be checked — ` +
        `showing the last thing we knew. Nothing has been removed.`,
        "slow"
      );
    } else {
      setStatus(`${STATE.total} campgrounds loaded.`, "done");
    }
  } catch (err) {
    setStatus("Couldn't reach the CampgroundFinder server. Is it still running?", "slow");
  }
}

document.getElementById("search").addEventListener("input", (e) => {
  query = e.target.value;
  render();
});
document.getElementById("view-map").addEventListener("click", () => setView("map"));
document.getElementById("view-list").addEventListener("click", () => setView("list"));
document.getElementById("base-auto").addEventListener("click", () => setBasemapMode("auto"));
document.getElementById("base-topo").addEventListener("click", () => setBasemapMode("topo"));
document.getElementById("base-street").addEventListener("click", () => setBasemapMode("street"));

load();
