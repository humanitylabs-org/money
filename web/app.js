'use strict';
const el = (id) => document.getElementById(id);
const hosted = document.documentElement.classList.contains('wz-hosted');
let ov = null, rows = [], tab = 'review', cache = {}, month = new Date().toISOString().slice(0, 7), cur = null, pick = null, poll = null, timer = null;

function say(text, kind = '', undo = false) {
  const s = el('status'); s.classList.toggle('wz-sr', !kind); s.classList.toggle('note', kind === 'note'); s.replaceChildren(text);
  if (undo) { const b = h('button', 'mn-undo', 'Undo'); b.type = 'button'; b.onclick = doUndo; s.append(' ', b); }
}
async function api(path, body) {
  const init = body === undefined ? { cache: 'no-store' } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, init);
  let out = {};
  try { out = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(out.error || 'Something went wrong. Try again.');
  return out;
}
/* Show the last answer instantly, then refresh it quietly (stale-while-revalidate). */
const CK = 'money.cache.v1';
try { cache = JSON.parse(localStorage.getItem(CK) || '{}'); } catch (e) { cache = {}; }
let saveT = null;
function remember(path, data) {
  cache[path] = data;
  clearTimeout(saveT);
  saveT = setTimeout(() => {
    const keep = {}; for (const k of Object.keys(cache).slice(-12)) keep[k] = cache[k];
    cache = keep;
    try { localStorage.setItem(CK, JSON.stringify(keep)); } catch (e) {}
  }, 300);
  return data;
}
async function get(path, paint) {
  if (cache[path]) paint(cache[path], true);
  const fresh = remember(path, await api(path));
  paint(fresh, false);
  return fresh;
}
function h(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
const money = (n, cur) => n == null ? '' : new Intl.NumberFormat(undefined, { style: 'currency', currency: cur || 'USD' }).format(n);
const day = (d) => d ? new Date(d + 'T12:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '';
const when = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'not yet';

/* ---------- overview ---------- */
async function loadOverview() {
  await get('api/overview', paintOverview);
}
function paintOverview(data) {
  ov = data;
  const ready = ov.transactions > 0 || ov.accounts.length > 0;
  const anyConnected = ov.sources.some((s) => s.connected);
  el('setup').hidden = ready || anyConnected || ov.syncing;
  el('badge').hidden = !ov.to_review; el('badge').textContent = ov.to_review > 99 ? '99+' : ov.to_review;
  try { window.WizardApp?.badge?.set(ov.to_review); } catch (e) {}
  renderAlerts();
  renderAccounts();
  clearTimeout(poll);
  if (ov.syncing) poll = setTimeout(() => refresh(), 4000);
  if (tab === 'review') renderFilterChips();
}

/* ---------- review: filter and sort ---------- */
const FKEY = 'money.filter.v1';
const DEF = { show: 'todo', sort: 'newest', direction: '', size: '', when: '', category: '', account: '', origin: '' };
let f = { ...DEF };
try { f = { ...DEF, ...JSON.parse(localStorage.getItem(FKEY) || '{}') }; } catch (e) {}
if (f.sort === 'spend' || f.sort === 'income') f.sort = 'largest';
const saveF = () => { try { localStorage.setItem(FKEY, JSON.stringify(f)); } catch (e) {} };
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
function range(w) {
  const t = new Date();
  if (w === 'month') return [iso(new Date(t.getFullYear(), t.getMonth(), 1)), iso(t)];
  if (w === 'last') return [iso(new Date(t.getFullYear(), t.getMonth() - 1, 1)), iso(new Date(t.getFullYear(), t.getMonth(), 0))];
  if (w === '30' || w === '90') return [iso(new Date(t - (+w) * 864e5)), iso(t)];
  if (/^\d{4}-\d{2}$/.test(w)) { const [y, m] = w.split('-').map(Number); return [w + '-01', iso(new Date(y, m, 0))]; }
  return [null, null];
}
const monthName = (w) => new Date(w + '-15').toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
const SORTS = [['newest', 'Newest first'], ['oldest', 'Oldest first'], ['largest', 'Biggest amounts'], ['smallest', 'Smallest amounts'],
  ['merchant', 'Merchant A–Z'], ['merchant_desc', 'Merchant Z–A'], ['account', 'Card A–Z'], ['account_desc', 'Card Z–A'],
  ['category', 'Category A–Z'], ['category_desc', 'Category Z–A']];
const SIZES = [['', 'Any size'], ['50', '$50+'], ['100', '$100+'], ['500', '$500+'], ['1000', '$1,000+']];
function groups() {
  const cats = ['Uncategorized', ...(ov?.categories || [])];
  const accts = (ov?.accounts || []).map((a) => [a.id, a.name]);
  const g = [
    ['show', 'Show', [['todo', 'To review'], ['all', 'Everything']]],
    ['sort', 'Sort', SORTS, true],
    ['direction', 'Money', [['', 'In and out'], ['out', 'Money out'], ['in', 'Money in']]],
    ['size', 'Size', SIZES],
    ['when', 'When', [['', 'Any time'], ['month', 'This month'], ['last', 'Last month'], ['30', 'Last 30 days'], ['90', 'Last 90 days']]],
    ['category', 'Category', [['', 'Any category'], ...cats.map((c) => [c, c])], true],
    ['account', 'Account', [['', 'Any account'], ...accts], true],
  ];
  if (ov?.history) g.push(['origin', 'Source', [['', 'Both'], ['bank', 'From ' + ov.source_name], ['import', ov.history.from_name + ' history']]]);
  return g;
}
function label(key, val) {
  if (key === 'when' && /^\d{4}-\d{2}$/.test(val)) return monthName(val);
  const g = groups().find((x) => x[0] === key);
  const o = g && g[2].find((x) => x[0] === val);
  if (!o && key === 'account') { const r = rows.find((t) => t.account === val); return r ? shortAcct(r.account_name) : 'One card'; }
  return o ? o[1] : val;
}
function renderFilterChips() {
  const box = el('fchips'); box.replaceChildren();
  let n = 0;
  for (const k of ['show', 'sort', 'direction', 'size', 'when', 'category', 'account', 'origin']) {
    if (f[k] === DEF[k] || (k === 'show')) continue;
    n++;
    const b = h('button', 'mn-chip on', label(k, f[k])); b.type = 'button';
    b.setAttribute('aria-label', 'Remove ' + label(k, f[k]));
    b.onclick = () => { f[k] = DEF[k]; saveF(); renderFilterChips(); loadList().catch((e) => say(e.message, 'error')); };
    box.append(b);
  }
  if (f.show === 'all') {
    const b = h('button', 'mn-chip on', 'Everything'); b.type = 'button'; b.setAttribute('aria-label', 'Back to only to review');
    b.onclick = () => setF('show', 'todo'); box.prepend(b); n++;
  }
  if (n > 1) {
    const c = h('button', 'mn-chip mn-clear', 'Clear all'); c.type = 'button';
    c.onclick = () => { f = { ...DEF }; el('search').value = ''; saveF(); renderFilterChips(); loadList().catch((e) => say(e.message, 'error')); };
    box.append(c);
  }
}
function query() {
  const q = new URLSearchParams({ limit: 500 });
  if (f.show === 'todo') q.set('reviewed', 'false');
  if (f.sort !== 'newest') q.set('sort', f.sort);
  for (const k of ['direction', 'category', 'account', 'origin']) if (f[k]) q.set(k, f[k]);
  if (f.size) q.set('min_abs', f.size);
  const [from, to] = range(f.when);
  if (from) { q.set('from', from); q.set('to', to); }
  const s = el('search').value.trim(); if (s) q.set('search', s);
  return 'api/transactions?' + q;
}
let listSeq = 0;
async function loadList() {
  const seq = ++listSeq;
  await get(query(), (r, stale) => { if (seq === listSeq) paintList(r, stale); });
}
/* ---------- visual cues ---------- */
/* Colours are kit variables only (tests/no-hex.sh): neutral by default, semantic roles where they carry meaning. */
const POS = 'var(--pos)', WARN = 'var(--warn)';
function avatar(name) {
  const clean = String(name || '?').replace(/[^\p{L}\p{N} ]/gu, ' ').trim();
  const w = clean.split(/\s+/).filter(Boolean);
  const ini = ((w[0]?.[0] || '?') + (w.length > 1 ? w[1][0] : (w[0]?.[1] || ''))).toUpperCase();
  const a = h('span', 'mn-av', ini); a.setAttribute('aria-hidden', 'true');
  return a;
}
/* Card art: a neutral tile with the last four digits; the account name sits beside it (no brand colours). */
function card(t) {
  const txt = `${t.institution || ''} ${t.account_name || ''}`;
  const l4 = t.last_four || (String(t.account_name || '').match(/(\d{4})\)?\s*$/) || [])[1] || '';
  const chip = h('span', 'mn-card'); chip.title = txt.trim();
  chip.append(h('i'), h('b', '', l4 ? l4.slice(-4) : '••'));
  return chip;
}
const tidyCase = (x) => /[a-z]/.test(x) ? x : x.replace(/\b([A-Z])([A-Z]+)\b/g, (m, a, b) => (['USAA', 'CKG', 'ATM', 'IRA', 'HSA'].includes(m) ? m : a + b.toLowerCase())).replace(/\bCkg\b|\bCKG\b/g, 'Checking');
const shortAcct = (n) => tidyCase(String(n || '').replace(/\s*\((?:\.{0,3}|X+)\d{4,5}\)\s*$/i, '').replace(/\s*(?:X{2,}|\*{2})\d{4}\b/gi, '').replace(/®|TM\b|™/g, '').replace(/\s{2,}/g, ' ').trim());
/* Category icons: small drawn SVGs (always render; no emoji font needed) and a colour per category */
const IC = {
  warn: 'M12 3l10 18H2z M12 10v5 M12 18h.01', medal: 'M8 3l4 6 4-6 M12 21a6 6 0 1 0 0-12 6 6 0 0 0 0 12z', brief: 'M3 8h18v12H3z M8 8V5h8v3 M3 13h18',
  chart: 'M3 20h18 M6 16l4-5 3 3 5-7', gift: 'M3 10h18v4H3z M5 14v7h14v-7 M12 10v11 M12 10c-3 0-5-1-5-3s3-2 5 3c2-5 5-5 5-3s-2 3-5 3',
  bulb: 'M9 18h6 M10 21h4 M12 3a6 6 0 0 0-4 10.5V16h8v-2.5A6 6 0 0 0 12 3z', fuel: 'M4 21V5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v16 M3 21h12 M4 10h10 M14 8l4 3v7a1.5 1.5 0 0 0 3 0V9l-3-3',
  car: 'M3 16v-4l2-5h14l2 5v4z M3 16v2h3v-2 M18 16v2h3v-2 M7 13h.01 M17 13h.01', train: 'M6 3h12v13H6z M6 10h12 M8 20l-2 2 M16 20l2 2 M9 14h.01 M15 14h.01',
  taxi: 'M5 17h14v-5l-2-5H7l-2 5z M9 7V5h6v2 M7 17v2 M17 17v2', home: 'M3 11l9-8 9 8 M5 9v12h14V9 M10 21v-6h4v6', phone: 'M7 2h10v20H7z M11 18h2',
  bot: 'M5 8h14v11H5z M12 4v4 M9 13h.01 M15 13h.01 M9 16h6', repeat: 'M17 2l4 4-4 4 M3 12V9a3 3 0 0 1 3-3h15 M7 22l-4-4 4-4 M21 12v3a3 3 0 0 1-3 3H3',
  cart: 'M3 4h2l2.5 11h11L21 7H6 M9 20h.01 M18 20h.01', cup: 'M4 8h13v6a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5z M17 10h2a2 2 0 0 1 0 4h-2 M8 2v3 M12 2v3',
  fork: 'M7 2v8 M4 2v5a3 3 0 0 0 6 0V2 M7 10v12 M17 2c-2 2-3 5-3 8h3v12', bed: 'M3 18V7 M3 14h18v4 M21 14v-3a3 3 0 0 0-3-3h-7v6 M7 11h.01',
  plane: 'M2 13l20-8-6 16-4-7z M12 14l4-4', bag: 'M5 8h14l-1 13H6z M9 8a3 3 0 0 1 6 0', health: 'M12 21s-8-5-8-11a4 4 0 0 1 8-2 4 4 0 0 1 8 2c0 6-8 11-8 11z',
  film: 'M4 4h16v16H4z M8 4v16 M16 4v16 M4 9h4 M4 15h4 M16 9h4 M16 15h4', receipt: 'M5 3h14v18l-3-2-2 2-2-2-2 2-2-2-3 2z M9 8h6 M9 12h6',
  swap: 'M7 4L3 8l4 4 M3 8h14 M17 20l4-4-4-4 M21 16H7', card: 'M2 6h20v12H2z M2 10h20 M6 15h4', mega: 'M3 10v4h4l8 5V5L7 10z M18 9a4 4 0 0 1 0 6',
  bank: 'M3 10l9-6 9 6 M5 10v8 M10 10v8 M14 10v8 M19 10v8 M3 20h18', cap: 'M2 9l10-5 10 5-10 5z M6 11v5c3 2 9 2 12 0v-5', paw: 'M12 14c-3 0-5 3-5 5 0 1.5 2 2 5 2s5-.5 5-2c0-2-2-5-5-5z M6 10h.01 M9.5 6h.01 M14.5 6h.01 M18 10h.01',
  scale: 'M12 3v18 M5 21h14 M4 7h16 M7 7l-3 6a3 3 0 0 0 6 0z M17 7l-3 6a3 3 0 0 0 6 0z', back: 'M9 14l-5-5 5-5 M4 9h11a5 5 0 0 1 0 10h-3',
  shield: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z', scooter: 'M5 18h.01 M19 18h.01 M5 18h10l3-10h3 M14 8h4', tag: 'M3 3h8l10 10-8 8L3 11z M7.5 7.5h.01',
  heart: 'M12 21s-8-5-8-11a4 4 0 0 1 8-2 4 4 0 0 1 8 2c0 6-8 11-8 11z' };
const CAT_ICONS = [[/uncategor/i, 'warn', WARN], [/va |military/i, 'medal', null], [/income|salary|payroll|revenue/i, 'brief', POS],
  [/interest|dividend|invest/i, 'chart', POS], [/gift|charity|donat/i, 'gift', null], [/gas & elec|electric|utilit|water|garbage/i, 'bulb', null],
  [/^gas|fuel/i, 'fuel', null], [/parking|toll/i, 'car', null], [/auto|car\b|rental car/i, 'car', null], [/transit|train|bus/i, 'train', null],
  [/taxi|ride|grab|bolt|uber/i, 'taxi', null], [/scooter/i, 'scooter', null], [/mortgage|rent|home/i, 'home', null],
  [/internet|cable|phone/i, 'phone', null], [/ai usage/i, 'bot', null], [/subscri|software|saas/i, 'repeat', null], [/grocer/i, 'cart', null],
  [/coffee/i, 'cup', null], [/restaurant|dining|bar|lineman|food|lpth/i, 'fork', null], [/hotel|lodging/i, 'bed', null],
  [/travel|vacation|flight|air/i, 'plane', null], [/shop|tiktok|amazon|cloth/i, 'bag', null], [/health|medical|pharm|doctor|dental/i, 'health', null],
  [/fitness|gym|sport/i, 'heart', null], [/entertain|movie|music|game/i, 'film', null], [/fee|financial|bank charge/i, 'receipt', null],
  [/transfer/i, 'swap', null], [/card payment|credit card|payment/i, 'card', null], [/advertis|promot|marketing/i, 'mega', null],
  [/tax/i, 'bank', null], [/educat|course|book/i, 'cap', null], [/pet/i, 'paw', null], [/balance|adjust/i, 'scale', null],
  [/business|office|work|biz/i, 'brief', null], [/refund|credit/i, 'back', POS], [/insur/i, 'shield', null]];
function catIcon(c) {
  const m = CAT_ICONS.find(([re]) => re.test(c || 'Uncategorized'));
  const name = m ? m[1] : 'tag', col = m ? m[2] : null;
  const e = h('em', 'mn-ci'); if (col) e.style.setProperty('--ic', col);
  e.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${IC[name]}"/></svg>`;
  return e;
}

/* ---------- the review table ---------- */
const COLS = [
  ['date', 'Date', 'newest', 'oldest'], ['merchant', 'Merchant', 'merchant', 'merchant_desc'], ['account', 'Card', 'account', 'account_desc'],
  ['category', 'Category', 'category', 'category_desc'], ['amount', 'Amount', 'largest', 'smallest'], ['reviewed', '✓']];
function colActive(k) {
  return { date: !!f.when, merchant: !!el('search').value.trim(), account: !!f.account, category: !!f.category,
    amount: !!(f.direction || f.size), reviewed: f.show !== DEF.show }[k];
}
function header() {
  const hd = h('div', 'mn-th'); hd.setAttribute('role', 'row');
  for (const [k, title, asc, desc] of COLS) {
    const b = h('button', 'mn-thc c-' + k); b.type = 'button';
    b.append(h('span', '', title));
    const sorted = f.sort === asc || f.sort === desc;
    if (sorted) b.append(h('i', 'mn-arrow', (k === 'date' ? f.sort === 'newest' : k === 'amount' ? f.sort === 'largest' : f.sort === desc) ? '↓' : '↑'));
    if (colActive(k)) b.append(h('i', 'mn-dot'));
    b.classList.toggle('on', sorted || !!colActive(k));
    b.onclick = (e) => { e.stopPropagation(); openMenu(k, b); };
    hd.append(b);
  }
  return hd;
}
function row(t, maxAbs) {
  const li = h('li', 'mn-row' + (t.reviewed ? ' is-reviewed' : '') + (t.amount > 0 ? ' is-in' : ''));
  const open = () => openSheet(t);
  const dt = h('button', 'mn-td c-date'); dt.type = 'button'; dt.onclick = open;
  const d = t.date ? new Date(t.date + 'T12:00:00') : null;
  if (d) dt.append(h('b', '', String(d.getDate())), h('small', '', d.toLocaleDateString(undefined, { month: 'short' })));
  const mer = h('button', 'mn-td c-merchant'); mer.type = 'button'; mer.onclick = open;
  const nm = h('span', 'mn-mn'); nm.append(h('b', '', t.merchant || t.description));
  const sub = h('small', '');
  const mc = card(t); mc.classList.add('mn-mcard'); sub.append(mc, h('span', 'mn-msub', shortAcct(t.account_name)));
  if (t.origin === 'import') sub.append(h('span', 'mn-tag import', 'History'));
  if (t.status === 'pending') sub.append(h('span', 'mn-tag pend', 'Pending'));
  if (t.note) sub.append(h('span', 'mn-note', '✎ ' + t.note));
  nm.append(sub);
  mer.append(avatar(t.merchant || t.description), nm);
  const ac = h('button', 'mn-td c-account'); ac.type = 'button'; ac.title = t.account_name || '';
  ac.onclick = (e) => { e.stopPropagation(); f.account = f.account === t.account ? '' : t.account; saveF(); renderFilterChips(); loadList().catch((x) => say(x.message, 'error')); };
  ac.setAttribute('aria-label', `Card ${t.account_name || ''}. Show only this card`);
  ac.append(card(t), h('span', 'mn-acn', shortAcct(t.account_name)));
  const cat = h('button', 'mn-rowcat c-category' + (t.category ? '' : ' none')); cat.type = 'button';
  cat.setAttribute('aria-haspopup', 'listbox'); cat.setAttribute('aria-label', 'Category: ' + (t.category || 'Uncategorized') + '. Change');
  cat.append(catIcon(t.category), h('span', '', t.splits ? t.splits.map((x) => x.category + ' ' + money(Math.abs(x.amount))).join(' + ') : (t.category || 'Uncategorized')));
  cat.insertAdjacentHTML('beforeend', '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>');
  cat.onclick = (e) => { e.stopPropagation(); openPop(t, cat); };
  const am = h('button', 'mn-td c-amount'); am.type = 'button'; am.onclick = open;
  const bar = h('i', 'mn-abar'); bar.style.width = Math.max(4, Math.abs(t.amount || 0) / maxAbs * 100) + '%';
  am.append(h('b', 'mn-amt' + (t.amount > 0 ? ' pos' : ''), (t.amount > 0 ? '+' : '') + money(t.amount)), h('span', 'mn-abg', '')); am.lastChild.append(bar);
  const tick = h('button', 'mn-tick c-reviewed' + (t.reviewed ? ' on' : ''), '✓'); tick.type = 'button';
  tick.setAttribute('aria-label', t.reviewed ? 'Mark not reviewed' : 'Mark reviewed');
  tick.onclick = async () => {
    t.reviewed = !t.reviewed; li.classList.toggle('is-reviewed', t.reviewed); tick.classList.toggle('on', t.reviewed);
    try { await api('api/update', { source: t.source, id: t.id, reviewed: t.reviewed }); say(t.reviewed ? 'Reviewed.' : 'Marked to review.', 'note', true); refresh(); } catch (e) { say(e.message, 'error'); }
  };
  li.append(dt, mer, ac, cat, am, tick);
  return li;
}
function tile(label, value, cls, onclick) {
  const b = h(onclick ? 'button' : 'div', 'mn-tile ' + (cls || '')); if (onclick) { b.type = 'button'; b.onclick = onclick; }
  b.append(h('small', '', label), h('b', '', value)); return b;
}
function paintSummary(r) {
  const box = el('sumbar'); box.replaceChildren();
  const sm = r.summary || { out: 0, in: 0, uncategorized: 0 };
  box.append(tile(f.show === 'todo' ? 'To review' : 'Showing', r.total.toLocaleString(), 'count'),
    tile('Money out', money(sm.out), 'out', () => setF('direction', f.direction === 'out' ? '' : 'out')),
    tile('Money in', money(sm.in), 'in', () => setF('direction', f.direction === 'in' ? '' : 'in')),
    tile('Uncategorized', String(sm.uncategorized), sm.uncategorized ? 'warn' : 'ok', sm.uncategorized ? () => setF('category', f.category === 'Uncategorized' ? '' : 'Uncategorized') : null));
}
function paintList(r, stale) {
  rows = r.transactions;
  paintSummary(r);
  const box = el('list'); box.replaceChildren();
  const maxAbs = Math.max(1, ...rows.map((t) => Math.abs(t.amount || 0)));
  const tbl = h('div', 'mn-table'); tbl.setAttribute('role', 'table');
  tbl.append(header());
  const ul = h('ul', 'mn-rows'); for (const t of rows) ul.append(row(t, maxAbs)); tbl.append(ul);
  if (rows.length < r.total) tbl.append(h('p', 'mn-small mn-more', `Showing the first ${rows.length} of ${r.total}. Narrow it with a filter.`));
  box.append(tbl);
  const todo = f.show === 'todo';
  el('all-done').hidden = !(todo && rows.length > 1);
  el('all-done').textContent = `Mark these ${rows.length} reviewed`;
  el('empty').hidden = rows.length > 0 || stale;
  const filtered = ['sort', 'direction', 'size', 'when', 'category', 'account', 'origin'].some((k) => f[k] !== DEF[k]) || el('search').value.trim();
  el('empty').textContent = ov && ov.syncing ? 'Pulling your transactions…' : todo && !filtered ? 'All caught up. Nothing left to review.' : 'No transactions match.';
}

/* ---------- column menus: sort and filter per column ---------- */
/* Every pop-up is the shared sheet (wizard-sheet.js): bottom sheet when narrow, centred card when wide. */
const openDlg = (d) => (window.WzSheet ? WzSheet.open(d) : d.showModal());
const closeDlg = (d) => { if (d.open) d.close(); };
let menuKey = null, menuBtn = null;
function closeMenu() { closeDlg(el('menu')); }
el('menu').addEventListener('close', () => { menuKey = null; menuBtn = null; });
function mItem(text, on, fn, icon) {
  const b = h('button', 'mn-mi' + (on ? ' on' : '')); b.type = 'button';
  if (icon) b.append(icon); b.append(h('span', '', text));
  b.onclick = () => { fn(); if (menuKey) drawMenu(); }; return b;
}
function mSec(t) { return h('div', 'mn-mh', t); }
function drawMenu() {
  const m = el('menu-body'); m.replaceChildren();
  const col = COLS.find((c) => c[0] === menuKey); if (!col) return;
  const [k, title, asc, desc] = col;
  const sortTo = (v) => { f.sort = v; saveF(); renderFilterChips(); loadList().catch((e) => say(e.message, 'error')); };
  const setKey = (key, v) => { f[key] = v; saveF(); renderFilterChips(); loadList().catch((e) => say(e.message, 'error')); };
  const top = h('div', 'mn-mtop'); top.append(h('b', '', title === '✓' ? 'Reviewed' : title)); m.append(top);
  if (asc) {
    m.append(mSec('Sort'));
    const names = { date: ['Newest first', 'Oldest first'], amount: ['Biggest first', 'Smallest first'] }[k] || ['A → Z', 'Z → A'];
    const g = h('div', 'mn-mseg');
    g.append(mItem(names[0], f.sort === asc, () => sortTo(asc)), mItem(names[1], f.sort === desc, () => sortTo(desc)));
    m.append(g);
  }
  m.append(mSec('Filter'));
  const list = h('div', 'mn-mlist');
  if (k === 'date') {
    for (const [v, t] of groups().find((x) => x[0] === 'when')[2]) list.append(mItem(t, f.when === v, () => setKey('when', v)));
    if (/^\d{4}-\d{2}$/.test(f.when)) list.append(mItem(monthName(f.when), true, () => setKey('when', '')));
  } else if (k === 'merchant') {
    const inp = h('input', 'wz-input'); inp.type = 'search'; inp.placeholder = 'Merchant contains…'; inp.value = el('search').value;
    inp.oninput = () => { el('search').value = inp.value; el('search').oninput(); };
    list.append(inp); setTimeout(() => inp.focus({ preventScroll: true }), 30);
  } else if (k === 'account') {
    list.append(mItem('Every card', !f.account, () => setKey('account', '')));
    for (const a of ov?.accounts || []) list.append(mItem(shortAcct(a.name), f.account === a.id, () => setKey('account', a.id), card({ institution: a.institution, account_name: a.name, last_four: a.last_four })));
  } else if (k === 'category') {
    const inp = h('input', 'wz-input'); inp.type = 'search'; inp.placeholder = 'Find a category'; list.append(inp);
    const box = h('div', 'mn-mcats'); list.append(box);
    const draw = () => {
      box.replaceChildren(); const q = inp.value.trim().toLowerCase();
      if (!q) box.append(mItem('Any category', !f.category, () => setKey('category', '')), mItem('Uncategorized', f.category === 'Uncategorized', () => setKey('category', 'Uncategorized'), catIcon('Uncategorized')));
      for (const g of catGroups()) {
        const hits = g.categories.filter((c) => !q || c.toLowerCase().includes(q));
        if (hits.length) box.append(mSec(groupHead(g)));
        for (const c of hits) box.append(mItem(c, f.category === c, () => setKey('category', c), catIcon(c)));
      }
    };
    inp.oninput = draw; draw();
  } else if (k === 'amount') {
    const g = h('div', 'mn-mseg');
    for (const [v, t] of [['', 'In and out'], ['out', 'Money out'], ['in', 'Money in']]) g.append(mItem(t, f.direction === v, () => setKey('direction', v)));
    list.append(g, mSec('At least'));
    const g2 = h('div', 'mn-mseg');
    for (const [v, t] of SIZES) g2.append(mItem(t.replace('Any size', 'Any'), f.size === v, () => setKey('size', v)));
    list.append(g2);
  } else if (k === 'reviewed') {
    list.append(mItem('Only to review', f.show === 'todo', () => setKey('show', 'todo')), mItem('Everything', f.show === 'all', () => setKey('show', 'all')));
  }
  m.append(list);
}
function openMenu(k, btn) {
  if (menuKey === k) { closeMenu(); return; }
  closePop?.(); menuKey = k; menuBtn = btn;
  drawMenu(); openDlg(el('menu'));
}

function setF(key, v) { f[key] = v; saveF(); renderFilterChips(); loadList().catch((e) => say(e.message, 'error')); }
renderFilterChips();

function openSheet(t) {
  cur = t; pick = t.category;
  el('t-merchant').textContent = t.merchant || t.description;
  el('t-meta').textContent = [day(t.date), t.account_name, t.status === 'pending' ? 'pending' : null, 'from ' + t.origin_name].filter(Boolean).join(' · ') + (t.description && t.description !== t.merchant ? ' — ' + t.description : '');
  el('t-amount').textContent = money(t.amount); el('t-amount').classList.toggle('pos', t.amount > 0);
  el('t-name').value = t.merchant || ''; el('t-note').value = t.note || ''; el('t-rule').checked = false;
  el('t-rule-text').textContent = `Always use this for “${(t.merchant || t.description || '').slice(0, 30)}”`;
  el('t-catq').value = ''; closePicker(); showPick();
  el('t-hide').textContent = t.hidden ? 'Unhide' : 'Hide';
  el('t-reply').hidden = !canReply();
  /* Reply must open the draft inside the tap itself, so fetch this transaction's context now and keep it. */
  ctxCache = null;
  if (canReply()) api('api/transactions/context?' + new URLSearchParams({ source: t.source, id: t.id })).then((c) => { if (cur === t) ctxCache = c; }).catch(() => {});
  openDlg(el('sheet'));
}
const closeSheet = () => closeDlg(el('sheet'));
el('sheet').addEventListener('close', () => { cur = null; ctxCache = null; closePicker(); });

/* Reply: open a fresh, unsent Chats draft with this item attached (the shared WizardApp.nav.openContextDraft).
 * Money never sends anything; the owner writes and sends from Chats. Older shells don't have it: no Reply then. */
let ctxCache = null;
const canReply = () => typeof window.WizardApp?.nav?.openContextDraft === 'function';
function reply(ctx) {
  try {
    if (!ctx) throw new Error('Still loading this item. Try again in a second.');
    const receipt = window.WizardApp.nav.openContextDraft(ctx);
    if (receipt?.kind !== 'unsent-draft' || receipt.sent !== false) throw new Error('Could not confirm an unsent draft. Nothing was sent.');
    say('Draft opened in Chats with this item attached. Nothing sent.', 'note');
  } catch (e) {
    say(e.message === 'Context draft unavailable' ? 'Reload Wizard (refresh the whole page) to turn on Reply. Nothing sent.' : (e.message || 'Could not open Chats. Nothing sent.'), 'error');
  }
}
el('t-reply').onclick = () => reply(ctxCache);
/* An alert is already on screen in full, so its draft is built right here. */
function alertContext(a) {
  const slug = String(a.institution || a.kind || a.title || 'alert').replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80) || 'alert';
  const lines = [`Money alert: ${a.title}`, a.message || ''];
  if (a.institution) lines.push(`Bank: ${a.institution}`);
  if (a.as_of) lines.push(`Last update from the bank: ${a.as_of}` + (a.days != null ? ` (${a.days} days ago)` : ''));
  if (a.fix_url) lines.push(`Where he fixes it: ${a.fix_label || 'Fix it'} (${a.fix_url})`);
  return { id: `alert-${a.kind || 'x'}-${slug}`.slice(0, 128), title: `Money: ${a.title}`.slice(0, 1000), message: lines.filter(Boolean).join('\n').slice(0, 32000) };
}
/* Esc with the category list open folds the list first, as before */
el('sheet').addEventListener('cancel', (e) => { if (!el('t-picker').hidden) { e.preventDefault(); closePicker(); } });

/* category picker */
const RKEY = 'money.recentcats.v1';
const recent = () => { try { return JSON.parse(localStorage.getItem(RKEY) || '[]'); } catch (e) { return []; } };
function remember_cat(c) { try { localStorage.setItem(RKEY, JSON.stringify([c, ...recent().filter((x) => x !== c)].slice(0, 6))); } catch (e) {} }
function showPick() { el('t-catname').textContent = pick || 'Uncategorized'; el('t-catname').style.color = pick ? '' : 'var(--ink-3)'; }
function closePicker() { el('t-picker').hidden = true; el('t-catbtn').setAttribute('aria-expanded', 'false'); }
function openPicker() {
  el('t-picker').hidden = false; el('t-catbtn').setAttribute('aria-expanded', 'true');
  drawCats(); el('t-catq').focus({ preventScroll: true });
  el('t-picker').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
function choose(c) { pick = c; showPick(); closePicker(); }
/* category headings (Food & Dining, Transfers...) in his order; transfers and income say how they count */
const KIND_NOTE = { transfer: 'not spending', income: 'income' };
function catGroups() { return ov?.category_groups || [{ group: 'Categories', kind: 'expense', categories: ov?.categories || [] }]; }
function groupHead(g) { const n = KIND_NOTE[g.kind]; return g.group + (n && !g.group.toLowerCase().includes(n) ? ' · ' + n : ''); }
function catList(ul, q, current, onChoose) {
  ul.replaceChildren();
  q = q.trim(); const ql = q.toLowerCase();
  const key = (c) => c.replace(/^[^\p{L}\p{N}]+/u, '');
  const all = [...(ov?.categories || [])].sort((a, b) => key(a).localeCompare(key(b), undefined, { sensitivity: 'base' }));
  const item = (c, cls = '') => {
    const li = h('li'); const b = h('button', cls + (c === current ? ' on' : '')); b.type = 'button'; b.setAttribute('role', 'option');
    b.append(catIcon(c), h('span', 'mn-cn', c));
    b.onclick = () => onChoose(c); li.append(b); ul.append(li);
  };
  if (q) {
    const hits = all.filter((c) => c.toLowerCase().includes(ql)).sort((a, b) => a.toLowerCase().indexOf(ql) - b.toLowerCase().indexOf(ql));
    if (!all.some((c) => c.toLowerCase() === ql)) {
      const li = h('li'); const b = h('button', 'mn-new', `+ Create and use “${q.slice(0, 60)}”`); b.type = 'button';
      b.onclick = () => onChoose(q.slice(0, 60)); li.append(b); ul.append(li);
    }
    hits.forEach((c) => item(c));
    return;
  }
  const rec = recent().filter((c) => all.includes(c));
  if (current && !rec.includes(current)) rec.unshift(current);
  if (rec.length) { ul.append(h('li', 'mn-cath', 'Recent')); rec.forEach((c) => item(c)); }
  for (const g of catGroups()) { ul.append(h('li', 'mn-cath', groupHead(g))); g.categories.forEach((c) => item(c)); }
}
function drawCats() { catList(el('t-cats'), el('t-catq').value, pick, choose); }

/* the same dropdown, straight from a review row: one tap opens it, choosing saves */
let popT = null, popBtn = null;
function openPop(t, anchor) {
  if (popT === t) { closePop(); return; }
  closePop();
  popT = t; popBtn = anchor; anchor.setAttribute('aria-expanded', 'true');
  el('pop-q').value = '';
  drawPop(); openDlg(el('catpop'));
  if (matchMedia('(hover: hover)').matches) el('pop-q').focus({ preventScroll: true });
  el('pop-list').scrollTop = 0;
}
function drawPop() { if (popT) catList(el('pop-list'), el('pop-q').value, popT.category, setRowCat); }
function closePop() { closeDlg(el('catpop')); }
el('catpop').addEventListener('close', () => { popBtn?.setAttribute('aria-expanded', 'false'); popT = null; popBtn = null; });
async function setRowCat(c) {
  const t = popT, b = popBtn; closePop();
  if (!t || c === t.category) return;
  const before = t.category; t.category = c; remember_cat(c);
  if (b) { b.querySelector('span').textContent = c; b.querySelector('.mn-ci').replaceWith(catIcon(c)); b.classList.remove('none'); }
  try { await api('api/update', { source: t.source, id: t.id, category: c }); say(`Set to ${c}.`, 'note', true); refresh(); }
  catch (e) { t.category = before; if (b) b.querySelector('span').textContent = before || 'Uncategorized'; say(e.message, 'error'); }
}
el('pop-q').oninput = drawPop;
el('pop-q').onkeydown = (e) => {
  if (e.key === 'Enter') { e.preventDefault(); el('pop-list').querySelector('button')?.click(); }
};
el('t-catbtn').onclick = () => (el('t-picker').hidden ? openPicker() : closePicker());
el('t-catq').oninput = drawCats;
el('t-catq').onkeydown = (e) => {
  if (e.key === 'Enter') { e.preventDefault(); el('t-cats').querySelector('button')?.click(); }
};
el('t-close').onclick = closeSheet;
el('t-save').onclick = async () => {
  const t = cur; if (!t) return;
  const body = { source: t.source, id: t.id, reviewed: true, note: el('t-note').value };
  const name = el('t-name').value.trim();
  if (name && name !== t.merchant) body.merchant = name;
  if (pick && pick !== t.category) body.category = pick;
  if (pick) remember_cat(pick);
  if (el('t-rule').checked && pick) { body.category = pick; body.make_rule = true; body.match = (t.merchant || t.description || '').slice(0, 40); }
  try { await api('api/update', body); closeSheet(); say(body.make_rule ? 'Saved, and future ones will match.' : 'Saved.', 'note', true); refresh(); } catch (e) { say(e.message, 'error'); }
};
el('t-hide').onclick = async () => {
  const t = cur; if (!t) return;
  try { await api('api/update', { source: t.source, id: t.id, hidden: !t.hidden, reviewed: true }); closeSheet(); say(t.hidden ? 'Unhidden.' : 'Hidden from lists and totals.', 'note', true); refresh(); } catch (e) { say(e.message, 'error'); }
};
el('all-done').onclick = async () => {
  if (!rows.length) return;
  const by = {}; for (const t of rows) (by[t.source] ||= []).push(t.id);
  try {
    for (const [src, ids] of Object.entries(by)) await api('api/update', { source: src, ids, reviewed: true });
    say(`${rows.length} marked reviewed.`, 'note', true); refresh();
  } catch (e) { say(e.message, 'error'); }
};
/* A yes/no question in the shared sheet (replaces the browser's confirm box). Resolves true on OK only. */
function ask(text, ok) {
  return new Promise((done) => {
    const d = el('ask'); let yes = false;
    el('ask-text').textContent = text; el('ask-ok').textContent = ok;
    el('ask-ok').onclick = () => { yes = true; closeDlg(d); };
    el('ask-no').onclick = () => closeDlg(d);
    d.addEventListener('close', () => done(yes), { once: true });
    openDlg(d);
  });
}
async function doUndo() { try { await api('api/undo', {}); say('Undone.', 'note'); refresh(); } catch (e) { say(e.message, 'error'); } }

/* ---------- spending ---------- */
const prevMonth = (m) => { const d = new Date(m + '-15'); d.setMonth(d.getMonth() - 1); return d.toISOString().slice(0, 7); };
async function loadSpending() {
  const m = month;
  await Promise.all([get('api/spending?month=' + m, (s) => paintSpending(s)), get('api/spending?month=' + prevMonth(m), () => { if (cache['api/spending?month=' + m]) paintSpending(cache['api/spending?month=' + m]); })]);
}
function donut(parts, total, center) {
  const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 120 120'); svg.setAttribute('class', 'mn-donut'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Spending by category');
  const R = 48, C = 2 * Math.PI * R; let off = 0;
  const bg = document.createElementNS(NS, 'circle'); bg.setAttribute('cx', 60); bg.setAttribute('cy', 60); bg.setAttribute('r', R); bg.setAttribute('class', 'mn-donut-bg'); svg.append(bg);
  for (const [v, col] of parts) {
    const len = Math.max(0, v / total * C); if (len < 0.4) continue;
    const c = document.createElementNS(NS, 'circle'); c.setAttribute('cx', 60); c.setAttribute('cy', 60); c.setAttribute('r', R);
    c.style.stroke = col; c.setAttribute('stroke-dasharray', `${Math.max(0, len - 1.2)} ${C}`); c.setAttribute('stroke-dashoffset', -off);
    c.setAttribute('class', 'mn-donut-seg'); svg.append(c); off += len;
  }
  const wrap = h('div', 'mn-donut-w'); wrap.append(svg);
  const mid = h('div', 'mn-donut-c'); mid.append(h('small', '', center[0]), h('b', '', center[1])); wrap.append(mid);
  return wrap;
}
function paintSpending(s) {
  if (s.month !== month) return;
  const now = new Date().toISOString().slice(0, 7);
  el('month-label').textContent = new Date(month + '-15').toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  el('next').disabled = month >= now;
  const prev = cache['api/spending?month=' + prevMonth(month)];
  const [y, m] = month.split('-').map(Number);
  const days = month === now ? new Date().getDate() : new Date(y, m, 0).getDate();
  const delta = prev && prev.spent ? (s.spent - prev.spent) / prev.spent * 100 : null;
  const tiles = el('sp-tiles'); tiles.replaceChildren(
    tile('Spent', money(s.spent), 'out'),
    tile('vs ' + new Date(prevMonth(month) + '-15').toLocaleDateString(undefined, { month: 'short' }), delta == null ? '—' : `${delta > 0 ? '▲' : '▼'} ${Math.abs(delta).toFixed(0)}%`, delta == null ? '' : delta > 0 ? 'warn' : 'ok'),
    tile('Income', money(s.income), 'in'),
    tile('Per day', money(s.spent / Math.max(1, days)), ''));
  const cats = s.categories.filter((c) => c.amount > 0), total = cats.reduce((x, c) => x + c.amount, 0) || 1;
  const top = cats.slice(0, 8), rest = cats.slice(8).reduce((x, c) => x + c.amount, 0);
  /* Top categories: one accent ramp (accent, accent toward ink, accent tints, inks) so every skin keeps the slices apart. */
  const mix = (p, w) => `color-mix(in srgb, var(--accent) ${p}%, ${w})`;
  const RANK = ['var(--accent)', mix(55, 'var(--ink)'), 'var(--ink-2)', mix(58, 'transparent'), mix(25, 'var(--ink)'), 'var(--ink-3)', mix(32, 'transparent'), 'color-mix(in srgb, var(--ink) 30%, transparent)'];
  const REST = 'var(--line-2)';
  const colOf = {}; top.forEach((c, i) => { colOf[c.category] = RANK[i]; });
  const parts = top.map((c) => [c.amount, colOf[c.category]]); if (rest > 0) parts.push([rest, REST]);
  const chart = el('sp-chart'); chart.replaceChildren();
  if (cats.length) {
    chart.append(donut(parts, total, ['Spent', money(s.spent)]));
    const leg = h('ul', 'mn-legend');
    for (const c of top) { const li = h('li'); const dot = h('i'); dot.style.background = colOf[c.category]; li.append(dot, h('span', '', c.category), h('b', '', Math.round(c.amount / total * 100) + '%')); leg.append(li); }
    if (rest > 0) { const li = h('li'); const dot = h('i'); dot.style.background = REST; li.append(dot, h('span', '', `${cats.length - 8} more`), h('b', '', Math.round(rest / total * 100) + '%')); leg.append(li); }
    chart.append(leg);
  }
  const prevBy = Object.fromEntries((prev?.categories || []).map((c) => [c.category, c.amount]));
  const ul = el('cats'); ul.replaceChildren();
  const gTot = Object.fromEntries((s.groups || []).map((g) => [g.group, g.amount]));
  const order = (s.groups || []).map((g) => g.group);
  const sorted = s.groups ? [...s.categories].sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group)) : s.categories;
  let lastG = null;
  for (const c of sorted) {
    if (s.groups && c.group !== lastG) { lastG = c.group; const gh = h('li', 'mn-gh'); gh.append(h('span', '', c.group), h('b', '', money(gTot[c.group]))); ul.append(gh); }
    const li = h('li', 'mn-cat'); li.tabIndex = 0; li.setAttribute('role', 'button');
    const pct = c.amount > 0 ? c.amount / total * 100 : 0, maxAmt = cats[0]?.amount || 1;
    const body = h('div', 'mn-cat-b');
    const t = h('div', 'mn-cat-t'); t.append(h('span', 'mn-cat-n', c.category));
    const was = prevBy[c.category]; const tr = h('span', 'mn-trendc');
    if (was && c.amount > 0) { const d = (c.amount - was) / was * 100; if (Math.abs(d) >= 10) tr.append(h('i', 'mn-trend ' + (d > 0 ? 'up' : 'down'), `${d > 0 ? '▲' : '▼'} ${d > 999 ? '999+' : Math.abs(d).toFixed(0)}%`)); }
    else if (!was && c.amount > 0 && prev) tr.append(h('i', 'mn-trend new', 'New'));
    t.append(tr, h('small', 'mn-pct', c.amount > 0 ? pct.toFixed(pct < 1 ? 1 : 0) + '%' : 'refund'), h('b', 'mn-camt' + (c.amount < 0 ? ' pos' : ''), money(c.amount)));
    const bar = h('div', 'mn-bar-bg'); const fill = h('div', 'mn-bar-fill'); fill.style.width = Math.max(c.amount > 0 ? 1.5 : 0, c.amount / maxAmt * 100) + '%';  bar.append(fill);
    body.append(t, bar);
    const ic = catIcon(c.category); const cc = colOf[c.category] || 'var(--ink-3)'; ic.style.setProperty('--ic', cc); fill.style.background = cc;
    li.append(ic, body, h('span', 'mn-chev', '›'));
    const go = () => { tab = 'review'; f = { ...DEF, show: 'all', sort: 'largest', category: c.category, when: month }; saveF(); el('search').value = ''; showTab(); renderFilterChips(); loadList().catch((e) => say(e.message, 'error')); };
    li.onclick = go; li.onkeydown = (e) => { if (e.key === 'Enter') go(); };
    ul.append(li);
  }
  if (!s.categories.length) ul.append(h('li', 'mn-empty', 'No spending this month yet.'));
}

/* ---------- net worth ---------- */
let nwRange = 'ytd', nwData = null;
const TAG = { bank: 'bank', manual: 'manual', import: 'import' };
async function loadNetWorth() { await get('api/networth', paintNetWorth); }
function acctIcon(a) {
  if (a.origin === 'manual') { const e = h('span', 'mn-av mn-btc', a.detail && /BTC/.test(a.detail) ? '₿' : '$'); if (!(a.detail && /BTC/.test(a.detail))) e.style.setProperty('--c', POS); return e; }
  return card({ institution: a.institution, account_name: a.name, last_four: a.last_four });
}
function paintNetWorth(data) {
  nwData = data;
  el('nw-total').textContent = money(nwData.net_worth);
  el('nw-total').classList.toggle('neg', nwData.net_worth < 0);
  el('nw-assets').textContent = money(nwData.assets);
  el('nw-debts').textContent = money(nwData.liabilities);
  const tot = Math.abs(nwData.assets) + Math.abs(nwData.liabilities) || 1;
  el('nw-split').style.setProperty('--a', (nwData.assets / tot * 100) + '%');
  drawChart();
  const box = el('nw-list'); box.replaceChildren();
  for (const [title, list, cls] of [['Assets', nwData.accounts.filter((a) => a.balance > 0), 'in'], ['Owed', nwData.accounts.filter((a) => a.balance < 0), 'out']]) {
    if (!list.length) continue;
    const sum = list.reduce((x, a) => x + a.balance, 0), max = Math.max(...list.map((a) => Math.abs(a.balance)));
    const col = h('div', 'mn-nwcol');
    const hd = h('div', 'mn-nwh ' + cls); hd.append(h('b', '', title), h('small', '', `${list.length} account${list.length === 1 ? '' : 's'}`), h('span', 'mn-amt' + (cls === 'in' ? ' pos' : ' neg'), money(sum)));
    const ul = h('ul', 'mn-accts mn-card-list');
    for (const a of list) {
      const li = h('li'); const n = h('span', 'mn-row-n');
      const sub = h('small', ''); sub.append(h('span', 'mn-tag ' + TAG[a.origin], a.origin === 'import' ? 'History' : a.origin_name), ' ', [a.detail, a.institution !== 'Manual' ? a.institution : null, a.origin === 'import' && a.as_of ? 'as of ' + day(String(a.as_of).slice(0, 10)) : null].filter(Boolean).join(' · '));
      n.append(h('b', '', shortAcct(a.name)), sub);
      const right = h('span', 'mn-nwamt'); const bar = h('i'); bar.style.width = Math.max(3, Math.abs(a.balance) / max * 100) + '%';
      const bg = h('span', 'mn-abg ' + cls); bg.append(bar);
      right.append(h('b', 'mn-amt' + (a.balance > 0 ? ' pos' : ' neg'), money(a.balance)), bg);
      li.append(acctIcon(a), n, right); ul.append(li);
    }
    col.append(hd, ul); box.append(col);
  }
}
function drawChart() {
  const box = el('nw-chart'); box.replaceChildren();
  let pts = (nwData?.history || []).map((p) => ({ d: p.date, v: p.value }));
  const today = new Date(); const cut = nwRange === 'ytd' ? `${today.getFullYear()}-01-01` : nwRange === '90' ? new Date(today - 90 * 864e5).toISOString().slice(0, 10) : '0000';
  pts = pts.filter((p) => p.d >= cut);
  const ch = el('nw-change'); ch.replaceChildren();
  if (pts.length < 2) { box.append(h('p', 'mn-small', 'The chart fills in as days go by.')); return; }
  const diff = pts[pts.length - 1].v - pts[0].v, pc = pts[0].v ? diff / Math.abs(pts[0].v) * 100 : 0;
  ch.className = 'mn-change ' + (diff >= 0 ? 'up' : 'down');
  ch.append(`${diff >= 0 ? '▲' : '▼'} ${money(Math.abs(diff))}`, h('small', '', pts[0].v > 0 ? ` ${pc >= 0 ? '+' : '−'}${Math.abs(pc).toFixed(1)}% since ${day(pts[0].d)}` : ` since ${day(pts[0].d)}`));
  const W = 600, H = 170, P = 4, t0 = Date.parse(pts[0].d), t1 = Date.parse(pts[pts.length - 1].d) || t0 + 1;
  let lo = Math.min(...pts.map((p) => p.v)), hi = Math.max(...pts.map((p) => p.v)); const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.1 || 1; lo -= pad; hi += pad;
  const x = (d) => P + (Date.parse(d) - t0) / Math.max(1, t1 - t0) * (W - 2 * P);
  const y = (v) => P + (hi - v) / Math.max(1, hi - lo) * (H - 2 * P);
  const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('preserveAspectRatio', 'none'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Net worth over time');
  svg.innerHTML = '<defs><linearGradient id="nwg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="currentColor" stop-opacity=".35"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs>';
  const grid = h('div', 'mn-yax');
  for (const g of [0.08, 0.5, 0.92]) {
    const v = hi - (hi - lo) * g, gl = document.createElementNS(NS, 'line');
    gl.setAttribute('x1', 0); gl.setAttribute('x2', W); gl.setAttribute('y1', y(v)); gl.setAttribute('y2', y(v)); gl.setAttribute('class', 'mn-zero'); svg.append(gl);
    const lab = h('span', '', new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1 }).format(v)); lab.style.top = (y(v) / H * 100) + '%'; grid.append(lab);
  }
  const line = pts.map((p) => `${x(p.d).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ');
  const area = document.createElementNS(NS, 'polygon'); area.setAttribute('points', `${x(pts[0].d)},${H} ${line} ${x(pts[pts.length - 1].d)},${H}`); area.setAttribute('fill', 'url(#nwg)'); area.setAttribute('class', 'mn-area');
  const pl = document.createElementNS(NS, 'polyline'); pl.setAttribute('points', line); pl.setAttribute('class', 'mn-line');
  svg.append(area, pl);
  const wrap = h('div', 'mn-plot'); wrap.append(svg, grid);
  const last = pts[pts.length - 1], dot = h('i', 'mn-lastdot'); dot.style.left = (x(last.d) / W * 100) + '%'; dot.style.top = (y(last.v) / H * 100) + '%'; wrap.append(dot);
  box.append(wrap);
  const lab = h('div', 'mn-chart-x'); const mid = pts[Math.floor(pts.length / 2)];
  lab.append(h('span', '', day(pts[0].d)), h('span', '', day(mid.d)), h('span', '', day(last.d))); box.append(lab);
}
el('nw-range').querySelectorAll('.mn-chip').forEach((b) => b.onclick = () => { nwRange = b.dataset.range; el('nw-range').querySelectorAll('.mn-chip').forEach((x) => x.classList.toggle('on', x === b)); drawChart(); });

/* ---------- attention: never fail silently ---------- */
function renderAlerts() {
  const box = el('alerts'), list = ov.attention || [];
  box.replaceChildren(); box.hidden = !list.length;
  for (const a of list) {
    const row = h('div', 'mn-alert');
    const ic = h('em', 'mn-ci mn-alert-ic'); ic.style.setProperty('--ic', WARN);
    ic.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${IC.warn}"/></svg>`;
    const t = h('div', 'mn-alert-t'); t.append(h('b', '', a.title), h('p', '', a.message));
    row.append(ic, t);
    if (a.fix_url) {
      const go = h('a', 'mn-btn mn-sm mn-go', a.fix_label || 'Fix it'); go.href = a.fix_url;
      if (/^https?:/.test(a.fix_url)) { go.target = '_blank'; go.rel = 'noopener noreferrer'; go.textContent += ' ↗'; }
      row.append(go);
    }
    if (canReply()) {
      const rb = h('button', 'mn-btn mn-sm mn-alert-reply', 'Reply'); rb.type = 'button';
      rb.onclick = () => reply(alertContext(a));
      row.append(rb);
    }
    box.append(row);
  }
}
const behind = (inst) => (ov.attention || []).find((a) => a.institution === inst);

/* ---------- accounts ---------- */
function renderAccounts() {
  const box = el('accts'); box.replaceChildren();
  const by = {};
  for (const a of ov.accounts) (by[a.institution || 'Other'] ||= []).push(a);
  const groups = Object.entries(by).sort((x, y) => Math.abs(y[1].reduce((s, a) => s + (a.balance || 0), 0)) - Math.abs(x[1].reduce((s, a) => s + (a.balance || 0), 0)));
  for (const [inst, list] of groups) {
    const sum = list.reduce((s, a) => s + (a.balance || 0), 0);
    const asof = list.map((a) => a.balance_at).filter(Boolean).sort().pop();
    const issue = behind(inst);
    const card_ = h('div', 'mn-bank' + (issue ? ' mn-stale' : ''));
    const hd = h('div', 'mn-bank-h'); const logo = card({ institution: inst, account_name: inst }); logo.classList.add('mn-logo'); logo.querySelector('b').textContent = '';
    const ht = h('span', 'mn-row-n'); ht.append(h('b', '', inst), h('small', '', `${list.length} account${list.length === 1 ? '' : 's'}${asof ? ' · as of ' + day(String(asof).slice(0, 10)) : ''}`));
    if (issue) ht.append(h('span', 'mn-tag pend', issue.kind === 'behind' ? `Out of date · last update ${day(issue.as_of)}` : 'Needs reconnecting'));
    hd.append(logo, ht, h('span', 'mn-amt' + (sum > 0.004 ? ' pos' : sum < -0.004 ? ' neg' : ' zero'), money(sum)));
    card_.append(hd);
    const ul = h('ul', 'mn-accts');
    for (const a of [...list].sort((x, y) => Math.abs(y.balance || 0) - Math.abs(x.balance || 0))) {
      const li = h('li', 'mn-tap'); li.tabIndex = 0; li.title = 'Leave this account out of Money';
      li.onclick = async () => {
        if (!(await ask(`Leave ${a.name} out of Money? It disappears from transactions, spending and net worth. You can bring it back at the bottom of this tab.`, 'Leave it out'))) return;
        try { ov = await api('api/accounts/hide', { account: a.id, hidden: true }); say(`${a.name} hidden.`, 'note'); refresh(); } catch (e) { say(e.message, 'error'); }
      };
      // A visible control for the same action (never tap-the-row only)
      const off = h('button', 'mn-off'); off.type = 'button'; off.setAttribute('aria-label', `Leave ${a.name} out of Money`); off.title = 'Leave out of Money';
      off.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18 M10.6 5.1A10 10 0 0 1 12 5c5 0 9 4.5 10 7a13 13 0 0 1-3.2 4.3 M6.6 6.6A13 13 0 0 0 2 12c1 2.5 5 7 10 7a10 10 0 0 0 4.4-1 M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';
      off.onclick = (e) => { e.stopPropagation(); li.onclick(); };
      const b = a.balance || 0;
      const n = h('span', 'mn-row-n'); n.append(h('b', '', shortAcct(a.name)), h('small', '', [a.last_four ? '•• ' + a.last_four : null, /credit|card|sapphire|freedom|bonvoy|aspire|platinum/i.test((a.type || '') + ' ' + a.name) ? 'Card' : /saving/i.test(a.name) ? 'Savings' : /check|ckg/i.test(a.name) ? 'Checking' : /crypto/i.test(a.name) ? 'Crypto' : /invest|brokerage|ira|401/i.test(a.name) ? 'Investing' : 'Account'].filter(Boolean).join(' · ')));
      li.append(card({ institution: a.institution, account_name: a.name, last_four: a.last_four }), n, h('span', 'mn-amt' + (b > 0.004 ? ' pos' : b < -0.004 ? ' neg' : ' zero'), money(a.balance, a.currency)), off); ul.append(li);
    }
    card_.append(ul); box.append(card_);
  }
  if (!ov.accounts.length) box.append(h('p', 'mn-empty', ov.syncing ? 'Pulling your accounts…' : 'No accounts yet.'));
  el('sec-bank').textContent = ov.source_name;
  const mb = el('manual'); mb.replaceChildren();
  if (ov.manual.length) {
    const card = h('div', 'mn-bank'); const ul = h('ul', 'mn-accts');
    for (const m of ov.manual) {
      const li = h('li', 'mn-tap'); li.tabIndex = 0; li.onclick = () => openManual(m);
      const n = h('span', 'mn-row-n');
      const sub = m.kind === 'btc' ? `${(+m.amount).toLocaleString(undefined, { maximumFractionDigits: 8 })} BTC` + (m.btc_price ? ` at ${money(m.btc_price)}${m.price_stale ? ' (last known)' : ''}` : ' · price unavailable') : 'Dollars';
      n.append(h('b', '', m.name), h('small', '', sub + ' · updated ' + day(String(m.updated).slice(0, 10))));
      li.append(n, h('span', 'mn-amt pos', m.balance == null ? '—' : money(m.balance))); ul.append(li);
    }
    card.append(ul); mb.append(card);
  } else mb.append(h('p', 'mn-small mn-pad', 'For things no bank connection can see, like a cold-storage Bitcoin wallet.'));
  const hs = el('hist-sec'); hs.replaceChildren();
  if (ov.hidden_accounts?.length) {
    hs.append(h('h3', 'mn-sec', 'Left out of Money'));
    const card = h('div', 'mn-bank'); const ul = h('ul', 'mn-accts');
    for (const a of ov.hidden_accounts) {
      const li = h('li'); const n = h('span', 'mn-row-n'); n.append(h('b', '', a.name), h('small', '', a.institution || ''));
      const btn = h('button', 'mn-btn mn-sm', 'Bring back'); btn.type = 'button';
      btn.onclick = async () => { try { ov = await api('api/accounts/hide', { account: a.id, hidden: false }); say(`${a.name} is back.`, 'note'); refresh(); } catch (e) { say(e.message, 'error'); } };
      li.append(n, btn); ul.append(li);
    }
    card.append(ul); hs.append(card);
  }
  if (ov.history) {
    const hh = h('h3', 'mn-sec', ''); hs.append(hh);
    hh.append(h('span', 'mn-tag import', 'History'), ` Imported from ${ov.history.from_name}`);
    hs.append(h('p', 'mn-small mn-pad', `${ov.history.showing.toLocaleString()} past transactions from ${day(ov.history.from)}, kept for reference. Each account switches to ${ov.source_name} where its own data begins, so nothing shows twice.`));
  }
  const src = el('sources'); src.replaceChildren();
  for (const s of ov.sources) {
    const row = h('label', 'mn-src' + (s.installed ? '' : ' off'));
    const r = h('input'); r.type = 'radio'; r.name = 'src'; r.value = s.id; r.checked = s.id === ov.source; r.disabled = !s.installed;
    r.onchange = async () => { try { ov = await api('api/source', { source: s.id }); say(`Now reading ${s.name}.`, 'note'); refresh(); } catch (e) { say(e.message, 'error'); } };
    const t = h('span'); t.append(h('b', '', s.name), h('small', '', !s.installed ? 'Not installed' : s.connected ? `${s.banks} bank${s.banks === 1 ? '' : 's'} connected` : 'Installed, not connected yet'));
    row.append(r, t); src.append(row);
  }
  el('sync-note').textContent = ov.syncing ? 'Refreshing…' : `Last refreshed ${when(ov.last_sync)}${ov.error ? ' · ' + ov.error : ''}`;
  const rl = el('rules'); rl.replaceChildren();
  for (const r of ov.rules) {
    const li = h('li'); li.append(h('span', '', `“${r.match}” → ${r.category}`));
    const x = h('button', 'mn-btn mn-sm', 'Delete'); x.type = 'button';
    x.onclick = async () => { try { await api(`api/rules/${r.id}/delete`, {}); refresh(); } catch (e) { say(e.message, 'error'); } };
    li.append(x); rl.append(li);
  }
  if (!ov.rules.length) rl.append(h('li', 'mn-small', 'No rules yet. Tick “Always use this” when you categorize something.'));
}
/* ---------- manual accounts ---------- */
let mcur = null, mkind = 'btc';
function setKind(k) {
  mkind = k; el('m-kind').querySelectorAll('.mn-chip').forEach((b) => b.classList.toggle('on', b.dataset.kind === k));
  el('m-amount-lab').textContent = k === 'btc' ? 'Amount in BTC' : 'Balance in dollars';
  el('m-amount').placeholder = k === 'btc' ? '0.25' : '1000';
  el('m-hint').hidden = k !== 'btc';
}
function openManual(m) {
  mcur = m || null;
  el('m-title').textContent = m ? m.name : 'New manual account';
  el('m-name').value = m ? m.name : ''; el('m-amount').value = m ? m.amount : ''; el('m-note').value = m ? (m.note || '') : '';
  setKind(m ? m.kind : 'btc'); el('m-del').hidden = !m; openDlg(el('msheet'));
}
const closeManual = () => closeDlg(el('msheet'));
el('msheet').addEventListener('close', () => { mcur = null; });
el('m-kind').querySelectorAll('.mn-chip').forEach((b) => b.onclick = () => setKind(b.dataset.kind));
el('add-manual').onclick = () => openManual(null);
el('m-close').onclick = closeManual;
el('m-save').onclick = async () => {
  try {
    await api('api/manual', { id: mcur?.id, name: el('m-name').value, kind: mkind, amount: el('m-amount').value.replace(/,/g, ''), note: el('m-note').value });
    closeManual(); say('Saved.', 'note'); refresh();
  } catch (e) { say(e.message, 'error'); }
};
el('m-del').onclick = async () => {
  if (!mcur || !(await ask(`Delete ${mcur.name}?`, 'Delete'))) return;
  try { await api(`api/manual/${mcur.id}/delete`, {}); closeManual(); say('Deleted.', 'note'); refresh(); } catch (e) { say(e.message, 'error'); }
};

el('sync').onclick = async () => { try { ov = await api('api/sync', {}); say('Refreshing…', 'note'); refresh(); } catch (e) { say(e.message, 'error'); } };
el('setup-refresh').onclick = () => refresh();

/* ---------- shell ---------- */
function showTab() {
  document.querySelectorAll('[data-tab]').forEach((b) => b.classList.toggle('is-on', b.dataset.tab === tab));
  document.querySelectorAll('[data-panel]').forEach((p) => p.hidden = p.dataset.panel !== tab);
}
document.querySelectorAll('[data-tab]').forEach((b) => b.onclick = () => { tab = b.dataset.tab; showTab(); refresh(); });
el('prev').onclick = () => { const d = new Date(month + '-15'); d.setMonth(d.getMonth() - 1); month = d.toISOString().slice(0, 7); loadSpending().catch((e) => say(e.message, 'error')); };
el('next').onclick = () => { const d = new Date(month + '-15'); d.setMonth(d.getMonth() + 1); month = d.toISOString().slice(0, 7); loadSpending().catch((e) => say(e.message, 'error')); };
const LOAD = { review: () => loadList(), spending: () => loadSpending(), networth: () => loadNetWorth(), accounts: () => null };
let warmed = false;
async function refresh() {
  try {
    await Promise.all([loadOverview(), LOAD[tab]()]);
    if (!warmed) {   // fetch the other tabs in the background so they open instantly
      warmed = true;
      for (const k of Object.keys(LOAD)) if (k !== tab) LOAD[k]()?.catch?.(() => {});
    }
  } catch (e) { say(e.message, 'error'); }
}
el('search').oninput = () => { clearTimeout(timer); timer = setTimeout(() => loadList().catch((e) => say(e.message, 'error')), 250); };

/* Back: wizard-sheet.js (loaded after this file) closes the open sheet first, then falls back to this. */
window.__wizardCloseOverlay = () => false;
/* Inside the shell, Back (and Esc on the desktop) reach this app through WizardApp.back, not __wizardCloseOverlay,
 * and the desktop shell consumes the Esc key itself. Route both to the open sheet so they close it there too. */
try { window.WizardApp?.back?.register({ canGoBack: () => !!document.querySelector('dialog.wz-sheet[open]'), back: () => window.__wizardCloseOverlay() === true }); } catch (e) {}
if (!hosted) {
  history.pushState({ wzGuard: 1 }, '', location.href);
  addEventListener('popstate', () => { window.__wizardCloseOverlay(); history.pushState({ wzGuard: 1 }, '', location.href); });
}
showTab(); refresh().then(openFromLink);

/* Deep link from the Wizard inbox (Chats): #tx=<source>:<id> opens that transaction's sheet on Review. */
async function openFromLink() {
  const m = /^#tx=([^:]+):(.+)$/.exec(location.hash || '');
  if (!m) return;
  try { history.replaceState(history.state, '', location.pathname + location.search); } catch (e) {}
  try {
    const r = await api('api/transactions?' + new URLSearchParams({ source: decodeURIComponent(m[1]), id: decodeURIComponent(m[2]), hidden: 'true', limit: 1 }));
    const t = r.transactions && r.transactions[0];
    if (!t) { say('That transaction is no longer here.', 'note'); return; }
    if (tab !== 'review') { tab = 'review'; showTab(); refresh(); }
    openSheet(t);
  } catch (e) { say(e.message, 'error'); }
}
addEventListener('hashchange', openFromLink);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
