# -*- coding: utf-8 -*-
"""
Standalone validator for Nurse Rostering solutions.

Re-parses the instance file and the solution JSON from scratch, then:
  - Checks every hard constraint and reports violations.
  - Recomputes the soft-constraint objective independently.
  - Compares the recomputed objective with the value reported by the solver.

Standard library only. No PuLP / no shared imports with the model code.

Usage:
    python validator.py Instance2.ros solution.json
"""

from __future__ import annotations
import argparse
import json
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Instance parsing (self-contained, mirrors problem_reader.py for independence)
# ---------------------------------------------------------------------------

def _read_sections(path: str) -> Dict[str, List[str]]:
    sections: Dict[str, List[str]] = {}
    cur = None
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = re.fullmatch(r"\s*SECTION_([A-Z_]+)\s*", line)
            if m:
                cur = m.group(1)
                sections.setdefault(cur, [])
                continue
            if cur is not None:
                sections[cur].append(line)
    return sections


def parse_instance(path: str) -> Dict[str, Any]:
    secs = _read_sections(path)

    horiz_match = re.search(r"\b(\d+)\b", " ".join(secs.get("HORIZON", [])))
    if not horiz_match:
        raise ValueError("HORIZON section missing or empty.")
    H = int(horiz_match.group(1))

    shifts: Dict[str, Dict[str, Any]] = {}
    for line in secs.get("SHIFTS", []):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        sid, length = parts[0], int(parts[1])
        forbidden = set()
        if len(parts) > 2 and parts[2]:
            forbidden = {x for x in parts[2].split("|") if x}
        shifts[sid] = {"length": length, "cannot_follow": forbidden}

    staff: Dict[str, Dict[str, Any]] = {}
    for line in secs.get("STAFF", []):
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) < 8:
            continue
        emp = parts[0]
        max_per_type: Dict[str, int] = {}
        for piece in re.split(r"[;|]+", parts[1]):
            mm = re.fullmatch(r"([A-Za-z0-9_]+)\s*=\s*(\d+)", piece.strip())
            if mm:
                max_per_type[mm.group(1)] = int(mm.group(2))
        staff[emp] = {
            "id": emp,
            "max_shifts_per_type": max_per_type,
            "max_total_minutes": int(parts[2]),
            "min_total_minutes": int(parts[3]),
            "max_consec_work": int(parts[4]),
            "min_consec_work": int(parts[5]),
            "min_consec_off": int(parts[6]),
            "max_weekends": int(parts[7]),
            "days_off": set(),
        }

    for line in secs.get("DAYS_OFF", []):
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if not parts or parts[0] not in staff:
            continue
        for d in parts[1:]:
            if d.isdigit():
                staff[parts[0]]["days_off"].add(int(d))

    on_req: Dict[Tuple[str, int, str], int] = {}
    for line in secs.get("SHIFT_ON_REQUESTS", []):
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) == 4 and parts[1].isdigit() and parts[3].isdigit():
            on_req[(parts[0], int(parts[1]), parts[2])] = int(parts[3])

    off_req: Dict[Tuple[str, int, str], int] = {}
    for line in secs.get("SHIFT_OFF_REQUESTS", []):
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) == 4 and parts[1].isdigit() and parts[3].isdigit():
            off_req[(parts[0], int(parts[1]), parts[2])] = int(parts[3])

    cover: Dict[Tuple[int, str], Dict[str, int]] = {}
    for line in secs.get("COVER", []):
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) == 5 and all(parts[i].isdigit() for i in (0, 2, 3, 4)):
            cover[(int(parts[0]), parts[1])] = {
                "requirement": int(parts[2]),
                "w_under": int(parts[3]),
                "w_over": int(parts[4]),
            }

    return {
        "horizon": H,
        "shifts": shifts,
        "staff": staff,
        "on_req": on_req,
        "off_req": off_req,
        "cover": cover,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _runs(seq: List[bool]) -> List[Tuple[bool, int, int]]:
    """Group seq into (value, start, length) runs."""
    out = []
    i = 0
    while i < len(seq):
        j = i
        while j < len(seq) and seq[j] == seq[i]:
            j += 1
        out.append((seq[i], i, j - i))
        i = j
    return out


def check_solution(inst: Dict[str, Any], sol: Dict[str, Any]) -> Dict[str, Any]:
    H = inst["horizon"]
    nurses = list(inst["staff"].keys())
    shifts = inst["shifts"]
    violations: List[str] = []

    # Build assignment table
    assign: Dict[Tuple[str, int], str] = {}
    for entry in sol.get("assignments", []):
        n = entry.get("nurse")
        d = entry.get("day")
        s = entry.get("shift")
        if n not in inst["staff"]:
            violations.append(f"Unknown nurse '{n}' in solution.")
            continue
        if not isinstance(d, int) or not (0 <= d < H):
            violations.append(f"Day {d} out of horizon for nurse {n}.")
            continue
        if s not in shifts:
            violations.append(f"Unknown shift '{s}' (nurse {n}, day {d}).")
            continue
        if (n, d) in assign:
            violations.append(
                f"H1: nurse {n} has multiple shifts on day {d} "
                f"({assign[(n, d)]} and {s})."
            )
        assign[(n, d)] = s

    # H2: forbidden sequences
    for n in nurses:
        for d in range(H - 1):
            s1 = assign.get((n, d))
            s2 = assign.get((n, d + 1))
            if s1 and s2 and s2 in shifts[s1]["cannot_follow"]:
                violations.append(
                    f"H2: forbidden sequence {s1}->{s2} for nurse {n} (day {d} -> {d + 1})."
                )

    # H3: max shifts per type
    for n in nurses:
        cnt: Dict[str, int] = defaultdict(int)
        for d in range(H):
            s = assign.get((n, d))
            if s:
                cnt[s] += 1
        sd = inst["staff"][n]
        for s, c in cnt.items():
            mx = sd["max_shifts_per_type"].get(s, 0)
            if c > mx:
                violations.append(
                    f"H3: nurse {n} works shift {s} {c} times (max {mx})."
                )

    # H4: total minutes range
    for n in nurses:
        sd = inst["staff"][n]
        total = sum(
            shifts[assign[(n, d)]]["length"]
            for d in range(H)
            if (n, d) in assign
        )
        if total > sd["max_total_minutes"]:
            violations.append(
                f"H4: nurse {n} total minutes {total} > max {sd['max_total_minutes']}."
            )
        if total < sd["min_total_minutes"]:
            violations.append(
                f"H4: nurse {n} total minutes {total} < min {sd['min_total_minutes']}."
            )

    # H5/H6/H7: consecutive runs
    for n in nurses:
        sd = inst["staff"][n]
        work = [(n, d) in assign for d in range(H)]
        runs = _runs(work)
        for typ, start, length in runs:
            at_start = start == 0
            at_end = start + length == H
            if typ:  # working block
                if length > sd["max_consec_work"]:
                    violations.append(
                        f"H5: nurse {n} has work block of length {length} "
                        f"starting day {start} (max {sd['max_consec_work']})."
                    )
                # min consec work: enforce at boundaries too
                if length < sd["min_consec_work"]:
                    violations.append(
                        f"H6: nurse {n} has work block of length {length} "
                        f"starting day {start} (min {sd['min_consec_work']})."
                    )
            else:  # off block
                # min consec off: lenient at boundaries
                if length < sd["min_consec_off"] and not at_start and not at_end:
                    violations.append(
                        f"H7: nurse {n} has off block of length {length} "
                        f"starting day {start} (min {sd['min_consec_off']})."
                    )

    # H8: max weekends
    num_weeks = H // 7
    for n in nurses:
        sd = inst["staff"][n]
        wk_count = 0
        for wk in range(num_weeks):
            sat, sun = 7 * wk + 5, 7 * wk + 6
            in_sat = sat < H and (n, sat) in assign
            in_sun = sun < H and (n, sun) in assign
            if in_sat or in_sun:
                wk_count += 1
        if wk_count > sd["max_weekends"]:
            violations.append(
                f"H8: nurse {n} works {wk_count} weekends (max {sd['max_weekends']})."
            )

    # H9: forced days off
    for n in nurses:
        for d in inst["staff"][n]["days_off"]:
            if (n, d) in assign:
                violations.append(
                    f"H9: nurse {n} assigned shift {assign[(n, d)]} on forced day-off {d}."
                )

    # ---- Soft objective ----
    breakdown: Dict[str, int] = {}
    cov_pen = 0
    for (d, s), cr in inst["cover"].items():
        assigned = sum(1 for n in nurses if assign.get((n, d)) == s)
        diff = cr["requirement"] - assigned
        if diff > 0:
            cov_pen += cr["w_under"] * diff
        elif diff < 0:
            cov_pen += cr["w_over"] * (-diff)
    breakdown["coverage"] = cov_pen

    on_pen = 0
    for (n, d, s), wt in inst["on_req"].items():
        if assign.get((n, d)) != s:
            on_pen += wt
    breakdown["shift_on_requests_unmet"] = on_pen

    off_pen = 0
    for (n, d, s), wt in inst["off_req"].items():
        if assign.get((n, d)) == s:
            off_pen += wt
    breakdown["shift_off_requests_violated"] = off_pen

    total_obj = cov_pen + on_pen + off_pen

    return {
        "feasible": len(violations) == 0,
        "violations": violations,
        "objective": total_obj,
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Validate a nurse rostering solution.")
    ap.add_argument("instance", help="Path to .ros instance file")
    ap.add_argument("solution", help="Path to solution JSON file")
    args = ap.parse_args()

    inst = parse_instance(args.instance)
    with open(args.solution, "r", encoding="utf-8") as f:
        sol = json.load(f)

    res = check_solution(inst, sol)

    print("=" * 60)
    print(f"Feasible: {res['feasible']}")
    if res["violations"]:
        print(f"Violations ({len(res['violations'])}):")
        for v in res["violations"]:
            print(f"  - {v}")
    else:
        print("No hard-constraint violations.")
    print("-" * 60)
    print(f"Recomputed objective: {res['objective']}")
    print("Breakdown:")
    for k, v in res["breakdown"].items():
        print(f"  {k}: {v}")

    sol_obj = sol.get("objective")
    if sol_obj is not None:
        print("-" * 60)
        print(f"Solver-reported objective: {sol_obj}")
        if abs(sol_obj - res["objective"]) > 1e-6:
            print("WARNING: solver objective does not match recomputed objective.")
            return 2
        print("Objectives match.")
    print("=" * 60)

    return 0 if res["feasible"] else 1


if __name__ == "__main__":
    sys.exit(main())
