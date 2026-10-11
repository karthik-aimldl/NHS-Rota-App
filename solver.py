"""Rota solver (OR-Tools CP-SAT) and basic rota checks."""
from datetime import timedelta, date
from ortools.sat.python import cp_model

SHIFTS = ['SD', 'LD', 'LN']
SHIFT_LABELS = {'SD': 'Short Days', 'LD': 'Long Days', 'LN': 'Long Nights', 'TOTAL': 'All working days'}
# Fairness penalty per 1/100th of a shift of difference between the busiest and quietest doctor
# in the same group. Long Days and Long Nights are weighted highest because of the long hours.
FAIRNESS_WEIGHTS = {'LD': 8, 'LN': 8, 'SD': 3, 'TOTAL': 5}
EXTRA_COVER_PENALTY = 25      # discourages staffing a shift above the minimum
MIN_AVAILABLE_DAYS = 7        # doctors with less availability than this are left out of fairness balancing


def _add_spread(m, tag, items, weight, terms, big):
    """items: [(count_expression, capacity_percent)]. Minimises max-min of count per unit of capacity."""
    if len(items) < 2:
        return
    hi = m.NewIntVar(0, big, f'hi_{tag}')
    lo = m.NewIntVar(0, big, f'lo_{tag}')
    for cnt, cap in items:
        m.Add(hi * cap >= cnt * 10000)
        m.Add(lo * cap <= cnt * 10000)
    terms.append(weight * (hi - lo))


def generate_rota(doctors, leaves, start, weeks):
    active = [d for d in doctors if d['status'] == 'Active']
    days = [start + timedelta(days=i) for i in range(weeks * 7)]
    period = len(days)
    if not any(d['rota_group'] == 'Junior' for d in active) or not any(d['rota_group'] == 'Senior' for d in active):
        return {'ok': False, 'message': 'At least one active Junior and one active Senior doctor are required.'}

    unavailable = set()
    for l in leaves:
        a, b = date.fromisoformat(l['start_date']), date.fromisoformat(l['end_date'])
        for day in days:
            if a <= day <= b:
                unavailable.add((int(l['doctor_id']), day.isoformat()))

    m = cp_model.CpModel()
    x, w, avail = {}, {}, {}
    for d in active:
        did = int(d['id'])
        avail[did] = 0
        for i, day in enumerate(days):
            w[did, i] = m.NewBoolVar(f'w_{did}_{i}')
            for s in SHIFTS:
                x[did, i, s] = m.NewBoolVar(f'x_{did}_{i}_{s}')
            m.Add(sum(x[did, i, s] for s in SHIFTS) == w[did, i])
            blocked = (did, day.isoformat()) in unavailable or (
                d.get('last_working_date') and day.isoformat() > d['last_working_date'])
            if blocked:
                m.Add(w[did, i] == 0)
            else:
                avail[did] += 1
        for i in range(period):
            if i + 6 <= period:
                m.Add(sum(x[did, j, 'SD'] for j in range(i, i + 6)) <= 5)
            if i + 4 <= period:
                m.Add(sum(x[did, j, 'LD'] for j in range(i, i + 4)) <= 3)
                m.Add(sum(x[did, j, 'LN'] for j in range(i, i + 4)) <= 3)
                m.Add(sum(x[did, j, 'LN'] for j in range(i, i + 3)) <= 2 + (1 - w[did, i + 3]))
        # After a Long Night, the next day can only be another Long Night (or a day off) - never SD or LD.
        for i in range(period - 1):
            m.Add(x[did, i, 'LN'] + x[did, i + 1, 'SD'] <= 1)
            m.Add(x[did, i, 'LN'] + x[did, i + 1, 'LD'] <= 1)

    # Cover: each shift needs >=1 Junior, >=1 Senior, 2-4 doctors in total.
    for i in range(period):
        for s in SHIFTS:
            allx = [x[int(d['id']), i, s] for d in active]
            jun = [x[int(d['id']), i, s] for d in active if d['rota_group'] == 'Junior']
            sen = [x[int(d['id']), i, s] for d in active if d['rota_group'] == 'Senior']
            m.Add(sum(jun) >= 1)
            m.Add(sum(sen) >= 1)
            m.Add(sum(allx) >= 2)
            m.Add(sum(allx) <= 4)

    terms = [EXTRA_COVER_PENALTY * sum(w[int(d['id']), i] for d in active for i in range(period))]
    big = period * 10000 + 10
    for group in ('Junior', 'Senior'):
        members = [d for d in active if d['rota_group'] == group and avail[int(d['id'])] >= MIN_AVAILABLE_DAYS]
        caps = {}
        for d in members:
            fte = max(.1, float(d.get('fte') or 1))
            caps[int(d['id'])] = max(1, round(100 * fte * avail[int(d['id'])] / period))
        for metric, weight in FAIRNESS_WEIGHTS.items():
            items = []
            for d in members:
                did = int(d['id'])
                cnt = sum(w[did, i] for i in range(period)) if metric == 'TOTAL' else sum(x[did, i, metric] for i in range(period))
                items.append((cnt, caps[did]))
            _add_spread(m, f'{group}_{metric}', items, weight, terms, big)
    m.Minimize(sum(terms))

    sol = cp_model.CpSolver()
    sol.parameters.max_time_in_seconds = 12
    sol.parameters.relative_gap_limit = 0.10
    sol.parameters.num_search_workers = 2
    status = sol.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {'ok': False, 'message': 'No feasible rota found. Check staffing, leave and last-working dates.'}
    out = []
    for d in active:
        did = int(d['id'])
        for i, day in enumerate(days):
            for s in SHIFTS:
                if sol.Value(x[did, i, s]):
                    out.append({'date': day.isoformat(), 'doctor_id': did, 'shift': s})
    return {'ok': True, 'assignments': out, 'status': sol.StatusName(status)}


def validate_rota(rows, doctors, leaves):
    issues = []
    by = {}
    for r in rows:
        by.setdefault((r['rota_date'], r['shift_code']), []).append(r)
    for (day, shift), items in sorted(by.items()):
        groups = {x.get('rota_group') for x in items}
        if 'Junior' not in groups or 'Senior' not in groups:
            issues.append(f'{day} {shift}: missing Junior or Senior coverage.')
        if len(items) > 4:
            issues.append(f'{day} {shift}: more than 4 doctors assigned.')
    seq = {}
    for r in rows:
        seq.setdefault(r['doctor_id'], {})[r['rota_date']] = (r['shift_code'], r.get('name', ''))
    for did, days_ in seq.items():
        for day, (sh, nm) in sorted(days_.items()):
            nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            if sh == 'LN' and nxt in days_ and days_[nxt][0] in ('SD', 'LD'):
                issues.append(f"{nm}: {days_[nxt][0]} on {nxt} directly after Long Night on {day}; only LN or a day off is allowed.")
    if not issues:
        issues.append('No basic coverage issues detected. This is not a complete compliance validation.')
    return issues


def fairness_report(rows, doctors):
    """Per group and shift type: lowest, highest and spread of shifts per doctor (raw counts)."""
    counts = {}
    for r in rows:
        c = counts.setdefault(r['doctor_id'], {'SD': 0, 'LD': 0, 'LN': 0})
        if r['shift_code'] in c:
            c[r['shift_code']] += 1
    out = []
    for group in ('Junior', 'Senior'):
        members = [d for d in doctors if d['rota_group'] == group and (d['status'] == 'Active' or d['id'] in counts)]
        if len(members) < 2:
            continue
        for metric in ('LD', 'LN', 'SD', 'TOTAL'):
            vals = []
            for d in members:
                c = counts.get(d['id'], {'SD': 0, 'LD': 0, 'LN': 0})
                vals.append(sum(c.values()) if metric == 'TOTAL' else c[metric])
            out.append({'group': group, 'metric': SHIFT_LABELS[metric], 'min': min(vals), 'max': max(vals),
                        'spread': max(vals) - min(vals), 'doctors': len(members)})
    return out
