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

function initMap() {
  const settings = STATE.map || {};
  const tiles = settings.tiles || {};

  map = L.map("map", { preferCanvas: true });
  if (settings.center) {
    map.setView(settings.center, settings.zoom || 7);
  } else {
    // No home_base and no configured centre. Rather than invent a coordinate,
    // frame whatever we actually have once the pins are drawn.
    map.setView([45.5, -122.5], 5);
    needsFit = true;
  }

  const layer = L.tileLayer(tiles.url, {
    attribution: tiles.attribution || "",
    subdomains: tiles.subdomains || "abc",
    maxZoom: tiles.max_zoom || 17,
  });
  // A blank background must never be left to read as "there is nothing here".
  layer.on("load", () => {
    tilesEverLoaded = true;
    document.getElementById("tile-warning").hidden = true;
  });
  layer.on("tileerror", () => {
    if (!tilesEverLoaded) document.getElementById("tile-warning").hidden = false;
  });
  layer.addTo(map);

  cluster = L.markerClusterGroup({
    // 774 pins at low zoom is a wall of dots; cluster until it means something.
    maxClusterRadius: 45,
    showCoverageOnHover: false,
  });
  map.addLayer(cluster);
}

/* A pin is two parts on purpose (§8h): a dark base that says "we know about
   this campground", and a coloured core that says what we currently know about
   its availability. The base is always drawn — that is the "nothing is ever
   missing" guarantee made literal. */
function pinIcon(c) {
  const open = c.open_sites || 0;
  const size = c.status === "available" ? (open > 20 ? 20 : open > 5 ? 16 : 13) : 12;
  const el = document.createElement("span");
  el.className = `pin pin-${c.status}`;
  el.style.width = el.style.height = `${size}px`;
  return L.divIcon({
    html: el.outerHTML,
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

load();
