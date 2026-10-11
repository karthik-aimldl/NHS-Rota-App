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
MAX_RESULTS = 500


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


# ---------- search ----------
def search(q, dfrom, dto, doctors, leaves, rota):
    q = (q or '').strip().lower()
    dfrom, dto = (dfrom or '').strip(), (dto or '').strip()
    if dfrom and not dto: dto = dfrom
    if dto and not dfrom: dfrom = dto
    if dfrom and dto and dto < dfrom: dfrom, dto = dto, dfrom
    active = bool(q or dfrom)
    res = {'active': active, 'rota': [], 'doctors': [], 'leaves': [], 'on_leave': [], 'truncated': False,
           'q': q, 'dfrom': dfrom, 'dto': dto}
    if not active: return res

    def hit(*vals): return (not q) or any(q in str(v).lower() for v in vals)

    rota_hits = []
    for r in rota:
        if dfrom and not (dfrom <= r['rota_date'] <= dto): continue
        if hit(r['rota_date'], r['shift_code'], SHIFT_NAMES.get(r['shift_code'], ''), r['name'], r['rota_group']):
            rota_hits.append(r)
    res['truncated'] = len(rota_hits) > MAX_RESULTS
    res['rota'] = rota_hits[:MAX_RESULTS]
    res['rota_total'] = len(rota_hits)

    shift_totals = {}
    for r in rota:
        t = shift_totals.setdefault(r['doctor_id'], {'SD': 0, 'LD': 0, 'LN': 0})
        if r['shift_code'] in t: t[r['shift_code']] += 1
    leave_counts = {}
    for l in leaves: leave_counts[l['doctor_id']] = leave_counts.get(l['doctor_id'], 0) + 1
    if q:
        for d in doctors:
            if hit(d['name'], d['designation'], d['rota_group'], d['status'], d['fte'], d['last_working_date']):
                t = shift_totals.get(d['id'], {'SD': 0, 'LD': 0, 'LN': 0})
                res['doctors'].append({**d, **t, 'leave_entries': leave_counts.get(d['id'], 0)})

    for l in leaves:
        if dfrom and not (l['start_date'] <= dto and l['end_date'] >= dfrom): continue
        if hit(l['name'], l['leave_type'], l['start_date'], l['end_date'], 'leave'):
            res['leaves'].append(l)
    if dfrom:
        res['on_leave'] = sorted({l['name'] for l in leaves if l['start_date'] <= dto and l['end_date'] >= dfrom})
    return res


def ctx(page, **extra):
    d, l, r = ds(), ls(), rs()
    base = dict(page=page, doctors=d, leaves=l, rota=r, issues=[], today=date.today().isoformat(),
                storage='PostgreSQL (persistent)' if USE_PG else 'SQLite (temporary on free hosting)')
    base.update(extra)
    return base


init()


@app.route('/')
def home(): return render_template('page.html', **ctx('Dashboard'))


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
                return redirect(url_for('rota'))
        except Exception as e:
            flash(f'Generation failed: {e}')
        return redirect(url_for('generate'))
    c = ctx('Generate')
    c['result'] = search(request.args.get('q'), request.args.get('date_from'), request.args.get('date_to'),
                         c['doctors'], c['leaves'], c['rota'])
    return render_template('page.html', **c)


@app.route('/rota')
def rota(): return render_template('page.html', **ctx('Rota'))


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
