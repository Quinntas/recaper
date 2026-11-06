// recaper web UI - no build step, plain ES modules.
const $app = document.getElementById('app');
const STAGES = [
  ['panels', 'Panels', 'Cut chapters into panels'],
  ['read', 'Read', 'Vision model reads every panel'],
  ['script', 'Script', 'Writer turns notes into narration'],
  ['voice', 'Voice', 'Text-to-speech for every beat'],
  ['render', 'Render', 'Compose frames + encode video'],
];
let pollers = [];
let STATE = { sources: null, schema: null, doctor: null };

// ------------------------------------------------------------------ helpers
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  let value;
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'value') value = v;
    else if (['checked', 'disabled', 'selected', 'multiple', 'open', 'hidden', 'webkitdirectory'].includes(k)) el[k] = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  if (value !== undefined) el.value = value;
  return el;
}
const enc = encodeURIComponent;
async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || 'POST';
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || data.detail || r.statusText);
  return data;
}
function toast(msg, kind = '') {
  const t = h('div', { class: `toast ${kind}` }, msg);
  document.getElementById('toasts').append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 7000 : 3500);
}
function fmtBytes(n) { return n > 1e9 ? (n / 1e9).toFixed(2) + ' GB' : (n / 1e6).toFixed(1) + ' MB'; }
function fmtDate(t) { return new Date(t * 1000).toLocaleString(); }
function fmtDur(s) { s = Math.max(0, Math.round(s)); return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m` : s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`; }
function statusChip(status) {
  const cls = { done: 'ok', failed: 'bad', cancelled: 'warn', running: 'accent', queued: '' }[status] || '';
  return h('span', { class: `chip ${cls}` }, status);
}
function clearPollers() { pollers.forEach(clearInterval); pollers = []; }
function every(ms, fn) { const id = setInterval(fn, ms); pollers.push(id); return id; }
function modal(title, body, actions = []) {
  const root = document.getElementById('modal-root');
  const close = () => { root.innerHTML = ''; };
  const back = h('div', { class: 'modal-back', onclick: (e) => { if (e.target === back) close(); } },
    h('div', { class: 'modal' },
      h('div', { class: 'row' }, h('h2', {}, title), h('div', { class: 'spacer' }), h('button', { class: 'ghost', onclick: close }, '✕')),
      body,
      actions.length ? h('div', { class: 'row end', style: { marginTop: '16px' } }, actions) : null));
  root.innerHTML = '';
  root.append(back);
  return close;
}
function route() {
  const raw = location.hash.slice(1) || '/';
  const [path, qs] = raw.split('?');
  return { parts: path.split('/').filter(Boolean).map(decodeURIComponent), q: new URLSearchParams(qs || '') };
}
function go(hash) { location.hash = hash; }
function bar(frac, big = false) { return h('div', { class: `bar ${big ? 'big' : ''}` }, h('i', { style: { width: `${Math.round((frac || 0) * 100)}%` } })); }

// ------------------------------------------------------------------ router
let renderSeq = 0;
async function render() {
  clearPollers();
  const seq = ++renderSeq;
  const { parts, q } = route();
  document.querySelectorAll('[data-nav]').forEach((a) => a.classList.toggle('active',
    (parts[0] || 'library') === a.dataset.nav || (parts[0] === 'p' && a.dataset.nav === 'library')));
  $app.innerHTML = '';
  $app.append(h('div', { class: 'loading' }, 'Loading…'));
  // views that know their title only after loading data set it on `page` (grab STATE.page before any await)
  const page = STATE.page = { title: !parts.length ? 'Library' : { discover: 'Download', jobs: 'Jobs', settings: 'Settings', p: parts[1] }[parts[0]] || 'Not found' };
  document.title = `${page.title} · recaper`;
  let view;
  try {
    if (!parts.length) view = await viewLibrary();
    else if (parts[0] === 'discover' && parts.length === 1) view = await viewDiscover(q);
    else if (parts[0] === 'discover') view = await viewManga(parts[1], parts[2], q);
    else if (parts[0] === 'p') view = await viewProject(parts[1], q.get('tab') || 'make');
    else if (parts[0] === 'jobs') view = await viewJobs();
    else if (parts[0] === 'settings') view = await viewSettings();
    else view = h('div', { class: 'empty' }, h('h2', {}, 'Not found'));
  } catch (e) {
    page.title = 'Error';
    view = h('div', { class: 'empty' }, h('h2', {}, 'Something went wrong'), h('p', {}, e.message));
  }
  if (seq !== renderSeq) return; // the user already navigated elsewhere
  document.title = `${page.title} · recaper`;
  $app.innerHTML = '';
  $app.append(view);
}
window.addEventListener('hashchange', render);

// ------------------------------------------------------------------ library
async function viewLibrary() {
  const [projects, state] = await Promise.all([api('/api/projects'), api('/api/state')]);
  const grid = h('div', { class: 'cards' },
    h('button', { class: 'newcard', onclick: newProjectModal }, h('div', {}, h('div', { style: { fontSize: '30px' } }, '+'), 'New project')),
    projects.map(projectCard));
  return h('div', {},
    h('div', { class: 'row', style: { marginBottom: '18px' } },
      h('div', {}, h('h1', {}, 'Library'), h('div', { class: 'muted small' }, `${projects.length} project(s) in ${state.library}`)),
      h('div', { class: 'spacer' }),
      h('a', { class: 'btn', href: '#/discover' }, '⬇ Download manga'),
      h('button', { class: 'primary', onclick: newProjectModal }, '+ New project')),
    projects.length ? grid : h('div', { class: 'card empty' },
      h('h2', {}, 'No projects yet'),
      h('p', {}, 'Download chapters from a source, or create a project and drop your own chapter folders, .cbz or .pdf files into it.'),
      h('div', { class: 'row', style: { justifyContent: 'center' } },
        h('a', { class: 'btn primary', href: '#/discover' }, '⬇ Download manga'),
        h('button', { onclick: newProjectModal }, '+ New project'))));
}
function projectCard(p) {
  const cover = h('div', { class: 'cover', style: { backgroundImage: `url("/api/projects/${enc(p.name)}/cover"), linear-gradient(135deg, #2a2440, #1a1d24)` } },
    h('div', { class: 'over' },
      p.active_job ? h('span', { class: 'chip accent' }, '● working') : null,
      p.videos.length ? h('span', { class: 'chip ok' }, '▶ video') : null,
      h('div', { class: 'spacer' }),
      h('button', { class: 'small ghost cardx', title: 'Delete…', onclick: (e) => { e.preventDefault(); e.stopPropagation(); deleteProjectModal(p.name, render); } }, '🗑')));
  return h('a', { class: 'pcard', href: `#/p/${enc(p.name)}` }, cover,
    h('div', { class: 'meta' },
      h('div', { class: 'title' }, p.name),
      h('div', { class: 'muted small' }, `${p.chapters} ch · ${p.pages} pages`, p.source ? ` · ${p.source.id}` : '')));
}
function newProjectModal() {
  const input = h('input', { placeholder: 'e.g. Solo Leveling', autofocus: true });
  const create = async () => {
    try {
      const p = await api('/api/projects', { json: { name: input.value.trim() } });
      close(); go(`#/p/${enc(p.name)}?tab=chapters`);
    } catch (e) { toast(e.message, 'bad'); }
  };
  input.addEventListener('keydown', (e) => e.key === 'Enter' && create());
  const close = modal('New project', h('div', { class: 'stack' },
    h('p', { class: 'muted' }, 'A project is a folder of chapters. After creating it you can upload chapter folders, .cbz or .pdf files - or download chapters from a source.'),
    input), [h('button', { onclick: () => close() }, 'Cancel'), h('button', { class: 'primary', onclick: create }, 'Create')]);
  setTimeout(() => input.focus(), 30);
}

// ------------------------------------------------------------------ discover
async function sources() { return STATE.sources || (STATE.sources = await api('/api/sources')); }
function lsGet(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }
function mangaChips(m) {
  return [
    m.license ? h('span', { class: `chip ${m.license_verified ? 'ok' : 'warn'}`, title: m.license_note || '' }, m.license_verified ? `✓ ${m.license}` : m.license) : null,
    !m.license && m.license_note ? h('span', { class: 'chip', title: m.license_note }, '© unverified') : null,
    m.nsfw ? h('span', { class: 'chip bad' }, `18+ ${m.content_rating}`) : null,
    m.status ? h('span', { class: 'chip' }, m.status) : null,
  ];
}
async function viewDiscover(q) {
  const srcs = await sources();
  const src = q.get('src') || lsGet('rc.src', srcs[0].id);
  const source = srcs.find((s) => s.id === src) || srcs[0];
  const langCodes = source.languages.map(([c]) => c);
  const lang = q.has('lang') ? q.get('lang') : (langCodes.includes(lsGet('rc.lang', 'en')) ? lsGet('rc.lang', 'en') : langCodes[0]);
  const query = q.get('q') || '';
  const sorts = source.sorts.map(([v]) => v);
  const sort = sorts.includes(q.get('sort')) ? q.get('sort') : (query ? sorts[0] : (sorts.includes('popular') ? 'popular' : sorts[0]));
  const avails = source.availability.map(([v]) => v);
  const avail = avails.includes(q.get('avail')) ? q.get('avail') : 'any';
  const nsfw = source.supports_nsfw && (q.has('nsfw') ? q.get('nsfw') === '1' : lsGet('rc.nsfw18', '1') === '1');
  lsSet('rc.src', source.id); lsSet('rc.lang', lang);

  const srcSel = h('select', { value: source.id, onchange: () => go(`#/discover?src=${enc(srcSel.value)}`) }, srcs.map((s) => h('option', { value: s.id }, s.name)));
  const langSel = h('select', { value: lang }, source.languages.map(([c, l]) => h('option', { value: c }, c ? `${l} (${c})` : l)));
  const sortSel = h('select', { value: sort }, source.sorts.map(([v, l]) => h('option', { value: v }, l)));
  const availSel = h('select', { value: avail }, source.availability.map(([v, l]) => h('option', { value: v }, l)));
  const nsfwBox = h('input', { type: 'checkbox', checked: nsfw, disabled: !source.supports_nsfw });
  const input = h('input', { value: query, placeholder: 'Search a title (or leave empty to browse), or paste a link', style: { flex: 1, minWidth: '260px' } });
  const submit = () => {
    lsSet('rc.nsfw18', nsfwBox.checked ? '1' : '0');
    go(`#/discover?src=${enc(srcSel.value)}&lang=${enc(langSel.value)}&q=${enc(input.value.trim())}&sort=${enc(sortSel.value)}&avail=${enc(availSel.value)}&nsfw=${nsfwBox.checked ? 1 : 0}`);
  };
  input.addEventListener('keydown', (e) => e.key === 'Enter' && submit());
  [langSel, sortSel, availSel, nsfwBox].forEach((el) => el.addEventListener('change', submit));

  const grid = h('div', { class: 'cards' });
  const more = h('div', { class: 'row', style: { justifyContent: 'center', marginTop: '16px' } });
  let offset = 0;
  const pageSize = 24;
  const load = async () => {
    more.innerHTML = '';
    more.append(h('span', { class: 'muted' }, 'Loading…'));
    try {
      const items = await api(`/api/sources/${enc(source.id)}/search?q=${enc(query)}&lang=${enc(lang)}&sort=${enc(sort)}&availability=${enc(avail)}&nsfw=${nsfw}&offset=${offset}&limit=${pageSize}`);
      more.innerHTML = '';
      if (!items.length && offset === 0) {
        grid.replaceWith(h('div', { class: 'card empty' }, h('h2', {}, 'Nothing found'),
          h('p', {}, avail !== 'any' ? 'Try "Everything" in the availability filter, another language or another source.' : 'Try another language, title or source.')));
        return;
      }
      items.forEach((m) => grid.append(h('a', { class: 'pcard', href: `#/discover/${enc(source.id)}/${enc(m.id)}?lang=${enc(lang)}` },
        h('div', { class: 'cover', style: { backgroundImage: m.cover_url ? `url("/api/sources/${enc(source.id)}/cover?url=${enc(m.cover_url)}")` : '' } },
          h('div', { class: 'over' }, mangaChips(m))),
        h('div', { class: 'meta' }, h('div', { class: 'title' }, m.title),
          h('div', { class: 'muted small' }, [m.year, m.original_language && `orig: ${m.original_language}`,
            m.popularity ? `${m.popularity.toLocaleString()} downloads` : null].filter(Boolean).join(' · '))))));
      offset += pageSize;
      // some filters verify results after fetching, so a page can be shorter than pageSize: still offer more
      if (items.length) more.append(h('button', { onclick: load }, 'Load more'));
    } catch (e) { more.innerHTML = ''; more.append(h('span', { class: 'chip bad' }, e.message)); }
  };
  load();

  return h('div', {},
    h('h1', {}, 'Download manga / manhwa'),
    h('p', { class: 'muted', style: { marginBottom: '6px' } }, source.description),
    source.licence_info ? h('p', { class: 'faint small', style: { marginTop: 0 } }, source.licence_info) : null,
    h('div', { class: 'card tight stack', style: { marginBottom: '18px' } },
      h('div', { class: 'row' },
        h('div', { style: { width: '170px' } }, srcSel), h('div', { style: { width: '230px' } }, langSel), input,
        h('button', { class: 'primary', onclick: submit }, 'Search')),
      h('div', { class: 'row' },
        h('span', { class: 'muted small' }, 'Sort'), h('div', { style: { width: '190px' } }, sortSel),
        h('span', { class: 'muted small' }, 'Show'), h('div', { style: { width: '320px' } }, availSel),
        h('label', { class: 'check', title: source.supports_nsfw ? 'Include erotica / pornographic works (shown unblurred)' : 'This source has no content ratings' },
          nsfwBox, 'NSFW / 18+'),
        avail === 'free' ? h('span', { class: 'chip ok' }, 'Only works you can adapt without copyright issues') : null)),
    grid, more);
}

function licenceBox(m) {
  if (!m.license && !m.license_note && !m.attribution) return null;
  const credit = m.attribution || '';
  return h('div', { class: 'card tight stack', style: { marginTop: '16px' } },
    h('div', { class: 'row' }, h('strong', {}, 'Licence'),
      m.license ? h('span', { class: `chip ${m.license_verified ? 'ok' : 'warn'}` }, m.license_verified ? `✓ ${m.license}` : m.license)
        : h('span', { class: 'chip warn' }, 'Copyrighted / unknown'),
      m.license_note ? h('span', { class: 'muted small' }, m.license_note) : null),
    m.license_verified && credit ? h('div', { class: 'row' }, h('span', { class: 'muted small' }, 'Credit to put in your video description:'),
      h('code', {}, credit), h('button', { class: 'small', onclick: () => { navigator.clipboard.writeText(credit); toast('Copied', 'ok'); } }, 'Copy')) : null);
}
function numKey(c) { const n = parseFloat(c.number); return Number.isFinite(n) ? n : 1e9; }
async function viewManga(sid, mid, q) {
  const page = STATE.page;
  const lang = q.get('lang') || 'en';
  const linkTo = q.get('link') || '';
  const data = await api(`/api/sources/${enc(sid)}/manga/${enc(mid)}?lang=${enc(lang)}`);
  const { manga } = data;
  page.title = `${manga.title} · Download`;
  const srcs = await sources();
  const source = srcs.find((s) => s.id === sid);

  // one row per chapter number; each row can pick between releases (scanlation groups)
  const rowsByNum = new Map();
  for (const c of data.chapters) {
    const key = c.number ?? `oneshot-${c.id}`;
    if (!rowsByNum.has(key)) rowsByNum.set(key, []);
    rowsByNum.get(key).push(c);
  }
  const groups = [...new Set(data.chapters.filter((c) => c.downloadable).map((c) => c.group))];
  let preferred = '';
  const rows = [...rowsByNum.entries()].map(([num, rel]) => ({ num, releases: rel, pick: null, selected: false }));
  const choose = (row) => {
    const ok = row.releases.filter((r) => r.downloadable);
    const pref = ok.find((r) => preferred && r.group === preferred);
    row.pick = pref || ok.sort((a, b) => b.pages - a.pages)[0] || row.releases[0];
  };
  rows.forEach(choose);
  rows.sort((a, b) => numKey(a.releases[0]) - numKey(b.releases[0]));

  const tbody = h('tbody');
  const countEl = h('span', { class: 'muted' });
  const updateCount = () => {
    const sel = rows.filter((r) => r.selected && r.pick?.downloadable);
    countEl.textContent = `${sel.length} chapter(s) selected · ${sel.reduce((a, r) => a + r.pick.pages, 0)} pages`;
  };
  const drawRows = () => {
    tbody.innerHTML = '';
    for (const row of rows) {
      const c = row.pick;
      const dl = c.downloadable;
      const relSel = row.releases.length > 1
        ? h('select', { value: c.id, onchange: (e) => { row.pick = row.releases.find((r) => r.id === e.target.value); drawRows(); updateCount(); } },
            row.releases.map((r) => h('option', { value: r.id }, `${r.group}${r.downloadable ? '' : ' (official site)'}`)))
        : h('span', { class: 'muted' }, c.group);
      tbody.append(h('tr', { class: dl ? '' : 'disabled' },
        h('td', {}, h('input', { type: 'checkbox', checked: row.selected && dl, disabled: !dl, onchange: (e) => { row.selected = e.target.checked; updateCount(); } })),
        h('td', {}, c.number ? `Ch. ${c.number}` : 'Oneshot', c.volume ? h('span', { class: 'faint small' }, ` · vol ${c.volume}`) : null),
        h('td', {}, c.title || h('span', { class: 'faint' }, '—')),
        h('td', {}, relSel),
        h('td', {}, !dl ? '—' : c.kind === 'file' ? (c.size ? fmtBytes(c.size) : 'file') : c.pages),
        h('td', {}, c.downloaded ? h('span', { class: 'chip ok' }, 'downloaded')
          : dl ? '' : h('a', { href: c.external_url, target: '_blank', rel: 'noopener' }, 'official site ↗'))));
    }
  };
  drawRows(); updateCount();

  const rangeInput = h('input', { placeholder: 'e.g. 1-20, 25', style: { width: '160px' } });
  const selectRange = () => {
    const spec = rangeInput.value.trim();
    const parts = spec.split(',').map((s) => s.trim()).filter(Boolean);
    rows.forEach((r) => {
      const n = parseFloat(r.pick.number);
      r.selected = r.pick.downloadable && parts.some((p) => {
        if (p.includes('-')) { const [a, b] = p.split('-'); return n >= (parseFloat(a) || 0) && n <= (parseFloat(b) || 1e9); }
        return n === parseFloat(p);
      });
    });
    drawRows(); updateCount();
  };
  const setAll = (fn) => { rows.forEach((r) => { r.selected = r.pick.downloadable && fn(r); }); drawRows(); updateCount(); };
  const prefSel = h('select', { onchange: (e) => { preferred = e.target.value; rows.forEach(choose); drawRows(); updateCount(); } },
    h('option', { value: '' }, 'Auto (most pages)'), groups.map((g) => h('option', { value: g }, g)));
  const langSel = h('select', { value: lang, onchange: (e) => go(`#/discover/${enc(sid)}/${enc(mid)}?lang=${enc(e.target.value)}`) },
    (source?.languages || []).filter(([c]) => !manga.languages.length || manga.languages.includes(c) || c === lang)
      .map(([c, l]) => h('option', { value: c }, `${l} (${c})`)));
  const projectInput = h('input', { value: data.suggested_project });
  const qualitySel = h('select', {}, h('option', { value: 'data' }, 'Original quality'), h('option', { value: 'data-saver' }, 'Data saver (smaller)'));
  const runAfter = h('input', { type: 'checkbox', checked: !q.get('link') });
  const untilSel = h('select', {}, [['render', 'Full video'], ['script', 'Stop after script (review first)'], ['panels', 'Only cut panels']].map(([v, l]) => h('option', { value: v }, l)));
  const jobBox = h('div', {});
  const start = async () => {
    const ids = rows.filter((r) => r.selected && r.pick.downloadable).map((r) => r.pick.id);
    if (!ids.length) { toast('Select at least one downloadable chapter', 'bad'); return; }
    try {
      const job = await api('/api/download', { json: {
        source: sid, manga_id: mid, lang, chapter_ids: ids, project: projectInput.value.trim(), quality: qualitySel.value,
        run_after: runAfter.checked, run: { until: untilSel.value, force: [], preview: false }, link_to: linkTo || null,
      } });
      toast('Download started', 'ok');
      watchJob(job.id, jobBox, { project: projectInput.value.trim() });
    } catch (e) { toast(e.message, 'bad'); }
  };
  const downloadable = data.chapters.filter((c) => c.downloadable).length;

  return h('div', {},
    h('div', { class: 'row', style: { marginBottom: '10px' } }, h('a', { href: `#/discover?src=${enc(sid)}&lang=${enc(lang)}` }, '← Back to search')),
    linkTo ? h('div', { class: 'card tight row', style: { marginBottom: '12px', borderColor: 'var(--accent)' } },
      h('span', { class: 'chip accent' }, 'image source'), h('span', {}, 'Chapters you download here will be linked as related-work images for '),
      h('a', { href: `#/p/${enc(linkTo)}?tab=images` }, h('strong', {}, linkTo))) : null,
    h('div', { class: 'mhead card' },
      h('div', { class: 'cover', style: { backgroundImage: manga.cover_url ? `url("/api/sources/${enc(sid)}/cover?url=${enc(manga.cover_url)}")` : '' } }),
      h('div', { class: 'stack' },
        h('h1', {}, manga.title),
        manga.alt_titles.length ? h('div', { class: 'muted small' }, manga.alt_titles.slice(0, 3).join(' · ')) : null,
        h('div', { class: 'row' }, mangaChips({ ...manga, nsfw: ['erotica', 'pornographic'].includes(manga.content_rating), status: null }),
          [manga.status, manga.year, manga.content_rating, manga.original_language && `orig ${manga.original_language}`]
          .filter(Boolean).map((t) => h('span', { class: 'chip' }, t)), manga.tags.slice(0, 6).map((t) => h('span', { class: 'chip accent' }, t))),
        h('div', { class: 'desc' }, manga.description || ''),
        h('div', { class: 'row' }, h('span', { class: 'muted' }, 'Language'), h('div', { style: { width: '230px' } }, langSel),
          manga.url ? h('a', { href: manga.url, target: '_blank', rel: 'noopener' }, 'Open on source ↗') : null))),
    licenceBox(manga),
    h('div', { class: 'card', style: { marginTop: '16px' } },
      h('div', { class: 'section-title' }, h('h2', {}, `Chapters (${rows.length})`), h('span', { class: 'muted small' }, `${downloadable} downloadable release(s)`)),
      downloadable === 0 ? h('div', { class: 'chip warn', style: { marginBottom: '10px' } },
        'This series is only available on official sites in this language - nothing to download here. Try another language.') : null,
      h('div', { class: 'row', style: { marginBottom: '12px' } },
        h('button', { class: 'small', onclick: () => setAll(() => true) }, 'All'),
        h('button', { class: 'small', onclick: () => setAll(() => false) }, 'None'),
        h('button', { class: 'small', onclick: () => setAll((r) => !r.pick.downloaded) }, 'Not downloaded'),
        rangeInput, h('button', { class: 'small', onclick: selectRange }, 'Select range'),
        h('div', { class: 'spacer' }), h('span', { class: 'muted small' }, 'Preferred group'), h('div', { style: { width: '220px' } }, prefSel)),
      h('div', { class: 'tablewrap' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, ''), h('th', {}, 'Chapter'), h('th', {}, 'Title'), h('th', {}, 'Release'), h('th', {}, 'Pages'), h('th', {}, ''))), tbody))),
    h('div', { class: 'card', style: { marginTop: '16px' } },
      h('h2', {}, 'Download'),
      h('div', { class: 'optgrid' },
        h('div', { class: 'opt' }, h('label', {}, 'Project folder'), projectInput, h('div', { class: 'help' }, 'Existing project = chapters are added; already downloaded chapters are skipped.')),
        h('div', { class: 'opt' }, h('label', {}, 'Image quality'), qualitySel),
        h('div', { class: 'opt' }, h('label', {}, 'After download'), h('label', { class: 'check' }, runAfter, 'Make the video'), untilSel,
          h('div', { class: 'help' }, 'Uses the project options (Options tab).'))),
      h('div', { class: 'row', style: { marginTop: '14px' } }, countEl, h('div', { class: 'spacer' }), h('button', { class: 'primary', onclick: start }, '⬇ Download')),
      jobBox));
}

// ------------------------------------------------------------------ jobs
function stagesView(job) {
  const wrap = h('div', { class: 'stack' });
  if (job.kind === 'download') {
    const st = job.stages.download || {};
    wrap.append(h('div', { class: 'row' }, h('strong', {}, job.title), statusChip(job.status), h('div', { class: 'spacer' }),
      h('span', { class: 'muted small' }, st.total ? `${Math.floor(st.done)}/${st.total}` : '')),
      bar(job.progress, true), h('div', { class: 'muted small' }, st.msg || ''));
    return wrap;
  }
  const order = STAGES.map((s) => s[0]);
  const curIdx = order.indexOf(job.current);
  wrap.append(h('div', { class: 'row' }, h('strong', {}, job.title), statusChip(job.status), h('div', { class: 'spacer' }),
    job.started ? h('span', { class: 'muted small' }, fmtDur((job.finished || Date.now() / 1000) - job.started)) : null,
    h('span', { class: 'muted small' }, `${Math.round(job.progress * 100)}%`)));
  wrap.append(bar(job.progress, true));
  wrap.append(h('div', { class: 'stages' }, STAGES.map(([id, name, desc], i) => {
    const st = job.stages[id];
    const frac = st && st.total ? Math.min(1, st.done / st.total) : 0;
    const done = job.status === 'done' ? !!st : (st && frac >= 1 && i < curIdx) || i < curIdx;
    return h('div', { class: `stage ${job.current === id && job.status === 'running' ? 'active' : ''} ${done ? 'done' : ''}`, title: desc },
      h('div', { class: 'name' }, name), h('div', { class: 'msg' }, st ? (st.msg || (st.total ? `${st.done}/${st.total}` : '')) : desc), bar(done ? 1 : frac));
  })));
  return wrap;
}
function watchJob(jobId, container, { project, onDone, showLog = true } = {}) {
  let since = 0;
  const top = h('div', {});
  const log = h('div', { class: 'log mono', hidden: !showLog });
  const actions = h('div', { class: 'row' });
  container.innerHTML = '';
  container.append(h('div', { class: 'card', style: { marginTop: '16px' } }, h('div', { class: 'stack' }, top, actions, log)));
  let finished = false;
  const tick = async () => {
    let job;
    try { job = await api(`/api/jobs/${jobId}?since=${since}`); } catch { return; }
    since = job.log_size;
    top.innerHTML = '';
    top.append(stagesView(job));
    if (job.log.length) {
      const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      log.append(job.log.join('\n') + '\n');
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
    actions.innerHTML = '';
    if (job.status === 'running' || job.status === 'queued') {
      actions.append(h('button', { class: 'danger small', onclick: async () => { await api(`/api/jobs/${jobId}/cancel`, { method: 'POST' }); toast('Cancelling…'); } }, '■ Stop'));
    } else {
      if (job.error && job.status === 'failed') actions.append(h('span', { class: 'chip bad' }, job.error.slice(0, 220)));
      if (project) actions.append(h('a', { class: 'btn small', href: `#/p/${enc(project)}` }, 'Open project →'));
      if (job.result && job.result.output) actions.append(h('a', { class: 'btn small primary', href: `#/p/${enc(project || job.project)}?tab=videos` }, '▶ Watch video'));
      if (!finished) {
        finished = true;
        toast(`${job.title}: ${job.status}`, job.status === 'done' ? 'ok' : 'bad');
        // a download may have chained a pipeline job
        if (job.kind === 'download' && job.status === 'done') {
          setTimeout(async () => {
            const all = await api('/api/jobs');
            const next = all.find((j) => j.project === job.project && j.kind === 'pipeline' && ['running', 'queued'].includes(j.status));
            if (next) watchJob(next.id, container, { project: job.project, onDone });
          }, 800);
        }
        onDone && onDone(job);
      }
    }
  };
  tick();
  every(1000, () => { if (!finished) tick(); });
}
async function viewJobs() {
  const list = h('div', { class: 'stack' });
  const draw = async () => {
    const jobs = await api('/api/jobs');
    list.innerHTML = '';
    if (!jobs.length) { list.append(h('div', { class: 'card empty' }, 'No jobs yet in this session.')); return; }
    for (const j of jobs) {
      list.append(h('div', { class: 'card' }, h('div', { class: 'row', style: { marginBottom: '8px' } },
        h('a', { href: `#/p/${enc(j.project)}` }, h('strong', {}, j.project)), h('span', { class: 'muted small' }, fmtDate(j.created)),
        h('div', { class: 'spacer' }),
        ['running', 'queued'].includes(j.status) ? h('button', { class: 'danger small', onclick: async () => { await api(`/api/jobs/${j.id}/cancel`, { method: 'POST' }); draw(); } }, '■ Stop') : null),
      stagesView(j)));
    }
  };
  await draw();
  every(1500, draw);
  return h('div', {}, h('h1', {}, 'Jobs'), h('p', { class: 'muted' }, 'Downloads and video runs started from this UI (since the server started).'), list);
}
async function refreshBadge() {
  try {
    const jobs = await api('/api/jobs');
    const n = jobs.filter((j) => ['running', 'queued'].includes(j.status)).length;
    const b = document.getElementById('jobs-badge');
    b.hidden = !n; b.textContent = n;
  } catch { /* server restarting */ }
}
setInterval(refreshBadge, 3000);

// ------------------------------------------------------------------ project
async function viewProject(name, tab) {
  const page = STATE.page;
  const p = await api(`/api/projects/${enc(name)}`);
  const tabs = [['make', 'Make video'], ['options', 'Options'], ['script', 'Script'], ['videos', `Videos${p.videos.length ? ` (${p.videos.length})` : ''}`], ['chapters', `Chapters (${p.chapters})`], ['images', 'Images'], ['panels', 'Panels'], ['usage', 'Usage']];
  const tabLabel = { make: 'Make video', options: 'Options', script: 'Script', videos: 'Videos', chapters: 'Chapters', images: 'Images', panels: 'Panels' }[tab] || 'Make video';
  page.title = `${p.name} · ${tabLabel}`;
  const body = h('div', {});
  const view = h('div', {},
    h('div', { class: 'phead' },
      h('div', { class: 'cover', style: { backgroundImage: `url("/api/projects/${enc(name)}/cover"), linear-gradient(135deg, #2a2440, #1a1d24)` } }),
      h('div', { class: 'stack', style: { flex: 1, gap: '8px' } },
        h('a', { href: '#/', class: 'small' }, '← Library'),
        h('h1', {}, p.name),
        h('div', { class: 'row' },
          h('span', { class: 'chip' }, `${p.chapters} chapters`), h('span', { class: 'chip' }, `${p.pages} pages`),
          p.detected_source ? h('span', { class: 'chip' }, p.detected_source) : null,
          p.has_script ? h('span', { class: 'chip ok' }, 'script ready') : null,
          p.videos.length ? h('span', { class: 'chip ok' }, `${p.videos.length} video(s)`) : null,
          p.source ? h('a', { class: 'chip accent', href: `#/discover/${enc(p.source.id)}/${enc(p.source.manga_id)}` }, `from ${p.source.id} ↗`) : null,
          p.source?.license ? h('span', { class: `chip ${p.source.license_verified ? 'ok' : 'warn'}` }, p.source.license_verified ? `✓ ${p.source.license}` : p.source.license) : null)),
      h('div', { class: 'stack', style: { gap: '8px', alignItems: 'stretch' } },
        h('button', { class: 'primary', onclick: () => go(`#/p/${enc(name)}?tab=make`) }, '▶ Make video'),
        h('button', { class: 'danger', onclick: () => deleteProjectModal(name) }, '🗑 Delete…'))),
    h('div', { class: 'tabs' }, tabs.map(([id, label]) => h('a', { href: `#/p/${enc(name)}?tab=${id}`, class: tab === id ? 'active' : '' }, label))),
    body);
  const renderers = { make: tabMake, options: tabOptions, script: tabScript, videos: tabVideos, chapters: tabChapters, images: tabImages, panels: tabPanels, usage: tabUsage };
  body.append(await (renderers[tab] || tabMake)(p));
  return view;
}

// ------------------------------------------------------------------ chapter selection + viewer
function rangeSpec(nums, total) {
  const s = [...new Set(nums)].sort((a, b) => a - b);
  if (!s.length || s.length === total) return '';
  const out = [];
  for (let i = 0; i < s.length; i++) {
    let j = i;
    while (j + 1 < s.length && s[j + 1] === s[j] + 1) j++;
    out.push(j > i ? `${s[i]}-${s[j]}` : `${s[i]}`);
    i = j;
  }
  return out.join(',');
}
function parseSpec(spec, total) {
  const out = new Set();
  for (const part of String(spec || '').split(',').map((x) => x.trim()).filter(Boolean)) {
    const m = part.match(/^(\d*)\s*-\s*(\d*)$/);
    if (m) {
      const a = m[1] ? +m[1] : 1; const b = m[2] ? +m[2] : total;
      for (let k = Math.max(1, a); k <= Math.min(total, b); k++) out.add(k);
    } else if (/^\d+$/.test(part) && +part >= 1 && +part <= total) out.add(+part);
  }
  return out;
}

// Chapter picker for the Make tab: returns { el, spec() } - spec '' = all chapters.
function chapterPicker(p, index) {
  const total = index.chapters.length;
  let sel = new Set(index.selected.length ? index.selected : index.chapters.map((c) => c.n));
  let last = null;
  const summary = h('span', { class: 'muted small' });
  const specInput = h('input', { placeholder: 'all chapters, or e.g. 1-5,8', style: { width: '180px' } });
  const list = h('div', { class: 'chlist' });
  const boxes = new Map();
  const sync = (fromInput = false) => {
    for (const [n, cb] of boxes) { cb.checked = sel.has(n); cb.closest('.chrow').classList.toggle('on', sel.has(n)); }
    if (!fromInput) specInput.value = rangeSpec([...sel], total);
    const words = index.kind === 'book' ? index.chapters.filter((c) => sel.has(c.n)).reduce((a, c) => a + (c.words || 0), 0) : 0;
    summary.textContent = `${sel.size} of ${total} chapter(s)${words ? ` · ${(words / 1000).toFixed(0)}k words` : ''}`
      + (sel.size && sel.size < total ? ' · saved as a separate video' : '');
  };
  specInput.addEventListener('input', () => { sel = parseSpec(specInput.value, total); if (!specInput.value.trim()) sel = new Set(index.chapters.map((c) => c.n)); sync(true); });
  for (const c of index.chapters) {
    const cb = h('input', { type: 'checkbox' });
    cb.addEventListener('click', (e) => {
      if (e.shiftKey && last !== null) {  // shift-click selects a range
        const [a, b] = [Math.min(last, c.n), Math.max(last, c.n)];
        for (let k = a; k <= b; k++) { if (cb.checked) sel.add(k); else sel.delete(k); }
      } else if (cb.checked) sel.add(c.n); else sel.delete(c.n);
      last = c.n;
      sync();
    });
    boxes.set(c.n, cb);
    const meta = index.kind === 'book' ? `${c.words.toLocaleString()} words${c.images ? ` · ${c.images} img` : ''}` : (c.pages ? `${c.pages} pages` : c.kind);
    list.append(h('label', { class: 'chrow' }, cb,
      h('span', { class: 'chn mono' }, c.n),
      h('span', { class: 'chtitle', title: c.title }, c.title),
      h('span', { class: 'faint small' }, meta),
      c.read ? h('span', { class: 'chip tiny ok', title: 'Panels already read (cached)' }, 'read') : null,
      c.written ? h('span', { class: 'chip tiny ok', title: 'Script already written (cached)' }, 'script') : null,
      h('button', { type: 'button', class: 'small ghost', title: 'Open this chapter', onclick: (e) => { e.preventDefault(); openViewer(p.name, c.n, total); } }, '👁')));
  }
  sync();
  const btn = (label, fn) => h('button', { type: 'button', class: 'small', onclick: () => { fn(); sync(); } }, label);
  const unread = index.chapters.filter((c) => !c.written).map((c) => c.n);
  const el = h('div', { class: 'opt wide' },
    h('label', {}, 'Chapters'),
    h('div', { class: 'row', style: { marginBottom: '6px' } }, specInput,
      btn('All', () => { sel = new Set(index.chapters.map((c) => c.n)); }),
      btn('None', () => { sel = new Set(); }),
      unread.length && unread.length < total ? btn(`Not written yet (${unread.length})`, () => { sel = new Set(unread); }) : null,
      h('div', { class: 'spacer' }), summary),
    total ? list : h('div', { class: 'muted small' }, 'No chapters yet.'),
    h('div', { class: 'help' }, 'Tick chapters (shift-click selects a range) or type a range. A subset is saved as <title>_ch1-5_recap.mp4, so it never overwrites the full video. Cached chapters cost nothing to reuse.'));
  return { el, spec: () => rangeSpec([...sel], total), count: () => sel.size };
}

// Full-screen chapter viewer: strip (webtoon) or page-by-page; books show text + illustrations.
async function openViewer(project, n, total) {
  const root = document.getElementById('modal-root');
  let mode = lsGet('rc.viewer', 'strip');
  let data = null;
  let page = 0;
  const body = h('div', { class: 'vbody' });
  const title = h('div', { class: 'vtitle' });
  const pageInfo = h('span', { class: 'muted small' });
  const close = () => { root.innerHTML = ''; document.removeEventListener('keydown', onKey); };
  const go = (k) => { if (k >= 1 && k <= total) load(k); };
  const modeBtn = h('button', { class: 'small', onclick: () => { mode = mode === 'strip' ? 'pages' : 'strip'; lsSet('rc.viewer', mode); draw(); } });
  const draw = () => {
    body.innerHTML = '';
    if (!data) return;
    modeBtn.hidden = data.kind !== 'pages';
    modeBtn.textContent = mode === 'strip' ? '▤ Pages' : '▥ Strip';
    if (data.kind === 'book') {
      pageInfo.textContent = `${data.paragraphs.length} paragraphs${data.images.length ? ` · ${data.images.length} illustration(s)` : ''}`;
      body.append(h('div', { class: 'vbook' },
        data.images.length ? h('div', { class: 'vimgs' }, data.images.map((u) => h('img', { src: u, loading: 'lazy' }))) : null,
        data.volume ? h('div', { class: 'faint small' }, data.volume) : null,
        h('h2', {}, data.title), data.paragraphs.map((t) => h('p', {}, t))));
      body.scrollTop = 0;
      return;
    }
    if (!data.pages.length) { body.append(h('div', { class: 'empty' }, 'No pages.')); return; }
    if (mode === 'strip') {
      pageInfo.textContent = `${data.pages.length} pages`;
      body.append(h('div', { class: 'vstrip' }, data.pages.map((u) => h('img', { src: u, loading: 'lazy' }))));
      body.scrollTop = 0;
    } else {
      page = Math.min(page, data.pages.length - 1);
      pageInfo.textContent = `page ${page + 1} / ${data.pages.length}`;
      const img = h('img', { src: data.pages[page], class: 'vpage', onclick: (e) => {
        const left = e.offsetX < e.target.clientWidth / 2;
        step(left ? -1 : 1);
      } });
      body.append(img);
      if (data.pages[page + 1]) { const pre = new Image(); pre.src = data.pages[page + 1]; }
    }
  };
  const step = (d) => {
    if (!data || data.kind !== 'pages' || mode !== 'pages') return;
    const k = page + d;
    if (k < 0) { if (n > 1) load(n - 1, true); return; }
    if (k >= data.pages.length) { go(n + 1); return; }
    page = k; draw();
  };
  const load = async (k, atEnd = false) => {
    n = k;
    title.textContent = `Chapter ${k} / ${total}`;
    body.innerHTML = '';
    body.append(h('div', { class: 'loading' }, 'Opening chapter…'));
    try {
      data = await api(`/api/projects/${enc(project)}/chapter-view/${k}`);
      title.textContent = `${k}. ${data.title}`;
      page = atEnd && data.kind === 'pages' ? data.pages.length - 1 : 0;
      prevBtn.disabled = k <= 1; nextBtn.disabled = k >= total;
      draw();
    } catch (e) { body.innerHTML = ''; body.append(h('div', { class: 'empty' }, e.message)); }
  };
  const onKey = (e) => {
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowRight') { if (mode === 'pages' && data?.kind === 'pages') step(1); else go(n + 1); }
    else if (e.key === 'ArrowLeft') { if (mode === 'pages' && data?.kind === 'pages') step(-1); else go(n - 1); }
  };
  const prevBtn = h('button', { class: 'small', onclick: () => go(n - 1) }, '‹ Prev chapter');
  const nextBtn = h('button', { class: 'small', onclick: () => go(n + 1) }, 'Next chapter ›');
  root.innerHTML = '';
  root.append(h('div', { class: 'viewer' },
    h('div', { class: 'vbar' }, title, pageInfo, h('div', { class: 'spacer' }), modeBtn, prevBtn, nextBtn,
      h('button', { class: 'small ghost', title: 'Close (Esc)', onclick: close }, '✕')),
    body));
  document.addEventListener('keydown', onKey);
  load(n);
}

async function tabMake(p) {
  const untilSel = h('select', {}, [['render', 'Full video'], ['voice', 'Stop after voice'], ['script', 'Stop after script'], ['read', 'Stop after reading panels'], ['panels', 'Stop after cutting panels']]
    .map(([v, l]) => h('option', { value: v }, l)));
  const force = Object.fromEntries(STAGES.map(([id]) => [id, h('input', { type: 'checkbox' })]));
  const preview = h('input', { type: 'checkbox' });
  const output = h('input', { placeholder: 'default: <title>_recap.mp4' });
  const jobBox = h('div', {});
  const estimateBox = h('div', {});
  let index = { kind: 'pages', chapters: [], selected: [], selection: '' };
  try { index = await api(`/api/projects/${enc(p.name)}/chapter-index`); } catch (e) { toast(e.message, 'bad'); }
  const picker = chapterPicker(p, index);
  api(`/api/projects/${enc(p.name)}/usage-log`).then((d) => {
    const e = d.estimate;
    if (!e || e.error) return;
    const bits = [`~${e.reader.calls} reader call(s) (${e.reader.images} panel images)`, `~${e.writer.calls} writer call(s)`];
    if (e.images.max) bits.push(`up to ${e.images.max} generated images${e.images.usd ? ` (≈ $${e.images.usd.toFixed(2)})` : ''}`);
    estimateBox.append(h('div', { class: 'card tight row small' }, h('b', {}, 'Next run:'), h('span', { class: 'muted' }, bits.join(' · ')),
      h('div', { class: 'spacer' }), h('a', { href: `#/p/${enc(p.name)}?tab=usage` }, 'Usage & history →')));
  }).catch(() => {});
  const doctor = await getDoctor();
  const warn = [];
  const eff = p.effective;
  for (const [role, spec] of [['Reader', eff.reader], ['Writer', eff.writer]]) {
    const names = spec.split(',').map((s) => s.trim());
    const ok = names.some((n) => {
      const key = Object.keys(doctor.providers).find((k) => n.startsWith(k.split(':')[0])
        || (k === 'deepseek-free' && n.includes('opencode'))
        || (k === 'antigravity' && (n === 'agy' || n.startsWith('agy:'))));
      return !key || doctor.providers[key].ok;
    });
    if (!ok) warn.push(`${role} (${spec}) looks unavailable on this machine - check System check.`);
  }
  if (!p.chapters) warn.push('No chapters yet - add some in the Chapters tab or download them.');
  const start = async (quick = false) => {
    if (index.chapters.length && !picker.count()) { toast('Pick at least one chapter', 'bad'); return; }
    // '' = all: send an explicit full range when the project's saved options narrow the selection
    const chapters = picker.spec() || (index.selection ? `1-${index.chapters.length}` : null);
    try {
      const job = await api(`/api/projects/${enc(p.name)}/run`, { json: {
        until: quick ? 'render' : untilSel.value, force: Object.entries(force).filter(([, el]) => el.checked).map(([k]) => k),
        preview: preview.checked, output: output.value.trim() || null, quick, chapters,
      } });
      watchJob(job.id, jobBox, { project: p.name });
    } catch (e) { toast(e.message, 'bad'); }
  };
  if (p.active_job) setTimeout(() => watchJob(p.active_job, jobBox, { project: p.name }), 0);
  const recent = p.jobs.filter((j) => j.id !== p.active_job).slice(0, 5);
  return h('div', { class: 'stack' },
    warn.length ? h('div', { class: 'card tight stack' }, warn.map((w) => h('div', { class: 'chip warn' }, w))) : null,
    h('div', { class: 'card' },
      h('div', { class: 'section-title' }, h('h2', {}, 'Run'), h('span', { class: 'muted small' },
        `reader ${eff.reader} · writer ${eff.writer} · style ${eff.style}`), h('div', { class: 'spacer' }),
        h('a', { class: 'small', href: `#/p/${enc(p.name)}?tab=options` }, 'Change options →')),
      h('div', { class: 'optgrid' },
        picker.el,
        h('div', { class: 'opt' }, h('label', {}, 'Stop after'), untilSel, h('div', { class: 'help' }, 'Stop after the script to review/edit it before voicing.')),
        h('div', { class: 'opt' }, h('label', {}, 'Output file'), output),
        h('div', { class: 'opt' }, h('label', {}, 'Redo stages (ignore cache)'),
          h('div', { class: 'row' }, STAGES.map(([id, name]) => h('label', { class: 'check' }, force[id], name))),
          h('div', { class: 'help' }, 'Everything is cached; finished stages are skipped unless ticked here.')),
        h('div', { class: 'opt' }, h('label', {}, 'Extras'), h('label', { class: 'check' }, preview, 'Write panel contact sheets (Panels tab)'))),
      h('div', { class: 'row end', style: { marginTop: '14px' } },
        h('span', { class: 'muted small' }, 'Quick preview: the first ~60 s at 480p, to check look, voice and pacing. Its audio is reused by the full run.'),
        h('button', { onclick: () => start(true), disabled: !!p.active_job, title: 'Writes <title>_recap_preview.mp4' }, '⚡ Quick preview'),
        h('button', { class: 'primary', onclick: () => start(false), disabled: !!p.active_job }, '▶ Start'))),
    estimateBox,
    jobBox,
    recent.length ? h('div', { class: 'card' }, h('h3', {}, 'Recent runs'), recent.map((j) => h('div', { class: 'row', style: { padding: '6px 0' } },
      statusChip(j.status), h('span', {}, j.title), h('span', { class: 'muted small' }, fmtDate(j.created)),
      j.error ? h('span', { class: 'faint small' }, j.error.slice(0, 120)) : null))) : null);
}

// ------------------------------------------------------------------ usage
function billChip(list) {
  if (!list || !list.length) return null;
  return list.includes('api') ? h('span', { class: 'chip warn tiny', title: 'Billed per token by an API key' }, 'API-billed')
    : h('span', { class: 'chip ok tiny', title: 'Subscription plan, local or free tier: counts against the plan\'s limits, no bill' }, 'plan / free');
}
function estimateCard(e, p) {
  if (!e || e.error) return h('div', { class: 'card tight muted small' }, `No estimate: ${e ? e.error : 'unknown'}`);
  const row = (label, value, extra = null) => h('tr', {}, h('td', { class: 'muted' }, label), h('td', {}, value, extra ? ' ' : null, extra));
  const gen = e.images.max ? `up to ${e.images.max} images (${e.images.spec})` : (e.images.spec === 'none' ? 'off' : 'none needed (script exists)');
  return h('div', { class: 'card stack' },
    h('div', { class: 'section-title' }, h('h2', {}, 'Next full run (estimate)'), h('span', { class: 'muted small' },
      `${e.chapters} chapter(s) selected · ${e.unread} to read · ${e.unwritten} to write`)),
    h('div', { class: 'tablewrap' }, h('table', {}, h('tbody', {},
      row('Reader (vision)', `~${e.reader.calls} call(s) · ~${e.reader.images} panel images sent`, billChip(e.reader.billing)),
      row('Writer', `~${e.writer.calls} call(s)`, billChip(e.writer.billing)),
      row('Image generation', gen, e.images.usd ? h('span', { class: 'chip warn tiny' }, `≈ $${e.images.usd.toFixed(2)} max`) : null),
      row('Voice', `~${e.voice.words.toLocaleString()} words with ${e.voice.engine} (only changed lines are re-voiced)`),
      row('Video', `~${e.video.minutes} min${e.video.parts > 1 ? ` in ${e.video.parts} parts` : ''}`),
      row('Upscaler', `${e.upscaler.mode} · ${e.upscaler.cached} panel(s) already upscaled`)))),
    h('div', { class: 'muted small' }, 'Finished work is cached, so these counts only cover what is missing. Reading every panel is what uses most of a Claude / Antigravity plan window.'));
}
async function tabUsage(p) {
  const data = await api(`/api/projects/${enc(p.name)}/usage-log`);
  const runs = data.runs;
  const runCard = (r) => {
    const llm = Object.entries(r.llm);
    const calls = llm.reduce((a, [, v]) => a + v.calls, 0);
    const quota = llm.reduce((a, [, v]) => a + v.quota, 0);
    return h('div', { class: 'card stack' },
      h('div', { class: 'row' }, statusChip(r.status === 'unfinished' ? 'running' : r.status), h('b', {}, fmtDate(r.started)),
        h('span', { class: 'muted small' }, `${fmtDur(r.seconds)} · ${r.meta.quick ? 'quick preview' : `until ${r.meta.until || 'render'}`}`),
        quota ? h('span', { class: 'chip bad tiny' }, `${quota} quota hit(s)`) : null,
        h('div', { class: 'spacer' }), r.outputs.length ? h('span', { class: 'muted small' }, r.outputs.join(', ')) : null),
      r.error ? h('div', { class: 'chip bad' }, r.error.slice(0, 300)) : null,
      llm.length ? h('div', { class: 'tablewrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Model', 'Role', 'Calls', 'OK', 'Quota', 'Refused', 'Errors', 'Images sent', 'Text in / out', 'Time'].map((x) => h('th', {}, x)))),
        h('tbody', {}, llm.map(([prov, v]) => h('tr', {},
          h('td', {}, h('span', { class: 'mono small' }, prov), ' ', billChip([v.billing])), h('td', {}, v.role), h('td', {}, v.calls), h('td', {}, v.ok),
          h('td', {}, v.quota || ''), h('td', {}, v.refused || ''), h('td', {}, v.errors || ''), h('td', {}, v.images_sent || ''),
          h('td', { class: 'small' }, `${(v.chars_in / 1000).toFixed(0)}k / ${(v.chars_out / 1000).toFixed(0)}k chars`), h('td', {}, fmtDur(v.seconds))))))) : null,
      h('div', { class: 'row small muted' },
        calls ? null : h('span', {}, 'No model calls (everything cached).'),
        r.images.ok || r.images.failed ? h('span', {}, `Images: ${r.images.ok} generated${r.images.failed ? `, ${r.images.failed} failed` : ''} (${r.images.provider})`) : null,
        r.voice.lines ? h('span', {}, `Voice: ${r.voice.lines} new line(s) with ${r.voice.engine}`) : null,
        r.upscaled ? h('span', {}, `Upscaled ${r.upscaled} panel(s)`) : null,
        r.renders.map((x) => h('span', {}, `Rendered ${x.output}: ${x.video_minutes} min in ${fmtDur(x.seconds)} (${x.rendered}/${x.chunks} chunks new)`))));
  };
  return h('div', { class: 'stack' },
    estimateCard(data.estimate, p),
    h('h2', { style: { margin: '6px 0 0' } }, 'Past runs'),
    runs.length ? runs.map(runCard) : h('div', { class: 'card empty' }, 'No runs logged yet. Every run from now on records its model calls, quota hits, images, voice and render time here.'));
}

async function getSchema() { return STATE.schema || (STATE.schema = await api('/api/options')); }
async function getDoctor(refresh = false) {
  if (!STATE.doctor || refresh) STATE.doctor = await api(`/api/doctor${refresh ? '?refresh=1' : ''}`);
  return STATE.doctor;
}

// Multi-select dropdown for AI model chains (reader / writer).
let _liveModelsFetched = false;
function modelsSelect({ schema, value, placeholder, needVision = false, onchange }) {
  const parse = (v) => (v ? String(v).split(',').map((s) => s.trim()).filter(Boolean) : []);
  let selected = parse(value);
  const pills = h('div', { class: 'msel-pills' });
  const menu = h('div', { class: 'msel-menu', hidden: true });
  const customInput = h('input', {
    placeholder: 'Custom model (e.g. antigravity:claude-opus-5-5, ollama:llava)…',
    onclick: (e) => e.stopPropagation(),
  });
  const addCustom = (e) => {
    e && e.stopPropagation();
    const parts = parse(customInput.value);
    if (!parts.length) return;
    for (const p of parts) { if (!selected.includes(p)) selected.push(p); }
    customInput.value = '';
    commit();
  };
  customInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addCustom(e); } });
  const customRow = h('div', { class: 'msel-custom', onclick: (e) => e.stopPropagation() },
    customInput, h('button', { type: 'button', class: 'small', onclick: addCustom }, '+ Add'));

  const labelMap = () => {
    const map = new Map();
    for (const g of schema.model_groups || []) {
      for (const m of g.models || []) map.set(m.value, m.label);
    }
    return map;
  };
  const drawPills = () => {
    pills.innerHTML = '';
    if (!selected.length) {
      pills.append(h('span', { class: 'msel-placeholder' }, placeholder));
      return;
    }
    const labels = labelMap();
    selected.forEach((spec, idx) => {
      const move = (dir, e) => {
        e.stopPropagation();
        const j = idx + dir;
        if (j < 0 || j >= selected.length) return;
        [selected[idx], selected[j]] = [selected[j], selected[idx]];
        commit();
      };
      const rm = (e) => {
        e.stopPropagation();
        selected.splice(idx, 1);
        commit();
      };
      pills.append(h('span', { class: 'msel-pill', title: spec },
        h('span', { class: 'msel-ord' }, `${idx + 1}.`),
        h('span', { class: 'msel-name' }, labels.get(spec) || spec),
        selected.length > 1 ? h('button', { type: 'button', title: 'Move earlier', disabled: idx === 0, onclick: (e) => move(-1, e) }, '‹') : null,
        selected.length > 1 ? h('button', { type: 'button', title: 'Move later', disabled: idx === selected.length - 1, onclick: (e) => move(1, e) }, '›') : null,
        h('button', { type: 'button', title: 'Remove', onclick: rm }, '×')));
    });
  };
  const drawMenu = () => {
    menu.innerHTML = '';
    const groups = schema.model_groups || [];
    const known = new Set();
    for (const g of groups) {
      const box = h('div', { class: 'msel-group' }, h('div', { class: 'msel-gtitle' }, g.label));
      for (const m of g.models || []) {
        known.add(m.value);
        const idx = selected.indexOf(m.value);
        const disabled = needVision && m.vision === false;
        const cb = h('input', {
          type: 'checkbox', checked: idx !== -1, disabled,
          onchange: () => {
            if (cb.checked) { if (!selected.includes(m.value)) selected.push(m.value); }
            else selected = selected.filter((x) => x !== m.value);
            commit();
          },
        });
        box.append(h('label', { class: `msel-opt ${idx !== -1 ? 'on' : ''} ${disabled ? 'disabled' : ''}`, onclick: (e) => e.stopPropagation() },
          cb,
          h('span', { class: 'msel-olabel' }, m.label),
          h('span', { class: 'faint small mono' }, m.value),
          disabled ? h('span', { class: 'chip tiny warn' }, 'text only') : null,
          idx !== -1 ? h('span', { class: 'chip tiny accent' }, `#${idx + 1}`) : null));
      }
      menu.append(box);
    }
    const extras = selected.filter((s) => !known.has(s));
    if (extras.length) {
      const box = h('div', { class: 'msel-group' }, h('div', { class: 'msel-gtitle' }, 'Custom'));
      for (const spec of extras) {
        const idx = selected.indexOf(spec);
        const cb = h('input', {
          type: 'checkbox', checked: true,
          onchange: () => { selected = selected.filter((x) => x !== spec); commit(); },
        });
        box.append(h('label', { class: 'msel-opt on', onclick: (e) => e.stopPropagation() },
          cb, h('span', { class: 'msel-olabel mono' }, spec), h('span', { class: 'chip tiny accent' }, `#${idx + 1}`)));
      }
      menu.append(box);
    }
    menu.append(customRow);
  };
  const commit = () => {
    drawPills();
    if (!menu.hidden) drawMenu();
    onchange(selected.join(','));
  };
  const toggle = (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    document.querySelectorAll('.msel-menu').forEach((m) => { m.hidden = true; });
    menu.hidden = !open;
    if (open) {
      drawMenu();
      if (!_liveModelsFetched) {
        _liveModelsFetched = true;
        api('/api/models?live=true').then((r) => {
          if (r && r.groups) { schema.model_groups = r.groups; drawPills(); if (!menu.hidden) drawMenu(); }
        }).catch(() => {});
      }
    }
  };
  const trigger = h('div', { class: 'msel-box', tabindex: '0', onclick: toggle, onkeydown: (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(e); }
    else if (e.key === 'Escape') menu.hidden = true;
  } }, pills, h('span', { class: 'msel-arrow' }, '▾'));
  drawPills();
  const root = h('div', { class: 'msel' }, trigger, menu);
  return {
    el: root,
    reset: () => { selected = []; drawPills(); if (!menu.hidden) drawMenu(); },
  };
}
document.addEventListener('click', () => {
  document.querySelectorAll('.msel-menu').forEach((m) => { m.hidden = true; });
});

// ------------------------------------------------------------------ option widgets
// Dropdown with labelled (optionally grouped) choices, an "inherited" empty entry and an optional "Custom…" text box.
function comboSelect({ choices = [], labels = {}, groups = null, custom = false, emptyLabel, value, onchange }) {
  const flat = groups ? groups.flatMap((g) => g.options.map((o) => String(o[0]))) : choices.map(String);
  const v = value === '' || value == null ? '' : String(value);
  const isCustom = custom && v !== '' && !flat.includes(v);
  const text = h('input', { value: isCustom ? v : '', placeholder: 'Type a value…', hidden: !isCustom, oninput: (e) => onchange(e.target.value) });
  const opt = (val, lab) => h('option', { value: val }, lab);
  const sel = h('select', { onchange: (e) => {
    if (e.target.value === '__custom') { text.hidden = false; text.focus(); onchange(text.value); }
    else { text.hidden = true; onchange(e.target.value); }
  } },
  opt('', emptyLabel),
  groups ? groups.map((g) => h('optgroup', { label: g.label }, g.options.map(([a, b]) => opt(a, b))))
    : choices.map((c) => opt(c, labels[c] || c)),
  custom ? opt('__custom', 'Custom…') : null);
  sel.value = isCustom ? '__custom' : v;
  return {
    el: h('div', { class: 'combo' }, sel, text),
    reset: () => { sel.value = ''; text.hidden = true; text.value = ''; },
  };
}

// Slider + number box. Empty number box = inherited (slider shows the inherited value, dimmed).
function rangeInput({ range, zero, value, inh, onchange }) {
  const [min, max, step] = range;
  const has = value !== '' && value != null;
  const home = inh ?? min;
  const fmt = (x) => (zero && Number(x) === 0 ? zero : String(x));
  const slider = h('input', { type: 'range', min, max, step, class: has ? '' : 'inherited' });
  slider.value = has ? value : home;
  const num = h('input', { type: 'number', step, class: 'rnum', placeholder: inh == null ? '' : fmt(inh) });
  if (has) num.value = value;
  const hint = h('span', { class: 'faint small rzero' }, zero ? `0 = ${zero}` : '');
  slider.addEventListener('input', () => {
    num.value = slider.value; slider.classList.remove('inherited'); onchange(Number(slider.value));
  });
  num.addEventListener('input', () => {
    if (num.value === '') { slider.value = home; slider.classList.add('inherited'); onchange(''); return; }
    slider.value = num.value; slider.classList.remove('inherited'); onchange(Number(num.value));
  });
  return {
    el: h('div', { class: 'rangebox' }, slider, num, zero ? hint : null),
    reset: () => { num.value = ''; slider.value = home; slider.classList.add('inherited'); },
  };
}

// Multi-select with pills (comma-separated value), options loaded lazily: load() -> [{value, label, detail}].
function multiSelect({ load, value, placeholder, onchange }) {
  const parse = (v) => (v ? String(v).split(',').map((s) => s.trim()).filter(Boolean) : []);
  let selected = parse(value);
  let options = null;
  const pills = h('div', { class: 'msel-pills' });
  const menu = h('div', { class: 'msel-menu', hidden: true });
  const commit = () => { drawPills(); if (!menu.hidden) drawMenu(); onchange(selected.join(', ')); };
  const drawPills = () => {
    pills.innerHTML = '';
    if (!selected.length) { pills.append(h('span', { class: 'msel-placeholder' }, placeholder)); return; }
    selected.forEach((v, i) => pills.append(h('span', { class: 'msel-pill', title: v },
      h('span', { class: 'msel-name' }, v),
      h('button', { type: 'button', title: 'Remove', onclick: (e) => { e.stopPropagation(); selected.splice(i, 1); commit(); } }, '×'))));
  };
  const drawMenu = () => {
    menu.innerHTML = '';
    if (!options) { menu.append(h('div', { class: 'muted small', style: { padding: '6px' } }, 'Loading…')); return; }
    const all = [...options, ...selected.filter((v) => !options.some((o) => o.value === v)).map((v) => ({ value: v, label: v, detail: 'not in the library' }))];
    if (!all.length) { menu.append(h('div', { class: 'muted small', style: { padding: '6px' } }, 'Nothing to pick yet.')); return; }
    for (const o of all) {
      const on = selected.includes(o.value);
      const cb = h('input', { type: 'checkbox', checked: on, onchange: () => {
        selected = cb.checked ? [...selected, o.value] : selected.filter((x) => x !== o.value);
        commit();
      } });
      menu.append(h('label', { class: `msel-opt ${on ? 'on' : ''}`, onclick: (e) => e.stopPropagation() },
        cb, h('span', { class: 'msel-olabel' }, o.label), o.detail ? h('span', { class: 'faint small' }, o.detail) : null));
    }
  };
  const toggle = (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    document.querySelectorAll('.msel-menu').forEach((m) => { m.hidden = true; });
    menu.hidden = !open;
    if (open) {
      drawMenu();
      if (!options) load().then((o) => { options = o; if (!menu.hidden) drawMenu(); }).catch(() => { options = []; drawMenu(); });
    }
  };
  const trigger = h('div', { class: 'msel-box', tabindex: '0', onclick: toggle, onkeydown: (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(e); } else if (e.key === 'Escape') menu.hidden = true;
  } }, pills, h('span', { class: 'msel-arrow' }, '▾'));
  drawPills();
  return { el: h('div', { class: 'msel' }, trigger, menu), reset: () => { selected = []; drawPills(); if (!menu.hidden) drawMenu(); } };
}

// Shared editor for a project's recaper.toml and the global settings file.
// values = keys this file sets; base[key] = what applies when the key is left empty; from[key] = 'global' | 'cwd' (else default).
function optionsEditor({ schema, values, base, from = {}, skip = [], self = null, intro, onSave, toml = '' }) {
  const fromLabel = (k) => ({ global: 'Global', cwd: 'Folder' }[from[k]] || 'Default');
  const tomlPre = h('pre', { class: 'log mono', style: { height: 'auto', maxHeight: '240px' } }, toml || 'Nothing set yet: save to see the generated file.');
  const fieldFor = (o) => {
    const cur = values[o.key];
    const shown = cur === undefined || cur === null ? '' : (o.kind === 'json' ? JSON.stringify(cur) : cur);
    const inh = base[o.key];
    const def = inh === null || inh === undefined ? '' : (typeof inh === 'object' ? JSON.stringify(inh) : String(inh));
    const src = fromLabel(o.key);
    const onchange = (v) => {
      if (v === '' || v === null) delete values[o.key]; else values[o.key] = v;
      wrap.classList.toggle('changed', o.key in values);
    };
    let input;
    let resetUi = null;
    const ui = o.ui || {};
    const labelOf = (x) => (ui.labels && ui.labels[x]) || (ui.groups && ui.groups.flatMap((g) => g.options).find(([a]) => a === x)?.[1]) || x;
    const emptyLabel = `${src} (${def === '' ? 'auto' : labelOf(def)})`;
    if ((o.kind === 'int' || o.kind === 'number') && ui.range) {
      const r = rangeInput({ range: ui.range, zero: ui.zero, value: shown, inh, onchange });
      input = r.el; resetUi = r.reset;
    } else if (o.kind === 'projects' && ui.multi) {
      const ms = multiSelect({
        value: shown, placeholder: 'None linked - pick library projects', onchange,
        load: () => api('/api/projects').then((ps) => ps.filter((x) => x.name !== self).map((x) => ({ value: x.name, label: x.name, detail: `${x.chapters} ch` }))),
      });
      input = ms.el; resetUi = ms.reset;
    } else if (o.kind !== 'models' && o.kind !== 'color' && (ui.groups || (o.choices && o.choices.length && o.kind !== 'bool'))) {
      const cs = comboSelect({ choices: (o.choices || []).map(String), labels: ui.labels || {}, groups: ui.groups, custom: !!ui.custom, emptyLabel, value: shown, onchange });
      input = cs.el; resetUi = cs.reset;
    } else if (o.kind === 'bool') {
      const effective = o.key in values ? !!values[o.key] : !!inh;
      const box = h('input', { type: 'checkbox', checked: effective, onchange: (e) => onchange(e.target.checked === !!inh ? '' : e.target.checked) });
      input = h('label', { class: 'check' }, box, h('span', {}, `On (${src.toLowerCase()} ${inh ? 'on' : 'off'})`));
      resetUi = () => { box.checked = !!inh; };
    } else if (o.kind === 'models') {
      const ms = modelsSelect({
        schema, value: shown, placeholder: `${src} (${def})`, needVision: o.key === 'reader', onchange,
      });
      input = ms.el;
      resetUi = ms.reset;
    } else if (o.kind === 'color') {
      const presets = o.choices || [];
      const c = shown === '' ? '' : String(shown);
      const isCustom = c && !presets.includes(c);
      const picker = h('input', { type: 'color', value: isCustom && /^#[0-9a-f]{6}$/i.test(c) ? c : '#101014', hidden: !isCustom,
        style: { width: '64px', padding: '2px', height: '36px' }, oninput: (e) => onchange(e.target.value) });
      const sel = h('select', { value: isCustom ? '__custom' : c, onchange: (e) => {
        if (e.target.value === '__custom') { picker.hidden = false; onchange(picker.value); } else { picker.hidden = true; onchange(e.target.value); }
      } }, h('option', { value: '' }, `${src} (${def})`), presets.map((x) => h('option', { value: x }, x)), h('option', { value: '__custom' }, 'Custom colour…'));
      input = h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, sel, picker);
      resetUi = () => { sel.value = ''; picker.hidden = true; };
    } else if (o.kind === 'projects') {
      const listId = `dl-${o.key.replace('.', '-')}`;
      const dl = h('datalist', { id: listId });
      api('/api/projects').then((ps) => ps.filter((x) => x.name !== self).forEach((x) => dl.append(h('option', { value: x.name }))));
      input = h('div', {}, h('input', { placeholder: 'e.g. Mushoku Tensei (manga)', value: shown, list: listId, oninput: (e) => onchange(e.target.value) }), dl);
    } else if (o.kind === 'select') {
      input = h('select', { value: shown === '' ? '' : String(shown), onchange: (e) => onchange(e.target.value) },
        h('option', { value: '' }, `${src} (${def || 'none'})`), (o.choices || []).map((x) => h('option', { value: x }, x)));
    } else if (o.kind === 'textarea' || o.kind === 'json') {
      input = h('textarea', { class: o.kind === 'json' ? 'mono' : '', placeholder: def || (o.kind === 'json' ? '{}' : ''), value: shown, oninput: (e) => onchange(e.target.value) });
    } else {
      const listId = o.suggestions ? `dl-${o.key.replace('.', '-')}` : null;
      input = h('input', {
        type: o.kind === 'int' || o.kind === 'number' ? 'number' : 'text', step: o.kind === 'int' ? '1' : 'any',
        placeholder: def, value: shown, list: listId, oninput: (e) => onchange(e.target.value),
      });
      if (listId) input = h('div', {}, input, h('datalist', { id: listId }, o.suggestions.map((s) => h('option', { value: s }))));
    }
    const reset = h('span', { class: 'reset', title: `Back to the ${src.toLowerCase()} value`, onclick: () => {
      delete values[o.key]; wrap.classList.remove('changed');
      if (resetUi) resetUi(); else { const el = wrap.querySelector('input,select,textarea'); el.value = ''; }
    } }, 'reset');
    const wrap = h('div', { class: `opt ${o.kind === 'textarea' || o.kind === 'json' ? 'wide' : ''} ${o.key in values ? 'changed' : ''}` },
      h('label', {}, reset, o.label, from[o.key] === 'global' ? h('span', { class: 'chip tiny', title: 'Set on the Settings page (applies to every project)' }, 'global') : null),
      input, o.help ? h('div', { class: 'help' }, o.help) : null, h('div', { class: 'faint small mono' }, o.key));
    return wrap;
  };
  const save = async () => {
    try {
      const res = await onSave(values);
      tomlPre.textContent = res.toml || '(empty: everything inherited)';
      toast('Options saved', 'ok');
    } catch (e) { toast(e.message, 'bad'); }
  };
  const groups = schema.groups.map((g) => {
    const items = schema.options.filter((o) => o.group === g.id && !skip.includes(o.key));
    return items.length ? h('div', { class: 'card optgroup' }, h('h2', {}, g.label), h('div', { class: 'optgrid' }, items.map(fieldFor))) : null;
  });
  return h('div', {},
    h('div', { class: 'row', style: { marginBottom: '14px' } },
      h('p', { class: 'muted', style: { margin: 0, flex: 1 } }, intro),
      h('button', { class: 'primary', onclick: save }, 'Save options')),
    groups,
    h('div', { class: 'card' }, h('h3', {}, 'Generated file'), tomlPre),
    h('div', { class: 'row end', style: { marginTop: '14px' } }, h('button', { class: 'primary', onclick: save }, 'Save options')));
}

async function tabOptions(p) {
  const schema = await getSchema();
  const globals = Object.keys(p.inherited_from).filter((k) => p.inherited_from[k] === 'global').length;
  return optionsEditor({
    schema, values: { ...p.values }, base: p.inherited, from: p.inherited_from, self: p.name,
    intro: h('span', {}, 'Settings for this project only. Empty = inherited from ',
      h('a', { href: '#/settings' }, 'global Settings'), globals ? ` (${globals} set there)` : '', ' or the default. Saved to recaper.toml in the project folder (the same file the CLI reads).'),
    onSave: (values) => api(`/api/projects/${enc(p.name)}/config`, { method: 'PUT', json: values }),
  });
}

// ------------------------------------------------------------------ global settings
async function viewSettings() {
  const [schema, s] = await Promise.all([getSchema(), api('/api/settings')]);
  const base = Object.fromEntries(schema.options.map((o) => [o.key, o.default]));
  return h('div', {},
    h('div', { style: { marginBottom: '14px' } }, h('h1', {}, 'Settings'),
      h('div', { class: 'muted small' }, 'General options for every project: models, language, voice, look, image generator… A project\'s own Options win over these. Per-series options (title, chapters, image sources) stay on the project.')),
    optionsEditor({
      schema, values: { ...s.values }, base, skip: schema.project_only,
      intro: h('span', {}, 'Empty = built-in default. Saved to ', h('code', {}, s.path), '. The CLI (', h('code', {}, 'recaper run'), ') uses it too.'),
      onSave: (values) => api('/api/settings', { method: 'PUT', json: values }),
      toml: s.toml,
    }));
}

// ------------------------------------------------------------------ deleting
function confirmModal(title, body, label = 'Delete') {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; close(); resolve(v); } };
    const close = modal(title, typeof body === 'string' ? h('p', {}, body) : body,
      [h('button', { onclick: () => finish(false) }, 'Cancel'), h('button', { class: 'primary danger-fill', onclick: () => finish(true) }, label)]);
  });
}

async function deleteProjectModal(name, after = () => go('#/')) {
  let usage;
  try { usage = await api(`/api/projects/${enc(name)}/usage`); } catch (e) { toast(e.message, 'bad'); return; }
  const sz = usage.sizes;
  let what = 'all';
  const opt = (value, label, detail) => h('label', { class: 'check', style: { alignItems: 'flex-start' } },
    h('input', { type: 'radio', name: 'delwhat', value, checked: value === what, onchange: () => { what = value; btn.textContent = labels[value]; } }),
    h('span', {}, h('b', {}, label), h('div', { class: 'muted small' }, detail)));
  const labels = { all: 'Delete project', cache: 'Clear cache', videos: 'Delete videos' };
  const btn = h('button', { class: 'primary danger-fill', onclick: async () => {
    btn.disabled = true;
    try {
      await api(`/api/projects/${enc(name)}?what=${what}`, { method: 'DELETE' });
      close();
      toast({ all: `Deleted ${name}`, cache: 'Cache cleared', videos: 'Videos deleted' }[what], 'ok');
      if (what === 'all') after(); else render();
    } catch (e) { btn.disabled = false; toast(e.message, 'bad'); }
  } }, labels.all);
  const close = modal(`Delete "${name}"`, h('div', { class: 'stack' },
    opt('all', `Everything (${fmtBytes(sz.total)})`, 'The project folder: chapters, videos, script, cache and its settings. This cannot be undone.'),
    opt('cache', `Cache only (${fmtBytes(sz.cache)})`, 'Panels, notes, script, audio and render chunks. Chapters, videos and settings stay; the next run starts from scratch.'),
    opt('videos', `Videos only (${fmtBytes(sz.videos)})`, 'The finished .mp4 files and their chapter lists.'),
    usage.active_job ? h('div', { class: 'chip warn' }, 'A job is running for this project: it will be stopped.') : null,
    usage.linked_by.length ? h('div', { class: 'muted small' }, `Used as an image source by ${usage.linked_by.join(', ')}; deleting everything unlinks it there.`) : null),
  [h('button', { onclick: () => close() }, 'Cancel'), btn]);
}

async function tabScript(p) {
  let data;
  try { data = await api(`/api/projects/${enc(p.name)}/script`); } catch {
    const box = h('div', {});
    return h('div', { class: 'card empty' }, h('h2', {}, 'No script yet'),
      h('p', {}, 'Generate it (cut panels → read → write), review and edit it here, then render.'),
      h('button', { class: 'primary', onclick: async () => {
        try { const job = await api(`/api/projects/${enc(p.name)}/run`, { json: { until: 'script', force: [], preview: false } }); watchJob(job.id, box, { project: p.name, onDone: (j) => j.status === 'done' && render() }); }
        catch (e) { toast(e.message, 'bad'); }
      } }, 'Generate script'), box);
  }
  const script = data.script;
  const panelsByCid = data.panels;
  const cids = Object.keys(panelsByCid);
  const stats = h('span', { class: 'muted small' });
  const updateStats = () => {
    const all = [script.hook, ...script.chapters.map((c) => c.beats)].flat();
    const words = all.reduce((a, b) => a + b.text.split(/\s+/).filter(Boolean).length, 0);
    stats.textContent = `${all.length} beats · ${words} words · ≈ ${(words / 200).toFixed(1)} min at 200 wpm`;
  };
  const panelUrl = (pid) => `/api/projects/${enc(p.name)}/panel/${enc(pid)}`;
  const pickPanel = (defaultCid, onPick) => {
    const grid = h('div', { class: 'picker' });
    const sel = h('select', { value: defaultCid, onchange: () => fill(sel.value) }, cids.map((c) => h('option', { value: c }, c)));
    const fill = (cid) => {
      grid.innerHTML = '';
      (panelsByCid[cid] || []).forEach((pid) => grid.append(h('div', { class: 'p', style: { backgroundImage: `url("${panelUrl(pid)}")` },
        onclick: () => { onPick(pid); close(); } }, h('span', {}, pid.split('-')[1]))));
    };
    fill(defaultCid || cids[0]);
    const close = modal('Pick a panel', h('div', { class: 'stack' }, h('div', { style: { width: '200px' } }, sel), grid));
  };
  const beatList = (beats, cid) => {
    const list = h('div', { class: 'stack' });
    const draw = () => {
      list.innerHTML = '';
      beats.forEach((b, i) => {
        const thumbs = h('div', { class: 'thumbs' },
          b.panels.map((pid, k) => h('div', { class: 'thumb', title: pid, style: { backgroundImage: `url("${panelUrl(pid)}")` } },
            h('button', { title: 'Remove', onclick: () => { if (b.panels.length > 1) { b.panels.splice(k, 1); draw(); } else toast('A beat needs at least one panel', 'bad'); } }, '×'))),
          b.panels.length < 4 ? h('div', { class: 'thumb add', title: 'Add panel', onclick: () => pickPanel(b.panels[0]?.split('-')[0] || cid || cids[0], (pid) => { b.panels.push(pid); draw(); }) }, '+') : null,
          b.generate ? h('div', { class: 'faint small', style: { flexBasis: '100%' }, title: b.generate }, `🎨 asked for: ${b.generate.slice(0, 160)}${b.generate.length > 160 ? '…' : ''}`) : null);
        list.append(h('div', { class: 'beat' },
          h('div', {}, h('textarea', { value: b.text, oninput: (e) => { b.text = e.target.value; updateStats(); } }), thumbs),
          h('div', { class: 'side' },
            h('select', { value: b.emphasis || 'normal', onchange: (e) => { b.emphasis = e.target.value; } },
              [['normal', 'Normal'], ['hit', 'Hit (punch-in)'], ['reveal', 'Reveal (slow push)']].map(([v, l]) => h('option', { value: v }, l))),
            h('div', { class: 'row' },
              h('button', { class: 'small', title: 'Move up', disabled: i === 0, onclick: () => { [beats[i - 1], beats[i]] = [beats[i], beats[i - 1]]; draw(); } }, '↑'),
              h('button', { class: 'small', title: 'Move down', disabled: i === beats.length - 1, onclick: () => { [beats[i + 1], beats[i]] = [beats[i], beats[i + 1]]; draw(); } }, '↓'),
              h('button', { class: 'small', title: 'Insert beat after', onclick: () => { beats.splice(i + 1, 0, { text: '', panels: [...b.panels], emphasis: 'normal' }); draw(); } }, '+'),
              h('button', { class: 'small danger', title: 'Delete beat', onclick: () => { if (beats.length > 1) { beats.splice(i, 1); draw(); updateStats(); } } }, '🗑')))));
      });
    };
    draw();
    return list;
  };
  const box = h('div', {});
  const save = async (thenRender) => {
    try {
      await api(`/api/projects/${enc(p.name)}/script`, { method: 'PUT', json: script });
      toast('Script saved', 'ok');
      if (thenRender) {
        const job = await api(`/api/projects/${enc(p.name)}/run`, { json: { until: 'render', force: [], preview: false } });
        watchJob(job.id, box, { project: p.name });
      }
    } catch (e) { toast(e.message, 'bad'); }
  };
  updateStats();
  const bible = p.bible && p.bible.characters ? h('details', { class: 'card' }, h('summary', {}, h('strong', {}, `Story bible (${Object.keys(p.bible.characters).length} characters)`)),
    h('div', { class: 'stack', style: { marginTop: '10px' } }, Object.entries(p.bible.characters).map(([n, c]) => h('div', {},
      h('strong', {}, n), c.aliases?.length ? h('span', { class: 'faint small' }, ` aka ${c.aliases.join(', ')}`) : null, h('div', { class: 'muted small' }, c.desc))))) : null;
  return h('div', { class: 'stack' },
    h('div', { class: 'card tight row', style: { position: 'sticky', top: '58px', zIndex: 5 } },
      stats, h('div', { class: 'spacer' }),
      h('button', { onclick: () => save(false) }, 'Save'),
      h('button', { class: 'primary', onclick: () => save(true) }, 'Save & render video')),
    box,
    h('p', { class: 'muted small' }, 'Edit text, emphasis and panels. Only changed lines are re-voiced and only affected video chunks re-rendered. To regenerate the script from scratch use Make video → Redo "Script".'),
    bible,
    h('details', { class: 'chapter-block card', open: true }, h('summary', {}, 'Intro (cold-open hook)'), beatList(script.hook, cids[0])),
    script.chapters.map((c) => h('details', { class: 'chapter-block card' }, h('summary', {}, `${c.title} `, h('span', { class: 'faint small' }, `${c.cid} · ${c.beats.length} beats`)), beatList(c.beats, c.cid))));
}

async function tabVideos(p) {
  const credit = p.source?.license_verified && p.source.attribution ? h('div', { class: 'card tight row' },
    h('span', { class: 'chip ok' }, `✓ ${p.source.license}`), h('span', { class: 'muted small' }, 'The licence requires credit - paste this into the video description:'),
    h('code', {}, p.source.attribution),
    h('button', { class: 'small', onclick: () => { navigator.clipboard.writeText(p.source.attribution); toast('Copied', 'ok'); } }, 'Copy')) : null;
  if (credit && p.videos_detail.length) {
    const list = await tabVideosList(p);
    return h('div', { class: 'stack' }, credit, list);
  }
  return tabVideosList(p);
}
async function tabVideosList(p) {
  if (!p.videos_detail.length) return h('div', { class: 'card empty' }, h('h2', {}, 'No videos yet'), h('a', { class: 'btn primary', href: `#/p/${enc(p.name)}?tab=make` }, '▶ Make video'));
  return h('div', { class: 'stack' }, p.videos_detail.map((v) => {
    const url = `/api/projects/${enc(p.name)}/file/${enc(v.name)}`;
    const chap = h('textarea', { class: 'mono', readonly: true, value: v.chapters, style: { minHeight: '120px' } });
    return h('div', { class: 'card stack' },
      h('div', { class: 'row' }, h('h2', { style: { margin: 0 } }, v.name), h('span', { class: 'muted small' }, `${fmtBytes(v.size)} · ${fmtDate(v.modified)}`),
        h('div', { class: 'spacer' }), h('a', { class: 'btn', href: url, download: v.name }, '⬇ Download'),
        h('button', { class: 'danger', onclick: async () => {
          if (!await confirmModal('Delete video', `Delete ${v.name}? This cannot be undone (the cache stays, so it can be rendered again quickly).`)) return;
          try { await api(`/api/projects/${enc(p.name)}/videos/${enc(v.name)}`, { method: 'DELETE' }); toast('Video deleted', 'ok'); render(); } catch (e) { toast(e.message, 'bad'); }
        } }, '🗑')),
      h('video', { src: url, controls: true, preload: 'metadata' }),
      v.chapters ? h('div', { class: 'stack' }, h('div', { class: 'row' }, h('h3', { style: { margin: 0 } }, 'YouTube chapters'), h('div', { class: 'spacer' }),
        h('button', { class: 'small', onclick: () => { navigator.clipboard.writeText(v.chapters); toast('Copied', 'ok'); } }, 'Copy')), chap) : null);
  }));
}

async function tabChapters(p) {
  const status = h('div', { class: 'muted small' });
  let index = { kind: 'pages', chapters: [] };
  try { index = await api(`/api/projects/${enc(p.name)}/chapter-index`); } catch { /* listing still works */ }
  const byTitle = new Map(index.chapters.map((c) => [c.title, c.n]));
  const total = index.chapters.length;
  const upload = async (files) => {
    if (!files.length) return;
    const fd = new FormData();
    for (const { file, path } of files) fd.append('files', file, path);
    status.textContent = `Uploading ${files.length} file(s)…`;
    try {
      const r = await fetch(`/api/projects/${enc(p.name)}/upload`, { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'upload failed');
      toast(`Uploaded ${data.saved} file(s)`, 'ok');
      render();
    } catch (e) { status.textContent = ''; toast(e.message, 'bad'); }
  };
  const fromInput = (input) => upload([...input.files].map((f) => ({ file: f, path: f.webkitRelativePath || f.name })));
  const filesInput = h('input', { type: 'file', multiple: true, accept: '.cbz,.zip,.pdf,image/*', hidden: true, onchange: (e) => fromInput(e.target) });
  const dirInput = h('input', { type: 'file', multiple: true, webkitdirectory: true, hidden: true, onchange: (e) => fromInput(e.target) });
  const walk = (entry, prefix = '') => new Promise((resolve) => {
    if (entry.isFile) entry.file((f) => resolve([{ file: f, path: prefix + f.name }]));
    else {
      const reader = entry.createReader(); const out = [];
      const read = () => reader.readEntries(async (ents) => {
        if (!ents.length) { resolve(out.flat()); return; }
        for (const e of ents) out.push(await walk(e, `${prefix}${entry.name}/`));
        read();
      });
      read();
    }
  });
  const zone = h('div', { class: 'dropzone',
    ondragover: (e) => { e.preventDefault(); zone.classList.add('over'); },
    ondragleave: () => zone.classList.remove('over'),
    ondrop: async (e) => {
      e.preventDefault(); zone.classList.remove('over');
      const items = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
      const files = (await Promise.all(items.map((it) => walk(it)))).flat();
      upload(files);
    } },
    h('div', { style: { fontSize: '26px' } }, '⇪'),
    h('div', {}, 'Drop chapter folders, .cbz/.zip or .pdf files here'),
    h('div', { class: 'row', style: { justifyContent: 'center', marginTop: '10px' } },
      h('button', { onclick: () => filesInput.click() }, 'Choose files'), h('button', { onclick: () => dirInput.click() }, 'Choose a folder')),
    filesInput, dirInput, status);
  return h('div', { class: 'stack' },
    h('div', { class: 'card' }, h('div', { class: 'section-title' }, h('h2', {}, 'Add chapters'), h('div', { class: 'spacer' }),
      p.source ? h('a', { class: 'btn small', href: `#/discover/${enc(p.source.id)}/${enc(p.source.manga_id)}` }, '⬇ Download more chapters') : h('a', { class: 'btn small', href: '#/discover' }, '⬇ Download from a source')),
      zone),
    h('div', { class: 'card' }, h('h2', {}, `Chapters (${p.chapter_list.length})`),
      p.chapter_list.length ? h('div', { class: 'tablewrap' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, '#'), h('th', {}, 'Chapter'), h('th', {}, 'Type'), h('th', {}, 'Pages'), h('th', {}, ''))),
        h('tbody', {}, p.chapter_list.map((c, i) => h('tr', {}, h('td', {}, i + 1), h('td', {}, c.title), h('td', {}, h('span', { class: 'chip' }, c.kind)), h('td', {}, c.pages ?? '—'),
          h('td', { style: { textAlign: 'right', whiteSpace: 'nowrap' } },
            index.kind === 'pages' && byTitle.has(c.title) ? h('button', { class: 'small ghost', title: 'Open', onclick: () => openViewer(p.name, byTitle.get(c.title), total) }, '👁 Open') : null,
            c.file ? h('button', { class: 'small ghost danger', title: 'Delete this chapter', onclick: async () => {
            if (!await confirmModal('Delete chapter', `Delete "${c.title}" from disk? Downloaded chapters can be downloaded again.`)) return;
            try { await api(`/api/projects/${enc(p.name)}/chapters?file=${enc(c.file)}`, { method: 'DELETE' }); toast('Chapter deleted', 'ok'); render(); } catch (e) { toast(e.message, 'bad'); }
          } }, '🗑') : null))))))
        : h('div', { class: 'empty' }, 'No chapters yet.')),
    index.kind === 'book' && total ? h('div', { class: 'card' }, h('h2', {}, `Book chapters (${total})`),
      h('p', { class: 'muted small', style: { marginTop: 0 } }, 'How the volumes above were split - these are the chapter numbers the Make tab uses.'),
      h('div', { class: 'tablewrap' }, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, '#'), h('th', {}, 'Chapter'), h('th', {}, 'Volume'), h('th', {}, 'Words'), h('th', {}, ''))),
        h('tbody', {}, index.chapters.map((c) => h('tr', {}, h('td', {}, c.n), h('td', {}, c.title), h('td', { class: 'muted small' }, c.volume),
          h('td', {}, c.words.toLocaleString()), h('td', { style: { textAlign: 'right' } }, h('button', { class: 'small ghost', onclick: () => openViewer(p.name, c.n, total) }, '👁 Read')))))))) : null);
}

async function tabImages(p) {
  const info = await api(`/api/projects/${enc(p.name)}/images`);
  const linkedBox = h('div', { class: 'stack' });
  const relatedBox = h('div', {});
  const save = async (name, remove = false) => {
    try {
      await api(`/api/projects/${enc(p.name)}/image-sources`, { json: { name, remove } });
      toast(remove ? `Unlinked ${name}` : `Linked ${name}`, 'ok');
      render();
    } catch (e) { toast(e.message, 'bad'); }
  };
  linkedBox.append(info.linked.length
    ? h('div', { class: 'row' }, info.linked.map((l) => h('span', { class: `chip ${l.exists ? 'ok' : 'bad'}` },
        h('a', { href: `#/p/${enc(l.name)}` }, l.name), ` · ${l.chapters} ch `,
        h('a', { href: '#', title: 'Unlink', onclick: (e) => { e.preventDefault(); save(l.name, true); } }, '✕'))))
    : h('div', { class: 'muted' }, 'No related works linked yet. Link an adaptation (manga / manhwa) of this story so its panels can illustrate the narration.'));

  const lang = lsGet('rc.lang', 'en');
  const query = h('input', { value: info.series_title, style: { flex: 1, minWidth: '240px' } });
  const search = async () => {
    relatedBox.innerHTML = '';
    relatedBox.append(h('div', { class: 'loading' }, 'Searching sources and your library…'));
    try {
      const r = await api(`/api/projects/${enc(p.name)}/related?q=${enc(query.value.trim())}&lang=${enc(lang)}`);
      relatedBox.innerHTML = '';
      if (r.library.length) {
        relatedBox.append(h('h3', {}, 'In your library'), h('div', { class: 'row', style: { marginBottom: '14px' } },
          r.library.map((l) => h('div', { class: 'chip accent' }, `${l.name} · ${l.chapters} ch `,
            h('button', { class: 'small', onclick: () => save(l.name) }, 'Link')))));
      }
      for (const [sid, items] of Object.entries(r.sources)) {
        if (items.error) { relatedBox.append(h('div', { class: 'chip bad' }, `${sid}: ${items.error}`)); continue; }
        if (!items.length) continue;
        relatedBox.append(h('h3', {}, `${sid} - downloadable adaptations`), h('div', { class: 'cards', style: { marginBottom: '16px' } },
          items.map((m) => h('a', { class: 'pcard', href: `#/discover/${enc(sid)}/${enc(m.id)}?lang=${enc(lang)}&link=${enc(p.name)}` },
            h('div', { class: 'cover', style: { backgroundImage: m.cover_url ? `url("/api/sources/${enc(sid)}/cover?url=${enc(m.cover_url)}")` : '' } },
              h('div', { class: 'over' }, mangaChips(m))),
            h('div', { class: 'meta' }, h('div', { class: 'title' }, m.title),
              h('div', { class: 'muted small' }, [m.year, m.original_language && `orig: ${m.original_language}`].filter(Boolean).join(' · ')))))));
      }
      if (!relatedBox.children.length) relatedBox.append(h('div', { class: 'card empty' }, 'Nothing related found. Try another title (e.g. the original Japanese name).'));
    } catch (e) { relatedBox.innerHTML = ''; relatedBox.append(h('div', { class: 'chip bad' }, e.message)); }
  };
  query.addEventListener('keydown', (e) => e.key === 'Enter' && search());

  const gen = info.generator;
  const gallery = info.generated.length ? h('div', { class: 'picker', style: { maxHeight: 'none' } }, info.generated.map((g) => h('div', {
    class: 'p', title: g.prompt, style: { backgroundImage: `url("/api/projects/${enc(p.name)}/panel/${enc(g.id)}")`, aspectRatio: '16/9', backgroundPosition: 'center' },
  }, h('span', {}, g.id)))) : h('div', { class: 'muted small' }, 'No generated images yet.');

  return h('div', { class: 'stack' },
    h('div', { class: 'card stack' },
      h('div', { class: 'section-title' }, h('h2', {}, 'Image sources'), h('span', { class: 'muted small' }, 'used to illustrate books / light novels (and any project)')),
      linkedBox),
    h('div', { class: 'card stack' },
      h('h2', {}, 'Find related works'),
      h('p', { class: 'muted small', style: { margin: 0 } }, 'Searches every source and your library for adaptations of this story. Opening a result lets you download chapters straight into a new project that is linked here automatically.'),
      h('div', { class: 'row' }, query, h('button', { class: 'primary', onclick: search }, 'Search')),
      relatedBox),
    h('div', { class: 'card stack' },
      h('div', { class: 'section-title' }, h('h2', {}, 'Generated images'),
        h('span', { class: `chip ${gen.available ? 'ok' : (gen.spec === 'none' ? '' : 'warn')}` }, gen.spec === 'none' ? 'generator off' : `${gen.spec}${gen.available ? ' ready' : ''}`),
        gen.detail ? h('span', { class: 'muted small' }, gen.detail) : null, h('div', { class: 'spacer' }),
        h('a', { class: 'small', href: `#/p/${enc(p.name)}?tab=options` }, 'Generator settings →')),
      h('p', { class: 'muted small', style: { margin: 0 } }, `When no real image fits a key moment, the writer describes the scene and the generator draws it using the work's own art (cover, illustrations, linked panels) as style and character reference - up to ${gen.max} per video. Hover an image to see its prompt.`),
      gallery));
}

async function tabPanels(p) {
  const box = h('div', {});
  const gen = async () => {
    try {
      const job = await api(`/api/projects/${enc(p.name)}/run`, { json: { until: 'panels', force: [], preview: true } });
      watchJob(job.id, box, { project: p.name, onDone: (j) => j.status === 'done' && render() });
    } catch (e) { toast(e.message, 'bad'); }
  };
  return h('div', { class: 'stack' },
    h('div', { class: 'card row' }, h('div', {}, h('h2', {}, 'Panel previews'),
      h('div', { class: 'muted small' }, 'Contact sheets of how each chapter was cut into panels (in reading order). Use them to check the slicing.')),
      h('div', { class: 'spacer' }), h('button', { class: 'primary', onclick: gen }, p.previews.length ? 'Regenerate previews' : 'Generate previews')),
    box,
    h('div', { class: 'previews' }, p.previews.map((f) => h('div', {}, h('h3', {}, f.replace('.jpg', '')),
      h('img', { loading: 'lazy', src: `/api/projects/${enc(p.name)}/file/.recaper/preview/${enc(f)}` })))));
}

// ------------------------------------------------------------------ doctor
document.getElementById('doctor-btn').addEventListener('click', async () => {
  const body = h('div', { class: 'loading' }, 'Checking…');
  const close = modal('System check', body, [h('button', { onclick: () => { close(); document.getElementById('doctor-btn').click(); } }, 'Re-check')]);
  try {
    const d = await getDoctor(true);
    body.className = 'stack';
    body.innerHTML = '';
    body.append(
      h('div', { class: 'row' }, h('span', { class: `chip ${d.ffmpeg ? 'ok' : 'bad'}` }, `ffmpeg ${d.ffmpeg ? '✓' : '✗'}`),
        h('span', { class: `chip ${d.nvenc ? 'ok' : 'warn'}` }, d.nvenc ? 'NVENC GPU encoding ✓' : 'no NVENC (CPU x264)')),
      h('h3', {}, 'AI providers'),
      h('table', {}, h('tbody', {}, Object.entries(d.providers).map(([k, v]) => h('tr', {},
        h('td', {}, h('strong', {}, k)), h('td', {}, h('span', { class: `chip ${v.ok ? 'ok' : 'bad'}` }, v.ok ? 'ready' : 'unavailable')),
        h('td', {}, v.vision ? h('span', { class: 'chip' }, 'vision') : h('span', { class: 'chip warn' }, 'text only')),
        h('td', { class: 'muted small' }, v.detail))))),
      h('h3', {}, 'TTS engines installed'),
      h('div', { class: 'row' }, Object.entries(d.tts_engines).map(([k, v]) => h('span', { class: `chip ${v ? 'ok' : ''}` }, `${k}${v ? ' ✓' : ''}`))),
      h('p', { class: 'muted small' }, 'Install more engines with e.g.  uv sync --extra edge'));
  } catch (e) { body.textContent = e.message; }
});

refreshBadge();
render();
