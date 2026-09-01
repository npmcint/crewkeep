import pytest

import llm as llm_mod
import resume as resume_mod
import screening


RESUME_GOOD = """Mick Smith
0401 234 567 · mick.smith@email.com

Roof plumber with 12 years experience. White Card, Working at Heights,
EWP ticket. Installed Colorbond and tile roofs for ABC Roofing (2016-2024),
ran 2-man crews, fixed leaks on commercial jobs. First aid cert current.
References from my last two supervisors available."""

RESUME_FAKE = """John Doe
john.doe@mail.com

I am a highly motivated individual with excellent communication skills and
attention to detail. I am a fast learner, self motivated and willing to go
the extra mile. I have experience in various roofing projects and I am
looking for a new opportunity to grow. References available on request."""


def test_scan_licences():
    found = screening.scan_licences(RESUME_GOOD)
    assert "White Card" in found
    assert "Working at Heights" in found
    assert "EWP ticket" in found
    assert "First Aid" in found
    assert screening.scan_licences(RESUME_FAKE) == []


def test_template_hits():
    hits = screening.template_hits(RESUME_FAKE)
    assert "highly motivated individual" in hits
    assert len(hits) >= 3
    assert screening.template_hits(RESUME_GOOD) == []


def test_heuristics_contacts_and_flags():
    det = screening.heuristics(RESUME_GOOD)
    assert det["contacts"]["phones"] == ["0401234567"]
    assert "mick.smith@email.com" in det["contacts"]["emails"]
    assert det["flags"] == []
    det2 = screening.heuristics("No contact details here at all, just a name.")
    assert det2["flags"]  # no phone/email + very short
    assert "No phone or email" in det2["flags"][0]


def test_extract_json_variants():
    assert llm_mod.extract_json('{"a": 1}') == {"a": 1}
    assert llm_mod.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm_mod.extract_json('Sure, here it is: {"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        llm_mod.extract_json("no json here")


def test_screen_merges_deterministic(monkeypatch):
    def fake_llm(messages, timeout=120):
        return {"summary": "solid", "role_fit": "good", "score": 75,
                "years_experience": 12, "red_flags": [], "consistency_issues": [],
                "ai_generated_likelihood": "low", "ai_generated_reasons": [],
                "licences": ["QBCC licence"], "licence_gaps": [],
                "phone_screen_questions": ["Q1"], "verification_checks": ["White Card"],
                "verdict": "hire_priority", "notes_for_boss": "call him"}
    monkeypatch.setattr(llm_mod, "llm_json", fake_llm)
    rep = screening.screen("Mick Smith", "Roof plumber", RESUME_GOOD)
    # deterministic licences merged with LLM list, deduped
    assert "White Card" in rep["licences"]
    assert "QBCC licence" in rep["licences"]
    assert rep["verdict"] == "hire_priority"
    assert rep["_deterministic"]["template_hits"] == []


def test_screen_normalizes_bad_verdict(monkeypatch):
    def fake_llm(messages, timeout=120):
        return {"verdict": "maybe", "score": 50, "licences": []}
    monkeypatch.setattr(llm_mod, "llm_json", fake_llm)
    rep = screening.screen("X", "R", RESUME_GOOD)
    assert rep["verdict"] == "consider"


def test_screen_with_pool_injects_context(monkeypatch):
    captured = {}

    def fake_llm(messages, timeout=120):
        captured["user"] = messages[-1]["content"]
        return {"summary": "solid", "role_fit": "good", "score": 75,
                "years_experience": 12, "red_flags": [], "consistency_issues": [],
                "ai_generated_likelihood": "low", "ai_generated_reasons": [],
                "licences": [], "licence_gaps": [],
                "phone_screen_questions": ["Q1"], "verification_checks": [],
                "verdict": "hire_priority", "notes_for_boss": "call him",
                "boss_context": "Clear #1 of the pool — probe the licence before offering."}
    monkeypatch.setattr(llm_mod, "llm_json", fake_llm)
    pool = [{"name": "Alex Carter", "role_applied": "Roof labourer",
             "score": 10, "verdict": "likely_fake", "summary": "generic template"}]
    rep = screening.screen("Terry Jenkins", "Roof plumber", RESUME_GOOD, pool=pool)
    assert "POOL CONTEXT" in captured["user"]
    assert "Alex Carter" in captured["user"]
    assert "score 10" in captured["user"]
    assert rep["boss_context"] == "Clear #1 of the pool — probe the licence before offering."


def test_screen_without_pool_tells_llm_first_candidate(monkeypatch):
    captured = {}

    def fake_llm(messages, timeout=120):
        captured["user"] = messages[-1]["content"]
        return {"verdict": "consider", "score": 50, "licences": [],
                "boss_context": "First candidate in the pool."}
    monkeypatch.setattr(llm_mod, "llm_json", fake_llm)
    screening.screen("X", "R", RESUME_GOOD)
    assert "none yet" in captured["user"]
    assert "POOL CONTEXT" in captured["user"]


def test_screen_tolerates_missing_boss_context(monkeypatch):
    def fake_llm(messages, timeout=120):
        return {"verdict": "consider", "score": 50, "licences": []}
    monkeypatch.setattr(llm_mod, "llm_json", fake_llm)
    rep = screening.screen("X", "R", RESUME_GOOD)
    assert "boss_context" not in rep
    assert rep["verdict"] == "consider"


def test_resume_parse_txt(tmp_path):
    p = tmp_path / "cv.txt"
    p.write_text("line one\n\n\n\nline two")
    assert resume_mod.parse_resume(p) == "line one\n\nline two"
    with pytest.raises(ValueError):
        resume_mod.parse_resume(tmp_path / "cv.png")
