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
/* "provider|facility_id" -> openings. **null means we don't know**, an empty
   array means we looked and found none. The two must never be conflated:
   one is a gap in our knowledge, the other is a fact about the campground. */
let OPENINGS = null;
let COVERAGE = {};

let map = null;
let markers = null;
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
let labelsFromZoom = 11;

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
  updateLabelVisibility();
  const zoom = map.getZoom();
  document.getElementById("basemap-note").textContent =
    `zoom ${zoom} · ` +
    `${shownBasemap === "topo" ? "topographic" : "street"} tiles` +
    (basemapMode === "auto" ? ` · topo from zoom ${topoFromZoom}` : " · pinned") +
    (zoom < labelsFromZoom ? ` · names from zoom ${labelsFromZoom}` : " · names on");
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
  labelsFromZoom = settings.labels_from_zoom || 11;
  basemaps.topo = makeBasemap(settings.tiles || {});
  // Falls back to the topo layer's own settings rather than to nothing: a
  // config without street tiles should lose the swap, not the background.
  basemaps.street = makeBasemap(settings.street_tiles || settings.tiles || {});
  applyBasemap();
  map.on("zoomend", applyBasemap);

  /* A plain layer group, NOT a cluster.

     Clustering was here and had to go (Scott, 2026-07-31). He zoomed out one
     step and the open campgrounds **vanished entirely** — swallowed into
     numbered cluster bubbles, so a map whose whole job is "where can I camp
     this weekend" stopped showing the three places you could. The bubbles
     also read as bare unlabelled numbers, which say nothing about status.

     His rule: **always show a dot for every campground, and let them overlap
     if they must.** Overlapping dots still convey density and still let you
     navigate; a cluster replaces the answer with a count. The wall-of-dots
     worry the clustering was added for is real but is the lesser problem —
     and it is what the zoom-based label hiding already handles. */
  markers = L.layerGroup();
  map.addLayer(markers);
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

  // What's actually open, with dates and site names — the thing the map has
  // been unable to say until now.
  const openings = openingsFor(c);
  if (openings && openings.length) box.appendChild(openingsList(openings));

  const lines = [];
  if (c.status_reason) lines.push(c.status_reason);
  if (c.status === "available" && c.open_sites > 0 && !openings) {
    lines.push(`${c.open_sites} opening${c.open_sites === 1 ? "" : "s"} in the window we've scanned`);
  }
  // The three "nothing to show" cases are three different sentences. Saying
  // "no openings" for all of them would report our own ignorance as a fact
  // about the campground (§8g).
  if (openings === null) {
    lines.push("We haven't been able to load what's open — the campground is real, the availability is unknown.");
  } else if (!openings.length && (c.status === "unknown" || c.status === "stale")) {
    lines.push("Not checked yet — this campground exists, we just haven't looked.");
  } else if (!openings.length) {
    lines.push("Nothing open in the dates we've scanned. Set a watch and we'll tell you if that changes.");
  }
  // Why we think there's water, in the words the evidence came in. A derived
  // flag that can't say why it fired is a guess wearing a badge.
  if (c.water_nearby === "yes" && c.water_evidence) {
    lines.push("Water: " + c.water_evidence);
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

/* The openings themselves: a date range, then the sites free across it.

   Grouped by (date, nights) rather than listed one row per site, because
   "Aug 1-3, 2 nights: A01, A04, B12" is one decision and twelve rows are
   twelve. Long lists are truncated with a count, never silently — "+18 more"
   is honest where showing four and stopping is not.

   textContent throughout: site names come from provider scrapes. */
const MAX_SITES_SHOWN = 6;
const MAX_RUNS_SHOWN = 5;

function openingsList(openings) {
  const wrap = document.createElement("div");
  wrap.className = "openings";

  const runs = new Map();
  for (const o of openings) {
    const key = o.available_date + "|" + o.nights;
    if (!runs.has(key)) runs.set(key, []);
    runs.get(key).push(o);
  }
  const ordered = [...runs.entries()].sort((a, b) => a[0].localeCompare(b[0]));

  for (const [key, sites] of ordered.slice(0, MAX_RUNS_SHOWN)) {
    const [date, nights] = key.split("|");
    const row = document.createElement("div");
    row.className = "opening";

    const when = document.createElement("b");
    when.textContent = `${date} · ${nights} night${nights === "1" ? "" : "s"}`;
    row.appendChild(when);

    const named = sites.map((s) => s.site_name).filter(Boolean);
    const shown = named.slice(0, MAX_SITES_SHOWN).join(", ");
    const extra = named.length - MAX_SITES_SHOWN;
    const what = document.createElement("span");
    what.className = "opening-sites";
    what.textContent = extra > 0 ? `${shown} +${extra} more` : shown;
    row.appendChild(what);

    // Only claim a fit when a site row backed it. "unknown" is left unsaid
    // here rather than rendered as a warning on every Washington opening.
    const fits = sites.filter((s) => s.length_verdict === "fits").length;
    if (fits) {
      const tag = document.createElement("span");
      tag.className = "opening-fit";
      tag.textContent = `${fits} fit your rig`;
      row.appendChild(tag);
    }
    wrap.appendChild(row);
  }

  if (ordered.length > MAX_RUNS_SHOWN) {
    const more = document.createElement("div");
    more.className = "popup-note";
    more.textContent = `+${ordered.length - MAX_RUNS_SHOWN} more date${
      ordered.length - MAX_RUNS_SHOWN === 1 ? "" : "s"}`;
    wrap.appendChild(more);
  }
  return wrap;
}

/* The name, painted by us, beside the pin.

   Neither basemap can be relied on for this: OpenTopoMap labels no campgrounds
   at all, and the street tiles label only some, in small type. That is also
   the right division of labour — a name on the basemap is a name somebody else
   holds data for, while these are ours, spelled the way the provider spells
   them.

   Built with textContent because campground names come from scrapes; passing a
   string to bindTooltip would hand a scraped name to an HTML parser. */
function labelFor(c, size) {
  const el = document.createElement("span");
  el.textContent = c.name;
  return L.tooltip({
    permanent: true,
    direction: "right",
    offset: [size / 2 + 3, 0],
    className: "pin-label",
    opacity: 1,
    // Names are long and pins are dense; a label must never swallow the click
    // that opens the thing it names.
    interactive: false,
  }).setContent(el);
}

/* Labels are hidden wholesale by a class on the map container rather than by
   unbinding them: 774 tooltips torn down and rebuilt on every zoom is a lot of
   churn for something CSS does in one line.

   This is now the ONLY decluttering the map does. Clustering used to hide
   pins as well and was removed on 2026-07-31 — hiding a *name* at low zoom
   costs a reader nothing, because the dot is still there to click; hiding the
   *dot* removed the answer. Names and dots are not the same decision. */
function updateLabelVisibility() {
  if (!map) return;
  map.getContainer().classList.toggle(
    "labels-off", map.getZoom() < labelsFromZoom
  );
}

/* Does this campground answer the filters, or is it merely still on the map?

   Returns 'match' | 'dim' | 'unknown'. Scott's rule (2026-07-31): **filters
   dim, they never hide.** Every campground keeps its dot at every zoom and
   under every filter — hiding them is the CampSage behaviour he most
   dislikes, and a map that quietly drops rows is lying about how much exists.

   `unknown` dims too, but for a different reason, and the popup says which:
   "nothing open this weekend" and "we haven't checked yet" are different
   sentences and a camper needs to be able to tell them apart. */
function matchState(c) {
  const openings = openingsFor(c);
  if (openings === null) return "unknown";   // availability never loaded
  if (openings.length) return "match";
  if (c.status === "unknown" || c.status === "stale") return "unknown";
  return "dim";
}

function renderMap() {
  if (!map) return;
  const located = visible().filter((c) => c.located && c.latitude != null);
  markers.clearLayers();
  // Matching campgrounds are added LAST so they sit on top of the dimmed
  // ones. With overlap allowed and no clustering, draw order is the only
  // thing deciding which dot wins a collision — and the one you can book
  // should never be the one underneath.
  const ordered = [...located].sort(
    (a, b) => (matchState(a) === "match" ? 1 : 0) - (matchState(b) === "match" ? 1 : 0)
  );
  for (const c of ordered) {
    const icon = pinIcon(c);
    const marker = L.marker([c.latitude, c.longitude], { icon, title: c.name })
      .bindTooltip(labelFor(c, icon.options.iconSize[0]))
      .bindPopup(() => popupFor(c));
    // 50% is Scott's starting value, to be looked at rather than trusted.
    // NOT cumulative by design: failing four filters looks like failing
    // one, because dimness answers "does this match?", not "how badly?".
    if (matchState(c) !== "match") marker.setOpacity(0.5);
    marker.addTo(markers);
  }
  updateLabelVisibility();

  // Frame the catalog once, on first draw only — refitting on every keystroke
  // would yank the map around while someone is typing.
  if (needsFit && located.length) {
    needsFit = false;
    const bounds = L.latLngBounds(located.map((c) => [c.latitude, c.longitude]));
    map.fitBounds(bounds, { padding: [20, 20] });
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

/* Openings, indexed by the campground they belong to.

   Fetched as a SECOND request rather than folded into /api/state, because the
   catalog is the thing that must always draw and availability is the thing
   that might be slow or absent. If this call fails the map still shows every
   campground — it just can't say what's open, which is `unknown`, which is a
   state this interface already knows how to show (§8g). */
async function loadOpenings() {
  try {
    const res = await fetch("/api/openings");
    if (!res.ok) throw new Error(res.statusText);
    const payload = await res.json();
    OPENINGS = new Map();
    for (const o of payload.openings) {
      const key = o.provider + "|" + o.facility_id;
      if (!OPENINGS.has(key)) OPENINGS.set(key, []);
      OPENINGS.get(key).push(o);
    }
    COVERAGE = payload.coverage || {};
  } catch (err) {
    // Deliberately not an error state. No openings loaded is "we don't know
    // what's open", not "nothing is open" — and those must never look alike.
    OPENINGS = null;
  }
}

function openingsFor(c) {
  if (!OPENINGS) return null;              // null = unknown, [] = none found
  return OPENINGS.get(c.provider + "|" + c.id) || [];
}

async function load() {
  setStatus("Loading campgrounds…");
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(res.statusText);
    STATE = await res.json();
    await loadOpenings();
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
