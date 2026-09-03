// Exercises the rendered router under a stub DOM. Run by test_routing.py.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

const pages = ['dashboard','mediamender','library-refresh','mark-watched',
               'metadata-audit','timestamp-repair','history','settings'];
const sections = ['plex','trash-removal','library-refresh','mark-watched',
                  'metadata-health','timestamp-repair','features','providers',
                  'notifications','general','security','about'];

function el(id, extra = {}) {
  const classes = new Set();
  return {
    id, dataset: extra.dataset || {}, hidden: false, value: '', innerHTML: '',
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c),
      contains: c => classes.has(c), toggle: (c, on) => on ? classes.add(c) : classes.delete(c),
      _set: classes,
    },
    setAttribute() {}, getAttribute: () => null, addEventListener() {},
    querySelectorAll: () => [], closest: () => null,
  };
}

const nodes = new Map();
const pageFeature = {
  'mediamender': 'trash_removal', 'library-refresh': 'library_refresh',
  'mark-watched': 'mark_watched', 'metadata-audit': 'metadata_health',
  'timestamp-repair': 'timestamp_repair',
};
for (const p of pages) {
  const dataset = pageFeature[p] ? {feature: pageFeature[p]} : {};
  nodes.set(`page-${p}`, el(`page-${p}`, {dataset}));
  nodes.set(`nav-${p}`, el(`nav-${p}`));
}
for (const s of sections) nodes.set(`ss-${s}`, el(`ss-${s}`));
nodes.set('dashboard-panel-overview', el('dashboard-panel-overview'));
nodes.set('dashboard-panel-server-0', el('dashboard-panel-server-0'));
nodes.set('toast', el('toast'));

const tabbedPages = ['mediamender','library-refresh','mark-watched',
                     'metadata-audit','timestamp-repair'];
const tabPanels = new Map();
for (const page of tabbedPages) {
  for (const tab of ['main','configure']) {
    const panel = el(`tab-${page}-${tab}`, {dataset: {tab}});
    if (tab === 'main') panel.classList.add('active');
    nodes.set(panel.id, panel);
    tabPanels.set(`${page}/${tab}`, panel);
  }
  const host = nodes.get(`page-${page}`);
  host.querySelectorAll = sel => {
    if (sel === '.feature-panel') return ['main','configure'].map(t => tabPanels.get(`${page}/${t}`));
    if (sel === '.feature-tab') return [];
    return [];
  };
}

const sectionButtons = sections.map(name => {
  const button = el(`ssnav-${name}`, {dataset: {section: name}});
  return button;
});

const hashListeners = [];
global.window = {
  location: {href: 'http://localhost/', origin: 'http://localhost', hash: ''},
  addEventListener: (type, fn) => { if (type === 'hashchange') hashListeners.push(fn); },
  matchMedia: () => ({matches: false, addEventListener() {}}),
};
global.history = {
  replaceState: (_s, _t, url) => { window.location.hash = url; },
  pushState: (_s, _t, url) => { window.location.hash = url; },
};
const noop = () => {};
global.localStorage = {getItem: () => null, setItem: noop, removeItem: noop};
global.document = {
  getElementById: id => nodes.get(id) || null,
  querySelector: sel => {
    const m = /\.settings-nav-item\[data-section="([a-z-]+)"\]/.exec(sel);
    if (m) return sectionButtons.find(b => b.dataset.section === m[1]) || null;
    const tabbed = /^#page-([a-z-]+) \.feature-tabs$/.exec(sel);
    if (tabbed) return tabbedPages.includes(tabbed[1]) ? el('feature-tabs') : null;
    return null;
  },
  querySelectorAll: sel => {
    if (sel === '.page') return pages.map(p => nodes.get(`page-${p}`));
    if (sel === '.nav-link') return pages.map(p => nodes.get(`nav-${p}`));
    if (sel === '.settings-section') return sections.map(s => nodes.get(`ss-${s}`));
    if (sel === '.settings-nav-item') return sectionButtons;
    return [];
  },
  addEventListener: noop, documentElement: {setAttribute: noop},
  body: {classList: {add: noop, remove: noop}},
};
global.window.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve({})});
global.fetch = global.window.fetch;
global.Headers = class { set() {} };
global.setInterval = noop;
global.setTimeout = noop;
global.clearTimeout = noop;

const api = new Function(src + `
  return {showPage, showSettingsSection, applyRoute, currentRoute,
          firstAvailablePage, pageIsAvailable, goToSettings, PAGES,
          selectDashboardView, showFeatureTab, hasFeatureTabs,
          set permissions(list) {
            canAccess = p => list.includes('*') || list.includes(p);
          },
          set features(v) { _activeFeatures = v; }};
`)();

const results = [];
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  results.push({name, ok, actual, expected});
}
function activePage() {
  return pages.find(p => nodes.get(`page-${p}`).classList.contains('active')) || null;
}
function activeSection() {
  return sections.find(s => nodes.get(`ss-${s}`).classList.contains('active')) || null;
}

api.permissions = ['*'];

// A page navigation writes a linkable URL.
api.showPage('mark-watched', nodes.get('nav-mark-watched'));
check('page sets hash', window.location.hash, '#mark-watched');
check('page becomes active', activePage(), 'mark-watched');
check('nav item highlights', nodes.get('nav-mark-watched').classList.contains('active'), true);

// A settings section is addressable as a sub-route.
api.goToSettings('notifications');
check('settings sub-route', window.location.hash, '#settings/notifications');
check('section active', activeSection(), 'notifications');

// Loading a deep link restores both page and sub-view.
window.location.hash = '#settings/security';
api.applyRoute();
check('deep link page', activePage(), 'settings');
check('deep link section', activeSection(), 'security');

// The back button is just another hash change.
window.location.hash = '#history';
hashListeners.forEach(fn => fn());
check('hashchange navigates', activePage(), 'history');
check('history nav highlights', nodes.get('nav-history').classList.contains('active'), true);

// Hashes this app used to write still resolve.
window.location.hash = '#server-0';
api.applyRoute();
check('legacy dashboard hash', activePage(), 'dashboard');

// Garbage falls back rather than blanking the UI.
window.location.hash = '#not-a-page';
api.applyRoute();
check('unknown route falls back', activePage(), 'dashboard');

// A disabled feature is unreachable even by direct link.
api.features = {mark_watched: false, trash_removal: true, library_refresh: true,
                metadata_health: true, timestamp_repair: true};
window.location.hash = '#mark-watched';
api.applyRoute();
check('disabled feature unreachable', activePage() === 'mark-watched', false);

// So is a page the signed-in user has no permission for.
api.features = {mark_watched: true, trash_removal: true, library_refresh: true,
                metadata_health: true, timestamp_repair: true};
api.permissions = ['mark_watched'];
check('permitted page available', api.pageIsAvailable('mark-watched'), true);
check('unpermitted page unavailable', api.pageIsAvailable('settings'), false);
check('first available respects permission', api.firstAvailablePage(), 'mark-watched');

// A feature's Configure tab is addressable, and returns to the operate tab
// when the page is reopened from the navigation.
function activeTab(page) {
  for (const tab of ['main','configure']) {
    if (tabPanels.get(`${page}/${tab}`).classList.contains('active')) return tab;
  }
  return null;
}
api.permissions = ['*'];
api.showPage('mark-watched', nodes.get('nav-mark-watched'));
check('page opens on its main tab', activeTab('mark-watched'), 'main');
api.showFeatureTab('mark-watched', 'configure');
check('configure tab sets hash', window.location.hash, '#mark-watched/configure');
check('configure tab active', activeTab('mark-watched'), 'configure');

window.location.hash = '#timestamp-repair/configure';
api.applyRoute();
check('deep link opens configure tab', activeTab('timestamp-repair'), 'configure');
check('deep link page is correct', activePage(), 'timestamp-repair');

api.showPage('timestamp-repair', nodes.get('nav-timestamp-repair'));
check('reopening returns to main tab', activeTab('timestamp-repair'), 'main');

api.showFeatureTab('mark-watched', 'not-a-tab');
check('unknown tab falls back to main', activeTab('mark-watched'), 'main');

check('settings page has no feature tabs', api.hasFeatureTabs('settings'), false);

const failed = results.filter(r => !r.ok);
for (const r of results) {
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}` +
    (r.ok ? '' : `  (got ${JSON.stringify(r.actual)}, want ${JSON.stringify(r.expected)})`));
}
console.log(failed.length ? `\n${failed.length} FAILED` : `\nALL ${results.length} ROUTER CHECKS PASSED`);
process.exit(failed.length ? 1 : 0);
