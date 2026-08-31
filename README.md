# CrewKeep — hiring + staff retention for a small roofing business

Lean ATS + retention tracker for sub-10-staff trades businesses. Built for
Nigel's mate's roofing company (2026-08-31), reusing the xjobs patterns
(auth/sessions, LLM dispatcher, pymupdf parsing) stripped to one company.

## What it does

**Hiring (weeds out fake/bad applications):**
- Add an applicant (Seek / Gumtree / Facebook / referral) + upload resume
  (pdf/docx/txt) — parsed automatically
- One-click LLM screening report (~6s, ~$0.001 via DeepSeek):
  role fit, 0-100 score, red flags, consistency issues, AI-generation
  likelihood, licence/cert detection (White Card, Working at Heights, QBCC,
  EWP, first aid…), and layered phone-screen questions that break fake
  candidates
- Pipeline: new → phone_screen → interview → trial → hired / rejected /
  **pool** (the pre-screened "good but not now" list that kills panic hiring)
- Verification checklist (White Card, Working at Heights, QBCC licence,
  references, police check, first aid)
- Notes + timeline per applicant

**Retention (keeps the good long-term staff):**
- Staff records (role, rate, site, supervisor, skills/tickets)
- Onboarding milestones: day 1 / week 1 / month 1 / day 90
- Stay interviews (quarterly, the 3 questions: what keeps you here, what
  might tempt you away, what would make this better) with risk level
- Early-warning flags (absences, lateness, complaints, engagement drop)
- Exit records with reason — dashboard aggregates exits by reason so
  patterns show up instead of being anecdotes

## Dev

```bash
cd ~/crewkeep
./serve.sh                      # sources ~/jobhunt/.auth.env for LLM keys
                                # → http://0.0.0.0:8091 (first user = admin)
./.venv/bin/python -m pytest tests/ -q    # 27 tests, all green
./.venv/bin/python crewkeep.py users add <user> <pass> --name "Office"
```

LLM engine: `DEEPSEEK_API_KEY` (default, cheap) or `LLM_PROVIDER=anthropic`
+ `ANTHROPIC_API_KEY` (better judgement, ~10x cost). Model via `LLM_MODEL`.
PITFALL: `deepseek-v4-flash` returns empty content — use `deepseek-chat`.

## Data

SQLite in `data/` (CREWKEEP_DATA env override): `crewkeep.db` (app data) +
`users.db` (auth) + `resumes/` (uploaded files). Dev data is separate from
prod (bind-mounted on the NAS).

## Deploy (NAS)

See `deploy_to_nas.sh` — builds the image via Portainer, creates/recreates
stack `crewkeep` (Portainer stack id 26), adds the Caddy reverse-proxy entry
and Cloudflare tunnel ingress, then health-checks the public URL.
