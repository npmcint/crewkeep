"""Screening engine — weed out fake/bad applications.

Two layers:
1. DETERMINISTIC scans (no LLM, instant): licence/cert mentions relevant to
   Australian roofing (White Card, Working at Heights, QBCC licence, EWP…),
   contact presence, template-phrase detection, resume length.
2. LLM report (DeepSeek by default, ~$0.001/call): consistency check, red
   flags, AI-generation likelihood, role fit, verdict, and the layered
   phone-screen questions that break fake applicants (the '5 layers of
   follow-up' pattern: fakes collapse at layer 3).

Everything the LLM says is advisory — the boss makes the call.
"""
from __future__ import annotations

import re

import db  # noqa: F401  (module init; keeps data dir consistent)
import llm as llm_mod
import resume as resume_mod

# (canonical name, regex) — Australian roofing-relevant certs/tickets
LICENCE_PATTERNS = [
    ("White Card", r"(white\s*card|whs\s*construction|ohs\s*construction|general\s+construction\s+induction|prepare\s+to\s+work\s+safely)"),
    ("Working at Heights", r"(working\s*at\s*heights|work\s*at\s*heights|height\s*safety)"),
    ("QBCC licence", r"(qbcc|building\s*licence|licence\s*no|licensed\s*roofer)"),
    ("EWP ticket", r"\bewp\b|elevated\s*work\s*platform|boom\s*lift|scissor\s*lift"),
    ("First Aid", r"(first\s*aid|senior\s*first\s*aid)"),
    ("Asbestos awareness", r"(asbestos\s*awareness|asbestos\s*removal)"),
    ("Police check", r"(police\s*check|national\s*police|blue\s*card)"),
    ("Driver licence", r"(open\s*[cC]\s*licence|car\s*licence|driver'?s?\s*licence|drivers?\s*licen[cs]e)"),
    ("Roof/height ticket", r"(roof\s*plumber|colorbond|metal\s*roof|roofing\s*experience|slate|tile\s*roof)"),
]

# phrases that scream "template / mass application" (weak signal alone)
TEMPLATE_PHRASES = [
    "i am a hard worker", "hard working and reliable", "team player",
    "excellent communication skills", "highly motivated individual",
    "attention to detail", "fast learner", "willing to learn",
    "self motivated", "go the extra mile", "references available on request",
]


def scan_licences(text: str) -> list[str]:
    low = text.lower()
    found = []
    for name, pat in LICENCE_PATTERNS:
        if re.search(pat, low):
            found.append(name)
    return found


def template_hits(text: str) -> list[str]:
    low = re.sub(r"[^a-z ]", " ", text.lower())
    return [p for p in TEMPLATE_PHRASES if p in low]


def heuristics(text: str) -> dict:
    contacts = resume_mod.extract_contacts(text)
    licences = scan_licences(text)
    templates = template_hits(text)
    flags = []
    if not contacts["phones"] and not contacts["emails"]:
        flags.append("No phone or email found in the resume")
    if len(text) < 300:
        flags.append("Resume is very short (<300 chars) — little verifiable detail")
    if len(templates) >= 3:
        flags.append(f"{len(templates)} generic template phrases "
                     f"({', '.join(templates[:4])}) — possible mass application")
    return {
        "licences": licences,
        "template_hits": templates,
        "contacts": contacts,
        "flags": flags,
    }


SYSTEM_PROMPT = """You are an experienced roofing-industry hiring manager's assistant. You review ONE job applicant's resume and produce a candid, structured screening report. You are ruthless about inconsistency and AI-generated filler, and fair to genuine tradies who write short, plain resumes.

Rules:
- Base EVERY claim on the resume text provided. Never invent facts, dates, or experience.
- Experience claims must trace to the resume; unverifiable claims are flagged, not assumed true.
- Short, plain, grammatically imperfect resumes from genuine tradies are NORMAL — do not penalise brevity, lack of design, or poor English. Penalise: vague self-praise with zero specifics, contradictory dates/titles, copied-sounding boilerplate, and claims that sound too perfect.
- "ai_generated_likelihood" is high only with real evidence (generic buzzword stacking, template phrasing, no concrete details, perfect but empty structure). Low for plain-but-specific resumes.
- phone_screen_questions: 4-6 questions, including 2-3 layered follow-ups ("why did you do it that way rather than the other way?") that break candidates who only rehearsed surface answers.
- "verdict": hire_priority (clearly strong + consistent), consider (worth a phone screen), weak (missing essentials / poor fit / thin detail), likely_fake (strong indicators of fabricated or AI-generated application).
- score: 0-100 overall recommendation to interview.
- notes_for_boss: 2-3 plain-English sentences the boss can read on his phone.
- boss_context: 2-3 plain-English sentences comparing THIS candidate with the REST of the pool (use ONLY the candidates listed in POOL CONTEXT — never invent others): where they sit, what stands out vs the others, and the 1-2 things to probe in the phone screen before offering anything (e.g. licence jurisdiction, relocation, employment gaps, ticket expiry). If POOL CONTEXT says there are no other candidates, state that this is the first/only candidate so far and what to verify.

Reply with ONLY a JSON object, no prose, with EXACTLY these keys:
{"summary": str, "role_fit": "strong|good|weak|poor", "score": int 0-100,
 "years_experience": int|null, "red_flags": [{"flag": str, "detail": str}],
 "ai_generated_likelihood": "low|medium|high",
 "ai_generated_reasons": [str], "licences": [str], "licence_gaps": [str],
 "consistency_issues": [str], "phone_screen_questions": [str],
 "verification_checks": [str], "verdict": "hire_priority|consider|weak|likely_fake",
 "notes_for_boss": str, "boss_context": str}"""


def screen(name: str, role_applied: str, resume_text: str,
           pool: list[dict] | None = None) -> dict:
    """Full screening report: deterministic scan + LLM report merged.

    pool: other in-play applicants (name/role/score/verdict/summary) so the
    LLM can rank this candidate against the rest for boss_context.
    """
    det = heuristics(resume_text)
    if pool:
        lines = []
        for p in pool:
            summary = (p.get("summary") or "").strip()[:200]
            lines.append(
                f"- {p.get('name') or '?'} ({p.get('role_applied') or 'role not stated'}): "
                f"score {p.get('score')}, verdict {p.get('verdict') or 'unscreened'}"
                + (f" — {summary}" if summary else ""))
        pool_block = "\n".join(lines)
    else:
        pool_block = "(none yet — this is the first screened candidate)"
    user = (
        f"ROLE APPLIED FOR: {role_applied or 'not stated'}\n"
        f"CANDIDATE NAME: {name}\n"
        f"RESUME TEXT:\n{resume_text[:8000]}\n"
        f"\nDETERMINISTIC SCAN (use these, don't repeat them as discoveries):\n"
        f"- licences/certs found in text: {det['licences'] or 'none'}\n"
        f"- template phrases found: {det['template_hits'] or 'none'}\n"
        f"- contacts found: {det['contacts']}\n"
        f"- automatic flags: {det['flags'] or 'none'}\n"
        f"\nPOOL CONTEXT (other applicants in play — for boss_context ONLY):\n"
        f"{pool_block}\n"
        f"Produce the screening report JSON now."
    )
    report = llm_mod.llm_json([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ])
    # merge deterministic licence scan into the report (union, dedup)
    known = set(report.get("licences") or [])
    for l in det["licences"]:
        if l not in known:
            known.add(l)
    report["licences"] = sorted(known)
    report["_deterministic"] = det
    report["verdict"] = (report.get("verdict") or "consider")
    if report["verdict"] not in db.VERDICTS:
        report["verdict"] = "consider"
    return report
