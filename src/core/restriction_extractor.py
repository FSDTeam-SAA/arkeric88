"""
Deterministic extraction of structured restriction codes from free-text answers
to Q11 ("Are there any activities you'd like to avoid, or things you can't do?").

Per AI_DEVELOPER_CHANGES.md #4, this is explicitly NOT a medical assessment. It
is a best-effort keyword match used only to (a) merge with any predefined chip
codes the frontend already submitted and (b) surface phrases the system could
not classify so a human/concierge can review them. It never asserts a property
is safe -- see retreat_scoring.py, which always marks restriction_status as
"unverified" until the workbook gains real Activity/Restriction Tags
(tracked in BACKEND_DEVELOPER_CHANGES.md "Database Changes").
"""

import re
from dataclasses import dataclass, field
from typing import List

from src.core.answer_mappings import ACCESSIBILITY_KEYWORDS, RESTRICTION_KEYWORDS


@dataclass
class ExtractedRestrictions:
    codes: List[str] = field(default_factory=list)
    accessibility_needs: List[str] = field(default_factory=list)
    unresolved_text: List[str] = field(default_factory=list)
    original_text: str = ""


def _split_clauses(text: str) -> List[str]:
    return [
        clause.strip(" .")
        for clause in re.split(r",|\band\b|;", text, flags=re.IGNORECASE)
        if clause.strip(" .")
    ]


def extract_restrictions(text: str, submitted_codes: List[str]) -> ExtractedRestrictions:
    """
    Merge frontend-submitted chip codes with codes/accessibility needs extracted
    from free text. The original text is always preserved unmodified.
    """
    text = text or ""
    lower_text = text.lower()

    codes = list(dict.fromkeys(submitted_codes or []))
    accessibility_needs: List[str] = []
    matched_clause_indices: set[int] = set()

    clauses = _split_clauses(text)
    lower_clauses = [clause.lower() for clause in clauses]

    for code, keywords in RESTRICTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in lower_text:
                if code not in codes:
                    codes.append(code)
                for index, clause in enumerate(lower_clauses):
                    if keyword in clause:
                        matched_clause_indices.add(index)
                break

    for keyword in ACCESSIBILITY_KEYWORDS:
        if keyword in lower_text:
            accessibility_needs.append(keyword)
            for index, clause in enumerate(lower_clauses):
                if keyword in clause:
                    matched_clause_indices.add(index)

    unresolved_text = [
        clause for index, clause in enumerate(clauses) if index not in matched_clause_indices
    ]

    return ExtractedRestrictions(
        codes=codes,
        accessibility_needs=list(dict.fromkeys(accessibility_needs)),
        unresolved_text=unresolved_text,
        original_text=text,
    )
