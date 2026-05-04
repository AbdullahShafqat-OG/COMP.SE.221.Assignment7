# -*- coding: utf-8 -*-
"""
Robust line-oriented reader for the SECTION_* rostering format.

- Skips lines where the first non-space char is '#'
- Sections are introduced by a line exactly matching: SECTION_<NAME>
- Section content continues until the next SECTION_* line or EOF
- Horizon is any integer found in the HORIZON section body (e.g., a line "14")
"""

from dataclasses import dataclass, field
from typing import Dict, Tuple, Set, Optional, List
import re

# -------------------------
# Data structures
# -------------------------

@dataclass
class ShiftDef:
    length: int
    cannot_follow: Set[str]

@dataclass
class StaffDef:
    id: str
    max_shifts_per_type: Dict[str, int]
    max_total_minutes: int
    min_total_minutes: int
    max_consec_work: int
    min_consec_work: int
    min_consec_off: int
    max_weekends: int
    days_off: Set[int] = field(default_factory=set)

@dataclass
class CoverReq:
    requirement: int
    w_under: int
    w_over: int

@dataclass
class Instance:
    horizon_days: int
    shifts: Dict[str, ShiftDef]
    staff: Dict[str, StaffDef]
    shift_on_requests: Dict[Tuple[str, int, str], int]
    shift_off_requests: Dict[Tuple[str, int, str], int]
    cover: Dict[Tuple[int, str], CoverReq]


# -------------------------
# Reading & sectionization
# -------------------------

def _iter_noncomment_lines(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            # Keep newline semantics simple: strip trailing newline; preserve inner spaces
            line = raw.rstrip("\n\r")
            # Skip lines that start with '#' after optional leading whitespace
            if line.lstrip().startswith("#"):
                continue
            # Skip pure empty lines to reduce noise
            if not line.strip():
                continue
            yield line

def _read_sections(path: str) -> Dict[str, List[str]]:
    """
    Returns: {SECTION_NAME: [lines]}
    """
    sections: Dict[str, List[str]] = {}
    cur: Optional[str] = None

    for line in _iter_noncomment_lines(path):
        m = re.fullmatch(r"\s*SECTION_([A-Z_]+)\s*", line)
        if m:
            cur = m.group(1)
            if cur not in sections:
                sections[cur] = []
            continue
        if cur is None:
            # Lines before the first SECTION_* are ignored
            continue
        sections[cur].append(line)

    return sections


# -------------------------
# Parsing helpers
# -------------------------

def _tokenize_csvish(lines: List[str]) -> List[str]:
    """
    Split a list of lines by commas and whitespace/newlines into tokens.
    """
    buf = ",".join(lines)
    toks = [t.strip() for t in re.split(r"[,\n\r]+", buf) if t.strip()]
    return toks


# -------------------------
# Public API
# -------------------------

def parse_instance_from_file(path: str) -> Instance:
    sections = _read_sections(path)

    # --- Horizon ---
    horiz_body = " ".join(sections.get("HORIZON", []))
    m = re.search(r"\b(\d+)\b", horiz_body)
    if not m:
        raise ValueError("No horizon length found in SECTION_HORIZON.")
    H = int(m.group(1))

    # --- Shifts ---
    # Body lines like:  D,480,    or   D,480,N|L    or  L,480,E   (single value)
    shifts: Dict[str, ShiftDef] = {}
    if "SHIFTS" in sections:
        for line in sections["SHIFTS"]:
            parts = line.split(",")
            if len(parts) < 2:
                continue
            sid = parts[0].strip()
            len_tok = parts[1].strip()
            if not re.fullmatch(r"[A-Za-z0-9_]+", sid):
                continue
            if not re.fullmatch(r"\d+", len_tok):
                continue
            length = int(len_tok)
            forbidden: Set[str] = set()
            if len(parts) >= 3 and parts[2].strip():
                for tok in parts[2].split("|"):
                    tok = tok.strip()
                    if tok:
                        forbidden.add(tok)
            shifts[sid] = ShiftDef(length=length, cannot_follow=forbidden)

    # --- Staff ---
    staff: Dict[str, StaffDef] = {}
    if "STAFF" in sections:
        # We parse line-by-line; each line is one employee row
        for line in sections["STAFF"]:
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) < 8:
                continue
            emp = parts[0]
            max_spec = parts[1]
            max_shifts_per_type: Dict[str, int] = {}
            for piece in re.split(r"[;|]+", max_spec):
                m2 = re.fullmatch(r"([A-Za-z0-9_]+)\s*=\s*(\d+)", piece.strip())
                if m2:
                    max_shifts_per_type[m2.group(1)] = int(m2.group(2))
            staff[emp] = StaffDef(
                id=emp,
                max_shifts_per_type=max_shifts_per_type,
                max_total_minutes=int(parts[2]),
                min_total_minutes=int(parts[3]),
                max_consec_work=int(parts[4]),
                min_consec_work=int(parts[5]),
                min_consec_off=int(parts[6]),
                max_weekends=int(parts[7]),
            )

    # --- Days off ---
    if "DAYS_OFF" in sections:
        for line in sections["DAYS_OFF"]:
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if not parts:
                continue
            emp = parts[0]
            if emp not in staff:
                continue
            days = [int(x) for x in parts[1:]]
            staff[emp].days_off |= set(days)

    # --- Shift ON requests ---
    shift_on_requests: Dict[Tuple[str, int, str], int] = {}
    if "SHIFT_ON_REQUESTS" in sections:
        for line in sections["SHIFT_ON_REQUESTS"]:
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) != 4:
                continue
            emp, day, sid, w = parts
            if re.fullmatch(r"\d+", day) and re.fullmatch(r"\d+", w):
                shift_on_requests[(emp, int(day), sid)] = int(w)

    # --- Shift OFF requests ---
    shift_off_requests: Dict[Tuple[str, int, str], int] = {}
    if "SHIFT_OFF_REQUESTS" in sections:
        for line in sections["SHIFT_OFF_REQUESTS"]:
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) != 4:
                continue
            emp, day, sid, w = parts
            if re.fullmatch(r"\d+", day) and re.fullmatch(r"\d+", w):
                shift_off_requests[(emp, int(day), sid)] = int(w)

    # --- Cover ---
    cover: Dict[Tuple[int, str], CoverReq] = {}
    if "COVER" in sections:
        for line in sections["COVER"]:
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) != 5:
                continue
            d, sid, req, w_under, w_over = parts
            if not (re.fullmatch(r"\d+", d) and re.fullmatch(r"\d+", req)
                    and re.fullmatch(r"\d+", w_under) and re.fullmatch(r"\d+", w_over)):
                continue
            cover[(int(d), sid)] = CoverReq(int(req), int(w_under), int(w_over))

    return Instance(
        horizon_days=H,
        shifts=shifts,
        staff=staff,
        shift_on_requests=shift_on_requests,
        shift_off_requests=shift_off_requests,
        cover=cover,
    )