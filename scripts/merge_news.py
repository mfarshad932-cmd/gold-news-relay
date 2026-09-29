#!/usr/bin/env python3
"""Merge ForexFactory calendar (primary) with TradingView US events (best-effort) into the
FF-style event schema the Worker parses: title,country,date,impact,forecast,previous (+ optional actual).

usage: merge_news.py --out merged.json [--ff raw.json] [--tv tv.json] [--old news.json] [--now ISO]
Never raises on bad optional inputs: an unreadable/invalid --tv or --ff is treated as 'absent'.
Exit 0 + writes {"events":[...], "meta":{...}} to --out; exit 2 if there is nothing usable at all.
"""
import argparse, json, re, sys
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo('America/New_York')
except Exception:
    NY = timezone(timedelta(hours=-4))

CARRY_MAX_H = 8   # same as the Worker's NEWS_RELAY_MAX_MIN (480): never present FF data older than this as fresh

def load(path):
    if not path: return None
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except Exception: return None

def parse_dt(s):
    try:
        s = str(s).replace('Z', '+00:00')
        d = datetime.fromisoformat(s)
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception: return None

ALIASES = [  # (regex on normalised title) -> canonical key. Applied to BOTH FF and TV titles. ORDER MATTERS (adp before nfp).
    (r'^(fomc member|fed) (?P<who>[a-z]+) (speaks|speech)$', 'SPEECH'),
    (r'\badp (non ?farm )?employment change\b', 'adp'),
    (r'\bnon ?farm (payrolls|employment change)\b|\bnonfarm payrolls\b', 'nfp'),
    (r'\b(initial jobless claims|unemployment claims)\b', 'claims'),
    (r'\bjolts job openings\b|\bjolts\b', 'jolts'),
    (r'\bcore inflation rate mom\b|\bcore cpi mom\b', 'core cpi mom'),
    (r'\bcore inflation rate yoy\b|\bcore cpi yoy\b', 'core cpi yoy'),
    (r'\binflation rate mom\b|\bcpi mom\b', 'cpi mom'),
    (r'\binflation rate yoy\b|\bcpi yoy\b', 'cpi yoy'),
    (r'\bfomc (meeting )?minutes\b', 'fomc minutes'),
    (r'\b(federal funds rate|fed interest rate decision|interest rate decision)\b', 'fed rate decision'),
    (r'\b(michigan consumer sentiment|uom consumer sentiment|prelim uom consumer sentiment).*$', 'umich sentiment'),
    (r'\bgdp (growth rate )?(qoq|annualized)\b', 'gdp qoq'),
]
KEY_TV_ONLY = re.compile(r'fomc|fed interest|federal funds|non ?farm|payroll|\bcpi\b|inflation rate|\bpce\b|\bgdp\b|\bppi\b|retail sales|\bism\b|jolts|unemployment rate|jobless|claims', re.I)

def canon(title):
    t = str(title or '').lower()
    t = re.sub(r'\bm/m\b', 'mom', t); t = re.sub(r'\bq/q\b', 'qoq', t); t = re.sub(r'\by/y\b', 'yoy', t)
    t = re.sub(r'[-–—/.,()]', ' ', t)
    t = re.sub(r'\b(final|adv|advance|prelim|preliminary|flash|revised|p|r|f)\b', ' ', t)
    t = re.sub(r'\bgrowth rate\b', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    for rx, key in ALIASES:
        m = re.search(rx, t)
        if m: return ('speech ' + m.group('who')) if key == 'SPEECH' else key
    return t

def toks(t): return set(w for w in canon(t).split() if w)

def similar(a, b):
    ca, cb = canon(a), canon(b)
    if ca == cb: return True
    A, B = toks(a), toks(b)
    return bool(A and B) and len(A & B) / len(A | B) >= 0.75

def fmt_num(v, unit, scale):
    """FF style: 7.079M / 4.1% / 162K. The Worker strips everything but digits, '.', '-' before comparing."""
    if v is None or isinstance(v, bool): return ''
    try: n = float(v)
    except Exception: return ''
    s = ('%.6f' % n).rstrip('0').rstrip('.')
    if s in ('', '-0'): s = '0'
    return s + (scale or '') + ('%' if unit == '%' else '')

def tv_events(tv):
    out = []
    res = tv.get('result') if isinstance(tv, dict) else None
    if not isinstance(res, list): return out
    for r in res:
        if not isinstance(r, dict) or r.get('country') != 'US': continue
        d = parse_dt(r.get('date'))
        imp = r.get('importance')
        if d is None or imp not in (0, 1): continue      # -1 = low: ignored
        unit, scale = r.get('unit'), r.get('scale')
        out.append({'title': str(r.get('title') or ''), 'dt': d, 'imp': imp,
                    'actual': fmt_num(r.get('actual'), unit, scale),
                    'forecast': fmt_num(r.get('forecast'), unit, scale),
                    'previous': fmt_num(r.get('previous'), unit, scale)})
    return out

def week_window(now):
    """Sunday 00:00 UTC of the current week -> +7 days (covers FF 'this week', which starts Sunday evening ET)."""
    d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = d0 - timedelta(days=(d0.weekday() + 1) % 7)
    return start, start + timedelta(days=7)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out'); ap.add_argument('--print-window', action='store_true'); ap.add_argument('--ff'); ap.add_argument('--tv'); ap.add_argument('--old'); ap.add_argument('--now')
    a = ap.parse_args()
    now = parse_dt(a.now) if a.now else datetime.now(timezone.utc)
    ws, we = week_window(now)
    if a.print_window:
        print(ws.strftime('%Y-%m-%dT%H:%M:%S.000Z'), (we + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')); return 0
    if not a.out: sys.stderr.write('--out required\n'); return 2
    meta = {'ff': False, 'ffMode': 'none', 'tv': False, 'ffCarried': False, 'tvMerged': 0, 'tvAppended': 0, 'tvEvents': 0}

    ff = load(a.ff)
    base = None
    if isinstance(ff, list) and any(isinstance(e, dict) and e.get('date') and e.get('title') and e.get('country') for e in ff):
        base = [dict(e) for e in ff if isinstance(e, dict)]
        meta['ff'] = True; meta['ffMode'] = 'fresh'
    tv = load(a.tv)
    tvs = tv_events(tv) if tv else []
    if isinstance(tv, dict) and tv.get('status') == 'ok' and isinstance(tv.get('result'), list): meta['tv'] = True
    meta['tvEvents'] = len(tvs)

    if base is None and meta['tv']:
        # FF down: carry the stored FF-derived events of THIS week if the stored FF copy is < CARRY_MAX_H old, then layer TV on top.
        old = load(a.old)
        if isinstance(old, dict) and isinstance(old.get('events'), list):
            ref = parse_dt(old.get('ffFetchedAt') or old.get('fetchedAt'))
            if ref and (now - ref) < timedelta(hours=CARRY_MAX_H):
                keep = []
                for e in old['events']:
                    d = parse_dt(e.get('date')) if isinstance(e, dict) else None
                    if d and ws <= d < we: keep.append(dict(e))
                if keep: base = keep; meta['ffCarried'] = True; meta['ffMode'] = 'carried'
        if base is None: base = []
    if base is None:
        sys.stderr.write('no usable FF or TV data\n'); return 2

    # index FF/base USD events by epoch
    idx = {}
    for i, e in enumerate(base):
        if e.get('country') != 'USD': continue
        d = parse_dt(e.get('date'))
        if d: idx.setdefault(int(d.timestamp()), []).append(i)
    ff_down = (not meta['ff']) and (not meta['ffCarried'])
    for t in tvs:
        hit = None
        for i in idx.get(int(t['dt'].timestamp()), []):
            if similar(base[i].get('title'), t['title']): hit = i; break
        if hit is not None:
            e = base[hit]
            if t['actual'] != '': e['actual'] = t['actual']
            if not e.get('forecast') and t['forecast']: e['forecast'] = t['forecast']
            if not e.get('previous') and t['previous']: e['previous'] = t['previous']
            meta['tvMerged'] += 1
        elif ws <= t['dt'] < we and (t['imp'] == 1 or ff_down):
            # TV-only event. With FF healthy it is appended only if TV rates it High; it is labelled High only when it is a
            # recognised key release (FOMC minutes etc.), otherwise Medium, so TV cannot silently widen the Worker's block set.
            impact = 'High' if (t['imp'] == 1 and (ff_down or KEY_TV_ONLY.search(t['title']))) else 'Medium'
            if ff_down and t['imp'] == 0: impact = 'Medium'
            ne = {'title': t['title'], 'country': 'USD',
                  'date': t['dt'].astimezone(NY).isoformat(),
                  'impact': impact, 'forecast': t['forecast'], 'previous': t['previous']}
            if t['actual'] != '': ne['actual'] = t['actual']
            ne['src'] = 'tv'      # marks a TradingView-only event (carried forward if TV is briefly unavailable)
            base.append(ne); meta['tvAppended'] += 1
    # TV unavailable this run: do not let a flaky TV call erase values we already had (prevents commit flapping too).
    if not meta['tv']:
        old = load(a.old)
        oe = old.get('events') if isinstance(old, dict) else None
        if isinstance(oe, list):
            have = {(str(e.get('title')), str(e.get('date'))): e for e in base}
            for e in oe:
                if not isinstance(e, dict): continue
                k = (str(e.get('title')), str(e.get('date')))
                if k in have:
                    if e.get('actual') and not have[k].get('actual'): have[k]['actual'] = e['actual']
                elif e.get('src') == 'tv':
                    d = parse_dt(e.get('date'))
                    if d and ws <= d < we: base.append(dict(e)); meta['tvCarried'] = meta.get('tvCarried', 0) + 1
    base.sort(key=lambda e: (parse_dt(e.get('date')) or datetime.max.replace(tzinfo=timezone.utc)))
    # canonical key order, actual last; unknown extra FF keys preserved
    order = ['title', 'country', 'date', 'impact', 'forecast', 'previous', 'actual']
    ev = []
    for e in base:
        o = {k: e[k] for k in order if k in e}
        for k in e:
            if k not in o: o[k] = e[k]
        for k in ('forecast', 'previous', 'impact'):
            if not isinstance(o.get(k), str): o[k] = ''
        ev.append(o)
    # when the FF part was last REALLY fetched (now if fresh, carried from the stored copy otherwise)
    ff_at = None
    if meta['ffMode'] == 'fresh': ff_at = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    elif meta['ffMode'] == 'carried':
        o = load(a.old); ff_at = (o.get('ffFetchedAt') or o.get('fetchedAt')) if isinstance(o, dict) else None
    with open(a.out, 'w', encoding='utf-8') as f: json.dump({'events': ev, 'meta': meta, 'ffFetchedAt': ff_at}, f, ensure_ascii=False)
    print(json.dumps(meta)); return 0

if __name__ == '__main__': sys.exit(main())
