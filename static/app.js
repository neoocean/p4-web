/* p4-web frontend: single-page app, hash-based routing, no build step. */

"use strict";

const $ = (sel, el) => (el || document).querySelector(sel);

/* ---------- theme ---------- */

function themePref() {
  return localStorage.getItem("theme") || "auto";
}

function applyTheme() {
  const pref = themePref();
  const dark =
    pref === "dark" ||
    (pref === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", pref === "dark");
  document.documentElement.classList.toggle("light", pref === "light");
  $("#hl-light").disabled = dark;
  $("#hl-dark").disabled = !dark;
  const btn = $("#theme-btn");
  btn.innerHTML = pref === "auto" ? "&#9681;" : pref === "dark" ? "&#9790;" : "&#9728;";
  btn.title = `Theme: ${pref} (click to change)`;
}

applyTheme();
$("#theme-btn").addEventListener("click", () => {
  const order = { auto: "dark", dark: "light", light: "auto" };
  localStorage.setItem("theme", order[themePref()]);
  applyTheme();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

/* ---------- API helper ---------- */

async function api(path, options) {
  const res = await fetch(path, options);
  if (res.status === 401 && !path.startsWith("/api/login")) {
    // A 401 after we were logged in means the session expired — say so
    // and keep the current location so re-login lands back here. The
    // flag is sticky: several in-flight requests may 401 at once, and
    // only the first still sees currentUser set.
    if (currentUser !== null) sessionExpired = true;
    currentUser = null;
    showLogin();
    throw new ApiError("Not logged in", 401);
  }
  if (!res.ok) {
    let message = res.statusText;
    let raw = null;
    try {
      const detail = (await res.json()).detail;
      if (detail && typeof detail === "object") {
        message = detail.message || message;
        raw = detail.raw || null;
      } else if (detail) {
        message = detail;
      }
    } catch (e) { /* keep statusText */ }
    throw new ApiError(message, res.status, raw);
  }
  return res.json();
}

class ApiError extends Error {
  constructor(message, status, raw) {
    super(message);
    this.status = status;
    this.raw = raw;
  }
}

/* ---------- auth ---------- */

let currentUser = null;
let lastUser = null;         // remembered across an expiry to prefill the form
let sessionExpired = false;  // sticky until the next successful login

function showLogin() {
  $("#app").classList.add("hidden");
  $("#login-screen").classList.remove("hidden");
  const notice = $("#login-notice");
  if (sessionExpired) {
    notice.textContent = "Your session expired — please log in again.";
    notice.classList.remove("hidden");
  } else {
    notice.classList.add("hidden");
  }
  const userInput = $("#login-user");
  if (lastUser && !userInput.value) userInput.value = lastUser;
  (userInput.value ? $("#login-password") : userInput).focus();
}

function showApp() {
  sessionExpired = false;
  $("#login-screen").classList.add("hidden");
  $("#login-notice").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#whoami").textContent = currentUser ? `${currentUser.user} @ ${currentUser.p4port}` : "";
  route();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#login-btn");
  const errEl = $("#login-error");
  btn.disabled = true;
  errEl.classList.add("hidden");
  try {
    currentUser = await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user: $("#login-user").value,
        password: $("#login-password").value,
      }),
    });
    $("#login-password").value = "";
    lastUser = currentUser.user;
    showApp();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove("hidden");
  } finally {
    btn.disabled = false;
  }
});

$("#logout-btn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (e) { /* session may be gone */ }
  currentUser = null;
  showLogin();
});

/* ---------- topbar search ---------- */

$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#search-q").value.trim();
  if (!q) return;
  const kind = $("#search-kind").value;
  // Keep the current browse directory as the default scope.
  let scope = "";
  const bm = location.hash.match(/^#\/(?:browse|file)(\/\/[^?]*)/);
  if (bm) {
    scope = decodeURIComponent(bm[1]);
    if (location.hash.startsWith("#/file")) scope = scope.slice(0, scope.lastIndexOf("/"));
  }
  const p = new URLSearchParams({ q, kind });
  if (scope) p.set("path", scope);
  location.hash = `#/search?${p}`;
});

$("#more-menu .dropdown-toggle").addEventListener("click", (e) => e.preventDefault());

/* Whole-row tap targets: rows carrying data-href navigate unless the
   tap landed on a real control inside them. */
$("#view").addEventListener("click", (e) => {
  if (e.target.closest("a, button, input, select, label, summary, .disclosure")) return;
  const row = e.target.closest("tr[data-href]");
  if (row) location.hash = row.dataset.href;
});

/* ---------- depot tree sidebar ---------- */

const tree = {
  cache: new Map(),   // path -> [{path, name}]
  initPromise: null,
};

async function treeChildren(path) {
  if (!tree.cache.has(path)) {
    const data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
    tree.cache.set(path, data.dirs.map((d) => ({ path: d.path, name: d.name })));
  }
  return tree.cache.get(path);
}

function treeNodeHtml(d) {
  return `<div class="tnode" data-path="${esc(d.path)}">
    <div class="trow">
      <span class="ttog">&#9656;</span>
      <a class="tlink" href="#/browse${esc(d.path)}" title="${esc(d.path)}">${esc(d.name)}</a>
    </div>
    <div class="tkids hidden"></div>
  </div>`;
}

async function treeExpand(node, open) {
  const kids = node.querySelector(":scope > .tkids");
  const tog = node.querySelector(":scope > .trow .ttog");
  const willOpen = open != null ? open : kids.classList.contains("hidden");
  if (willOpen && !kids.dataset.loaded) {
    tog.innerHTML = "&#8987;";
    try {
      const dirs = await treeChildren(node.dataset.path);
      kids.innerHTML = dirs.map(treeNodeHtml).join("") ||
        '<div class="tempty">no subfolders</div>';
      kids.dataset.loaded = "1";
    } catch (e) {
      tog.innerHTML = "&#9656;";
      return;
    }
  }
  kids.classList.toggle("hidden", !willOpen);
  tog.innerHTML = willOpen ? "&#9662;" : "&#9656;";
}

function initTree() {
  if (!tree.initPromise) {
    tree.initPromise = (async () => {
      const dirs = await treeChildren("");
      $("#tree").innerHTML = dirs.map(treeNodeHtml).join("");
      $("#tree").addEventListener("click", (e) => {
        const tog = e.target.closest(".ttog");
        if (tog) treeExpand(tog.closest(".tnode"));
        // Navigating from the mobile overlay should dismiss it.
        if (e.target.closest(".tlink")) closeTreeOverlay();
      });
    })();
  }
  return tree.initPromise;
}

async function syncTreeToPath(path) {
  // Expand ancestors of the current browse path and highlight it.
  if (!path) return;
  const parts = path.slice(2).split("/");
  let acc = "";
  for (let i = 0; i < parts.length; i++) {
    acc += (i === 0 ? "//" : "/") + parts[i];
    const node = $(`#tree .tnode[data-path="${CSS.escape(acc)}"]`);
    if (!node) break;
    if (i < parts.length - 1) await treeExpand(node, true);
  }
  for (const el of document.querySelectorAll("#tree .trow.cur")) el.classList.remove("cur");
  const cur = $(`#tree .tnode[data-path="${CSS.escape(path)}"] > .trow`);
  if (cur) {
    cur.classList.add("cur");
    if (cur.scrollIntoViewIfNeeded) cur.scrollIntoViewIfNeeded();
  }
}

$("#tree-collapse").addEventListener("click", () => {
  const collapsed = $("#sidebar").classList.toggle("collapsed");
  localStorage.setItem("treeCollapsed", collapsed ? "1" : "");
  $("#tree-collapse").innerHTML = collapsed ? "&#187;" : "&#171;";
});

function closeTreeOverlay() {
  $("#sidebar").classList.remove("overlay-open");
  const bd = $("#tree-backdrop");
  if (bd) bd.remove();
}

$("#tree-fab").addEventListener("click", () => {
  const sb = $("#sidebar");
  sb.classList.remove("collapsed", "hidden");
  sb.classList.add("overlay-open");
  const bd = document.createElement("div");
  bd.id = "tree-backdrop";
  bd.className = "tree-backdrop";
  bd.addEventListener("click", closeTreeOverlay);
  document.body.appendChild(bd);
  initTree().catch(() => {});
});

function updateSidebar(hash) {
  const sb = $("#sidebar");
  const onDepot = hash.startsWith("#/browse") || hash.startsWith("#/file") || hash === "" || hash === "#/";
  closeTreeOverlay();
  sb.classList.toggle("hidden", !onDepot);
  $("#tree-fab").classList.toggle("hidden", !onDepot);
  if (!onDepot) return;
  if (localStorage.getItem("treeCollapsed")) {
    sb.classList.add("collapsed");
    $("#tree-collapse").innerHTML = "&#187;";
  }
  initTree().then(() => {
    const m = hash.match(/^#\/(?:browse|file)(\/\/[^?]*)/);
    if (m) {
      let p = decodeURIComponent(m[1]).replace(/\/+$/, "");
      if (hash.startsWith("#/file")) p = p.slice(0, p.lastIndexOf("/"));
      syncTreeToPath(p);
    }
  }).catch(() => {});
}

/* ---------- routing ---------- */

const routes = [];

function registerRoute(pattern, render) {
  routes.push({ pattern, render });
}

function route() {
  if (!currentUser) return;
  const hash = location.hash || "#/browse";
  // Each navigation gets a fresh container; a slower, superseded
  // renderer finishes into its now-detached node instead of
  // clobbering the current view (login + deep link raced before).
  const view = document.createElement("div");
  view.className = "route-container";
  $("#view").replaceChildren(view);
  updateSidebar(hash);
  const navKey = hash.startsWith("#/file") ? "browse" : null;
  for (const a of document.querySelectorAll(".topnav a")) {
    a.classList.toggle(
      "active",
      hash.startsWith("#/" + a.dataset.nav) || a.dataset.nav === navKey
    );
  }
  for (const r of routes) {
    const m = hash.match(r.pattern);
    if (m) {
      r.render(view, m);
      return;
    }
  }
  view.innerHTML = '<p class="placeholder">Not found</p>';
}

window.addEventListener("hashchange", route);

/* ---------- shared rendering helpers ---------- */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtTime(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return bytes + " B";
  const units = ["KB", "MB", "GB"];
  let v = bytes;
  let u = -1;
  do { v /= 1024; u++; } while (v >= 1024 && u < units.length - 1);
  return v.toFixed(v < 10 ? 1 : 0) + " " + units[u];
}

function breadcrumbs(path, routePrefix) {
  // path: "//depot/a/b" -> Root / depot / a / b, all but last are links
  const parts = path ? path.slice(2).split("/") : [];
  const crumbs = ['<a href="#/browse">depots</a>'];
  let acc = "/";
  for (let i = 0; i < parts.length; i++) {
    acc += "/" + parts[i];
    if (i === parts.length - 1 && routePrefix === null) {
      crumbs.push(`<span class="crumb-here">${esc(parts[i])}</span>`);
    } else {
      crumbs.push(`<a href="#/browse${esc(acc)}">${esc(parts[i])}</a>`);
    }
  }
  return `<nav class="breadcrumbs">${crumbs.join('<span class="crumb-sep">/</span>')}</nav>`;
}

function errorBoxHtml(err) {
  const raw = err && err.raw
    ? `<details class="error-raw"><summary>p4 output</summary><pre>${esc(err.raw)}</pre></details>`
    : "";
  return `<div class="error-box"><p class="error-msg">${esc(err.message)}</p>${raw}</div>`;
}

function renderError(view, err) {
  view.innerHTML = `<div class="pane">${errorBoxHtml(err)}</div>`;
}

function spinner(view) {
  view.innerHTML = '<p class="placeholder">Loading…</p>';
}

/* ---------- depot browser ---------- */

/* ---------- favorites ---------- */

let favoritesPromise = null;

function loadFavorites(force) {
  if (force || !favoritesPromise) {
    favoritesPromise = api("/api/favorites").then((d) => d.favorites);
    favoritesPromise.catch(() => { favoritesPromise = null; });
  }
  return favoritesPromise;
}

async function renderFavoritesDash(container) {
  let favs;
  try { favs = await loadFavorites(); } catch (e) { return; }
  if (!container.isConnected) return;
  if (!favs.length) {
    container.innerHTML = '<p class="notice">No favorites yet — open a directory and press ☆ to pin it here with its recent changes.</p>';
    return;
  }
  container.innerHTML = favs.map((f, i) => `
    <div class="fav-card" data-path="${esc(f.path)}">
      <div class="fav-head">
        <a class="fav-path mono" href="#/browse${esc(f.path)}">${esc(f.path)}</a>
        <span>
          <a class="toolbtn" href="#/changes?path=${encodeURIComponent(f.path)}">changes</a>
          <button class="toolbtn fav-remove" data-path="${esc(f.path)}" title="Remove favorite">★</button>
        </span>
      </div>
      <div class="fav-changes" id="favc-${i}"><span class="muted">Loading…</span></div>
    </div>`).join("");
  for (const btn of container.querySelectorAll(".fav-remove")) {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/favorites?path=${encodeURIComponent(btn.dataset.path)}`, { method: "DELETE" });
        loadFavorites(true);
        btn.closest(".fav-card").remove();
      } catch (err) { alert(err.message); }
    });
  }
  favs.forEach(async (f, i) => {
    const slot = $(`#favc-${i}`, container);
    try {
      const data = await api(`/api/changes?path=${encodeURIComponent(f.path)}&max=3`);
      if (!slot || !slot.isConnected) return;
      slot.innerHTML = data.changes.map((c) => `
        <a class="fav-chg" href="#/change/${c.change}">
          <span class="cl-num">${c.change}</span>
          <span class="muted">${fmtTime(c.time)}</span>
          <span class="fav-chg-desc">${esc(firstLine(c.desc))}</span>
        </a>`).join("") || '<span class="muted">No changes under this path</span>';
    } catch (e) {
      if (slot && slot.isConnected) slot.innerHTML = '<span class="muted">—</span>';
    }
  });
}

const BROWSE_SORTS = {
  name: (a, b) => a.name.localeCompare(b.name),
  rev: (a, b) => a.rev - b.rev,
  change: (a, b) => a.change - b.change,
  type: (a, b) => a.type.localeCompare(b.type),
  time: (a, b) => a.time - b.time,
};

function sortHeader(key, label, cur, dirn, extraClass) {
  const active = cur === key;
  const arrow = active ? (dirn === "desc" ? " ▾" : " ▴") : "";
  return `<th class="sortable ${extraClass || ""}" data-sort="${key}">${label}${arrow}</th>`;
}

registerRoute(/^#\/browse(\/\/[^?]*)?(\?.*)?$/, async (view, m) => {
  const [, params] = parseHashQuery(location.hash);
  const path = m[1] ? decodeURIComponent(m[1]).replace(/\/+$/, "") : "";
  const sortKey = params.get("sort") || "name";
  const sortDir = params.get("dir") || (sortKey === "name" ? "asc" : "desc");
  spinner(view);
  let data;
  try {
    data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
  } catch (err) {
    if (err.status !== 401) renderError(view, err);
    return;
  }
  const cmp = BROWSE_SORTS[sortKey] || BROWSE_SORTS.name;
  data.files.sort((a, b) => (sortDir === "desc" ? -cmp(a, b) : cmp(a, b)));
  const rows = [];
  for (const d of data.dirs) {
    rows.push(`<tr class="row-dir" data-href="#/browse${esc(d.path)}">
      <td class="cell-icon">&#128193;</td>
      <td><a href="#/browse${esc(d.path)}">${esc(d.name)}</a></td>
      <td class="hide-sm"></td><td class="hide-sm"></td><td class="hide-sm">${esc(d.desc || "")}</td><td></td>
    </tr>`);
  }
  for (const f of data.files) {
    rows.push(`<tr class="row-file" data-href="#/file${esc(f.path)}">
      <td class="cell-icon">&#128196;</td>
      <td><a href="#/file${esc(f.path)}">${esc(f.name)}</a></td>
      <td class="hide-sm">#${f.rev}</td>
      <td class="hide-sm"><a href="#/change/${f.change}">${f.change}</a></td>
      <td class="hide-sm">${esc(f.type)}</td>
      <td>${fmtTime(f.time)}</td>
    </tr>`);
  }
  view.innerHTML = `
    <div class="pane">
      <div class="pane-toolbar">
        ${breadcrumbs(path, null)}
        <span class="toolbar-btns">
          ${path ? `<button class="toolbtn" id="fav-btn" title="Favorite this path">☆</button>
          <a class="toolbtn" href="#/folderdiff?left=${encodeURIComponent(path)}">Folder diff…</a>` : ""}
        </span>
      </div>
      ${path ? "" : '<div id="fav-dash"></div>'}
      <table class="listing">
        <thead><tr><th></th>
          ${sortHeader("name", "Name", sortKey, sortDir)}
          ${sortHeader("rev", "Rev", sortKey, sortDir, "hide-sm")}
          ${sortHeader("change", "Change", sortKey, sortDir, "hide-sm")}
          ${sortHeader("type", "Type / Description", sortKey, sortDir, "hide-sm")}
          ${sortHeader("time", "Modified", sortKey, sortDir)}
        </tr></thead>
        <tbody>${rows.join("") || '<tr><td></td><td colspan="5" class="muted">Empty directory</td></tr>'}</tbody>
      </table>
    </div>`;

  for (const th of view.querySelectorAll("th.sortable")) {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      const dir = key === sortKey && sortDir !== "desc" ? "desc"
        : key === sortKey ? "asc"
        : key === "name" ? "asc" : "desc";
      const q = new URLSearchParams();
      if (key !== "name" || dir !== "asc") { q.set("sort", key); q.set("dir", dir); }
      const qs = q.toString();
      location.hash = `#/browse${path}${qs ? "?" + qs : ""}`;
    });
  }

  // Favorites: star toggle on directories, dashboard at the root.
  const favBtn = $("#fav-btn", view);
  if (favBtn) {
    loadFavorites().then((favs) => {
      const starred = favs.some((f) => f.path === path);
      favBtn.textContent = starred ? "★" : "☆";
      favBtn.classList.toggle("starred", starred);
    }).catch(() => {});
    favBtn.addEventListener("click", async () => {
      try {
        const favs = await loadFavorites();
        const starred = favs.some((f) => f.path === path);
        if (starred) {
          await api(`/api/favorites?path=${encodeURIComponent(path)}`, { method: "DELETE" });
        } else {
          await apiJson("/api/favorites", "POST", { path });
        }
        loadFavorites(true);
        favBtn.textContent = starred ? "☆" : "★";
        favBtn.classList.toggle("starred", !starred);
        toast(starred ? "Removed from favorites" : "Added to favorites");
      } catch (err) { alert(err.message); }
    });
  }
  const dash = $("#fav-dash", view);
  if (dash) renderFavoritesDash(dash);

  // Swarm-style: render the directory's README below the listing.
  const readme = data.files.find((f) => /^readme\.(md|markdown)$/i.test(f.name));
  if (readme) {
    const slot = document.createElement("div");
    slot.className = "md-panel readme-panel";
    slot.innerHTML = `<div class="readme-head mono">${esc(readme.name)}</div>`;
    view.querySelector(".pane").appendChild(slot);
    api(`/api/file?path=${encodeURIComponent(readme.path)}`).then((rf) => {
      if (rf.content != null && slot.isConnected) {
        slot.appendChild(renderMarkdown(rf.content, path));
      }
    }).catch(() => slot.remove());
  }
});

/* ---------- folder diff ---------- */

const FOLDER_STATUS_LABEL = {
  "content": "differs",
  "types": "type differs",
  "left only": "left only",
  "right only": "right only",
};

registerRoute(/^#\/folderdiff/, async (view) => {
  const [, params] = parseHashQuery(location.hash);
  const left = params.get("left") || "";
  const right = params.get("right") || "";

  const renderShell = (inner) => {
    view.innerHTML = `<div class="pane">
      <h2 class="search-title">Folder diff</h2>
      <form id="fd-form" class="filterbar">
        <label>Left <input name="left" value="${esc(left)}" placeholder="//depot/dirA[@change]" size="34" required></label>
        <label>Right <input name="right" value="${esc(right)}" placeholder="//depot/dirB[@change]" size="34" required></label>
        <button type="submit">Compare</button>
      </form>${inner}</div>`;
    $("#fd-form", view).addEventListener("submit", (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const q = new URLSearchParams({ left: fd.get("left"), right: fd.get("right") });
      const target = `#/folderdiff?${q}`;
      if (target === location.hash) route();
      else location.hash = target;
    });
  };

  if (!left || !right) {
    renderShell('<p class="notice">Enter two depot directories to compare. A side may carry a revision suffix, e.g. <span class="mono">//depot/proj@12345</span> to compare against an older point in time (same path on both sides works).</p>');
    return;
  }
  renderShell('<p class="placeholder">Comparing…</p>');
  let data;
  try {
    data = await api(`/api/folderdiff?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);
  } catch (err) {
    if (err.status !== 401) renderShell(errorBoxHtml(err));
    return;
  }
  const rows = data.pairs.map((p) => {
    let link = "";
    if (p.status === "content" || p.status === "types") {
      const q = new URLSearchParams({
        tab: "diff", spec1: `#${p.leftRev}`, spec2: `#${p.rightRev}`,
      });
      if (p.leftFile !== p.rightFile) q.set("path2", p.rightFile);
      link = `<a href="#/file${esc(p.leftFile)}?${q}">diff</a>`;
    }
    const side = (f, r) => f ? `<a href="#/file${esc(f)}">${esc(f)}</a><span class="muted">#${r}</span>` : '<span class="muted">—</span>';
    return `<tr>
      <td><span class="badge badge-${p.status === "content" || p.status === "types" ? "edit" : p.status === "left only" ? "delete" : "add"}">${esc(FOLDER_STATUS_LABEL[p.status] || p.status)}</span></td>
      <td class="mono">${side(p.leftFile, p.leftRev)}</td>
      <td class="mono">${side(p.rightFile, p.rightRev)}</td>
      <td>${link}</td>
    </tr>`;
  });
  renderShell(`
    <p class="muted search-scope">${data.pairs.length}${data.truncated ? "+" : ""} differing file${data.pairs.length === 1 ? "" : "s"}</p>
    <table class="listing">
      <thead><tr><th>Status</th><th>Left</th><th>Right</th><th></th></tr></thead>
      <tbody>${rows.join("") || '<tr><td colspan="4" class="muted">Folders are identical</td></tr>'}</tbody>
    </table>`);
});

/* ---------- file viewer ---------- */

const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "svg", "avif"]);

function isImageFile(name) {
  return IMAGE_EXTS.has(name.toLowerCase().split(".").pop());
}

const LANG_BY_EXT = {
  py: "python", js: "javascript", mjs: "javascript", ts: "typescript",
  html: "xml", htm: "xml", xml: "xml", css: "css", json: "json",
  md: "markdown", markdown: "markdown", sh: "bash", bash: "bash", zsh: "bash",
  c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp",
  java: "java", go: "go", rs: "rust", rb: "ruby", php: "php",
  yaml: "yaml", yml: "yaml", sql: "sql", cs: "csharp", swift: "swift",
  kt: "kotlin", m: "objectivec", mm: "objectivec", pl: "perl", lua: "lua",
  ini: "ini", conf: "ini", toml: "ini", dockerfile: "dockerfile",
  txt: "plaintext",
};

function highlightCode(content, filename) {
  const ext = filename.toLowerCase().split(".").pop();
  const lang = LANG_BY_EXT[ext];
  if (lang && window.hljs) {
    try {
      return hljs.highlight(content, { language: lang }).value;
    } catch (e) { /* fall through to plaintext */ }
  }
  return esc(content);
}

function renderCode(content, filename, highlightLine) {
  const html = highlightCode(content, filename);
  const lineCount = content === "" ? 0 : content.split("\n").length;
  // Drop the phantom line after a trailing newline.
  const lines = content.endsWith("\n") ? lineCount - 1 : lineCount;
  let gutter = "";
  for (let i = 1; i <= lines; i++) {
    gutter += `<span class="ln ${highlightLine === i ? "line-hl" : ""}" data-l="${i}" title="Copy link to line ${i}">${i}</span>\n`;
  }
  return `<div class="code-view" ${highlightLine ? `data-hl-line="${highlightLine}"` : ""}>
    <pre class="code-gutter">${gutter}</pre>
    <pre class="code-src"><code class="hljs">${html}</code></pre>
  </div>`;
}

function toast(msg) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 1600);
}

function highlightGutterLine(gutter, n) {
  for (const el of gutter.querySelectorAll(".line-hl")) el.classList.remove("line-hl");
  const ln = gutter.querySelector(`.ln[data-l="${n}"]`);
  if (ln) {
    ln.classList.add("line-hl");
    ln.scrollIntoView({ block: "center" });
  }
}

function bindLinePermalinks(view, path) {
  const gutter = view.querySelector(".code-gutter");
  if (!gutter) return;
  gutter.addEventListener("click", (e) => {
    const ln = e.target.closest(".ln");
    if (!ln) return;
    const n = Number(ln.dataset.l);
    const old = $("#line-menu");
    if (old) old.remove();
    const menu = document.createElement("div");
    menu.id = "line-menu";
    menu.innerHTML = `
      <button data-act="copy">Copy link to line ${n}</button>
      <button data-act="comment">Comment on line ${n}</button>`;
    const r = ln.getBoundingClientRect();
    menu.style.left = `${r.right + 6}px`;
    menu.style.top = `${r.top - 4}px`;
    document.body.appendChild(menu);
    const close = () => { menu.remove(); document.removeEventListener("click", closeOnAway, true); };
    const closeOnAway = (ev) => { if (!menu.contains(ev.target)) close(); };
    setTimeout(() => document.addEventListener("click", closeOnAway, true));
    menu.addEventListener("click", async (ev) => {
      const act = ev.target.closest("button");
      if (!act) return;
      close();
      for (const el of gutter.querySelectorAll(".line-hl")) el.classList.remove("line-hl");
      ln.classList.add("line-hl");
      if (act.dataset.act === "copy") {
        const hash = `#/file${path}?line=${n}`;
        history.replaceState(null, "", hash);
        const url = location.origin + location.pathname + hash;
        try {
          await navigator.clipboard.writeText(url);
          toast(`Link to line ${n} copied`);
        } catch (err) {
          toast(`Line ${n} — copy failed (clipboard blocked)`);
        }
      } else {
        const input = $("#cmt-line-input", view);
        const ta = $("#cmt-text", view);
        if (input) input.value = n;
        if (ta) { ta.scrollIntoView({ block: "center" }); ta.focus(); }
      }
    });
  });
}

function scrollToHighlight(view) {
  const el = view.querySelector(".line-hl");
  if (el) el.scrollIntoView({ block: "center" });
}

/* ---------- markdown rendering ---------- */

function isMarkdownFile(name) {
  return /\.(md|markdown)$/i.test(name);
}

function retryOnError(img, tries = 3) {
  /* A page full of images can momentarily overwhelm the server, and a
     failed <img> stays broken forever on its own. Retry with backoff so
     a transient hiccup doesn't cost the reader the picture. */
  let attempt = 0;
  img.addEventListener("error", () => {
    if (attempt >= tries) return;
    attempt += 1;
    const url = img.src.split("#")[0];
    setTimeout(() => { img.src = `${url}#retry${attempt}`; }, 400 * attempt);
  });
}

function splitFrontMatter(md) {
  /* A leading YAML block is metadata, not prose: marked would turn it
     into an <hr> plus a paragraph where underscores become italics. Peel
     it off so the reader gets a table instead of mangled text. */
  const m = /^﻿?---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/.exec(md);
  if (!m) return [null, md];
  const fields = [];
  for (const line of m[1].split(/\r?\n/)) {
    if (!line.trim() || /^\s*#/.test(line)) continue;
    const kv = /^([A-Za-z0-9_.-]+)\s*:\s*(.*)$/.exec(line);
    if (kv) fields.push([kv[1], kv[2].trim()]);
    else if (fields.length) {
      // Continuation: nested map entry or list item under the last key.
      const prev = fields[fields.length - 1];
      prev[1] = prev[1] ? `${prev[1]} ${line.trim()}` : line.trim();
    } else return [null, md];  // not a key/value block — leave it alone
  }
  if (!fields.length) return [null, md];
  return [fields, md.slice(m[0].length)];
}

function frontMatterHtml(fields) {
  const rows = fields
    .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v ? esc(v) : '<span class="fm-empty">—</span>'}</td></tr>`)
    .join("");
  return `<details class="md-frontmatter" open>
    <summary>Metadata</summary>
    <table><tbody>${rows}</tbody></table>
  </details>`;
}

function renderMarkdown(md, baseDir) {
  /* Depot content is untrusted: parse with marked, sanitize with
     DOMPurify, then fix up relative links/images against the file's
     directory. Returns an element. */
  const [fm, body] = splitFrontMatter(md);
  const raw = (fm ? frontMatterHtml(fm) : "") + marked.parse(body, { async: false });
  const clean = DOMPurify.sanitize(raw);
  const div = document.createElement("div");
  div.className = "md-body";
  div.innerHTML = clean;
  const isRel = (u) => u && !/^([a-z][a-z0-9+.-]*:|\/|#)/i.test(u);
  for (const img of div.querySelectorAll("img")) {
    const src = img.getAttribute("src") || "";
    if (isRel(src)) img.src = `/api/raw?path=${encodeURIComponent(baseDir + "/" + src)}`;
    img.loading = "lazy";
    retryOnError(img);
  }
  for (const a of div.querySelectorAll("a")) {
    const href = a.getAttribute("href") || "";
    if (isRel(href)) a.setAttribute("href", `#/file${baseDir}/${href}`);
    else if (/^[a-z][a-z0-9+.-]*:/i.test(href)) a.setAttribute("rel", "noopener");
  }
  for (const code of div.querySelectorAll("pre code")) {
    try { hljs.highlightElement(code); } catch (e) { /* plain text */ }
  }
  return div;
}

function parseHashQuery(hash) {
  const qIdx = hash.indexOf("?");
  if (qIdx === -1) return [hash, new URLSearchParams()];
  return [hash.slice(0, qIdx), new URLSearchParams(hash.slice(qIdx + 1))];
}

function fileTabBar(path, active, rev) {
  const revQ = rev ? `&rev=${rev}` : "";
  const tabs = [
    ["content", "Content"],
    ["history", "History"],
    ["annotate", "Annotate"],
    ["graph", "Graph"],
  ];
  return `<div class="tabbar">${tabs
    .map(
      ([key, label]) =>
        `<a class="tab ${active === key ? "active" : ""}" href="#/file${esc(path)}?tab=${key}${revQ}">${label}</a>`
    )
    .join("")}</div>`;
}

function fileHeadHtml(path, extra) {
  const name = path.split("/").pop();
  return `
    ${breadcrumbs(path.slice(0, path.lastIndexOf("/")), "browse")}
    <div class="file-head">
      <h2 class="file-name">${esc(name)}</h2>
      <div class="file-meta">${extra}</div>
    </div>`;
}

async function renderFileContent(view, path, params) {
  const rev = params.get("rev");
  const hlLine = Number(params.get("line")) || null;
  const f = await api(`/api/file?path=${encodeURIComponent(path)}${rev ? `&rev=${rev}` : ""}`);
  const name = path.split("/").pop();
  const revOptions = [];
  for (let r = f.headRev; r >= 1; r--) {
    revOptions.push(`<option value="${r}" ${r === f.rev ? "selected" : ""}>#${r}</option>`);
  }
  const rawUrl = `/api/raw?path=${encodeURIComponent(path)}${rev ? `&rev=${rev}` : ""}`;
  // Markdown: rendered view by default (Swarm-style), source on demand;
  // a ?line= link always lands on the source so the highlight is visible.
  const isMd = isMarkdownFile(name) && !f.deleted && !f.binary && !f.truncated;
  const mdRendered = isMd && !hlLine && (localStorage.getItem("mdView") || "rendered") === "rendered";
  let body;
  if (f.deleted) {
    body = '<p class="notice">This file is deleted at head revision.</p>';
  } else if (isImageFile(name)) {
    body = `<div class="img-preview"><img src="${esc(rawUrl)}" alt="${esc(name)}"></div>`;
  } else if (f.binary) {
    body = `<p class="notice">Binary file (${esc(f.type)}, ${fmtSize(f.size)}) — no inline preview.
      <a href="${esc(rawUrl)}&download=1">Download</a></p>`;
  } else if (f.truncated) {
    body = `<p class="notice">File is too large to display inline (${fmtSize(f.size)}).
      <a href="${esc(rawUrl)}&download=1">Download</a></p>`;
  } else if (mdRendered) {
    body = '<div id="md-slot" class="md-panel"></div>';
  } else {
    body = renderCode(f.content, name, hlLine);
  }
  const mdToggle = isMd ? `
    <div class="diff-toggle md-toggle">
      <button data-md="rendered" class="${mdRendered ? "on" : ""}">Rendered</button>
      <button data-md="source" class="${mdRendered ? "" : "on"}">Source</button>
    </div>` : "";
  view.innerHTML = `
    <div class="pane">
      ${fileHeadHtml(path, `
        <label>Revision <select id="rev-select">${revOptions.join("")}</select></label>
        <span>${esc(f.type)}</span>
        <span>${fmtSize(f.size)}</span>
        <span>change <a href="#/change/${f.change}">${f.change}</a></span>
        <span>${fmtTime(f.time)}</span>
        <a href="${esc(rawUrl)}" target="_blank" rel="noopener">Raw</a>
        <a href="${esc(rawUrl)}&download=1">Download</a>
        ${f.deleted ? "" : `<a href="#" id="file-edit">Edit</a>
        <a href="#" id="file-delete" class="danger-link">Delete</a>`}`)}
      ${fileTabBar(path, "content", f.rev !== f.headRev ? f.rev : null)}
      ${mdToggle}
      ${body}
      <div id="file-comments"></div>
    </div>`;
  renderCommentsPanel($("#file-comments", view), { path, rev: f.rev }, {
    onLineClick: (n) => {
      const gutter = view.querySelector(".code-gutter");
      if (gutter) highlightGutterLine(gutter, n);
      else location.hash = `#/file${path}?line=${n}`;  // markdown-rendered view
    },
  });
  if (mdRendered) {
    $("#md-slot", view).appendChild(renderMarkdown(f.content, path.slice(0, path.lastIndexOf("/"))));
  }
  for (const btn of view.querySelectorAll(".md-toggle button")) {
    btn.addEventListener("click", () => {
      localStorage.setItem("mdView", btn.dataset.md);
      route();
    });
  }
  const sel = $("#rev-select", view);
  if (sel) {
    sel.addEventListener("change", () => {
      location.hash = `#/file${path}?rev=${sel.value}`;
    });
  }
  const editBtn = $("#file-edit", view);
  if (editBtn) {
    editBtn.addEventListener("click", (e) => {
      e.preventDefault();
      openInChangelist(path, "edit");
    });
  }
  const delBtn = $("#file-delete", view);
  if (delBtn) {
    delBtn.addEventListener("click", (e) => {
      e.preventDefault();
      if (confirm(`Mark for delete?\n${path}`)) openInChangelist(path, "delete");
    });
  }
  bindLinePermalinks(view, path);
  if (hlLine) scrollToHighlight(view);
}

async function renderFileHistory(view, path) {
  const data = await api(`/api/filelog?path=${encodeURIComponent(path)}`);
  const sections = data.segments.map((seg, si) => {
    const rows = seg.revs.map((r) => {
      const revLink = `#/file${esc(seg.depotFile)}?rev=${r.rev}`;
      const diffPrev =
        r.rev > 1 && r.action !== "delete" && r.action !== "move/delete"
          ? `<a href="#/file${esc(seg.depotFile)}?tab=diff&rev1=${r.rev - 1}&rev2=${r.rev}">diff prev</a>`
          : "";
      const integRows = (r.integrations || []).map((g) => `
        <tr class="integ-row">
          <td></td>
          <td colspan="8">&#8627; ${esc(g.how)}
            <a href="#/file${esc(g.file)}" class="mono">${esc(g.file)}</a>
            <span class="muted mono">${esc(g.srev)}${g.erev && g.erev !== g.srev ? "," + esc(g.erev) : ""}</span>
          </td>
        </tr>`).join("");
      return `<tr>
        <td><input type="checkbox" class="rev-pick" data-seg="${si}" data-rev="${r.rev}"></td>
        <td><a href="${revLink}">#${r.rev}</a></td>
        <td><a href="#/change/${r.change}">${r.change}</a></td>
        <td><span class="badge badge-${esc(r.action.replace("/", "-"))}">${esc(r.action)}</span></td>
        <td class="hide-sm">${esc(r.user)}</td>
        <td>${fmtTime(r.time)}</td>
        <td class="hide-sm">${r.size != null ? fmtSize(r.size) : ""}</td>
        <td class="desc-cell" title="${esc(r.desc)}">${esc(firstLine(r.desc))}</td>
        <td>${diffPrev}</td>
      </tr>${integRows}`;
    });
    const caption =
      data.segments.length > 1 || seg.depotFile !== path
        ? `<p class="segment-label">${esc(seg.depotFile)}</p>`
        : "";
    return `${caption}
      <table class="listing">
        <thead><tr><th></th><th>Rev</th><th>Change</th><th>Action</th><th class="hide-sm">User</th><th>Date</th><th class="hide-sm">Size</th><th>Description</th><th></th></tr></thead>
        <tbody>${rows.join("")}</tbody>
      </table>`;
  });
  view.innerHTML = `
    <div class="pane">
      ${fileHeadHtml(path, `<button id="diff-selected" class="load-more slim" disabled>Diff selected revisions</button>`)}
      ${fileTabBar(path, "history", null)}
      ${sections.join("")}
    </div>`;

  const btn = $("#diff-selected", view);
  const picks = () => [...view.querySelectorAll(".rev-pick:checked")];
  view.addEventListener("change", (e) => {
    if (!e.target.classList.contains("rev-pick")) return;
    const checked = picks();
    if (checked.length > 2) e.target.checked = false;
    btn.disabled = picks().length !== 2;
  });
  btn.addEventListener("click", () => {
    const [a, b] = picks();
    if (!a || !b) return;
    const segA = data.segments[a.dataset.seg], segB = data.segments[b.dataset.seg];
    let r1 = { seg: segA, rev: Number(a.dataset.rev) };
    let r2 = { seg: segB, rev: Number(b.dataset.rev) };
    if (segA === segB && r1.rev > r2.rev) [r1, r2] = [r2, r1];
    const q = new URLSearchParams({ tab: "diff", rev1: r1.rev, rev2: r2.rev });
    if (r1.seg.depotFile !== r2.seg.depotFile) {
      // Cross-rename diff: left side is the older segment's path.
      q.set("path2", r2.seg.depotFile);
      location.hash = `#/file${r1.seg.depotFile}?${q}`;
      return;
    }
    location.hash = `#/file${r1.seg.depotFile}?${q}`;
  });
}

async function renderFileAnnotate(view, path, params) {
  const rev = params.get("rev");
  const data = await api(`/api/annotate?path=${encodeURIComponent(path)}${rev ? `&rev=${rev}` : ""}`);
  let lastChange = null;
  let bucket = 0;
  const rows = data.lines.map((l, i) => {
    if (l.change !== lastChange) {
      bucket ^= 1;
      lastChange = l.change;
    }
    return `<tr class="ann-b${bucket}">
      <td class="ann-meta"><a href="#/change/${l.change}">${l.change}</a> ${esc(l.user)}</td>
      <td class="ann-num">${i + 1}</td>
      <td class="ann-code">${esc(l.data) || " "}</td>
    </tr>`;
  });
  // Time-lapse: slide through revisions, blame re-renders per stop.
  const slider = data.headRev > 1 ? `
    <span class="timelapse">
      <input type="range" id="ann-slider" min="1" max="${data.headRev}" value="${data.rev}">
      <span id="ann-rev-label">#${data.rev} / ${data.headRev}</span>
    </span>` : `<span>#${data.rev}</span>`;
  view.innerHTML = `
    <div class="pane">
      ${fileHeadHtml(path, slider)}
      ${fileTabBar(path, "annotate", rev)}
      <table class="annotate">${rows.join("")}</table>
    </div>`;
  const sl = $("#ann-slider", view);
  if (sl) {
    sl.addEventListener("input", () => {
      $("#ann-rev-label", view).textContent = `#${sl.value} / ${data.headRev}`;
    });
    sl.addEventListener("change", () => {
      location.hash = `#/file${path}?tab=annotate&rev=${sl.value}`;
    });
  }
}

async function renderFileDiff(view, path, params) {
  const qp = new URLSearchParams({ path });
  for (const k of ["rev1", "rev2", "spec1", "spec2", "path2"]) {
    if (params.get(k)) qp.set(k, params.get(k));
  }
  const data = await api(`/api/diff?${qp}`);
  view.innerHTML = `
    <div class="pane">
      ${fileHeadHtml(path, `<span>diff ${esc(data.spec1)} → ${esc(data.spec2)}</span>`)}
      ${fileTabBar(path, "diff", null)}
      ${diffBlockHtml(data.diff)}
    </div>`;
  bindDiffToggles(view);
}

function firstLine(s) {
  const line = (s || "").split("\n")[0];
  return line.length > 100 ? line.slice(0, 100) + "…" : line;
}

function diffViewPref() {
  return localStorage.getItem("diffView") || "unified";
}

function diffToggleHtml() {
  const pref = diffViewPref();
  return `<div class="diff-toggle">
    <button data-mode="unified" class="${pref === "unified" ? "on" : ""}">Unified</button>
    <button data-mode="split" class="${pref === "split" ? "on" : ""}">Side by side</button>
  </div>`;
}

function bindDiffToggles(container) {
  for (const btn of container.querySelectorAll(".diff-toggle button")) {
    btn.addEventListener("click", () => {
      localStorage.setItem("diffView", btn.dataset.mode);
      // Re-render every diff block that stored its raw text.
      for (const holder of document.querySelectorAll("[data-diff-raw]")) {
        holder.innerHTML = renderDiffText(holder.dataset.diffRaw);
        bindDiffToggles(holder);
      }
    });
  }
}

function diffBlockHtml(text) {
  // Wrapper that remembers the raw diff so the view toggle can re-render.
  return `<div data-diff-raw="${esc(text)}">${renderDiffText(text)}</div>`;
}

function renderDiffText(text) {
  if (!text.trim()) return '<p class="notice">No differences.</p>';
  const toggle = diffToggleHtml();
  if (diffViewPref() === "split") return toggle + renderDiffSplit(text);
  const rows = text.replace(/\n$/, "").split("\n").map((l) => {
    let cls = "ctx";
    if (l.startsWith("====")) cls = "file";
    else if (l.startsWith("@@")) cls = "hunk";
    else if (l.startsWith("+")) cls = "add";
    else if (l.startsWith("-")) cls = "del";
    return `<div class="diff-line diff-${cls}">${esc(l) || "&nbsp;"}</div>`;
  });
  return `${toggle}<div class="diff-view">${rows.join("")}</div>`;
}

function renderDiffSplit(text) {
  // Parse a unified diff into aligned left/right rows.
  const out = [];
  const push = (lcls, lno, ltext, rcls, rno, rtext) => {
    out.push(`<tr>
      <td class="sp-num">${lno ?? ""}</td><td class="sp-code sp-${lcls}">${ltext == null ? "" : esc(ltext) || " "}</td>
      <td class="sp-num">${rno ?? ""}</td><td class="sp-code sp-${rcls}">${rtext == null ? "" : esc(rtext) || " "}</td>
    </tr>`);
  };
  let lno = 0, rno = 0;
  const lines = text.replace(/\n$/, "").split("\n");
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (l.startsWith("====")) {
      out.push(`<tr><td colspan="4" class="sp-file">${esc(l)}</td></tr>`);
      i++;
    } else if (l.startsWith("@@")) {
      const m = l.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) { lno = Number(m[1]); rno = Number(m[2]); }
      out.push(`<tr><td colspan="4" class="sp-hunk">${esc(l)}</td></tr>`);
      i++;
    } else if (l.startsWith("-")) {
      // Collect a run of deletions, then pair with following additions.
      const dels = [];
      while (i < lines.length && lines[i].startsWith("-")) dels.push(lines[i++].slice(1));
      const adds = [];
      while (i < lines.length && lines[i].startsWith("+")) adds.push(lines[i++].slice(1));
      const n = Math.max(dels.length, adds.length);
      for (let k = 0; k < n; k++) {
        push(
          k < dels.length ? "del" : "empty", k < dels.length ? lno++ : null, k < dels.length ? dels[k] : null,
          k < adds.length ? "add" : "empty", k < adds.length ? rno++ : null, k < adds.length ? adds[k] : null
        );
      }
    } else if (l.startsWith("+")) {
      push("empty", null, null, "add", rno++, l.slice(1));
      i++;
    } else {
      const body = l.startsWith(" ") ? l.slice(1) : l;
      push("ctx", lno++, body, "ctx", rno++, body);
      i++;
    }
  }
  return `<div class="diff-view split-view"><table class="split-table">${out.join("")}</table></div>`;
}

/* ---------- revision graph ---------- */

const INTEG_COLORS = [
  [/branch/, "#16a34a"],
  [/merge|integrate/, "#2563eb"],
  [/copy/, "#9333ea"],
  [/move/, "#ea580c"],
  [/delete/, "#dc2626"],
  [/./, "#9ca3af"],
];

function integColor(how) {
  return INTEG_COLORS.find(([re]) => re.test(how))[1];
}

async function renderFileGraph(view, path) {
  const data = await api(`/api/filelog?path=${encodeURIComponent(path)}`);
  const MAX_LANES = 8;
  const segs = data.segments.slice(0, MAX_LANES);
  const laneW = 250, rowH = 48, left = 50, top = 64;

  const nodes = [];
  segs.forEach((seg, li) => {
    for (const r of seg.revs) nodes.push({ file: seg.depotFile, li, r });
  });
  nodes.sort((a, b) => b.r.change - a.r.change || b.r.rev - a.r.rev);
  nodes.forEach((n, i) => { n.row = i; });
  const byKey = new Map(nodes.map((n) => [`${n.file}#${n.r.rev}`, n]));
  const X = (li) => left + li * laneW;
  const Y = (row) => top + row * rowH;

  const svg = [];
  // lane headers + spines
  segs.forEach((seg, li) => {
    const short = seg.depotFile.length > 34 ? "…" + seg.depotFile.slice(-33) : seg.depotFile;
    svg.push(`<text x="${X(li) - 12}" y="20" class="glane"><title>${esc(seg.depotFile)}</title>${esc(short)}</text>`);
  });

  // sequential edges within a lane
  segs.forEach((seg, li) => {
    for (let i = 0; i + 1 < seg.revs.length; i++) {
      const a = byKey.get(`${seg.depotFile}#${seg.revs[i].rev}`);
      const b = byKey.get(`${seg.depotFile}#${seg.revs[i + 1].rev}`);
      if (a && b) {
        svg.push(`<line x1="${X(li)}" y1="${Y(a.row)}" x2="${X(li)}" y2="${Y(b.row)}" class="gseq"/>`);
      }
    }
  });

  // integration edges (deduped; "from" points at this rev, "into" away)
  const seen = new Set();
  for (const n of nodes) {
    for (const g of n.r.integrations || []) {
      const rev = parseInt((g.erev || "").replace("#", ""), 10);
      if (!rev) continue;
      const other = byKey.get(`${g.file}#${rev}`);
      if (!other) continue;
      const from = / from/.test(g.how) ? other : n;
      const to = / from/.test(g.how) ? n : other;
      const edgeKey = `${from.file}#${from.r.rev}>${to.file}#${to.r.rev}`;
      if (seen.has(edgeKey)) continue;
      seen.add(edgeKey);
      const x1 = X(from.li), y1 = Y(from.row), x2 = X(to.li), y2 = Y(to.row);
      const bend = x1 === x2 ? 40 : 0;
      svg.push(`<path d="M ${x1} ${y1} C ${x1 + (x2 - x1) / 2 + bend} ${y1}, ${x1 + (x2 - x1) / 2 + bend} ${y2}, ${x2} ${y2}"
        class="ginteg" stroke="${integColor(g.how)}"><title>${esc(g.how)} ${esc(g.file)}${esc(g.srev)},${esc(g.erev)}</title></path>
        <circle cx="${x2}" cy="${y2}" r="3.5" fill="${integColor(g.how)}"/>`);
    }
  }

  // nodes on top
  for (const n of nodes) {
    const x = X(n.li), y = Y(n.row);
    const tip = `#${n.r.rev} ${n.r.action} — ${n.r.user} ${fmtTime(n.r.time)}\nCL ${n.r.change}: ${firstLine(n.r.desc)}`;
    svg.push(`<a href="#/file${esc(n.file)}?rev=${n.r.rev}">
      <g class="gnode"><title>${esc(tip)}</title>
        <circle cx="${x}" cy="${y}" r="9"/>
        <text x="${x}" y="${y + 3.5}" text-anchor="middle" class="grev">${n.r.rev}</text>
        <text x="${x + 16}" y="${y - 3}" class="gmeta-top">${esc(n.r.action)}</text>
        <text x="${x + 16}" y="${y + 10}" class="gmeta">CL ${n.r.change} · ${esc(n.r.user)}</text>
      </g></a>`);
  }

  const width = left + segs.length * laneW;
  const height = top + nodes.length * rowH;
  const legend = [
    ["branch", "#16a34a"], ["merge", "#2563eb"], ["copy", "#9333ea"], ["move", "#ea580c"],
  ].map(([k, c]) => `<span class="legend-item"><span class="legend-dot" style="background:${c}"></span>${k}</span>`).join("");
  view.innerHTML = `
    <div class="pane">
      ${fileHeadHtml(path, `<span class="graph-legend">${legend}</span>`)}
      ${fileTabBar(path, "graph", null)}
      ${data.segments.length > MAX_LANES ? `<p class="notice">Showing the first ${MAX_LANES} of ${data.segments.length} related paths.</p>` : ""}
      <div class="graph-wrap">
        <svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">${svg.join("")}</svg>
      </div>
    </div>`;
}

registerRoute(/^#\/file(\/\/.*)$/, async (view, m) => {
  const [rawPath, params] = parseHashQuery(m[1]);
  const path = decodeURIComponent(rawPath);
  const tab = params.get("tab") || "content";
  spinner(view);
  try {
    if (tab === "history") await renderFileHistory(view, path);
    else if (tab === "annotate") await renderFileAnnotate(view, path, params);
    else if (tab === "diff") await renderFileDiff(view, path, params);
    else if (tab === "graph") await renderFileGraph(view, path);
    else await renderFileContent(view, path, params);
  } catch (err) {
    if (err.status !== 401) renderError(view, err);
  }
});

/* ---------- search results ---------- */

function searchFormHtml(q, kind, scope) {
  return `<form id="search-page-form" class="filterbar">
    <label>Kind
      <select name="kind">
        <option value="files" ${kind === "files" ? "selected" : ""}>Files</option>
        <option value="content" ${kind === "content" ? "selected" : ""}>Content</option>
      </select>
    </label>
    <label>Query <input name="q" value="${esc(q)}" size="24" required></label>
    <label>Scope <input name="path" value="${esc(scope)}" placeholder="//depot/dir" size="28"></label>
    <button type="submit">Search</button>
  </form>`;
}

registerRoute(/^#\/search/, async (view) => {
  const [, params] = parseHashQuery(location.hash);
  const q = params.get("q") || "";
  const kind = params.get("kind") || "files";
  const scope = params.get("path") || "";
  $("#search-q").value = q;
  $("#search-kind").value = kind;

  const renderShell = (inner) => {
    view.innerHTML = `<div class="pane">${searchFormHtml(q, kind, scope)}${inner}</div>`;
    $("#search-page-form", view).addEventListener("submit", (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const nq = new URLSearchParams({ q: fd.get("q"), kind: fd.get("kind") });
      if (fd.get("path")) nq.set("path", fd.get("path"));
      const target = `#/search?${nq}`;
      if (target === location.hash) route();
      else location.hash = target;
    });
  };

  if (!q) {
    renderShell('<p class="notice">Type a query. Content search requires a scope path; file search matches substrings of depot paths (wildcards <span class="mono">*</span> and <span class="mono">...</span> allowed).</p>');
    return;
  }
  renderShell('<p class="placeholder">Searching…</p>');
  const qp = new URLSearchParams({ q, kind });
  if (scope) qp.set("path", scope);
  let data;
  try {
    data = await api(`/api/search?${qp}`);
  } catch (err) {
    if (err.status !== 401) renderShell(errorBoxHtml(err));
    return;
  }
  const scopeNote = scope
    ? `in <a href="#/browse${esc(scope)}">${esc(scope)}</a>`
    : "in all depots";
  let body;
  if (kind === "files") {
    body = `<table class="listing">
      <thead><tr><th class="hide-sm"></th><th>File</th><th class="hide-sm">Rev</th><th class="hide-sm">Change</th><th class="hide-sm">Type</th><th class="hide-sm">Modified</th></tr></thead>
      <tbody>${data.results.map((f) => `<tr data-href="#/file${esc(f.path)}">
        <td class="cell-icon hide-sm">&#128196;</td>
        <td class="wrap-sm"><a href="#/file${esc(f.path)}">${esc(f.path)}</a></td>
        <td class="hide-sm">#${f.rev}</td>
        <td class="hide-sm"><a href="#/change/${f.change}">${f.change}</a></td>
        <td class="hide-sm">${esc(f.type)}</td>
        <td class="hide-sm">${fmtTime(f.time)}</td>
      </tr>`).join("") || '<tr><td></td><td colspan="5" class="muted">No files matched</td></tr>'}</tbody>
    </table>`;
  } else {
    const byFile = new Map();
    for (const m of data.results) {
      if (!byFile.has(m.path)) byFile.set(m.path, []);
      byFile.get(m.path).push(m);
    }
    const groups = [...byFile.entries()].map(([path, ms]) => `
      <div class="grep-group">
        <div class="grep-file"><a href="#/file${esc(path)}">${esc(path)}</a>
          <span class="muted">${ms.length} match${ms.length === 1 ? "" : "es"}</span></div>
        ${ms.map((m) => `<a class="grep-line" href="#/file${esc(path)}?line=${m.line}">
          <span class="grep-lineno">${m.line}</span><span class="grep-text">${esc(m.text)}</span>
        </a>`).join("")}
      </div>`);
    body = groups.join("") || '<p class="notice">No matches found.</p>';
  }
  renderShell(`
    <p class="muted search-scope">${data.results.length}${data.results.length >= 500 ? "+" : ""} result${data.results.length === 1 ? "" : "s"} ${scopeNote}</p>
    ${data.warnings.map((w) => `<p class="notice">${esc(w)}</p>`).join("")}
    ${body}`);
});

/* ---------- changes list ---------- */

function changeRow(c) {
  return `<tr data-href="#/change/${c.change}">
    <td><a href="#/change/${c.change}">${c.change}</a></td>
    <td>${fmtTime(c.time)}</td>
    <td class="hide-sm">${esc(c.user)}</td>
    <td class="desc-cell" title="${esc(c.desc)}">${esc(firstLine(c.desc))}</td>
  </tr>`;
}

let userDatalistPromise = null;

function ensureUserDatalist() {
  // One fetch per session feeds a <datalist> for user-filter inputs.
  if (!userDatalistPromise) {
    userDatalistPromise = api("/api/users").then((data) => {
      const dl = document.createElement("datalist");
      dl.id = "user-list";
      dl.innerHTML = data.users
        .map((u) => `<option value="${esc(u.user)}">${esc(u.fullName)}</option>`)
        .join("");
      document.body.appendChild(dl);
    }).catch(() => { userDatalistPromise = null; });
  }
  return userDatalistPromise;
}

function indexBarHtml(s, building, loading) {
  // `loading`: the shell is up but the index status hasn't answered yet.
  // Say so rather than flashing "Indexed 0 changes".
  if (loading && !s) {
    return `<div class="index-bar">
      <span class="muted">&#9889; Checking index…</span>
      <span class="index-bar-actions"></span>
    </div>`;
  }
  const built = s && s.changes > 0;
  const when = built && s.updated ? fmtTime(s.updated) : "—";
  const range = built ? ` (#${s.oldest}–#${s.newest})` : "";
  const canBackfill = built && !s.fullyBackfilled;
  let older = "";
  if (canBackfill) {
    older = _backfillRun
      ? ' · <span class="muted">indexing older history…</span>'
      : ' · <span class="muted">older history not indexed yet</span>';
  }
  const note = building ? ' · <span class="muted">building…</span>' : "";
  return `<div class="index-bar">
    <span>&#9889; Indexed ${built ? s.changes.toLocaleString() : 0} changes${range} · updated ${when}${older}${note}</span>
    <span class="index-bar-actions">
      <button type="button" id="idx-refresh" class="linklike">Refresh</button>
      ${canBackfill ? `<button type="button" id="idx-older" class="linklike">${
        _backfillRun ? "Stop" : "Index older"}</button>` : ""}
    </span>
  </div>`;
}

// "Index older" walks history backward one batch at a time instead of
// stopping after a single batch: it keeps calling refresh?backfill=1
// until the depot runs dry, the user hits Stop, or they navigate off the
// Changes page. State is module-level so a re-render doesn't lose the
// loop, and the bar is patched in place so the listing doesn't flicker
// under the user between batches.
let _backfillRun = false;
let _lastIdxStatus = null;

// Forward catch-up only; walking backward is runBackfill()'s job.
async function refreshIndex(btn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Working…";
  try {
    await api("/api/index/refresh?backfill=0", { method: "POST" });
    route();
  } catch (err) {
    btn.disabled = false;
    btn.textContent = label;
    if (err.status !== 401) alert("Index refresh failed: " + err.message);
  }
}

function bindIndexBar(view) {
  const idxR = $("#idx-refresh", view);
  if (idxR) idxR.addEventListener("click", () => refreshIndex(idxR));
  const idxO = $("#idx-older", view);
  if (idxO) {
    idxO.addEventListener("click", () => {
      if (_backfillRun) _backfillRun = false;  // Stop: loop exits after the in-flight batch
      else runBackfill();
    });
  }
}

function paintIndexBar(s) {
  if (s) _lastIdxStatus = s;
  const bar = $(".index-bar");
  if (!bar) return;
  bar.outerHTML = indexBarHtml(_lastIdxStatus, false);
  bindIndexBar(document);
}

async function runBackfill() {
  if (_backfillRun) return;
  _backfillRun = true;
  paintIndexBar(null);  // flip the button to Stop right away
  let prevOldest = null;
  let stalls = 0;
  try {
    while (_backfillRun && location.hash.startsWith("#/changes")) {
      let s;
      try {
        s = await api("/api/index/refresh?backfill=1", { method: "POST" });
      } catch (err) {
        if (err.status !== 401) alert("Index backfill failed: " + err.message);
        break;
      }
      paintIndexBar(s);
      if (s.fullyBackfilled) break;
      // A batch that leaves `oldest` where it was means p4 has no older
      // changes to give, or a concurrent refresh held the per-user lock.
      // Pause and retry once before giving up, so we neither spin nor
      // quit on a transient lock.
      if (prevOldest !== null && s.oldest >= prevOldest) {
        if (++stalls >= 2) break;
        await new Promise((r) => setTimeout(r, 1000));
      } else {
        stalls = 0;
      }
      prevOldest = s.oldest;
    }
  } finally {
    _backfillRun = false;
    // Refresh the listing once at the end so the new rows show up; the
    // per-batch updates only touched the bar.
    if (location.hash.startsWith("#/changes")) route();
  }
}

// Background index warming: kept current without the user asking. Fires
// a quick forward-catch-up refresh when the index is empty or its last
// update is stale, then re-renders once if it gained rows. Guarded so
// navigating around the app doesn't spam the server or loop on itself.
let _idxWarmAt = 0;
let _idxWarming = false;

function warmIndex(idxStatus) {
  if (_idxWarming) return;
  const now = Date.now() / 1000;
  const empty = !idxStatus || !idxStatus.changes;
  const stale = !idxStatus || !idxStatus.updated || now - idxStatus.updated > 60;
  if (!empty && !stale) return;
  if (now - _idxWarmAt < 30) return;  // don't hammer on rapid navigation
  _idxWarming = true;
  _idxWarmAt = now;
  api("/api/index/refresh?backfill=0", { method: "POST" })
    .then((s) => {
      _idxWarming = false;
      // Only re-render if we're still on Changes and the index actually
      // grew (first build, or new submits caught up).
      if (location.hash.startsWith("#/changes") && s && s.indexed_now > 0) route();
    })
    .catch(() => { _idxWarming = false; });
}

registerRoute(/^#\/changes/, async (view, m) => {
  ensureUserDatalist();
  const [, params] = parseHashQuery(location.hash);
  const status = params.get("status") || "submitted";
  const user = params.get("user") || "";
  const path = params.get("path") || "";
  const file = params.get("file") || "";
  const text = params.get("text") || "";
  const dateFrom = params.get("from") || "";
  const dateTo = params.get("to") || "";

  // Pending status and path-prefix filters can only be answered live —
  // see the engine notes on the listing load below.
  const forceLive = status === "pending" || (Boolean(path) && !file);

  // The page paints before anything is fetched: filters and the index
  // bar are pure markup, and the listing fills in underneath. Waiting on
  // the index (its status query scans a table that grows with history,
  // and a running refresh can hold it) used to keep the whole view on a
  // spinner — the user is here to search, so give them the form at once.
  const hasDates = Boolean(dateFrom || dateTo);
  const hasFilters = Boolean(user || path || file || text || hasDates || status !== "submitted");
  view.innerHTML = `
    <div class="pane">
      <button type="button" id="chg-ftoggle" class="filters-toggle">
        Filters${hasFilters ? " (on)" : ""} &#9662;
      </button>
      <form id="chg-filter" class="filterbar wrap collapsible ${hasFilters ? "open" : ""}">
        <label>Status
          <select name="status">
            <option value="submitted" ${status === "submitted" ? "selected" : ""}>Submitted</option>
            <option value="pending" ${status === "pending" ? "selected" : ""}>Pending</option>
          </select>
        </label>
        <label>User <input name="user" list="user-list" value="${esc(user)}" placeholder="any user" size="10"></label>
        <label>Path <input name="path" value="${esc(path)}" placeholder="//depot/..." size="20"></label>
        <label>Filename <input name="file" value="${esc(file)}" placeholder="touched file" size="14"></label>
        <label>Text <input name="text" value="${esc(text)}" placeholder="description contains" size="16"></label>
        <label>From <input name="from" value="${esc(dateFrom)}" placeholder="YYYY-MM-DD" pattern="[0-9]{4}-[0-9]{2}-[0-9]{2}" title="YYYY-MM-DD" size="11"></label>
        <label>To <input name="to" value="${esc(dateTo)}" placeholder="YYYY-MM-DD" pattern="[0-9]{4}-[0-9]{2}-[0-9]{2}" title="YYYY-MM-DD" size="11"></label>
        <button type="submit">Filter</button>
      </form>
      ${forceLive ? "" : indexBarHtml(_lastIdxStatus, false, !_lastIdxStatus)}
      <table class="listing">
        <thead><tr><th>Change</th><th>Date</th><th class="hide-sm">User</th><th>Description</th></tr></thead>
        <tbody id="chg-rows"><tr><td colspan="4" class="muted">Loading…</td></tr></tbody>
      </table>
      <div id="chg-more-slot"></div>
    </div>`;

  $("#chg-ftoggle", view).addEventListener("click", () => {
    $("#chg-filter", view).classList.toggle("open");
  });

  $("#chg-filter", view).addEventListener("submit", (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const nq = new URLSearchParams();
    if (fd.get("status") !== "submitted") nq.set("status", fd.get("status"));
    for (const k of ["user", "path", "file", "text", "from", "to"]) {
      if (fd.get(k)) nq.set(k, fd.get(k));
    }
    const qs = nq.toString();
    const target = `#/changes${qs ? "?" + qs : ""}`;
    if (target === location.hash) route();
    else location.hash = target;
  });

  bindIndexBar(view);

  // ---- listing, loaded after the shell is on screen ----
  //
  // Engine is chosen automatically — there is no user-facing toggle:
  //   * pending status, or a path-prefix filter without a filename, must
  //     use live p4 (the index is submitted-only and has no path search);
  //   * everything else prefers the per-user index, which spans all
  //     indexed history (live description search only covers one page)
  //     and adds filename search. A filename filter forces the index.
  //   * if the index isn't built yet we fall back to live and warm it in
  //     the background, upgrading to the index on the next render.
  //
  // The search and the status query run side by side, and the rows do
  // not wait for status: the search itself answers in milliseconds while
  // status counts rows, so the listing lands first and the bar catches
  // up. Status is only awaited when the search comes back empty, since
  // that is the one case where "no matches" and "no index yet" look the
  // same from here.
  const iq = new URLSearchParams({ max: "200" });
  if (text) iq.set("q", text);
  if (file) iq.set("file", file);
  if (user) iq.set("user", user);
  if (dateFrom) iq.set("date_from", dateFrom);
  if (dateTo) iq.set("date_to", dateTo);
  const statusP = forceLive ? null : api("/api/index/status");
  const indexP = forceLive ? null : api(`/api/index/search?${iq}`);
  if (statusP) statusP.catch(() => {});  // consumed below; never unhandled

  let data = null;
  let liveQ = null;
  try {
    if (indexP) {
      data = await indexP;
      if (!data.changes.length && !(await statusP.catch(() => null))?.changes) {
        data = null;  // empty because the index isn't built — use live p4
      }
    }
    if (!data) {
      liveQ = new URLSearchParams({ status, max: "100" });
      if (user) liveQ.set("user", user);
      if (path) liveQ.set("path", path);
      if (text) liveQ.set("text", text);
      if (dateFrom) liveQ.set("date_from", dateFrom);
      if (dateTo) liveQ.set("date_to", dateTo);
      data = await api(`/api/changes?${liveQ}`);
    }
  } catch (err) {
    if (err.status !== 401) renderError(view, err);
    return;
  }
  if (!view.isConnected) return;  // navigated away while the query ran

  $("#chg-rows", view).innerHTML = data.changes.map(changeRow).join("") ||
    '<tr><td colspan="4" class="muted">No changes found</td></tr>';

  // The index bar and the background warm-up trail the listing.
  if (statusP) {
    statusP.then((s) => {
      if (!view.isConnected) return;
      _lastIdxStatus = s;
      const bar = $(".index-bar", view);
      if (bar) {
        // "warming" = no index yet, so the live fallback is showing.
        bar.outerHTML = indexBarHtml(s, !(s && s.changes > 0), false);
        bindIndexBar(view);
      }
      warmIndex(s);  // keep the index current in the background (no-op when fresh)
    }).catch(() => {});
  }

  if (liveQ && data.rawCount >= data.pageSize && status === "submitted" && !hasDates) {
    $("#chg-more-slot", view).innerHTML =
      '<button id="chg-more" class="load-more">Load more</button>';
    const more = $("#chg-more", view);
    let oldest = data.oldest || 0;
    more.addEventListener("click", async () => {
      more.disabled = true;
      try {
        const mq = new URLSearchParams(liveQ);
        mq.set("before", oldest);
        const page = await api(`/api/changes?${mq}`);
        $("#chg-rows", view).insertAdjacentHTML(
          "beforeend", page.changes.map(changeRow).join("")
        );
        if (page.oldest) oldest = page.oldest;
        // rawCount reflects pre-filter rows: only stop when p4 itself ran dry.
        if (page.rawCount < page.pageSize) more.remove();
        else more.disabled = false;
      } catch (err) {
        more.textContent = "Error: " + err.message;
      }
    });
  }
});

/* ---------- change detail ---------- */

const DIFFABLE = new Set(["edit", "integrate", "move/add"]);

// Clicking a file path opens a readable (text/markdown) file in the
// inline viewer, or downloads a binary one directly. Mirrors the
// backend's binary-type markers (see BINARY_TYPE_MARKERS in p4.py).
function fileOpenCell(f) {
  const revSuffix = f.rev ? `<span class="muted">#${f.rev}</span>` : "";
  let href;
  if (/binary|apple|resource/i.test(f.type || "")) {
    const revQ = f.rev ? `&rev=${f.rev}` : "";
    href = esc(`/api/raw?path=${encodeURIComponent(f.path)}${revQ}&download=1`);
  } else {
    href = `#/file${esc(f.path)}${f.rev ? `?rev=${f.rev}` : ""}`;
  }
  return `<a href="${href}">${esc(f.path)}</a>${revSuffix}`;
}

registerRoute(/^#\/change\/(\d+)$/, async (view, m) => {
  const change = m[1];
  spinner(view);
  let c;
  try {
    c = await api(`/api/change/${change}`);
  } catch (err) {
    if (err.status !== 401) renderError(view, err);
    return;
  }
  const fileRows = c.files.map((f, i) => {
    const canDiff =
      (c.status === "submitted" && DIFFABLE.has(f.action) && f.rev > 1) ||
      (c.status === "pending" && c.shelved && DIFFABLE.has(f.action) && f.rev >= 1);
    const links = [];
    const openable = f.action !== "delete" && f.action !== "move/delete";
    if (c.status === "submitted" && openable) {
      links.push(`<a href="#/file${esc(f.path)}${f.rev ? `?rev=${f.rev}` : ""}">view</a>`);
    }
    links.push(`<a href="#/file${esc(f.path)}?tab=history">history</a>`);
    const pathCell =
      c.status === "submitted" && openable
        ? fileOpenCell(f)
        : `${esc(f.path)}${f.rev ? `<span class="muted">#${f.rev}</span>` : ""}`;
    return `<tr class="chg-file" data-i="${i}">
      <td class="cell-icon">${canDiff ? '<span class="disclosure" data-i="' + i + '">&#9656;</span>' : ""}</td>
      <td class="mono wrap-sm">${pathCell}</td>
      <td><span class="badge badge-${esc((f.action || "").replace("/", "-"))}">${esc(f.action)}</span></td>
      <td class="hide-sm">${esc(f.type)}</td>
      <td class="file-links">${links.join(" ")}</td>
    </tr>
    <tr class="diff-row hidden" id="diff-row-${i}"><td></td><td colspan="4"></td></tr>`;
  });
  view.innerHTML = `
    <div class="pane">
      <div class="chg-head">
        <h2>Change ${c.change}
          <span class="badge badge-${c.status === "submitted" ? "edit" : "pending"}">${esc(c.status)}${c.shelved ? " · shelved" : ""}</span>
        </h2>
        <div class="file-meta">
          <span>${esc(c.user)}@${esc(c.client)}</span>
          <span>${fmtTime(c.time)}</span>
          <span>${c.files.length} file${c.files.length === 1 ? "" : "s"}</span>
          ${c.jobs && c.jobs.length ? `<span>jobs: ${c.jobs.map((j) => `<a href="#/job/${encodeURIComponent(j)}">${esc(j)}</a>`).join(", ")}</span>` : ""}
        </div>
      </div>
      <pre class="chg-desc">${esc(c.desc)}</pre>
      <table class="listing">
        <thead><tr><th></th><th>File</th><th>Action</th><th class="hide-sm">Type</th><th></th></tr></thead>
        <tbody>${fileRows.join("") ||
          '<tr><td></td><td colspan="4" class="muted">No files</td></tr>'}</tbody>
      </table>
      <div id="chg-comments"></div>
    </div>`;
  renderCommentsPanel($("#chg-comments", view), { change: c.change });

  for (const d of view.querySelectorAll(".disclosure")) {
    d.addEventListener("click", async () => {
      const i = Number(d.dataset.i);
      const f = c.files[i];
      const row = $(`#diff-row-${i}`, view);
      const open = !row.classList.contains("hidden");
      if (open) {
        row.classList.add("hidden");
        d.innerHTML = "&#9656;";
        return;
      }
      d.innerHTML = "&#9662;";
      row.classList.remove("hidden");
      const cell = row.children[1];
      if (!cell.dataset.loaded) {
        cell.innerHTML = '<p class="muted">Loading diff…</p>';
        try {
          const dq = new URLSearchParams({ path: f.path });
          if (c.status === "pending" && c.shelved) {
            // Shelved file: base revision vs the shelf.
            dq.set("spec1", `#${f.rev}`);
            dq.set("spec2", `@=${c.change}`);
          } else {
            dq.set("rev1", f.rev - 1);
            dq.set("rev2", f.rev);
          }
          const dd = await api(`/api/diff?${dq}`);
          cell.innerHTML = diffBlockHtml(dd.diff);
          bindDiffToggles(cell);
          cell.dataset.loaded = "1";
        } catch (err) {
          cell.innerHTML = errorBoxHtml(err);
        }
      }
    });
  }
});

/* ---------- metadata browsers ---------- */

registerRoute(/^#\/labels$/, async (view) => {
  spinner(view);
  let data;
  try { data = await api("/api/labels"); }
  catch (err) { if (err.status !== 401) renderError(view, err); return; }
  view.innerHTML = `<div class="pane">
    <h2 class="search-title">Labels</h2>
    <table class="listing">
      <thead><tr><th>Label</th><th class="hide-sm">Owner</th><th>Updated</th><th>Description</th></tr></thead>
      <tbody>${data.labels.map((l) => `<tr data-href="#/label/${encodeURIComponent(l.name)}">
        <td><a href="#/label/${encodeURIComponent(l.name)}">${esc(l.name)}</a></td>
        <td class="hide-sm">${esc(l.owner)}</td>
        <td>${fmtTime(l.update)}</td>
        <td class="desc-cell" title="${esc(l.desc)}">${esc(firstLine(l.desc))}</td>
      </tr>`).join("") || '<tr><td colspan="4" class="muted">No labels</td></tr>'}</tbody>
    </table>
  </div>`;
});

registerRoute(/^#\/label\/(.+)$/, async (view, m) => {
  const name = decodeURIComponent(m[1]);
  spinner(view);
  let l;
  try { l = await api(`/api/label/${encodeURIComponent(name)}`); }
  catch (err) { if (err.status !== 401) renderError(view, err); return; }
  view.innerHTML = `<div class="pane">
    <h2 class="search-title"><a href="#/labels">Labels</a> / ${esc(l.name)}</h2>
    <div class="file-meta" style="margin-bottom:12px">
      <span>${esc(l.owner)}</span>
      <span>${esc(l.options)}</span>
      ${l.revision ? `<span>revision ${esc(String(l.revision))}</span>` : ""}
      <span>${esc(l.update)}</span>
    </div>
    ${l.desc ? `<pre class="chg-desc">${esc(l.desc)}</pre>` : ""}
    ${l.views.length ? `<pre class="chg-desc">${l.views.map(esc).join("\n")}</pre>` : ""}
    <p class="muted">${l.files.length}${l.truncated ? "+" : ""} file${l.files.length === 1 ? "" : "s"} tagged</p>
    <table class="listing">
      <thead><tr><th>File</th><th>Rev</th><th>Action</th></tr></thead>
      <tbody>${l.files.map((f) => `<tr>
        <td class="mono"><a href="#/file${esc(f.path)}">${esc(f.path)}</a></td>
        <td>#${f.rev}</td>
        <td><span class="badge badge-${esc(f.action.replace("/", "-"))}">${esc(f.action)}</span></td>
      </tr>`).join("") || '<tr><td colspan="3" class="muted">No files tagged</td></tr>'}</tbody>
    </table>
  </div>`;
});

registerRoute(/^#\/jobs$/, async (view) => {
  spinner(view);
  let data;
  try { data = await api("/api/jobs"); }
  catch (err) { if (err.status !== 401) renderError(view, err); return; }
  view.innerHTML = `<div class="pane">
    <h2 class="search-title">Jobs</h2>
    <table class="listing">
      <thead><tr><th>Job</th><th>Status</th><th class="hide-sm">User</th><th class="hide-sm">Date</th><th>Description</th></tr></thead>
      <tbody>${data.jobs.map((j) => `<tr data-href="#/job/${encodeURIComponent(j.name)}">
        <td class="wrap-sm"><a href="#/job/${encodeURIComponent(j.name)}">${esc(j.name)}</a></td>
        <td><span class="badge">${esc(j.status)}</span></td>
        <td class="hide-sm">${esc(j.user)}</td>
        <td class="hide-sm">${esc(j.date)}</td>
        <td class="desc-cell" title="${esc(j.desc)}">${esc(firstLine(j.desc))}</td>
      </tr>`).join("") || '<tr><td colspan="5" class="muted">No jobs</td></tr>'}</tbody>
    </table>
  </div>`;
});

registerRoute(/^#\/job\/(.+)$/, async (view, m) => {
  const name = decodeURIComponent(m[1]);
  spinner(view);
  let j;
  try { j = await api(`/api/job/${encodeURIComponent(name)}`); }
  catch (err) { if (err.status !== 401) renderError(view, err); return; }
  view.innerHTML = `<div class="pane">
    <h2 class="search-title"><a href="#/jobs">Jobs</a> / ${esc(j.name)}</h2>
    <div class="file-meta" style="margin-bottom:12px">
      <span class="badge">${esc(j.status)}</span>
      <span>${esc(j.user)}</span>
      <span>${esc(j.date)}</span>
    </div>
    <pre class="chg-desc">${esc(j.desc)}</pre>
    <p class="muted">${j.fixes.length} linked change${j.fixes.length === 1 ? "" : "s"}</p>
    <table class="listing">
      <thead><tr><th>Change</th><th>Date</th><th>User</th><th>Status</th></tr></thead>
      <tbody>${j.fixes.map((f) => `<tr>
        <td><a href="#/change/${f.change}">${f.change}</a></td>
        <td>${fmtTime(f.date)}</td>
        <td>${esc(f.user)}</td>
        <td>${esc(f.status)}</td>
      </tr>`).join("") || '<tr><td colspan="4" class="muted">No fixes</td></tr>'}</tbody>
    </table>
  </div>`;
});

registerRoute(/^#\/branches$/, async (view) => {
  spinner(view);
  let data;
  try { data = await api("/api/branches"); }
  catch (err) { if (err.status !== 401) renderError(view, err); return; }
  view.innerHTML = `<div class="pane">
    <h2 class="search-title">Branch mappings</h2>
    <table class="listing">
      <thead><tr><th>Branch</th><th class="hide-sm">Owner</th><th>Updated</th><th>Description</th></tr></thead>
      <tbody>${data.branches.map((b) => `<tr data-href="#/branch/${encodeURIComponent(b.name)}">
        <td class="wrap-sm"><a href="#/branch/${encodeURIComponent(b.name)}">${esc(b.name)}</a></td>
        <td class="hide-sm">${esc(b.owner)}</td>
        <td>${fmtTime(b.update)}</td>
        <td class="desc-cell" title="${esc(b.desc)}">${esc(firstLine(b.desc))}</td>
      </tr>`).join("") || '<tr><td colspan="4" class="muted">No branch mappings</td></tr>'}</tbody>
    </table>
  </div>`;
});

registerRoute(/^#\/branch\/(.+)$/, async (view, m) => {
  const name = decodeURIComponent(m[1]);
  spinner(view);
  let b;
  try { b = await api(`/api/branch/${encodeURIComponent(name)}`); }
  catch (err) { if (err.status !== 401) renderError(view, err); return; }
  view.innerHTML = `<div class="pane">
    <h2 class="search-title"><a href="#/branches">Branches</a> / ${esc(b.name)}</h2>
    <div class="file-meta" style="margin-bottom:12px">
      <span>${esc(b.owner)}</span><span>${esc(b.update)}</span>
    </div>
    ${b.desc ? `<pre class="chg-desc">${esc(b.desc)}</pre>` : ""}
    <p class="muted">View mapping</p>
    <pre class="chg-desc">${b.views.map(esc).join("\n") || "(empty)"}</pre>
  </div>`;
});

registerRoute(/^#\/streams$/, async (view) => {
  spinner(view);
  let data;
  try { data = await api("/api/streams"); }
  catch (err) { if (err.status !== 401) renderError(view, err); return; }
  view.innerHTML = `<div class="pane">
    <h2 class="search-title">Streams</h2>
    <table class="listing">
      <thead><tr><th>Stream</th><th>Name</th><th>Type</th><th>Parent</th><th>Owner</th></tr></thead>
      <tbody>${data.streams.map((s) => `<tr>
        <td class="mono"><a href="#/browse${esc(s.stream)}">${esc(s.stream)}</a></td>
        <td>${esc(s.name)}</td>
        <td>${esc(s.type)}</td>
        <td class="mono">${esc(s.parent)}</td>
        <td>${esc(s.owner)}</td>
      </tr>`).join("") || '<tr><td colspan="5" class="muted">No streams on this server</td></tr>'}</tbody>
    </table>
  </div>`;
});

registerRoute(/^#\/users$/, async (view) => {
  spinner(view);
  let data;
  try { data = await api("/api/users"); }
  catch (err) { if (err.status !== 401) renderError(view, err); return; }
  view.innerHTML = `<div class="pane">
    <h2 class="search-title">Users</h2>
    <table class="listing">
      <thead><tr><th>User</th><th>Full name</th><th class="hide-sm">Email</th><th class="hide-sm">Type</th><th class="hide-sm">Last access</th></tr></thead>
      <tbody>${data.users.map((u) => `<tr>
        <td><a href="#/changes?user=${encodeURIComponent(u.user)}">${esc(u.user)}</a></td>
        <td>${esc(u.fullName)}</td>
        <td class="hide-sm">${esc(u.email)}</td>
        <td class="hide-sm">${esc(u.type)}</td>
        <td class="hide-sm">${fmtTime(u.access)}</td>
      </tr>`).join("")}</tbody>
    </table>
    <h2 class="search-title" style="margin-top:22px">Groups</h2>
    <table class="listing">
      <thead><tr><th>Group</th><th>Members</th><th>Description</th></tr></thead>
      <tbody>${data.groups.map((g) => `<tr>
        <td>${esc(g.name)}</td>
        <td>${g.users.map(esc).join(", ")}${g.subgroups.length ? `<span class="muted"> (+ groups: ${g.subgroups.map(esc).join(", ")})</span>` : ""}</td>
        <td class="desc-cell" title="${esc(g.desc)}">${esc(firstLine(g.desc))}</td>
      </tr>`).join("") || '<tr><td colspan="3" class="muted">No groups</td></tr>'}</tbody>
    </table>
  </div>`;
});

/* ---------- inline comments ---------- */

function commentBodyHtml(c) {
  if (c.deleted) return '<p class="cmt-deleted">(comment deleted)</p>';
  const el = renderMarkdown(c.body, c.path ? c.path.slice(0, c.path.lastIndexOf("/")) : "//");
  el.classList.add("cmt-body");
  return el.outerHTML;
}

function commentHtml(c, ctx) {
  const lineTag = c.line
    ? `<a href="#" class="cmt-line" data-line="${c.line}">line ${c.line}${c.rev ? ` @#${c.rev}` : ""}</a>`
    : (c.rev ? `<span class="muted">#${c.rev}</span>` : "");
  const mine = ctx.me === c.user;
  const actions = c.deleted ? "" : `
    ${!c.parent ? `<a href="#" class="cmt-act" data-act="reply" data-id="${c.id}">reply</a>` : ""}
    ${mine ? `<a href="#" class="cmt-act" data-act="edit" data-id="${c.id}">edit</a>
    <a href="#" class="cmt-act danger-link" data-act="delete" data-id="${c.id}">delete</a>` : ""}
    ${!c.parent ? `<a href="#" class="cmt-act" data-act="resolve" data-id="${c.id}" data-resolved="${c.resolved ? 1 : 0}">${c.resolved ? "reopen" : "resolve"}</a>` : ""}`;
  return `<div class="cmt ${c.parent ? "cmt-reply" : ""}" data-id="${c.id}">
    <div class="cmt-head">
      <span class="cmt-user">${esc(c.user)}</span>
      <span class="muted">${fmtTime(Math.floor(c.created))}${c.updated ? " · edited" : ""}</span>
      ${lineTag}
      <span class="cmt-actions">${actions}</span>
    </div>
    ${commentBodyHtml(c)}
  </div>`;
}

async function renderCommentsPanel(slot, anchor, opts = {}) {
  /* anchor: {path, rev} or {change}. opts.onLineClick(line). */
  const me = currentUser ? currentUser.user : "";
  const qs = anchor.path
    ? `path=${encodeURIComponent(anchor.path)}`
    : `change=${anchor.change}`;
  let comments;
  try {
    comments = (await api(`/api/comments?${qs}`)).comments;
  } catch (e) { slot.innerHTML = ""; return; }
  if (!slot.isConnected) return;

  const roots = comments.filter((c) => !c.parent);
  const replies = (id) => comments.filter((c) => c.parent === id);
  const open = roots.filter((r) => !r.resolved);
  const resolved = roots.filter((r) => r.resolved);
  const thread = (r) =>
    `<div class="cmt-thread ${r.resolved ? "resolved" : ""}">${
      [commentHtml(r, { me }), ...replies(r.id).map((c) => commentHtml(c, { me }))].join("")
    }<div class="cmt-reply-slot" data-root="${r.id}"></div></div>`;

  slot.innerHTML = `
    <div class="cmt-panel">
      <h3 class="cmt-title">Comments <span class="muted">${open.length}${resolved.length ? ` open · ${resolved.length} resolved` : ""}</span></h3>
      ${open.map(thread).join("")}
      ${resolved.length ? `<details class="cmt-resolved"><summary>Resolved threads (${resolved.length})</summary>${resolved.map(thread).join("")}</details>` : ""}
      <form class="cmt-form" id="cmt-new">
        ${anchor.path ? `<label class="cmt-line-label">Line <input type="number" min="1" id="cmt-line-input" placeholder="—"></label>` : ""}
        <textarea id="cmt-text" rows="3" placeholder="Add a comment… (markdown supported)"></textarea>
        <button type="submit" class="toolbtn">Comment</button>
      </form>
    </div>`;

  const refresh = () => renderCommentsPanel(slot, anchor, opts);

  $("#cmt-new", slot).addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("#cmt-text", slot).value.trim();
    if (!text) return;
    const payload = { body: text };
    if (anchor.path) {
      payload.path = anchor.path;
      const line = Number(($("#cmt-line-input", slot) || {}).value) || null;
      if (line) { payload.line = line; payload.rev = anchor.rev || null; }
    } else {
      payload.change = anchor.change;
    }
    try { await apiJson("/api/comments", "POST", payload); refresh(); }
    catch (err) { alert(err.message); }
  });

  slot.addEventListener("click", async (e) => {
    const lineLink = e.target.closest(".cmt-line");
    if (lineLink) {
      e.preventDefault();
      if (opts.onLineClick) opts.onLineClick(Number(lineLink.dataset.line));
      return;
    }
    const act = e.target.closest(".cmt-act");
    if (!act) return;
    e.preventDefault();
    const id = Number(act.dataset.id);
    try {
      if (act.dataset.act === "resolve") {
        await apiJson(`/api/comments/${id}`, "PATCH", { resolved: act.dataset.resolved !== "1" });
        refresh();
      } else if (act.dataset.act === "delete") {
        if (confirm("Delete this comment?")) {
          await api(`/api/comments/${id}`, { method: "DELETE" });
          refresh();
        }
      } else if (act.dataset.act === "reply") {
        const holder = slot.querySelector(`.cmt-reply-slot[data-root="${id}"]`);
        if (holder.querySelector("form")) return;
        holder.innerHTML = `<form class="cmt-form"><textarea rows="2" placeholder="Reply…"></textarea>
          <button type="submit" class="toolbtn">Reply</button></form>`;
        holder.querySelector("form").addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const t = holder.querySelector("textarea").value.trim();
          if (!t) return;
          try { await apiJson("/api/comments", "POST", { body: t, parent: id }); refresh(); }
          catch (err) { alert(err.message); }
        });
        holder.querySelector("textarea").focus();
      } else if (act.dataset.act === "edit") {
        const cmt = act.closest(".cmt");
        const old = comments.find((c) => c.id === id);
        const bodyEl = cmt.querySelector(".cmt-body, .cmt-deleted");
        bodyEl.outerHTML = `<form class="cmt-form cmt-editing"><textarea rows="3">${esc(old.body)}</textarea>
          <button type="submit" class="toolbtn">Save</button></form>`;
        cmt.querySelector("form").addEventListener("submit", async (ev) => {
          ev.preventDefault();
          const t = cmt.querySelector("textarea").value.trim();
          if (!t) return;
          try { await apiJson(`/api/comments/${id}`, "PATCH", { body: t }); refresh(); }
          catch (err) { alert(err.message); }
        });
      }
    } catch (err) { alert(err.message); }
  });
}

/* ---------- my changes (write operations) ---------- */

function apiJson(path, method, body) {
  return api(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function pickChangelist() {
  /* Modal: choose an existing pending CL or create a new one.
     Resolves to a change number, or null on cancel. */
  const dlg = $("#cl-picker");
  const sel = $("#clp-select");
  const descWrap = $("#clp-desc-wrap");
  const descEl = $("#clp-desc");
  return new Promise(async (resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    let changes = [];
    try {
      changes = (await api("/api/my/pending")).changes;
    } catch (e) { /* dialog still usable for "new" */ }
    sel.innerHTML =
      changes.map((c) =>
        `<option value="${c.change}">${c.change} — ${esc(firstLine(c.desc)).slice(0, 60)}</option>`
      ).join("") + '<option value="new">＋ New changelist…</option>';
    descWrap.classList.toggle("hidden", sel.value !== "new");
    sel.onchange = () => descWrap.classList.toggle("hidden", sel.value !== "new");
    $("#clp-cancel").onclick = () => { dlg.close(); finish(null); };
    dlg.onclose = () => finish(null);
    $("#clp-form").onsubmit = async (e) => {
      e.preventDefault();
      let cl = sel.value;
      if (cl === "new") {
        const desc = descEl.value.trim();
        if (!desc) { descEl.focus(); return; }
        try {
          cl = (await apiJson("/api/my/pending", "POST", { description: desc })).change;
        } catch (err) {
          alert(err.message);
          return;
        }
        descEl.value = "";
      }
      dlg.onclose = null;
      dlg.close();
      finish(Number(cl));
    };
    dlg.showModal();
  });
}

function confirmSubmit(change, desc, files) {
  const dlg = $("#submit-confirm");
  $("#sc-change").textContent = change;
  $("#sc-desc").textContent = desc;
  $("#sc-files").innerHTML = files
    .map((f) => `<li><span class="badge badge-${esc(f.action.replace("/", "-"))}">${esc(f.action)}</span> <span class="mono">${esc(f.path)}</span></li>`)
    .join("");
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    $("#sc-cancel").onclick = () => { dlg.close(); finish(false); };
    dlg.onclose = () => finish(false);
    $("#sc-form").onsubmit = (e) => {
      e.preventDefault();
      dlg.onclose = null;
      dlg.close();
      finish(true);
    };
    dlg.showModal();
  });
}

async function openInChangelist(path, action) {
  /* Entry point from the file viewer: pick a CL, open the file. */
  const cl = await pickChangelist();
  if (!cl) return;
  try {
    const r = await apiJson(`/api/my/pending/${cl}/open`, "POST", { path, action });
    if (action === "edit" && !r.binary && !r.tooLarge) {
      location.hash = `#/my/${cl}/edit?path=${encodeURIComponent(path)}`;
    } else {
      location.hash = `#/my/${cl}`;
    }
  } catch (err) {
    alert(err.message);
  }
}

registerRoute(/^#\/my$/, async (view) => {
  spinner(view);
  let data;
  try { data = await api("/api/my/pending"); }
  catch (err) { if (err.status !== 401) renderError(view, err); return; }
  const cards = data.changes.map((c) => `
    <a class="cl-card" href="#/my/${c.change}">
      <div class="cl-card-head">
        <span class="cl-num">${c.change}</span>
        ${c.shelved ? '<span class="badge badge-pending">shelved</span>' : ""}
        <span class="muted">${c.files.length} file${c.files.length === 1 ? "" : "s"} · ${fmtTime(c.time)}</span>
      </div>
      <div class="cl-card-desc">${esc(firstLine(c.desc))}</div>
    </a>`);
  view.innerHTML = `
    <div class="pane">
      <div class="pane-toolbar">
        <h2 class="search-title">My pending changelists <span class="muted mono">${esc(data.client)}</span></h2>
        <button id="new-cl" class="toolbtn">＋ New changelist</button>
      </div>
      ${cards.join("") || '<p class="notice">No pending changelists. Create one, or use Edit / Delete on any file page.</p>'}
    </div>`;
  $("#new-cl", view).addEventListener("click", async () => {
    const desc = prompt("Description for the new changelist:");
    if (!desc || !desc.trim()) return;
    try {
      const r = await apiJson("/api/my/pending", "POST", { description: desc.trim() });
      location.hash = `#/my/${r.change}`;
    } catch (err) { alert(err.message); }
  });
});

registerRoute(/^#\/my\/(\d+)$/, async (view, m) => {
  const change = Number(m[1]);
  spinner(view);
  let data;
  try { data = await api("/api/my/pending"); }
  catch (err) { if (err.status !== 401) renderError(view, err); return; }
  const c = data.changes.find((x) => x.change === change);
  if (!c) {
    renderError(view, { message: `Pending changelist ${change} not found in your workspace.` });
    return;
  }
  const fileRows = c.files.map((f) => {
    const editable = f.action === "edit" || f.action === "add" || f.action === "move/add";
    return `<tr>
      <td class="mono wrap-sm">${esc(f.path)}</td>
      <td><span class="badge badge-${esc(f.action.replace("/", "-"))}">${esc(f.action)}</span></td>
      <td class="hide-sm">${esc(f.type)}</td>
      <td class="file-links">
        ${editable ? `<a href="#/my/${change}/edit?path=${encodeURIComponent(f.path)}">edit</a>
        <a href="#" class="f-upload" data-path="${esc(f.path)}">upload</a>` : ""}
        <a href="#" class="f-revert" data-path="${esc(f.path)}">revert</a>
      </td>
    </tr>`;
  });
  view.innerHTML = `
    <div class="pane">
      <h2 class="search-title"><a href="#/my">My Changes</a> / ${change}</h2>
      <div class="my-desc">
        <textarea id="cl-desc" rows="4">${esc(c.desc)}</textarea>
        <button id="save-desc" class="toolbtn">Save description</button>
      </div>
      <table class="listing">
        <thead><tr><th>File</th><th>Action</th><th class="hide-sm">Type</th><th></th></tr></thead>
        <tbody>${fileRows.join("") || '<tr><td colspan="4" class="muted">No files opened yet</td></tr>'}</tbody>
      </table>
      <div class="my-add filterbar wrap">
        <label>Depot path <input id="add-path" placeholder="//depot/dir/newfile.txt" size="36"></label>
        <button id="add-editor" class="toolbtn">New file in editor</button>
        <button id="add-upload" class="toolbtn">Upload…</button>
        <button id="open-edit" class="toolbtn">Open existing for edit</button>
        <button id="open-delete" class="toolbtn danger">Mark for delete</button>
        <input type="file" id="add-file-input" class="hidden">
      </div>
      <div class="my-actions">
        <button id="submit-cl" class="submit-btn" ${c.files.length ? "" : "disabled"}>Submit ${c.files.length} file${c.files.length === 1 ? "" : "s"}…</button>
        <button id="shelve-cl" class="toolbtn" ${c.files.length ? "" : "disabled"}>${c.shelved ? "Re-shelve" : "Shelve"} files</button>
        ${c.shelved ? `
        <button id="unshelve-cl" class="toolbtn">Unshelve</button>
        <button id="delete-shelf" class="toolbtn danger">Delete shelf</button>` : ""}
        <button id="revert-all" class="toolbtn danger" ${c.files.length ? "" : "disabled"}>Revert all</button>
        <button id="delete-cl" class="toolbtn danger" ${c.files.length || c.shelved ? "disabled" : ""}>Delete changelist</button>
      </div>
      ${c.shelved ? '<p class="muted" style="margin-top:8px">This changelist has a shelf on the server. Unshelve restores it into the workspace (overwrites unsaved edits); Delete shelf discards it.</p>' : ""}
      <div id="my-comments"></div>
    </div>`;

  renderCommentsPanel($("#my-comments", view), { change });

  const refresh = () => route();
  const pathVal = () => {
    const p = $("#add-path", view).value.trim();
    if (!p.startsWith("//")) { alert("Depot path must start with //"); return null; }
    return p;
  };

  $("#save-desc", view).addEventListener("click", async () => {
    try {
      await apiJson(`/api/my/pending/${change}`, "PATCH", { description: $("#cl-desc", view).value });
      $("#save-desc", view).textContent = "Saved ✓";
      setTimeout(() => { const b = $("#save-desc", view); if (b) b.textContent = "Save description"; }, 1500);
    } catch (err) { alert(err.message); }
  });

  $("#add-editor", view).addEventListener("click", () => {
    const p = pathVal();
    if (p) location.hash = `#/my/${change}/edit?path=${encodeURIComponent(p)}&new=1`;
  });

  $("#open-edit", view).addEventListener("click", async () => {
    const p = pathVal();
    if (!p) return;
    try {
      const r = await apiJson(`/api/my/pending/${change}/open`, "POST", { path: p, action: "edit" });
      if (!r.binary && !r.tooLarge) location.hash = `#/my/${change}/edit?path=${encodeURIComponent(p)}`;
      else refresh();
    } catch (err) { alert(err.message); }
  });

  $("#open-delete", view).addEventListener("click", async () => {
    const p = pathVal();
    if (!p || !confirm(`Mark for delete?\n${p}`)) return;
    try {
      await apiJson(`/api/my/pending/${change}/open`, "POST", { path: p, action: "delete" });
      refresh();
    } catch (err) { alert(err.message); }
  });

  const uploadTo = async (path, fileInput) => {
    const f = fileInput.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("path", path);
    fd.append("file", f);
    try {
      await api(`/api/my/pending/${change}/upload`, { method: "POST", body: fd });
      refresh();
    } catch (err) { alert(err.message); }
    fileInput.value = "";
  };

  $("#add-upload", view).addEventListener("click", () => {
    const p = pathVal();
    if (!p) return;
    const inp = $("#add-file-input", view);
    inp.onchange = () => uploadTo(p, inp);
    inp.click();
  });

  for (const a of view.querySelectorAll(".f-upload")) {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const inp = $("#add-file-input", view);
      inp.onchange = () => uploadTo(a.dataset.path, inp);
      inp.click();
    });
  }

  for (const a of view.querySelectorAll(".f-revert")) {
    a.addEventListener("click", async (e) => {
      e.preventDefault();
      if (!confirm(`Revert?\n${a.dataset.path}`)) return;
      try {
        await apiJson(`/api/my/pending/${change}/revert`, "POST", { path: a.dataset.path });
        refresh();
      } catch (err) { alert(err.message); }
    });
  }

  $("#revert-all", view).addEventListener("click", async () => {
    if (!confirm(`Revert all ${c.files.length} files in change ${change}?`)) return;
    try {
      await apiJson(`/api/my/pending/${change}/revert`, "POST", {});
      refresh();
    } catch (err) { alert(err.message); }
  });

  $("#shelve-cl", view).addEventListener("click", async () => {
    try {
      await api(`/api/my/pending/${change}/shelve`, { method: "POST" });
      refresh();
    } catch (err) { alert(err.message); }
  });

  const unshelveBtn = $("#unshelve-cl", view);
  if (unshelveBtn) {
    unshelveBtn.addEventListener("click", async () => {
      if (!confirm("Unshelve? Shelved content overwrites unsaved workspace edits.")) return;
      try {
        await api(`/api/my/pending/${change}/unshelve`, { method: "POST" });
        refresh();
      } catch (err) { alert(err.message); }
    });
  }

  const delShelfBtn = $("#delete-shelf", view);
  if (delShelfBtn) {
    delShelfBtn.addEventListener("click", async () => {
      if (!confirm(`Delete the shelf of change ${change}? The shelved copies are lost.`)) return;
      try {
        await api(`/api/my/pending/${change}/shelve`, { method: "DELETE" });
        refresh();
      } catch (err) { alert(err.message); }
    });
  }

  $("#delete-cl", view).addEventListener("click", async () => {
    if (!confirm(`Delete pending changelist ${change}?`)) return;
    try {
      await api(`/api/my/pending/${change}`, { method: "DELETE" });
      location.hash = "#/my";
    } catch (err) { alert(err.message); }
  });

  $("#submit-cl", view).addEventListener("click", async () => {
    if (!(await confirmSubmit(change, c.desc, c.files))) return;
    const btn = $("#submit-cl", view);
    btn.disabled = true;
    btn.textContent = "Submitting…";
    try {
      const r = await api(`/api/my/pending/${change}/submit`, { method: "POST" });
      location.hash = `#/change/${r.submittedChange}`;
    } catch (err) {
      alert(err.message);
      refresh();
    }
  });
});

registerRoute(/^#\/my\/(\d+)\/edit/, async (view, m) => {
  const change = Number(m[1]);
  const [, params] = parseHashQuery(location.hash);
  const path = params.get("path") || "";
  const isNew = params.get("new") === "1";
  spinner(view);
  let content = "";
  if (!isNew) {
    try {
      content = (await api(`/api/my/pending/${change}/file?path=${encodeURIComponent(path)}`)).content;
    } catch (err) {
      if (err.status !== 401) renderError(view, err);
      return;
    }
  }
  const name = path.split("/").pop();
  view.innerHTML = `
    <div class="pane editor-pane">
      <div class="pane-toolbar">
        <h2 class="search-title"><a href="#/my/${change}">CL ${change}</a> / <span class="mono">${esc(name)}</span>
          <span id="ed-dirty" class="muted"></span></h2>
        <div>
          <button id="ed-save" class="toolbtn">Save${isNew ? " (add)" : ""}</button>
        </div>
      </div>
      <p class="muted mono ed-path">${esc(path)}</p>
      <textarea id="ed-text" class="editor" spellcheck="false">${esc(content)}</textarea>
    </div>`;
  const ta = $("#ed-text", view);
  const dirty = $("#ed-dirty", view);
  ta.addEventListener("input", () => { dirty.textContent = "· unsaved"; });
  const save = async () => {
    try {
      await apiJson(`/api/my/pending/${change}/file`, "PUT", { path, content: ta.value });
      dirty.textContent = "· saved ✓";
      // After the first save the file exists; fix the URL without
      // firing hashchange (a re-render would wipe the editor state).
      if (isNew) history.replaceState(null, "", `#/my/${change}/edit?path=${encodeURIComponent(path)}`);
    } catch (err) { alert(err.message); }
  };
  $("#ed-save", view).addEventListener("click", save);
  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
    if (e.key === "Tab") {
      e.preventDefault();
      const s = ta.selectionStart;
      ta.setRangeText("    ", s, ta.selectionEnd, "end");
      dirty.textContent = "· unsaved";
    }
  });
});

/* ---------- boot ---------- */

(async function boot() {
  api("/api/info")
    .then((i) => { $("#login-port").textContent = i.p4port; })
    .catch(() => {});
  try {
    currentUser = await api("/api/me");
    lastUser = currentUser.user;
    showApp();
  } catch (e) {
    showLogin();
  }
})();
