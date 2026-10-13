"""Comparison B, stronger form (issue #138): rebuild v1's flights from v1's RAW positions and compare them with flight_record.

Why: v1's summary table (flight_summaries) is incomplete (its summary job omitted about a dozen flights that v1's own raw rows
show), so it is a weak reference. v1's raw table `flights` holds every position v1 saw. Rule used, v1's own: key
(cid, callsign, departure, arrival), a new flight when two rows of one key are more than 120 minutes apart; rows without both
departure and arrival are ignored (v1 drops them at ingest).

Run from a folder holding the exports (all READ ONLY SELECTs, see README.md section 2e for the commands):
  v1raw_<n>.csv   v1 raw rows, 6 hour chunks: cid, callsign, departure, arrival, last_updated  (ssh vatpac, container vatsim_postgres)
  b_v3_wide.csv   flight_record rows: cid, callsign, departure, arrival, logon_time, start_time, end_time, session_id,
                  session_count, sample_count, with end_time >= window start - 2 h and start_time < window end + 2 h (NO logon or
                  power-cut filter: those are applied here, on both sides alike)
Window: both systems collecting. v1 polled at least 30 times an hour from 08-10-2026 08:00 to 11-10-2026 03:00 UTC (it was off
11-10 03:00 to 12-10 about 15:30); the joint power cut 09-10 15:30-15:50 and 30 minutes at each edge are excluded from both
sides. Edit W0, W1, CUT0, CUT1 for another window. Read the result with README.md section 2f.
"""
import csv,glob,collections
from datetime import datetime,timedelta,timezone
def ts(s):
    s=s.strip().replace(' ','T')
    if s.endswith('+00'): s+=':00'
    return datetime.fromisoformat(s)
W0=ts('2026-10-08 08:00:00+00'); W1=ts('2026-10-11 03:00:00+00')
CUT0=ts('2026-10-09 15:30:00+00'); CUT1=ts('2026-10-09 15:50:00+00')
EDGE=timedelta(minutes=30)
# v1 raw -> flights by v1's rule (key + split on a 120 minute gap)
rows=collections.defaultdict(set)
for f in glob.glob('v1raw_*.csv'):
    for r in csv.DictReader(open(f,encoding='utf-8')):
        rows[(int(r['cid']),r['callsign'],r['departure'].strip().upper(),r['arrival'].strip().upper())].add(ts(r['last_updated']))
v1=[]
for k,s in rows.items():
    s=sorted(s); a=s[0]; p=s[0]; n=1
    for t in s[1:]:
        if t-p>timedelta(minutes=120): v1.append((k,a,p,n)); a=t; n=0
        p=t; n+=1
    v1.append((k,a,p,n))
# v3 flights
v3=[]
for r in csv.DictReader(open('b_v3_wide.csv',encoding='utf-8')):
    v3.append(((int(r['cid']),r['callsign'],r['departure'].strip().upper(),r['arrival'].strip().upper()),ts(r['start_time']),ts(r['end_time']),int(r['sample_count']),r['session_id']))
def excluded(a,b):
    return a<W0+EDGE or b>W1-EDGE or (a<CUT1 and b>CUT0)
v1x=[f for f in v1 if not excluded(f[1],f[2])]
v3x=[f for f in v3 if not excluded(f[1],f[2])]
by3=collections.defaultdict(list)
for f in v3x: by3[f[0]].append(f)
by1=collections.defaultdict(list)
for f in v1x: by1[f[0]].append(f)
TOL=timedelta(minutes=3)
def overl(a0,a1,b0,b1): return a0<=b1+TOL and b0<=a1+TOL
m=0;split=0;v1only=[];merge=0
used3=set()
for f in v1x:
    c=[g for g in by3.get(f[0],[]) if overl(f[1],f[2],g[1],g[2])]
    if len(c)==1: m+=1; used3.add(id(c[0]))
    elif len(c)>1: split+=1; [used3.add(id(g)) for g in c]
    else: v1only.append(f)
v3only=[g for g in v3x if id(g) not in used3]
print('v1 flights rebuilt from raw (all):',len(v1),' after edge/power-cut exclusion:',len(v1x))
print('v3 flights after same exclusion:',len(v3x))
print('v1raw flight = exactly one v3 flight overlapping (same key):',m)
print('v1raw flight covered by 2+ v3 flights (v3 splits):',split)
print('v1raw only (no v3 flight with that key overlaps):',len(v1only))
print('v3 only (no v1 raw flight with that key overlaps):',len(v3only))
def show(l,tag):
    for x in l[:40]:
        k=x[0]; print(' ',tag,k[0],k[1],k[2]+'-'+k[3],x[1].strftime('%d-%m %H:%M'),'->',x[2].strftime('%d-%m %H:%M'),'n=',x[3])
show(v1only,'v1raw-only'); show(v3only,'v3-only')
print('--- amendment check ---')
by3c=collections.defaultdict(list)
for g in v3: by3c[(g[0][0],g[0][1])].append(g)
by1c=collections.defaultdict(list)
for f in v1: by1c[(f[0][0],f[0][1])].append(f)
amend=0;other=[]
for f in v1only:
    c=[g for g in by3c.get((f[0][0],f[0][1]),[]) if overl(f[1],f[2],g[1],g[2]) and g[0]!=f[0]]
    if c: amend+=1
    else: other.append(f)
print('v1raw-only explained as plan amendment (same pilot+callsign overlapping v3 flight with another departure/arrival):',amend,' unexplained:',len(other))
for x in other: print('  unexplained v1raw-only',x[0],x[1],x[2],x[3])
am3=0;oth3=[]
for g in v3only:
    c=[f for f in by1c.get((g[0][0],g[0][1]),[]) if overl(f[1],f[2],g[1],g[2]) and f[0]!=g[0]]
    (am3:=am3+1) if c else oth3.append(g)
print('v3-only explained as plan amendment:',am3,' no v1 raw at all:',len(oth3))
for x in oth3: print('  v3-only without any v1 raw',x[0],x[1],x[2],'n=',x[3])
print('--- timing agreement on the one-to-one matches ---')
import statistics,random
diffs=[]
for f in v1x:
    c=[g for g in by3.get(f[0],[]) if overl(f[1],f[2],g[1],g[2])]
    if len(c)==1:
        g=c[0]; diffs.append(((f[1]-g[1]).total_seconds()/60,(g[2]-f[2]).total_seconds()/60,f,g))
# v1 sees fewer polls so v1 start >= v3 start and v1 end <= v3 end expected, by up to about 1.5 min each side
def pct(vals,p): v=sorted(vals); return v[int(p*(len(v)-1))]
ds=[d[0] for d in diffs]; de=[d[1] for d in diffs]
print('v1 first row minus v3 start (min): median %.1f  p95 %.1f  max %.1f ; v1 earlier than v3 by >1.5 min: %d'%(statistics.median(ds),pct(ds,.95),max(ds),sum(1 for x in ds if x<-1.5)))
print('v3 end minus v1 last row (min):   median %.1f  p95 %.1f  max %.1f ; v1 later than v3 by >1.5 min: %d'%(statistics.median(de),pct(de,.95),max(de),sum(1 for x in de if x<-1.5)))
big=[d for d in diffs if abs(d[0])>5 or abs(d[1])>5]
print('flights where start or end differs by more than 5 minutes:',len(big),'of',len(diffs))
for d in sorted(big,key=lambda d:-max(abs(d[0]),abs(d[1])))[:12]:
    f,g=d[2],d[3]; print('  ',f[0][0],f[0][1],f[0][2]+'-'+f[0][3],'start diff %.1f end diff %.1f'%(d[0],d[1]),'v1 n=%d v3 n=%d'%(f[3],g[3]))
