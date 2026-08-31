"""Resume text extraction — pdf / docx / txt. Text-first, cheap, no OCR."""
from __future__ import annotations

import re
from pathlib import Path

MAX_CHARS = 9000


def parse_resume(path: str | Path) -> str:
    """Dispatch on extension -> plain text (whitespace-collapsed, capped)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".pdf":
        import fitz  # pymupdf
        text = ""
        with fitz.open(p) as doc:
            for page in doc:
                text += page.get_text()
    elif ext in (".docx", ".doc"):
        import docx
        d = docx.Document(str(p))
        text = "\n".join(par.text for par in d.paragraphs)
        for table in d.tables:
            for row in table.rows:
                text += "\n" + " | ".join(cell.text for cell in row.cells)
    elif ext in (".txt", ".md"):
        text = p.read_text(errors="replace")
    else:
        raise ValueError(f"unsupported resume type: {ext} (pdf/docx/txt only)")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_CHARS]


def extract_contacts(text: str) -> dict:
    """Cheap deterministic contact extraction — phone + email."""
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    compact = re.sub(r"[\s\-().]", "", text)
    phones = re.findall(r"(?:\+?61)?04\d{8}", compact)
    phones = [("+61" + p[-9:]) if p.startswith("04") and len(p) == 12 else p
              for p in phones]
    seen, out = set(), []
    for ph in phones:
        if ph not in seen:
            seen.add(ph)
            out.append(ph)
    return {"emails": sorted(set(emails))[:3], "phones": out[:3]}
