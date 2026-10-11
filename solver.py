from datetime import timedelta, date
from ortools.sat.python import cp_model
SHIFTS=['SD','LD','LN']
def generate_rota(doctors,leaves,start,weeks):
 active=[d for d in doctors if d['status']=='Active']; days=[start+timedelta(days=i) for i in range(weeks*7)]
 if not any(d['rota_group']=='Junior' for d in active) or not any(d['rota_group']=='Senior' for d in active):return {'ok':False,'message':'At least one active Junior and Senior are required.'}
 unavailable=set()
 for l in leaves:
  a=date.fromisoformat(l['start_date']);b=date.fromisoformat(l['end_date'])
  for day in days:
   if a<=day<=b:unavailable.add((int(l['doctor_id']),day.isoformat()))
 m=cp_model.CpModel();x={};w={}
 for d in active:
  did=int(d['id'])
  for i,day in enumerate(days):
   w[did,i]=m.NewBoolVar(f'w_{did}_{i}')
   for s in SHIFTS:x[did,i,s]=m.NewBoolVar(f'x_{did}_{i}_{s}')
   m.Add(sum(x[did,i,s] for s in SHIFTS)==w[did,i])
   if (did,day.isoformat()) in unavailable or (d.get('last_working_date') and day.isoformat()>d['last_working_date']):m.Add(w[did,i]==0)
  for i in range(len(days)):
   if i+6<=len(days):m.Add(sum(x[did,j,'SD'] for j in range(i,i+6))<=5)
   if i+4<=len(days):
    m.Add(sum(x[did,j,'LD'] for j in range(i,i+4))<=3);m.Add(sum(x[did,j,'LN'] for j in range(i,i+4))<=3)
   if i+4<=len(days):m.Add(sum(x[did,j,'LN'] for j in range(i,i+3))<=2+(1-w[did,i+3]))
  # After a Long Night, the next day can only be another Long Night (or a day off) - never SD or LD.
  for i in range(len(days)-1):
   m.Add(x[did,i,'LN']+x[did,i+1,'SD']<=1);m.Add(x[did,i,'LN']+x[did,i+1,'LD']<=1)
 for i,day in enumerate(days):
  for s in SHIFTS:
   allx=[x[int(d['id']),i,s] for d in active];jun=[x[int(d['id']),i,s] for d in active if d['rota_group']=='Junior'];sen=[x[int(d['id']),i,s] for d in active if d['rota_group']=='Senior']
   m.Add(sum(jun)>=1);m.Add(sum(sen)>=1);m.Add(sum(allx)>=2);m.Add(sum(allx)<=4)
 terms=[];ftes=sum(max(.1,float(d.get('fte') or 1)) for d in active);period=len(days)
 for d in active:
  did=int(d['id']);target=max(1,round(period*3*max(.1,float(d.get('fte') or 1))/ftes))
  count=sum(w[did,i] for i in range(period));dev=m.NewIntVar(0,period,f'dev_{did}');m.Add(count-target<=dev);m.Add(target-count<=dev);terms.append(dev*4)
  nt=max(1,round(period*2*max(.1,float(d.get('fte') or 1))/ftes));nc=sum(x[did,i,'LN'] for i in range(period));nd=m.NewIntVar(0,period,f'nd_{did}');m.Add(nc-nt<=nd);m.Add(nt-nc<=nd);terms.append(nd*2)
 m.Minimize(sum(terms));sol=cp_model.CpSolver();sol.parameters.max_time_in_seconds=15;sol.parameters.num_search_workers=2;status=sol.Solve(m)
 if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):return {'ok':False,'message':'No feasible rota found. Check staffing, leave and last-working dates.'}
 out=[]
 for d in active:
  did=int(d['id'])
  for i,day in enumerate(days):
   for s in SHIFTS:
    if sol.Value(x[did,i,s]):out.append({'date':day.isoformat(),'doctor_id':did,'shift':s})
 return {'ok':True,'assignments':out,'status':sol.StatusName(status)}
def validate_rota(rows,doctors,leaves):
 issues=[];by={}
 for r in rows:by.setdefault((r['rota_date'],r['shift_code']),[]).append(r)
 for (day,shift),items in sorted(by.items()):
  groups={x.get('rota_group') for x in items}
  if 'Junior' not in groups or 'Senior' not in groups:issues.append(f'{day} {shift}: missing Junior or Senior coverage.')
  if len(items)>4:issues.append(f'{day} {shift}: more than 4 doctors assigned.')
 seq={}
 for r in rows:seq.setdefault(r['doctor_id'],{})[r['rota_date']]=(r['shift_code'],r.get('name',''))
 for did,days_ in seq.items():
  for day,(sh,nm) in sorted(days_.items()):
   nxt=(date.fromisoformat(day)+timedelta(days=1)).isoformat()
   if sh=='LN' and nxt in days_ and days_[nxt][0] in ('SD','LD'):issues.append(f"{nm}: {days_[nxt][0]} on {nxt} directly after Long Night on {day}; only LN or a day off is allowed.")
 if not issues:issues.append('No basic coverage issues detected. This is not a complete compliance validation.')
 return issues
