#!/usr/bin/env python3
"""Regression guard for merge_news.py (added 2026-10-13 after the phantom 'Retail Sales MoM' 09:30 ET event).
usage: test_merge_news.py [merged.json [ff.json]]
  1) fixture tests (merge logic): TV twins of FF events (same/shifted minute, or same name same week) merge into FF,
     FF time kept; busy FOMC/NFP days keep every FF High and add no TV High next to them.
  2) merged file: FAIL only when the output is truly broken: not JSON, no events, bad schema, or a FF High event
     (from ff.json) missing / moved. A TV High within 3 h of a FF High is only a ::warning:: (merge_news.py already
     downgrades those), so the guard can never freeze the relay over a borderline TV event (fault 3, 2026-10-13).
  Exit 1 = the workflow step fails and news.json is NOT replaced."""
import json, os, subprocess, sys, tempfile
from datetime import datetime, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
def pdt(s):
    d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
def violations(events):
    ff = [e for e in events if e.get('src') != 'tv' and e.get('impact') == 'High']
    bad = []
    for e in events:
        if e.get('src') == 'tv' and e.get('impact') == 'High':
            for f in ff:
                if f.get('country') == e.get('country') and abs((pdt(f['date']) - pdt(e['date'])).total_seconds()) <= 3*3600:
                    bad.append((e['title'], e['date'], f['title'], f['date'])); break
    return bad
def run_merge(ff, tv, now):
    d = tempfile.mkdtemp(); p = lambda n: os.path.join(d, n)
    json.dump(ff, open(p('ff.json'), 'w')); json.dump(tv, open(p('tv.json'), 'w'))
    subprocess.run([sys.executable, os.path.join(HERE, 'merge_news.py'), '--out', p('o.json'), '--ff', p('ff.json'), '--tv', p('tv.json'), '--now', now], check=True, capture_output=True)
    return json.load(open(p('o.json')))['events']
def fixtures():
    ff = [{"title": "Retail Sales m/m", "country": "USD", "date": "2026-10-15T08:30:00-04:00", "impact": "High", "forecast": "0.3%", "previous": "1.2%"},
          {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High", "forecast": "0.6%", "previous": "0.4%"},
          {"title": "Fed Chairman Warsh Speaks", "country": "USD", "date": "2026-10-15T23:30:00-04:00", "impact": "High", "forecast": "", "previous": ""}]
    tv = {"status": "ok", "result": [
        {"country": "US", "title": "Retail Sales MoM", "date": "2026-10-15T13:30:00Z", "importance": 1, "actual": 0.4, "unit": "%"},
        {"country": "US", "title": "Inflation Rate MoM", "date": "2026-10-14T12:30:00Z", "importance": 1},
        {"country": "US", "title": "Fed Chair Warsh Speech", "date": "2026-10-16T03:30:00Z", "importance": 0},
        {"country": "US", "title": "FOMC Minutes", "date": "2026-10-14T18:00:00Z", "importance": 1}]}
    ev = run_merge(ff, tv, '2026-10-12T12:00:00Z')
    errs = []
    if violations(ev): errs.append('fixture: TV High twin added: %s' % violations(ev))
    for f in ff:
        if not any(e['title'] == f['title'] and e['date'] == f['date'] and e['impact'] == 'High' for e in ev): errs.append('fixture: FF event lost/shifted: ' + f['title'])
    if not any(e['title'] == 'FOMC Minutes' for e in ev): errs.append('fixture: real TV-only FOMC Minutes dropped')
    if sum(1 for e in ev if 'Warsh' in e['title']) != 1: errs.append('fixture: Warsh duplicate')
    rs = [e for e in ev if e['title'] == 'Retail Sales m/m']
    if not rs or rs[0].get('actual') != '0.4%': errs.append('fixture: TV actual not merged into FF event')
    return errs
def broken(path, ffpath=None):
    errs = []
    try: j = json.load(open(path))
    except Exception as ex: return ['merged file is not valid JSON: %s' % ex]
    ev = j.get('events') if isinstance(j, dict) else j
    if not isinstance(ev, list) or not ev: return ['merged file has no events']
    for e in ev:
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ('title', 'country', 'date', 'impact', 'forecast', 'previous')):
            errs.append('bad event schema: %r' % (e,)[:120]); break
        try: pdt(e['date'])
        except Exception: errs.append('bad date: %r' % e.get('date')); break
    if ffpath and os.path.exists(ffpath):
        try: ff = json.load(open(ffpath))
        except Exception: ff = []
        have = {(e.get('title'), e.get('country'), e.get('date'), e.get('impact')) for e in ev if isinstance(e, dict)}
        for f in ff if isinstance(ff, list) else []:
            if isinstance(f, dict) and f.get('impact') == 'High' and (f.get('title'), f.get('country'), f.get('date'), 'High') not in have:
                errs.append('FF High missing/moved: %s %s %s' % (f.get('country'), f.get('title'), f.get('date')))
    return errs
def busy_days():
    errs = []
    # FOMC day (Oct 28 2026): statement 14:00 ET + press conf 14:30 ET in FF; TV lists rate decision, statement, press conf
    ff = [{"title": "Federal Funds Rate", "country": "USD", "date": "2026-10-28T14:00:00-04:00", "impact": "High", "forecast": "", "previous": ""},
          {"title": "FOMC Statement", "country": "USD", "date": "2026-10-28T14:00:00-04:00", "impact": "High", "forecast": "", "previous": ""},
          {"title": "FOMC Press Conference", "country": "USD", "date": "2026-10-28T14:30:00-04:00", "impact": "High", "forecast": "", "previous": ""},
          {"title": "Advance GDP q/q", "country": "USD", "date": "2026-10-29T08:30:00-04:00", "impact": "High", "forecast": "", "previous": ""}]
    tv = {"status": "ok", "result": [
        {"country": "US", "title": "Fed Interest Rate Decision", "date": "2026-10-28T18:00:00Z", "importance": 1},
        {"country": "US", "title": "Fed Press Conference", "date": "2026-10-28T18:30:00Z", "importance": 1},
        {"country": "US", "title": "FOMC Economic Projections", "date": "2026-10-28T18:00:00Z", "importance": 1},
        {"country": "US", "title": "GDP Growth Rate QoQ Adv", "date": "2026-10-29T12:30:00Z", "importance": 1},
        {"country": "US", "title": "Core PCE Prices QoQ Adv", "date": "2026-10-29T12:30:00Z", "importance": 1}]}
    errs += busy_check('FOMC', ff, tv, '2026-10-26T12:00:00Z')
    # NFP day (Nov 6 2026, EST): FF NFP/UR/AHE 08:30 ET; TV lists them + one shifted +1 h
    ff = [{"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00", "impact": "High", "forecast": "", "previous": ""},
          {"title": "Unemployment Rate", "country": "USD", "date": "2026-11-06T08:30:00-05:00", "impact": "High", "forecast": "", "previous": ""},
          {"title": "Average Hourly Earnings m/m", "country": "USD", "date": "2026-11-06T08:30:00-05:00", "impact": "High", "forecast": "", "previous": ""},
          {"title": "ISM Services PMI", "country": "USD", "date": "2026-11-04T10:00:00-05:00", "impact": "Medium", "forecast": "", "previous": ""}]
    tv = {"status": "ok", "result": [
        {"country": "US", "title": "Non Farm Payrolls", "date": "2026-11-06T13:30:00Z", "importance": 1},
        {"country": "US", "title": "Unemployment Rate", "date": "2026-11-06T14:30:00Z", "importance": 1},
        {"country": "US", "title": "Average Hourly Earnings MoM", "date": "2026-11-06T13:30:00Z", "importance": 1},
        {"country": "US", "title": "Participation Rate", "date": "2026-11-06T13:30:00Z", "importance": 1},
        {"country": "US", "title": "ISM Services PMI", "date": "2026-11-04T15:00:00Z", "importance": 1}]}
    errs += busy_check('NFP', ff, tv, '2026-11-02T12:00:00Z')
    return errs
def busy_check(tag, ff, tv, now):
    ev = run_merge(ff, tv, now); errs = []
    for f in ff:
        if not any(e['title'] == f['title'] and e['date'] == f['date'] and e['impact'] == f['impact'] for e in ev): errs.append('%s: FF event lost/changed %s' % (tag, f['title']))
    if violations(ev): errs.append('%s: TV High next to FF High %s' % (tag, violations(ev)))
    if any(e.get('src') == 'tv' and similar_any(e, ff) for e in ev): errs.append('%s: TV twin of a FF event kept' % tag)
    return errs
def similar_any(e, ff):
    sys.path.insert(0, HERE); import merge_news as m
    return any(m.similar(e['title'], f['title']) for f in ff)
if __name__ == '__main__':
    errs = fixtures() + busy_days()
    if len(sys.argv) > 1:
        errs += broken(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
        try:
            j = json.load(open(sys.argv[1])); ev = j['events'] if isinstance(j, dict) else j
            for v in violations(ev): print('::warning::TV High %s @ %s within 3h of FF High %s @ %s' % v)
        except Exception: pass
    for e in errs: print('::error::' + e)
    print('merge regression: %s' % ('FAIL (%d)' % len(errs) if errs else 'PASS'))
    sys.exit(1 if errs else 0)
