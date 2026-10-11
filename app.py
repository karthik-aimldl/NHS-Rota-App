from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import sqlite3, csv, io, os, calendar
from pathlib import Path
from datetime import date, timedelta
from collections import OrderedDict
from solver import generate_rota, validate_rota, fairness_report

ROOT = Path(__file__).resolve().parent
SQLITE_PATH = ROOT / 'data' / 'rota.db'
SQLITE_PATH.parent.mkdir(exist_ok=True)


def _clean_url(v):
    v = (v or '').strip()
    if v.lower().startswith('psql'):          # Neon's "psql 'postgresql://...'" snippet pasted whole
        v = v[4:].strip()
    return v.strip('\'"` ')


# If DATABASE_URL is set (e.g. a free Neon PostgreSQL) data survives restarts and redeploys.
# Otherwise the app falls back to a local SQLite file (temporary on free hosting).
DATABASE_URL = _clean_url(os.environ.get('DATABASE_URL', ''))
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = 'postgresql://' + DATABASE_URL[len('postgres://'):]
USE_PG = bool(DATABASE_URL)
if USE_PG:
    import psycopg2, psycopg2.extras

app = Flask(__name__, template_folder='app/templates', static_folder='app/static')
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-prototype-secret')

SHIFT_NAMES = {'SD': 'Short Day', 'LD': 'Long Day', 'LN': 'Long Night'}
MAX_RESULTS = 1000


# ---------- database helpers (SQLite and PostgreSQL) ----------
def _connect():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL, connect_timeout=10)
    c = sqlite3.connect(SQLITE_PATH)
    c.row_factory = sqlite3.Row
    return c


def _sql(sql):
    return sql.replace('?', '%s') if USE_PG else sql


def query(sql, params=()):
    c = _connect()
    try:
        if USE_PG:
            cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(_sql(sql), params)
            return [dict(r) for r in cur.fetchall()]
        return [dict(r) for r in c.execute(sql, params)]
    finally:
        c.close()


def execute(statements):
    """statements: list of (sql, params) or (sql, [params,...], True) for executemany. One transaction."""
    c = _connect()
    try:
        cur = c.cursor()
        for item in statements:
            sql, params = item[0], item[1]
            if len(item) == 3:
                cur.executemany(_sql(sql), params)
            else:
                cur.execute(_sql(sql), params)
        c.commit()
    finally:
        c.close()


def _has_column(table, column):
    if USE_PG:
        return bool(query("SELECT 1 AS x FROM information_schema.columns WHERE table_name=? AND column_name=?", (table, column)))
    return column in [r['name'] for r in query(f'PRAGMA table_info({table})')]


def init():
    pk = 'SERIAL PRIMARY KEY' if USE_PG else 'INTEGER PRIMARY KEY'
    execute([
        (f'CREATE TABLE IF NOT EXISTS trusts(id {pk},name TEXT)', ()),
        (f'CREATE TABLE IF NOT EXISTS departments(id {pk},trust_id INTEGER,name TEXT)', ()),
        (f'CREATE TABLE IF NOT EXISTS doctors(id {pk},name TEXT,designation TEXT,rota_group TEXT,fte REAL,last_working_date TEXT,status TEXT,department_id INTEGER)', ()),
        (f'CREATE TABLE IF NOT EXISTS leaves(id {pk},doctor_id INTEGER,leave_type TEXT,start_date TEXT,end_date TEXT)', ()),
        (f'CREATE TABLE IF NOT EXISTS rota(id {pk},rota_date TEXT,doctor_id INTEGER,shift_code TEXT)', ()),
    ])
    if not _has_column('doctors', 'department_id'):          # upgrade a database created by an older version
        execute([('ALTER TABLE doctors ADD COLUMN department_id INTEGER', ())])
    if not query('SELECT id FROM trusts LIMIT 1'):
        execute([('INSERT INTO trusts(name) VALUES(?)', ('Example NHS Trust',))])
        tid = query('SELECT id FROM trusts ORDER BY id LIMIT 1')[0]['id']
        execute([('INSERT INTO departments(trust_id,name) VALUES(?,?)', (tid, 'Example Department'))])
    first_dept = query('SELECT id FROM departments ORDER BY id LIMIT 1')[0]['id']
    execute([('UPDATE doctors SET department_id=? WHERE department_id IS NULL', (first_dept,))])
    if query('SELECT COUNT(*) AS n FROM doctors')[0]['n'] == 0:
        execute([('INSERT INTO doctors(name,designation,rota_group,fte,last_working_date,status,department_id) VALUES(?,?,?,?,?,?,?)', [
            ('Example Junior 1', 'Doctor', 'Junior', 1, '', 'Active', first_dept),
            ('Example Junior 2', 'Doctor', 'Junior', 1, '', 'Active', first_dept),
            ('Example Junior 3', 'Doctor', 'Junior', 1, '', 'Active', first_dept),
            ('Example Junior 4', 'Doctor', 'Junior', 1, '', 'Active', first_dept),
            ('Example Junior 5', 'Doctor', 'Junior', .8, '', 'Active', first_dept),
            ('Example Senior 1', 'Doctor', 'Senior', 1, '', 'Active', first_dept),
            ('Example Senior 2', 'Doctor', 'Senior', 1, '', 'Active', first_dept),
            ('Example Senior 3', 'Doctor', 'Senior', .8, '', 'Active', first_dept),
            ('Example Senior 4', 'Doctor', 'Senior', 1, '', 'Active', first_dept),
            ('Example Senior 5', 'Doctor', 'Senior', 1, '', 'Active', first_dept)], True)])


ORG_JOIN = 'LEFT JOIN departments dep ON dep.id=d.department_id LEFT JOIN trusts t ON t.id=dep.trust_id'
ORG_COLS = 'd.department_id AS department_id, dep.name AS department, dep.trust_id AS trust_id, t.name AS trust'


def ds():
    return query(f'SELECT d.*, dep.name AS department, dep.trust_id AS trust_id, t.name AS trust FROM doctors d {ORG_JOIN} '
                 'ORDER BY t.name, dep.name, d.rota_group, d.name')


def ls():
    return query(f'SELECT l.*, d.name, {ORG_COLS} FROM leaves l JOIN doctors d ON d.id=l.doctor_id {ORG_JOIN} ORDER BY l.start_date')


def rs():
    return query(f'SELECT r.*, d.name, d.rota_group, {ORG_COLS} FROM rota r JOIN doctors d ON d.id=r.doctor_id {ORG_JOIN} '
                 'ORDER BY r.rota_date, r.shift_code, d.name')


def orgs():
    trusts = query('SELECT id,name FROM trusts ORDER BY name')
    deps = query('SELECT dep.id, dep.name, dep.trust_id, t.name AS trust FROM departments dep JOIN trusts t ON t.id=dep.trust_id ORDER BY t.name, dep.name')
    for t in trusts:
        t['departments'] = [d for d in deps if d['trust_id'] == t['id']]
    return trusts, deps


def org_filter(rows, trust, dept):
    return [r for r in rows
            if (not trust or str(r.get('trust_id')) == trust) and (not dept or str(r.get('department_id')) == dept)]


def month_label(m):
    y, mo = m.split('-')
    return date(int(y), int(mo), 1).strftime('%B %Y')


# ---------- monthly summary ----------
def monthly_summary(doctors, rota):
    """{ 'YYYY-MM': [ {name, trust, department, group, SD, LD, LN, total}, ... ] }"""
    counts = {}
    for r in rota:
        key = (r['rota_date'][:7], r['doctor_id'])
        counts.setdefault(key, {'SD': 0, 'LD': 0, 'LN': 0})
        if r['shift_code'] in counts[key]:
            counts[key][r['shift_code']] += 1
    out = OrderedDict()
    for month in sorted({k[0] for k in counts}):
        rows = []
        for d in doctors:
            c = counts.get((month, d['id']))
            if c is None and d['status'] != 'Active':
                continue
            c = c or {'SD': 0, 'LD': 0, 'LN': 0}
            rows.append({'name': d['name'], 'trust': d.get('trust') or '', 'department': d.get('department') or '',
                         'group': d['rota_group'], 'SD': c['SD'], 'LD': c['LD'], 'LN': c['LN'],
                         'total': c['SD'] + c['LD'] + c['LN']})
        rows.sort(key=lambda x: (x['trust'], x['department'], x['group'], x['name']))
        out[month] = rows
    return out


# ---------- rota filters / search ----------
def rota_view(args, doctors, leaves, rota):
    q = (args.get('q') or '').strip().lower()
    raw_from, raw_to = (args.get('date_from') or '').strip(), (args.get('date_to') or '').strip()
    shift = (args.get('shift') or '').strip().upper()
    group = (args.get('group') or '').strip()
    doc = (args.get('doctor') or '').strip()
    dfrom, dto = raw_from, raw_to
    if dfrom and not dto: dto = dfrom
    if dto and not dfrom: dfrom = dto
    if dfrom and dto and dto < dfrom: dfrom, dto = dto, dfrom
    org = bool(args.get('trust') or args.get('dept'))
    res = {'q': q, 'dfrom': raw_from, 'dto': raw_to, 'shift': shift, 'group': group, 'doctor': doc,
           'filtered': bool(q or dfrom or shift or group or doc or org),
           'groups': [], 'rota_total': 0, 'truncated': False, 'doctors': [], 'leaves': [], 'on_leave': []}

    def hit(*vals): return (not q) or any(q in str(v).lower() for v in vals)

    rows = []
    for r in rota:
        if dfrom and not (dfrom <= r['rota_date'] <= dto): continue
        if shift and r['shift_code'] != shift: continue
        if group and r['rota_group'] != group: continue
        if doc and str(r['doctor_id']) != doc: continue
        if hit(r['rota_date'], r['shift_code'], SHIFT_NAMES.get(r['shift_code'], ''), r['name'], r['rota_group'],
               r.get('trust'), r.get('department')):
            rows.append(r)
    res['rota_total'] = len(rows)
    res['truncated'] = len(rows) > MAX_RESULTS
    grouped = {}
    for r in rows[:MAX_RESULTS]:
        grouped.setdefault((r.get('trust') or '—', r.get('department') or '—'), []).append(r)
    res['groups'] = [{'trust': k[0], 'department': k[1], 'rows': v} for k, v in sorted(grouped.items())]

    if q:
        totals = {}
        for r in rota:
            t = totals.setdefault(r['doctor_id'], {'SD': 0, 'LD': 0, 'LN': 0})
            if r['shift_code'] in t: t[r['shift_code']] += 1
        lcount = {}
        for l in leaves: lcount[l['doctor_id']] = lcount.get(l['doctor_id'], 0) + 1
        for d in doctors:
            if hit(d['name'], d['designation'], d['rota_group'], d['status'], d['fte'], d['last_working_date'],
                   d.get('trust'), d.get('department')):
                t = totals.get(d['id'], {'SD': 0, 'LD': 0, 'LN': 0})
                res['doctors'].append({**d, **t, 'leave_entries': lcount.get(d['id'], 0)})
    if q or dfrom:
        for l in leaves:
            if dfrom and not (l['start_date'] <= dto and l['end_date'] >= dfrom): continue
            if hit(l['name'], l['leave_type'], l['start_date'], l['end_date'], 'leave', l.get('department')):
                res['leaves'].append(l)
    if dfrom:
        res['on_leave'] = sorted({l['name'] for l in leaves if l['start_date'] <= dto and l['end_date'] >= dfrom})
    return res


# ---------- dashboard charts ----------
def shift_charts(doctors, rota, month=''):
    counts = {}
    for r in rota:
        if month and r['rota_date'][:7] != month: continue
        c = counts.setdefault(r['doctor_id'], {'SD': 0, 'LD': 0, 'LN': 0})
        if r['shift_code'] in c: c[r['shift_code']] += 1
    rows = [d for d in doctors if d['status'] == 'Active' or d['id'] in counts]
    rows.sort(key=lambda d: (d.get('trust') or '', d.get('department') or '', d['rota_group'], d['name']))
    charts = []
    for code in ('SD', 'LD', 'LN'):
        items = [{'name': d['name'], 'group': d['rota_group'], 'count': counts.get(d['id'], {}).get(code, 0)} for d in rows]
        mx = max([i['count'] for i in items] or [0]) or 1
        for i in items: i['pct'] = round(i['count'] * 100 / mx)
        charts.append({'code': code, 'title': SHIFT_NAMES[code] + 's', 'total': sum(i['count'] for i in items), 'bars': items})
    return charts


# ---------- matrix (rows = dates, columns = doctors) ----------
def build_matrix(doctors, leaves, rota, month):
    y, mo = int(month[:4]), int(month[5:7])
    days = [date(y, mo, i) for i in range(1, calendar.monthrange(y, mo)[1] + 1)]
    iso = [d.isoformat() for d in days]
    cell = {(r['doctor_id'], r['rota_date']): r['shift_code'] for r in rota if r['rota_date'][:7] == month}
    leave = {}
    for l in leaves:
        for day in iso:
            if l['start_date'] <= day <= l['end_date']:
                leave[(l['doctor_id'], day)] = l['leave_type']
    used = {k[0] for k in cell}
    cols = [d for d in doctors if d['status'] == 'Active' or d['id'] in used]
    cols.sort(key=lambda d: (0 if d['rota_group'] == 'Junior' else 1, d['name']))
    rows = []
    for day, s in zip(days, iso):
        rows.append({'date': s, 'dow': day.strftime('%a'), 'weekend': day.weekday() >= 5,
                     'cells': [{'code': cell.get((d['id'], s), ''), 'leave': leave.get((d['id'], s), '')} for d in cols]})
    totals = {code: [sum(1 for s in iso if cell.get((d['id'], s)) == code) for d in cols] for code in ('SD', 'LD', 'LN')}
    totals['ALL'] = [sum(totals[c][i] for c in ('SD', 'LD', 'LN')) for i in range(len(cols))]
    groups = []
    for d in cols:
        if groups and groups[-1]['name'] == d['rota_group']: groups[-1]['span'] += 1
        else: groups.append({'name': d['rota_group'], 'span': 1})
    return {'cols': cols, 'groups': groups, 'rows': rows, 'totals': totals, 'label': month_label(month)}


# ---------- page context ----------
def ctx(page, scope=False, title=None):
    d, l, r = ds(), ls(), rs()
    t_sel, p_sel = request.args.get('trust', ''), request.args.get('dept', '')
    if scope:
        d, l, r = org_filter(d, t_sel, p_sel), org_filter(l, t_sel, p_sel), org_filter(r, t_sel, p_sel)
    trusts, departments = orgs()
    return dict(page=page, title=title or page, doctors=d, leaves=l, rota=r, issues=[], today=date.today().isoformat(),
                storage='PostgreSQL (persistent)' if USE_PG else 'SQLite (temporary on free hosting)',
                trusts=trusts, departments=departments, sel_trust=t_sel, sel_dept=p_sel, max_results=MAX_RESULTS)


init()


@app.route('/')
def home():
    c = ctx('Dashboard', scope=True)
    month = request.args.get('month', '')
    c['charts'] = shift_charts(c['doctors'], c['rota'], month)
    c['months'] = [{'key': k, 'label': month_label(k)} for k in sorted({r['rota_date'][:7] for r in c['rota']})]
    c['selected_month'] = month
    return render_template('page.html', **c)


# ----- organisation: trusts and departments -----
@app.route('/organisation')
def organisation():
    c = ctx('Organisation', title='Trusts & Departments')
    n = {}
    for d in c['doctors']: n[d['department_id']] = n.get(d['department_id'], 0) + 1
    for t in c['trusts']:
        for dep in t['departments']: dep['doctor_count'] = n.get(dep['id'], 0)
    return render_template('page.html', **c)


@app.route('/organisation/trust', methods=['POST'])
def add_trust():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Enter a trust name.')
    elif any(t['name'].lower() == name.lower() for t in orgs()[0]):
        flash('That trust already exists.')
    else:
        execute([('INSERT INTO trusts(name) VALUES(?)', (name,))])
        flash(f'Trust "{name}" added.')
    return redirect(url_for('organisation'))


@app.route('/organisation/department', methods=['POST'])
def add_department():
    name, trust_id = request.form.get('name', '').strip(), request.form.get('trust_id', '')
    if not name or not trust_id:
        flash('Choose a trust and enter a department name.')
    elif any(d['name'].lower() == name.lower() and str(d['trust_id']) == trust_id for d in orgs()[1]):
        flash('That department already exists in this trust.')
    else:
        execute([('INSERT INTO departments(trust_id,name) VALUES(?,?)', (int(trust_id), name))])
        flash(f'Department "{name}" added.')
    return redirect(url_for('organisation'))


@app.route('/organisation/department/<int:dep_id>/delete', methods=['POST'])
def delete_department(dep_id):
    if query('SELECT 1 AS x FROM doctors WHERE department_id=? LIMIT 1', (dep_id,)):
        flash('This department still has doctors. Move or delete them first.')
    else:
        execute([('DELETE FROM departments WHERE id=?', (dep_id,))])
        flash('Department deleted.')
    return redirect(url_for('organisation'))


@app.route('/organisation/trust/<int:trust_id>/delete', methods=['POST'])
def delete_trust(trust_id):
    if query('SELECT 1 AS x FROM departments WHERE trust_id=? LIMIT 1', (trust_id,)):
        flash('This trust still has departments. Delete them first.')
    else:
        execute([('DELETE FROM trusts WHERE id=?', (trust_id,))])
        flash('Trust deleted.')
    return redirect(url_for('organisation'))


# ----- doctors -----
@app.route('/doctors', methods=['GET', 'POST'])
def doctors():
    if request.method == 'POST':
        dept = request.form.get('department_id', '')
        if not dept:
            flash('Choose a department. Add one under Organisation first.')
            return redirect(url_for('doctors'))
        execute([('INSERT INTO doctors(name,designation,rota_group,fte,last_working_date,status,department_id) VALUES(?,?,?,?,?,?,?)',
                  (request.form['name'], request.form.get('designation', 'Doctor'), request.form['rota_group'],
                   float(request.form.get('fte') or 1), request.form.get('last_working_date', ''),
                   request.form.get('status', 'Active'), int(dept)))])
        flash('Doctor added.')
        return redirect(url_for('doctors'))
    return render_template('page.html', **ctx('Doctors', scope=True))


@app.route('/doctors/<int:doc_id>/archive', methods=['POST'])
def archive_doctor(doc_id):
    rows = query('SELECT name,status FROM doctors WHERE id=?', (doc_id,))
    if rows:
        new = 'Inactive' if rows[0]['status'] == 'Active' else 'Active'
        execute([('UPDATE doctors SET status=? WHERE id=?', (new, doc_id))])
        flash(f"{rows[0]['name']} archived (kept in history, excluded from new rotas)." if new == 'Inactive'
              else f"{rows[0]['name']} reactivated.")
    return redirect(request.referrer or url_for('doctors'))


@app.route('/doctors/<int:doc_id>/delete', methods=['POST'])
def delete_doctor(doc_id):
    rows = query('SELECT name FROM doctors WHERE id=?', (doc_id,))
    if rows:
        execute([('DELETE FROM rota WHERE doctor_id=?', (doc_id,)), ('DELETE FROM leaves WHERE doctor_id=?', (doc_id,)),
                 ('DELETE FROM doctors WHERE id=?', (doc_id,))])
        flash(f"{rows[0]['name']} deleted, with their leave and rota assignments. Regenerate the rota to refill cover.")
    return redirect(request.referrer or url_for('doctors'))


@app.route('/doctors/<int:doc_id>/move', methods=['POST'])
def move_doctor(doc_id):
    dept = request.form.get('department_id', '')
    if dept:
        execute([('UPDATE doctors SET department_id=? WHERE id=?', (int(dept), doc_id))])
        flash('Doctor moved. Their existing rota entries now count under the new department; regenerate if needed.')
    return redirect(request.referrer or url_for('doctors'))


# ----- leave -----
@app.route('/leave/<int:leave_id>/delete', methods=['POST'])
def delete_leave(leave_id):
    execute([('DELETE FROM leaves WHERE id=?', (leave_id,))])
    flash('Leave entry removed.')
    return redirect(url_for('leave'))


@app.route('/leave', methods=['GET', 'POST'])
def leave():
    if request.method == 'POST':
        a, b = request.form['start_date'], request.form['end_date']
        if b < a:
            flash('End date must be on or after start date.')
        else:
            execute([('INSERT INTO leaves(doctor_id,leave_type,start_date,end_date) VALUES(?,?,?,?)',
                      (int(request.form['doctor_id']), request.form['leave_type'], a, b))])
            flash('Leave saved.')
        return redirect(url_for('leave'))
    return render_template('page.html', **ctx('Leave'))


# ----- generate / rota -----
@app.route('/generate', methods=['GET', 'POST'])
def generate():
    if request.method == 'POST':
        trust, dept = request.form.get('trust', ''), request.form.get('dept', '')
        try:
            start = date.fromisoformat(request.form['start_date'])
            weeks = int(request.form['weeks'])
            assert weeks in (4, 8)
            all_docs, all_leaves = ds(), ls()
            targets = [d for d in orgs()[1] if (not trust or str(d['trust_id']) == trust) and (not dept or str(d['id']) == dept)]
            if not targets:
                flash('No department selected. Add a trust and department under Organisation first.')
            for dep in targets:
                label = f"{dep['trust']} › {dep['name']}"
                docs = [d for d in all_docs if d['department_id'] == dep['id']]
                if not docs:
                    flash(f'{label}: no doctors yet, skipped.')
                    continue
                lv = [l for l in all_leaves if l['department_id'] == dep['id']]
                out = generate_rota(docs, lv, start, weeks)
                if not out['ok']:
                    flash(f"{label}: no rota generated. {out['message']}")
                    continue
                execute([
                    ('DELETE FROM rota WHERE rota_date>=? AND rota_date<? AND doctor_id IN (SELECT id FROM doctors WHERE department_id=?)',
                     (start.isoformat(), (start + timedelta(days=weeks * 7)).isoformat(), dep['id'])),
                    ('INSERT INTO rota(rota_date,doctor_id,shift_code) VALUES(?,?,?)',
                     [(x['date'], x['doctor_id'], x['shift']) for x in out['assignments']], True)])
                flash(f"{label}: generated {len(out['assignments'])} assignments. Review Quality; prototype only.")
        except Exception as e:
            flash(f'Generation failed: {e}')
        return redirect(url_for('generate', trust=trust, dept=dept) + '#rota-details')
    c = ctx('Generate', scope=True)
    c['result'] = rota_view(request.args, c['doctors'], c['leaves'], c['rota'])
    return render_template('page.html', **c)


@app.route('/rota')
def rota():
    c = ctx('Rota', scope=True)
    c['result'] = rota_view(request.args, c['doctors'], c['leaves'], c['rota'])
    return render_template('page.html', **c)


@app.route('/matrix')
def matrix():
    c = ctx('Matrix', title='Rota matrix')
    dept = request.args.get('dept', '')
    valid = {str(d['id']) for d in c['departments']}
    if dept not in valid:
        with_rota = [str(d['id']) for d in c['departments'] if any(r['department_id'] == d['id'] for r in c['rota'])]
        dept = (with_rota or [str(d['id']) for d in c['departments']] or [''])[0]
    docs = [d for d in c['doctors'] if str(d['department_id']) == dept]
    rows = [r for r in c['rota'] if str(r['department_id']) == dept]
    lvs = [l for l in c['leaves'] if str(l['department_id']) == dept]
    months = sorted({r['rota_date'][:7] for r in rows})
    month = request.args.get('month', '')
    if month not in months: month = months[0] if months else ''
    c.update(sel_dept=dept, months=[{'key': k, 'label': month_label(k)} for k in months], selected_month=month,
             matrix=build_matrix(docs, lvs, rows, month) if month else None)
    return render_template('page.html', **c)


@app.route('/summary')
def summary():
    c = ctx('Summary', scope=True)
    sm = monthly_summary(c['doctors'], c['rota'])
    c['months'] = [{'key': k, 'label': month_label(k)} for k in sm]
    c['summary'] = [{'key': k, 'label': month_label(k), 'rows': v} for k, v in sm.items()]
    sel = request.args.get('month', '')
    if sel: c['summary'] = [s for s in c['summary'] if s['key'] == sel]
    c['selected_month'] = sel
    return render_template('page.html', **c)


@app.route('/summary.csv')
def summary_csv():
    t_sel, p_sel = request.args.get('trust', ''), request.args.get('dept', '')
    sm = monthly_summary(org_filter(ds(), t_sel, p_sel), org_filter(rs(), t_sel, p_sel))
    o = io.StringIO(); w = csv.writer(o)
    w.writerow(['Month', 'Trust', 'Department', 'Doctor', 'Group', 'Short Days (SD)', 'Long Days (LD)', 'Long Nights (LN)', 'Total'])
    for m, rows in sm.items():
        for r in rows: w.writerow([m, r['trust'], r['department'], r['name'], r['group'], r['SD'], r['LD'], r['LN'], r['total']])
    return send_file(io.BytesIO(o.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True,
                     download_name='doctor_monthly_summary.csv')


@app.route('/quality')
def quality():
    c = ctx('Quality', scope=True)
    reports = []
    for dep in c['departments']:
        if (c['sel_trust'] and str(dep['trust_id']) != c['sel_trust']) or (c['sel_dept'] and str(dep['id']) != c['sel_dept']):
            continue
        rows = [r for r in c['rota'] if r['department_id'] == dep['id']]
        if not rows: continue
        docs = [d for d in c['doctors'] if d['department_id'] == dep['id']]
        lvs = [l for l in c['leaves'] if l['department_id'] == dep['id']]
        reports.append({'label': f"{dep['trust']} › {dep['name']}", 'issues': validate_rota(rows, docs, lvs),
                        'fairness': fairness_report(rows, docs)})
    c['reports'] = reports
    return render_template('page.html', **c)


@app.route('/export.csv')
def export():
    o = io.StringIO(); w = csv.writer(o); w.writerow(['Trust', 'Department', 'Date', 'Shift', 'Doctor', 'Rota Group'])
    for r in rs(): w.writerow([r['trust'], r['department'], r['rota_date'], r['shift_code'], r['name'], r['rota_group']])
    return send_file(io.BytesIO(o.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True,
                     download_name='nhs_rota.csv')


@app.route('/status')
def status():
    """Shows which database the app is really using and what is in it (no secrets shown)."""
    info = {'storage': 'PostgreSQL' if USE_PG else 'SQLite (temporary on free hosting)',
            'DATABASE_URL_set': bool(os.environ.get('DATABASE_URL', '').strip())}
    try:
        for t in ('trusts', 'departments', 'doctors', 'leaves', 'rota'):
            info[t + '_rows'] = query(f'SELECT COUNT(*) AS n FROM {t}')[0]['n']
        if USE_PG:
            info['tables_in_public_schema'] = sorted(
                r['table_name'] for r in query("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))
            info['database'] = query('SELECT current_database() AS d')[0]['d']
        info['connection'] = 'ok'
    except Exception as e:
        info['connection'] = f'FAILED: {type(e).__name__}'
    return info


if __name__ == '__main__':
    app.run(debug=False, host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', 5000)))
