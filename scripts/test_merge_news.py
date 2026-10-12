#!/usr/bin/env python3
"""Regression guard for merge_news.py (added 2026-10-13 after the phantom 'Retail Sales MoM' 09:30 ET event).
usage: test_merge_news.py [merged.json]
  1) fixture tests: TV twins of FF events (same or shifted minute) must merge into FF (FF time kept), never be added.
  2) if a merged file is given: FAIL if any TradingView-added (src=tv) High event lies within +/-3 h of a FF
     High event of the same country. Exit 1 = the workflow step fails and news.json is NOT replaced."""
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
if __name__ == '__main__':
    errs = fixtures()
    if len(sys.argv) > 1:
        j = json.load(open(sys.argv[1])); ev = j['events'] if isinstance(j, dict) else j
        for v in violations(ev): errs.append('merged file: TV High %s @ %s within 3h of FF High %s @ %s' % v)
    for e in errs: print('::error::' + e)
    print('merge regression: %s' % ('FAIL (%d)' % len(errs) if errs else 'PASS'))
    sys.exit(1 if errs else 0)
