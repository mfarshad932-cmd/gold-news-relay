# News relay — design note (2026-10-13)

## Rules
1. ForexFactory (FF) is the authority for USD events, times and impact. TradingView (TV) only adds `actual` to FF events and fills gaps. It never moves an FF time or changes an FF impact.
2. The guard (`scripts/test_merge_news.py`) only logs. It never blocks output. The one remaining refusal is the old jq schema check: a structurally invalid file is not written, and the last good `news.json` stays.
3. Staleness is reported through the bot's existing health states, with no Worker change:
   - relay older than 8 h → AGED, printed on every alert;
   - older than 24 h → NEWS DATA MISSING, plus a Telegram warning when NFP/CPI/FOMC is within 24 h;
   - the built-in NFP/CPI/FOMC list still blocks those release windows.

## The 5 faults: root cause and why the fix holds
| # | Fault | Root cause | Fix / why it does not come back |
|---|---|---|---|
| 1 | Relay ran only ~4x/day (gaps of 6-10 h) | GitHub drops or delays `schedule` runs on low-activity repos. A cron line cannot be made reliable. | Self-chain: each run waits about 28 min, then dispatches the next run with the built-in GITHUB_TOKEN (`workflow_dispatch` is allowed). No new secret. The cron stays as a restarter. Before dispatching, a run checks for another queued or running run, so there is never more than one chain. Heartbeat is 60 min, so `fetchedAt` age stays at 60 min or less while the chain runs. |
| 2 | Fake High event (TV "Retail Sales MoM" 09:30 ET vs FF 08:30) | The match required the same epoch second, so a TV event 1 h off became a second High event. | Match a similar title within ±3 h, keeping the FF time. A final pass drops any TV event whose release FF already lists that week, including events carried over from old files. |
| 3 | Guard could freeze the relay | The first guard failed the run on any TV High within 3 h of an FF High. | The guard only logs. Merge downgrades such a TV event to Medium (a TV event never widens the block set next to a real FF release). |
| 4 | TV raised an FF Medium to High (ISM Services, Oct 4) | TV-only append used TV's importance. | A TV event is dropped when FF lists the same release that week, so the FF impact wins. TV-only High stays only for real gaps, e.g. the Sunday before FF rolls its weekly file. |
| 5 | Duplicate Fed Chair speech | The speech alias did not cover "Fed Chairman X Speaks". | Alias extended, and the same-week drop covers any other name variant. |

## Failure modes covered (all are permanent fixtures in test_merge_news.py)
- **FF down, no old copy:** TV fills the gap, so the Worker still sees High events.
- **FF down, FF copy under 8 h old:** the FF copy is carried and TV is merged into it.
- **TV down:** FF only. Old TV-only events are re-checked, so phantoms are dropped.
- **Both down:** merge refuses and `news.json` is untouched. The Worker shows AGED, then MISSING (built-in big-3 backstop).
- **Schedule stall:** the cron restarts the chain. A stall over 8 h shows AGED on alerts.
- **Busy days:** FOMC (rate, statement, press conference, projections) and NFP (with one TV event shifted +1 h). All FF Highs are kept and no TV High lands next to them.
- **DST weeks:** EU-only week (Oct 25 to Nov 1) and US EST week. Times are absolute ISO with offsets, and ±3 h covers a 1 h DST skew.
- **Week rollover:** the Sunday FF file is still last week's, so this week's TV Highs are kept as gap-fill until FF rolls. Historically FF rolled before the Sunday 22:00 UTC market open.

## Evidence
- Replay of every relay snapshot in git (59): 0 FF events lost or changed, 0 TV twins of an FF release, 0 TV High within 3 h of an FF High. The TV-only High count dropped from 1106 under the old script to 8 genuine gap-fills.
- The old script fails 7 of the fixture/edge checks; the new script passes all of them.
