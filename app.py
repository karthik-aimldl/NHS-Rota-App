from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import sqlite3, csv, io, os
from pathlib import Path
from datetime import date, timedelta
from solver import generate_rota, validate_rota
ROOT=Path(__file__).resolve().parent; DB=ROOT/'data'/'rota.db'; DB.parent.mkdir(exist_ok=True)
app=Flask(__name__, template_folder='app/templates', static_folder='app/static'); app.secret_key=os.environ.get('SECRET_KEY','change-this-prototype-secret')
def db():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def init():
 with db() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS doctors(id INTEGER PRIMARY KEY,name TEXT,designation TEXT,rota_group TEXT,fte REAL,last_working_date TEXT,status TEXT); CREATE TABLE IF NOT EXISTS leaves(id INTEGER PRIMARY KEY,doctor_id INTEGER,leave_type TEXT,start_date TEXT,end_date TEXT); CREATE TABLE IF NOT EXISTS rota(id INTEGER PRIMARY KEY,rota_date TEXT,doctor_id INTEGER,shift_code TEXT);''')
  if c.execute('SELECT COUNT(*) FROM doctors').fetchone()[0]==0:
   c.executemany('INSERT INTO doctors(name,designation,rota_group,fte,last_working_date,status) VALUES(?,?,?,?,?,?)',[('Example Junior 1','Doctor','Junior',1,'','Active'),('Example Junior 2','Doctor','Junior',1,'','Active'),('Example Senior 1','Doctor','Senior',1,'','Active'),('Example Senior 2','Doctor','Senior',1,'','Active'),('Example Senior 3','Doctor','Senior',.8,'','Active')])
def getall(sql):
 with db() as c:return [dict(x) for x in c.execute(sql)]
def ds():return getall('SELECT * FROM doctors ORDER BY rota_group,name')
def ls():return getall('SELECT l.*,d.name FROM leaves l JOIN doctors d ON d.id=l.doctor_id ORDER BY start_date')
def rs():return getall('SELECT r.*,d.name,d.rota_group FROM rota r JOIN doctors d ON d.id=r.doctor_id ORDER BY rota_date,shift_code,d.name')
init()
@app.route('/')
def home():
 d,l,r=ds(),ls(),rs(); return render_template('page.html',page='Dashboard',doctors=d,leaves=l,rota=r,issues=[],today=date.today().isoformat())
@app.route('/doctors',methods=['GET','POST'])
def doctors():
 if request.method=='POST':
  with db() as c:c.execute('INSERT INTO doctors(name,designation,rota_group,fte,last_working_date,status) VALUES(?,?,?,?,?,?)',(request.form['name'],request.form.get('designation','Doctor'),request.form['rota_group'],float(request.form.get('fte') or 1),request.form.get('last_working_date',''),request.form.get('status','Active')))
  flash('Doctor added.');return redirect(url_for('doctors'))
 return render_template('page.html',page='Doctors',doctors=ds(),leaves=ls(),rota=rs(),issues=[],today=date.today().isoformat())
@app.route('/leave',methods=['GET','POST'])
def leave():
 if request.method=='POST':
  a,b=request.form['start_date'],request.form['end_date']
  if b<a:flash('End date must be on or after start date.')
  else:
   with db() as c:c.execute('INSERT INTO leaves(doctor_id,leave_type,start_date,end_date) VALUES(?,?,?,?)',(request.form['doctor_id'],request.form['leave_type'],a,b))
   flash('Leave saved.')
  return redirect(url_for('leave'))
 return render_template('page.html',page='Leave',doctors=ds(),leaves=ls(),rota=rs(),issues=[],today=date.today().isoformat())
@app.route('/generate',methods=['GET','POST'])
def generate():
 if request.method=='POST':
  try:
   start=date.fromisoformat(request.form['start_date']); weeks=int(request.form['weeks']); assert weeks in (4,8)
   out=generate_rota(ds(),ls(),start,weeks)
   if not out['ok']:flash('No rota generated: '+out['message'])
   else:
    with db() as c:
     c.execute('DELETE FROM rota WHERE rota_date>=? AND rota_date<?',(start.isoformat(),(start+timedelta(days=weeks*7)).isoformat()))
     c.executemany('INSERT INTO rota(rota_date,doctor_id,shift_code) VALUES(?,?,?)',[(x['date'],x['doctor_id'],x['shift']) for x in out['assignments']])
    flash(f"Generated {len(out['assignments'])} assignments. Review Quality; prototype only.");return redirect(url_for('rota'))
  except Exception as e:flash(f'Generation failed: {e}')
  return redirect(url_for('generate'))
 return render_template('page.html',page='Generate',doctors=ds(),leaves=ls(),rota=rs(),issues=[],today=date.today().isoformat())
@app.route('/rota')
def rota():return render_template('page.html',page='Rota',doctors=ds(),leaves=ls(),rota=rs(),issues=[],today=date.today().isoformat())
@app.route('/quality')
def quality():return render_template('page.html',page='Quality',doctors=ds(),leaves=ls(),rota=rs(),issues=validate_rota(rs(),ds(),ls()),today=date.today().isoformat())
@app.route('/export.csv')
def export():
 o=io.StringIO();w=csv.writer(o);w.writerow(['Date','Shift','Doctor','Rota Group'])
 for r in rs():w.writerow([r['rota_date'],r['shift_code'],r['name'],r['rota_group']])
 return send_file(io.BytesIO(o.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='nhs_rota.csv')
if __name__=='__main__':app.run(debug=False,host=os.environ.get('HOST','127.0.0.1'),port=int(os.environ.get('PORT',5000)))
