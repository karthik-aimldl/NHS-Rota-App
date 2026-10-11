from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import sqlite3, csv, io, os
from pathlib import Path
from datetime import date, timedelta
from collections import OrderedDict
from solver import generate_rota, validate_rota

ROOT = Path(__file__).resolve().parent
SQLITE_PATH = ROOT / 'data' / 'rota.db'
SQLITE_PATH.parent.mkdir(exist_ok=True)

# If DATABASE_URL is set (e.g. a free Neon/Supabase PostgreSQL), data is stored there and
# survives restarts and redeploys. Otherwise the app falls back to a local SQLite file.
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = 'postgresql://' + DATABASE_URL[len('postgres://'):]
USE_PG = bool(DATABASE_URL)
if USE_PG:
    import psycopg2, psycopg2.extras

app = Flask(__name__, template_folder='app/templates', static_folder='app/static')
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-prototype-secret')

SHIFT_NAMES = {'SD': 'Short Day', 'LD': 'Long Day', 'LN': 'Long Night'}
MAX_RESULTS = 1000


# ---------- database helpers (work for both SQLite and PostgreSQL) ----------
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


def init():
    pk = 'SERIAL PRIMARY KEY' if USE_PG else 'INTEGER PRIMARY KEY'
    execute([
        (f'CREATE TABLE IF NOT EXISTS doctors(id {pk},name TEXT,designation TEXT,rota_group TEXT,fte REAL,last_working_date TEXT,status TEXT)', ()),
        (f'CREATE TABLE IF NOT EXISTS leaves(id {pk},doctor_id INTEGER,leave_type TEXT,start_date TEXT,end_date TEXT)', ()),
        (f'CREATE TABLE IF NOT EXISTS rota(id {pk},rota_date TEXT,doctor_id INTEGER,shift_code TEXT)', ()),
    ])
    if query('SELECT COUNT(*) AS n FROM doctors')[0]['n'] == 0:
        execute([('INSERT INTO doctors(name,designation,rota_group,fte,last_working_date,status) VALUES(?,?,?,?,?,?)', [
            ('Example Junior 1', 'Doctor', 'Junior', 1, '', 'Active'),
            ('Example Junior 2', 'Doctor', 'Junior', 1, '', 'Active'),
            ('Example Junior 3', 'Doctor', 'Junior', 1, '', 'Active'),
            ('Example Junior 4', 'Doctor', 'Junior', 1, '', 'Active'),
            ('Example Junior 5', 'Doctor', 'Junior', .8, '', 'Active'),
            ('Example Senior 1', 'Doctor', 'Senior', 1, '', 'Active'),
            ('Example Senior 2', 'Doctor', 'Senior', 1, '', 'Active'),
            ('Example Senior 3', 'Doctor', 'Senior', .8, '', 'Active'),
            ('Example Senior 4', 'Doctor', 'Senior', 1, '', 'Active'),
            ('Example Senior 5', 'Doctor', 'Senior', 1, '', 'Active')], True)])


def ds(): return query('SELECT * FROM doctors ORDER BY rota_group,name')
def ls(): return query('SELECT l.*,d.name FROM leaves l JOIN doctors d ON d.id=l.doctor_id ORDER BY start_date')
def rs(): return query('SELECT r.*,d.name,d.rota_group FROM rota r JOIN doctors d ON d.id=r.doctor_id ORDER BY rota_date,shift_code,d.name')


# ---------- monthly summary ----------
def monthly_summary(doctors, rota):
    """{ 'YYYY-MM': [ {name, group, SD, LD, LN, total}, ... ] } for every month that has rota data."""
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
            rows.append({'name': d['name'], 'group': d['rota_group'], 'SD': c['SD'], 'LD': c['LD'], 'LN': c['LN'],
                         'total': c['SD'] + c['LD'] + c['LN']})
        rows.sort(key=lambda x: (x['group'], x['name']))
        out[month] = rows
    return out


def month_label(m):
    y, mo = m.split('-')
    return date(int(y), int(mo), 1).strftime('%B %Y')


# ---------- rota filters / search ----------
def rota_view(args, doctors, leaves, rota):
    """Filters the saved rota by free text, doctor, group, shift and date (or date range)."""
    q = (args.get('q') or '').strip().lower()
    raw_from, raw_to = (args.get('date_from') or '').strip(), (args.get('date_to') or '').strip()
    shift = (args.get('shift') or '').strip().upper()
    group = (args.get('group') or '').strip()
    doc = (args.get('doctor') or '').strip()
    dfrom, dto = raw_from, raw_to
    if dfrom and not dto: dto = dfrom
    if dto and not dfrom: dfrom = dto
    if dfrom and dto and dto < dfrom: dfrom, dto = dto, dfrom
    res = {'q': q, 'dfrom': raw_from, 'dto': raw_to, 'shift': shift, 'group': group, 'doctor': doc,
           'filtered': bool(q or dfrom or shift or group or doc),
           'rota': [], 'rota_total': 0, 'truncated': False, 'doctors': [], 'leaves': [], 'on_leave': []}

    def hit(*vals): return (not q) or any(q in str(v).lower() for v in vals)

    rows = []
    for r in rota:
        if dfrom and not (dfrom <= r['rota_date'] <= dto): continue
        if shift and r['shift_code'] != shift: continue
        if group and r['rota_group'] != group: continue
        if doc and str(r['doctor_id']) != doc: continue
        if hit(r['rota_date'], r['shift_code'], SHIFT_NAMES.get(r['shift_code'], ''), r['name'], r['rota_group']):
            rows.append(r)
    res['rota_total'] = len(rows)
    res['truncated'] = len(rows) > MAX_RESULTS
    res['rota'] = rows[:MAX_RESULTS]

    if q:  # doctor details
        totals = {}
        for r in rota:
            t = totals.setdefault(r['doctor_id'], {'SD': 0, 'LD': 0, 'LN': 0})
            if r['shift_code'] in t: t[r['shift_code']] += 1
        lcount = {}
        for l in leaves: lcount[l['doctor_id']] = lcount.get(l['doctor_id'], 0) + 1
        for d in doctors:
            if hit(d['name'], d['designation'], d['rota_group'], d['status'], d['fte'], d['last_working_date']):
                t = totals.get(d['id'], {'SD': 0, 'LD': 0, 'LN': 0})
                res['doctors'].append({**d, **t, 'leave_entries': lcount.get(d['id'], 0)})
    if q or dfrom:  # leave entries
        for l in leaves:
            if dfrom and not (l['start_date'] <= dto and l['end_date'] >= dfrom): continue
            if hit(l['name'], l['leave_type'], l['start_date'], l['end_date'], 'leave'):
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
    rows.sort(key=lambda d: (d['rota_group'], d['name']))
    charts = []
    for code in ('SD', 'LD', 'LN'):
        items = [{'name': d['name'], 'group': d['rota_group'], 'count': counts.get(d['id'], {}).get(code, 0)} for d in rows]
        mx = max([i['count'] for i in items] or [0]) or 1
        for i in items: i['pct'] = round(i['count'] * 100 / mx)
        charts.append({'code': code, 'title': SHIFT_NAMES[code] + 's', 'total': sum(i['count'] for i in items), 'bars': items})
    return charts


def ctx(page, **extra):
    d, l, r = ds(), ls(), rs()
    base = dict(page=page, doctors=d, leaves=l, rota=r, issues=[], today=date.today().isoformat(),
                storage='PostgreSQL (persistent)' if USE_PG else 'SQLite (temporary on free hosting)')
    base.update(extra)
    return base


init()


@app.route('/')
def home():
    c = ctx('Dashboard')
    month = request.args.get('month', '')
    c['charts'] = shift_charts(c['doctors'], c['rota'], month)
    c['months'] = [{'key': k, 'label': month_label(k)} for k in sorted({r['rota_date'][:7] for r in c['rota']})]
    c['selected_month'] = month
    return render_template('page.html', **c)


@app.route('/doctors', methods=['GET', 'POST'])
def doctors():
    if request.method == 'POST':
        execute([('INSERT INTO doctors(name,designation,rota_group,fte,last_working_date,status) VALUES(?,?,?,?,?,?)',
                  (request.form['name'], request.form.get('designation', 'Doctor'), request.form['rota_group'],
                   float(request.form.get('fte') or 1), request.form.get('last_working_date', ''),
                   request.form.get('status', 'Active')))])
        flash('Doctor added.')
        return redirect(url_for('doctors'))
    return render_template('page.html', **ctx('Doctors'))


@app.route('/doctors/<int:doc_id>/archive', methods=['POST'])
def archive_doctor(doc_id):
    rows = query('SELECT name,status FROM doctors WHERE id=?', (doc_id,))
    if rows:
        new = 'Inactive' if rows[0]['status'] == 'Active' else 'Active'
        execute([('UPDATE doctors SET status=? WHERE id=?', (new, doc_id))])
        flash(f"{rows[0]['name']} archived (kept in history, excluded from new rotas)." if new == 'Inactive'
              else f"{rows[0]['name']} reactivated.")
    return redirect(url_for('doctors'))


@app.route('/doctors/<int:doc_id>/delete', methods=['POST'])
def delete_doctor(doc_id):
    rows = query('SELECT name FROM doctors WHERE id=?', (doc_id,))
    if rows:
        execute([('DELETE FROM rota WHERE doctor_id=?', (doc_id,)), ('DELETE FROM leaves WHERE doctor_id=?', (doc_id,)),
                 ('DELETE FROM doctors WHERE id=?', (doc_id,))])
        flash(f"{rows[0]['name']} deleted, with their leave and rota assignments. Regenerate the rota to refill cover.")
    return redirect(url_for('doctors'))


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


@app.route('/generate', methods=['GET', 'POST'])
def generate():
    if request.method == 'POST':
        try:
            start = date.fromisoformat(request.form['start_date'])
            weeks = int(request.form['weeks'])
            assert weeks in (4, 8)
            out = generate_rota(ds(), ls(), start, weeks)
            if not out['ok']:
                flash('No rota generated: ' + out['message'])
            else:
                execute([
                    ('DELETE FROM rota WHERE rota_date>=? AND rota_date<?',
                     (start.isoformat(), (start + timedelta(days=weeks * 7)).isoformat())),
                    ('INSERT INTO rota(rota_date,doctor_id,shift_code) VALUES(?,?,?)',
                     [(x['date'], x['doctor_id'], x['shift']) for x in out['assignments']], True)])
                flash(f"Generated {len(out['assignments'])} assignments. Review Quality; prototype only.")
                return redirect(url_for('generate') + '#rota-details')
        except Exception as e:
            flash(f'Generation failed: {e}')
        return redirect(url_for('generate'))
    c = ctx('Generate')
    c['result'] = rota_view(request.args, c['doctors'], c['leaves'], c['rota'])
    return render_template('page.html', **c)


@app.route('/rota')
def rota():
    c = ctx('Rota')
    c['result'] = rota_view(request.args, c['doctors'], c['leaves'], c['rota'])
    return render_template('page.html', **c)


@app.route('/summary')
def summary():
    c = ctx('Summary')
    sm = monthly_summary(c['doctors'], c['rota'])
    c['summary'] = [{'key': k, 'label': month_label(k), 'rows': v} for k, v in sm.items()]
    sel = request.args.get('month', '')
    if sel: c['summary'] = [s for s in c['summary'] if s['key'] == sel]
    c['months'] = [{'key': k, 'label': month_label(k)} for k in sm]
    c['selected_month'] = sel
    return render_template('page.html', **c)


@app.route('/summary.csv')
def summary_csv():
    sm = monthly_summary(ds(), rs())
    o = io.StringIO(); w = csv.writer(o)
    w.writerow(['Month', 'Doctor', 'Group', 'Short Days (SD)', 'Long Days (LD)', 'Long Nights (LN)', 'Total'])
    for m, rows in sm.items():
        for r in rows: w.writerow([m, r['name'], r['group'], r['SD'], r['LD'], r['LN'], r['total']])
    return send_file(io.BytesIO(o.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True,
                     download_name='doctor_monthly_summary.csv')


@app.route('/quality')
def quality():
    c = ctx('Quality')
    c['issues'] = validate_rota(c['rota'], c['doctors'], c['leaves'])
    return render_template('page.html', **c)


@app.route('/export.csv')
def export():
    o = io.StringIO(); w = csv.writer(o); w.writerow(['Date', 'Shift', 'Doctor', 'Rota Group'])
    for r in rs(): w.writerow([r['rota_date'], r['shift_code'], r['name'], r['rota_group']])
    return send_file(io.BytesIO(o.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True,
                     download_name='nhs_rota.csv')


if __name__ == '__main__':
    app.run(debug=False, host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', 5000)))
