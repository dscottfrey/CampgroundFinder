/* CampgroundFinder — list view.
   Reads only our own server; never contacts a camping website directly. */

const LABELS = {
  available: "Open now",
  full: "Full",
  unknown: "Not checked yet",
  stale: "Couldn't reach the website",
  closed: "Closed",
};

let STATE = { campgrounds: [], states: [], counts: {}, last_checked: null };
let activeStates = new Set();
let query = "";

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

function render() {
  const rows = document.getElementById("rows");
  const q = query.trim().toLowerCase();
  const list = STATE.campgrounds.filter((c) => {
    if (activeStates.size && !activeStates.has(c.state)) return false;
    if (q && !(c.name || "").toLowerCase().includes(q)) return false;
    return true;
  });

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
  const checked = ago(STATE.last_checked);
  document.getElementById("foot").textContent =
    `Showing ${list.length} of ${STATE.total} campgrounds` +
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

async function load() {
  setStatus("Loading campgrounds…");
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(res.statusText);
    STATE = await res.json();
    activeStates = new Set(STATE.states);
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

load();
