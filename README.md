# NHS Rota Optimiser V1 — Prototype

A local-first, rule-driven NHS rota planning starter. **Not approved for live clinical use.**

## Run
- Python 3.10+ required.
- Windows: double-click `run.bat`.
- macOS/Linux: `bash run.sh`.
- Open http://127.0.0.1:5000.

The app includes a responsive dashboard, doctor register, leave register, rota generation, saved rota and basic quality checks. It uses Flask, SQLite and Google OR-Tools CP-SAT. The Excel configuration workbook and Word user manual are in `config/` and `docs/`.

## Prototype limitations
Basic staffing coverage, leave/contract availability, one shift per doctor per day, consecutive SD/LD/LN limits, and post-three-night recovery are included in the solver starter. The exact rolling rule (max 3 LD in any 5 working shifts), every LD-to-SD transition nuance, prior-rota continuity, locks, swap/rebalance, full audit trail, and full fairness/quality validation are **not fully implemented**. The quality page is not a compliance certificate. Do not use for live clinical staffing until rules are completed, independently tested, and approved by clinical/operational governance, security and information governance.

## V1.0.1 hotfix
This package explicitly configures Flask to load templates from `app/templates` and static files from `app/static`. This fixes the `TemplateNotFound: page.html` error seen when launching the prototype.
