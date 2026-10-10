# NHS Rota Optimiser (V1 Prototype)

A rule-driven rota planning tool for NHS doctors. It uses a constraint solver to build a draft 4- or 8-week rota from staffing rules, leave and contract availability, then checks the result for issues.

**Live demo:** https://nhs-rota-demo-v3s2.onrender.com/
*(Free hosting: the first load may take 30-60 seconds while the app wakes up. Demo data resets on restart.)*

> **Prototype only. Not approved for live clinical use. All data shown is sample data.**

## Why I built it
Rota planning is slow, manual and error-prone. This project shows how constraint programming can produce a fair first draft quickly, leaving the rota coordinator to review and adjust rather than build from scratch.

## Features
- Dashboard with a rota overview
- Doctor register (Junior / Senior groups, FTE, status)
- Leave register with date validation
- Automatic 4- or 8-week rota generation
- Saved rota view and CSV export
- Quality page that checks the rota for rule issues

## Rules currently handled
- Minimum staffing cover per shift
- Leave and contract availability
- One shift per doctor per day
- Consecutive SD / LD / LN limits
- Recovery after three consecutive night shifts

## Not yet implemented
- Rolling rule (max 3 LD in any 5 working shifts)
- Every LD-to-SD transition nuance
- Continuity from the previous rota
- Locked shifts, swaps and rebalancing
- Full audit trail and fairness reporting
- User login and role-based access

## Tech stack
- **Backend:** Python, Flask
- **Optimisation:** Google OR-Tools (CP-SAT)
- **Database:** SQLite
- **Frontend:** HTML, CSS, Jinja templates
- **Hosting:** Render (gunicorn)

## Run locally
Requires Python 3.10+.

- Windows: double-click `run.bat`
- macOS / Linux: `bash run.sh`
- Then open http://127.0.0.1:5000

## Project structure
```
app.py            Flask routes and database
solver.py         OR-Tools rota solver and validation
app/templates/    HTML templates
app/static/       CSS
config/           Excel configuration workbook
docs/             User manual
```

## Roadmap to production use
1. Complete and clinically validate all rota rules
2. Add authentication and role-based access
3. Move to PostgreSQL with backups
4. Add audit logging and rota locking
5. Independent testing, then information governance and clinical safety review

## Author
Your Name · your-email@example.com · LinkedIn link
