# -*- coding: utf-8 -*-
"""
Heuristic Nurse Rostering solver.

Pipeline:
  1.  Greedy constructive build that fills coverage demand day-by-day,
      preferring nurses who want the shift (SHIFT_ON_REQUESTS), avoiding
      those who don't, and balancing workload toward each nurse's
      MinTotalMinutes target.
  2.  Hill-Climbing local search using two move types:
        - Reassign: change the shift at (nurse, day) to another shift
          or to "off".
        - Swap: exchange shifts between two nurses on the same day.
      Both moves are evaluated with a single weighted cost
          total = soft_objective + HARD_PENALTY * (hard violations)
      so the climber is free to walk through infeasible space and
      eventually settles at a feasible (or near-feasible) local optimum.

Reads the same instance file as `main.py` (the SECTION_* `.txt` format
parsed by `problem_reader.py`) and writes the same set of outputs:
`solution.json`, `solution.md`, and a RosterViewer-compatible Roster XML
(`solution.xml`).  At the end it invokes the standalone `validator.py`
logic so the heuristic's objective can be compared head-to-head with the
exact PuLP run.

Usage:
    python heuristic_solver.py Instance2.txt --time-limit 30 --seed 42
"""

from __future__ import annotations
import argparse
import json
import os
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from problem_reader import Instance, parse_instance_from_file
from main import (
    write_solution_json,
    write_solution_xml,
    render_markdown,
    _resolve_ros_relpath,
)
import validator as val_module


# ---------------------------------------------------------------------------
# Types and cost functions
# ---------------------------------------------------------------------------

Assignment = Dict[Tuple[str, int], str]   # (nurse, day) -> shift code

HARD_PENALTY = 1_000_000   # weight per hard-constraint violation


def soft_cost(inst: Instance, A: Assignment) -> Tuple[int, Dict[str, int]]:
    """Coverage + shift-on + shift-off penalty (the true competition objective)."""
    nurses = list(inst.staff.keys())
    cov = 0
    for (d, s), cr in inst.cover.items():
        a = sum(1 for n in nurses if A.get((n, d)) == s)
        if a < cr.requirement:
            cov += cr.w_under * (cr.requirement - a)
        elif a > cr.requirement:
            cov += cr.w_over * (a - cr.requirement)
    on = sum(
        w for (n, d, s), w in inst.shift_on_requests.items()
        if A.get((n, d)) != s
    )
    off = sum(
        w for (n, d, s), w in inst.shift_off_requests.items()
        if A.get((n, d)) == s
    )
    return cov + on + off, {
        "coverage": cov,
        "shift_on_unmet": on,
        "shift_off_violated": off,
    }


def hard_violations(inst: Instance, A: Assignment) -> int:
    """Count hard-constraint violations in the assignment."""
    H = inst.horizon_days
    nurses = list(inst.staff.keys())
    n_viol = 0

    # H2: forbidden sequences (e.g. L -> E)
    for n in nurses:
        for d in range(H - 1):
            s1, s2 = A.get((n, d)), A.get((n, d + 1))
            if s1 and s2 and s2 in inst.shifts[s1].cannot_follow:
                n_viol += 1

    # H3: max shifts per type
    for n in nurses:
        cnt: Dict[str, int] = defaultdict(int)
        for d in range(H):
            s = A.get((n, d))
            if s:
                cnt[s] += 1
        sd = inst.staff[n]
        for s, c in cnt.items():
            cap = sd.max_shifts_per_type.get(s, 0)
            if c > cap:
                n_viol += c - cap

    # H4: total minutes range
    for n in nurses:
        sd = inst.staff[n]
        total = sum(
            inst.shifts[A[(n, d)]].length
            for d in range(H) if (n, d) in A
        )
        if total > sd.max_total_minutes:
            n_viol += 1
        if total < sd.min_total_minutes:
            n_viol += 1

    # H5/H6/H7: consecutive runs
    for n in nurses:
        sd = inst.staff[n]
        work = [(n, d) in A for d in range(H)]
        i = 0
        while i < H:
            j = i
            while j < H and work[j] == work[i]:
                j += 1
            length = j - i
            at_start = i == 0
            at_end = j == H
            if work[i]:
                if length > sd.max_consec_work:
                    n_viol += 1
                if length < sd.min_consec_work:
                    n_viol += 1
            else:
                if length < sd.min_consec_off and not at_start and not at_end:
                    n_viol += 1
            i = j

    # H8: max weekends
    for n in nurses:
        sd = inst.staff[n]
        wk = 0
        for k in range(H // 7):
            sat, sun = 7 * k + 5, 7 * k + 6
            if (sat < H and (n, sat) in A) or (sun < H and (n, sun) in A):
                wk += 1
        if wk > sd.max_weekends:
            n_viol += 1

    # H9: forced days off
    for n in nurses:
        for d in inst.staff[n].days_off:
            if (n, d) in A:
                n_viol += 1

    return n_viol


def cost(inst: Instance, A: Assignment) -> Tuple[int, int, int]:
    """Returns (weighted_total, soft_objective, hard_violation_count)."""
    soft, _ = soft_cost(inst, A)
    nv = hard_violations(inst, A)
    return soft + HARD_PENALTY * nv, soft, nv


# ---------------------------------------------------------------------------
# Greedy construction
# ---------------------------------------------------------------------------

def greedy_construct(inst: Instance, rng: random.Random) -> Assignment:
    """
    Walk through the cover requirements in chronological order; at each
    (day, shift) pick the required number of nurses that minimise the
    immediate scoring function:

        score = -100 * shift_on_weight        (want this shift here)
              + +100 * shift_off_weight       (don't want this shift)
              -   1  * hours_deficit_in_shifts (prefer underloaded)
              + +1000 if would create a forbidden sequence with neighbours
              + small random tie-breaker

    Filters: not already assigned, not on a forced day off, allowed by
    MaxShifts per type, and current per-type tally below cap.

    Other hard constraints (consecutive-run lengths, MinTotalMinutes,
    MaxWeekends) are intentionally NOT enforced here -- the hill climber
    repairs them later via the HARD_PENALTY weighting.
    """
    A: Assignment = {}
    H = inst.horizon_days
    nurses = list(inst.staff.keys())

    cover_order = sorted(inst.cover.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    for (d, s), cr in cover_order:
        is_weekend_day = (d % 7) in (5, 6)
        wk_idx = d // 7
        avail: List[Tuple[float, float, str]] = []
        for n in nurses:
            if (n, d) in A:
                continue
            sd = inst.staff[n]

            # ---- Hard filters (never produce a violating assignment) ----
            if d in sd.days_off:                                    # H9
                continue
            if sd.max_shifts_per_type.get(s, 0) <= 0:               # H3
                continue
            current_cnt = sum(1 for dd in range(H) if A.get((n, dd)) == s)
            if current_cnt >= sd.max_shifts_per_type.get(s, 0):     # H3
                continue
            mins_so_far = sum(
                inst.shifts[A[(n, dd)]].length
                for dd in range(H) if (n, dd) in A
            )
            if mins_so_far + inst.shifts[s].length > sd.max_total_minutes:  # H4
                continue

            # H5: would the new shift extend a run beyond MaxConsec?
            run_left = 0
            dd = d - 1
            while dd >= 0 and (n, dd) in A:
                run_left += 1
                dd -= 1
            run_right = 0
            dd = d + 1
            while dd < H and (n, dd) in A:
                run_right += 1
                dd += 1
            if run_left + 1 + run_right > sd.max_consec_work:
                continue

            # H2: forbidden sequence with either neighbour
            prev_s = A.get((n, d - 1)) if d > 0 else None
            if prev_s and s in inst.shifts[prev_s].cannot_follow:
                continue
            next_s = A.get((n, d + 1)) if d < H - 1 else None
            if next_s and next_s in inst.shifts[s].cannot_follow:
                continue

            # H8: would picking this nurse on a weekend day exceed MaxWeekends?
            if is_weekend_day:
                wk_used = 0
                for k in range(H // 7):
                    sat, sun = 7 * k + 5, 7 * k + 6
                    if (sat < H and (n, sat) in A) or (sun < H and (n, sun) in A):
                        wk_used += 1
                this_wk_already = (
                    (7 * wk_idx + 5 < H and (n, 7 * wk_idx + 5) in A)
                    or (7 * wk_idx + 6 < H and (n, 7 * wk_idx + 6) in A)
                )
                if not this_wk_already and wk_used >= sd.max_weekends:
                    continue

            # ---- Scoring ----
            score = 0.0
            score -= 100 * inst.shift_on_requests.get((n, d, s), 0)
            score += 100 * inst.shift_off_requests.get((n, d, s), 0)
            deficit_shifts = max(0, sd.min_total_minutes - mins_so_far) / 480.0
            score -= deficit_shifts
            avail.append((score, rng.random(), n))

        avail.sort()
        for _, _, n in avail[: cr.requirement]:
            A[(n, d)] = s

    return A


# ---------------------------------------------------------------------------
# Hill-climbing local search
# ---------------------------------------------------------------------------

def _apply_swap(A: Assignment, n1: str, d1: int, n2: str, d2: int) -> None:
    """Exchange shifts between (n1,d1) and (n2,d2). Calling twice undoes."""
    s1 = A.get((n1, d1))
    s2 = A.get((n2, d2))
    if s2 is None:
        A.pop((n1, d1), None)
    else:
        A[(n1, d1)] = s2
    if s1 is None:
        A.pop((n2, d2), None)
    else:
        A[(n2, d2)] = s1


def hill_climb(
    inst: Instance,
    A: Assignment,
    *,
    max_passes: int = 50,
    time_limit: float = 60.0,
    verbose: bool = True,
) -> Tuple[Assignment, int, int, int]:
    """First-improvement hill climbing with reassign + swap moves."""
    H = inst.horizon_days
    nurses = list(inst.staff.keys())
    shifts = list(inst.shifts.keys())

    cur_total, cur_soft, cur_viol = cost(inst, A)
    if verbose:
        print(
            f"[HC] start            total={cur_total:>9} "
            f"soft={cur_soft:>5} hard_viol={cur_viol}"
        )

    start = time.time()
    for pass_no in range(1, max_passes + 1):
        if time.time() - start > time_limit:
            break
        improved = False

        # ---- Reassign each (nurse, day) cell ----
        for n in nurses:
            for d in range(H):
                if time.time() - start > time_limit:
                    break
                cur_s = A.get((n, d))
                best_new = cur_s
                best_total = cur_total
                best_soft = cur_soft
                best_viol = cur_viol
                for new_s in [None] + shifts:
                    if new_s == cur_s:
                        continue
                    saved = A.get((n, d))
                    if new_s is None:
                        A.pop((n, d), None)
                    else:
                        A[(n, d)] = new_s
                    nt, ns, nv = cost(inst, A)
                    # revert
                    if saved is None:
                        A.pop((n, d), None)
                    else:
                        A[(n, d)] = saved
                    if nt < best_total:
                        best_total, best_soft, best_viol = nt, ns, nv
                        best_new = new_s
                if best_new != cur_s and best_total < cur_total:
                    if best_new is None:
                        A.pop((n, d), None)
                    else:
                        A[(n, d)] = best_new
                    cur_total, cur_soft, cur_viol = best_total, best_soft, best_viol
                    improved = True

        # ---- Swap shifts between two nurses on the same day ----
        for d in range(H):
            if time.time() - start > time_limit:
                break
            for i in range(len(nurses)):
                for j in range(i + 1, len(nurses)):
                    n1, n2 = nurses[i], nurses[j]
                    if A.get((n1, d)) == A.get((n2, d)):
                        continue
                    _apply_swap(A, n1, d, n2, d)
                    nt, ns, nv = cost(inst, A)
                    if nt < cur_total:
                        cur_total, cur_soft, cur_viol = nt, ns, nv
                        improved = True
                    else:
                        _apply_swap(A, n1, d, n2, d)  # revert

        # ---- Cross-day swap: move a shift from (n,d1) to (n,d2) ----
        # (essentially "swap with an empty cell of the same nurse")
        # This is the move that fixes Max-Weekends by relocating a
        # weekend assignment to a weekday.
        for n in nurses:
            if time.time() - start > time_limit:
                break
            for d1 in range(H):
                if (n, d1) not in A:
                    continue
                for d2 in range(H):
                    if d1 == d2 or (n, d2) in A:
                        continue
                    _apply_swap(A, n, d1, n, d2)
                    nt, ns, nv = cost(inst, A)
                    if nt < cur_total:
                        cur_total, cur_soft, cur_viol = nt, ns, nv
                        improved = True
                    else:
                        _apply_swap(A, n, d1, n, d2)  # revert

        # ---- Cross-nurse cross-day swap: (n1,d1) <-> (n2,d2) ----
        # Needed to fix tightly-coupled violations (e.g. H8 weekend caps)
        # where moving one cell alone is infeasible because the target
        # cell is occupied by someone else.
        for d1 in range(H):
            if time.time() - start > time_limit:
                break
            for d2 in range(d1 + 1, H):
                for i in range(len(nurses)):
                    for j in range(len(nurses)):
                        if i == j:
                            continue
                        n1, n2 = nurses[i], nurses[j]
                        s1 = A.get((n1, d1))
                        s2 = A.get((n2, d2))
                        if s1 is None and s2 is None:
                            continue
                        if s1 == s2:
                            # exchanging identical assignments is a no-op
                            continue
                        _apply_swap(A, n1, d1, n2, d2)
                        nt, ns, nv = cost(inst, A)
                        if nt < cur_total:
                            cur_total, cur_soft, cur_viol = nt, ns, nv
                            improved = True
                        else:
                            _apply_swap(A, n1, d1, n2, d2)  # revert

        if verbose:
            elapsed = time.time() - start
            print(
                f"[HC] pass {pass_no:>2} ({elapsed:5.1f}s)  "
                f"total={cur_total:>9} soft={cur_soft:>5} hard_viol={cur_viol}  "
                f"{'+' if improved else '='}"
            )
        if not improved:
            break

    # Final recompute as ground truth (defends against any drift in the
    # incremental cur_* tracking through the move loops above).
    final_total, final_soft, final_viol = cost(inst, A)
    return A, final_total, final_soft, final_viol


# ---------------------------------------------------------------------------
# Output adapter
# ---------------------------------------------------------------------------

def to_result(inst: Instance, A: Assignment, status: str) -> dict:
    """Convert an Assignment dict to the result shape expected by main.py output writers."""
    soft, _ = soft_cost(inst, A)
    nurses = list(inst.staff.keys())
    under = {}
    over = {}
    for (d, s), cr in inst.cover.items():
        a = sum(1 for n in nurses if A.get((n, d)) == s)
        under[(d, s)] = max(0, cr.requirement - a)
        over[(d, s)] = max(0, a - cr.requirement)
    return {
        "status": status,
        "objective": soft,
        "assignments": dict(A),
        "under": under,
        "over": over,
        "num_vars": 0,
        "num_constraints": 0,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Greedy + Hill-Climb heuristic for the Nurse Rostering Problem."
    )
    p.add_argument("instance", help="Path to .txt (SECTION_*) instance file")
    p.add_argument("--time-limit", type=float, default=30.0,
                   help="Wall-clock limit for hill climbing (seconds).")
    p.add_argument("--max-passes", type=int, default=50,
                   help="Max number of full HC sweeps.")
    p.add_argument("--seed", type=int, default=42,
                   help="RNG seed for greedy tie-breaking (each restart uses seed + 1000*r).")
    p.add_argument("--restarts", type=int, default=5,
                   help="Number of greedy+HC restarts; the lowest-cost result wins.")
    p.add_argument("--out-json", default="solution_heur.json")
    p.add_argument("--out-md", default="solution_heur.md")
    p.add_argument("--out-xml", default="solution_heur.xml")
    p.add_argument("--ros-xml", default=None,
                   help="Path written into <SchedulingPeriodFile> in the XML output.")
    p.add_argument("--quiet", action="store_true", help="Suppress hill-climb progress lines.")
    args = p.parse_args()

    instance_path = Path(args.instance)
    inst = parse_instance_from_file(args.instance)
    print(
        f"Parsed: horizon={inst.horizon_days}d, nurses={len(inst.staff)}, "
        f"shifts={len(inst.shifts)}, cover={len(inst.cover)}, "
        f"on-requests={len(inst.shift_on_requests)}, "
        f"off-requests={len(inst.shift_off_requests)}"
    )

    best_A: Optional[Assignment] = None
    best_total = float("inf")
    best_soft = best_viol = 0
    per_restart_budget = max(1.0, args.time_limit / max(1, args.restarts))
    overall_start = time.time()

    for r in range(args.restarts):
        if time.time() - overall_start > args.time_limit:
            break
        rng = random.Random(args.seed + 1000 * r)
        t0 = time.time()
        A0 = greedy_construct(inst, rng)
        g_total, g_soft, g_viol = cost(inst, A0)
        print(
            f"[Restart {r + 1}/{args.restarts}] greedy in {time.time() - t0:.2f}s "
            f"-- soft={g_soft}, hard={g_viol}, total={g_total}"
        )

        remaining = args.time_limit - (time.time() - overall_start)
        budget = max(1.0, min(per_restart_budget, remaining))
        t1 = time.time()
        A, total, soft, viol = hill_climb(
            inst,
            dict(A0),
            max_passes=args.max_passes,
            time_limit=budget,
            verbose=not args.quiet,
        )
        print(
            f"[Restart {r + 1}] HC finished in {time.time() - t1:.2f}s "
            f"-- soft={soft}, hard={viol}, total={total}"
        )

        if total < best_total:
            best_total, best_soft, best_viol = total, soft, viol
            best_A = dict(A)
            print(f"[Restart {r + 1}] *** new best: total={best_total} ***")

    assert best_A is not None
    A, total, soft, viol = best_A, best_total, best_soft, best_viol

    # Defensive recompute on best_A in case any incremental tracking drifted.
    soft, _ = soft_cost(inst, A)
    viol = hard_violations(inst, A)
    total = soft + HARD_PENALTY * viol
    print(
        f"[Best of {args.restarts}] soft={soft}, hard_violations={viol}, "
        f"weighted_total={total}"
    )

    status = "Heuristic-Feasible" if viol == 0 else f"Heuristic-Infeasible({viol})"
    result = to_result(inst, A, status)

    # Outputs (re-using main.py writers so the formats match the PuLP run)
    write_solution_json(result, inst, args.out_json)
    Path(args.out_md).write_text(render_markdown(result, inst), encoding="utf-8")

    xml_path = Path(args.out_xml)
    sp_file = _resolve_ros_relpath(args, instance_path, xml_path)
    write_solution_xml(result, inst, args.out_xml, scheduling_period_file=sp_file)
    print(f"Wrote: {args.out_json}, {args.out_md}, {args.out_xml}")
    print(f"  (SchedulingPeriodFile: {sp_file})")

    # ---- Independent validation using validator.py's logic ----
    val_inst = val_module.parse_instance(args.instance)
    sol_dict = json.loads(Path(args.out_json).read_text(encoding="utf-8"))
    val_res = val_module.check_solution(val_inst, sol_dict)
    print()
    print("=" * 60)
    print("Validator (independent re-check):")
    print(f"  feasible            : {val_res['feasible']}")
    print(f"  recomputed objective: {val_res['objective']}")
    for k, v in val_res["breakdown"].items():
        print(f"    {k}: {v}")
    if val_res["violations"]:
        print(f"  violations ({len(val_res['violations'])}):")
        for v in val_res["violations"][:10]:
            print(f"    - {v}")
        if len(val_res["violations"]) > 10:
            print(f"    ... and {len(val_res['violations']) - 10} more.")
    print("=" * 60)

    # ---- Comparison with the exact PuLP solution if available ----
    pulp_json = Path("solution.json")
    if pulp_json.is_file():
        try:
            pulp_obj = json.loads(pulp_json.read_text(encoding="utf-8")).get("objective")
        except (OSError, json.JSONDecodeError):
            pulp_obj = None
        if pulp_obj is not None:
            pulp_obj_f = float(pulp_obj)
            heur_obj_f = float(val_res["objective"])
            print()
            print("Comparison with exact PuLP solution (solution.json):")
            print(f"  PuLP objective      : {pulp_obj_f:g}")
            print(f"  Heuristic objective : {heur_obj_f:g}")
            gap = heur_obj_f - pulp_obj_f
            sign = "+" if gap >= 0 else ""
            if pulp_obj_f > 0:
                pct = 100.0 * gap / pulp_obj_f
                print(f"  Gap (heur - pulp)   : {sign}{gap:g}  ({sign}{pct:.2f}%)")
            else:
                print(f"  Gap (heur - pulp)   : {sign}{gap:g}")
            note = (
                "feasible" if val_res["feasible"]
                else f"INFEASIBLE ({len(val_res['violations'])} hard violations)"
            )
            print(f"  Heuristic status    : {note}")


if __name__ == "__main__":
    main()
