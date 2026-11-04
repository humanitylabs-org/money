"""Money: a simple Monarch-style money app over the box's bank connections.

Reads accounts and transactions from whichever bank-connection apps are installed (SimpleFIN by default; Monarch
and Teller also work) and keeps the owner's own layer on top in its own database: reviewed state, categories,
merchant names, notes, hidden transactions and auto-category rules. The connector's data is never changed; a fresh
sync never overwrites the owner's edits.

Stdlib only. Reads connectors over the box's private app network; offers the same data to the agent through MCP.
"""
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'money'
VERSION = '0.16.3'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8809')
SYNC_MINUTES = float(os.environ.get('MONEY_SYNC_MINUTES', '30'))
HISTORY_DAYS = int(os.environ.get('MONEY_HISTORY_DAYS', '730'))
# Known bank-connection apps, in default order. The first one installed and connected becomes the default source.
SOURCES = [s.split('=', 1) for s in os.environ.get('MONEY_SOURCES',
           'wizard-simplefin=http://wizard-simplefin:8808,wizard-monarch=http://wizard-monarch:8806,'
           'wizard-teller=http://wizard-teller:8805').split(',') if '=' in s]
NAMES = {'wizard-simplefin': 'SimpleFIN', 'wizard-monarch': 'Monarch', 'wizard-teller': 'Teller'}
# Where the owner fixes a bank that stopped updating (his own account on the provider's site).
FIX_URLS = {'wizard-simplefin': 'https://beta-bridge.simplefin.org/', 'wizard-monarch': 'https://app.monarchmoney.com/settings/institutions',
            'wizard-teller': None}
# A bank whose newest balance is older than this many days is called out as behind.
STALE_DAYS = int(os.environ.get('MONEY_STALE_DAYS', '2'))
# Money's own refresh is called out when it is this many hours old.
STALE_SYNC_HOURS = float(os.environ.get('MONEY_STALE_SYNC_HOURS', '6'))
CATEGORIES = ['Groceries', 'Restaurants', 'Coffee', 'Shopping', 'Travel', 'Hotels', 'Flights', 'Rides & Taxis', 'Gas',
              'Rent', 'Utilities', 'Phone & Internet', 'Subscriptions', 'Health', 'Entertainment', 'Fees', 'Gifts',
              'Business', 'Income', 'Refunds & Credits', 'Transfer', 'Card Payment', 'Other']
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
LOCK = threading.RLock()
SYNC_LOCK = threading.Lock()
SYNCING = threading.Event()
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


class Problem(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


# ---------------- storage ----------------

def _private():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    return DATA


def db():
    c = sqlite3.connect(_private() / 'money.sqlite3', timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE IF NOT EXISTS accounts (source TEXT, id TEXT, institution TEXT, name TEXT, last_four TEXT, type TEXT,
        currency TEXT, balance REAL, balance_at TEXT, PRIMARY KEY (source, id));
      CREATE TABLE IF NOT EXISTS tx (source TEXT, id TEXT, account TEXT, date TEXT, amount REAL, description TEXT,
        merchant TEXT, source_category TEXT, status TEXT, seen TEXT, PRIMARY KEY (source, id));
      CREATE INDEX IF NOT EXISTS tx_date ON tx(source, date);
      CREATE TABLE IF NOT EXISTS edits (source TEXT, id TEXT, reviewed INTEGER, category TEXT, merchant TEXT,
        note TEXT, hidden INTEGER, at TEXT, PRIMARY KEY (source, id));
      CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY AUTOINCREMENT, match TEXT NOT NULL, category TEXT NOT NULL,
        merchant TEXT, created TEXT);
      CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY, pos INTEGER);
      CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, source TEXT, tx TEXT, before TEXT);
      CREATE TABLE IF NOT EXISTS networth (date TEXT PRIMARY KEY, value REAL, assets REAL, liabilities REAL, origin TEXT);
      CREATE TABLE IF NOT EXISTS manual (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, amount REAL,
        note TEXT, updated TEXT);
      CREATE TABLE IF NOT EXISTS hints (id TEXT PRIMARY KEY, akey TEXT, date TEXT, amount REAL, merchant TEXT,
        category TEXT, seen TEXT);
    ''')
    if 'kind' not in {r[1] for r in c.execute('PRAGMA table_info(categories)')}:
        c.execute('ALTER TABLE categories ADD COLUMN kind TEXT')   # expense | income | transfer (None = guess by name)
    if 'splits' not in {r[1] for r in c.execute('PRAGMA table_info(edits)')}:
        c.execute('ALTER TABLE edits ADD COLUMN splits TEXT')   # [{category, amount}] parts of one charge
    if 'grp' not in {r[1] for r in c.execute('PRAGMA table_info(categories)')}:
        c.execute('ALTER TABLE categories ADD COLUMN grp TEXT')   # the heading a category sits under (Food & Dining, Transfers...)
    rcols = {r[1] for r in c.execute('PRAGMA table_info(rules)')}
    for col in ('min_abs REAL', 'max_abs REAL', 'days TEXT', 'split_category TEXT', 'split_amount REAL'):   # optional conditions: size range, days of the month
        if col.split()[0] not in rcols:
            c.execute('ALTER TABLE rules ADD COLUMN ' + col)
    if not c.execute('SELECT 1 FROM categories LIMIT 1').fetchone():
        with c:
            c.executemany('INSERT INTO categories(name,pos) VALUES (?,?)', [(n, i) for i, n in enumerate(CATEGORIES)])
    os.chmod(DATA / 'money.sqlite3', 0o600)
    return c


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _setting(c, k, default=None):
    r = c.execute('SELECT v FROM settings WHERE k=?', (k,)).fetchone()
    return json.loads(r[0]) if r else default


def _set(c, k, v):
    c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (k, json.dumps(v)))


# ---------------- connectors ----------------

def _get(base, path, query=None, timeout=30):
    url = base.rstrip('/') + path + ('?' + urlencode(query) if query else '')
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Accept': 'application/json'}), timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get('error')
        except ValueError:
            msg = None
        raise Problem(502, msg or f'The bank connection answered {e.code}.') from None
    except (OSError, ValueError):
        raise Problem(502, 'Could not reach that bank connection app.') from None


_SRC = {'at': 0.0, 'list': None}
_SRC_LOCK = threading.Lock()
SRC_TTL = 90


def _probe(sid, base):
    s = {'id': sid, 'name': NAMES.get(sid, sid), 'installed': False, 'connected': False, 'banks': 0, 'last_sync': None}
    try:
        st = _get(base, '/api/status', timeout=4)
        s.update(installed=True, connected=st.get('status') == 'connected' or bool(st.get('banks')),
                 banks=len(st.get('banks') or []), last_sync=st.get('last_sync'))
    except Problem:
        pass
    return s


def sources(fresh=False):
    """Every known connector, whether it's installed (reachable) and whether it's connected to a bank.
    Checked in parallel and kept for a minute or two: an app that isn't installed takes seconds to fail its name
    lookup, and that must never sit in front of every screen tap. A background loop keeps it warm."""
    if not fresh and _SRC['list'] is not None and time.time() - _SRC['at'] < SRC_TTL:
        return [dict(x) for x in _SRC['list']]
    with _SRC_LOCK:
        if not fresh and _SRC['list'] is not None and time.time() - _SRC['at'] < SRC_TTL:
            return [dict(x) for x in _SRC['list']]
        out = [None] * len(SOURCES)

        def one(i, sid, base):
            out[i] = _probe(sid, base)
        ts = [threading.Thread(target=one, args=(i, sid, base), daemon=True) for i, (sid, base) in enumerate(SOURCES)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(8)
        out = [o or {'id': sid, 'name': NAMES.get(sid, sid), 'installed': False, 'connected': False, 'banks': 0, 'last_sync': None}
               for o, (sid, _) in zip(out, SOURCES)]
        _SRC.update(at=time.time(), list=out)
        return [dict(x) for x in out]


def _sources_loop():
    while True:
        try:
            sources(fresh=True)
        except Exception:  # noqa: BLE001 - keep the cache warm after a failed check
            pass
        time.sleep(60)


def active_source(c=None, srcs=None):
    own = c is None
    c = c or db()
    try:
        chosen = _setting(c, 'source')
    finally:
        if own:
            c.close()
    if chosen:
        return chosen
    srcs = srcs if srcs is not None else sources()
    return next((s['id'] for s in srcs if s['connected']), next((s['id'] for s in srcs if s['installed']), SOURCES[0][0]))


def _num(v):
    try:
        return round(float(v), 2) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def sync(source=None):
    if not SYNC_LOCK.acquire(blocking=False):
        raise Problem(409, 'Already syncing.')
    SYNCING.set()
    try:
        source = source or active_source()
        base = dict(SOURCES).get(source)
        if not base:
            raise Problem(400, 'Unknown bank connection.')
        accts = _get(base, '/api/accounts').get('accounts') or []
        # The connector's own view of each bank (signed out, errors). Best effort: older connectors may not answer.
        try:
            st = _get(base, '/api/status', timeout=10)
            health = {'source': source, 'error': st.get('error') or st.get('last_error'),
                      'banks': [{'institution': b.get('institution'), 'status': b.get('status'), 'error': b.get('error')}
                                for b in st.get('banks') or []]}
        except Problem:
            health = None
        # Page newest -> oldest by date (connectors return at most 1000 rows per call).
        since = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
        rows, to, guard = {}, None, 0
        while guard < 40:
            q = {'limit': 1000, 'from': since, **({'to': to} if to else {})}
            batch = _get(base, '/api/transactions', q, timeout=60).get('transactions') or []
            new = [t for t in batch if t['id'] not in rows]
            for t in new:
                rows[t['id']] = t
            guard += 1
            if len(batch) < 1000 or not new:
                break
            to = min(t['date'] for t in batch if t.get('date'))
        now = _now()
        with LOCK:
            c = db()
            try:
                with c:
                    c.execute('DELETE FROM accounts WHERE source=?', (source,))
                    for a in accts:
                        bal = a.get('balance') if 'balance' in a else (a.get('ledger') if a.get('ledger') is not None else a.get('available'))
                        c.execute('INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?,?)', (
                            source, str(a['id']), a.get('institution'), a.get('name'), a.get('last_four'),
                            a.get('type') or ('credit' if re.search(r'card|credit', str(a.get('name')), re.I) else None),
                            a.get('currency') or 'USD', _num(bal), a.get('balance_at')))
                    was = {r[0]: (r[1], r[2]) for r in c.execute("SELECT id, status, amount FROM tx WHERE source=? AND status='pending'", (source,))}
                    for t in rows.values():
                        old = was.get(str(t['id']))
                        new_amt, new_status = _num(t.get('amount')), t.get('status') or 'posted'
                        if old and new_amt is not None and old[1] is not None and abs(new_amt - old[1]) >= 0.01:
                            # a reviewed pending charge settled at a different amount (tip, deposit, exchange rate): back to review
                            e = c.execute('SELECT reviewed, note FROM edits WHERE source=? AND id=?', (source, str(t['id']))).fetchone()
                            if e and e['reviewed']:
                                msg = f"{'Posted' if new_status == 'posted' else 'Changed'} at {abs(new_amt):.2f} (was {abs(old[1]):.2f} when reviewed)."
                                c.execute('UPDATE edits SET reviewed=0, note=? WHERE source=? AND id=?',
                                          (((e['note'] + ' ') if e['note'] else '') + msg, source, str(t['id'])))
                        c.execute('''INSERT INTO tx VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,id) DO UPDATE SET
                          account=excluded.account, date=excluded.date, amount=excluded.amount, description=excluded.description,
                          merchant=excluded.merchant, source_category=excluded.source_category, status=excluded.status, seen=excluded.seen''', (
                            source, str(t['id']), str(t.get('account')), t.get('date'), _num(t.get('amount')),
                            (t.get('description') or '')[:200], (t.get('counterparty') or None), t.get('category'),
                            t.get('status') or 'posted', now))
                    # A pending charge that posted under a new id disappears from the connector: drop it here too.
                    if rows:
                        gone = c.execute('''SELECT t.date, t.amount, t.description, a.name AS account_name FROM tx t LEFT JOIN accounts a
                                            ON a.source=t.source AND a.id=t.account WHERE t.source=? AND t.status='pending' AND t.seen<>?''', (source, now)).fetchall()
                        if gone:   # holds that dropped off without posting (hotel/rental deposits, pre-auths): keep a 30-day note
                            cut = (dt.date.today() - dt.timedelta(days=30)).isoformat()
                            keep = [x for x in (_setting(c, 'released') or []) if x.get('at', '') >= cut]
                            keep += [{'date': g['date'], 'amount': g['amount'], 'description': g['description'], 'account_name': g['account_name'], 'at': now[:10]} for g in gone]
                            _set(c, 'released', keep[-100:])
                        c.execute("DELETE FROM tx WHERE source=? AND status='pending' AND seen<>?", (source, now))
                    _set(c, 'last_sync', now)
                    _set(c, 'last_error', None)
                    _set(c, 'health', health)
            finally:
                c.close()
        hints = refresh_hints(source)
        return {'source': source, 'accounts': len(accts), 'transactions': len(rows), 'hints': hints}
    except Problem as e:
        c = db()
        try:
            with c:
                _set(c, 'last_error', e.message)
        finally:
            c.close()
        raise
    finally:
        SYNCING.clear()
        SYNC_LOCK.release()


def start_sync():
    def run():
        try:
            sync()
        except Problem:
            pass
    threading.Thread(target=run, daemon=True).start()


def _scheduler():
    while True:
        try:
            c = db()
            try:
                last = _setting(c, 'last_sync')
            finally:
                c.close()
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.strptime(last, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)).total_seconds() if last else 1e9
            if age > SYNC_MINUTES * 60 and not SYNCING.is_set():
                sync()
                networth()
                check_alerts()
        except Exception:  # noqa: BLE001 - keep scheduling after a failed sync
            pass
        time.sleep(60)


# ---------------- the owner's layer ----------------

def _rules(c):
    return [dict(r) for r in c.execute('SELECT id,match,category,merchant,min_abs,max_abs,days,split_category,split_amount FROM rules ORDER BY id')]


def _in_days(spec, date):
    """'25-5' = from the 25th through the 5th of the next month; '1-10' = 1st to 10th."""
    try:
        a, b = (int(x) for x in str(spec).split('-'))
        d = int(str(date)[8:10])
    except (ValueError, TypeError):
        return True
    return a <= d <= b if a <= b else (d >= a or d <= b)


def _norm(s):
    """Lowercase words only: '7-11 SUKHUMVIT23*POINT2' -> '7 11 sukhumvit23 point2'."""
    return ' '.join(re.findall(r"[a-z0-9]+", str(s or '').lower().replace("'", '')))


def _apply_rules(t, rules):
    """The most specific matching rule wins: one with amount/day conditions, then the longest match text."""
    raw = f"{t.get('merchant') or ''} {t.get('description') or ''}"
    text, ntext = raw.lower(), ' ' + _norm(raw) + ' '
    size = abs(t.get('amount') or 0)
    best = None
    for r in rules:
        m = r['match'].lower()
        nm = _norm(m)
        if not (m in text or (len(nm) >= 3 and nm in ntext)):
            continue
        if r.get('min_abs') is not None and size < r['min_abs'] or r.get('max_abs') is not None and size > r['max_abs']:
            continue
        if r.get('days') and not _in_days(r['days'], t.get('date')):
            continue
        rank = (r.get('min_abs') is not None or r.get('max_abs') is not None or bool(r.get('days')), len(m))
        if best is None or rank > best[0]:
            best = (rank, r)
    return best and best[1]


HINT_DAYS = int(os.environ.get('MONEY_HINT_DAYS', '120'))
# Borrowing Monarch's category picks is off: Money stands on its live connection alone (the owner's choice).
MONARCH_HINTS = os.environ.get('MONEY_MONARCH_HINTS', '0') == '1'


def refresh_hints(live):
    """Running beside Monarch: keep a copy of Monarch's categories for recent transactions, matched later by
    card, amount and date. Best effort, never fails a sync; costs no Monarch quota (reads the connector's copy)."""
    base = dict(SOURCES).get('wizard-monarch')
    if not MONARCH_HINTS or not base or live == 'wizard-monarch':
        with LOCK:   # off: Money's categories come only from the owner, his rules, his history and the live bank
            c = db()
            try:
                with c:
                    c.execute('DELETE FROM hints')
            finally:
                c.close()
        return 0
    try:
        rows = _fetch_all(base, (dt.date.today() - dt.timedelta(days=HINT_DAYS)).isoformat())
    except Problem:
        return 0
    try:   # Monarch's category groups say which categories are income or money moving between his own accounts
        groups = ((_get(base, '/api/snapshot', {'kind': 'categories'}).get('data') or {}).get('category_groups') or [])
    except Problem:
        groups = []
    now = _now()
    n = 0
    with LOCK:
        c = db()
        try:
            with c:
                for g in groups:   # fills only kinds not set yet: once Monarch is gone, Money keeps what it learned
                    if g.get('type') in ('income', 'expense', 'transfer'):
                        for cat in g.get('categories') or []:
                            c.execute('UPDATE categories SET kind=? WHERE name=? AND kind IS NULL', (g['type'], cat.get('name')))
                for t in rows.values():
                    k = _key(t.get('institution'), t.get('account_name'), t.get('last_four'))
                    if not k or not t.get('category') or t.get('amount') is None:
                        continue
                    c.execute('INSERT OR REPLACE INTO hints VALUES (?,?,?,?,?,?,?)', (str(t['id']), json.dumps(k), t.get('date'),
                              _num(t['amount']), t.get('counterparty') or t.get('description'), t['category'], now))
                    n += 1
                c.execute('DELETE FROM hints WHERE seen<>?', (now,))
        finally:
            c.close()
    return n


PAYMENT_RE = re.compile(r'autopay|automatic payment|payment\W+thank|thank you.*payment|online payment|mobile payment|'
                        r'credit crd|crd epay|card pmt|epayment|payment received|ach pmt', re.I)
# Words too common to identify a merchant by themselves.
STOP = set('the and of at in co ltd inc llc com www pay payment online store shop market mart coffee cafe central world '
           'terminal bangkok thailand sukhumvit thonglor asok branch bkk th us usa card credit bill purchase pos debit '
           'service services international group company new food restaurant bar hotel'.split())
# Payment processors that come before the real merchant ('WWW.2C2P.COM*LAZADA PAY', 'PwP Privacy.com').
PROCESSORS = set('www com 2c2p pwp sq tst paypal pp omise ksher pf lm sp dna'.split())


def _strip(words):
    while words and (words[0] in PROCESSORS or words[0].isdigit()):
        words = words[1:]
    return words


def _memory(c):
    """What he called each merchant before: imported history (with his fixes), his own picks, and Monarch's recent
    picks. Merchant words -> category, kept only where the past answer is clear (at least 2 in 3 agree)."""
    votes = {}

    def add(name, cat):
        k = ' '.join(_strip(_norm(name).split()))
        if not cat or len(k) < 4 or k in STOP:
            return
        k = k.replace(' ', '')   # 'Event Pop' and 'Eventpop' are the same merchant
        votes.setdefault(k, {}).setdefault(cat, 0)
        votes[k][cat] += 1
    for r in c.execute('''SELECT t.source, t.merchant, t.description, t.source_category, e.category FROM tx t
                          LEFT JOIN edits e ON e.source=t.source AND e.id=t.id'''):
        cat = r['category'] or (r['source_category'] if r['source'] == 'import' else None)
        if cat:
            add(r['merchant'] or r['description'], cat)
            if r['source'] != 'import' and r['description']:
                add(_tidy(r['description']), cat)
    for r in c.execute('SELECT merchant, category FROM hints'):
        add(r['merchant'], r['category'])
    out = {}
    for k, cs in votes.items():
        cat, n = max(cs.items(), key=lambda x: x[1])
        if n * 3 >= sum(cs.values()) * 2:
            out[k] = cat
    return out


def _remember(t, memory):
    """Find a known merchant at the start of the bank's text (the merchant name nearly always comes first)."""
    for text in (t.get('description'), t.get('merchant')):
        words = _strip(_norm(text).split())
        for start in range(min(2, len(words))):
            for end in range(min(len(words), start + 5), start, -1):
                k = ''.join(words[start:end])
                if len(k) >= 4 and k in memory:
                    return memory[k]
    return None


# Common knowledge every owner gets: well-known merchants and plain words -> a kind of spending, mapped onto the
# owner's own category names (first one he has wins). Lowest priority: his picks, rules and history always win.
KINDS = {
    'groceries': ['Groceries'], 'restaurants': ['Restaurants & Bars', 'Restaurants'], 'coffee': ['Coffee Shops', 'Coffee'],
    'shopping': ['Shopping'], 'clothing': ['Clothing', 'Shopping'], 'electronics': ['Electronics', 'Shopping'],
    'home': ['Furniture & Housewares', 'Home Improvement', 'Shopping'], 'personal': ['Personal', 'Health'],
    'medical': ['Medical', 'Health'], 'dentist': ['Dentist', 'Medical', 'Health'], 'fitness': ['Fitness', 'Health'],
    'rides': ['Taxi & Ride Shares', 'Rides & Taxis'], 'transit': ['Public Transit', 'Rides & Taxis'], 'gas': ['Gas'],
    'parking': ['Parking & Tolls'], 'flights': ['Travel & Vacation', 'Flights', 'Travel'],
    'hotels': ['Travel & Vacation', 'Hotels', 'Travel'], 'travel': ['Travel & Vacation', 'Travel'],
    'entertainment': ['Entertainment & Recreation', 'Entertainment'], 'subscriptions': ['Subscriptions'],
    'phone': ['Phone', 'Phone & Internet'], 'internet': ['Internet & Cable', 'Phone & Internet'],
    'utilities': ['Gas & Electric', 'Utilities'], 'pets': ['Pets'], 'cash': ['Cash & ATM'],
    'fees': ['Financial Fees', 'Fees'], 'interest': ['Interest', 'Income'], 'shipping': ['Postage & Shipping'],
    'insurance': ['Insurance'], 'taxes': ['Taxes'], 'education': ['Education'], 'charity': ['Charity', 'Gifts'],
}
COMMON = {
    'groceries': 'tops|big c|lotus s|lotuss|makro|villa market|foodland|gourmet market|max valu|7 eleven|7 11|familymart|family mart|'
                 'lawson|cj express|whole foods|trader joe s|trader joes|safeway|kroger|publix|aldi|costco|walmart grocery|'
                 'supermarket|grocery|deli mart|minimart|mini mart',
    'coffee': 'starbucks|cafe amazon|dunkin|tim hortons|blue bottle|coffee|cafe|caffe|roaster|roasters|espresso|bakery',
    'restaurants': 'mcdonald s|mcdonalds|kfc|burger king|subway|pizza|domino s|dominos|swensen s|sizzler|mk restaurant|'
                   'after you|hai di lao|haidilao|shabushi|bonchon|chipotle|wendy s|taco bell|restaurant|bistro|kitchen|'
                   'grill|bar b q|bbq|sushi|ramen|noodle|steak|izakaya|brasserie|eatery|diner|food court|take home|gourmet eats',
    'clothing': 'h m|uniqlo|zara|gap|nike|adidas|lululemon|decathlon|victoria s secret|victorias secret|victoria secret|'
                'old navy|forever 21|pull bear|cotton on|foot locker',
    'electronics': 'apple store|power buy|banana it|best buy|mi shop|xiaomi|samsung store',
    'home': 'ikea|homepro|home pro|nitori|index living|home depot|lowe s|lowes|bed bath',
    'shopping': 'miniso|daiso|muji|department store|'
                'lazada|shopee|amazon|amzn|aliexpress|temu|target|walmart|ebay|etsy|sephora|jung saem mool|duty free|king power',
    'personal': 'watsons|boots|eveandboy|salon|barber|spa|massage|nail',
    'medical': 'hospital|clinic|pharmacy|cvs|walgreens|bumrungrad|samitivej|bnh|medical|doctor',
    'dentist': 'dental|dentist|orthodont',
    'fitness': 'gym|fitness|jetts|virgin active|anytime fitness|yoga|pilates|crossfit|muay thai',
    'rides': 'uber|lyft|bolt|taxi|cabify|kakao mobility|indrive',
    'transit': 'bts|mrt|airport rail link|korail|metro|subway fare|train|amtrak|greyhound|ferry|bus',
    'gas': 'shell|ptt|chevron|esso|exxon|mobil|bangchak|caltex|bp|petrol|fuel|gas station',
    'parking': 'parking|parkwhiz|spothero|toll|easy park',
    'flights': 'airlines|airways|airasia|air asia|thai airways|bangkok airways|nok air|vietjet|cathay|singapore airlines|'
               'emirates|qatar airways|united airlines|delta air|american airlines|jetblue|southwest|korean air|eva air|'
               'ana|jal|lufthansa|klm|air france|ryanair|easyjet|teaflight',
    'hotels': 'marriott|hilton|hyatt|ihg|holiday inn|accor|sofitel|novotel|sheraton|westin|w hotel|four seasons|'
              'kempinski|hotel|resort|hostel|inns',
    'travel': 'airbnb|booking com|agoda|expedia|trip com|traveloka|hotels com|klook|kkday|makemytrip|gotogate',
    'entertainment': 'event pop|eventpop|ticketmelon|megatix|ticketmaster|eventbrite|sf cinema|major cineplex|cinema|'
                     'imax|theatre|theater|concert|museum|steam|playstation|nintendo|xbox|bowling|karaoke|club',
    'subscriptions': 'netflix|spotify|disney|hbo|youtube premium|apple com bill|google one|icloud|dropbox|notion|'
                     'canva|adobe|microsoft|github|slack|figma|chatgpt|patreon|substack|medium|audible|kindle unlimited',
    'phone': 'ais|dtac|true move|truemove|airalo|t mobile|verizon|at t|mint mobile|google fi|holafly|esim',
    'internet': 'comcast|xfinity|spectrum|3bb|true online|ais fibre',
    'utilities': 'electric|electricity|mea|pea|water authority|pg e|con ed',
    'pets': 'pet|vet|veterinary|animal hospital|petco|petsmart',
    'cash': 'atm|cash withdrawal|withdrawal',
    'fees': 'annual fee|membership fee|late fee|foreign transaction fee|interest charge|overdraft|service charge',
    'interest': 'interest paid|interest payment|dividend',
    'shipping': 'lalamove|fedex|dhl|ups|usps|kerry express|flash express|thailand post|post office',
    'insurance': 'insurance|geico|progressive|state farm|allianz|axa',
    'taxes': 'irs|internal revenue|tax payment|franchise tax',
    'education': 'udemy|coursera|tuition|school|university|course',
    'charity': 'donation|charity|red cross|unicef',
}
_COMMON = sorted(((' ' + _norm(w) + ' ', kind) for kind, words in COMMON.items() for w in words.split('|') if _norm(w)),
                 key=lambda x: -len(x[0]))


def _common(t, names):
    """The kind of merchant anyone would know, as one of his categories (None if he has no fitting category)."""
    text = ' ' + ' '.join(_strip(_norm(f"{t.get('description') or ''} {t.get('merchant') or ''}").split())) + ' '
    best = None   # the merchant name comes first; later words are usually the mall or street it sits in
    for pat, kind in _COMMON:
        i = text.find(pat)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, kind)
    return best and next((n for n in KINDS[best[1]] if n in names), None)


def _context(c):
    hints = {}
    for r in c.execute('SELECT akey,date,amount,category FROM hints'):
        hints.setdefault((tuple(json.loads(r['akey'])), round(r['amount'] or 0, 2)), []).append((r['date'], r['category']))
    names = {r[0] for r in c.execute('SELECT name FROM categories')}
    pay = next((n for n in ('Credit Card Payment', 'Card Payment') if n in names), 'Credit Card Payment')
    merged = {k.lower(): v for k, v in (_setting(c, 'merged') or {}).items()}
    return {'rules': _rules(c), 'memory': _memory(c), 'hints': hints, 'payment': pay, 'merged': merged, 'names': names}


def _hint(t, hints):
    k = _key(t.get('institution'), t.get('account_name'), t.get('last_four'))
    best = None
    for d, cat in hints.get((k, round(t.get('amount') or 0, 2)), []) if k else []:
        gap = abs(_days_between(d, t.get('date')))
        if gap <= 4 and (best is None or gap < best[0]):
            best = (gap, cat)
    return best and best[1]


def _rule_split(amount, rule):
    """A rule that sends a fixed amount to one category and the rest to its own: below the fixed amount everything
    is his own, exactly it is all the split category, above it the charge is shared."""
    a, fixed = abs(amount or 0), rule['split_amount']
    sign = -1 if (amount or 0) < 0 else 1
    if a + 0.005 < fixed:
        return [{'category': rule['category'], 'amount': amount}]
    if abs(a - fixed) < 0.01:
        return [{'category': rule['split_category'], 'amount': amount}]
    return [{'category': rule['split_category'], 'amount': round(sign * fixed, 2)},
            {'category': rule['category'], 'amount': round(sign * (a - fixed), 2)}]


def _parts(t):
    return t.get('splits') or [{'category': t.get('category'), 'amount': t.get('amount')}]


def _days_between(a, b):
    try:
        return (dt.date.fromisoformat(str(a)[:10]) - dt.date.fromisoformat(str(b)[:10])).days
    except ValueError:
        return 999


def _view(row, ctx):
    """His category wins, then his rules, then the bank's own category, then (beside Monarch) Monarch's pick for the
    same card, amount and date, then card payments, then what he called this merchant before."""
    t = dict(row)
    rules = ctx['rules'] if isinstance(ctx, dict) else ctx
    rule = _apply_rules(t, rules)
    cat, came = t.pop('e_category'), 'you'
    if not cat and row['source'] == 'import' and t.get('source_category'):
        cat, came = t['source_category'], 'bank'   # history he already reviewed in the old app beats rules
    if not cat and rule:
        cat, came = rule['category'], 'rule'
    if not cat and t.get('source_category'):
        cat, came = t['source_category'], 'bank'
    if not cat and isinstance(ctx, dict):
        if row['source'] != 'import':
            cat, came = _hint(t, ctx['hints']), 'monarch'
        if not cat and PAYMENT_RE.search(t.get('description') or ''):
            cat, came = ctx['payment'], 'payment'
        if not cat:
            cat, came = _remember(t, ctx['memory']), 'memory'
        if not cat:
            cat, came = _common(t, ctx['names']), 'common'
    merged = ctx.get('merged') if isinstance(ctx, dict) else None
    if cat and merged:
        cat = merged.get(cat.lower(), cat)
    parts = None
    if t.get('e_splits'):
        parts, came = json.loads(t['e_splits']), 'you'
    elif rule and rule.get('split_category') and rule.get('split_amount') and came == 'rule':
        parts = _rule_split(t.get('amount'), rule)
    t.pop('e_splits', None)
    if parts and merged:
        parts = [{**x, 'category': merged.get(x['category'].lower(), x['category'])} for x in parts]
    if parts and len(parts) > 1:
        t['splits'] = parts
        cat = max(parts, key=lambda x: abs(x['amount']))['category']
    elif parts:
        cat = parts[0]['category']
    t['category'] = cat or None
    t['category_from'] = came if cat else None
    t['merchant'] = t.pop('e_merchant') or (rule and rule['merchant']) or t.get('merchant') or _tidy(t.get('description'))
    t['reviewed'] = bool(t.pop('reviewed'))
    t['hidden'] = bool(t.pop('hidden'))
    return t


def _tidy(desc):
    d = re.sub(r'\s{2,}', ' ', re.sub(r'[#*]\S+|\b\d{4,}\b', '', str(desc or ''))).strip()
    return d[:60].title() if d.isupper() else d[:60]


SELECT = '''SELECT t.source, t.id, t.account, a.institution, a.name AS account_name, a.last_four, t.date, t.amount,
  t.description, t.merchant, t.source_category, t.status, COALESCE(e.reviewed,0) AS reviewed, e.category AS e_category,
  e.merchant AS e_merchant, e.note, COALESCE(e.hidden,0) AS hidden, e.splits AS e_splits
  FROM tx t LEFT JOIN accounts a ON a.source=t.source AND a.id=t.account LEFT JOIN edits e ON e.source=t.source AND e.id=t.id'''


def _key(inst, name, last_four):
    """Match one account across providers: bank word + last four digits (+ Morgan Stanley, which shares numbers)."""
    l4 = last_four or (re.findall(r'(\d{4,5})\)?\s*$', name or '') or [None])[0]
    if not l4:
        return None
    word = (re.findall(r'[a-z]+', (inst or '').lower()) or [''])[0]
    return (word, l4[-4:], 'morgan' in (name or '').lower())


def _live_starts(c, live):
    starts = {}
    for r in c.execute('''SELECT a.institution, a.name, a.last_four, MIN(t.date) AS d FROM tx t
                          JOIN accounts a ON a.source=t.source AND a.id=t.account WHERE t.source=? GROUP BY t.account''', (live,)):
        k = _key(r['institution'], r['name'], r['last_four'])
        if k and r['d']:
            starts[k] = min(starts.get(k, r['d']), r['d'])
    return starts


def _origin_names(c, live):
    meta = _setting(c, 'history') or {}
    return {'bank': NAMES.get(live, live), 'import': (meta.get('from_name') or 'Imported') + ' history'}


def _excluded(c):
    """Accounts the owner left out of Money, stored as account keys so imported history for them hides too."""
    return {tuple(k) for k in (_setting(c, 'excluded') or [])}


def _is_out(ex, inst, name, l4):
    k = _key(inst, name, l4)
    return bool(k) and k in ex


def set_excluded(body):
    with LOCK:
        c = db()
        try:
            src = active_source(c)
            a = c.execute('SELECT institution,name,last_four FROM accounts WHERE id=? AND source IN (?, \'import\')', (str(body.get('account') or ''), src)).fetchone()
            if not a:
                raise Problem(404, 'No such account.')
            k = _key(a['institution'], a['name'], a['last_four'])
            if not k:
                raise Problem(400, 'This account has no number to tell it apart, so it cannot be hidden.')
            ex = _excluded(c)
            ex = ex | {k} if body.get('hidden', True) else ex - {k}
            with c:
                _set(c, 'excluded', sorted(list(x) for x in ex))
        finally:
            c.close()
    return overview()


def transactions(args, c=None):
    own = c is None
    c = c or db()
    try:
        src = active_source(c)
        q, p = [SELECT + " WHERE t.source IN (?, 'import')"], [src]
        if args.get('origin') == 'bank':
            q.append('AND t.source=?'); p.append(src)
        elif args.get('origin') == 'import':
            q.append("AND t.source='import'")
        for key, op in (('from', '>='), ('to', '<=')):
            v = args.get(key)
            if v:
                if not DATE_RE.match(str(v)):
                    raise Problem(400, f'{key} must be YYYY-MM-DD.')
                q.append(f'AND t.date {op} ?'); p.append(v)
        if args.get('account'):
            v = str(args['account'])[:60]
            q.append('AND (t.account=? OR a.last_four=? OR a.name LIKE ?)'); p += [v, v, '%' + v + '%']
        if args.get('source') and args.get('id'):   # one transaction (the inbox feed's open link)
            q.append('AND t.source=? AND t.id=?'); p += [str(args['source'])[:40], str(args['id'])[:200]]
        if args.get('reviewed') in (True, False, 'true', 'false'):
            q.append('AND COALESCE(e.reviewed,0)=?'); p.append(1 if args['reviewed'] in (True, 'true') else 0)
        if str(args.get('hidden')) not in ('true', 'True'):
            q.append('AND COALESCE(e.hidden,0)=0')
        if args.get('min_abs') not in (None, ''):
            q.append('AND ABS(t.amount) >= ?'); p.append(abs(float(args['min_abs'])))
        for key, op in (('min_amount', '>='), ('max_amount', '<=')):
            if args.get(key) not in (None, ''):
                q.append(f'AND t.amount {op} ?'); p.append(float(args[key]))
        if args.get('direction') == 'out':
            q.append('AND t.amount < 0')
        elif args.get('direction') == 'in':
            q.append('AND t.amount > 0')
        limit = max(1, min(int(args.get('limit') or 200), 2000))
        q.append('ORDER BY t.date ASC, t.id ASC' if args.get('sort') == 'oldest' else 'ORDER BY t.date DESC, t.id DESC')
        ctx = _context(c)
        starts, names, ex = _live_starts(c, src), _origin_names(c, src), _excluded(c)
        keep = set(_setting(c, 'import_keep') or [])
        live_amounts = {}
        if keep:
            for r2 in c.execute('SELECT t.date,t.amount,a.institution,a.name,a.last_four FROM tx t JOIN accounts a ON a.source=t.source AND a.id=t.account WHERE t.source=?', (src,)):
                k2 = (_key(r2['institution'], r2['name'], r2['last_four']), round(r2['amount'] or 0, 2))
                d0 = dt.date.fromisoformat(r2['date'])
                for off in range(-4, 5):
                    live_amounts.setdefault((d0 + dt.timedelta(days=off)).isoformat(), set()).add(k2)
        rows = []
        for r in c.execute(' '.join(q), p):
            if ex and _is_out(ex, r['institution'], r['account_name'], r['last_four']):
                continue
            if r['source'] == 'import':
                k = _key(r['institution'], r['account_name'], r['last_four'])
                if k in starts and (r['date'] or '') >= starts[k]:
                    if r['id'] not in keep or (k, round(r['amount'] or 0, 2)) in live_amounts.get(r['date'], set()):
                        continue   # the live connection covers this account from here on: never show it twice
            v = _view(r, ctx)
            v['origin'] = 'import' if r['source'] == 'import' else 'bank'
            v['origin_name'] = names[v['origin']]
            rows.append(v)
        if args.get('search'):
            s = str(args['search'])[:80].lower()
            rows = [r for r in rows if any(s in str(r.get(k) or '').lower() for k in ('merchant', 'description', 'note', 'category', 'account_name'))]
        if args.get('category'):
            want = str(args['category']).lower()
            rows = [r for r in rows if any((x['category'] or 'Uncategorized').lower() == want for x in _parts(r))]
        srt = str(args.get('sort') or '')
        base, desc = (srt[:-5], True) if srt.endswith('_desc') else (srt, False)
        text_keys = {'merchant': lambda r: re.sub(r'^\W+', '', r['merchant'] or r['description'] or '').lower(),
                     'account': lambda r: (r['account_name'] or '').lower(),
                     'category': lambda r: re.sub(r'^\W+', '', r['category']).lower() if r['category'] else chr(0xffff)}
        if base in text_keys:
            rows.sort(key=lambda r: r['date'] or '', reverse=True)
            rows.sort(key=text_keys[base], reverse=desc)
        elif srt == 'smallest':
            rows.sort(key=lambda r: abs(r['amount'] or 0))
        elif srt == 'largest':    # biggest amounts first, in or out (absolute value)
            rows.sort(key=lambda r: -abs(r['amount'] or 0))
        elif args.get('sort') == 'spend':      # costs the most first: most negative amount at the top
            rows.sort(key=lambda r: (r['amount'] if r['amount'] is not None else 0, r['date'] or ''))
        elif args.get('sort') == 'income':   # biggest money in first
            rows.sort(key=lambda r: -(r['amount'] if r['amount'] is not None else 0))
        # Money out/in leave out money moving between his own accounts (card payments, transfers), so a card
        # charge and the autopay that settles it are not counted twice. Filtering to one category counts it all.
        kinds = _kinds(c)
        real = rows if args.get('category') else [r for r in rows if kinds.get((r['category'] or '').lower()) != 'transfer']
        out = {'count': len(rows[:limit]), 'total': len(rows), 'transactions': rows[:limit],
               'summary': {'out': round(sum(r['amount'] for r in real if (r['amount'] or 0) < 0), 2),
                           'in': round(sum(r['amount'] for r in real if (r['amount'] or 0) > 0), 2),
                           'transfers': len(rows) - len(real),
                           'uncategorized': sum(1 for r in rows if not r['category'])}}
        if args.get('all') is True:
            out['rows'] = rows
        return out
    finally:
        if own:
            c.close()


def _draft_id(prefix, raw):
    """A Chats draft id ([A-Za-z0-9_-], <=128): readable when the bank's id already fits, else a stable hash."""
    raw = str(raw)
    if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', raw):
        return f'{prefix}-{raw}'
    return f'{prefix}-{hashlib.sha1(raw.encode()).hexdigest()[:20]}'


def reply_context(args):
    """One transaction as reference data for a Chats draft (Reply). Nothing is sent by the app."""
    src, tid = str(args.get('source') or '')[:40], str(args.get('id') or '')[:200]
    if not src or not tid:
        raise Problem(400, 'Pass source and id.')
    c = db()
    try:
        rows = transactions({'source': src, 'id': tid, 'hidden': 'true', 'limit': 1}, c)['transactions']
        if not rows:
            raise Problem(404, 'No such transaction.')
        t = rows[0]
        cur = None   # Money's amounts are dollars, as everywhere else in the app
        name = t.get('merchant') or t.get('description') or 'Transaction'
        acct = ' '.join(x for x in (t.get('institution'), t.get('account_name'), f"…{t['last_four']}" if t.get('last_four') else None) if x)
        lines = [f"Money transaction: {name} (source {t['source']}, id {t['id']})",
                 f"{_amount_text(t.get('amount'), cur)} on {t.get('date')}" + (f' from {acct}' if acct else '')
                 + (' (pending, may still change)' if t.get('status') == 'pending' else '') + '.']
        if t.get('description') and t.get('description') != name:
            lines.append(f"Bank description: {t['description'][:200]}")
        came = {'you': 'set by Miguel', 'rule': 'from a rule', 'bank': "the bank's", 'monarch': "Monarch's pick",
                'payment': 'card payment', 'memory': 'what he used before', 'common': 'common merchant guess'}
        if t.get('splits'):
            lines.append('Split: ' + '; '.join(f"{x['category']} {_amount_text(x['amount'], cur)}" for x in t['splits']))
        else:
            lines.append(f"Category: {t.get('category') or 'Uncategorized'}" + (f" ({came.get(t.get('category_from'), t.get('category_from'))})" if t.get('category') else ''))
        lines.append('Status: ' + ', '.join(x for x in ('reviewed' if t.get('reviewed') else 'waiting for review',
                                                         'hidden from lists and totals' if t.get('hidden') else None) if x))
        if t.get('note'):
            lines.append(f"Note: {t['note'][:300]}")
        others = [r for r in transactions({'search': name[:40], 'limit': 8}, c)['transactions']
                  if not (r['source'] == t['source'] and r['id'] == t['id']) and (r.get('merchant') or '') == name][:5]
        if others:
            lines.append(f'\nOther recent {name} transactions:')
            lines += [f"- {r['date']}: {_amount_text(r.get('amount'), cur)}, {r.get('category') or 'Uncategorized'}" for r in others]
        lines.append(f"\nTo change it, use money_update with source {t['source']!r} and id {t['id']!r}, only when Miguel asks.")
        return {'id': _draft_id('tx', f"{t['source']}-{t['id']}"), 'title': f'Money: {name}'[:200], 'message': '\n'.join(lines)[:32000]}
    finally:
        c.close()


def update(body):
    src, tid = str(body.get('source') or ''), str(body.get('id') or '')
    ids = body.get('ids') if isinstance(body.get('ids'), list) else [tid]
    fields = {k: body[k] for k in ('reviewed', 'category', 'merchant', 'note', 'hidden', 'splits') if k in body}
    if not fields:
        raise Problem(400, 'Nothing to change.')
    for k in ('category', 'merchant', 'note'):
        if k in fields and fields[k] is not None:
            fields[k] = str(fields[k]).strip()[:300 if k == 'note' else 60] or None
    with LOCK:
        c = db()
        try:
            with c:
                if 'category' in fields and fields['category'] and not c.execute('SELECT 1 FROM categories WHERE name=?', (fields['category'],)).fetchone():
                    c.execute('INSERT INTO categories(name,pos) VALUES (?, (SELECT COALESCE(MAX(pos),0)+1 FROM categories))', (fields['category'],))
                for i in ids[:500]:
                    i = str(i)
                    if not c.execute('SELECT 1 FROM tx WHERE source=? AND id=?', (src, i)).fetchone():
                        raise Problem(404, 'No such transaction.')
                    old = c.execute('SELECT reviewed,category,merchant,note,hidden,splits FROM edits WHERE source=? AND id=?', (src, i)).fetchone()
                    c.execute('INSERT INTO log(at,source,tx,before) VALUES (?,?,?,?)', (_now(), src, i, json.dumps(dict(old) if old else None)))
                    cur = dict(old) if old else {'reviewed': 0, 'category': None, 'merchant': None, 'note': None, 'hidden': 0, 'splits': None}
                    for k, v in fields.items():
                        if k == 'splits':
                            amt = c.execute('SELECT amount FROM tx WHERE source=? AND id=?', (src, i)).fetchone()[0]
                            v = _check_splits(v, amt)
                            if 'category' not in fields:
                                cur['category'] = None
                        elif k == 'category' and 'splits' not in fields:
                            cur['splits'] = None   # one category replaces a split
                        cur[k] = (1 if v else 0) if k in ('reviewed', 'hidden') else v
                    c.execute('INSERT OR REPLACE INTO edits(source,id,reviewed,category,merchant,note,hidden,at,splits) VALUES (?,?,?,?,?,?,?,?,?)',
                              (src, i, cur['reviewed'], cur['category'], cur['merchant'], cur['note'], cur['hidden'], _now(), cur['splits']))
                if body.get('make_rule') and fields.get('category'):
                    match = str(body.get('match') or '').strip()[:60]
                    if len(match) < 3:
                        raise Problem(400, 'A rule needs at least 3 letters to match.')
                    c.execute('INSERT INTO rules(match,category,merchant,created) VALUES (?,?,?,?)', (match, fields['category'], fields.get('merchant'), _now()))
        finally:
            c.close()
    return {'ok': True, 'changed': len(ids)}


def _check_splits(parts, amount):
    """[{category, amount}] (amounts as sizes or signed); they must add up to the charge. None or [] clears."""
    if not parts:
        return None
    if not isinstance(parts, list) or len(parts) > 10:
        raise Problem(400, 'splits must be a short list of {category, amount}.')
    sign = -1 if (amount or 0) < 0 else 1
    out = [{'category': str(x.get('category') or '').strip()[:60], 'amount': round(sign * abs(float(x.get('amount'))), 2)} for x in parts]
    if any(not x['category'] for x in out) or abs(sum(x['amount'] for x in out) - (amount or 0)) > 0.01:
        raise Problem(400, f'The parts must add up to {abs(amount or 0):.2f}.')
    return json.dumps(out)


def undo():
    with LOCK:
        c = db()
        try:
            with c:
                last = c.execute('SELECT id,at FROM log ORDER BY id DESC LIMIT 1').fetchone()
                if not last:
                    raise Problem(409, 'Nothing to undo.')
                batch = c.execute('SELECT id,source,tx,before FROM log WHERE at=? ORDER BY id DESC', (last['at'],)).fetchall()
                for r in batch:
                    b = json.loads(r['before']) if r['before'] else None
                    if b:
                        c.execute('INSERT OR REPLACE INTO edits(source,id,reviewed,category,merchant,note,hidden,at,splits) VALUES (?,?,?,?,?,?,?,?,?)',
                                  (r['source'], r['tx'], b['reviewed'], b['category'], b['merchant'], b['note'], b['hidden'], _now(), b.get('splits')))
                    else:
                        c.execute('DELETE FROM edits WHERE source=? AND id=?', (r['source'], r['tx']))
                    c.execute('DELETE FROM log WHERE id=?', (r['id'],))
        finally:
            c.close()
    return {'ok': True, 'undone': len(batch)}


def categories_change(body):
    """merge (from -> into: every transaction, rule and future pick moves; from disappears), delete (unused only),
    kind (expense / income / transfer), add."""
    act, name = body.get('action'), str(body.get('name') or '').strip()[:60]
    with LOCK:
        c = db()
        try:
            with c:
                have = {r[0] for r in c.execute('SELECT name FROM categories')}
                if act == 'add':
                    if not name:
                        raise Problem(400, 'Name the category.')
                    c.execute('INSERT OR IGNORE INTO categories(name,pos) VALUES (?, (SELECT COALESCE(MAX(pos),0)+1 FROM categories))', (name,))
                    if body.get('kind') in ('expense', 'income', 'transfer'):
                        c.execute('UPDATE categories SET kind=? WHERE name=?', (body['kind'], name))
                elif name not in have:
                    raise Problem(404, 'No such category.')
                elif act == 'group':
                    c.execute('UPDATE categories SET grp=? WHERE name=?', (str(body.get('group') or '').strip()[:60] or None, name))
                    if body.get('position') is not None:
                        c.execute('UPDATE categories SET pos=? WHERE name=?', (int(body['position']), name))
                elif act == 'kind':
                    if body.get('kind') not in ('expense', 'income', 'transfer'):
                        raise Problem(400, 'kind must be expense, income or transfer.')
                    c.execute('UPDATE categories SET kind=? WHERE name=?', (body['kind'], name))
                elif act == 'merge':
                    into = str(body.get('into') or '').strip()[:60]
                    if into not in have or into == name:
                        raise Problem(400, 'Pick another existing category to merge into.')
                    c.execute('UPDATE edits SET category=? WHERE category=?', (into, name))
                    c.execute('UPDATE rules SET category=? WHERE category=?', (into, name))
                    m = {k: v for k, v in (_setting(c, 'merged') or {}).items() if v != name}
                    m = {k: (into if v == name else v) for k, v in m.items()}
                    m[name] = into
                    _set(c, 'merged', m)
                    c.execute('DELETE FROM categories WHERE name=?', (name,))
                elif act == 'delete':
                    used = transactions({'category': name, 'limit': 1, 'hidden': 'true'}, c)['total']
                    if used or c.execute('SELECT 1 FROM rules WHERE category=?', (name,)).fetchone():
                        raise Problem(409, f'{name} is still used ({used} transactions); merge it instead.')
                    c.execute('DELETE FROM categories WHERE name=?', (name,))
                else:
                    raise Problem(400, 'action must be add, kind, group, merge or delete.')
        finally:
            c.close()
    return {'ok': True, 'categories': overview()['categories']}


def rules_change(body):
    """add {match, category, merchant?, min_abs?, max_abs?, days? ('25-5')} or delete {ids}."""
    with LOCK:
        c = db()
        try:
            with c:
                if body.get('action') == 'delete':
                    ids = [int(x) for x in body.get('ids') or []][:500]
                    n = sum(c.execute('DELETE FROM rules WHERE id=?', (i,)).rowcount for i in ids)
                    return {'ok': True, 'deleted': n}
                match, cat = str(body.get('match') or '').strip()[:60], str(body.get('category') or '').strip()[:60]
                if len(match) < 3 or not cat:
                    raise Problem(400, 'A rule needs match text (3+ letters) and a category.')
                days = str(body['days']) if body.get('days') else None
                if days and not re.fullmatch(r'\d{1,2}-\d{1,2}', days):
                    raise Problem(400, "days looks like '25-5' (25th through the 5th).")
                lo, hi = (_num(body.get(k)) for k in ('min_abs', 'max_abs'))
                sc, sa = (str(body.get('split_category') or '').strip()[:60] or None), _num(body.get('split_amount'))
                if bool(sc) != bool(sa):
                    raise Problem(400, 'A split rule needs split_category and split_amount.')
                c.execute('INSERT INTO rules(match,category,merchant,created,min_abs,max_abs,days,split_category,split_amount) VALUES (?,?,?,?,?,?,?,?,?)',
                          (match, cat, body.get('merchant') or None, _now(), lo, hi, days, sc, abs(sa) if sa else None))
                c.execute('INSERT OR IGNORE INTO categories(name,pos) VALUES (?, (SELECT COALESCE(MAX(pos),0)+1 FROM categories))', (cat,))
                return {'ok': True, 'id': c.execute('SELECT last_insert_rowid()').fetchone()[0]}
        finally:
            c.close()


FREE_RE = re.compile(r'free', re.I)


def free_check(c, days=120):
    """Free categories are card-benefit buys that a statement credit cancels. Pair each charge with a credit on the
    same card (same Free category, or same amount within 60 days); a charge left unpaired is still real spending."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = [r for r in transactions({'from': since, 'all': True, 'limit': 1}, c)['rows'] if r['category'] and FREE_RE.search(r['category'])]
    credits = [r for r in rows if (r['amount'] or 0) > 0]
    used, waiting = set(), []
    for ch in sorted((r for r in rows if (r['amount'] or 0) < 0), key=lambda r: r['date']):
        hit = None
        for cr in credits:
            if id(cr) in used or cr['account_name'] != ch['account_name'] or not (0 <= _days_between(cr['date'], ch['date']) <= 60):
                continue
            if round(cr['amount'], 2) == round(-ch['amount'], 2) or cr['category'] == ch['category']:
                hit = cr
                break
        if hit:
            used.add(id(hit))
        else:
            waiting.append({k: ch[k] for k in ('date', 'amount', 'merchant', 'category', 'account_name', 'source', 'id')})
    return {'since': since, 'charges': sum(1 for r in rows if (r['amount'] or 0) < 0), 'credits': len(credits),
            'waiting_for_credit': waiting, 'waiting_total': round(-sum(w['amount'] for w in waiting), 2)}


def rule_delete(rid):
    with LOCK:
        c = db()
        try:
            with c:
                if not c.execute('DELETE FROM rules WHERE id=?', (int(rid),)).rowcount:
                    raise Problem(404, 'No such rule.')
        finally:
            c.close()
    return {'ok': True}


def import_setup(body):
    """Bring categories and rules over from another money app: categories (ordered names) and rules
    ([{match, category, merchant?}]). Existing rules with the same match text are replaced."""
    cats = body.get('categories')
    rules = body.get('rules')
    if cats is not None and not (isinstance(cats, list) and all(isinstance(x, str) for x in cats) and len(cats) <= 500):
        raise Problem(400, 'categories must be a list of names.')
    if rules is not None and not (isinstance(rules, list) and len(rules) <= 2000):
        raise Problem(400, 'rules must be a list.')
    added = 0
    with LOCK:
        c = db()
        try:
            with c:
                if cats:
                    names = list(dict.fromkeys(x.strip()[:60] for x in cats if x.strip()))
                    if body.get('replace_categories'):
                        c.execute('DELETE FROM categories')
                    start = c.execute('SELECT COALESCE(MAX(pos),-1)+1 FROM categories').fetchone()[0]
                    for i, n in enumerate(names):
                        c.execute('INSERT OR IGNORE INTO categories(name,pos) VALUES (?,?)', (n, start + i))
                for r in rules or []:
                    if not isinstance(r, dict):
                        continue
                    match, cat = str(r.get('match') or '').strip()[:60], str(r.get('category') or '').strip()[:60]
                    if len(match) < 3 or not cat:
                        continue
                    c.execute('DELETE FROM rules WHERE lower(match)=lower(?)', (match,))
                    c.execute('INSERT INTO rules(match,category,merchant,created) VALUES (?,?,?,?)',
                              (match, cat, (str(r['merchant']).strip()[:60] or None) if r.get('merchant') else None, _now()))
                    c.execute('INSERT OR IGNORE INTO categories(name,pos) VALUES (?, (SELECT COALESCE(MAX(pos),0)+1 FROM categories))', (cat,))
                    added += 1
        finally:
            c.close()
    return {'ok': True, 'rules_added': added}


def _fetch_all(base, since):
    rows, to, guard = {}, None, 0
    while guard < 40:
        q = {'limit': 1000, 'from': since, **({'to': to} if to else {})}
        batch = _get(base, '/api/transactions', q, timeout=90).get('transactions') or []
        new = [t for t in batch if t['id'] not in rows]
        for t in new:
            rows[t['id']] = t
        guard += 1
        if len(batch) < 1000 or not new:
            break
        to = min(t['date'] for t in batch if t.get('date'))
    return rows


def import_history(body):
    """One-time copy of another money app's transactions from a start date. Rows for an account stop showing
    from the day the live connection's own data for that account begins, so nothing appears twice."""
    sid = body.get('source') or 'wizard-monarch'
    base = dict(SOURCES).get(sid)
    since = str(body.get('from') or '2026-01-01')
    if not base or not DATE_RE.match(since):
        raise Problem(400, 'Pick a money app to import from and a start date (YYYY-MM-DD).')
    with LOCK:
        c = db()
        try:
            if sid == active_source(c):
                raise Problem(400, 'That app is already your live connection.')
        finally:
            c.close()
    accts = _get(base, '/api/accounts').get('accounts') or []
    rows = _fetch_all(base, since)
    review = {str(x) for x in (body.get('review_ids') or []) if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(x))}
    missing = review - set(rows)
    if missing:   # still waiting for review in the old app but older than the start date: bring just those
        for t in _fetch_all(base, (dt.date.fromisoformat(since) - dt.timedelta(days=730)).isoformat()).values():
            if str(t['id']) in missing:
                rows[t['id']] = t
    now = _now()
    made = 0
    with LOCK:
        c = db()
        try:
            with c:
                c.execute("DELETE FROM accounts WHERE source='import'")
                c.execute("DELETE FROM tx WHERE source='import'")
                for a in accts:
                    if (a.get('institution') or '').lower() == 'manual':
                        # The other app's hand-kept accounts become Money manual accounts (kept if already made).
                        if not c.execute('SELECT 1 FROM manual WHERE lower(name)=lower(?)', (a.get('name'),)).fetchone():
                            c.execute('INSERT INTO manual VALUES (?,?,?,?,?,?)', ('m' + str(a['id'])[-10:], str(a.get('name'))[:60], 'usd',
                                      _num(a.get('ledger') if a.get('ledger') is not None else a.get('balance')), f'Copied from {NAMES.get(sid, sid)}', now))
                            made += 1
                        continue
                    bal = a.get('ledger') if a.get('ledger') is not None else a.get('balance')
                    c.execute('INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?,?)', ('import', str(a['id']), a.get('institution'),
                              a.get('name'), a.get('last_four'), a.get('type'), a.get('currency') or 'USD', _num(bal), a.get('balance_at')))
                for t in rows.values():
                    c.execute('INSERT OR REPLACE INTO tx VALUES (?,?,?,?,?,?,?,?,?,?)', ('import', str(t['id']), str(t.get('account')), t.get('date'),
                              _num(t.get('amount')), (t.get('description') or '')[:200], t.get('counterparty') or None, t.get('category'), 'posted', now))
                    # History arrives already reviewed: it was reviewed (or not) in the old app, not here.
                    pend = str(t['id']) in review
                    c.execute('INSERT OR IGNORE INTO edits(source,id,reviewed,category,merchant,note,hidden,at) VALUES (?,?,?,?,?,?,?,?)', ('import', str(t['id']), 0 if pend else 1, None, None, None, 0, now))
                    if pend:
                        c.execute("UPDATE edits SET reviewed=0 WHERE source='import' AND id=?", (str(t['id']),))
                _set(c, 'import_keep', sorted(review))
                try:   # monthly net worth history, if the other app keeps one
                    snaps = (_get(base, '/api/snapshot', {'kind': 'net_worth'}).get('data') or {}).get('snapshots') or []
                except Problem:
                    snaps = []
                c.execute("DELETE FROM networth WHERE origin='import'")
                for sn in snaps:
                    if DATE_RE.match(str(sn.get('date'))) and sn['date'] >= since[:8] + '01' and sn.get('net_worth') is not None:
                        c.execute("INSERT OR IGNORE INTO networth VALUES (?,?,?,?,'import')", (sn['date'], _num(sn['net_worth']),
                                  _num(sn.get('assets_balance')), _num(sn.get('liabilities_balance'))))
                _set(c, 'history', {'from': since, 'from_name': NAMES.get(sid, sid), 'source': sid, 'at': now, 'rows': len(rows)})
        finally:
            c.close()
    shown = transactions({'origin': 'import', 'limit': 1, 'hidden': 'true'})['total']
    return {'ok': True, 'imported': len(rows), 'showing': shown, 'manual_accounts_added': made}


def _btc_price(c):
    cached = _setting(c, 'btc') or {}
    if cached.get('at') and time.time() - cached['at'] < 600:
        return cached
    try:
        req = urllib.request.Request('https://api.coinbase.com/v2/prices/BTC-USD/spot', headers={'User-Agent': f'money/{VERSION}'})
        with urllib.request.urlopen(req, timeout=8) as r:
            price = float(json.loads(r.read())['data']['amount'])
        cached = {'price': round(price, 2), 'at': time.time()}
        with c:
            _set(c, 'btc', cached)
    except (OSError, ValueError, KeyError):
        cached['stale'] = True
    return cached


def manual_accounts(c):
    rows = [dict(r) for r in c.execute('SELECT id,name,kind,amount,note,updated FROM manual ORDER BY name')]
    price = _btc_price(c) if any(r['kind'] == 'btc' for r in rows) else {}
    for r in rows:
        r['balance'] = round((r['amount'] or 0) * price['price'], 2) if r['kind'] == 'btc' and price.get('price') else (r['amount'] if r['kind'] != 'btc' else None)
        if r['kind'] == 'btc':
            r['btc_price'] = price.get('price')
            r['price_stale'] = bool(price.get('stale'))
    return rows


def manual_save(body):
    name = str(body.get('name') or '').strip()[:60]
    kind = body.get('kind')
    if not name:
        raise Problem(400, 'Give the account a name.')
    if kind not in ('btc', 'usd'):
        raise Problem(400, 'Pick Bitcoin or Dollars.')
    try:
        amount = float(body.get('amount'))
    except (TypeError, ValueError):
        raise Problem(400, 'Enter an amount.') from None
    if not -1e12 < amount < 1e12 or (kind == 'btc' and not 0 <= amount <= 21e6):
        raise Problem(400, 'That amount looks wrong.')
    mid = str(body.get('id') or '') or 'm' + secrets_token()
    with LOCK:
        c = db()
        try:
            with c:
                c.execute('INSERT OR REPLACE INTO manual VALUES (?,?,?,?,?,?)', (mid, name, kind, round(amount, 8),
                          (str(body.get('note') or '').strip()[:200] or None), _now()))
        finally:
            c.close()
    return {'ok': True, 'id': mid}


def secrets_token():
    import secrets
    return secrets.token_hex(5)


def manual_delete(mid):
    with LOCK:
        c = db()
        try:
            with c:
                if not c.execute('DELETE FROM manual WHERE id=?', (mid,)).rowcount:
                    raise Problem(404, 'No such account.')
        finally:
            c.close()
    return {'ok': True}


def networth():
    """Everything owned minus owed: live bank accounts, then imported balances for accounts the live connection
    doesn't have yet, then manual accounts. Records one point per day for the history chart."""
    c = db()
    try:
        src = active_source(c)
        names = _origin_names(c, src)
        rows = []
        ex = _excluded(c)
        live = [dict(r) for r in c.execute('SELECT institution,name,last_four,type,balance,balance_at FROM accounts WHERE source=?', (src,))
                if not _is_out(ex, r['institution'], r['name'], r['last_four'])]
        keys = {_key(a['institution'], a['name'], a['last_four']) for a in live}
        for a in live:
            if a['balance'] is not None:
                rows.append({'name': a['name'], 'institution': a['institution'], 'origin': 'bank', 'origin_name': names['bank'], 'balance': a['balance'], 'as_of': a['balance_at']})
        hist = _setting(c, 'history') or {}
        for a in c.execute("SELECT institution,name,last_four,balance,balance_at FROM accounts WHERE source='import' AND balance IS NOT NULL"):
            if _key(a['institution'], a['name'], a['last_four']) in keys or _is_out(ex, a['institution'], a['name'], a['last_four']):
                continue
            if abs(a['balance']) < 0.005:
                continue
            rows.append({'name': a['name'], 'institution': a['institution'], 'origin': 'import', 'origin_name': names['import'],
                         'balance': a['balance'], 'as_of': a['balance_at'] or hist.get('at')})
        for m in manual_accounts(c):
            if m['balance'] is not None:
                rows.append({'name': m['name'], 'institution': 'Manual', 'origin': 'manual', 'origin_name': 'Manual', 'balance': m['balance'], 'as_of': m['updated'],
                             'detail': f"{m['amount']:g} BTC" if m['kind'] == 'btc' else None})
        assets = round(sum(r['balance'] for r in rows if r['balance'] > 0), 2)
        debts = round(sum(r['balance'] for r in rows if r['balance'] < 0), 2)
        total = round(assets + debts, 2)
        if rows:
            with c:
                c.execute("INSERT OR REPLACE INTO networth VALUES (?,?,?,?,'money')", (dt.date.today().isoformat(), total, assets, debts))
        points = [dict(r) for r in c.execute('SELECT date,value,origin FROM networth ORDER BY date')]
    finally:
        c.close()
    rows.sort(key=lambda r: -abs(r['balance']))
    return {'net_worth': total, 'assets': assets, 'liabilities': debts, 'accounts': rows, 'history': points,
            'note': 'Negative balances are money owed (cards, loans). Imported balances are as of the import and drop out once the live connection has that account.'}


def set_source(body):
    sid = body.get('source')
    if sid not in dict(SOURCES):
        raise Problem(400, 'Unknown bank connection.')
    with LOCK:
        c = db()
        try:
            with c:
                _set(c, 'source', sid)
                _set(c, 'last_sync', None)
        finally:
            c.close()
    _SRC['at'] = 0.0
    start_sync()
    return overview()


# ---------------- summaries ----------------

def _month_bounds(month):
    if month and not re.match(r'^\d{4}-\d{2}$', str(month)):
        raise Problem(400, 'month must be YYYY-MM.')
    d = dt.date.fromisoformat((month or dt.date.today().strftime('%Y-%m')) + '-01')
    nxt = (d + dt.timedelta(days=32)).replace(day=1)
    return d.isoformat(), (nxt - dt.timedelta(days=1)).isoformat(), d.strftime('%Y-%m')


SKIP = {'transfer', 'card payment', 'credit card payment'}
INCOME = {'income', 'business income', 'other income', 'va income', 'military income', 'interest', 'paychecks'}


KIND_GROUP = {'income': 'Income', 'transfer': 'Transfers', 'expense': 'Other'}


def _groups(c):
    """Categories under their headings, in his order: [{group, kind, categories}]. A heading's kind is its first
    category's (income, expense or transfer = not counted as spending or income)."""
    kinds, out, at = _kinds(c), [], {}
    for name, grp in c.execute('SELECT name, grp FROM categories ORDER BY pos'):
        kind = kinds.get(name.lower(), 'expense')
        g = grp or KIND_GROUP[kind]
        if g not in at:
            at[g] = {'group': g, 'kind': kind, 'categories': []}
            out.append(at[g])
        at[g]['categories'].append(name)
    return out


def _kinds(c):
    """Category -> expense / income / transfer. His setting (or Monarch's group) wins; otherwise guess by name."""
    out = {}
    for name, kind in c.execute('SELECT name, kind FROM categories'):
        out[name.lower()] = kind or ('transfer' if name.lower() in SKIP else 'income' if name.lower() in INCOME else 'expense')
    return out


def spending(month=None):
    start, end, label = _month_bounds(month)
    rows = transactions({'from': start, 'to': end, 'limit': 2000})['transactions']
    c = db()
    try:
        kinds = _kinds(c)
        gof = {n: g['group'] for g in _groups(c) for n in g['categories']}
    finally:
        c.close()
    cats, income, spent = {}, 0.0, 0.0
    for part in (x for t in rows for x in _parts(t)):
        cat, amt = part['category'] or 'Uncategorized', part['amount']
        kind = kinds.get(cat.lower()) or ('transfer' if cat.lower() in SKIP else 'income' if cat.lower() in INCOME else 'expense')
        if kind == 'transfer' or amt is None:
            continue
        if amt > 0 and kind == 'income':
            income += amt
            continue
        cats[cat] = cats.get(cat, 0) - amt   # refunds and credits net against spending
    cats = {k: round(v, 2) for k, v in cats.items() if abs(v) >= 0.005}
    spent = round(sum(v for v in cats.values() if v > 0), 2)
    rows = sorted(({'category': k, 'amount': v, 'group': gof.get(k, 'Other')} for k, v in cats.items()), key=lambda x: -x['amount'])
    groups = {}
    for r in rows:
        groups[r['group']] = round(groups.get(r['group'], 0) + r['amount'], 2)
    return {'month': label, 'spent': spent, 'income': round(income, 2), 'categories': rows,
            'groups': sorted(({'group': k, 'amount': v} for k, v in groups.items()), key=lambda x: -x['amount'])}


def _days_ago(d, today):
    try:
        return (today - dt.date.fromisoformat(str(d)[:10])).days
    except ValueError:
        return None


def attention(c, src, allaccts, visible):
    """Problems the owner must see: a bank that stopped updating, a bank signed out, or Money unable to refresh.
    Each item says what is wrong and where he fixes it."""
    name, fix, app = NAMES.get(src, src), FIX_URLS.get(src), '/' + src + '/'
    today = dt.datetime.now(dt.timezone.utc).date()
    out = []
    err, last = _setting(c, 'last_error'), _setting(c, 'last_sync')
    if err:
        out.append({'kind': 'sync', 'institution': None, 'title': f"Money can't refresh from {name}",
                    'message': err, 'fix_url': app, 'fix_label': f'Open {name}'})
    elif last and not SYNCING.is_set():
        hrs = (dt.datetime.now(dt.timezone.utc) - dt.datetime.strptime(last, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)).total_seconds() / 3600
        if hrs > STALE_SYNC_HOURS:
            out.append({'kind': 'sync', 'institution': None, 'title': f"Money hasn't refreshed in {int(hrs)} hours",
                        'message': f'Last refresh {last[:16].replace("T", " ")} UTC. Tap Refresh now on the Accounts tab.', 'fix_url': None, 'fix_label': None})
    health = _setting(c, 'health') or {}
    if health.get('source') != src:
        health = {}
    if health.get('error'):
        out.append({'kind': 'connection', 'institution': None, 'title': f'{name} needs attention',
                    'message': health['error'], 'fix_url': app, 'fix_label': f'Open {name}'})
    shown = {a['institution'] for a in visible}
    flagged = set()
    for b in health.get('banks') or []:
        if b.get('institution') in shown and (b.get('error') or (b.get('status') or 'connected') != 'connected'):
            flagged.add(b['institution'])
            out.append({'kind': 'signed_out', 'institution': b['institution'], 'title': f"{b['institution']} needs you to reconnect",
                        'message': (b.get('error') or f'{name} lost access to {b["institution"]}.') + f' Sign in to {b["institution"]} again on {name}.',
                        'fix_url': fix or app, 'fix_label': f'Fix on {name}'})
    newest = {}
    for a in allaccts:
        d = str(a.get('balance_at') or '')[:10]
        if DATE_RE.match(d) and d > newest.get(a['institution'], ''):
            newest[a['institution']] = d
    for inst in sorted(shown - flagged, key=lambda x: x or ''):
        d = newest.get(inst)
        days = _days_ago(d, today) if d else None
        if days is not None and days > STALE_DAYS:
            out.append({'kind': 'behind', 'institution': inst, 'as_of': d, 'days': days, 'title': f'{inst} stopped updating',
                        'message': f'Nothing new from {inst} since {dt.date.fromisoformat(d).strftime("%b")} {dt.date.fromisoformat(d).day} ({days} days), and {name} '
                                   f'shows no error. Its balances and transactions here are out of date. Reconnecting {inst} on {name} usually fixes it.',
                        'fix_url': fix or app, 'fix_label': f'Fix on {name}'})
    return out


INBOX_MAX = 200


def _amount_text(amount, currency=None):
    a = amount or 0
    sign = '\u2212' if a < 0 else '+' if a > 0 else ''
    cur = (currency or 'USD').upper()
    return f"{sign}{'$' if cur == 'USD' else cur + ' '}{abs(a):,.2f}"


def inbox():
    """Wizard OS inbox feed v1: one item per transaction waiting for review, exactly what the app badge
    (overview to_review) counts. Read-only. Reviewing a transaction in Money drops it from the feed."""
    c = db()
    try:
        rows = [r for r in transactions({'limit': 1, 'all': True}, c)['rows'] if not r['reviewed']]
        cur = {(a['id']): a['currency'] for a in c.execute('SELECT id,currency FROM accounts')}
    finally:
        c.close()
    items = []
    for r in rows[:INBOX_MAX]:
        try:
            when = int(dt.datetime.combine(dt.date.fromisoformat(str(r['date'])[:10]), dt.time(12), dt.timezone.utc).timestamp())
        except ValueError:
            when = 0
        name = r.get('account_name') or ''
        l4 = r.get('last_four') or (re.findall(r'(\d{4})\)?\s*$', name) or [''])[0]
        acct = re.sub(r'\s{2,}', ' ', re.sub(r'\s*(?:X{2,}|\*{2})\d{4}\b', '', re.sub(r'\s*\((?:\.{0,3}|X+)\d{4,5}\)\s*$', '', name))).strip()
        bits = [_amount_text(r['amount'], cur.get(r['account'])), (acct + (f' ··{l4[-4:]}' if l4 and l4[-4:] not in acct else '')).strip(),
                r['category'] or 'Uncategorized', 'pending' if r.get('status') == 'pending' else None]
        items.append({'key': f"tx:{r['source']}:{r['id']}", 'title': (r['merchant'] or r['description'] or 'Transaction')[:200],
                      'text': ' · '.join(b for b in bits if b)[:2000], 'time': when, 'priority': 3,
                      'open': '#tx=' + quote(r['source'], safe='') + ':' + quote(str(r['id']), safe='')})
    return {'inbox': 1, 'items': items, 'total': len(rows)}


# ---------------- alerts ----------------
# Money tells the owner itself (no agent): a bank that needs him (signed out, stopped updating, Money can't refresh)
# and charges at or above his "large" size. Sent through the box's Notifications app when it is installed; a
# problem goes out as "needs you" (Notifications puts it where he will see it), a large charge follows Money's own
# notification choice. Each problem once, again after ALERT_REMIND_DAYS if still there; each charge once.
NOTIFY_URL = os.environ.get('WIZARD_NOTIFY_URL', 'http://notifications:8080/mcp').strip()
ALERT_LARGE = float(os.environ.get('MONEY_ALERT_LARGE', '500'))
ALERT_DAYS = 3            # only charges dated in the last few days count as new
ALERT_REMIND_DAYS = 3
ALERT_KEEP = 400


def alert_settings(c):
    s = _setting(c, 'alerts') or {}
    return {'on': s.get('on', True) is not False, 'large': float(s.get('large') or ALERT_LARGE)}


def _open_link():
    # The owner's own first listed shell origin (env order: the box lists its main shell first); a bare https host
    # with no port is the last resort, since the shell usually lives on its own port.
    listed = [x.strip() for x in os.environ.get('WIZARD_TRUSTED_ORIGINS', '').split(',') if x.strip().startswith('https://')]
    listed.sort(key=lambda o: urlsplit(o).port is None)
    return listed[0].rstrip('/') + '/wizard-os-home/?open=money' if listed else ''


def notify_owner(title, message, priority='default', tags=''):
    """One notification through the Notifications app. True only when it answered that it was sent."""
    u = urlsplit(NOTIFY_URL)
    if u.scheme != 'http' or u.username or u.password or not u.hostname:
        return False
    args = {'title': title[:120], 'message': message[:600], 'priority': priority, 'app': APP_ID}
    if tags:
        args['tags'] = tags
    link = _open_link()
    if link:
        args['link'] = link
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'notify', 'arguments': args}}).encode()
    req = urllib.request.Request(NOTIFY_URL, data=body, method='POST', headers={
        'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', 'User-Agent': f'{APP_ID}/{VERSION}'})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read(65536).decode('utf-8', 'replace')
    except (OSError, urllib.error.URLError, ValueError):
        return False
    m = re.search(r'\{.*\}', raw, re.S)   # plain JSON or one SSE data line
    try:
        res = json.loads(m.group(0)).get('result') or {} if m else {}
    except ValueError:
        return False
    return bool(res) and not res.get('isError')


def _charge_line(r, cur):
    name = r.get('merchant') or r.get('description') or 'Unknown merchant'
    acct = (r.get('account_name') or '').strip()
    l4 = (r.get('last_four') or '')[-4:]
    where = acct + (f' ··{l4}' if l4 and l4 not in acct else '')
    return f"{_amount_text(r['amount'], cur)} at {name}" + (f' on {where}' if where else '') + f" ({r.get('category') or 'Uncategorized'})"


def check_alerts(post=None, now=None):
    """Compare what needs the owner now with what he was already told; send only what's new. Returns a summary."""
    post = post or notify_owner
    now = now or time.time()
    c = db()
    try:
        cfg = alert_settings(c)
        state = _setting(c, 'alert_state')
        first = state is None
        state = state or {'problems': {}, 'charges': []}
        if not cfg['on']:
            return {'on': False, 'sent': 0}
        src = active_source(c)
        ex = _excluded(c)
        allaccts = [dict(r) for r in c.execute('SELECT id,institution,name,last_four,type,currency,balance,balance_at FROM accounts WHERE source=?', (src,))]
        visible = [a for a in allaccts if not _is_out(ex, a['institution'], a['name'], a['last_four'])]
        problems = attention(c, src, allaccts, visible)
        since = (dt.datetime.fromtimestamp(now, dt.timezone.utc).date() - dt.timedelta(days=ALERT_DAYS)).isoformat()
        rows = transactions({'from': since, 'origin': 'bank', 'direction': 'out', 'min_abs': cfg['large'], 'limit': 200}, c)['transactions']
        kinds = _kinds(c)
        cur = {a['id']: a['currency'] for a in allaccts}
    finally:
        c.close()
    sent, failed = [], 0
    # problems: key on what is wrong and where, so a changing hour count is still the same problem
    seen = state.get('problems') or {}
    keep = {}
    for p in problems:
        k = f"{p['kind']}:{p.get('institution') or ''}"
        last = seen.get(k)
        if last and now - last < ALERT_REMIND_DAYS * 86400:
            keep[k] = last
            continue
        if post(p['title'], p['message'] + (f" Fix: {p['fix_label']}." if p.get('fix_label') else ''), 'high', 'needs-you'):
            keep[k] = now
            sent.append(k)
        else:
            failed += 1
            if last:
                keep[k] = last
    state['problems'] = keep   # a fixed problem drops out, so it alerts again if it comes back
    # large charges: real spending only (no transfers or card payments), each once; pending and posted copies of one
    # charge share account + amount + merchant, so that is the key
    told = list(state.get('charges') or [])
    new = []
    for r in rows:
        if kinds.get((r.get('category') or '').lower()) == 'transfer':
            continue
        k = f"{r.get('account')}|{round(r['amount'], 2)}|{(r.get('merchant') or r.get('description') or '').lower()[:40]}"
        if k in told or k in [x[0] for x in new]:
            continue
        new.append((k, r))
    if new and not first:
        if len(new) == 1:
            ok = post('Large charge', _charge_line(new[0][1], cur.get(new[0][1].get('account'))) + '.')
        else:
            lines = [_charge_line(r, cur.get(r.get('account'))) for _, r in new[:6]]
            ok = post(f'{len(new)} large charges', '; '.join(lines) + ('; and more.' if len(new) > 6 else '.'))
        if ok:
            sent += [k for k, _ in new]
        else:
            failed += 1
            new = []   # try again on the next check
    told += [k for k, _ in new]
    state['charges'] = told[-ALERT_KEEP:]
    c = db()
    try:
        with LOCK:
            _set(c, 'alert_state', state)
            c.commit()
    finally:
        c.close()
    return {'on': True, 'large': cfg['large'], 'baseline': first, 'sent': len(sent), 'failed': failed,
            'problems': len(problems), 'new_charges': 0 if first else len(new)}


def alerts_change(a):
    """money_alerts: read or change the owner's alert settings, or send a test notification."""
    action = a.get('action') or 'get'
    c = db()
    try:
        cfg = alert_settings(c)
        if action == 'set':
            if 'on' in a:
                cfg['on'] = bool(a['on'])
            if a.get('large') is not None:
                v = float(a['large'])
                if not 1 <= v <= 1_000_000:
                    raise Problem(400, 'large is a charge size from 1 to 1,000,000.')
                cfg['large'] = v
            with LOCK:
                _set(c, 'alerts', cfg)
                c.commit()
        elif action == 'test':
            ok = notify_owner('Money alerts are on', f"You'll hear from Money when a bank needs you or a charge of "
                              f"{_amount_text(-cfg['large']).lstrip(chr(0x2212))} or more comes in.")
            return {**cfg, 'test_sent': ok, 'note': None if ok else "Couldn't reach the Notifications app."}
        elif action != 'get':
            raise Problem(400, 'action is get, set or test.')
    finally:
        c.close()
    return cfg


def overview():
    srcs = sources()
    c = db()
    try:
        src = active_source(c, srcs)
        ex = _excluded(c)
        allaccts = [dict(r) for r in c.execute('SELECT id,institution,name,last_four,type,currency,balance,balance_at FROM accounts WHERE source=? ORDER BY institution,name', (src,))]
        accts = [a for a in allaccts if not _is_out(ex, a['institution'], a['name'], a['last_four'])]
        hidden_accts = [a for a in allaccts if _is_out(ex, a['institution'], a['name'], a['last_four'])]
        visible = transactions({'limit': 1, 'all': True}, c)
        allrows = visible['rows']
        cats = [r[0] for r in c.execute('SELECT name FROM categories ORDER BY pos')]
        hist = _setting(c, 'history')
        if hist:
            hist['showing'] = sum(1 for r in allrows if r['origin'] == 'import')
        out = {'source': src, 'source_name': NAMES.get(src, src), 'sources': srcs, 'chosen': bool(_setting(c, 'source')),
               'accounts': accts, 'hidden_accounts': hidden_accts, 'manual': manual_accounts(c), 'history': hist,
               'to_review': sum(1 for r in allrows if not r['reviewed']), 'transactions': len(allrows),
               'categories': cats, 'category_kinds': _kinds(c), 'category_groups': _groups(c),
               'released_holds': _setting(c, 'released') or [], 'rules': _rules(c), 'free_check': free_check(c),
               'last_sync': _setting(c, 'last_sync'), 'error': _setting(c, 'last_error'), 'syncing': SYNCING.is_set(),
               'attention': attention(c, src, allaccts, accts), 'alerts': alert_settings(c)}
    finally:
        c.close()
    cash = sum(a['balance'] or 0 for a in accts if (a['type'] or '') in ('depository', 'checking', 'savings') or re.search(r'check|saving|ckg', (a['name'] or ''), re.I))
    out['cash'] = round(cash, 2)
    return out


# ---------------- MCP ----------------

TOOLS = [
    {'name': 'money_categories', 'description': "Change the owner's categories in Money only (never the bank or Monarch): action add (name, kind?), kind (name, kind = expense/income/transfer), group (name, group heading, optional position for order), merge (name into another: every transaction and rule moves, the old one disappears), delete (unused only). Only when the owner asked.",
     'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['add', 'kind', 'group', 'merge', 'delete']}, 'name': {'type': 'string'}, 'group': {'type': 'string'}, 'position': {'type': 'integer'},
                     'into': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['expense', 'income', 'transfer']}}, 'required': ['action', 'name'], 'additionalProperties': False}},
    {'name': 'money_rules', 'description': "Add or delete auto-category rules in Money only. add: match text, category, optional merchant, min_abs/max_abs (size of the amount) and days ('25-5' = 25th through the 5th); split_category + split_amount sends that fixed amount of each charge to split_category and the rest to category. The most specific matching rule wins. delete: ids. Only when the owner asked.",
     'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['add', 'delete']}, 'match': {'type': 'string'}, 'category': {'type': 'string'},
                     'merchant': {'type': 'string'}, 'min_abs': {'type': 'number'}, 'max_abs': {'type': 'number'}, 'days': {'type': 'string'},
                     'split_category': {'type': 'string'}, 'split_amount': {'type': 'number'},
                     'ids': {'type': 'array', 'items': {'type': 'integer'}}}, 'required': ['action'], 'additionalProperties': False}},
    {'name': 'money_overview', 'description': "Money app overview: which bank connection it reads (SimpleFIN by default), attention (banks that stopped updating or need reconnecting, or Money unable to refresh; tell the owner first), its accounts with balances, manual accounts (e.g. Bitcoin cold storage), imported history, how many transactions still need review, categories and auto-category rules, last sync.",
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'money_transactions', 'description': "Search transactions with the owner's categories, merchant names, notes and reviewed state applied. Filters: from/to (YYYY-MM-DD), account (id, last digits or name), search, category, reviewed (true/false), min_amount/max_amount, direction (out = money out, in = money in), origin (bank = live connection, import = imported history), min_abs (size at least, in or out), sort (newest default, oldest, largest = biggest amounts first in or out, smallest, merchant/account/category A-Z or *_desc Z-A, spend = costs the most first, income = biggest money in first), limit (default 200). Each row says its origin. Negative = money out.",
     'inputSchema': {'type': 'object', 'properties': {'from': {'type': 'string'}, 'to': {'type': 'string'}, 'account': {'type': 'string'}, 'search': {'type': 'string'}, 'category': {'type': 'string'},
                     'reviewed': {'type': 'boolean'}, 'min_amount': {'type': 'number'}, 'max_amount': {'type': 'number'}, 'limit': {'type': 'integer'},
                     'direction': {'type': 'string', 'enum': ['out', 'in']}, 'min_abs': {'type': 'number'}, 'origin': {'type': 'string', 'enum': ['bank', 'import']},
                     'sort': {'type': 'string', 'enum': ['newest', 'oldest', 'largest', 'smallest', 'merchant', 'merchant_desc', 'account', 'account_desc', 'category', 'category_desc', 'spend', 'income']}}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'money_spending', 'description': 'Spending by category for a month (YYYY-MM, default this month); refunds and credits net against their category; transfers and card payments excluded.',
     'inputSchema': {'type': 'object', 'properties': {'month': {'type': 'string'}}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'money_update', 'description': "Change the owner's layer on transactions (never the bank's data): reviewed, category, merchant, note, hidden, splits ([{category, amount}] that add up to the charge; an empty list or a plain category clears it). Pass source and id (or ids for several). With make_rule true and match text, also saves a rule so future matching transactions get that category. Only do this when the owner asked.",
     'inputSchema': {'type': 'object', 'properties': {'source': {'type': 'string'}, 'id': {'type': 'string'}, 'ids': {'type': 'array', 'items': {'type': 'string'}}, 'reviewed': {'type': 'boolean'},
                     'category': {'type': 'string'}, 'merchant': {'type': 'string'}, 'note': {'type': 'string'}, 'hidden': {'type': 'boolean'}, 'splits': {'type': 'array', 'items': {'type': 'object', 'properties': {'category': {'type': 'string'}, 'amount': {'type': 'number'}}}}, 'make_rule': {'type': 'boolean'}, 'match': {'type': 'string'}},
                     'required': ['source'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False}},
    {'name': 'money_net_worth', 'description': 'Net worth now (assets, liabilities, every account with where its balance comes from: bank connection, manual, or imported history) and its history (monthly imported points plus one point per day since).',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'money_manual_account', 'description': "Add or update one of the owner's manual accounts (e.g. a cold-storage Bitcoin wallet): name, kind ('btc' = amount in BTC, valued at the live price; 'usd' = dollar balance), amount, optional note, id to update. Only when the owner asks; never ask for seed phrases or keys.",
     'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'name': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['btc', 'usd']}, 'amount': {'type': 'number'}, 'note': {'type': 'string'}},
                     'required': ['name', 'kind', 'amount'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False}},
    {'name': 'money_alerts', 'description': "Money's own notifications to the owner (sent by the app, no agent needed): a bank that needs him (signed out, stopped updating, Money can't refresh) and any charge at or above 'large' (default 500). action get (current settings), set (on true/false, large = charge size), test (sends one test notification). Change only when the owner asks.",
     'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['get', 'set', 'test']}, 'on': {'type': 'boolean'}, 'large': {'type': 'number'}},
                     'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False}},
]


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            a, n = p.get('arguments') or {}, p.get('name')
            fn = {'money_overview': lambda: overview(), 'money_transactions': lambda: transactions(a),
                  'money_spending': lambda: spending(a.get('month')), 'money_update': lambda: update(a),
                  'money_manual_account': lambda: manual_save(a), 'money_net_worth': lambda: networth(),
                  'money_categories': lambda: categories_change(a), 'money_rules': lambda: rules_change(a),
                  'money_alerts': lambda: alerts_change(a)}.get(n)
            if not fn:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            v = fn()
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except Problem as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        if (self.headers.get('Host') or '').lower() not in TRUSTED_HOSTS:
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def send(self, code, body=None, kind='application/json'):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Problem(415, 'Use application/json.')
        size = int(self.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 262144:
            raise Problem(413, 'Request too large.')
        try:
            d = json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path, q = self.route(), {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            if path in ('/healthz', '/api/health'):
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION})
            if path == '/api/overview':
                return self.send(200, overview())
            if path == '/api/inbox':
                return self.send(200, inbox())
            if path == '/api/transactions':
                return self.send(200, transactions(q))
            if path == '/api/transactions/context':
                return self.send(200, reply_context(q))
            if path == '/api/networth':
                return self.send(200, networth())
            if path == '/api/spending':
                return self.send(200, spending(q.get('month')))
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/mcp':
                if any(self.headers.get(h) for h in FORWARDED):
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/update':
                return self.send(200, update(b))
            if path == '/api/categories':
                return self.send(200, categories_change(b))
            if path == '/api/rules':
                return self.send(200, rules_change(b))
            if path == '/api/undo':
                return self.send(200, undo())
            if path == '/api/accounts/hide':
                return self.send(200, set_excluded(b))
            if path == '/api/import/history':
                return self.send(200, import_history(b))
            if path == '/api/manual':
                return self.send(200, manual_save(b))
            m = re.fullmatch(r'/api/manual/([A-Za-z0-9]{1,20})/delete', path)
            if m:
                return self.send(200, manual_delete(m.group(1)))
            if path == '/api/import':
                return self.send(200, import_setup(b))
            if path == '/api/source':
                return self.send(200, set_source(b))
            if path == '/api/sync':
                if not SYNCING.is_set():
                    start_sync()
                time.sleep(0.2)
                return self.send(202, overview())
            m = re.fullmatch(r'/api/rules/(\d{1,9})/delete', path)
            if m:
                return self.send(200, rule_delete(m.group(1)))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    os.umask(0o077)
    for f in _private().rglob('*'):
        try:
            os.chmod(f, 0o700 if f.is_dir() else 0o600)
        except OSError:
            pass
    db().close()
    threading.Thread(target=_sources_loop, daemon=True).start()
    threading.Thread(target=_scheduler, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()
