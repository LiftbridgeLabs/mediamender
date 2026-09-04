/* mediaMender UI behaviour. Server-rendered values arrive via BOOT,
   declared in templates/index.html; everything else lives here. */
const _csrfToken = BOOT.csrfToken;
const _nativeFetch = window.fetch.bind(window);
window.fetch = function(input, init = {}) {
  const method = String(init.method || 'GET').toUpperCase();
  const url = typeof input === 'string' ? input : input.url;
  if (['POST','PUT','PATCH','DELETE'].includes(method) &&
      new URL(url, window.location.href).origin === window.location.origin) {
    init.headers = new Headers(init.headers || {});
    init.headers.set('X-CSRF-Token', _csrfToken);
  }
  return _nativeFetch(input, init);
};
const _instances = BOOT.instances;
const _configMissing = BOOT.configMissing;
const _identity = BOOT.identity;
// Rendered from src/features.py so the browser and the server cannot disagree
// about which features exist, what they are called, or whether they are on.
const FEATURE_REGISTRY = BOOT.features;
let _activeFeatures = Object.fromEntries(
  FEATURE_REGISTRY.map(feature => [feature.key, feature.enabled])
);
let _history = [], _filter = 'all', _expanded = new Set();

function applyFeatureVisibility(features = _activeFeatures) {
  _activeFeatures = {..._activeFeatures, ...features};
  document.querySelectorAll('[data-feature]').forEach(element => {
    element.hidden = _activeFeatures[element.dataset.feature] === false;
  });
}

function canAccess(permission) {
  return _identity.role === 'admin' || (_identity.permissions || []).includes('*') || (_identity.permissions || []).includes(permission);
}

function applyPermissionVisibility() {
  const mapping = {
    'nav-dashboard':'dashboard', 'nav-mediamender':'trash_removal',
    'nav-library-refresh':'library_refresh', 'nav-mark-watched':'mark_watched',
    'nav-metadata-health':'metadata_health', 'nav-timestamp-repair':'timestamp_repair',
    'nav-settings':'settings',
  };
  Object.entries(mapping).forEach(([id, permission]) => {
    const element = document.getElementById(id);
    if (element) element.hidden = !canAccess(permission);
  });
}

// ── Page routing ─────────────────────────────────────────────────────────────
// ── Routing ───────────────────────────────────────────────────────────────────
// Pages are addressable as #page or #page/subview, so the back button retraces
// steps, a reload returns where you were, and a screen can be linked to.
const PAGE_LOADERS = {
  'dashboard': () => {
    if (_activeFeatures.metadata_health) loadMetadataAuditStatus();
    if (_activeFeatures.timestamp_repair) loadRepairStatus();
    if (_activeFeatures.library_refresh) loadLibraryRefreshStatus();
    if (_activeFeatures.trash_removal) fetchStatus();
  },
  'history': () => fetchHistory(),
  'settings': () => loadSettings(),
  'timestamp-repair': () => loadRepairStatus(),
  'metadata-audit': () => loadMetadataAuditStatus(),
  'library-refresh': () => loadLibraryRefreshStatus(),
  'mark-watched': () => loadMarkWatched(),
};

const PAGES = (() => {
  const byPage = Object.fromEntries(
    FEATURE_REGISTRY.map(f => [f.page, {permission: f.key, feature: f.key}])
  );
  return {
    ...byPage,
    'dashboard': {permission: 'dashboard', feature: null},
    'history':   {permission: 'dashboard', feature: null},
    'settings':  {permission: 'settings',  feature: null},
    // Trash Removal's page id predates the feature naming; keep the old id
    // working as an alias so existing links do not break.
    'mediamender': byPage['trash-removal'] || {permission: 'trash_removal', feature: 'trash_removal'},
  };
})();

let _applyingRoute = false;

function pageIsAvailable(name) {
  const page = PAGES[name];
  if (!page) return false;
  if (page.permission && !canAccess(page.permission)) return false;
  if (page.feature && _activeFeatures[page.feature] === false) return false;
  return true;
}

function firstAvailablePage() {
  return ['dashboard', ...FEATURE_REGISTRY.map(f => f.page), 'settings']
    .find(pageIsAvailable) || 'dashboard';
}

function currentRoute() {
  const [page, sub] = decodeURIComponent(window.location.hash.slice(1)).split('/');
  return {page: page || '', sub: sub || ''};
}

function setRoute(page, sub = '', replace = false) {
  if (_applyingRoute) return;
  const hash = `#${sub ? `${page}/${sub}` : page}`;
  if (window.location.hash === hash) return;
  if (replace) history.replaceState(null, '', hash);
  else history.pushState(null, '', hash);
}

function applyRoute() {
  const {page, sub} = currentRoute();
  _applyingRoute = true;
  try {
    // A bare dashboard view name is how this app used to write its hash.
    if (page && !PAGES[page] && document.getElementById(`dashboard-panel-${page}`)) {
      showPage('dashboard', document.getElementById('nav-dashboard'));
      selectDashboardView(page);
      return;
    }
    const name = pageIsAvailable(page) ? page : firstAvailablePage();
    showPage(name, document.getElementById(`nav-${name}`));
    if (!sub) return;
    if (name === 'settings') showSettingsSection(sub, settingsNavButton(sub));
    else if (name === 'dashboard') selectDashboardView(sub);
    else if (hasFeatureTabs(name)) showFeatureTab(name, sub);
  } finally {
    _applyingRoute = false;
  }
}

function showPage(name, btn) {
  const page = PAGES[name];
  if (page && page.permission && !canAccess(page.permission)) {
    return toast('You do not have access to that page', 'fail');
  }
  if (!document.getElementById(`page-${name}`) || !pageIsAvailable(name)) {
    name = firstAvailablePage();
    btn = document.getElementById(`nav-${name}`);
  }
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(b => b.classList.remove('active'));
  document.getElementById(`page-${name}`).classList.add('active');
  (btn || document.getElementById(`nav-${name}`))?.classList.add('active');
  setRoute(name, '', true);
  if (hasFeatureTabs(name)) showFeatureTab(name, 'main');
  if (name !== 'settings') stopLogViewer();
  PAGE_LOADERS[name]?.();
}


// Mark-it-Watched
let _markWatchedData = {
  instances: [], libraries: [], instance: '', library: '', shows: [],
  page: 1, page_size: 12, pages: 1, total: 0, search: '', loaded: false,
};
let _markWatchedAbort = null;
let _markWatchedSearchTimer = null;
let _markWatchedJobTimer = null;

function markWatchedStorageKey(name) {
  return `mediamender-mark-watched-${_identity.username || 'default'}-${name}`;
}

async function loadMarkWatched(force = false) {
  const container = document.getElementById('mark-watched-libraries');
  if (!container) return;
  loadMarkWatchedJobs();
  if (!_markWatchedJobTimer) {
    _markWatchedJobTimer = setInterval(() => {
      if (document.getElementById('page-mark-watched')?.classList.contains('active')) loadMarkWatchedJobs();
    }, 4000);
  }
  if (_markWatchedData.loaded && !force) return;
  if (force) _markWatchedData.loaded = false;
  container.innerHTML = '<div class="empty-msg"><span class="spin"></span> Loading configured Plex servers&hellip;</div>';
  try {
    const response = await fetch('/api/mark-watched/options');
    const data = await readJsonResponse(response);
    if (!response.ok) throw new Error(data.error || 'Plex servers could not be loaded');
    _markWatchedData.instances = data.instances || [];
    const saved = localStorage.getItem(markWatchedStorageKey('instance')) || '';
    _markWatchedData.instance = _markWatchedData.instances.some(item => item.name === saved)
      ? saved : (_markWatchedData.instances[0]?.name || '');
    renderMarkWatchedSelectors();
    if (!_markWatchedData.instance) {
      container.innerHTML = '<div class="empty-msg">No visible Plex libraries are configured.</div>';
      return;
    }
    await loadMarkWatchedLibraryOptions(force);
  } catch (error) {
    container.innerHTML = `<div class="empty-msg">${h(error.message)}</div>`;
  }
}

function renderMarkWatchedSelectors() {
  const instance = document.getElementById('mark-watched-instance');
  const library = document.getElementById('mark-watched-library');
  const pageSize = document.getElementById('mark-watched-page-size');
  if (instance) instance.innerHTML = _markWatchedData.instances.map(item =>
    `<option value="${h(item.name)}" ${item.name===_markWatchedData.instance?'selected':''}>${h(item.name)}</option>`
  ).join('') || '<option value="">No configured servers</option>';
  if (library) library.innerHTML = _markWatchedData.libraries.map(item =>
    `<option value="${h(item.name)}" ${item.name===_markWatchedData.library?'selected':''}>${h(item.name)}</option>`
  ).join('') || '<option value="">No TV libraries on this server</option>';
  if (pageSize) pageSize.value = String(_markWatchedData.page_size);
}

async function selectMarkWatchedInstance(value) {
  _markWatchedData.instance = value;
  _markWatchedData.library = '';
  _markWatchedData.page = 1;
  _markWatchedData.libraries = [];
  localStorage.setItem(markWatchedStorageKey('instance'), value);
  renderMarkWatchedSelectors();
  await loadMarkWatchedLibraryOptions(false);
}

async function loadMarkWatchedLibraryOptions(force = false) {
  const container = document.getElementById('mark-watched-libraries');
  container.innerHTML = `<div class="empty-msg"><span class="spin"></span> Loading TV libraries from ${h(_markWatchedData.instance)}&hellip;</div>`;
  const query = new URLSearchParams({instance: _markWatchedData.instance});
  const response = await fetch(`/api/mark-watched/options?${query}`);
  const data = await readJsonResponse(response);
  if (!response.ok) {
    container.innerHTML = `<div class="empty-msg">${h(data.error || 'TV libraries could not be loaded')}</div>`;
    return;
  }
  _markWatchedData.libraries = data.libraries || [];
  const saved = localStorage.getItem(markWatchedStorageKey(`library-${_markWatchedData.instance}`)) || '';
  _markWatchedData.library = _markWatchedData.libraries.some(item => item.name === saved)
    ? saved : (_markWatchedData.libraries[0]?.name || '');
  renderMarkWatchedSelectors();
  if (!_markWatchedData.library) {
    container.innerHTML = '<div class="empty-msg">No visible TV libraries are configured for this Plex server. Movie libraries are intentionally excluded.</div>';
    document.getElementById('mark-watched-pagination-top').innerHTML = '';
    document.getElementById('mark-watched-pagination-bottom').innerHTML = '';
    return;
  }
  await loadMarkWatchedPage(force ? 1 : _markWatchedData.page);
}

async function selectMarkWatchedLibrary(value) {
  _markWatchedData.library = value;
  _markWatchedData.page = 1;
  localStorage.setItem(markWatchedStorageKey(`library-${_markWatchedData.instance}`), value);
  await loadMarkWatchedPage(1);
}

async function setMarkWatchedPageSize(value) {
  _markWatchedData.page_size = Number(value) || 12;
  await loadMarkWatchedPage(1, true);
}

function queueMarkWatchedSearch(value) {
  _markWatchedData.search = String(value || '').trim();
  _markWatchedData.page = 1;
  clearTimeout(_markWatchedSearchTimer);
  _markWatchedSearchTimer = setTimeout(() => loadMarkWatchedPage(1), 300);
}

async function loadMarkWatchedPage(page = 1, scrollToControls = false) {
  const container = document.getElementById('mark-watched-libraries');
  if (!_markWatchedData.instance || !_markWatchedData.library) return;
  if (_markWatchedAbort) _markWatchedAbort.abort();
  _markWatchedAbort = new AbortController();
  container.innerHTML = '<div class="empty-msg"><span class="spin"></span> Loading one page of shows and posters&hellip;</div>';
  const query = new URLSearchParams({
    instance: _markWatchedData.instance, library: _markWatchedData.library,
    page: String(Math.max(1, page)), page_size: String(_markWatchedData.page_size),
  });
  if (_markWatchedData.search) query.set('q', _markWatchedData.search);
  try {
    const response = await fetch(`/api/mark-watched/shows?${query}`, {signal:_markWatchedAbort.signal});
    const data = await readJsonResponse(response);
    if (!response.ok) throw new Error(data.error || 'Plex shows could not be loaded');
    Object.assign(_markWatchedData, data, {loaded:true});
    renderMarkWatchedLibraries();
    renderMarkWatchedPagination();
    if (scrollToControls) {
      document.getElementById('mark-watched-pagination-top')?.scrollIntoView({behavior:'smooth',block:'start'});
    }
  } catch (error) {
    if (error.name !== 'AbortError') container.innerHTML = `<div class="empty-msg">${h(error.message)}</div>`;
  }
}

function renderMarkWatchedLibraries() {
  const container = document.getElementById('mark-watched-libraries');
  const shows = _markWatchedData.shows || [];
  container.innerHTML = `
    <section class="mw-library">
      <div class="repair-section-heading"><h2 class="repair-section-title">${h(_markWatchedData.library)}</h2><span class="inst-chip">Plex: ${h(_markWatchedData.instance)}</span><div class="repair-section-line"></div></div>
      <div class="mw-grid">
        ${shows.map((show, showIndex) => `
          <article class="mw-card">
            ${show.poster_url ? `<img class="mw-poster" loading="lazy" src="${h(show.poster_url)}" alt="Poster for ${h(show.title)}">` : '<div class="mw-poster-empty">No poster</div>'}
            <div class="mw-card-body">
              <div class="mw-title" title="${h(show.title)}">${h(show.title)}</div>
              <div class="mw-meta">${show.year || 'Year unknown'} &middot; ${show.viewed_leaf_count}/${show.leaf_count} watched</div>
              <button class="btn btn-secondary btn-sm mw-rule ${show.rule_enabled?'on':''}" onclick="setShowRule(${showIndex},${show.rule_enabled?'false':'true'})">Auto-watch ${show.rule_enabled?'On':'Off'}</button>
              <button class="btn btn-secondary btn-sm" style="width:100%;margin-top:7px;" onclick="toggleMarkWatchedSeasons(${showIndex})">Season overrides</button>
              <button class="btn btn-warn btn-sm" style="width:100%;margin-top:7px;" onclick="applyMarkWatchedNow(${showIndex},null,${show.leaf_count || 0},this)">Mark show watched now</button>
            </div>
          </article>
          <div class="mw-seasons" id="mw-seasons-${showIndex}"></div>
        `).join('') || `<div class="empty-msg">${_markWatchedData.search?`No shows match “${h(_markWatchedData.search)}”.`:'No shows found on this page.'}</div>`}
      </div>
    </section>`;
}

function renderMarkWatchedPagination() {
  const page = Number(_markWatchedData.page || 1);
  const pages = Number(_markWatchedData.pages || 1);
  const start = _markWatchedData.total ? ((page - 1) * _markWatchedData.page_size) + 1 : 0;
  const end = Math.min(page * _markWatchedData.page_size, _markWatchedData.total || 0);
  const controls = `<button class="btn btn-secondary btn-sm" ${page<=1?'disabled':''} onclick="loadMarkWatchedPage(1,true)">First</button><button class="btn btn-secondary btn-sm" ${page<=1?'disabled':''} onclick="loadMarkWatchedPage(${page-1},true)">Previous</button><span class="mw-pagination-summary">Page ${page} of ${pages} &middot; ${start}-${end} of ${_markWatchedData.total} shows</span><button class="btn btn-secondary btn-sm" ${page>=pages?'disabled':''} onclick="loadMarkWatchedPage(${page+1},true)">Next</button><button class="btn btn-secondary btn-sm" ${page>=pages?'disabled':''} onclick="loadMarkWatchedPage(${pages},true)">Last</button>`;
  ['mark-watched-pagination-top','mark-watched-pagination-bottom'].forEach(id => {
    const target = document.getElementById(id);
    if (target) target.innerHTML = controls;
  });
}

async function setShowRule(showIndex, enabled) {
  const show = _markWatchedData.shows[showIndex];
  const response = await fetch('/api/mark-watched/rules', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({scope:'show', enabled, instance:_markWatchedData.instance,
      library:_markWatchedData.library, show_rating_key:show.rating_key}),
  });
  const data = await readJsonResponse(response);
  if (!response.ok) return toast(data.error || 'Rule could not be saved', 'fail');
  show.rule_enabled = enabled;
  renderMarkWatchedLibraries();
  toast(`${show.title}: future imports will ${enabled?'':'not '}be marked watched`, 'pass');
}

async function toggleMarkWatchedSeasons(showIndex) {
  const target = document.getElementById(`mw-seasons-${showIndex}`);
  if (target.classList.contains('open')) { target.classList.remove('open'); return; }
  const show = _markWatchedData.shows[showIndex];
  target.classList.add('open');
  target.innerHTML = '<div class="empty-msg"><span class="spin"></span> Loading seasons&hellip;</div>';
  const query = new URLSearchParams({instance:_markWatchedData.instance, library:_markWatchedData.library, show:show.rating_key});
  try {
    const response = await fetch(`/api/mark-watched/seasons?${query}`);
    const data = await readJsonResponse(response);
    if (!response.ok) throw new Error(data.error || 'Seasons could not be loaded');
    target.innerHTML = `<div class="mw-season-grid">${data.seasons.map(season => `
      <article class="mw-season">
        ${season.poster_url ? `<img loading="lazy" src="${h(season.poster_url)}" alt="Poster for ${h(season.title)}">` : ''}
        <div class="mw-season-info"><div class="mw-title">${h(season.title)}</div>
        <div class="mw-meta">${season.viewed_leaf_count}/${season.leaf_count} watched</div>
        <div class="mw-source">${season.rule.source==='season'?'Explicit season override':`Inherited from show: ${season.rule.enabled?'On':'Off'}`}</div></div>
        <div class="mw-season-actions">
          <button class="btn ${season.rule.source==='show'?'btn-primary':'btn-secondary'}" onclick="setSeasonRule(${showIndex},${season.index},null)">Inherit</button>
          <button class="btn ${season.rule.source==='season'&&season.rule.enabled?'btn-primary':'btn-secondary'}" onclick="setSeasonRule(${showIndex},${season.index},true)">On</button>
          <button class="btn ${season.rule.source==='season'&&!season.rule.enabled?'btn-primary':'btn-secondary'}" onclick="setSeasonRule(${showIndex},${season.index},false)">Off</button>
        </div>
        <button class="btn btn-warn btn-sm mw-apply-now" onclick="applyMarkWatchedNow(${showIndex},${season.index},${season.leaf_count || 0},this)">Mark season watched now</button>
      </article>`).join('') || '<div class="empty-msg">No seasons found.</div>'}</div>`;
  } catch (error) { target.innerHTML = `<div class="empty-msg">${h(error.message)}</div>`; }
}

async function setSeasonRule(showIndex, seasonIndex, enabled) {
  const show = _markWatchedData.shows[showIndex];
  const response = await fetch('/api/mark-watched/rules', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({scope:'season', enabled, season_index:seasonIndex,
      instance:_markWatchedData.instance, library:_markWatchedData.library, show_rating_key:show.rating_key}),
  });
  const data = await readJsonResponse(response);
  if (!response.ok) return toast(data.error || 'Season rule could not be saved', 'fail');
  const target = document.getElementById(`mw-seasons-${showIndex}`);
  target.classList.remove('open');
  await toggleMarkWatchedSeasons(showIndex);
  toast('Season rule saved', 'pass');
}

async function setAllMarkWatched(enabled) {
  const phrase = enabled ? 'ALL ON' : 'ALL OFF';
  if (!confirm(`${phrase} for every visible show?\n\nThis changes only future automatic rules. Existing Plex watch history will not be modified.`)) return;
  const response = await fetch('/api/mark-watched/all', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled, confirm:phrase}),
  });
  const data = await readJsonResponse(response);
  if (!response.ok) return toast(data.error || 'Bulk rule update failed', 'fail');
  toast(`${phrase}: ${data.shows} future show rules updated; Plex history unchanged`, 'pass');
  await loadMarkWatchedPage(_markWatchedData.page);
}

// A rule only governs future imports, so a show switched on today keeps every
// episode already in the library unwatched. This applies the rules you have to
// the history Plex already holds.
async function applyEnabledRulesNow(button) {
  if (!confirm(
    'Mark existing Plex episodes watched for EVERY show whose rule is on?\n\n' +
    'Only episodes Plex still counts unwatched are touched. Shows and seasons ' +
    'already fully watched are skipped without being read.\n\n' +
    'This changes real Plex watch history and cannot be undone from here. ' +
    'Seasons you have explicitly turned off are skipped.'
  )) return;
  button.dataset.label ||= button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span class="spin"></span> Queueing&hellip;';
  try {
    const response = await fetch('/api/mark-watched/apply-rules', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({confirm: 'MARK WATCHED NOW'}),
    });
    const data = await readJsonResponse(response, 'Catch up');
    if (!response.ok) throw new Error(data.error || 'Catch-up could not be queued');
    toast(data.message, 'pass');
  } catch (error) {
    toast(error.message || 'Catch-up could not be queued', 'fail');
  } finally {
    button.disabled = false;
    button.innerHTML = button.dataset.label;
    loadMarkWatchedJobs();
  }
}

async function applyMarkWatchedNow(showIndex, seasonIndex, episodeCount, button) {
  const show = _markWatchedData.shows[showIndex];
  const scope = seasonIndex === null ? 'show' : 'season';
  const label = scope === 'show' ? show.title : `${show.title} season ${seasonIndex}`;
  if (!confirm(`Mark every currently unwatched episode in ${label} as watched now?\n\nThis queues up to ${episodeCount} episodes and modifies existing Plex watch history. mediaMender cannot undo this action.`)) return;
  button.disabled = true;
  button.dataset.label ||= button.innerHTML;
  button.innerHTML = '<span class="spin"></span> queueing&hellip;';
  try {
    const payload = {
      scope, confirm:'MARK WATCHED NOW', instance:_markWatchedData.instance,
      library:_markWatchedData.library, show_rating_key:show.rating_key,
      show_title:show.title,
    };
    if (seasonIndex !== null) payload.season_index = seasonIndex;
    const response = await fetch('/api/mark-watched/apply', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload),
    });
    const data = await readJsonResponse(response);
    if (!response.ok) throw new Error(data.error || 'Manual watch update could not be queued');
    await loadMarkWatchedJobs();
    toast(`${label}: watch update queued`, 'pass');
  } catch (error) {
    toast(error.message || 'Manual watch update could not be queued', 'fail');
  } finally {
    button.disabled = false;
    button.innerHTML = button.dataset.label;
  }
}

function markWatchedJobBadge(job) {
  if (job.status === 'failed') return 'error';
  if (job.status !== 'succeeded') return 'skipped';
  // Marking nothing means opposite things for the two kinds of job. A manual
  // catch-up that marked none simply found every episode already watched,
  // which is the result you wanted. An import that matched episodes and still
  // marked none means no rule fired for it, which is a miss worth seeing.
  if (job.event?.source === 'manual') return 'success';
  return Number(job.result?.matched || 0) && !Number(job.result?.marked || 0)
    ? 'skipped' : 'success';
}

function markWatchedJobHint(job) {
  if (job.status === 'failed') return 'This job stopped without marking anything';
  if (job.status === 'waiting') return 'Plex has not scanned this episode yet';
  if (markWatchedJobBadge(job) === 'skipped' && job.status === 'succeeded') {
    return 'Plex had the episode but no watch rule was enabled for it';
  }
  if (job.status === 'succeeded') {
    return Number(job.result?.marked || 0)
      ? 'Marked watched in Plex' : 'Nothing to do; already watched in Plex';
  }
  return job.status;
}

function renderMarkWatchedJobs(data) {
  const target = document.getElementById('mark-watched-jobs');
  if (!target) return;
  const jobs = data.jobs || [];
  const workers = Number(data.workers || 0);
  const live = Number(data.live_workers || 0);
  const health = workers && live < workers
    ? `<div class="repair-warning">Only ${live} of ${workers} Mark-it-Watched workers are running. Select <strong>Run pending jobs now</strong> to restart the pool.</div>`
    : '';
  // Sonarr calling and being turned away used to look the same as Sonarr never
  // calling. Say which of the two is happening.
  const hooks = data.webhooks || {};
  const outcomes = hooks.outcomes || {};
  const imports = Number(outcomes.queued || 0) + Number(outcomes.duplicate || 0);
  const tests = Number(outcomes.test || 0);
  const refused = Number(outcomes.rejected || 0) + Number(outcomes.ignored || 0);
  let banner = '';
  if (imports) {
    banner = '';
  } else if (tests && !refused) {
    // The most common case, and the one that looks like success from Sonarr.
    banner = `<div class="repair-warning">Sonarr's <strong>Test</strong> event reaches ${h(PRODUCT_NAME)}, but no completed import has. A test only proves the callback URL is reachable. Open that connection in Sonarr and check that <strong>On File Import</strong> is enabled — a connection can pass its test with every event type switched off.</div>`;
  } else if (refused) {
    banner = `<div class="repair-warning">Sonarr has called ${hooks.total} time${hooks.total===1?'':'s'}, but nothing has been queued (${h(Object.entries(outcomes).map(([k,v])=>`${v} ${k}`).join(', '))}). The webhook log below says why each was turned away.</div>`;
  } else if (!hooks.total) {
    banner = `<div class="repair-warning">No Sonarr request has been recorded yet. This log begins at version 2.7.0, so a webhook test you ran before upgrading will not appear here — run it again to confirm the connection. Automatic rules run only when Sonarr calls after an import finishes.</div>`;
  }
  const hookRows = (hooks.recent || []).length
    ? `<details class="mw-job-log" style="margin:0 0 14px;"><summary>Sonarr webhook log &mdash; ${hooks.total} recent request${hooks.total===1?'':'s'}</summary><pre>${
        (hooks.recent || []).map(entry =>
          `${h(fmtStamp(entry.at))}  ${h((entry.outcome||'').padEnd(9))} ${h(entry.event_type||'-')}  ${h(entry.series||'')}  ${h(entry.detail||'')}`
        ).join('\n')}</pre></details>`
    : '';
  target.innerHTML = health + banner + hookRows + (jobs.length ? jobs.map(job => {
    const source = job.event?.source === 'manual' ? `Manual ${job.event?.manual?.scope || 'update'}` : 'Sonarr webhook';
    const attempts = Number(job.attempts || 0);
    const result = job.result || {};
    // A waiting job is normal progress, not a stall: say when it looks again.
    const nextCheck = job.status === 'waiting' && job.next_attempt_at
      ? ` · next check ${h(fmtIn(job.next_attempt_at))}` : '';
    const counts = result.matched !== undefined ? ` · ${Number(result.marked || 0)} marked · ${Number(result.matched || 0)} matched` : '';
    const trail = job.log || [];
    const log = trail.length
      ? `<details class="mw-job-log"><summary>${trail.length} log line${trail.length===1?'':'s'}</summary><pre>${trail.map(entry =>
          `${h(fmtStamp(entry.at))}  ${h(entry.message || '')}`).join('\n')}</pre></details>`
      : '';
    return `<div class="repair-history-item"><span class="badge ${markWatchedJobBadge(job)}" title="${h(markWatchedJobHint(job))}">${h(job.status)}</span><div><div class="repair-history-title">${h(job.event?.series?.title || 'Plex update')}</div><div class="repair-history-meta">${h(source)} · ${h(fmtAgo(job.updated_at || job.created_at))}${attempts?` · attempt ${attempts}`:''}${nextCheck}${counts}<br>${h(job.message || '')}</div>${log}</div></div>`;
  }).join('') : '<div class="empty-msg">No automatic or manual jobs yet.</div>');
}

async function retryMarkWatchedJobs(button) {
  button.dataset.label = button.dataset.label || button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span class="spin"></span> Queueing&hellip;';
  try {
    const response = await fetch('/api/mark-watched/retry', { method: 'POST' });
    const data = await readJsonResponse(response);
    if (!response.ok) throw new Error(data.error || 'Pending jobs could not be re-queued');
    toast(data.message, data.requeued ? 'pass' : '');
  } catch (error) {
    toast(error.message || 'Pending jobs could not be re-queued', 'fail');
  } finally {
    button.disabled = false;
    button.innerHTML = button.dataset.label;
    loadMarkWatchedJobs();
  }
}

async function loadMarkWatchedJobs() {
  try {
    const response = await fetch('/api/mark-watched/status');
    const data = await readJsonResponse(response);
    if (response.ok) renderMarkWatchedJobs(data);
  } catch (_) {}
}

// Plex library refresh
let _libraryRefreshStatus = null;

async function loadLibraryRefreshStatus() {
  const grid=document.getElementById('library-refresh-grid');
  try {
    const response=await fetch('/api/library-refresh/status');
    _libraryRefreshStatus=await readJsonResponse(response, 'Library Refresh');
    if (!response.ok) throw new Error(_libraryRefreshStatus.error || 'Could not load Library Refresh status');
    renderLibraryRefreshStatus(_libraryRefreshStatus);
  } catch (error) {
    if (grid) grid.innerHTML=`<div class="repair-warning">${h(error.message || 'Could not load library refresh status.')}</div>`;
  }
}

function renderLibraryRefreshStatus(status) {
  const records=status.records||{};
  const queue=status.queue||{};
  const instances=status.instances||[];
  const enabled=instances.reduce((count,instance)=>count+instance.libraries.filter(library=>library.enabled).length,0);
  const failed=Object.values(records).filter(record=>record.status==='failed').length;
  const rollup=document.getElementById('rollup-library-refresh');
  if (rollup) rollup.className=`rollup-card ${queue.running?'warn':failed?'warn':enabled?'pass':''}`;
  const rollupStatus=document.getElementById('rollup-library-refresh-status');
  const rollupCopy=document.getElementById('rollup-library-refresh-copy');
  if (rollupStatus) rollupStatus.textContent=queue.running?`Refreshing ${queue.current||0} of ${queue.total}`:enabled?`${enabled} scheduled ${enabled===1?'library':'libraries'}`:'No scheduled libraries';
  if (rollupCopy) rollupCopy.textContent=queue.running?(queue.library||'Preparing refresh queue'):'Manual refresh remains available for every configured library.';
  if (document.getElementById('rollup-refresh-enabled')) document.getElementById('rollup-refresh-enabled').textContent=enabled;
  if (document.getElementById('rollup-refresh-failed')) document.getElementById('rollup-refresh-failed').textContent=failed;
  const queueEl=document.getElementById('library-refresh-queue');
  if (queueEl) queueEl.innerHTML=queue.running?`<div class="repair-active-card"><div class="repair-active-title">Refreshing library ${Number(queue.current||0)} of ${Number(queue.total||0)}</div><div class="repair-active-meta">${h(queue.library||'Preparing queue')} &middot; ${Number(queue.completed||0)} accepted &middot; ${Number(queue.failed||0)} failed</div></div>`:queue.state==='completed_with_errors'?`<div class="repair-warning" style="border-color:var(--fail-line);color:var(--fail2);">Last queue completed with ${Number(queue.failed||0)} failed request(s). Review the history below.</div>`:'';
  const button=document.getElementById('refresh-run-enabled');
  if (button) { button.disabled=queue.running||enabled===0; button.textContent=instances.length===1?'Refresh scheduled libraries':'Refresh all scheduled'; }
  const grid=document.getElementById('library-refresh-grid');
  if (grid) grid.innerHTML=instances.map(instance=>`<section class="repair-instance-card"><div class="repair-instance-top"><div><div class="eyebrow">Plex server</div><h2 class="repair-instance-name">${h(instance.name)}</h2></div><span class="repair-status-pill ${instance.libraries.some(library=>library.enabled)?'ready':'setup'}">${instance.libraries.filter(library=>library.enabled).length} scheduled</span></div><div class="repair-library-panel">${instance.libraries.map(library=>{
    const record=records[`${instance.name}::${library.name}`]||{};
    const statusLabel=record.status==='accepted'?'Accepted by Plex':record.status==='failed'?'Request failed':'Never requested';
    return `<div class="repair-library-row"><div><div class="repair-library-name">${h(library.name)}</div><div class="repair-library-meta">${library.enabled?`${h(cronDescription(library.cron))} &middot; next ${h(library.next_run?fmtTime(library.next_run):'not scheduled')} &middot; `:'Manual only &middot; '}${h(statusLabel)}${record.requested_at?` &middot; ${h(fmtAgo(record.requested_at))}`:''}</div></div><div class="repair-library-action"><span class="badge ${record.status==='failed'?'error':record.status==='accepted'?'success':'skipped'}">${library.enabled?'scheduled':'manual'}</span><button class="btn btn-primary" onclick="runLibraryRefresh(${h(JSON.stringify(instance.name))},${h(JSON.stringify(library.name))})" ${queue.running?'disabled':''}>Refresh now</button></div></div>`;
  }).join('')}</div></section>`).join('')||'<div class="empty-msg">No Plex libraries are configured.</div>';
  const history=document.getElementById('library-refresh-history');
  if (history) history.innerHTML=(status.history||[]).length?(status.history||[]).slice(0,20).map(item=>`<div class="repair-history-item"><span class="badge ${item.status==='accepted'?'success':'error'}">${h(item.status)}</span><div><div class="repair-history-title">${h(item.instance)} / ${h(item.library)}</div><div class="repair-history-meta">${h(fmtTime(item.requested_at))} &middot; ${h(item.source||'manual')}${item.status==='accepted'?` &middot; Plex accepted HTTP ${h(item.http||'success')}`:` &middot; ${h(item.error||`HTTP ${item.http||'error'}`)}`}</div></div></div>`).join(''):'<div class="empty-msg">No refresh requests yet</div>';
  if (queue.running) setTimeout(loadLibraryRefreshStatus,1500);
}

async function runLibraryRefresh(instance,library) {
  if (!confirm(`Request a full Plex refresh for ${instance} / ${library}?\n\nPlex performs the scan asynchronously. Empty Trash will be held for this library during its configured safety period.`)) return;
  const response=await fetch('/api/library-refresh/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instance,library})});
  const data=await readJsonResponse(response);
  if (!response.ok) return toast(data.error||'Refresh could not start','fail');
  toast('Library refresh request queued','warn'); setTimeout(loadLibraryRefreshStatus,400);
}

async function runEnabledLibraryRefreshes() {
  if (!confirm('Refresh every scheduled library now?\n\nRequests are sent sequentially and each library receives its configured Empty Trash safety hold.')) return;
  const response=await fetch('/api/library-refresh/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled_only:true})});
  const data=await readJsonResponse(response);
  if (!response.ok) return toast(data.error||'Refresh queue could not start','fail');
  toast(`Queued ${data.libraries} library refresh request(s)`,'warn'); setTimeout(loadLibraryRefreshStatus,400);
}

function openLibraryRefreshSettings() {
  goToSettings('library-refresh');
}

// Plex unmatched metadata audit
let _metadataAuditStatus = null;
const _metadataExpanded = new Set();

async function loadMetadataAuditStatus() {
  const grid = document.getElementById('metadata-audit-grid');
  try {
    const response = await fetch('/api/metadata-audit/status');
    _metadataAuditStatus = await readJsonResponse(response);
    renderMetadataAudits(_metadataAuditStatus);
    updateMetadataRollup(_metadataAuditStatus);
  } catch (error) {
    if (grid) grid.innerHTML = '<div class="empty-msg">Could not load saved metadata audits.</div>';
  }
}

function renderMetadataAudits(status) {
  const grid = document.getElementById('metadata-audit-grid');
  if (!grid) return;
  const heading = document.getElementById('metadata-section-title');
  const scanAll = document.getElementById('metadata-scan-all');
  if (scanAll && !scanAll.disabled) scanAll.textContent = (status.instances || []).length === 1 ? 'Scan server' : 'Scan all servers';
  if ((status.instances || []).length === 1) {
    const instanceName = status.instances[0];
    if (heading) heading.textContent = `Libraries on ${instanceName}`;
    grid.innerHTML = renderSingleServerMetadata(status, instanceName);
    bindMetadataControls(grid);
    return;
  }
  if (heading) heading.textContent = 'Plex servers';
  grid.innerHTML = (status.instances || []).map(instanceName => {
    const audit = status.audits?.[instanceName];
    const ignoredNames = status.ignored_libraries?.[instanceName] || [];
    const ignored = new Set(ignoredNames.map(name => String(name).toLocaleLowerCase()));
    const libraries = (audit?.libraries || []).filter(library => !ignored.has(String(library.name).toLocaleLowerCase()));
    const unmatched = libraries.reduce((total, library) => total + Number(library.unmatched_count || 0), 0);
    const errors = libraries.filter(library => library.error).length;
    const total = audit ? libraries.reduce((sum, library) => sum + Number(library.total_items || 0), 0) : null;
    const expanded = _metadataExpanded.has(instanceName);
    const libraryBlocks = libraries.map(library => {
      if (library.error) return `<div class="metadata-library-block"><div class="metadata-library-summary"><strong>${h(library.name)}</strong><span>scan failed</span></div><div class="metadata-error">${h(library.error)}</div></div>`;
      const items = (library.items || []).map(item => `<div class="metadata-item"><div><div class="metadata-item-title">${h(item.title)}${item.year?` (${h(item.year)})`:''}</div><div class="metadata-item-key">ratingKey ${h(item.rating_key)} &middot; ${h(item.metadata_key)}</div></div>${item.plex_url?`<a class="btn btn-ghost" href="${h(item.plex_url)}" target="_blank" rel="noopener noreferrer">Open in Plex &nearr;</a>`:''}</div>`).join('');
      return `<div class="metadata-library-block"><div class="metadata-library-summary"><strong>${h(library.name)}</strong><span>${Number(library.unmatched_count||0).toLocaleString()} unmatched / ${Number(library.total_items||0).toLocaleString()} items</span></div><div class="metadata-items">${items || '<div class="metadata-clean">No unmatched items found.</div>'}</div></div>`;
    }).join('');
    const needsAttention = !audit || unmatched > 0 || errors > 0;
    const statusLabel = !audit ? 'Not scanned' : errors ? `${errors} scan error${errors===1?'':'s'}` : unmatched ? `${unmatched} need attention` : 'All matched';
    return `<section class="repair-instance-card metadata-audit-card ${needsAttention?'not-ready':''} ${expanded?'expanded':''}">
      <div class="repair-instance-head"><div class="repair-instance-name">${h(instanceName)}</div><div class="repair-status-pill ${needsAttention?'warn':''}"><span class="repair-status-dot"></span>${statusLabel}</div></div>
      <div class="repair-metrics">
        <div class="repair-metric"><div class="repair-metric-label">Unmatched</div><div class="repair-metric-value affected">${unmatched.toLocaleString()}</div></div>
        <div class="repair-metric"><div class="repair-metric-label">Items scanned</div><div class="repair-metric-value">${total!=null?Number(total).toLocaleString():'\u2014'}</div></div>
        <div class="repair-metric"><div class="repair-metric-label">Libraries</div><div class="repair-metric-value fixed">${audit?libraries.length:'\u2014'}</div></div>
      </div>
      <div class="metadata-card-body"><div class="repair-library-panel">${libraryBlocks || `<div class="repair-empty-audit"><div><strong style="display:block;color:var(--bright);font-size:17px;margin-bottom:6px;">${audit?'No included movie or TV libraries':'Run the first metadata scan'}</strong>${audit?'Review Metadata Health settings if every library is excluded.':'One bulk, read-only Plex request is used per included library.'}</div></div>`}</div></div>
      <div class="repair-instance-footer"><div class="repair-instance-footnote">${audit?`Last scanned ${h(fmtAgo(audit.audited_at))}`:'No saved results yet'}${ignoredNames.length?`<br>${ignoredNames.length} librar${ignoredNames.length===1?'y':'ies'} ignored`:''}<br>${audit?.machine_id?'Direct Plex links available':'Rating keys will be shown if no machine identifier is available'}</div><div class="repair-instance-actions"><button class="btn btn-secondary metadata-collapse" data-metadata-toggle="${h(instanceName)}">${expanded?'Hide details':'Show details'}</button><button class="btn btn-primary" data-metadata-audit-instance="${h(instanceName)}">Scan now</button></div></div>
    </section>`;
  }).join('') || '<div class="empty-msg">No Plex instances are configured.</div>';
  bindMetadataControls(grid);
}

function bindMetadataControls(grid) {
  grid.querySelectorAll('[data-metadata-audit-instance]').forEach(button => button.addEventListener('click', () => runMetadataAudit(button.dataset.metadataAuditInstance, button)));
  grid.querySelectorAll('[data-metadata-toggle]').forEach(button => button.addEventListener('click', () => toggleMetadataDetails(button.dataset.metadataToggle)));
}

function renderSingleServerMetadata(status, instanceName) {
  const audit = status.audits?.[instanceName];
  const ignored = new Set((status.ignored_libraries?.[instanceName] || []).map(name => String(name).toLocaleLowerCase()));
  const audited = new Map((audit?.libraries || []).map(library => [String(library.name).toLocaleLowerCase(), library]));
  const libraries = (status.libraries?.[instanceName] || []).filter(library => !ignored.has(String(library.name).toLocaleLowerCase())).map(library => ({...library, ...(audited.get(String(library.name).toLocaleLowerCase()) || {}), scanned:audited.has(String(library.name).toLocaleLowerCase())}));
  if (!libraries.length) return `<div class="empty-msg">${audit?'Every library is ignored or unsupported.':'Run the first metadata scan to load library results.'}</div>`;
  return libraries.map(library => {
    const key = `${instanceName}::${library.name}`;
    const expanded = _metadataExpanded.has(key);
    const unmatched = Number(library.unmatched_count || 0);
    const items = (library.items || []).map(item => `<div class="metadata-item"><div><div class="metadata-item-title">${h(item.title)}${item.year?` (${h(item.year)})`:''}</div><div class="metadata-item-key">ratingKey ${h(item.rating_key)} &middot; ${h(item.metadata_key)}</div></div>${item.plex_url?`<a class="btn btn-ghost" href="${h(item.plex_url)}" target="_blank" rel="noopener noreferrer">Open in Plex &nearr;</a>`:''}</div>`).join('');
    const statusLabel = !library.scanned ? 'Not scanned' : library.error ? 'Scan error' : unmatched ? `${unmatched} need attention` : 'All matched';
    return `<section class="repair-instance-card metadata-audit-card ${library.error||unmatched?'not-ready':''} ${expanded?'expanded':''}">
      <div class="repair-instance-head"><div class="repair-instance-name">${h(library.name)}</div><div class="repair-status-pill ${library.error||unmatched?'warn':''}"><span class="repair-status-dot"></span>${statusLabel}</div></div>
      <div class="repair-metrics"><div class="repair-metric"><div class="repair-metric-label">Unmatched</div><div class="repair-metric-value affected">${library.scanned?unmatched.toLocaleString():'—'}</div></div><div class="repair-metric"><div class="repair-metric-label">Items scanned</div><div class="repair-metric-value">${library.scanned?Number(library.total_items||0).toLocaleString():'—'}</div></div></div>
      <div class="metadata-card-body"><div class="repair-library-panel">${!library.scanned?'<div class="metadata-clean">Run a server scan to check this library.</div>':library.error?`<div class="metadata-error">${h(library.error)}</div>`:`<div class="metadata-items">${items||'<div class="metadata-clean">No unmatched items found.</div>'}</div>`}</div></div>
      <div class="repair-instance-footer"><div class="repair-instance-footnote">${audit?`Last scanned ${h(fmtAgo(audit.audited_at))}`:'Not scanned yet'}<br>${h(instanceName)}</div><div class="repair-instance-actions"><button class="btn btn-secondary metadata-collapse" data-metadata-toggle="${h(key)}">${expanded?'Hide details':'Show details'}</button></div></div>
    </section>`;
  }).join('');
}

function toggleMetadataDetails(instance) {
  if (_metadataExpanded.has(instance)) _metadataExpanded.delete(instance); else _metadataExpanded.add(instance);
  renderMetadataAudits(_metadataAuditStatus || {instances:[],audits:{}});
}

function collapseAllMetadata() {
  _metadataExpanded.clear();
  renderMetadataAudits(_metadataAuditStatus || {instances:[],audits:{}});
}

function expandMetadataProblems(instance, result) {
  if ((_metadataAuditStatus?.instances || []).length === 1) {
    (result.libraries || []).filter(library => library.error || Number(library.unmatched_count || 0) > 0).forEach(library => _metadataExpanded.add(`${instance}::${library.name}`));
  } else if (result.unmatched_count || result.error_count) {
    _metadataExpanded.add(instance);
  }
}

function openMetadataHealthSettings() {
  goToSettings('metadata-health');
}

function updateMetadataRollup(status) {
  const audits = status.audits || {};
  let unmatched = 0, scanned = 0, scannedServers = 0, errors = 0;
  (status.instances || []).forEach(instanceName => {
    const audit = audits[instanceName];
    if (!audit) return;
    scannedServers++;
    const ignored = new Set((status.ignored_libraries?.[instanceName] || []).map(name => String(name).toLocaleLowerCase()));
    (audit.libraries || []).filter(library => !ignored.has(String(library.name).toLocaleLowerCase())).forEach(library => {
      unmatched += Number(library.unmatched_count || 0);
      scanned += Number(library.total_items || 0);
      if (library.error) errors++;
    });
  });
  const card = document.getElementById('rollup-metadata');
  if (!card) return;
  document.getElementById('rollup-unmatched').textContent = unmatched.toLocaleString();
  document.getElementById('rollup-scanned').textContent = scanned.toLocaleString();
  const statusEl = document.getElementById('rollup-metadata-status');
  const copy = document.getElementById('rollup-metadata-copy');
  card.className = `rollup-card ${errors?'fail':unmatched?'warn':scannedServers?'pass':''}`;
  statusEl.textContent = errors ? `${errors} scan error${errors===1?'':'s'}` : unmatched ? `${unmatched.toLocaleString()} item${unmatched===1?' needs':'s need'} attention` : scannedServers ? 'All scanned items are matched' : 'Not scanned yet';
  copy.textContent = scannedServers ? `${scannedServers} of ${(status.instances || []).length} Plex server${(status.instances || []).length===1?'':'s'} have saved results.` : 'Run a metadata scan to find movies and shows that need a Plex match.';
}

async function runMetadataAudit(instance, button) {
  button.disabled = true;
  button.innerHTML = '<span class="spin"></span> scanning&hellip;';
  try {
    const response = await fetch('/api/metadata-audit/run', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instance})});
    const result = await readJsonResponse(response);
    if (!response.ok) throw new Error(result.error || 'Metadata scan failed');
    expandMetadataProblems(instance, result);
    toast(`Scan found ${result.unmatched_count} unmatched item(s) on ${instance}`, result.unmatched_count ? 'warn' : 'pass');
    await loadMetadataAuditStatus();
  } catch (error) {
    toast(error.message || 'Metadata scan failed', 'fail');
    button.disabled = false;
    button.textContent = 'Scan now';
  }
}

async function scanAllMetadata(button) {
  button.disabled = true;
  const instances = _metadataAuditStatus?.instances || _instances.map(instance => instance.name);
  let failures = 0;
  for (let index = 0; index < instances.length; index++) {
    button.innerHTML = `<span class="spin"></span> scanning ${index + 1} of ${instances.length}&hellip;`;
    try {
      const response = await fetch('/api/metadata-audit/run', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instance:instances[index]})});
      const result = await readJsonResponse(response);
      if (!response.ok) throw new Error(result.error || 'scan failed');
      expandMetadataProblems(instances[index], result);
    } catch (error) { failures++; }
  }
  await loadMetadataAuditStatus();
  button.disabled = false;
  button.textContent = instances.length === 1 ? 'Scan server' : 'Scan all servers';
  toast(failures ? `Metadata scan finished with ${failures} server error(s)` : 'All metadata scans complete', failures ? 'warn' : 'pass');
}

// Timestamp repair
let _repairStatus = null;
let _repairPoll = null;
const _repairSelection = new Map();
const _repairExpanded = new Set();
let _repairExpansionInitialized = false;
const _repairPhases = ['Preparing','Temporary rename','First Plex scan','Names restored','Second Plex scan','Verifying','Repaired'];

function repairPhaseIndex(state) {
  return ({prepared:0,renamed:1,first_plex_scan:2,waiting_for_first_scan:2,restoring:3,restored:3,second_plex_scan:4,verifying:5,completed:6})[state] ?? 0;
}

function renderRepairProgress(state) {
  const current = repairPhaseIndex(state);
  return `<div class="repair-progress">${_repairPhases.map((label,index)=>`<div class="repair-progress-step ${index<current?'done':index===current?'current':''}">${index+1}. ${label}</div>`).join('')}</div>`;
}

function formatUnixDate(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 'Unavailable';
  try { return new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeZone:'UTC'}).format(new Date(numeric*1000)); }
  catch (_) { return String(value); }
}

function pathStateLabel(state) {
  return ({missing_path:'Missing path',broken_symlink:'Broken symlink',regular_file:'Regular file',repairable_timestamp:'Repairable timestamp'})[state] || String(state||'Unknown path state');
}

async function loadRepairStatus() {
  try {
    const response = await fetch('/api/timestamp-repair/status');
    _repairStatus = await readJsonResponse(response);
    renderRepairOverview(_repairStatus);
    updateRepairRollup(_repairStatus);
    if (_repairStatus.running || _repairStatus.active_transaction) {
      clearTimeout(_repairPoll);
      _repairPoll = setTimeout(loadRepairStatus, 5000);
    }
  } catch (error) {
    document.getElementById('repair-summary-grid').innerHTML = '<div class="empty-msg">Could not load timestamp repair status.</div>';
  }
}

function updateRepairRollup(status) {
  const card = document.getElementById('rollup-repair');
  if (!card) return;
  const instances = status.instances || [];
  const ready = instances.filter(instance => instance.ready).length;
  const affected = Object.values(status.audits || {}).reduce((total, audit) => total + Number(audit.distinct_files || 0), 0);
  document.getElementById('rollup-repair-ready').textContent = ready.toLocaleString();
  document.getElementById('rollup-repair-affected').textContent = affected.toLocaleString();
  const statusEl = document.getElementById('rollup-repair-status');
  const copy = document.getElementById('rollup-repair-copy');
  if (status.running || status.active_transaction) {
    const recovery = status.active_transaction?.state === 'recovery_required';
    card.className = 'rollup-card warn'; statusEl.textContent = recovery ? 'Timestamp recovery required' : 'Repair operation active'; copy.textContent = status.batch?.running ? `Sequential repair ${status.batch.current || 0} of ${status.batch.total}` : 'Empty Trash remains locked until repair or recovery completes.';
  } else if (affected) {
    card.className = 'rollup-card warn'; statusEl.textContent = `${affected.toLocaleString()} affected file${affected===1?'':'s'}`; copy.textContent = 'Review folders, select the ones you approve, and run them sequentially.';
  } else if (ready) {
    card.className = 'rollup-card pass'; statusEl.textContent = 'Ready for manual scans'; copy.textContent = `${ready} Plex server${ready===1?' is':'s are'} configured for timestamp repair.`;
  } else {
    card.className = 'rollup-card'; statusEl.textContent = 'Setup required'; copy.textContent = 'Timestamp repair remains disabled until its database and repair paths are configured.';
  }
}

function renderRepairOverview(status) {
  const active = status.active_transaction;
  const batch = status.batch || {};
  document.getElementById('repair-active').innerHTML = active ? `
    <div class="repair-active-card">
      <div class="repair-active-title">${batch.running?`Folder ${batch.current} of ${batch.total} &middot; `:''}${h(active.state.replaceAll('_',' '))} &middot; ${h(active.instance)} / ${h(active.library)}</div>
      <div class="repair-active-meta">${active.worker?`Worker: ${h(active.worker)} &middot; `:''}${h(active.folder)} &middot; ${Number(active.scan_elapsed_seconds||0)}s elapsed &middot; heartbeat ${h(fmtAgo(active.last_heartbeat))}</div>
      ${renderRepairProgress(active.recovery_state || active.state)}
      <div style="margin-top:14px;display:flex;gap:10px;"><button class="btn btn-warn" onclick="cancelRepair()">Cancel safely</button>${active.state === 'recovery_required' ? '<button class="btn btn-primary" onclick="recoverRepair()">Recover</button>' : ''}</div>
    </div>` : batch.running ? `<div class="repair-active-card"><div class="repair-active-title">Preparing repair queue &middot; ${batch.current || 0} of ${batch.total}</div><div class="repair-active-meta">${h(batch.folder || 'Validating the first selected folder')}</div><div style="margin-top:14px;"><button class="btn btn-warn" onclick="cancelRepair()">Cancel safely</button></div></div>` : batch.state === 'failed' ? `<div class="repair-warning" style="border-color:var(--fail-line);color:var(--fail2);">Repair queue stopped after ${Number(batch.completed||0)} completed folder(s): ${h(batch.error||'repair failed')}</div>` : (status.state === 'failed' && status.error ? `<div class="repair-warning" style="border-color:var(--fail-line);color:var(--fail2);">Last repair attempt failed: ${h(status.error)}</div>` : '');

  const audits = status.audits || {};
  if (!_repairExpansionInitialized) {
    (status.instances || []).filter(instance => instance.enabled).forEach(instance => _repairExpanded.add(instance.name));
    _repairExpansionInitialized = true;
  }
  document.getElementById('repair-history').innerHTML = (status.history || []).length ? status.history.map(item => `
    <div class="repair-history-item"><span class="badge ${item.state==='completed'?'success':item.state==='failed'?'error':'skipped'}">${h(item.state)}</span>
      <div><div class="repair-history-title">${h(item.instance)} / ${h(item.library)}</div><div class="repair-history-meta">${h(item.folder)}<br>${h(fmtTime(item.completed_at||item.updated_at))}${item.error?` &middot; ${h(item.error)}`:''}${(item.timestamp_changes||[]).map(change=>`<br>${h(change.file_path.split(/[\\/]/).pop())}: ${h(change.before)} &rarr; ${change.after==null?'not verified':h(change.after)}`).join('')}</div></div>
    </div>`).join('') : '<div class="empty-msg">No repair transactions yet. Completed folder repairs will appear here.</div>';

  const orderedInstances = [...(status.instances || [])].sort((left,right) => Number(right.enabled)-Number(left.enabled) || Number(right.ready)-Number(left.ready));
  document.getElementById('repair-summary-grid').innerHTML = orderedInstances.map(instance => {
    const audit = audits[instance.name];
    const groups = {};
    (audit?.folders || []).forEach(folder => {
      const key = folder.library_section_id;
      groups[key] ||= {section:key,library:folder.library,folders:[],issues:[]};
      groups[key].folders.push(folder);
    });
    (audit?.path_issues || []).forEach(issue => {
      const key = issue.library_section_id;
      groups[key] ||= {section:key,library:`Section ${key}`,folders:[],issues:[]};
      groups[key].issues.push(issue);
    });
    (audit?.libraries || []).forEach(library => {
      groups[library.library_section_id] ||= {section:library.library_section_id,library:library.library,folders:[],issues:[]};
      groups[library.library_section_id].library = library.library;
      groups[library.library_section_id].totalItems = library.total_items;
    });
    const affected = Number(audit?.distinct_files || 0);
    const pathCounts = audit?.path_state_counts || {};
    const excludedPaths = Number(pathCounts.missing_path||0) + Number(pathCounts.broken_symlink||0) + Number(pathCounts.regular_file||0);
    const pathIssueSummary = [
      pathCounts.missing_path ? `${pathCounts.missing_path} missing` : '',
      pathCounts.broken_symlink ? `${pathCounts.broken_symlink} broken symlink${Number(pathCounts.broken_symlink)===1?'':'s'}` : '',
      pathCounts.regular_file ? `${pathCounts.regular_file} regular file${Number(pathCounts.regular_file)===1?'':'s'}` : '',
    ].filter(Boolean).join(' · ');
    const fixed = Number(status.repair_totals?.[instance.name] || 0);
    const totalItems = audit?.total_library_items;
    const libraryRows = Object.values(groups).map(group => {
      const count = group.folders.reduce((total,folder)=>total+folder.files.length,0);
      const folderCount = group.folders.length;
      const issueCount = (group.issues || []).length;
      const libraryTotal = group.totalItems != null && Number.isFinite(Number(group.totalItems)) ? Number(group.totalItems).toLocaleString() : '\u2014';
      return `<div class="repair-library-row">
        <div><div class="repair-library-name">${h(group.library)}</div><div class="repair-library-meta">${folderCount} repairable folder${folderCount===1?'':'s'}${issueCount?` &middot; ${issueCount} other path issue${issueCount===1?'':'s'}`:''} &middot; ${libraryTotal} total items</div></div>
        <div class="repair-library-count">${count.toLocaleString()}<span>affected</span></div>
        <button class="btn btn-ghost" data-repair-instance="${h(instance.name)}" data-repair-section="${h(group.section)}" data-repair-library="${h(group.library)}" ${count||issueCount?'':'disabled'}>View issues</button>
      </div>`;
    }).join('');
    const workerLabel = instance.worker === 'local' ? `Main ${PRODUCT_NAME} container` : `Worker: ${h(instance.worker)}`;
    const readinessLabel = instance.blocked ? 'Recovery blocked' : instance.ready ? 'Ready' : instance.enabled ? 'Setup required' : 'Disabled';
    const available = instance.ready && !instance.blocked;
    const expanded = _repairExpanded.has(instance.name);
    return `<section class="repair-instance-card ${instance.ready?'':'not-ready'} ${expanded?'':'repair-collapsed'}">
      <div class="repair-instance-head"><div><div class="repair-instance-kind">Plex server</div><div class="repair-instance-name">${h(instance.name)}</div></div><div class="repair-instance-head-actions"><div class="repair-status-pill ${available?'':'warn'}"><span class="repair-status-dot"></span>${readinessLabel}</div><button class="btn btn-secondary" data-repair-toggle="${h(instance.name)}">${expanded?'Hide details':'Show details'}</button></div></div>
      <div class="repair-overview-body">
      <div class="repair-metrics">
        <div class="repair-metric"><div class="repair-metric-label">Affected files</div><div class="repair-metric-value affected">${affected.toLocaleString()}</div></div>
        <div class="repair-metric"><div class="repair-metric-label">Files fixed</div><div class="repair-metric-value fixed">${fixed.toLocaleString()}</div></div>
        <div class="repair-metric"><div class="repair-metric-label">Library items</div><div class="repair-metric-value">${totalItems != null&&Number.isFinite(Number(totalItems))?Number(totalItems).toLocaleString():'\u2014'}</div></div>
      </div>
      ${excludedPaths ? `<div class="repair-warning" style="margin:0 0 16px;border-color:var(--fail-line);color:var(--fail2);"><strong>${excludedPaths} database entr${excludedPaths===1?'y':'ies'} excluded from repair:</strong> ${h(pathIssueSummary)}. View the affected library for details.</div>` : ''}
      ${audit?.database_count_changed ? `<div class="repair-warning" style="margin:0 0 16px;"><strong>Database changed since this audit:</strong> ${Number(audit.database_distinct_files||0).toLocaleString()} reviewed then, ${Number(audit.live_database_distinct_files||0).toLocaleString()} now. Reviewed folders remain selectable because each is revalidated immediately before repair; run a new audit to discover newly affected folders.</div>` : ''}
      <div class="repair-library-panel"><div class="repair-library-panel-head"><span>Library audit</span><span>${audit ? `snapshot ${h(fmtTime(audit.audited_at))}` : 'not audited'}</span></div>${libraryRows || `<div class="repair-empty-audit"><div><strong style="display:block;color:var(--bright);font-size:17px;margin-bottom:6px;">${instance.ready?'Run the first audit':'Finish setup to begin'}</strong>${h(instance.readiness)}</div></div>`}</div>
      <div class="repair-instance-footer"><div class="repair-instance-footnote">${workerLabel}<br>${audit ? `${affected.toLocaleString()} repairable files across ${Number(audit.affected_folders||0).toLocaleString()} folders · audit ${h(fmtAgo(audit.audited_at))}` : 'No audit results recorded yet'}</div><div class="repair-instance-actions">${!instance.ready?`<button class="btn btn-secondary" data-configure-repair="${h(instance.name)}">Configure</button>`:''}<button class="btn btn-primary" data-audit-instance="${h(instance.name)}" ${instance.ready&&!status.running&&!status.active_transaction?'':'disabled'}>Audit now</button></div></div></div>
    </section>`;
  }).join('') || '<div class="empty-msg">No Plex instances are configured.</div>';

  document.querySelectorAll('[data-repair-instance]').forEach(button => button.addEventListener('click', () => openRepairLibrary(button.dataset.repairInstance,button.dataset.repairSection,button.dataset.repairLibrary)));
  document.querySelectorAll('[data-audit-instance]').forEach(button => button.addEventListener('click', () => auditRepair(button.dataset.auditInstance)));
  document.querySelectorAll('[data-configure-repair]').forEach(button => button.addEventListener('click', () => { goToSettings('timestamp-repair'); }));
  document.querySelectorAll('[data-repair-toggle]').forEach(button => button.addEventListener('click', () => toggleRepairDetails(button.dataset.repairToggle)));
}

function toggleRepairDetails(instanceName) {
  if (_repairExpanded.has(instanceName)) _repairExpanded.delete(instanceName); else _repairExpanded.add(instanceName);
  renderRepairOverview(_repairStatus || {instances:[],audits:{},history:[]});
}

function setAllRepairDetails(expanded) {
  _repairExpanded.clear();
  if (expanded) (_repairStatus?.instances || []).forEach(instance => _repairExpanded.add(instance.name));
  renderRepairOverview(_repairStatus || {instances:[],audits:{},history:[]});
}

async function auditRepair(instance) {
  toast(`Auditing ${instance}...`);
  const response = await fetch('/api/timestamp-repair/audit', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instance})});
  const data = await readJsonResponse(response);
  if (!response.ok) return toast(data.error || 'Audit failed', 'fail');
  toast(`Found ${data.distinct_files} affected file(s)`, data.distinct_files ? 'warn' : 'pass');
  await loadRepairStatus();
}

function openRepairLibrary(instance, section, title) {
  const audit = _repairStatus?.audits?.[instance];
  if (!audit) return;
  document.getElementById('repair-overview').style.display = 'none';
  document.getElementById('repair-detail').style.display = '';
  document.getElementById('repair-detail-instance').textContent = instance;
  document.getElementById('repair-detail-title').textContent = title || 'Affected folders';
  const folders = (audit.folders || []).filter(folder => !section || folder.library_section_id === String(section));
  const pathIssues = (audit.path_issues || []).filter(issue => !section || issue.library_section_id === String(section));
  _repairSelection.clear();
  const fileCount = folders.reduce((total,folder)=>total+folder.files.length,0);
  document.getElementById('repair-detail-subtitle').textContent = `${fileCount} repairable file(s) across ${folders.length} folder(s)${pathIssues.length?` · ${pathIssues.length} other path issue(s) excluded`:''} · audit ${fmtTime(audit.audited_at)}`;
  const repairableCards = folders.map(folder => {
    const action = folder.library_type === 'movie' ? 'Repair movie folder' : folder.library_type === 'show' ? 'Repair season folder' : 'Repair folder';
    return `
    <div class="repair-folder-card"><div class="repair-folder-title">${h(folder.title)}</div><div class="repair-folder-subtitle">${h(folder.subtitle || folder.library)} &middot; ${folder.files.length} affected file${folder.files.length===1?'':'s'}</div>
      <div class="repair-files">${folder.files.map(file => `<div class="repair-file"><strong>${h(file.item_title || file.file_path.split(/[\\/]/).pop())}</strong><div class="repair-file-path">${h(file.file_path)}</div><div class="repair-change"><div><span>Plex timestamp</span><strong class="bad">${h(formatUnixDate(file.stored_timestamp))} (${h(file.stored_timestamp)})</strong></div><div><span>Filesystem timestamp</span><strong>${h(file.filesystem_timestamp_iso ? fmtTime(file.filesystem_timestamp_iso) : 'Unavailable')}</strong></div><div><span>Status</span><strong class="bad">Negative Plex part timestamp</strong></div></div></div>`).join('')}</div>
      <div class="repair-folder-footer"><label class="repair-select"><input type="checkbox" data-repair-select="${h(encodeURIComponent(JSON.stringify({instance,section:folder.library_section_id,folder:folder.folder,files:folder.files,libraryType:folder.library_type,title:folder.title})))}" ${_repairStatus.running||_repairStatus.active_transaction?'disabled':''}> Select</label><button class="btn btn-primary" data-repair-payload="${h(encodeURIComponent(JSON.stringify({instance,section:folder.library_section_id,folder:folder.folder,files:folder.files,libraryType:folder.library_type})))}" ${_repairStatus.running||_repairStatus.active_transaction?'disabled':''}>${action}</button></div>
    </div>`;
  }).join('');
  const issueCards = pathIssues.map(issue => `<div class="repair-folder-card repair-path-issue"><div class="repair-folder-title">${h(issue.item_title || issue.file_path.split(/[\\/]/).pop())}</div><div class="repair-folder-subtitle">Excluded from automatic repair</div><div class="repair-files"><div class="repair-file"><div class="repair-file-path">${h(issue.file_path)}</div><div class="repair-change"><div><span>Plex timestamp</span><strong class="bad">${h(formatUnixDate(issue.stored_timestamp))} (${h(issue.stored_timestamp)})</strong></div><div><span>Path status</span><strong class="bad">${h(pathStateLabel(issue.path_state))}</strong></div><div><span>Action</span><strong>Resolve the filesystem path before a fresh audit</strong></div></div></div></div></div>`).join('');
  document.getElementById('repair-folder-grid').innerHTML = repairableCards + issueCards || '<div class="empty-msg">No affected folders remain. Run a fresh audit to confirm.</div>';
  document.querySelectorAll('[data-repair-payload]').forEach(button => button.addEventListener('click', () => { const payload=JSON.parse(decodeURIComponent(button.dataset.repairPayload)); runRepairFolder(payload.instance,payload.section,payload.folder,payload.files,payload.libraryType); }));
  document.querySelectorAll('[data-repair-select]').forEach(input => input.addEventListener('change', () => {
    const payload=JSON.parse(decodeURIComponent(input.dataset.repairSelect));
    const key=`${payload.section}\u0000${payload.folder}`;
    if (input.checked) _repairSelection.set(key,payload); else _repairSelection.delete(key);
    updateRepairSelectionControls();
  }));
  updateRepairSelectionControls();
}

function closeRepairLibrary() { document.getElementById('repair-detail').style.display='none'; document.getElementById('repair-overview').style.display=''; }

async function runRepairFolder(instance, section, folder, files, libraryType) {
  const scope = libraryType === 'movie' ? 'movie folder' : libraryType === 'show' ? 'season folder' : 'folder';
  const exact = files.map(file => `  • ${file.file_path}\n    Plex timestamp: ${file.stored_timestamp} → positive value assigned and verified by Plex`).join('\n');
  if (!confirm(`Repair this ${scope}?\n\n${PRODUCT_NAME} will temporarily rename ${files.length} symlink${files.length===1?'':'s'} in this ${scope}, scan the folder, restore the original filename${files.length===1?'':'s'}, scan again, and verify that Plex stored a valid positive timestamp.\n\nThe underlying provider/NZBDAV object${files.length===1?'':'s'} will not be renamed or modified.\n\n${folder}\n\nAffected symlinks (${files.length}):\n${exact}\n\nEmpty Trash remains locked until restoration and verification complete.`)) return;
  const response = await fetch('/api/timestamp-repair/run', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instance,library_section_id:section,folder})});
  const data = await readJsonResponse(response);
  if (!response.ok) return toast(data.error || 'Repair could not start', 'fail');
  closeRepairLibrary(); toast('Timestamp repair started','warn'); setTimeout(loadRepairStatus,500);
}

function updateRepairSelectionControls() {
  const count=_repairSelection.size;
  const button=document.getElementById('repair-selected');
  if (button) { button.textContent=`Repair selected (${count})`; button.disabled=count===0||Boolean(_repairStatus?.running||_repairStatus?.active_transaction); }
  const selectAll=document.getElementById('repair-select-all');
  const available=[...document.querySelectorAll('[data-repair-select]:not(:disabled)')];
  if (selectAll) selectAll.textContent=available.length&&available.every(input=>input.checked)?'Clear selection':'Select all';
}

function selectAllRepairFolders() {
  const inputs=[...document.querySelectorAll('[data-repair-select]:not(:disabled)')];
  const select=inputs.some(input=>!input.checked);
  inputs.forEach(input=>{ if (input.checked!==select) { input.checked=select; input.dispatchEvent(new Event('change')); } });
  updateRepairSelectionControls();
}

async function runSelectedRepairFolders() {
  const selected=[..._repairSelection.values()];
  if (!selected.length) return;
  const files=selected.reduce((total,item)=>total+item.files.length,0);
  const list=selected.map((item,index)=>`${index+1}. ${item.title || item.folder} (${item.files.length} file${item.files.length===1?'':'s'})`).join('\n');
  if (!confirm(`Repair ${selected.length} reviewed folders sequentially?\n\n${list}\n\nEach folder will receive a fresh safety check and exact affected-file comparison immediately before repair. ${PRODUCT_NAME} stops the queue on the first changed or unsafe folder. Completed folders will not require another audit before the remaining reviewed folders continue.\n\nTotal affected symlinks: ${files}\nThe underlying provider objects will not be modified.`)) return;
  const response=await fetch('/api/timestamp-repair/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instance:selected[0].instance,folders:selected.map(item=>({library_section_id:item.section,folder:item.folder}))})});
  const data=await readJsonResponse(response);
  if (!response.ok) return toast(data.error||'Repair queue could not start','fail');
  _repairSelection.clear();
  closeRepairLibrary();
  toast(`Started sequential repair of ${data.folders} folder(s)`,'warn');
  setTimeout(loadRepairStatus,500);
}

async function cancelRepair() { await fetch('/api/timestamp-repair/cancel',{method:'POST'}); toast('Safe cancellation requested','warn'); }
async function recoverRepair() { const response=await fetch('/api/timestamp-repair/recover',{method:'POST'}); const data=await readJsonResponse(response); toast(data.message||data.error,response.ok?'pass':'fail'); loadRepairStatus(); }

function settingsNavButton(section) {
  return document.querySelector(`.settings-nav-item[data-section="${section}"]`);
}

// Open Settings at a named section. Callers used to do this three different
// ways, two of them matching on the button's visible label - which broke
// silently whenever a section was renamed.
function goToSettings(section) {
  showPage('settings', document.getElementById('nav-settings'));
  showSettingsSection(section, settingsNavButton(section));
}

// A feature's configuration lives on the feature's own page, as a tab, so
// changing how something behaves no longer means a trip to another page.
const FEATURE_TAB_LOADERS = {
  'mediamender/configure': () => { renderTrashRemovalSettings(); loadGlobalScheduleControls(); },
  'library-refresh/configure': () => renderLibraryRefreshSettings(),
  'mark-watched/configure': () => {
    renderMarkWatchedSettings();
    if (_identity.role === 'admin') loadSonarrConnectionStatus();
  },
  'metadata-audit/configure': () => renderMetadataHealthSettings(),
  'timestamp-repair/configure': () => renderRepairSettings(),
};

function hasFeatureTabs(page) {
  return !!document.querySelector(`#page-${page} .feature-tabs`);
}

function showFeatureTab(page, tab = 'main') {
  const host = document.getElementById(`page-${page}`);
  if (!host) return;
  if (!document.getElementById(`tab-${page}-${tab}`)) tab = 'main';
  host.querySelectorAll('.feature-panel').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.tab === tab);
  });
  host.querySelectorAll('.feature-tab').forEach(button => {
    const active = button.dataset.tab === tab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  setRoute(page, tab === 'main' ? '' : tab);
  if (tab === 'configure') {
    // The Configure panels read from the same settings payload the Settings
    // page uses, so it still has to be loaded before they render.
    loadSettings().then(() => FEATURE_TAB_LOADERS[`${page}/${tab}`]?.());
  }
}

function showSettingsSection(name, btn) {
  if (!document.getElementById(`ss-${name}`)) name = 'plex';
  document.querySelectorAll('.settings-section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.settings-nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById(`ss-${name}`).classList.add('active');
  (btn || settingsNavButton(name))?.classList.add('active');
  setRoute('settings', name);
  if (name === 'providers') loadProviderStatus();
  if (name === 'security')  { loadApiToken(); }
  if (name === 'timestamp-repair') renderRepairSettings();
  if (name === 'trash-removal') { renderTrashRemovalSettings(); loadGlobalScheduleControls(); }
  if (name === 'library-refresh') renderLibraryRefreshSettings();
  if (name === 'mark-watched') { renderMarkWatchedSettings(); if (_identity.role === 'admin') loadSonarrConnectionStatus(); }
  if (name === 'features') renderFeatureSettings();
  if (name === 'metadata-health') renderMetadataHealthSettings();
  if (name === 'general') startLogViewer();
  else stopLogViewer();
}

// ── Utils ────────────────────────────────────────────────────────────────────
function h(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
async function readJsonResponse(response, label = 'Request') {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch (_) {
    const summary = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 180);
    throw new Error(`${label} returned HTTP ${response.status}${summary?`: ${summary}`:''}`);
  }
}
function toast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show' + (type ? ` t-${type}` : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.className = '', 3500);
}
function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})
       + ' ' + d.toLocaleDateString([], {month:'short',day:'numeric'});
}
function fmtRel(iso) {
  if (!iso) return '—';
  const diff = Math.round((new Date(iso) - Date.now()) / 1000);
  if (diff <= 0) return 'now';
  const m = Math.floor(diff / 60), s = diff % 60;
  return m > 0 ? `${m}m` : `${s}s`;
}
function fmtAgo(iso) {
  if (!iso) return '—';
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
function fmtIn(iso) {
  if (!iso) return 'soon';
  const seconds = Math.round((new Date(iso) - Date.now()) / 1000);
  if (seconds <= 0) return 'now';
  if (seconds < 90) return `in ${seconds}s`;
  if (seconds < 5400) return `in ${Math.round(seconds / 60)}m`;
  return `in ${(seconds / 3600).toFixed(1)}h`;
}
function fmtStamp(iso) {
  if (!iso) return '--:--:--';
  const at = new Date(iso);
  return isNaN(at) ? '--:--:--' : at.toLocaleTimeString();
}
function cid(i, l) { return i.replace(/ /g,'_') + '_' + l.replace(/ /g,'_'); }
function statusCls(s) { return {success:'pass',skipped:'warn',error:'fail',dry_run:'info',preflight_pass:'pass',preflight_fail:'warn'}[s] || ''; }

// ── Dashboard views ───────────────────────────────────────────────────────────
function selectDashboardView(requestedView, updateLocation = true) {
  const requestedPanel = document.getElementById(`dashboard-panel-${requestedView}`);
  const view = requestedPanel ? requestedView : 'overview';
  document.querySelectorAll('.dashboard-view-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === `dashboard-panel-${view}`);
  });
  document.querySelectorAll('.server-tab[data-dashboard-view]').forEach(tab => {
    const active = tab.dataset.dashboardView === view;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  const select = document.getElementById('dashboard-server-select');
  if (select) select.value = view;
  if (updateLocation) setRoute('dashboard', view);
}

// ── Scheduling ────────────────────────────────────────────────────────────────
function applyScheduling(enabled) {
  const t = document.getElementById('sched-toggle');
  const l = document.getElementById('sched-label');
  if (!t || !l) return;
  t.className = 'toggle' + (enabled ? ' on' : '');
  l.textContent = enabled ? 'on' : 'paused';
  l.className = 'sched-label' + (enabled ? ' on' : '');
}
async function toggleScheduling() {
  const r = await fetch('/api/status'); const d = await readJsonResponse(r);
  const next = !d.scheduling_enabled;
  await fetch('/api/scheduling', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:next})});
  applyScheduling(next);
  toast(next ? '▶ Scheduling resumed' : '⏸ Scheduling paused', next ? 'pass' : 'warn');
}

// ── Instance status ───────────────────────────────────────────────────────────
function applyChecks(globalChecks) {
  _instances.forEach((inst, i) => {
    const pill = document.getElementById(`inst-pill-${i+1}`);
    if (!pill) return;
    const checks = globalChecks[inst.name] || {};
    const allPass = Object.values(checks).every(c => c.pass);
    const detail = Object.values(checks).map(c => c.detail).join(' | ') || 'checking…';
    const cls = Object.keys(checks).length === 0 ? '' : allPass ? 'pass' : 'fail';
    pill.className = `plex-status-pill ${cls}`;
    pill.innerHTML = `<span class="sdot ${cls}"></span><span title="${h(detail)}">${h(inst.name)}</span>`;
  });
}

function applyStatus(data, nextRuns) {
  const summary = {ready: 0, attention: 0, pending: 0, preflight: 0};
  _instances.forEach(inst => {
    const instData = (data.instances || []).find(i => i.name === inst.name) || {};
    (instData.libraries || []).forEach(lib => {
      const id = cid(inst.name, lib.name);
      const st = lib.status || {};
      const card = document.getElementById(`lc-${id}`);
      if (card) card.className = `lib-card ${st.last_status || 'idle'}`;
      const labels = {success:'Healthy',skipped:'Skipped',error:'Needs attention',dry_run:'Dry run passed',preflight_pass:'Safety checks passed',preflight_fail:'Safety checks failed'};
      const sv = document.getElementById(`ls-s-${id}`);
      if (sv) sv.textContent = labels[st.last_status] || 'Awaiting first run';
      const dot = document.getElementById(`ls-dot-${id}`);
      if (dot) dot.className = `lib-state-dot ${statusCls(st.last_status)}`;
      if (st.last_status === 'success' || st.last_status === 'dry_run' || st.last_status === 'preflight_pass') summary.ready++;
      else if (st.last_status === 'error' || st.last_status === 'skipped' || st.last_status === 'preflight_fail') summary.attention++;
      else summary.pending++;
      if (st.status_source === 'preflight') summary.preflight++;
      const rv = document.getElementById(`ls-r-${id}`);
      if (rv) { rv.textContent = st.removed_count != null ? st.removed_count : '—'; rv.className = `lib-stat-val ${st.removed_count > 0 ? 'pass' : ''}`; }
      const key = `${inst.name}::${lib.name}`;
      const nv = document.getElementById(`ls-n-${id}`);
      if (nv && nextRuns[key]) nv.textContent = fmtRel(nextRuns[key]);
      const tv = document.getElementById(`ls-t-${id}`);
      if (tv && (st.last_run || st.last_checked)) tv.innerHTML = `${st.status_source === 'preflight' ? 'Checked ' : ''}<b>${fmtTime(st.last_run || st.last_checked)}</b>`;
    });
  });
  updateProtectionOverview(summary);
  updateProtectionRollup(summary);
}

function updateProtectionRollup(summary) {
  const card = document.getElementById('rollup-mediamender');
  if (!card) return;
  document.getElementById('rollup-protected').textContent = summary.ready.toLocaleString();
  document.getElementById('rollup-protection-attention').textContent = summary.attention.toLocaleString();
  const status = document.getElementById('rollup-mediamender-status');
  const copy = document.getElementById('rollup-mediamender-copy');
  if (summary.attention) {
    card.className = 'rollup-card warn'; status.textContent = `${summary.attention} ${summary.attention===1?'library needs':'libraries need'} attention`; copy.textContent = 'Review the affected libraries before allowing Empty Trash.';
  } else if (summary.pending) {
    card.className = 'rollup-card'; status.textContent = 'Safety checks are starting'; copy.textContent = `${summary.pending} ${summary.pending===1?'library is':'libraries are'} waiting for current status.`;
  } else {
    card.className = 'rollup-card pass'; status.textContent = 'All libraries are healthy'; copy.textContent = 'Current safety state is ready across every monitored library.';
  }
}

let _startupPollPending = false;
function applyStartupProgress(progress) {
  if (!progress) return;
  const completed = Number(progress.completed || 0);
  const total = Number(progress.total || 0);
  const message = progress.running
    ? `Checking ${h(progress.current || 'library safety')} — ${completed} of ${total} complete`
    : `Read-only startup checks complete — ${completed} of ${total} libraries updated`;
  ['dashboard-startup-progress','mediamender-startup-progress'].forEach(id => {
    const element = document.getElementById(id);
    if (!element) return;
    element.className = `startup-progress ${progress.running?'':'done'}`;
    element.innerHTML = `${progress.running?'<span class="spin"></span>':'<span>✓</span>'}<span>${message}</span>`;
  });
  if (progress.running) {
    const title = document.getElementById('protection-title');
    const copy = document.getElementById('protection-copy');
    if (title) title.textContent = 'Checking current library safety…';
    if (copy) copy.textContent = `${completed} of ${total} libraries checked. This process is read-only and will not empty trash.`;
  }
}

function updateProtectionOverview(summary) {
  const total = summary.ready + summary.attention + summary.pending || 1;
  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  setText('protection-ready', summary.ready);
  setText('protection-attention', summary.attention);
  setText('protection-pending', summary.pending);
  [
    ['protection-ready-bar', summary.ready],
    ['protection-attention-bar', summary.attention],
    ['protection-pending-bar', summary.pending],
  ].forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.style.width = `${value / total * 100}%`;
  });
  const title = document.getElementById('protection-title');
  const copy = document.getElementById('protection-copy');
  if (!title || !copy) return;
  if (summary.attention) {
    title.textContent = `${summary.attention} ${summary.attention === 1 ? 'library needs' : 'libraries need'} attention`;
    copy.textContent = summary.preflight
      ? 'A current read-only safety check failed. Review the affected library card before running Empty Trash.'
      : 'A health check was skipped or failed. Review activity before emptying trash.';
  } else if (summary.pending) {
    title.textContent = summary.ready ? 'Protection is coming online' : 'Ready for the first protection run';
    copy.textContent = `${summary.pending} ${summary.pending === 1 ? 'library has' : 'libraries have'} not completed a run yet.`;
  } else if (summary.preflight) {
    title.textContent = 'All libraries are ready';
    copy.textContent = 'Current read-only safety checks passed. No trash was emptied.';
  } else {
    title.textContent = 'All libraries are protected';
    copy.textContent = 'Every monitored library completed its latest safety run successfully.';
  }
}

function renderDashboardActivity(data) {
  const container = document.getElementById('dashboard-activity');
  if (!container) return;
  const recent = (data || []).slice(0, 4);
  if (!recent.length) {
    container.innerHTML = '<div class="empty-msg" style="padding:28px 0;">No runs recorded yet</div>';
    return;
  }
  container.innerHTML = recent.map((run, index) => `
    <button type="button" class="activity-item" onclick="openRunDetails(${index})" aria-label="View details for ${h(run.library)} on ${h(run.instance)}">
      <span class="activity-status ${h(run.status)}"></span>
      <div style="min-width:0;">
        <div class="activity-name">${h(run.library)} · ${h(run.message || run.status)}</div>
        <div class="activity-instance">${h(run.instance)}</div>
      </div>
      <span class="activity-time">${fmtAgo(run.timestamp)}</span>
      <span class="activity-open" aria-hidden="true">›</span>
    </button>
  `).join('');
}

function runChecksHtml(run) {
  const checks = Object.entries(run.checks || {});
  if (!checks.length) return '<div class="run-detail-empty">No health-check details were recorded.</div>';
  return checks.map(([name, check]) =>
    `<div class="check-detail-row"><span class="cdot ${check.pass?'pass':'fail'}" style="margin-top:3px;flex-shrink:0;"></span><span class="cdn">${h(name)}</span><span class="cdd">${h(check.detail || '')}</span></div>`
  ).join('');
}

function runRemovedHtml(run) {
  if (!run.removed_items || !run.removed_items.length) {
    return '<div class="run-detail-empty">No items removed</div>';
  }
  return run.removed_items.map(item =>
    `<div class="removed-entry"><span class="re-type">${h(item.type || '')}</span><span>${h(item.title)}</span>${item.year ? `<span class="re-year">${h(item.year)}</span>` : ''}</div>`
  ).join('');
}

function openRunDetails(index) {
  const run = _history[index];
  const overlay = document.getElementById('run-detail-overlay');
  const body = document.getElementById('run-detail-body');
  if (!run || !overlay || !body) return;
  const count = Number(run.removed_count || 0);
  body.innerHTML = `
    <div class="run-detail-summary">
      <span class="inst-chip">${h(run.instance)}</span>
      <span class="lib-chip">${h(run.library)}</span>
      <span class="badge ${h(run.status)}">${h(String(run.status || '').replace('_', ' '))}</span>
    </div>
    <div class="run-detail-message">${h(run.message || run.status)}</div>
    <div class="run-detail-time">${fmtTime(run.timestamp)}</div>
    <hr class="divider">
    <div class="expand-cols">
      <div>
        <div class="expand-section-title">Health Checks</div>
        ${runChecksHtml(run)}
      </div>
      <div>
        <div class="expand-section-title">Removed Items (${count})</div>
        <div class="removed-scroll">${runRemovedHtml(run)}</div>
      </div>
    </div>`;
  overlay.showModal();
  overlay.querySelector('button')?.focus();
}

function closeRunDetails(event) {
  if (event && event.target !== event.currentTarget) return;
  const overlay = document.getElementById('run-detail-overlay');
  if (overlay?.open) overlay.close();
}

// ── History ───────────────────────────────────────────────────────────────────
function setFilter(f, btn) {
  _filter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderHistory(_history);
}
function renderHistory(data) {
  renderDashboardActivity(data);
  const filtered = _filter === 'all' ? data : data.filter(r => r.status === _filter);
  const hc = document.getElementById('hcount');
  if (hc) hc.textContent = `${filtered.length} / ${data.length} runs`;
  const tbody = document.getElementById('htbody');
  if (!tbody) return;
  if (!filtered.length) { tbody.innerHTML = `<tr><td colspan="7"><div class="empty-msg">No ${_filter === 'all' ? '' : _filter + ' '}runs yet</div></td></tr>`; return; }
  tbody.innerHTML = filtered.map(run => {
    const rid = `run-${run.timestamp.replace(/[:.]/g, '-')}`;
    const isOpen = _expanded.has(rid);
    const badge = `<span class="badge ${run.status}">${run.status.replace('_',' ')}</span>`;
    const dots = Object.entries(run.checks || {}).map(([n, c]) => `<span class="cdot ${c.pass?'pass':'fail'}" title="${h(n)}: ${h(c.detail)}"></span>`).join('');
    const rem = run.removed_count > 0 ? `<span style="color:var(--pass);font-family:var(--mono);font-weight:700;">${run.removed_count}</span>` : `<span style="color:var(--muted);">0</span>`;
    const checksHtml = Object.entries(run.checks || {}).map(([n,c]) =>
      `<div class="check-detail-row"><span class="cdot ${c.pass?'pass':'fail'}" style="margin-top:3px;flex-shrink:0;"></span><span class="cdn">${h(n)}</span><span class="cdd">${h(c.detail||'')}</span></div>`
    ).join('');
    const removedHtml = run.removed_items && run.removed_items.length
      ? run.removed_items.map(i => `<div class="removed-entry"><span class="re-type">${h(i.type||'')}</span><span>${h(i.title)}</span>${i.year?`<span class="re-year">${h(i.year)}</span>`:''}</div>`).join('')
      : `<div style="color:var(--muted);font-size:11px;font-style:italic;">No items removed</div>`;
    return `
      <tr class="data-row ${isOpen?'expanded':''}" onclick="toggleExpand('${rid}',this)">
        <td style="font-family:var(--mono);font-size:11px;color:var(--muted);">${fmtTime(run.timestamp)}</td>
        <td><span class="inst-chip">${h(run.instance)}</span></td>
        <td><span class="lib-chip">${h(run.library)}</span></td>
        <td>${badge}</td>
        <td><div class="check-dots">${dots}</div></td>
        <td style="font-size:12px;color:var(--text);">${h(run.message)}</td>
        <td>${rem}</td>
      </tr>
      <tr class="expand-row ${isOpen?'open':''}" id="${rid}">
        <td class="expand-td" colspan="7">
          <div class="expand-inner">
            <div class="expand-cols">
              <div><div class="expand-section-title">Health Checks</div>${checksHtml}</div>
              <div><div class="expand-section-title">Removed Items (${run.removed_count})</div><div class="removed-scroll">${removedHtml}</div></div>
            </div>
          </div>
        </td>
      </tr>`;
  }).join('');
}
function toggleExpand(rid, row) {
  const er = document.getElementById(rid);
  if (!er) return;
  if (_expanded.has(rid)) { _expanded.delete(rid); er.classList.remove('open'); row.classList.remove('expanded'); }
  else { _expanded.add(rid); er.classList.add('open'); row.classList.add('expanded'); }
}

// ── Log viewer ────────────────────────────────────────────────────────────────
let _logPollTimer = null;
let _selectedLog = '';
let _activeLog = '';
let _logLoading = false;

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function startLogViewer() {
  if (_logPollTimer) return;
  loadLogFiles(true);
  _logPollTimer = setInterval(async () => {
    await loadLogFiles(false);
    if (_selectedLog && _selectedLog === _activeLog) refreshLogView();
  }, 3000);
}

function stopLogViewer() {
  if (_logPollTimer) clearInterval(_logPollTimer);
  _logPollTimer = null;
}

async function loadLogFiles(preferActive = false) {
  if (_logLoading) return;
  _logLoading = true;
  try {
    const response = await fetch('/api/logs');
    const data = await readJsonResponse(response);
    const files = data.files || [];
    _activeLog = files.find(file => file.active)?.name || '';
    if (preferActive || !files.some(file => file.name === _selectedLog)) {
      _selectedLog = _activeLog || files[0]?.name || '';
    }

    const total = files.reduce((sum, file) => sum + Number(file.size_bytes || 0), 0);
    const summary = document.getElementById('log-storage-summary');
    if (summary) {
      summary.textContent = `${formatBytes(total)} used of ${data.policy?.max_total_size_mb ?? '—'} MB · ${data.policy?.retention_days ?? '—'} day retention · ${data.directory || 'data/logs'}`;
    }

    const list = document.getElementById('log-file-list');
    if (list) {
      list.innerHTML = files.length ? files.map(file => `
        <button type="button" class="log-file-button"
                data-log-name="${h(file.name)}"
                style="display:block;width:100%;text-align:left;border:1px solid ${file.name === _selectedLog ? 'var(--accent)' : 'transparent'};background:${file.name === _selectedLog ? 'var(--surface2)' : 'transparent'};color:var(--text);border-radius:6px;padding:8px;margin-bottom:4px;cursor:pointer;">
          <span style="display:flex;justify-content:space-between;gap:8px;">
            <strong style="font-family:var(--mono);font-size:11px;">${h(file.name)}</strong>
            ${file.active ? '<span class="badge success">live</span>' : ''}
          </span>
          <span style="display:block;color:var(--muted);font-size:10px;margin-top:3px;">${formatBytes(file.size_bytes)} · ${fmtTime(file.modified)}</span>
        </button>
      `).join('') : '<div class="empty-msg" style="padding:20px 8px;">No log files found</div>';
      list.querySelectorAll('[data-log-name]').forEach(button => {
        button.addEventListener('click', () => selectLogFile(button.dataset.logName));
      });
    }

    const download = document.getElementById('log-download');
    if (download) {
      download.href = _selectedLog
        ? `/api/logs/${encodeURIComponent(_selectedLog)}/download`
        : '#';
      download.style.pointerEvents = _selectedLog ? '' : 'none';
      download.style.opacity = _selectedLog ? '' : '.5';
    }
    if (preferActive) await refreshLogView();
  } catch (error) {
    const status = document.getElementById('log-view-status');
    if (status) status.textContent = 'Could not load log files.';
  } finally {
    _logLoading = false;
  }
}

async function selectLogFile(name) {
  _selectedLog = name;
  await loadLogFiles(false);
  await refreshLogView();
}

async function refreshLogView() {
  const viewer = document.getElementById('log-viewer');
  const status = document.getElementById('log-view-status');
  if (!viewer || !_selectedLog) return;
  try {
    const response = await fetch(`/api/logs/${encodeURIComponent(_selectedLog)}`);
    const data = await readJsonResponse(response);
    if (!response.ok) throw new Error(data.error || 'Log unavailable');
    const follow = document.getElementById('log-auto-scroll')?.checked && data.active;
    const wasNearBottom = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight < 40;
    viewer.textContent = data.content || '';
    if (status) {
      status.textContent = `${data.name} · ${formatBytes(data.size_bytes)}${data.truncated ? ' · showing the most recent portion' : ''}`;
    }
    if (follow || wasNearBottom) viewer.scrollTop = viewer.scrollHeight;
  } catch (error) {
    if (status) status.textContent = error.message || 'Could not read log.';
  }
}

// ── Settings ──────────────────────────────────────────────────────────────────
let _settingsData = { instances: [], repair_workers: [], features: {trash_removal:true,metadata_health:true,timestamp_repair:true,library_refresh:true,mark_watched:true}, mark_watched:{visible_libraries:null,webhook_secret:'',webhook_secret_configured:false}, default_cron: '0 * * * *', discord_webhook: '', notification_destinations: [], log_level: 'INFO', log_max_file_size_mb: 5, log_max_total_size_mb: 50, log_retention_days: 14, max_trash_items: 1000, max_trash_percent: 25, notify_emptied: true, notify_health_fail: true, notify_error: true, notify_clean: false, notify_skip: false };
let _trashSettingsInstance = 0;

async function loadSettings() {
  try {
    const r = await fetch('/api/config/load');
    const d = await readJsonResponse(r, 'Settings');
    if (!r.ok || !d.ok) throw new Error(d.error || 'Could not load config');
    const cfg = d.config || {};
    _settingsData.discord_webhook    = cfg.discord_webhook || '';
    const destinations = Array.isArray(cfg.notifications?.destinations) ? cfg.notifications.destinations : [];
    _settingsData.notification_destinations = destinations.map(destination => ({
      name: destination.name || '',
      service: destination.service || 'custom',
      url: destination.url || '',
      enabled: destination.enabled !== false,
      events: Array.isArray(destination.events) ? destination.events : ['emptied','clean','health_fail','error','skip'],
    }));
    _settingsData.log_level          = cfg.log_level || 'INFO';
    _settingsData.log_max_file_size_mb = cfg.logging?.max_file_size_mb ?? 5;
    _settingsData.log_max_total_size_mb = cfg.logging?.max_total_size_mb ?? 50;
    _settingsData.log_retention_days = cfg.logging?.retention_days ?? 14;
    _settingsData.default_cron       = cfg.schedule?.default_cron || '0 * * * *';
    _settingsData.features = {
      trash_removal: cfg.features?.trash_removal !== false,
      metadata_health: cfg.features?.metadata_health !== false,
      timestamp_repair: cfg.features?.timestamp_repair !== false,
      library_refresh: cfg.features?.library_refresh !== false,
      mark_watched: cfg.features?.mark_watched !== false,
    };
    _settingsData.mark_watched = {
      visible_libraries: Array.isArray(cfg.mark_watched?.visible_libraries) ? cfg.mark_watched.visible_libraries : null,
      give_up_after_hours: cfg.mark_watched?.give_up_after_hours ?? 120,
      workers: cfg.mark_watched?.workers ?? 4,
      scan_on_import: cfg.mark_watched?.scan_on_import !== false,
      webhook_secret: '',
      webhook_secret_configured: cfg.mark_watched?.webhook_secret_configured === true,
      retry_delays: cfg.mark_watched?.retry_delays || [10,30,60,120,300],
    };
    _settingsData.max_trash_items    = cfg.max_trash_items ?? 1000;
    _settingsData.max_trash_percent  = cfg.max_trash_percent ?? 25;
    _settingsData.notify_emptied     = cfg.notify?.on_emptied     ?? cfg.notify?.on_success ?? true;
    _settingsData.notify_health_fail = cfg.notify?.on_health_fail ?? cfg.notify?.on_failure ?? true;
    _settingsData.notify_error       = cfg.notify?.on_error       ?? true;
    _settingsData.notify_clean       = cfg.notify?.on_clean       ?? false;
    _settingsData.notify_skip        = cfg.notify?.on_skip        ?? false;
    const repairWorkers = Array.isArray(cfg.timestamp_repair_workers) ? cfg.timestamp_repair_workers : [];
    _settingsData.repair_workers = repairWorkers.map(worker => ({
      name: worker.name || '',
      url: worker.url || '',
      token: worker.token || '',
      controller_url: worker.controller_url || '',
    }));
    const plexInstances = Array.isArray(cfg.plex_instances) ? cfg.plex_instances : [];
    _settingsData.instances = plexInstances.map(inst => ({
      name:      inst.name || '',
      url:       inst.url  || '',
      token:     inst.token || '',
      machine_id: inst.machine_id || '',
      timestamp_repair: {
        enabled: inst.timestamp_repair?.enabled === true,
        worker: inst.timestamp_repair?.worker || 'local',
        database_path: inst.timestamp_repair?.database_path || '',
        allowed_prefixes: Array.isArray(inst.timestamp_repair?.allowed_prefixes) ? inst.timestamp_repair.allowed_prefixes : [],
        max_files_per_folder: inst.timestamp_repair?.max_files_per_folder ?? 5,
        scan_timeout_seconds: inst.timestamp_repair?.scan_timeout_seconds ?? 1800,
        poll_interval_seconds: inst.timestamp_repair?.poll_interval_seconds ?? 5,
        heartbeat_seconds: inst.timestamp_repair?.heartbeat_seconds ?? 30,
      },
      metadata_health: {
        ignored_libraries: Array.isArray(inst.metadata_health?.ignored_libraries) ? inst.metadata_health.ignored_libraries : [],
      },
      libraries: (Array.isArray(inst.libraries) ? inst.libraries : []).map(lib => ({
        name:  lib.name  || '',
        type:  lib.type  || 'physical',
        cron:  lib.cron  || '',
        section_id: lib.section_id ?? null,
        refresh_enabled: lib.refresh_enabled === true,
        refresh_cron: lib.refresh_cron || '0 * * * *',
        refresh_guard_minutes: lib.refresh_guard_minutes ?? 15,
        paths: (Array.isArray(lib.paths) ? lib.paths : []).map(p => ({
          path:            p.path          || '',
          type:            p.type          || 'physical',
          min_threshold:   p.min_threshold ?? 90,
          provider_checks: p.provider_checks || [],
        }))
      }))
    }));
    renderSettingsInstances();
    renderTrashRemovalSettings(_trashSettingsInstance);
    renderRepairSettings();
    renderMetadataHealthSettings();
    renderLibraryRefreshSettings();
    renderFeatureSettings();
    renderMarkWatchedSettings();
    applyFeatureVisibility(_settingsData.features);
    loadGlobalScheduleControls();
    // Populate notification fields
    document.getElementById('s-discord').value = _settingsData.discord_webhook;
    const logLevel = document.getElementById('s-log-level');
    if (logLevel) logLevel.value = _settingsData.log_level;
    document.getElementById('s-log-file-size').value = _settingsData.log_max_file_size_mb;
    document.getElementById('s-log-total-size').value = _settingsData.log_max_total_size_mb;
    document.getElementById('s-log-retention-days').value = _settingsData.log_retention_days;
    document.getElementById('s-max-trash-items').value = _settingsData.max_trash_items;
    document.getElementById('s-max-trash-percent').value = _settingsData.max_trash_percent;
    document.getElementById('s-notify-emptied').checked     = cfg.notify?.on_emptied     ?? cfg.notify?.on_success ?? true;
    document.getElementById('s-notify-health-fail').checked = cfg.notify?.on_health_fail  ?? cfg.notify?.on_failure ?? true;
    document.getElementById('s-notify-error').checked       = cfg.notify?.on_error        ?? true;
    document.getElementById('s-notify-clean').checked       = cfg.notify?.on_clean        ?? false;
    document.getElementById('s-notify-skip').checked        = cfg.notify?.on_skip         ?? false;
    renderNotificationDestinations();
    // Pre-populate username if set
    const authUser = document.getElementById('s-auth-user');
    if (authUser && cfg.auth && cfg.auth.username) authUser.value = cfg.auth.username;
    // Show placeholder dots for saved provider keys
    const provCfg = cfg.providers || {};
    ['realdebrid','alldebrid','torbox','debridlink'].forEach(p => {
      const el = document.getElementById(`pkey-${p}`);
      if (el && provCfg[p]?.api_key) el.placeholder = '••••••••  (saved — enter new key to replace)';
    });
    if (d.recovered_instances) toast('Plex instances recovered from the running configuration', 'warn');
  } catch(e) {
    const message = e.message || 'Error loading settings';
    ['settings-instances-list','library-refresh-settings','mark-watched-settings'].forEach(id => {
      const target = document.getElementById(id);
      if (target) target.innerHTML = `<div class="repair-warning">${h(message)}. Existing Plex configuration has not been changed.</div>`;
    });
    toast(message, 'fail');
  }
}

function renderSettingsInstances() {
  const container = document.getElementById('settings-instances-list');
  if (!container) return;
  if (!_settingsData.instances.length) {
    container.innerHTML = '<div style="color:var(--muted);font-size:12px;margin-bottom:16px;">No instances configured yet.</div>';
    return;
  }
  container.innerHTML = _settingsData.instances.map((inst, ii) => `
    <div class="inst-settings-card">
      <div class="inst-settings-header">
        <div>
          <div class="inst-settings-name">${h(inst.name || 'Unnamed Instance')}</div>
          <div style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:2px;">${h(inst.url || '')}</div>
        </div>
        <div style="display:flex;gap:6px;">
          <button class="btn btn-danger btn-sm" onclick="settingsRemoveInstance(${ii})">Remove</button>
        </div>
      </div>
      <div class="inst-settings-body">
        <div class="form-row">
          <div class="form-group">
            <label class="form-label">Instance Name</label>
            <input class="form-input" value="${h(inst.name)}" onchange="_settingsData.instances[${ii}].name=this.value;renderSettingsInstances()" />
            <div class="form-hint">Saved to config.yml; optional override: <code style="color:var(--accent2)">PLEX_TOKEN_${h(inst.name.toUpperCase().replace(/ /g,'_').replace(/-/g,'_'))}</code></div>
          </div>
          <div class="form-group">
            <label class="form-label">Plex URL</label>
            <input class="form-input" value="${h(inst.url)}" onchange="_settingsData.instances[${ii}].url=this.value" />
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Plex token</label>
          <input class="form-input" type="password" value="${h(inst.token||'')}" placeholder="Saved in persistent config.yml"
                 onchange="_settingsData.instances[${ii}].token=this.value" />
        </div>
      </div>
    </div>
  `).join('');
}

function renderTrashRemovalSettings(selectedIndex = _trashSettingsInstance) {
  const select = document.getElementById('trash-settings-instance');
  const container = document.getElementById('trash-settings-libraries');
  if (!select || !container) return;
  if (!_settingsData.instances.length) {
    select.innerHTML = '<option>No Plex instances configured</option>';
    select.disabled = true;
    container.innerHTML = '<div class="empty-msg">Add a Plex instance first.</div>';
    return;
  }
  select.disabled = false;
  _trashSettingsInstance = Math.max(0, Math.min(Number(selectedIndex)||0, _settingsData.instances.length-1));
  select.innerHTML = _settingsData.instances.map((instance,index) => `<option value="${index}" ${index===_trashSettingsInstance?'selected':''}>${h(instance.name||`Plex instance ${index+1}`)}</option>`).join('');
  const instance = _settingsData.instances[_trashSettingsInstance];
  container.innerHTML = `<div class="inst-settings-card"><div class="inst-settings-header"><div><div class="inst-settings-name">${h(instance.name||'Unnamed Plex instance')}</div><div class="form-hint">Trash Removal libraries and safety paths</div></div></div><div class="inst-settings-body"><div id="si-libs-${_trashSettingsInstance}">${renderSettingsLibraries(instance.libraries,_trashSettingsInstance)}</div><button class="btn btn-secondary btn-sm" onclick="settingsAddLibrary(${_trashSettingsInstance})">+ Add monitored library</button></div></div>`;
}

function renderFeatureSettings() {
  Object.entries(_settingsData.features).forEach(([name,enabled]) => {
    const button = document.getElementById(`feature-${name.replaceAll('_','-')}`);
    if (!button) return;
    button.classList.toggle('ignored', !enabled);
    button.innerHTML = `<span>${enabled?'Enabled':'Disabled'}</span><span>${enabled?'Shown and active':'Hidden and inactive'}</span>`;
  });
}

function toggleFeatureSetting(name) {
  _settingsData.features[name] = !_settingsData.features[name];
  renderFeatureSettings();
}

function repairConfig(instance) {
  instance.timestamp_repair ||= {
    enabled:false, worker:'local', database_path:'', allowed_prefixes:[],
    max_files_per_folder:5, scan_timeout_seconds:1800,
    poll_interval_seconds:5, heartbeat_seconds:30,
  };
  return instance.timestamp_repair;
}

function generateRepairWorkerToken() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, value => value.toString(16).padStart(2,'0')).join('');
}

function addRepairWorker() {
  const index = _settingsData.repair_workers.length;
  _settingsData.repair_workers.push({
    name:'altmount-worker', url:'http://vm-altmount:8223',
    controller_url:`${location.protocol}//${location.hostname}:${location.port || (location.protocol==='https:'?'443':'80')}`,
    token:generateRepairWorkerToken(),
  });
  renderRepairSettings();
  requestAnimationFrame(() => scrollToRepairWorker(index));
  toast('Worker added. Choose its Plex server in this card.', 'warn');
}

function removeRepairWorker(index) {
  const worker = _settingsData.repair_workers[index];
  if (!confirm(`Remove repair worker "${worker.name}"? Assigned Plex instances will be returned to local mode.`)) return;
  _settingsData.instances.forEach(instance => {
    const repair = repairConfig(instance);
    if (repair.worker === worker.name) repair.worker = 'local';
  });
  _settingsData.repair_workers.splice(index,1);
  renderRepairSettings();
}

function renameRepairWorker(index, name) {
  const previous = _settingsData.repair_workers[index].name;
  _settingsData.repair_workers[index].name = name.trim();
  _settingsData.instances.forEach(instance => {
    const repair = repairConfig(instance);
    if (repair.worker === previous) repair.worker = name.trim();
  });
  renderRepairSettings();
}

function repairWorkerOptions(selected) {
  return [`<option value="local" ${selected==='local'?'selected':''}>This ${PRODUCT_NAME} container</option>`,
    ..._settingsData.repair_workers.map(worker => `<option value="${h(worker.name)}" ${selected===worker.name?'selected':''}>Remote: ${h(worker.name)}</option>`)
  ].join('');
}

function assignRepairWorker(workerIndex) {
  const select = document.getElementById(`repair-worker-instance-${workerIndex}`);
  if (!select?.value) {
    toast('Choose a Plex server first.', 'fail');
    return;
  }
  const instanceIndex = Number(select?.value);
  if (!Number.isInteger(instanceIndex) || !_settingsData.instances[instanceIndex]) {
    toast('Choose a Plex server first.', 'fail');
    return;
  }
  const repair = repairConfig(_settingsData.instances[instanceIndex]);
  repair.worker = _settingsData.repair_workers[workerIndex].name;
  repair.enabled = true;
  renderRepairSettings();
  requestAnimationFrame(() => scrollToRepairInstance(instanceIndex));
}

function unassignRepairWorker(workerIndex, instanceIndex) {
  const repair = repairConfig(_settingsData.instances[instanceIndex]);
  if (repair.worker === _settingsData.repair_workers[workerIndex].name) {
    repair.worker = 'local';
    repair.enabled = false;
  }
  renderRepairSettings();
  requestAnimationFrame(() => scrollToRepairWorker(workerIndex));
}

function scrollToRepairWorker(index) {
  document.getElementById(`repair-worker-card-${index}`)?.scrollIntoView({behavior:'smooth',block:'center'});
}

function scrollToRepairInstance(index) {
  document.getElementById(`repair-instance-card-${index}`)?.scrollIntoView({behavior:'smooth',block:'start'});
}

function workerCompose(index) {
  const worker = _settingsData.repair_workers[index];
  const assigned = _settingsData.instances.filter(instance => repairConfig(instance).worker === worker.name);
  const databaseDirs = [...new Set(assigned.map(instance => repairConfig(instance).database_path.replace(/[\\/][^\\/]+$/, '')).filter(Boolean))];
  const prefixes = [...new Set(assigned.flatMap(instance => repairConfig(instance).allowed_prefixes).filter(Boolean))];
  const volumes = [
    ...databaseDirs.map((path,i) => `      - \${MEDIAMENDER_PLEX_DATABASE_DIR_${i+1}:?Set an absolute Plex database host path in Dockge .env}:${path}:ro`),
    ...prefixes.map((path,i) => `      - \${MEDIAMENDER_REPAIR_FOLDER_${i+1}:?Set an absolute repair-folder host path in Dockge .env}:${path}:rw,slave`),
  ];
  return `services:\n  mediamender-repair-worker:\n    image: liftbridgelabs/mediamender:latest\n    container_name: mediamender-repair-worker\n    restart: unless-stopped\n    ports:\n      - "8223:8223"\n    environment:\n      - MEDIAMENDER_ROLE=repair-worker\n      - MEDIAMENDER_WORKER_NAME=${worker.name}\n      - MEDIAMENDER_WORKER_TOKEN=\${MEDIAMENDER_WORKER_TOKEN:?Paste the pairing secret into Dockge .env}\n      - MEDIAMENDER_WORKER_DATABASE_ROOTS=${databaseDirs.join(',') || '/plex-db'}\n      - MEDIAMENDER_WORKER_MEDIA_ROOTS=${prefixes.join(',') || '/repair-media'}\n      - PUID=99\n      - PGID=100\n    volumes:\n      - \${MEDIAMENDER_WORKER_DATA_DIR:?Set an absolute worker-data host path in Dockge .env}:/app/data\n${volumes.join('\n')}`;
}

async function copyWorkerCompose(index) {
  const worker = _settingsData.repair_workers[index];
  const assigned = _settingsData.instances.filter(instance => repairConfig(instance).worker === worker.name);
  if (!assigned.length) {
    toast('Assign at least one Plex instance to this worker before copying Compose.', 'fail');
    return;
  }
  const incomplete = assigned.filter(instance => {
    const repair = repairConfig(instance);
    return !repair.database_path || !repair.allowed_prefixes.length;
  });
  if (incomplete.length) {
    toast(`Finish database and repair-folder setup for ${incomplete.map(instance=>instance.name).join(', ')} first.`, 'fail');
    return;
  }
  const text = workerCompose(index);
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else throw new Error('Clipboard API unavailable');
  } catch (_) {
    const area = document.createElement('textarea');
    area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
    document.body.appendChild(area); area.select(); document.execCommand('copy'); area.remove();
  }
  toast('Worker Compose copied. Set every required absolute host path in Dockge .env before deploying.', 'pass');
}

async function testRepairWorker(index) {
  const worker = _settingsData.repair_workers[index];
  const result = document.getElementById(`repair-worker-test-${index}`);
  result.textContent = 'Testing…';
  try {
    const response = await fetch('/api/timestamp-repair/worker-test', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(worker),
    });
    const data = await readJsonResponse(response);
    result.textContent = response.ok ? `✓ Connected · ${data.name}` : `✗ ${data.error}`;
    result.style.color = response.ok ? 'var(--pass)' : 'var(--fail2)';
  } catch (_) {
    result.textContent = '✗ Worker test failed'; result.style.color = 'var(--fail2)';
  }
}

function renderMetadataHealthSettings() {
  const target = document.getElementById('metadata-health-settings');
  if (!target) return;
  target.innerHTML = (_settingsData.instances || []).map((instance, index) => {
    const ignored = new Set(instance.metadata_health?.ignored_libraries || []);
    const rows = (instance.libraries || []).map(library => `
      <div class="metadata-setting-row">
        <div><strong>${h(library.name)}</strong><br><span>${h(library.type)} library</span></div>
        <button type="button" class="metadata-setting-toggle ${ignored.has(library.name)?'ignored':''}" onclick="setMetadataLibraryIgnored(${index},${h(JSON.stringify(library.name))},${ignored.has(library.name)?'false':'true'})"><span>${ignored.has(library.name)?'Ignored':'Included'}</span><span>${ignored.has(library.name)?'Skip scan':'Scan for matches'}</span></button>
      </div>`).join('');
    return `<section class="card" style="margin-top:18px;"><div class="card-header"><div><div class="card-title">${h(instance.name)}</div><div class="form-hint">Choose libraries that should not be checked for Plex matches.</div></div></div><div class="card-body"><div class="metadata-settings-list">${rows || '<div class="empty-msg">No libraries configured.</div>'}</div></div></section>`;
  }).join('') || '<div class="empty-msg">No Plex instances configured.</div>';
}

function setMetadataLibraryIgnored(instanceIndex, libraryName, ignored) {
  const instance = _settingsData.instances[instanceIndex];
  if (!instance.metadata_health) instance.metadata_health = {ignored_libraries: []};
  const names = new Set(instance.metadata_health.ignored_libraries || []);
  if (ignored) names.add(libraryName); else names.delete(libraryName);
  instance.metadata_health.ignored_libraries = [...names];
  renderMetadataHealthSettings();
}

function refreshScheduleValue(cron) {
  return ['*/30 * * * *','0 * * * *','0 */2 * * *','0 */6 * * *','0 2 * * *'].includes(cron) ? cron : 'custom';
}

function setLibraryRefreshSchedule(instanceIndex,libraryIndex,value) {
  const library=_settingsData.instances[instanceIndex].libraries[libraryIndex];
  if (value!=='custom') library.refresh_cron=value;
  else if (refreshScheduleValue(library.refresh_cron)!=='custom') library.refresh_cron='0 3 * * *';
  renderLibraryRefreshSettings();
}

function renderLibraryRefreshSettings() {
  const target=document.getElementById('library-refresh-settings');
  if (!target) return;
  target.innerHTML=(_settingsData.instances||[]).map((instance,instanceIndex)=>`<section class="card" style="margin-top:18px;"><div class="card-header"><div><div class="card-title">${h(instance.name)}</div><div class="form-hint">Enable schedules only for libraries that need ${PRODUCT_NAME} to trigger Plex discovery.</div></div></div><div class="card-body"><div class="metadata-settings-list">${(instance.libraries||[]).map((library,libraryIndex)=>{
    const selected=refreshScheduleValue(library.refresh_cron);
    return `<div class="metadata-setting-row" style="align-items:center;"><div><strong>${h(library.name)}</strong><br><span>${library.refresh_enabled?`${h(cronDescription(library.refresh_cron))} &middot; ${Number(library.refresh_guard_minutes)} minute trash hold`:'Manual refresh only'}</span></div><div class="library-refresh-control-grid"><label><span class="form-label">Refresh schedule</span><select class="form-input" onchange="setLibraryRefreshSchedule(${instanceIndex},${libraryIndex},this.value)"><option value="*/30 * * * *" ${selected==='*/30 * * * *'?'selected':''}>Every 30 minutes</option><option value="0 * * * *" ${selected==='0 * * * *'?'selected':''}>Every hour</option><option value="0 */2 * * *" ${selected==='0 */2 * * *'?'selected':''}>Every 2 hours</option><option value="0 */6 * * *" ${selected==='0 */6 * * *'?'selected':''}>Every 6 hours</option><option value="0 2 * * *" ${selected==='0 2 * * *'?'selected':''}>Daily at 2 AM</option><option value="custom" ${selected==='custom'?'selected':''}>Custom schedule</option></select></label><label><span class="form-label">Trash hold (minutes)</span><input class="form-input" type="number" min="1" max="240" value="${Number(library.refresh_guard_minutes)}" onchange="_settingsData.instances[${instanceIndex}].libraries[${libraryIndex}].refresh_guard_minutes=Number(this.value)"></label><button type="button" class="metadata-setting-toggle ${library.refresh_enabled?'':'ignored'}" onclick="_settingsData.instances[${instanceIndex}].libraries[${libraryIndex}].refresh_enabled=!_settingsData.instances[${instanceIndex}].libraries[${libraryIndex}].refresh_enabled;renderLibraryRefreshSettings()"><span>${library.refresh_enabled?'Scheduled':'Manual only'}</span><span>${library.refresh_enabled?'Automatic refresh on':'Automatic refresh off'}</span></button>${selected==='custom'?`<label><span class="form-label">Custom cron expression</span><input class="form-input" value="${h(library.refresh_cron)}" placeholder="0 3 * * *" onchange="_settingsData.instances[${instanceIndex}].libraries[${libraryIndex}].refresh_cron=this.value.trim()"></label><span class="form-hint">minute hour day month weekday</span>`:''}</div></div>`;
  }).join('')||'<div class="empty-msg">No libraries configured.</div>'}</div></div></section>`).join('')||'<div class="empty-msg">No Plex instances configured.</div>';
}

function renderMarkWatchedSettings() {
  const target = document.getElementById('mark-watched-settings');
  if (!target) return;
  const configured = _settingsData.mark_watched.visible_libraries;
  target.innerHTML = (_settingsData.instances || []).map((instance, instanceIndex) => `
    <section class="card" style="margin-top:14px;"><div class="card-header"><span class="card-title">${h(instance.name)}</span></div><div class="card-body"><div class="metadata-settings-list">
      ${(instance.libraries || []).map((library, libraryIndex) => {
        const key = `${instance.name}::${library.name}`;
        const visible = configured === null || configured.includes(key);
        return `<div class="metadata-setting-row"><div><strong>${h(library.name)}</strong><br><span>${visible?'Shown on Mark-it-Watched':'Hidden from Mark-it-Watched'}</span></div><button type="button" class="metadata-setting-toggle ${visible?'':'ignored'}" onclick="setMarkWatchedLibraryVisibility(${instanceIndex},${libraryIndex},${!visible})"><span>${visible?'Visible':'Hidden'}</span><span>${visible?'Users can set rules':'Excluded from rules page'}</span></button></div>`;
      }).join('') || '<div class="empty-msg">No libraries configured.</div>'}
    </div></div></section>`).join('') || '<div class="empty-msg">No Plex instances configured.</div>';
  const giveUp = document.getElementById('s-mw-give-up');
  if (giveUp) giveUp.value = _settingsData.mark_watched.give_up_after_hours ?? 120;
  const workers = document.getElementById('s-mw-workers');
  if (workers) workers.value = _settingsData.mark_watched.workers ?? 4;
  const scan = document.getElementById('s-mw-scan');
  if (scan) {
    const on = _settingsData.mark_watched.scan_on_import !== false;
    scan.classList.toggle('ignored', !on);
    scan.innerHTML = `<span>${on?'Enabled':'Disabled'}</span><span>${on?'Asks Plex to scan':'Polls only'}</span>`;
  }
  const status = document.getElementById('mark-watched-secret-status');
  if (status) status.textContent = `${_settingsData.mark_watched.webhook_secret_configured?'A secret is configured. ':''}Use X-Sonarr-Webhook-Secret or Bearer authentication. The secret is never returned by the API.`;
}

async function loadSonarrConnectionStatus() {
  const status = document.getElementById('mark-watched-sonarr-status');
  const callback = document.getElementById('mark-watched-callback-url');
  const list = document.getElementById('mark-watched-sonarr-connections');
  if (!status || !callback) return;
  if (!callback.value) callback.value = `${window.location.origin}/api/webhooks/sonarr`;
  try {
    const response = await fetch('/api/mark-watched/sonarr');
    const data = await readJsonResponse(response, 'Sonarr status');
    if (!response.ok) throw new Error(data.error || 'Could not load Sonarr status');
    const connections = data.connections || [];
    const latest = connections[0] || {};
    if (latest.callback_url) callback.value = latest.callback_url;
    const configured = connections.filter(item => item.configured_from_environment).length;
    const connected = connections.filter(item => item.status === 'connected').length;
    const failed = connections.filter(item => item.status === 'failed').length;
    if (connections.length) {
      const waiting = connections.filter(item => item.status === 'not_connected').length;
      status.textContent = `${configured} configured · ${connected} ${connected===1?'webhook':'webhooks'} installed${waiting?` · ${waiting} not connected`:''}${failed?` · ${failed} need attention`:''}.`;
      status.style.color = failed && !connected ? 'var(--fail2)' : 'var(--pass)';
    } else {
      status.textContent = 'No Sonarr connections recorded yet.';
      status.style.color = '';
    }
    if (list) list.innerHTML = connections.map(connection => {
      const installed = connection.status === 'connected';
      const action = installed ? 'Repair / test' : connection.status === 'failed' ? 'Retry webhook' : 'Install webhook';
      const version = connection.sonarr_version ? ` · v${h(connection.sonarr_version)}` : '';
      const verified = connection.last_success ? `<br><span>Last verified ${h(fmtAgo(connection.last_success))}</span>` : '';
      const missingKey = !connection.api_key_available ? '<br><span style="color:var(--warn2);">API key required</span>' : '';
      const remove = connection.saved_record ? `<button class="btn btn-danger btn-sm" onclick="removeSonarrConnection(${h(JSON.stringify(connection.sonarr_url || ''))},${installed||connection.notification_id?'true':'false'},${connection.api_key_available?'true':'false'},this)">Remove</button>` : '';
      return `<div class="metadata-setting-row"><div><strong>${h(connection.sonarr_instance || connection.environment_label || 'Sonarr')}</strong><br><span>${h(connection.sonarr_url || '')}${version}</span>${verified}${missingKey}${connection.error?`<br><span style="color:var(--fail2);">${h(connection.error)}</span>`:''}</div><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end;"><span class="badge ${installed?'success':connection.status==='failed'?'error':'skipped'}">${installed?'webhook installed':connection.status==='failed'?'needs attention':'not connected'}</span><button class="btn ${installed?'btn-secondary':'btn-primary'} btn-sm" onclick="connectSonarr(${h(JSON.stringify(connection.sonarr_url || ''))},this)">${action}</button>${remove}</div></div>`;
    }).join('') || '<div class="empty-msg">No Sonarr environment URLs found. Enter one above to connect it manually.</div>';
  } catch (error) {
    status.textContent = error.message || 'Could not load Sonarr status';
    status.style.color = 'var(--fail2)';
  }
}

async function connectSonarr(configuredUrl = '', actionButton = null) {
  const button = actionButton || document.getElementById('mark-watched-sonarr-connect');
  const status = document.getElementById('mark-watched-sonarr-status');
  const apiKey = document.getElementById('mark-watched-sonarr-api-key');
  const payload = {
    sonarr_url: configuredUrl || document.getElementById('mark-watched-sonarr-url')?.value.trim() || '',
    api_key: apiKey?.value || '',
    callback_url: document.getElementById('mark-watched-callback-url')?.value.trim() || '',
  };
  if (!payload.sonarr_url || !payload.callback_url) {
    toast('Sonarr URL and callback URL are required', 'fail');
    return;
  }
  button.disabled = true;
  button.dataset.label ||= button.innerHTML;
  button.innerHTML = '<span class="spin"></span> connecting&hellip;';
  status.textContent = 'Verifying Sonarr, testing the callback, and saving the webhook&hellip;';
  status.style.color = '';
  try {
    const response = await fetch('/api/mark-watched/sonarr/connect', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload),
    });
    const data = await readJsonResponse(response, 'Sonarr connection');
    if (!response.ok) throw new Error(data.error || 'Sonarr connection failed');
    if (apiKey) apiKey.value = '';
    _settingsData.mark_watched.webhook_secret_configured = true;
    renderMarkWatchedSettings();
    await loadSonarrConnectionStatus();
    toast(data.message || 'Sonarr connected', 'pass');
  } catch (error) {
    if (apiKey) apiKey.value = '';
    status.textContent = error.message || 'Sonarr connection failed';
    status.style.color = 'var(--fail2)';
    toast(status.textContent, 'fail');
  } finally {
    button.disabled = false;
    button.innerHTML = button.dataset.label || 'Connect entered Sonarr';
  }
}

async function removeSonarrConnection(sonarrUrl, hasRemoteWebhook, apiKeyAvailable, button) {
  if (!confirm(`Remove ${sonarrUrl} from mediaMender?${hasRemoteWebhook?' Its managed webhook will also be deleted from Sonarr.':''}`)) return;
  let apiKey = '';
  if (hasRemoteWebhook && !apiKeyAvailable) {
    // Cancelling used to abort the removal entirely, so a connection whose key you no longer had
    // could never be cleared. Skipping the key now removes it locally and says what was left in
    // Sonarr.
    apiKey = prompt('Sonarr API key, to delete its webhook. Leave blank to just forget the connection here.') || '';
  }
  button.disabled = true;
  try {
    const response = await fetch('/api/mark-watched/sonarr', {
      method:'DELETE', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({sonarr_url:sonarrUrl, api_key:apiKey}),
    });
    const data = await readJsonResponse(response, 'Sonarr removal');
    if (!response.ok) throw new Error(data.error || 'Sonarr connection could not be removed');
    await loadSonarrConnectionStatus();
    toast(data.message || 'Sonarr connection removed', data.webhook_left_behind ? 'warn' : 'pass');
  } catch (error) {
    button.disabled = false;
    toast(error.message || 'Sonarr connection could not be removed', 'fail');
  }
}

function setMarkWatchedLibraryVisibility(instanceIndex, libraryIndex, visible) {
  if (_settingsData.mark_watched.visible_libraries === null) {
    _settingsData.mark_watched.visible_libraries = _settingsData.instances.flatMap(instance =>
      instance.libraries.map(library => `${instance.name}::${library.name}`));
  }
  const instance = _settingsData.instances[instanceIndex];
  const key = `${instance.name}::${instance.libraries[libraryIndex].name}`;
  const values = new Set(_settingsData.mark_watched.visible_libraries);
  if (visible) values.add(key); else values.delete(key);
  _settingsData.mark_watched.visible_libraries = [...values];
  renderMarkWatchedSettings();
}

function toggleScanOnImport() {
  _settingsData.mark_watched.scan_on_import =
    _settingsData.mark_watched.scan_on_import === false;
  renderMarkWatchedSettings();
}

function useAllMarkWatchedLibraries() {
  _settingsData.mark_watched.visible_libraries = null;
  renderMarkWatchedSettings();
}

function renderRepairSettings() {
  const workers = document.getElementById('repair-workers-settings');
  const instances = document.getElementById('repair-instances-settings');
  if (!workers || !instances) return;
  workers.innerHTML = _settingsData.repair_workers.length ? _settingsData.repair_workers.map((worker,index) => {
    const assigned = _settingsData.instances.filter(instance => repairConfig(instance).worker === worker.name);
    const complete = assigned.length && assigned.every(instance => {
      const repair = repairConfig(instance);
      return repair.database_path && repair.allowed_prefixes.length;
    });
    const available = _settingsData.instances
      .map((instance,instanceIndex) => ({instance,instanceIndex}))
      .filter(({instance}) => repairConfig(instance).worker !== worker.name);
    const assignmentText = !assigned.length
      ? 'Choose the Plex server that runs on this worker machine.'
      : `${assigned.length} Plex server${assigned.length===1?'':'s'} assigned${complete?' · ready to copy Compose':' · path setup still required'}`;
    return `
    <div id="repair-worker-card-${index}" style="border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:14px;scroll-margin-top:90px;">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px;"><strong style="font-size:16px;color:var(--bright);">${h(worker.name || 'Unnamed worker')}</strong><button class="btn btn-danger btn-sm" onclick="removeRepairWorker(${index})">Remove</button></div>
      <div class="form-row">
        <div class="form-group"><label class="form-label">Worker name</label><input class="form-input" value="${h(worker.name)}" onchange="renameRepairWorker(${index},this.value)"></div>
        <div class="form-group"><label class="form-label">Worker URL from controller</label><input class="form-input" value="${h(worker.url)}" placeholder="http://vm-altmount:8223" onchange="_settingsData.repair_workers[${index}].url=this.value.trim()"></div>
      </div>
      <div class="form-group"><label class="form-label">Controller URL from worker</label><input class="form-input" value="${h(worker.controller_url)}" placeholder="http://UNRAID-IP:8222" onchange="_settingsData.repair_workers[${index}].controller_url=this.value.trim()"><div class="form-hint">The VM worker uses this address only for approved path-limited Plex scans.</div></div>
      <div class="form-group"><label class="form-label">Pairing secret</label><div style="display:flex;gap:8px;"><input class="form-input" type="password" value="${h(worker.token)}" onchange="_settingsData.repair_workers[${index}].token=this.value"><button class="btn btn-secondary" onclick="_settingsData.repair_workers[${index}].token=generateRepairWorkerToken();renderRepairSettings()">Regenerate</button></div></div>
      <div class="form-group"><label class="form-label">1. Assign a Plex server</label><div style="display:flex;gap:8px;align-items:center;"><select class="form-input" id="repair-worker-instance-${index}"><option value="">Choose Plex server…</option>${available.map(({instance,instanceIndex})=>`<option value="${instanceIndex}">${h(instance.name)}</option>`).join('')}</select><button class="btn btn-primary" type="button" onclick="assignRepairWorker(${index})" ${available.length?'':'disabled'}>Assign &amp; configure</button></div></div>
      ${assigned.map(instance => { const instanceIndex=_settingsData.instances.indexOf(instance); const ready=Boolean(repairConfig(instance).database_path && repairConfig(instance).allowed_prefixes.length); return `<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 13px;margin-bottom:9px;border:1px solid var(--border);border-radius:8px;background:var(--bg);"><div><strong>${h(instance.name)}</strong><div class="form-hint">${ready?'Paths configured':'Database and repair folders needed'}</div></div><div style="display:flex;gap:8px;"><button class="btn btn-secondary btn-sm" type="button" onclick="scrollToRepairInstance(${instanceIndex})">${ready?'Review setup':'Configure paths'}</button><button class="btn btn-danger btn-sm" type="button" onclick="unassignRepairWorker(${index},${instanceIndex})">Unassign</button></div></div>`; }).join('')}
      <div class="repair-warning" style="margin:12px 0 14px;">${h(assignmentText)}</div>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;"><button class="btn btn-secondary btn-sm" onclick="testRepairWorker(${index})">Test connection</button><button class="btn btn-secondary btn-sm" onclick="copyWorkerCompose(${index})" ${complete?'':'disabled'} title="${complete?'Copy a complete worker definition':'Assign and finish a Plex server below first'}">Copy worker Compose</button><span id="repair-worker-test-${index}" class="form-hint" style="margin:0;"></span></div>
    </div>`;
  }).join('') : '<div class="empty-msg">No remote worker is needed for Plex servers whose database and repair paths are mounted into this container.</div>';

  const orderedInstances = _settingsData.instances.map((instance,index) => ({instance,index})).sort((left,right) => Number(repairConfig(right.instance).enabled)-Number(repairConfig(left.instance).enabled));
  instances.innerHTML = orderedInstances.map(({instance,index}) => {
    const repair = repairConfig(instance);
    const remoteWorkerIndex = _settingsData.repair_workers.findIndex(worker => worker.name === repair.worker);
    const remoteReady = remoteWorkerIndex >= 0 && Boolean(repair.database_path && repair.allowed_prefixes.length);
    return `<div class="card" id="repair-instance-card-${index}" style="margin-bottom:18px;scroll-margin-top:90px;">
      <div class="card-header"><div><div class="card-title">${h(instance.name)}</div><div class="form-hint">Manual repair only; this does not change Empty Trash scheduling.</div></div><label style="display:flex;align-items:center;gap:8px;font-size:14px;color:var(--text2);"><input type="checkbox" ${repair.enabled?'checked':''} onchange="repairConfig(_settingsData.instances[${index}]).enabled=this.checked;renderRepairSettings()"> Enable</label></div>
      <div class="card-body">
        ${remoteWorkerIndex >= 0 ? `<div class="repair-warning" style="margin:0 0 16px;"><strong>2. Configure ${h(instance.name)}</strong> for ${h(repair.worker)}. Complete the database and repair-folder fields below.</div>` : ''}
        <div class="form-row">
          <div class="form-group"><label class="form-label">Runs on</label><select class="form-input" onchange="repairConfig(_settingsData.instances[${index}]).worker=this.value;renderRepairSettings()">${repairWorkerOptions(repair.worker)}</select></div>
          <div class="form-group"><label class="form-label">Maximum affected files per folder</label><input class="form-input" type="number" min="1" max="100" value="${repair.max_files_per_folder}" onchange="repairConfig(_settingsData.instances[${index}]).max_files_per_folder=Number(this.value)"></div>
        </div>
        <div class="form-group"><label class="form-label">Plex database used by this server</label><div style="display:flex;gap:8px;"><input class="form-input" value="${h(repair.database_path)}" placeholder="/plex-db/server/com.plexapp.plugins.library.db" onchange="repairConfig(_settingsData.instances[${index}]).database_path=this.value.trim();renderRepairSettings();requestAnimationFrame(()=>scrollToRepairInstance(${index}))"><button class="btn btn-secondary" onclick="discoverRepairDatabases(${index})">Discover</button></div><div class="form-hint" id="repair-db-result-${index}">Click Discover, then choose this Plex server's database from the list. Every displayed value is a path inside the ${PRODUCT_NAME} container.</div></div>
        <div class="form-group"><label class="form-label">Folders ${PRODUCT_NAME} may repair</label><textarea class="form-input" rows="3" placeholder="/mnt/symlink_media/symlinks/nzbdav" onchange="repairConfig(_settingsData.instances[${index}]).allowed_prefixes=this.value.split(/\\r?\\n/).map(v=>v.trim()).filter(Boolean);renderRepairSettings();requestAnimationFrame(()=>scrollToRepairInstance(${index}))">${h(repair.allowed_prefixes.join('\n'))}</textarea><div class="form-hint">Enter the <strong>Container Path</strong> from each narrow read/write Unraid mapping, one per line. For your nzbdav mapping, enter <code>/mnt/symlink_media/symlinks/nzbdav</code>. ${PRODUCT_NAME} refuses to rename anything outside the folders listed here.</div></div>
        ${remoteWorkerIndex >= 0 ? `<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding-top:8px;border-top:1px solid var(--border);"><span class="form-hint">3. ${remoteReady?'Setup complete. Copy the worker deployment now.':'Complete both path fields to unlock Compose.'}</span><div style="display:flex;gap:8px;"><button class="btn btn-secondary" type="button" onclick="scrollToRepairWorker(${remoteWorkerIndex})">Back to worker</button><button class="btn btn-primary" type="button" onclick="copyWorkerCompose(${remoteWorkerIndex})" ${remoteReady?'':'disabled'}>Copy worker Compose</button></div></div>` : ''}
      </div>
    </div>`;
  }).join('') || '<div class="empty-msg">Connect a Plex instance first.</div>';
}

async function discoverRepairDatabases(index) {
  const repair = repairConfig(_settingsData.instances[index]);
  const target = document.getElementById(`repair-db-result-${index}`);
  target.textContent = 'Searching mounted database roots…';
  try {
    const response = await fetch('/api/timestamp-repair/databases', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({worker:repair.worker})});
    const data = await readJsonResponse(response);
    if (!response.ok) throw new Error(data.error || 'Discovery failed');
    if (data.databases.length === 1) {
      repair.database_path = data.databases[0]; renderRepairSettings();
      toast('Plex database selected', 'pass'); return;
    }
    if (!data.databases.length) { target.textContent = 'No Plex databases found. Check the read-only Docker mount.'; return; }
    target.innerHTML = `<strong>Found ${data.databases.length} database files. Choose the one belonging to this Plex server:</strong><div class="repair-db-options">${data.databases.map(path => `<button type="button" class="repair-db-option" data-repair-db="${h(path)}" data-repair-index="${index}">${h(path)}<span>Use this database</span></button>`).join('')}</div>`;
    target.querySelectorAll('[data-repair-db]').forEach(button => button.addEventListener('click', () => { repairConfig(_settingsData.instances[Number(button.dataset.repairIndex)]).database_path=button.dataset.repairDb;renderRepairSettings(); }));
  } catch (error) { target.textContent = error.message; }
}

function cronDescription(cron) {
  const known = {
    '*/30 * * * *': 'Every 30 minutes',
    '0 * * * *': 'Every hour',
    '0 */2 * * *': 'Every 2 hours',
    '0 */6 * * *': 'Every 6 hours',
  };
  if (known[cron]) return known[cron];
  const daily = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(cron || '');
  if (daily) {
    const time = `${String(daily[2]).padStart(2,'0')}:${String(daily[1]).padStart(2,'0')}`;
    return `Daily at ${time}`;
  }
  return cron ? 'Custom schedule' : 'Every hour';
}

function isCustomCron(cron) {
  if (!cron) return false;
  if (['*/30 * * * *','0 * * * *','0 */2 * * *','0 */6 * * *','0 2 * * *'].includes(cron)) return false;
  return !/^(\d{1,2}) (\d{1,2}) \* \* \*$/.test(cron);
}

function applyScheduleLabels() {
  document.querySelectorAll('[data-schedule-cron]').forEach(label => {
    const prefix = label.dataset.scheduleGlobal === 'true' ? 'Global · ' : '';
    label.textContent = `${prefix}${cronDescription(label.dataset.scheduleCron)}`;
  });
}

function loadGlobalScheduleControls() {
  const select = document.getElementById('s-global-schedule');
  const time = document.getElementById('s-global-run-at');
  const custom = document.getElementById('s-global-custom-cron');
  if (!select || !time || !custom) return;
  const cron = _settingsData.default_cron || '0 * * * *';
  const known = [...select.options].some(option => option.value === cron && option.value !== 'custom');
  const daily = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(cron);
  if (known) {
    select.value = cron;
  } else if (daily) {
    select.value = 'daily';
    time.value = `${String(daily[2]).padStart(2,'0')}:${String(daily[1]).padStart(2,'0')}`;
  } else {
    select.value = 'custom';
    custom.value = cron;
  }
  updateGlobalScheduleControls();
}

function updateGlobalScheduleControls() {
  const select = document.getElementById('s-global-schedule');
  const group = document.getElementById('s-global-time-group');
  const customGroup = document.getElementById('s-global-custom-group');
  const summary = document.getElementById('s-global-schedule-summary');
  if (!select || !group || !customGroup || !summary) return;
  group.style.display = select.value === 'daily' ? '' : 'none';
  customGroup.style.display = select.value === 'custom' ? '' : 'none';
  const timezone = BOOT.schedulerTimezone;
  const overrideCount = _settingsData.instances.reduce(
    (count, instance) => count + instance.libraries.filter(library => Boolean(library.cron)).length,
    0
  );
  const overrides = overrideCount
    ? ` ${overrideCount} ${overrideCount === 1 ? 'library currently has' : 'libraries currently have'} a per-library override.`
    : ' Every library currently inherits this schedule.';
  summary.textContent = `${cronDescription(_settingsData.default_cron)} in the container timezone (${timezone}). The first automatic run is the next matching clock time after ${PRODUCT_NAME} starts.${overrides}`;
}

function globalScheduleChanged() {
  const select = document.getElementById('s-global-schedule');
  if (!select) return;
  if (select.value === 'custom') {
    const custom = document.getElementById('s-global-custom-cron');
    if (!isCustomCron(_settingsData.default_cron)) _settingsData.default_cron = '0 3 * * *';
    if (custom) custom.value = _settingsData.default_cron;
    updateGlobalScheduleControls();
    return;
  }
  if (select.value === 'daily') globalRunAtChanged();
  else _settingsData.default_cron = select.value;
  updateGlobalScheduleControls();
  renderTrashRemovalSettings();
}

function globalCustomCronChanged() {
  const value = document.getElementById('s-global-custom-cron')?.value.trim();
  if (value) _settingsData.default_cron = value;
  updateGlobalScheduleControls();
  renderTrashRemovalSettings();
}

function globalRunAtChanged() {
  const value = document.getElementById('s-global-run-at')?.value || '02:00';
  const [hour, minute] = value.split(':').map(Number);
  _settingsData.default_cron = `${minute} ${hour} * * *`;
  updateGlobalScheduleControls();
  renderTrashRemovalSettings();
}

function applyGlobalScheduleToAll() {
  _settingsData.instances.forEach(instance => {
    instance.libraries.forEach(library => { library.cron = ''; });
  });
  renderTrashRemovalSettings();
  updateGlobalScheduleControls();
  toast('All libraries will use the global schedule after saving', 'pass');
}

function libraryScheduleChanged(instanceIndex, libraryIndex, value) {
  const library = _settingsData.instances[instanceIndex].libraries[libraryIndex];
  const group = document.getElementById(`s-lib-custom-${instanceIndex}-${libraryIndex}`);
  if (value === 'custom') {
    if (!isCustomCron(library.cron)) library.cron = '0 3 * * *';
    if (group) {
      group.style.display = '';
      const input = group.querySelector('input');
      if (input) input.value = library.cron;
    }
  } else {
    library.cron = value;
    if (group) group.style.display = 'none';
  }
  updateGlobalScheduleControls();
}

function libraryCustomCronChanged(instanceIndex, libraryIndex, value) {
  const cron = value.trim();
  if (cron) _settingsData.instances[instanceIndex].libraries[libraryIndex].cron = cron;
  updateGlobalScheduleControls();
}

function renderSettingsLibraries(libs, ii) {
  return libs.map((lib, li) => `
    <div class="lib-settings-item">
      <div class="lib-settings-header">
        <button type="button" class="lib-settings-toggle"
                aria-expanded="false" aria-controls="si-lb-${ii}-${li}"
                onclick="toggleLibSettings('si-lb-${ii}-${li}',this)">
          <span style="display:flex;align-items:center;gap:10px;min-width:0;">
            <span class="lib-settings-name">${h(lib.name)}</span>
            <span class="type-chip ${lib.type}" style="font-size:9px;">${h(lib.type)}</span>
            <span style="font-family:var(--mono);font-size:10px;color:var(--muted);">${h(lib.cron ? cronDescription(lib.cron) : `Global · ${cronDescription(_settingsData.default_cron)}`)}</span>
          </span>
          <span style="display:flex;align-items:center;gap:6px;">
            <span style="font-size:11px;color:var(--muted);">${lib.paths.length} path${lib.paths.length!==1?'s':''}</span>
            <span style="color:var(--muted);">▾</span>
          </span>
        </button>
        <button type="button" class="btn btn-danger btn-xs"
                aria-label="Remove ${h(lib.name)} library"
                onclick="settingsRemoveLibrary(${ii},${li})">✕</button>
      </div>
      <div class="lib-settings-body" id="si-lb-${ii}-${li}">
        <div class="form-row">
          <div class="form-group">
            <label class="form-label">Library Name</label>
            <input class="form-input" value="${h(lib.name)}"
                   onchange="_settingsData.instances[${ii}].libraries[${li}].name=this.value" />
            <div class="form-hint">Must match Plex library name exactly</div>
          </div>
          <div class="form-group">
            <label class="form-label">Type</label>
            <select class="form-input" onchange="_settingsData.instances[${ii}].libraries[${li}].type=this.value">
              <option value="physical" ${lib.type==='physical'?'selected':''}>physical</option>
              <option value="debrid"   ${lib.type==='debrid'?'selected':''}>debrid</option>
              <option value="usenet"   ${lib.type==='usenet'?'selected':''}>usenet</option>
              <option value="mixed"    ${lib.type==='mixed'?'selected':''}>mixed</option>
            </select>
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Library schedule</label>
          <select class="form-input" onchange="libraryScheduleChanged(${ii},${li},this.value)">
            <option value="" ${!lib.cron?'selected':''}>Use global — ${h(cronDescription(_settingsData.default_cron))}</option>
            <option value="*/30 * * * *" ${lib.cron==='*/30 * * * *'?'selected':''}>Every 30 minutes</option>
            <option value="0 * * * *"    ${lib.cron==='0 * * * *'?'selected':''}>Every hour</option>
            <option value="0 */2 * * *"  ${lib.cron==='0 */2 * * *'?'selected':''}>Every 2 hours</option>
            <option value="0 */6 * * *"  ${lib.cron==='0 */6 * * *'?'selected':''}>Every 6 hours</option>
            <option value="0 2 * * *"    ${lib.cron==='0 2 * * *'?'selected':''}>Daily at 2am</option>
            <option value="custom" ${isCustomCron(lib.cron)?'selected':''}>Custom schedule</option>
          </select>
        </div>
        <div class="form-group" id="s-lib-custom-${ii}-${li}" style="display:${isCustomCron(lib.cron)?'':'none'};">
          <label class="form-label">Custom cron expression</label>
          <input class="form-input" value="${isCustomCron(lib.cron)?h(lib.cron):''}" placeholder="0 3 * * *"
                 onchange="libraryCustomCronChanged(${ii},${li},this.value)" />
          <div class="form-hint">Advanced: minute, hour, day, month, weekday.</div>
        </div>
        <div>
          <div style="font-size:11px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;">Paths</div>
          <div id="si-paths-${ii}-${li}">
            ${renderPathItems(lib.paths, ii, li)}
          </div>
          <button class="btn btn-secondary btn-sm" style="margin-top:6px;" onclick="showAddPathForm(${ii},${li})">+ Add Path</button>
          <div class="add-path-form" id="apf-${ii}-${li}">
            <div class="form-row" style="margin-bottom:10px;">
              <div class="form-group" style="margin-bottom:0;">
                <label class="form-label">Path Type</label>
                <select class="form-input" id="apf-type-${ii}-${li}">
                  <option value="physical">physical</option>
                  <option value="debrid">debrid</option>
                  <option value="usenet">usenet</option>
                </select>
              </div>
              <div class="form-group" style="margin-bottom:0;">
                <label class="form-label">Threshold %</label>
                <input class="form-input" type="number" id="apf-thr-${ii}-${li}" value="90" min="1" max="100" />
                <div class="form-hint">Min % of Plex count that must be on disk</div>
              </div>
            </div>
            <div class="form-group">
              <label class="form-label">Path</label>
              <div style="display:flex;gap:8px;">
                <input class="form-input" id="apf-path-${ii}-${li}" placeholder="/mnt/symlink_media/symlinks/radarr" style="flex:1;" />
                <button class="btn btn-secondary btn-sm" onclick="openBrowser(${ii},${li})">browse</button>
              </div>
            </div>
            <div style="display:flex;gap:8px;">
              <button class="btn btn-success btn-sm" onclick="addPath(${ii},${li})">Add Path</button>
              <button class="btn btn-secondary btn-sm" onclick="document.getElementById('apf-${ii}-${li}').classList.remove('open')">Cancel</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  `).join('') || '<div style="color:var(--muted);font-size:12px;">No libraries — add one below.</div>';
}

function renderPathItems(paths, ii, li, source = '_settingsData') {
  if (!paths.length) return '<div style="color:var(--muted);font-size:11px;margin-bottom:6px;">No paths configured</div>';
  return paths.map((p, pi) => {
    const isDebrid = p.type === 'debrid' || p.type === 'usenet';
    const hasProviderCheck = p.provider_checks && p.provider_checks.length > 0;
    const providerType = hasProviderCheck ? p.provider_checks[0].type : '';
    return `
    <div class="path-item" style="flex-direction:column;align-items:stretch;gap:6px;">
      <div style="display:flex;align-items:center;gap:8px;">
        <span class="path-item-type">${h(p.type)}</span>
        <span class="path-item-path">${h(p.path)}</span>
        <div style="display:flex;align-items:center;gap:4px;flex-shrink:0;">
          <input type="number" min="1" max="100"
            value="${p.min_threshold ?? 90}"
            title="Threshold %"
            onchange="${source}.instances[${ii}].libraries[${li}].paths[${pi}].min_threshold=parseInt(this.value)"
            style="width:52px;background:var(--surface2);border:1px solid var(--border2);border-radius:4px;padding:2px 6px;font-family:var(--mono);font-size:11px;color:var(--text);text-align:center;outline:none;" />
          <span style="font-size:10px;color:var(--muted);">%</span>
        </div>
        <button class="btn btn-danger btn-xs" onclick="removePath(${ii},${li},${pi},'${source}')" style="padding:2px 7px;flex-shrink:0;">✕</button>
      </div>
      ${isDebrid ? `
      <div style="padding:6px 8px;background:var(--bg);border:1px solid var(--border);border-radius:5px;display:flex;align-items:center;gap:10px;">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:11px;color:var(--text2);">
          <input type="checkbox" ${hasProviderCheck ? 'checked' : ''}
            style="accent-color:var(--accent);"
            onchange="toggleProviderCheck(${ii},${li},${pi},this.checked,'${source}')" />
          Provider health check
        </label>
        ${hasProviderCheck ? `
        <select style="background:var(--surface2);border:1px solid var(--border2);border-radius:4px;padding:2px 6px;font-size:11px;color:var(--text);outline:none;"
                onchange="setProviderType(${ii},${li},${pi},this.value,'${source}')">
          <option value="realdebrid" ${providerType==='realdebrid'?'selected':''}>Real-Debrid</option>
          <option value="alldebrid"  ${providerType==='alldebrid'?'selected':''}>AllDebrid</option>
          <option value="torbox"     ${providerType==='torbox'?'selected':''}>Torbox</option>
          <option value="debridlink" ${providerType==='debridlink'?'selected':''}>Debrid-Link</option>
        </select>
        <span style="font-size:10px;color:var(--muted);">API key from env var</span>
        ` : ''}
      </div>` : ''}
    </div>`;
  }).join('');
}

function toggleLibSettings(id, button) {
  const el = document.getElementById(id);
  if (!el) return;
  const open = el.classList.toggle('open');
  if (button) button.setAttribute('aria-expanded', String(open));
}

function settingsAddInstance() {
  _settingsData.instances.push({ name:'', url:'', token:'', libraries:[], timestamp_repair:{enabled:false,worker:'local',database_path:'',allowed_prefixes:[],max_files_per_folder:5,scan_timeout_seconds:1800,poll_interval_seconds:5,heartbeat_seconds:30} });
  renderSettingsInstances();
  renderTrashRemovalSettings(_settingsData.instances.length - 1);
  // Open the last instance's first field
  const inputs = document.querySelectorAll('.inst-settings-card input');
  if (inputs.length) inputs[inputs.length - 3]?.focus();
}

function settingsRemoveInstance(ii) {
  if (!confirm(`Remove instance "${_settingsData.instances[ii].name}"?`)) return;
  _settingsData.instances.splice(ii, 1);
  renderSettingsInstances();
  renderTrashRemovalSettings(0);
}

function settingsAddLibrary(ii) {
  _settingsData.instances[ii].libraries.push({ name:'', type:'physical', cron:'', refresh_enabled:false, refresh_cron:'0 * * * *', refresh_guard_minutes:15, paths:[] });
  renderTrashRemovalSettings(ii);
  // Auto-open the new library
  const newLi = _settingsData.instances[ii].libraries.length - 1;
  const body = document.getElementById(`si-lb-${ii}-${newLi}`);
  if (body) body.classList.add('open');
}

function settingsRemoveLibrary(ii, li) {
  _settingsData.instances[ii].libraries.splice(li, 1);
  renderTrashRemovalSettings(ii);
}

function showAddPathForm(ii, li) {
  document.getElementById(`apf-${ii}-${li}`).classList.add('open');
}

function addPath(ii, li) {
  const path      = document.getElementById(`apf-path-${ii}-${li}`).value.trim();
  const type      = document.getElementById(`apf-type-${ii}-${li}`).value;
  const threshold = parseInt(document.getElementById(`apf-thr-${ii}-${li}`).value) || 90;
  if (!path) { toast('Enter a path', 'fail'); return; }
  const pathObj = { path, type, min_threshold: threshold };
  if (type === 'debrid' || type === 'usenet') pathObj.provider_checks = [];
  _settingsData.instances[ii].libraries[li].paths.push(pathObj);
  document.getElementById(`si-paths-${ii}-${li}`).innerHTML = renderPathItems(_settingsData.instances[ii].libraries[li].paths, ii, li);
  document.getElementById(`apf-${ii}-${li}`).classList.remove('open');
  document.getElementById(`apf-path-${ii}-${li}`).value = '';
}

function removePath(ii, li, pi, source = '_settingsData') {
  const data = source === '_wizData' ? _wizData : _settingsData;
  data.instances[ii].libraries[li].paths.splice(pi, 1);
  const containerId = source === '_wizData' ? `wp-${ii}-${li}` : `si-paths-${ii}-${li}`;
  document.getElementById(containerId).innerHTML =
    renderPathItems(data.instances[ii].libraries[li].paths, ii, li, source);
}

const NOTIFICATION_PRESETS = {
  telegram: { label: 'Telegram', placeholder: 'tgram://BOT_TOKEN/CHAT_ID', hint: 'Create a bot with BotFather, then supply its token and target chat ID.' },
  ntfy: { label: 'ntfy', placeholder: 'ntfy://HOST/TOPIC', hint: 'Works with ntfy.sh or a self-hosted ntfy server.' },
  gotify: { label: 'Gotify', placeholder: 'gotify://HOST/TOKEN', hint: 'Use the application token created in Gotify.' },
  email: { label: 'Email / SMTP', placeholder: 'mailtos://USER:PASSWORD@SMTP_HOST', hint: 'Use SMTP credentials; an app password is recommended where supported.' },
  pushover: { label: 'Pushover', placeholder: 'pover://USER_KEY@APP_TOKEN', hint: 'Requires a Pushover user key and application token.' },
  webhook: { label: 'Generic webhook', placeholder: 'json://HOST/PATH', hint: 'Use Apprise json://, form://, or xml:// webhook syntax.' },
  custom: { label: 'Other Apprise URL', placeholder: 'service://credentials/target', hint: 'Accepts any service URL supported by Apprise.' },
};
const NOTIFICATION_EVENTS = [
  ['emptied', 'Trash emptied'],
  ['health_fail', 'Health check failed'],
  ['error', 'Error'],
  ['clean', 'Already clean'],
  ['skip', 'Skipped'],
];

function renderNotificationDestinations() {
  const container = document.getElementById('notification-destinations');
  if (!container) return;
  if (!_settingsData.notification_destinations.length) {
    container.innerHTML = '<div class="form-hint">No Apprise destinations configured. Native Discord can still be used above.</div>';
    return;
  }
  container.innerHTML = _settingsData.notification_destinations.map((destination, index) => {
    const preset = NOTIFICATION_PRESETS[destination.service] || NOTIFICATION_PRESETS.custom;
    const options = Object.entries(NOTIFICATION_PRESETS).map(([value, item]) =>
      `<option value="${value}" ${destination.service===value?'selected':''}>${h(item.label)}</option>`
    ).join('');
    const events = NOTIFICATION_EVENTS.map(([value, label]) => `
      <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);cursor:pointer;">
        <input type="checkbox" ${destination.events.includes(value)?'checked':''}
               onchange="setNotificationEvent(${index},'${value}',this.checked)"
               style="accent-color:var(--accent);" /> ${h(label)}
      </label>`).join('');
    return `
      <div class="card" style="margin-bottom:12px;">
        <div class="card-header">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
            <input type="checkbox" ${destination.enabled?'checked':''}
                   onchange="_settingsData.notification_destinations[${index}].enabled=this.checked"
                   style="accent-color:var(--accent);" />
            <span class="card-title">${h(destination.name || `Destination ${index + 1}`)}</span>
          </label>
          <button class="btn btn-danger btn-sm" type="button" onclick="removeNotificationDestination(${index})">Remove</button>
        </div>
        <div class="card-body">
          <div class="form-row">
            <div class="form-group">
              <label class="form-label" for="notify-name-${index}">Name</label>
              <input class="form-input" id="notify-name-${index}" value="${h(destination.name)}"
                     onchange="_settingsData.notification_destinations[${index}].name=this.value.trim();renderNotificationDestinations()" />
            </div>
            <div class="form-group">
              <label class="form-label" for="notify-service-${index}">Preset</label>
              <select class="form-input" id="notify-service-${index}"
                      onchange="setNotificationService(${index},this.value)">${options}</select>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label" for="notify-url-${index}">Apprise URL</label>
            <input class="form-input" type="password" autocomplete="off" id="notify-url-${index}"
                   value="${h(destination.url)}" placeholder="${h(preset.placeholder)}"
                   onchange="_settingsData.notification_destinations[${index}].url=this.value.trim()" />
            <div class="form-hint">${h(preset.hint)} The secret URL is stored in config.yml and hidden in this field.</div>
          </div>
          <div class="form-group">
            <div class="form-label">Route events</div>
            <div style="display:flex;flex-wrap:wrap;gap:10px 18px;">${events}</div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;">
            <button class="btn btn-secondary btn-sm" type="button" onclick="testNotificationDestination(${index})">Send test</button>
            <span class="form-hint" id="notify-test-${index}"></span>
          </div>
        </div>
      </div>`;
  }).join('');
}

function addNotificationDestination() {
  _settingsData.notification_destinations.push({
    name: '', service: 'telegram', url: '', enabled: true,
    events: ['emptied','health_fail','error'],
  });
  renderNotificationDestinations();
}

function removeNotificationDestination(index) {
  _settingsData.notification_destinations.splice(index, 1);
  renderNotificationDestinations();
}

function setNotificationService(index, service) {
  _settingsData.notification_destinations[index].service = service;
  renderNotificationDestinations();
}

function setNotificationEvent(index, event, enabled) {
  const destination = _settingsData.notification_destinations[index];
  const events = new Set(destination.events);
  if (enabled) events.add(event); else events.delete(event);
  destination.events = [...events];
}

function syncNotificationDestination(index) {
  const destination = _settingsData.notification_destinations[index];
  destination.name = document.getElementById(`notify-name-${index}`)?.value.trim() || '';
  destination.service = document.getElementById(`notify-service-${index}`)?.value || 'custom';
  destination.url = document.getElementById(`notify-url-${index}`)?.value.trim() || '';
  return destination;
}

async function testNotificationDestination(index) {
  const result = document.getElementById(`notify-test-${index}`);
  const destination = syncNotificationDestination(index);
  result.textContent = 'Sending…';
  try {
    const response = await fetch('/api/notifications/test', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(destination),
    });
    const payload = await readJsonResponse(response);
    result.textContent = payload.ok ? '✓ Test sent' : `✕ ${payload.error || 'Delivery failed'}`;
    result.style.color = payload.ok ? 'var(--success)' : 'var(--danger)';
  } catch (error) {
    result.textContent = `✕ ${error.message}`;
    result.style.color = 'var(--danger)';
  }
}

// Each Settings section sends only the fields it owns. The server refuses to
// write anything outside that set, so a control that failed to render can no
// longer blank a value the user never touched.
const SETTINGS_SECTION_PAYLOAD = {
  'plex': () => ({ plex_instances: _settingsData.instances }),
  'trash-removal': () => ({
    plex_instances: _settingsData.instances,
    max_trash_items: parseInt(document.getElementById('s-max-trash-items')?.value ?? '1000'),
    max_trash_percent: parseFloat(document.getElementById('s-max-trash-percent')?.value ?? '25'),
    schedule: { default_cron: _settingsData.default_cron },
  }),
  'library-refresh': () => ({ plex_instances: _settingsData.instances }),
  'metadata-health': () => ({ plex_instances: _settingsData.instances }),
  'timestamp-repair': () => ({
    plex_instances: _settingsData.instances,
    timestamp_repair_workers: _settingsData.repair_workers,
  }),
  'mark-watched': () => ({
    mark_watched: {
      ..._settingsData.mark_watched,
      webhook_secret: document.getElementById('mark-watched-webhook-secret')?.value || '',
      give_up_after_hours: Math.max(0, parseFloat(document.getElementById('s-mw-give-up')?.value ?? '120') || 0),
      workers: Math.min(16, Math.max(1, parseInt(document.getElementById('s-mw-workers')?.value ?? '4') || 4)),
    },
  }),
  'features': () => ({ features: _settingsData.features }),
  'notifications': () => {
    _settingsData.notification_destinations.forEach((_, index) => syncNotificationDestination(index));
    return {
      discord_webhook: document.getElementById('s-discord').value.trim(),
      notify: {
        on_emptied: document.getElementById('s-notify-emptied').checked,
        on_health_fail: document.getElementById('s-notify-health-fail').checked,
        on_error: document.getElementById('s-notify-error').checked,
        on_clean: document.getElementById('s-notify-clean').checked,
        on_skip: document.getElementById('s-notify-skip').checked,
      },
      notifications: { destinations: _settingsData.notification_destinations },
    };
  },
  'general': () => ({
    log_level: document.getElementById('s-log-level')?.value || 'INFO',
    logging: {
      max_file_size_mb: parseInt(document.getElementById('s-log-file-size')?.value ?? '5'),
      max_total_size_mb: parseInt(document.getElementById('s-log-total-size')?.value ?? '50'),
      retention_days: parseInt(document.getElementById('s-log-retention-days')?.value ?? '14'),
    },
  }),
};

function settingsSaveControls(section) {
  return {
    button: document.getElementById(`settings-save-btn-${section}`),
    result: document.getElementById(`settings-save-result-${section}`),
  };
}

async function settingsSave(section = '') {
  if (!section) {
    section = document.querySelector('.settings-section.active')?.id.replace('ss-','') || 'plex';
  }
  const build = SETTINGS_SECTION_PAYLOAD[section];
  if (!build) return toast(`Nothing to save in ${section}`, 'fail');

  const { button, result } = settingsSaveControls(section);
  if (button) {
    button.dataset.saveLabel ||= button.innerHTML;
    button.disabled = true;
    button.innerHTML = `<span class="spin"></span> saving…`;
  }
  try {
    const response = await fetch(`/api/settings/${section}`, {
      method: 'PATCH',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(build()),
    });
    const data = await readJsonResponse(response, 'Save');
    if (!response.ok) throw new Error(data.error || 'Settings could not be saved');
    if (result) result.innerHTML = `<span style="color:var(--pass);">✓ Saved and applied</span>`;
    toast(data.message || 'Settings saved', 'pass');
    if (section === 'features') applyFeatureVisibility(_settingsData.features);
    await loadSettings();
    showSettingsSection(section, settingsNavButton(section));
  } catch (error) {
    if (result) result.innerHTML = `<span style="color:var(--fail2);">✗ ${h(error.message)}</span>`;
    toast(error.message || 'Save failed', 'fail');
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = button.dataset.saveLabel || 'Save Configuration';
    }
  }
}

// ── File browser ──────────────────────────────────────────────────────────────
let _browserCb = null;
async function openBrowser(ii, li) {
  const input = document.getElementById(`apf-path-${ii}-${li}`);
  _browserCb = (path) => { input.value = path; };
  await showBrowser(input.value.trim());
}
async function showBrowser(path) {
  try {
    const r = await fetch('/api/wizard/browse', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});
    const d = await readJsonResponse(r);
    if (!d.ok) { toast(d.error, 'fail'); return; }
    let modal = document.getElementById('_browser');
    if (!modal) { modal = document.createElement('div'); modal.id = '_browser'; modal.className = 'browser-modal'; document.body.appendChild(modal); }
    // Use h() for all server-supplied values; use data-* for paths to avoid onclick injection
    const entriesHtml = d.entries.length
      ? d.entries.map(e => `
          <div class="browser-entry" data-path="${h(e.path)}">
            <span class="browser-entry-icon">📁</span>
            <span class="browser-entry-name">${h(e.name)}</span>
            ${e.is_link ? '<span class="browser-entry-link">symlink</span>' : ''}
          </div>`).join('')
      : `<div style="padding:16px;color:var(--muted);font-size:12px;">${h(d.empty_message || 'Empty directory')}</div>`;
    const displayPath = d.path || 'Allowed browse roots';
    const selectButton = d.selectable === false
      ? ''
      : '<button class="btn btn-success btn-sm" id="_browser-select">Use this path</button>';
    modal.innerHTML = `
      <div class="browser-inner">
        <div class="browser-header">
          <span style="font-size:13px;font-weight:600;color:var(--bright);">Browse Filesystem</span>
          <button class="btn btn-secondary btn-sm" id="_browser-close">✕</button>
        </div>
        <div class="browser-path-bar">
          ${d.parent !== null ? `<span style="cursor:pointer;color:var(--info2);" id="_browser-up">← up</span>` : ''}
          <span>${h(displayPath)}</span>
        </div>
        <div class="browser-list" id="_browser-list">${entriesHtml}</div>
        <div class="browser-footer">
          <span class="browser-current">${h(displayPath)}</span>
          ${selectButton}
        </div>
      </div>`;
    // Wire up handlers after render — avoids onclick string interpolation with path values
    modal.querySelector('#_browser-close').onclick = () => modal.remove();
    const select = modal.querySelector('#_browser-select');
    if (select) select.onclick = () => selectPath(d.path);
    if (d.parent !== null) modal.querySelector('#_browser-up').onclick = () => showBrowser(d.parent);
    modal.querySelectorAll('.browser-entry[data-path]').forEach(el => {
      el.onclick = () => showBrowser(el.dataset.path);
    });
  } catch(e) { toast('Browse failed', 'fail'); }
}
function selectPath(path) {
  if (_browserCb) _browserCb(path);
  const modal = document.getElementById('_browser');
  if (modal) modal.remove();
}

// ── Actions ───────────────────────────────────────────────────────────────────
async function runAll() {
  const btn = document.getElementById('btn-all');
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span>`;
  toast('Running all libraries…');
  const prev = [..._history];
  try {
    await fetch('/api/run/all', {method:'POST'});
    pollUntilUpdate(prev);
  } catch(e) { toast('Error', 'fail'); }
  finally { btn.disabled = false; btn.innerHTML = '▶ run all'; }
}
async function dryRunAll() {
  const btn = document.getElementById('btn-dryrun');
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span>`;
  toast('Dry run — all libraries…');
  const prev = [..._history];
  try {
    await fetch('/api/dryrun/all', {method:'POST'});
    pollUntilUpdate(prev);
  } catch(e) { toast('Error', 'fail'); }
  finally { btn.disabled = false; btn.innerHTML = '◎ dry run'; }
}
async function runLib(inst, lib) {
  const id = cid(inst, lib);
  const btn = document.getElementById(`btn-r-${id}`);
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spin"></span>`; }
  toast(`Running ${lib}…`);
  const prev = [..._history];
  try {
    await fetch(`/api/run/${encodeURIComponent(inst)}/${encodeURIComponent(lib)}`, {method:'POST'});
    pollUntilUpdate(prev);
  } catch(e) { toast('Error', 'fail'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '▶'; } }
}
async function dryRunLib(inst, lib) {
  const id = cid(inst, lib);
  const btn = document.getElementById(`btn-d-${id}`);
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spin"></span>`; }
  toast(`Dry run — ${lib}…`);
  const prev = [..._history];
  try {
    await fetch(`/api/dryrun/${encodeURIComponent(inst)}/${encodeURIComponent(lib)}`, {method:'POST'});
    pollUntilUpdate(prev);
  } catch(e) { toast('Error', 'fail'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '◎'; } }
}

// ── Wizard (first-run only) ───────────────────────────────────────────────────
let _wizStep = 1, _wizData = { instances:[], default_cron:'0 * * * *', discord_webhook:'', notify_emptied:true, notify_health_fail:true, notify_error:true, notify_clean:false, notify_skip:false };
function openWizard() { document.getElementById('wizard-overlay').classList.remove('hidden'); _wizStep=1; renderWizStep(); }
function closeWizard() { document.getElementById('wizard-overlay').classList.add('hidden'); }
function setWizStep(n) {
  _wizStep = n;
  [1,2,3].forEach(i => {
    const el = document.getElementById(`ws-${i}`);
    el.className = i < n ? 'step-dot done' : i === n ? 'step-dot active' : 'step-dot';
  });
}
function renderWizStep() {
  setWizStep(_wizStep);
  const body = document.getElementById('wiz-body');
  const back = document.getElementById('wiz-back');
  const next = document.getElementById('wiz-next');
  back.style.visibility = _wizStep === 1 ? 'hidden' : 'visible';
  if (_wizStep === 1) renderWiz1(body, next);
  else if (_wizStep === 2) renderWiz2(body, next);
  else if (_wizStep === 3) renderWiz3(body, next);
}
function wizBack() { if (_wizStep > 1) { _wizStep--; renderWizStep(); } }

function renderWiz1(body, next) {
  document.getElementById('wiz-title').textContent = 'Step 1 — Plex Instances';
  next.textContent = 'next →';
  next.onclick = () => { if (_wizData.instances.length) { _wizStep=2; renderWizStep(); } else toast('Add at least one instance', 'fail'); };
  body.innerHTML = `
    <p style="color:var(--muted);font-size:12px;margin-bottom:12px;">Connect your Plex account to discover servers and libraries automatically, or add one manually.</p>
    <div style="display:flex;gap:8px;margin-bottom:12px;">
      <button class="btn btn-primary" onclick="connectPlexAccount('wizard')">Connect Plex Account</button>
      <button class="btn btn-secondary" onclick="wizAddInst()">Add Manually</button>
    </div>
    <div id="plex-discovery-wizard" style="margin-bottom:14px;"></div>
    <div id="wiz-inst-list"></div>
    <div id="wiz-inst-form" style="display:none;margin-top:14px;background:var(--bg);border:1px solid var(--border2);border-radius:8px;padding:16px;">
      <div class="form-row">
        <div class="form-group"><label class="form-label">Name</label><input class="form-input" id="wi-name" placeholder="My Plex" /></div>
        <div class="form-group"><label class="form-label">URL</label><input class="form-input" id="wi-url" placeholder="http://192.168.1.100:32400" /></div>
      </div>
      <div class="form-group">
        <label class="form-label">Token (for testing — env var recommended)</label>
        <div style="display:flex;gap:8px;">
          <input class="form-input" id="wi-token" type="password" placeholder="Your Plex token" style="flex:1;" />
          <button class="btn btn-secondary" onclick="wizTestPlex()">Test</button>
        </div>
        <div class="form-hint" id="wi-hint">Token used to verify connection and fetch library names.</div>
      </div>
      <div id="wi-libs" style="display:none;">
        <hr class="divider"/>
        <div class="form-label" style="margin-bottom:8px;">Select Libraries</div>
        <div id="wi-lib-checks" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;"></div>
      </div>
      <div style="display:flex;gap:8px;margin-top:8px;">
        <button class="btn btn-success" id="wi-save" onclick="wizSaveInst()" style="display:none;">Save Instance</button>
        <button class="btn btn-secondary btn-sm" onclick="document.getElementById('wiz-inst-form').style.display='none'">Cancel</button>
      </div>
    </div>`;
  renderWizInstList();
}
function renderWizInstList() {
  const list = document.getElementById('wiz-inst-list');
  if (!list) return;
  list.innerHTML = _wizData.instances.map((inst, i) => `
    <div style="background:var(--bg);border:1px solid var(--border2);border-radius:7px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
      <div>
        <div style="font-weight:600;color:var(--bright);">${h(inst.name)}</div>
        <div style="font-family:var(--mono);font-size:10px;color:var(--muted);">${h(inst.url)} — ${inst.libraries.length} librar${inst.libraries.length===1?'y':'ies'}</div>
      </div>
      <button class="btn btn-danger btn-sm" onclick="_wizData.instances.splice(${i},1);renderWizInstList()">Remove</button>
    </div>`).join('');
}

let _plexDiscovered = [];
const _plexAuthAttempts = new Map();

async function cancelPlexAccountConnect(target, showMessage = true) {
  const attempt = _plexAuthAttempts.get(target);
  if (!attempt) return;
  attempt.cancelled = true;
  if (attempt.popup && !attempt.popup.closed) attempt.popup.close();
  if (attempt.state) {
    try {
      await fetch(`/api/plex/auth/cancel/${encodeURIComponent(attempt.state)}`, {method:'POST'});
    } catch (_) {
      // The local session expires automatically; cancellation is best effort.
    }
  }
  if (_plexAuthAttempts.get(target) === attempt) {
    _plexAuthAttempts.delete(target);
    if (showMessage) {
      const box = document.getElementById(`plex-discovery-${target}`);
      if (box) box.innerHTML = '<span style="color:var(--muted);">Plex sign-in cancelled.</span>';
    }
  }
}

async function connectPlexAccount(target) {
  const box = document.getElementById(`plex-discovery-${target}`);
  if (!box) return;
  // Keep window.open in the original click event so popup blockers allow it.
  cancelPlexAccountConnect(target, false);
  const popup = window.open(
    'about:blank',
    `mediamender-plex-auth-${target}`,
    'popup=yes,width=720,height=760,resizable=yes,scrollbars=yes'
  );
  if (popup) popup.opener = null;
  const attempt = {popup, state:null, cancelled:false};
  _plexAuthAttempts.set(target, attempt);
  box.innerHTML = '<span class="spin"></span> Starting Plex sign-in…';
  try {
    const startResponse = await fetch('/api/plex/auth/start', {method:'POST'});
    const started = await startResponse.json();
    if (!started.ok) throw new Error(started.error || 'Could not start Plex sign-in');
    attempt.state = started.state;
    if (attempt.cancelled || _plexAuthAttempts.get(target) !== attempt) {
      await fetch(`/api/plex/auth/cancel/${encodeURIComponent(started.state)}`, {method:'POST'});
      return;
    }
    if (popup && !popup.closed) popup.location.replace(started.auth_url);
    box.innerHTML = `<span class="spin"></span> Approve ${PRODUCT_NAME} in the Plex window. Waiting for authorization…
      <a href="${h(started.auth_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--info2);margin-left:8px;">Open Plex sign-in</a>
      <button class="btn btn-secondary btn-sm" style="margin-left:8px;" onclick="cancelPlexAccountConnect('${h(target)}')">Cancel</button>`;
    const deadline = Date.now() + (started.expires_in * 1000);
    let pollDelay = 3000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, pollDelay));
      if (attempt.cancelled || _plexAuthAttempts.get(target) !== attempt) return;
      // Poll once after a close in case Plex closed the window after approval.
      const popupWasClosed = Boolean(popup && popup.closed);
      const response = await fetch(`/api/plex/auth/status/${encodeURIComponent(started.state)}`);
      const result = await readJsonResponse(response);
      if (!result.ok) throw new Error(result.error || 'Plex authorization failed');
      if (result.pending) {
        if (popupWasClosed) {
          await cancelPlexAccountConnect(target);
          return;
        }
        pollDelay = Math.max(3000, Math.min(Number(result.retry_after || 3) * 1000, 60000));
        continue;
      }
      _plexDiscovered = result.servers || [];
      if (popup && !popup.closed) popup.close();
      _plexAuthAttempts.delete(target);
      renderDiscoveredPlex(target);
      return;
    }
    throw new Error('Plex authorization expired');
  } catch (error) {
    if (popup && !popup.closed) popup.close();
    if (attempt.state) {
      try {
        await fetch(`/api/plex/auth/cancel/${encodeURIComponent(attempt.state)}`, {method:'POST'});
      } catch (_) {}
    }
    if (_plexAuthAttempts.get(target) !== attempt) return;
    _plexAuthAttempts.delete(target);
    box.innerHTML = `<span style="color:var(--fail2);">✗ ${h(error.message)}</span>`;
  }
}

function renderDiscoveredPlex(target) {
  const box = document.getElementById(`plex-discovery-${target}`);
  if (!box) return;
  if (!_plexDiscovered.length) {
    box.innerHTML = '<span style="color:var(--warn2);">No Plex Media Servers were found on that account.</span>';
    return;
  }
  box.innerHTML = `<div class="card"><div class="card-body">
    <div class="form-label">Discovered Plex Servers</div>
    ${_plexDiscovered.map((server, index) => `
      <label style="display:flex;align-items:flex-start;gap:8px;padding:8px 0;border-bottom:1px solid var(--border);">
        <input type="checkbox" data-plex-server="${index}" ${server.error ? 'disabled' : 'checked'} style="margin-top:3px;accent-color:var(--accent);" />
        <span>
          <strong style="color:var(--bright);">${h(server.name)}</strong>
          <span style="color:var(--muted);font-size:10px;"> — ${h(server.url || 'no reachable URL')}</span>
          <div style="font-size:11px;color:${server.error ? 'var(--fail2)' : 'var(--text2)'};">
            ${server.error ? h(server.error) : `${server.libraries.length} libraries: ${server.libraries.map(l => h(l.title)).join(', ')}`}
          </div>
        </span>
      </label>`).join('')}
    <button class="btn btn-success btn-sm" style="margin-top:10px;" onclick="importDiscoveredPlex('${target}')">Import Selected</button>
  </div></div>`;
}

function uniqueInstanceName(base, existing) {
  let name = base || 'Plex';
  let suffix = 2;
  while (existing.some(instance => instance.name === name)) name = `${base} (${suffix++})`;
  return name;
}

function samePlexUrl(left, right) {
  const normalize = value => String(value || '').trim().replace(/\/+$/,'').toLowerCase();
  return normalize(left) && normalize(left) === normalize(right);
}

function samePlexLibrary(left, right) {
  if (left.section_id != null && right.id != null) {
    return String(left.section_id) === String(right.id);
  }
  return String(left.name || '').trim().toLowerCase() ===
         String(right.title || '').trim().toLowerCase();
}

function mergeConfiguredInstance(target, duplicate) {
  target.libraries = target.libraries || [];
  (duplicate.libraries || []).forEach(duplicateLibrary => {
    let library = (target.libraries || []).find(existing => {
      if (existing.section_id != null && duplicateLibrary.section_id != null) {
        return String(existing.section_id) === String(duplicateLibrary.section_id);
      }
      return String(existing.name || '').trim().toLowerCase() ===
             String(duplicateLibrary.name || '').trim().toLowerCase();
    });
    if (!library) {
      target.libraries.push(duplicateLibrary);
      return;
    }
    library.paths = library.paths || [];
    (duplicateLibrary.paths || []).forEach(path => {
      const exists = library.paths.some(existing =>
        existing.path === path.path && existing.type === path.type
      );
      if (!exists) library.paths.push(path);
    });
  });
}

function importDiscoveredPlex(target) {
  const destination = target === 'wizard' ? _wizData.instances : _settingsData.instances;
  const selected = [...document.querySelectorAll(`#plex-discovery-${target} [data-plex-server]:checked`)]
    .map(input => _plexDiscovered[Number(input.dataset.plexServer)]);
  let addedServers = 0, addedLibraries = 0, refreshedServers = 0;
  selected.forEach(server => {
    const matches = destination.filter(candidate =>
      (candidate.machine_id && candidate.machine_id === server.id) ||
      (!candidate.machine_id && samePlexUrl(candidate.url, server.url))
    );
    let instance = matches[0];
    matches.slice(1).forEach(duplicate => {
      mergeConfiguredInstance(instance, duplicate);
      const duplicateIndex = destination.indexOf(duplicate);
      if (duplicateIndex >= 0) destination.splice(duplicateIndex, 1);
    });
    if (!instance) {
      instance = {
        name: uniqueInstanceName(server.name, destination),
        machine_id: server.id,
        url: server.url,
        token: server.token,
        libraries: [],
      };
      destination.push(instance);
      addedServers++;
    } else {
      instance.machine_id = server.id;
      instance.url = server.url;
      instance.token = server.token;
      refreshedServers++;
    }
    server.libraries.forEach(library => {
      if (instance.libraries.some(existing => samePlexLibrary(existing, library))) return;
      instance.libraries.push({
        name: library.title,
        section_id: library.id,
        type: 'physical',
        cron: '',
        refresh_enabled: false,
        refresh_cron: '0 * * * *',
        refresh_guard_minutes: 15,
        paths: [],
      });
      addedLibraries++;
    });
  });
  if (target === 'wizard') renderWizInstList();
  else { renderSettingsInstances(); renderTrashRemovalSettings(); }
  _plexDiscovered = [];
  document.getElementById(`plex-discovery-${target}`).innerHTML = '';
  const summary = addedServers
    ? `Added ${addedServers} server${addedServers === 1 ? '' : 's'} and ${addedLibraries} libraries`
    : refreshedServers
      ? `Refreshed ${refreshedServers} server${refreshedServers === 1 ? '' : 's'}; ${addedLibraries} new libraries`
      : 'Nothing new to import';
  toast(summary, addedServers || addedLibraries ? 'pass' : 'warn');
}
function wizAddInst() { document.getElementById('wi-name').value=''; document.getElementById('wi-url').value=''; document.getElementById('wi-token').value=''; document.getElementById('wi-hint').textContent='Token used to verify connection and fetch library names.'; document.getElementById('wi-hint').className='form-hint'; document.getElementById('wi-libs').style.display='none'; document.getElementById('wi-save').style.display='none'; document.getElementById('wiz-inst-form').style.display='block'; }
async function wizTestPlex() {
  const url=document.getElementById('wi-url').value.trim(), token=document.getElementById('wi-token').value.trim(), hint=document.getElementById('wi-hint');
  if (!url||!token) { hint.textContent='Enter URL and token first.'; hint.className='form-hint error'; return; }
  hint.innerHTML=`<span class="spin"></span> testing…`; hint.className='form-hint';
  try {
    const r=await fetch('/api/wizard/test-plex',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,token})});
    const d=await readJsonResponse(r);
    if (d.ok) {
      hint.textContent=`✓ ${d.detail}`; hint.className='form-hint success';
      document.getElementById('wi-lib-checks').innerHTML=d.libraries.map(lib=>`
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:var(--text);">
          <input type="checkbox" value="${h(lib.title)}" data-section-id="${h(lib.id)}" checked style="accent-color:var(--accent);" /> ${h(lib.title)}
          <span style="color:var(--muted);font-size:10px;">(${h(lib.type)})</span>
        </label>`).join('');
      document.getElementById('wi-libs').style.display='block';
      document.getElementById('wi-save').style.display='inline-flex';
    } else { hint.textContent=`✗ ${d.error}`; hint.className='form-hint error'; }
  } catch(e) { hint.textContent='Connection failed'; hint.className='form-hint error'; }
}
function wizSaveInst() {
  const name=document.getElementById('wi-name').value.trim(), url=document.getElementById('wi-url').value.trim(), token=document.getElementById('wi-token').value.trim();
  if (!name||!url) { toast('Name and URL required','fail'); return; }
  const selected=[...document.querySelectorAll('#wi-lib-checks input:checked')].map(cb=>({
    name:cb.value, section_id:cb.dataset.sectionId, type:'physical', cron:'', paths:[]
  }));
  _wizData.instances.push({ name, url, token, libraries: selected });
  document.getElementById('wiz-inst-form').style.display='none';
  renderWizInstList();
  toast(`✓ ${name} added`,'pass');
}

function renderWiz2(body, next) {
  document.getElementById('wiz-title').textContent='Step 2 — Library Paths';
  next.textContent='next →'; next.onclick=()=>{_wizStep=3;renderWizStep();};
  body.innerHTML=`<p style="color:var(--muted);font-size:12px;margin-bottom:16px;">Configure filesystem paths for each library.</p><div id="wiz-paths-content"></div>`;
  let html='';
  _wizData.instances.forEach((inst,ii)=>{
    inst.libraries.forEach((lib,li)=>{
      html+=`<div style="background:var(--bg);border:1px solid var(--border2);border-radius:8px;padding:14px;margin-bottom:12px;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">
          <span style="font-weight:600;color:var(--bright);">${h(lib.name)}</span>
          <span style="color:var(--muted);font-size:11px;">— ${h(inst.name)}</span>
        </div>
        <div class="form-row" style="margin-bottom:10px;">
          <div class="form-group" style="margin-bottom:0;">
            <label class="form-label">Type</label>
            <select class="form-input" onchange="_wizData.instances[${ii}].libraries[${li}].type=this.value">
              <option value="physical" ${lib.type==='physical'?'selected':''}>physical</option>
              <option value="debrid" ${lib.type==='debrid'?'selected':''}>debrid</option>
              <option value="usenet" ${lib.type==='usenet'?'selected':''}>usenet</option>
              <option value="mixed" ${lib.type==='mixed'?'selected':''}>mixed</option>
            </select>
          </div>
          <div class="form-group" style="margin-bottom:0;">
            <label class="form-label">Schedule</label>
            <select class="form-input" onchange="_wizData.instances[${ii}].libraries[${li}].cron=this.value">
              <option value="" ${!lib.cron?'selected':''}>Use global — Every hour</option>
              <option value="*/30 * * * *" ${lib.cron==='*/30 * * * *'?'selected':''}>Every 30 min</option>
              <option value="0 * * * *" ${lib.cron==='0 * * * *'?'selected':''}>Every hour</option>
              <option value="0 */2 * * *" ${lib.cron==='0 */2 * * *'?'selected':''}>Every 2 hours</option>
              <option value="0 2 * * *" ${lib.cron==='0 2 * * *'?'selected':''}>Daily at 2am</option>
            </select>
          </div>
        </div>
        <div id="wp-${ii}-${li}">${renderPathItems(lib.paths,ii,li,'_wizData')}</div>
        <button class="btn btn-secondary btn-sm" style="margin-top:6px;" onclick="showWizPathForm(${ii},${li})">+ Add Path</button>
        <div class="add-path-form" id="wpf-${ii}-${li}">
          <div class="form-row" style="margin-bottom:10px;">
            <div class="form-group" style="margin-bottom:0;"><label class="form-label">Type</label>
              <select class="form-input" id="wpt-${ii}-${li}"><option value="physical">physical</option><option value="debrid">debrid</option><option value="usenet">usenet</option></select></div>
            <div class="form-group" style="margin-bottom:0;"><label class="form-label">Threshold %</label>
              <input class="form-input" type="number" id="wpm-${ii}-${li}" value="90" min="1" max="100" /></div>
          </div>
          <div class="form-group">
            <label class="form-label">Path</label>
            <div style="display:flex;gap:8px;">
              <input class="form-input" id="wpp-${ii}-${li}" placeholder="/mnt/..." style="flex:1;" />
              <button class="btn btn-secondary btn-sm" onclick="openWizBrowser(${ii},${li})">browse</button>
            </div>
          </div>
          <div style="display:flex;gap:8px;">
            <button class="btn btn-success btn-sm" onclick="addWizPath(${ii},${li})">Add</button>
            <button class="btn btn-secondary btn-sm" onclick="document.getElementById('wpf-${ii}-${li}').classList.remove('open')">Cancel</button>
          </div>
        </div>
      </div>`;
    });
  });
  document.getElementById('wiz-paths-content').innerHTML=html||'<div style="color:var(--muted);">No libraries. Go back to Step 1.</div>';
}
function showWizPathForm(ii,li){document.getElementById(`wpf-${ii}-${li}`).classList.add('open');}
function openWizBrowser(ii,li){
  const input=document.getElementById(`wpp-${ii}-${li}`);
  _browserCb=(p)=>{input.value=p;};
  showBrowser(input.value.trim());
}
function addWizPath(ii,li){
  const path=document.getElementById(`wpp-${ii}-${li}`).value.trim();
  const type=document.getElementById(`wpt-${ii}-${li}`).value;
  const thr=parseInt(document.getElementById(`wpm-${ii}-${li}`).value)||90;
  if(!path){toast('Enter a path','fail');return;}
  const p={path,type,min_threshold:thr};
  if(type==='debrid'||type==='usenet') p.provider_checks=[];
  _wizData.instances[ii].libraries[li].paths.push(p);
  document.getElementById(`wp-${ii}-${li}`).innerHTML=renderPathItems(_wizData.instances[ii].libraries[li].paths,ii,li,'_wizData');
  document.getElementById(`wpf-${ii}-${li}`).classList.remove('open');
}

function renderWiz3(body, next) {
  document.getElementById('wiz-title').textContent='Step 3 — Tokens & Save';
  next.textContent='💾 Save Config'; next.onclick=wizSave3;
  const envRows=_wizData.instances.map((inst,i)=>{
    const safe=inst.name.toUpperCase().replace(/ /g,'_').replace(/-/g,'_');
    return `<div style="margin-bottom:12px;">
      <div style="font-size:11px;color:var(--muted);margin-bottom:4px;">${h(inst.name)}</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <code style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:5px 10px;font-family:var(--mono);font-size:12px;color:var(--accent2);">PLEX_TOKEN_${safe}</code>
        <span style="color:var(--muted);font-size:11px;">=</span>
        <input type="password" id="wt-${i}" value="${h(inst.token||'')}" placeholder="your Plex token"
          style="flex:1;min-width:180px;background:var(--bg);border:1px solid var(--border2);border-radius:5px;padding:5px 10px;font-family:var(--mono);font-size:11px;color:var(--text);outline:none;" />
      </div>
    </div>`;
  }).join('');
  body.innerHTML=`
    <div style="margin-bottom:20px;">
      <div class="form-label" style="margin-bottom:10px;">Token Storage</div>
      <div class="choice-card"><input type="radio" name="wts" value="envvar" checked onchange="document.getElementById('wt-guide').style.display='block'" />
        <div><div class="choice-title">Environment variables <span style="font-family:var(--mono);font-size:9px;background:rgba(16,185,129,.1);color:var(--pass);padding:1px 6px;border-radius:3px;margin-left:4px;">recommended</span></div>
        <div class="choice-desc">Set in Docker UI — more secure, not written to disk.</div></div></div>
      <div class="choice-card"><input type="radio" name="wts" value="config" onchange="document.getElementById('wt-guide').style.display='none'" />
        <div><div class="choice-title">Store in config.yml</div>
        <div class="choice-desc">Written directly to config. Simpler but less secure.</div></div></div>
    </div>
    <div id="wt-guide" style="background:var(--bg);border:1px solid var(--border2);border-radius:7px;padding:14px;margin-bottom:16px;">
      <div class="form-label" style="margin-bottom:10px;">Set these env vars in Docker UI:</div>
      ${envRows}
    </div>
    <hr class="divider"/>
    <div class="form-group"><label class="form-label">Discord Webhook (optional)</label>
      <input class="form-input" id="wd-discord" value="${h(_wizData.discord_webhook||'')}" placeholder="https://discord.com/api/webhooks/…" /></div>
    <div id="wiz-save-result" style="margin-top:12px;"></div>`;
}
async function wizSave3() {
  _wizData.discord_webhook=document.getElementById('wd-discord').value.trim();
  _wizData.instances.forEach((inst,i)=>{const f=document.getElementById(`wt-${i}`);if(f) inst.token=f.value.trim();});
  const storeTokens=document.querySelector('input[name="wts"]:checked')?.value==='config';
  const btn=document.getElementById('wiz-next');
  btn.disabled=true; btn.innerHTML=`<span class="spin"></span> saving…`;
  try {
    const r=await fetch('/api/wizard/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({..._wizData,store_tokens:storeTokens})});
    const d=await readJsonResponse(r);
    const result=document.getElementById('wiz-save-result');
    if(d.ok){
      let envSection='';
      if(!storeTokens&&d.env_vars_needed?.length){
        envSection=`<div style="margin-top:10px;padding-top:10px;border-top:1px solid rgba(16,185,129,.2);">
          <div style="font-size:11px;color:var(--pass);font-weight:600;margin-bottom:6px;">Set in Docker UI:</div>
          ${d.env_vars_needed.map(ev=>`<div style="display:flex;gap:8px;margin-bottom:4px;align-items:center;">
            <code style="background:var(--bg);padding:2px 8px;border-radius:3px;font-family:var(--mono);font-size:11px;color:var(--accent2);">${h(ev.name)}</code>
            <span style="font-size:11px;color:var(--muted);">${h(ev.description)}</span></div>`).join('')}
        </div>`;
      }
      result.innerHTML=`<div style="background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.25);border-radius:7px;padding:14px;color:var(--pass);font-size:12px;line-height:1.7;">
        ✓ Config saved! ${envSection}
        <div style="margin-top:10px;">Settings are active now. Reloading the dashboard…</div>
      </div>`;
      btn.innerHTML='✓ saved';
      toast('Config saved','pass');
      setTimeout(() => location.reload(), 700);
    } else {
      result.innerHTML=`<div style="background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.25);border-radius:7px;padding:12px;color:var(--fail2);font-size:12px;">✗ ${h(d.error)}</div>`;
      btn.disabled=false; btn.innerHTML='💾 Save Config';
    }
  } catch(e) { toast('Save failed','fail'); btn.disabled=false; btn.innerHTML='💾 Save Config'; }
}
// ── Auth settings ─────────────────────────────────────────────────────────────
async function loadApiToken() {
  try {
    const r = await fetch('/api/auth/token');
    const d = await readJsonResponse(r);
    const status = document.getElementById('api-token-status');
    if (!status) return;
    if (!d.ok) status.textContent = d.error || 'Unavailable';
    else if (d.configured) status.textContent = d.source === 'environment'
      ? 'Configured by environment'
      : 'Token configured';
    else status.textContent = 'No token configured';
  } catch(e) {}
}
async function copyApiToken() {
  const el = document.getElementById('api-token-display');
  const result = document.getElementById('api-token-result');
  if (!el || !el.value) { if(result) result.innerHTML='<span style="color:var(--muted);">No token available</span>'; return; }
  try {
    await navigator.clipboard.writeText(el.value);
    if (result) result.innerHTML = '<span style="color:var(--pass);">✓ Copied to clipboard</span>';
    setTimeout(() => { if(result) result.innerHTML = ''; }, 2000);
  } catch(e) {
    if (result) result.innerHTML = '<span style="color:var(--muted);">Copy failed — select and copy manually</span>';
  }
}

async function generateApiToken() {
  if (!confirm('Generate a new API token? Any existing token will stop working immediately.')) return;
  const result = document.getElementById('api-token-result');
  const display = document.getElementById('api-token-display');
  try {
    const r = await fetch('/api/auth/token', {method:'POST'});
    const d = await readJsonResponse(r);
    if (!d.ok) {
      result.innerHTML=`<span style="color:var(--fail2);">âœ— ${h(d.error)}</span>`;
      return;
    }
    display.value = d.token;
    result.innerHTML='<span style="color:var(--warn2);">Copy this token now. It cannot be displayed again.</span>';
    await loadApiToken();
  } catch(e) {
    result.innerHTML='<span style="color:var(--fail2);">Token generation failed</span>';
  }
}

async function revokeApiToken() {
  if (!confirm('Revoke the current API token? Automations using it will stop working.')) return;
  const result = document.getElementById('api-token-result');
  try {
    const r = await fetch('/api/auth/token', {method:'DELETE'});
    const d = await readJsonResponse(r);
    if (!d.ok) {
      result.innerHTML=`<span style="color:var(--fail2);">âœ— ${h(d.error)}</span>`;
      return;
    }
    document.getElementById('api-token-display').value='';
    result.innerHTML='<span style="color:var(--pass);">âœ“ API token revoked</span>';
    await loadApiToken();
  } catch(e) {
    result.innerHTML='<span style="color:var(--fail2);">Token revocation failed</span>';
  }
}

async function saveAuth() {
  const user  = document.getElementById('s-auth-user').value.trim();
  const pass  = document.getElementById('s-auth-pass').value;
  const pass2 = document.getElementById('s-auth-pass2').value;
  const result = document.getElementById('auth-save-result');
  if (!user)          { result.innerHTML='<span style="color:var(--fail2);">Username required</span>'; return; }
  if (!pass)          { result.innerHTML='<span style="color:var(--fail2);">Password required</span>'; return; }
  if (pass !== pass2) { result.innerHTML='<span style="color:var(--fail2);">Passwords do not match</span>'; return; }
  try {
    const r = await fetch('/api/auth/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:user,password:pass})});
    const d = await readJsonResponse(r);
    if (d.ok) {
      result.innerHTML=`<span style="color:var(--pass);">✓ ${h(d.message)}</span>`;
      document.getElementById('s-auth-pass').value='';
      document.getElementById('s-auth-pass2').value='';
      updateAuthBanner(true, user);
      loadApiToken();
      toast('Auth credentials saved','pass');
    } else { result.innerHTML=`<span style="color:var(--fail2);">✗ ${h(d.error)}</span>`; }
  } catch(e) { result.innerHTML='<span style="color:var(--fail2);">Save failed</span>'; }
}

async function clearAuth() {
  if (!confirm('Remove authentication? Anyone on the network will be able to access the UI.')) return;
  const result = document.getElementById('auth-save-result');
  try {
    const r = await fetch('/api/auth/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({clear:true})});
    const d = await readJsonResponse(r);
    if (d.ok) {
      result.innerHTML=`<span style="color:var(--warn2);">✓ ${h(d.message)}</span>`;
      document.getElementById('s-auth-user').value='';
      updateAuthBanner(false,'');
      toast('Auth removed','warn');
    } else { result.innerHTML=`<span style="color:var(--fail2);">✗ ${h(d.error)}</span>`; }
  } catch(e) { result.innerHTML='<span style="color:var(--fail2);">Failed</span>'; }
}

function updateAuthBanner(enabled, username) {
  const el = document.getElementById('auth-status-banner');
  if (!el) return;
  if (enabled) {
    el.innerHTML=`<div style="background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.2);border-radius:7px;padding:10px 14px;font-size:12px;color:var(--pass);">🔒 Auth enabled — signed in as <strong>${h(username)}</strong></div>`;
  } else {
    el.innerHTML=`<div style="background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.2);border-radius:7px;padding:10px 14px;font-size:12px;color:var(--warn2);">⚠️ Auth disabled — UI is open to anyone on the network</div>`;
  }
}

function initAuthBanner() {
  updateAuthBanner(BOOT.authEnabled, BOOT.authUsername);
}

// ── Fetch ─────────────────────────────────────────────────────────────────────
async function fetchStatus() {
  try {
    const r = await fetch('/api/status'); const d = await readJsonResponse(r);
    if (d.global_checks) applyChecks(d.global_checks);
    applyStatus(d, d.next_runs || {});
    applyStartupProgress(d.startup_checks);
    if (d.scheduling_enabled !== undefined) applyScheduling(d.scheduling_enabled);
    if (d.startup_checks?.running && !_startupPollPending) {
      _startupPollPending = true;
      setTimeout(async () => { _startupPollPending = false; await fetchStatus(); }, 1000);
    }
  } catch(e) {}
}
async function fetchHistory() {
  try { const r = await fetch('/api/history'); _history = await readJsonResponse(r); renderHistory(_history); } catch(e) {}
}

async function saveProviders() {
  const keys = {
    realdebrid: document.getElementById('pkey-realdebrid')?.value || '',
    alldebrid:  document.getElementById('pkey-alldebrid')?.value  || '',
    torbox:     document.getElementById('pkey-torbox')?.value     || '',
    debridlink: document.getElementById('pkey-debridlink')?.value || '',
  };
  const result = document.getElementById('providers-save-result');
  try {
    const r = await fetch('/api/providers/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(keys)
    });
    const d = await readJsonResponse(r);
    if (d.ok) {
      if (result) result.innerHTML = '<span style="color:var(--pass);">✓ Saved</span>';
      toast('Provider keys saved', 'pass');
      // Clear inputs and reload status
      ['realdebrid','alldebrid','torbox','debridlink'].forEach(p => {
        const el = document.getElementById(`pkey-${p}`);
        if (el) el.value = '';
      });
      loadProviderStatus(true);
    } else {
      if (result) result.innerHTML = `<span style="color:var(--fail2);">✗ ${h(d.error)}</span>`;
    }
  } catch(e) {
    if (result) result.innerHTML = '<span style="color:var(--fail2);">Save failed</span>';
  }
}

async function loadProviderStatus(force = false) {
  const body = document.getElementById('provider-status-body');
  if (!body) return;
  if (!force && body.dataset.loaded) return;
  body.dataset.loaded = '1';
  body.innerHTML = '<div style="color:var(--muted);font-size:12px;"><span class="spin"></span> checking…</div>';
  try {
    const r = await fetch('/api/providers/status');
    const d = await readJsonResponse(r);
    const labels = { realdebrid:'Real-Debrid', alldebrid:'AllDebrid', torbox:'Torbox', debridlink:'Debrid-Link' };
    const configured = Object.entries(d).filter(([,s]) => s.error !== 'no_key');
    if (!configured.length) {
      body.innerHTML = '<div style="color:var(--muted);font-size:12px;">No provider keys configured. Add keys above and save.</div>';
      return;
    }
    const rows = configured.map(([key, s]) => {
      const label = labels[key] || key;
      const sourceHtml = s.source_name ? `<span style="font-size:10px;color:var(--muted);">from ${h(s.source_name)}</span>` : '';
      if (!s.ok) {
        return `<div style="display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--border);">
          <span style="font-size:12px;font-weight:600;color:var(--bright);min-width:110px;">${label}</span>
          ${sourceHtml}
          <span class="badge error">✗ ${h(s.error)}</span>
        </div>`;
      }
      let expiryHtml = '';
      if (s.days_left !== null && s.days_left !== undefined) {
        const cls = s.days_left <= 7 ? 'fail2' : s.days_left <= 30 ? 'warn2' : 'pass';
        expiryHtml = `<span style="font-family:var(--mono);font-size:12px;color:var(--${cls});font-weight:600;">${s.days_left}d left</span>
          <span style="font-size:10px;color:var(--muted);">expires ${h(String(s.expiration))}</span>`;
      }
      return `<div style="display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--border);flex-wrap:wrap;">
        <span style="font-size:12px;font-weight:600;color:var(--bright);min-width:110px;">${label}</span>
        ${sourceHtml}
        <span class="badge success">✓ connected</span>
        <span style="font-size:11px;color:var(--text2);">${h(s.username || '')}</span>
        ${expiryHtml}
      </div>`;
    }).join('');
    body.innerHTML = `<div>${rows}</div>`;
  } catch(e) {
    body.innerHTML = '<div style="color:var(--fail2);font-size:12px;">Failed to load provider status</div>';
  }
}

function toggleProviderCheck(ii, li, pi, enabled, source = '_settingsData') {
  const data = source === '_wizData' ? _wizData : _settingsData;
  if (enabled) {
    data.instances[ii].libraries[li].paths[pi].provider_checks = [{ type: 'realdebrid', api_key: '' }];
  } else {
    data.instances[ii].libraries[li].paths[pi].provider_checks = [];
  }
  const containerId = source === '_wizData' ? `wp-${ii}-${li}` : `si-paths-${ii}-${li}`;
  document.getElementById(containerId).innerHTML =
    renderPathItems(data.instances[ii].libraries[li].paths, ii, li, source);
}

function setProviderType(ii, li, pi, type, source = '_settingsData') {
  const data = source === '_wizData' ? _wizData : _settingsData;
  const pcs = data.instances[ii].libraries[li].paths[pi].provider_checks;
  if (pcs && pcs.length > 0) pcs[0].type = type;
}

// ── Fast poll after run ───────────────────────────────────────────────────────
function pollUntilUpdate(prevHistory) {
  let attempts = 0;
  const iv = setInterval(async () => {
    attempts++;
    await fetchStatus();
    try {
      const r = await fetch('/api/history');
      const h = await readJsonResponse(r);
      if (h.length !== prevHistory.length ||
          (h[0] && prevHistory[0] && h[0].timestamp !== prevHistory[0].timestamp)) {
        _history = h;
        renderHistory(_history);
        clearInterval(iv);
        return;
      }
    } catch(e) {}
    if (attempts >= 20) clearInterval(iv);
  }, 1000);
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const moon = document.getElementById('theme-icon-moon');
  const sun  = document.getElementById('theme-icon-sun');
  if (moon) moon.style.display = theme === 'light' ? 'none' : 'block';
  if (sun)  sun.style.display  = theme === 'light' ? 'block' : 'none';
  localStorage.setItem('mediamender-theme', theme);
}
function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  applyTheme(current === 'dark' ? 'light' : 'dark');
}
// Apply saved theme immediately
(function() {
  const saved = localStorage.getItem('mediamender-theme') || 'dark';
  applyTheme(saved);
})();

// ── Init ──────────────────────────────────────────────────────────────────────
function moveProtectionControls() {
  const target = document.getElementById('mediamender-global-controls');
  if (!target) return;
  const schedule = document.querySelector('.nav-right .sched-wrap');
  if (schedule) {
    const label = document.createElement('strong');
    label.textContent = 'Scheduling';
    label.style.fontSize = '15px';
    target.appendChild(label);
    target.appendChild(schedule);
  }
  ['btn-dryrun','btn-all'].forEach(id => {
    const button = document.getElementById(id);
    if (button) target.appendChild(button);
  });
}

if (!_configMissing) {
  moveProtectionControls();
  applyFeatureVisibility();
  applyPermissionVisibility();
  if (!canAccess('dashboard')) {
    const first = firstAvailablePage();
    showPage(first, document.getElementById(`nav-${first}`));
  }
  applyScheduleLabels();
  selectDashboardView('overview', false);
  window.addEventListener('hashchange', applyRoute);
  if (window.location.hash.length > 1) applyRoute();
  if (_activeFeatures.trash_removal && canAccess('trash_removal')) {
    setInterval(fetchStatus, 15000);
    setInterval(fetchHistory, 30000);
    fetchStatus();
    fetchHistory();
  }
  if (_activeFeatures.metadata_health && canAccess('metadata_health')) loadMetadataAuditStatus();
  if (_activeFeatures.timestamp_repair && canAccess('timestamp_repair')) loadRepairStatus();
  if (_activeFeatures.library_refresh && canAccess('library_refresh')) loadLibraryRefreshStatus();
  initAuthBanner();
}
