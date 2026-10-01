#!/usr/bin/env python3
"""Build headlines.json: gold-relevant headlines (free, no-key RSS) + upcoming Fed calendar items.

usage: build_headlines.py --out headlines.json [--old headlines.json] [--now ISO]

SAFE BY DESIGN: this script NEVER fails the job. Every source is optional; any failure becomes a
GitHub '::warning::' line and a per-source status entry. If NOTHING usable came back, headlines.json is left untouched.
The file is rewritten only when items/fedCalendar changed, or as a heartbeat when the stored generatedAt is >= 180 min old.

Schema: {generatedAt, count, windowHours, sources:{name:{http,items,kept,err?}},
         items:[{title,source,time(ISO Z),link,tags[]}], fedCalendar:[{title,time(ISO, ET offset),impact,kind,src:'fed',link?}]}
The Worker only reads items[].title/time/tags/source (+ fedCalendar for display). news.json is NOT touched by this script.
"""
import argparse, html, json, re, sys, os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo('America/New_York')
except Exception:
    NY = timezone(timedelta(hours=-4))

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
KEEP_H = 48          # keep items up to 48 h old
MAX_ITEMS = 60
PER_FEED_CAP = 12    # newest N items taken from any one feed (a chatty query cannot flood the file)
PER_PUBLISHER_CAP = 8
HEARTBEAT_MIN = 180
GN = 'https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q='

# name, url, mode ('all' = every item is relevant by construction, tags added automatically; 'filter' = keep only items that match a tag), auto tags
FEEDS = [
    ('fed_press',    'https://www.federalreserve.gov/feeds/press_all.xml',          'all',    ['FED'], 'Federal Reserve'),
    ('fed_monetary', 'https://www.federalreserve.gov/feeds/press_monetary.xml',     'all',    ['FED', 'FOMC', 'RATES'], 'Federal Reserve'),
    ('fed_speeches', 'https://www.federalreserve.gov/feeds/speeches.xml',           'all',    ['FED'], 'Federal Reserve'),
    ('ecb_press',    'https://www.ecb.europa.eu/rss/press.html',                    'all',    ['ECB'], 'ECB'),
    ('bea',          'https://www.bea.gov/rss/rss.xml',                             'all',    [], 'BEA'),
    ('fxstreet',     'https://www.fxstreet.com/rss/news',                           'filter', [], 'FXStreet'),
    ('forexlive',    'https://www.forexlive.com/feed/news',                         'filter', [], 'ForexLive'),
    ('investing_commodities', 'https://www.investing.com/rss/commodities.rss',      'filter', [], 'Investing.com'),
    ('investing_news', 'https://www.investing.com/rss/news_95.rss',                 'filter', [], 'Investing.com'),
    ('marketwatch',  'https://feeds.content.dowjones.io/public/rss/mw_topstories',  'filter', [], 'MarketWatch'),
    ('cnbc',         'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114', 'filter', [], 'CNBC'),
    ('bbc_business', 'https://feeds.bbci.co.uk/news/business/rss.xml',              'filter', [], 'BBC'),
    ('bbc_world',    'https://feeds.bbci.co.uk/news/world/rss.xml',                 'filter', [], 'BBC'),
    ('aljazeera',    'https://www.aljazeera.com/xml/rss/all.xml',                   'filter', [], 'Al Jazeera'),
    ('whitehouse',   'https://www.whitehouse.gov/presidential-actions/feed/',       'filter', [], 'White House'),
    ('gn_gold',      GN + 'gold+price+when:1d',                                      'filter', ['GOLD'], None),
    ('gn_fed',       GN + 'Federal+Reserve+OR+FOMC+when:1d',                         'filter', [], None),
    ('gn_tariff',    GN + 'tariff+when:1d',                                          'filter', ['TARIFF'], None),
    ('gn_war',       GN + 'war+OR+airstrike+OR+missile+OR+ceasefire+when:1d',        'filter', ['GEOPOLITICS'], None),
    ('gn_sanctions', GN + 'sanctions+when:1d',                                       'filter', ['SANCTIONS'], None),
    ('gn_yields',    GN + 'treasury+yields+OR+%22dollar+index%22+when:1d',           'filter', [], None),
    ('gn_oil',       GN + 'oil+prices+OPEC+when:1d',                                 'filter', ['OIL'], None),
    ('gn_cbgold',    GN + 'central+bank+gold+buying+when:2d',                        'filter', ['CENTRAL_BANK_GOLD'], None),
]
# Google News mixes in thousands of local/irrelevant outlets: only these publishers are accepted from gn_* queries.
GN_PUBLISHERS = ('reuters', 'bloomberg', 'cnbc', 'marketwatch', 'wall street journal', 'wsj', 'financial times', 'ft.com', 'kitco', 'fxstreet',
                 'fxempire', 'forexlive', 'investinglive', 'investing.com', 'yahoo finance', 'barron', 'associated press', 'ap news', 'bbc',
                 'al jazeera', 'the guardian', 'axios', 'politico', 'cnn', 'fox business', 'nbc news', 'cbs news', 'abc news', 'the economist',
                 'mining.com', 'oilprice', 'world gold council', 'business insider', 'seeking alpha', 'dailyfx', 'forbes', 'newsweek',
                 'the times of israel', 'times of israel', 'haaretz', 'jerusalem post', 'nikkei', 'south china morning post', 'the hill',
                 'washington post', 'new york times', 'npr', 'la times', 'marketscreener', 'tradingview', 'benzinga', 'zerohedge', 'rttnews', 'ft markets')
FED_CAL_URL = 'https://www.federalreserve.gov/json/calendar.json'

# tag -> (regex, case-sensitive?)
TAGS = [
    ('FOMC',  r'\bFOMC\b|\bdot plot\b|\bBeige Book\b|rate decision|policy meeting', False),
    ('FED',   r"\bFed\b|\bFed's\b|Federal Reserve|\bPowell\b|\bWarsh\b|\bWaller\b|\bBowman\b|\bJefferson\b|\bGoolsbee\b|\bKashkari\b|\bMusalem\b|\bBarkin\b|\bLogan\b", True),
    ('RATES', r'rate (hike|cut|increase|decrease|path|expectations)|(raise|cut|hike|lower) (interest )?rates?|interest rates?|basis points|\bbps\b|monetary policy|rate bets', False),
    ('USD',   r'dollar index|\bDXY\b|\bUS dollar\b|\bU\.S\. dollar\b|greenback|\bUSD\b|\bdollar\b', False),
    ('YIELDS', r'treasury (yields?|bonds?|notes?|bills?|auction|market|selloff|sell-off)|\bT-?bonds?\b|bond (yields?|market|sell|rout|rally)|\b\d+-year (yield|note|treasury)|\byields (rise|rose|fall|fell|jump|surge|slump|climb|drop|hit|touch)|\b(rising|falling|higher|lower|surging|retreating) yields', False),
    ('INFLATION', r'inflation|\bCPI\b|\bPCE\b|\bPPI\b|price index|deflator|consumer prices|producer prices', False),
    ('JOBS',  r'payrolls?|\bNFP\b|jobless|unemployment|jobs report|labou?r market|\bJOLTS\b|\bADP\b|job openings|employment', False),
    ('GEOPOLITICS', r'\bwar\b|\bwars\b|airstrikes?|missiles?|invasion|ceasefire|cease-fire|\bIran\b|\bIsrael\b|\bGaza\b|\bUkraine\b|\bRussia\b|\bTaiwan\b|\bHormuz\b|blockade|\bNATO\b|nuclear|military strike|escalat', False),
    ('TARIFF', r'tariffs?\b|trade war|trade deal|trade talks|export controls?|import dut(y|ies)', False),
    ('SANCTIONS', r'sanctions?\b|sanctioned', False),
    ('CENTRAL_BANK_GOLD', r'central banks?\b.{0,50}\bgold\b|\bgold\b.{0,50}central banks?|gold reserves|\bPBoC\b.{0,30}gold|official.sector.{0,20}gold', False),
    ('OIL',   r'\boil\b|crude|\bBrent\b|\bWTI\b|\bOPEC\b', False),
    ('CHINA', r'\bChina\b|\bChinese\b|\bPBoC\b|\byuan\b|\bBeijing\b|renminbi', False),
    ('ECB',   r'\bECB\b|European Central Bank|\bLagarde\b|\bSchnabel\b|\bBundesbank\b', False),
    ('CENTRAL_BANK', r'\bBoJ\b|Bank of Japan|\bBoE\b|Bank of England|\bSNB\b|\bRBA\b|\bPBoC\b|central bank', False),
    ('GOLD',  r'\bgold\b|\bXAU\b|bullion|safe.haven', False),
]
TAGS = [(n, re.compile(rx, 0 if cs else re.I)) for n, rx, cs in TAGS]
TAG_ORDER = [n for n, _ in TAGS]

def tags_for(title, auto):
    s = set(auto)
    for n, rx in TAGS:
        if rx.search(title): s.add(n)
    return [n for n in TAG_ORDER if n in s]

def warn(msg): print('::warning::' + msg)

def parse_dt(s):
    if not s: return None
    s = str(s).strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try: d = datetime.fromisoformat(s.replace('Z', '+00:00'))
        except Exception: return None
    if d is None: return None
    if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)

def fetch(url, timeout=25):
    """-> (http_status, bytes, err). Never raises."""
    last = (0, b'', 'unknown')
    for attempt in (1, 2):
        try:
            req = Request(url, headers={'User-Agent': UA, 'Accept': 'application/rss+xml, application/xml, text/xml, application/json, */*', 'Accept-Language': 'en-US,en;q=0.9'})
            with urlopen(req, timeout=timeout) as r:
                return (r.status, r.read(), '')
        except Exception as e:
            code = getattr(e, 'code', 0) or 0
            last = (code, b'', ('HTTP %s' % code) if code else (type(e).__name__ + ': ' + str(e)[:80]))
            if code in (403, 404, 410): break
    return last

def clean_title(t):
    t = html.unescape(re.sub(r'<[^>]+>', '', t or ''))
    return re.sub(r'\s+', ' ', t).strip()

def parse_feed(raw, auto, fixed_source, now):
    """-> list of items {title, source, dt, link}; raises on invalid XML."""
    root = ET.fromstring(raw)
    out = []
    for it in root.iter():
        tag = it.tag.split('}')[-1]
        if tag not in ('item', 'entry'): continue
        f = {}
        for c in it:
            ct = c.tag.split('}')[-1]
            if ct == 'link' and not (c.text or '').strip() and c.get('href'): f['link'] = c.get('href')
            elif ct in ('title', 'link', 'pubDate', 'published', 'updated', 'date', 'source') and ct not in f: f[ct] = (c.text or '').strip()
        title = clean_title(f.get('title'))
        dt = parse_dt(f.get('pubDate') or f.get('published') or f.get('updated') or f.get('date'))
        if not title or dt is None: continue
        if dt > now + timedelta(minutes=10): continue            # future-dated = bad data
        source = fixed_source or f.get('source') or 'news'
        if fixed_source is None:                                   # Google News: "Title - Publisher"
            pub = f.get('source') or ''
            if not any(w in pub.lower() for w in GN_PUBLISHERS): continue
            if pub and title.endswith(' - ' + pub): title = title[:-(len(pub) + 3)].strip()
            source = pub or 'Google News'
        out.append({'title': title[:220], 'source': source, 'dt': dt, 'link': f.get('link', '')})
    return out

def norm_key(t): return re.sub(r'[^a-z0-9]+', '', t.lower())[:70]

def fed_calendar(raw, now):
    """Upcoming Fed events from federalreserve.gov/json/calendar.json (single-day, timed, FOMC/speeches/testimony/Beige Book)."""
    j = json.loads(raw.decode('utf-8-sig'))
    out = []
    lo, hi = now - timedelta(hours=6), now + timedelta(days=35)
    for e in j.get('events', []):
        if not isinstance(e, dict): continue
        typ, title, tm, month, days = e.get('type'), clean_title(e.get('title')), (e.get('time') or '').strip(), e.get('month') or '', str(e.get('days') or '').strip()
        if typ not in ('FOMC', 'Speeches', 'Testimony', 'Beige') or not tm or not re.fullmatch(r'\d{1,2}', days): continue
        m = re.fullmatch(r'(\d{1,2}):(\d{2})\s*([ap])\.m\.', tm)
        if not m or not re.fullmatch(r'\d{4}-\d{2}', month): continue
        hh, mm = int(m.group(1)) % 12 + (12 if m.group(3) == 'p' else 0), int(m.group(2))
        try: d = datetime(int(month[:4]), int(month[5:7]), int(days), hh, mm, tzinfo=NY)
        except Exception: continue
        du = d.astimezone(timezone.utc)
        if not (lo <= du <= hi): continue
        if typ == 'FOMC':
            kind = 'FOMC'; impact = 'High'
            if title == 'FOMC Meeting': title = 'FOMC rate decision / statement'
        elif typ == 'Beige':
            kind = 'BEIGE'; impact = 'Medium'
        else:
            kind = 'TESTIMONY' if typ == 'Testimony' else 'SPEECH'
            # Chair (not Vice Chair) = High, any other speaker = Medium
            impact = 'High' if re.search(r'\bChair\b', title) and not re.search(r'Vice Chair', title) else 'Medium'
        item = {'title': title, 'time': d.isoformat(), 'impact': impact, 'kind': kind, 'src': 'fed'}
        if e.get('link'): item['link'] = e['link']
        out.append(item)
    out.sort(key=lambda x: x['time'])
    seen, res = set(), []
    for x in sorted(out, key=lambda x: (parse_dt(x['time']), x['title'])):
        k = (x['time'], x['title'])
        if k in seen: continue
        seen.add(k); res.append(x)
    return res[:40]

def build(now, old, fetcher=fetch):
    stats, pool = {}, []
    def work(f):
        name, url, mode, auto, fsrc = f
        code, raw, err = fetcher(url)
        return f, code, raw, err
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, FEEDS))
    for (name, url, mode, auto, fsrc), code, raw, err in results:
        st = {'http': code, 'items': 0, 'kept': 0}
        if code != 200 or not raw:
            st['err'] = err or ('HTTP %s' % code); stats[name] = st; warn('headlines: %s failed (%s)' % (name, st['err'])); continue
        try:
            items = parse_feed(raw, auto, fsrc, now)
        except Exception as e:
            st['err'] = 'parse: ' + str(e)[:60]; stats[name] = st; warn('headlines: %s invalid XML (%s)' % (name, st['err'])); continue
        st['items'] = len(items)
        items = [i for i in items if (now - i['dt']) <= timedelta(hours=KEEP_H)]
        items.sort(key=lambda i: i['dt'], reverse=True)
        kept = 0
        for i in items:
            tg = tags_for(i['title'], auto)
            if mode == 'filter' and not name.startswith('gn_') and not tg:
                continue                     # general feeds: need a real keyword match. gn_* are topic queries (already relevant) -> kept
            if mode == 'all' and not tg and name == 'bea':
                # BEA: keep only gold-relevant releases
                if not re.search(r'personal income|outlays|GDP|gross domestic|trade|international transactions', i['title'], re.I): continue
                tg = ['INFLATION'] if re.search(r'personal income|outlays', i['title'], re.I) else ['USD']
            if kept >= PER_FEED_CAP: break
            kept += 1
            pool.append({'title': i['title'], 'source': i['source'], 'dt': i['dt'], 'link': i['link'], 'tags': tg})
        st['kept'] = kept; stats[name] = st
    # carry forward still-fresh items from the previous file so one flaky source never empties the list
    if isinstance(old, dict) and isinstance(old.get('items'), list):
        for o in old['items']:
            try:
                dt = parse_dt(o['time'])
                if dt and (now - dt) <= timedelta(hours=KEEP_H) and o.get('title') and isinstance(o.get('tags'), list):
                    pool.append({'title': o['title'], 'source': o.get('source') or 'news', 'dt': dt, 'link': o.get('link') or '', 'tags': o['tags']})
            except Exception: pass
    pool.sort(key=lambda i: i['dt'], reverse=True)
    seen_t, seen_l, pub_n, final = set(), set(), {}, []
    for i in pool:
        k = norm_key(i['title'])
        if k in seen_t or (i['link'] and i['link'] in seen_l): continue
        if pub_n.get(i['source'], 0) >= PER_PUBLISHER_CAP: continue
        seen_t.add(k); seen_l.add(i['link']); pub_n[i['source']] = pub_n.get(i['source'], 0) + 1
        final.append(i)
        if len(final) >= MAX_ITEMS: break
    items_out = [{'title': i['title'], 'source': i['source'], 'time': i['dt'].strftime('%Y-%m-%dT%H:%M:%SZ'), 'link': i['link'], 'tags': i['tags']} for i in final]
    # Fed calendar (optional)
    cal, calst = [], {'http': 0, 'items': 0}
    code, raw, err = fetcher(FED_CAL_URL, 40)
    calst['http'] = code
    if code == 200 and raw:
        try: cal = fed_calendar(raw, now); calst['items'] = len(cal)
        except Exception as e: calst['err'] = 'parse: ' + str(e)[:60]; warn('headlines: fed calendar invalid (%s)' % calst['err'])
    else:
        calst['err'] = err or ('HTTP %s' % code); warn('headlines: fed calendar failed (%s)' % calst['err'])
    stats['fed_calendar'] = calst
    ok_sources = [n for n, s in stats.items() if s.get('items') and not s.get('err')]
    return {'generatedAt': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'count': len(items_out), 'windowHours': KEEP_H,
            'sources': stats, 'items': items_out, 'fedCalendar': cal}, ok_sources, cal_ok(calst)

def cal_ok(st): return st.get('http') == 200 and not st.get('err')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True); ap.add_argument('--old'); ap.add_argument('--now')
    a = ap.parse_args()
    try:
        now = parse_dt(a.now) if a.now else datetime.now(timezone.utc)
        old = None
        for p in (a.old, a.out):
            if p and os.path.exists(p):
                try:
                    with open(p, encoding='utf-8') as f: old = json.load(f)
                    break
                except Exception: old = None
        res, ok_sources, calok = build(now, old)
        for n, s in res['sources'].items():
            print('%-22s http=%-3s items=%-3s kept=%-3s %s' % (n, s.get('http'), s.get('items'), s.get('kept', ''), s.get('err', '')))
        print('headlines: %d items from %d sources, fedCalendar %d' % (res['count'], len(ok_sources), len(res['fedCalendar'])))
        if not ok_sources or res['count'] == 0:
            warn('headlines: nothing usable this run - headlines.json left untouched'); return 0
        write = True
        if isinstance(old, dict) and old.get('items') is not None:
            same = (json.dumps(old.get('items'), sort_keys=True) == json.dumps(res['items'], sort_keys=True)
                    and json.dumps(old.get('fedCalendar', []), sort_keys=True) == json.dumps(res['fedCalendar'], sort_keys=True))
            ots = parse_dt(old.get('generatedAt'))
            age = (now - ots).total_seconds() / 60 if ots else 99999
            if same and age < HEARTBEAT_MIN:
                write = False; print('headlines unchanged and stored copy only %d min old -> not rewriting' % age)
        if write:
            with open(a.out, 'w', encoding='utf-8') as f: json.dump(res, f, ensure_ascii=False, indent=1)
            print('headlines.json written')
    except Exception as e:
        warn('headlines: unexpected error (%s) - skipped' % str(e)[:100])
    return 0

if __name__ == '__main__': sys.exit(main())
