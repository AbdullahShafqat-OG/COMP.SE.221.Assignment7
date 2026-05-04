# -*- coding: utf-8 -*-
"""
PuLP MIP model for the Nurse Rostering Problem (Second Nurse Rostering
Competition format).

Hard constraints
----------------
H1  At most one shift per nurse per day.
H2  Forbidden shift sequences (e.g. L cannot follow E).
H3  MaxShifts per shift type per nurse.
H4  MinTotalMinutes <= total worked minutes <= MaxTotalMinutes.
H5  Max consecutive working days.
H6  Min consecutive working days (also enforced at horizon boundaries).
H7  Min consecutive days off (relaxed at horizon boundaries: an off-block
    that touches day 0 or day H-1 may be shorter than the minimum, since
    it could conceivably extend outside the horizon).
H8  Max weekends worked (a weekend counts if Saturday or Sunday is worked).
H9  DAYS_OFF: forced days off.

Soft objective (minimised)
--------------------------
- Coverage under/over penalties from SECTION_COVER.
- Unmet SHIFT_ON_REQUESTS weights.
- Violated SHIFT_OFF_REQUESTS weights.
"""

from __future__ import annotations
from typing import Dict, Tuple, Any
import pulp

from problem_reader import Instance


def build_and_solve(
    inst: Instance,
    time_limit: int = 120,
    mip_gap: float = 0.01,
    msg: bool = True,
) -> Dict[str, Any]:
    nurses = list(inst.staff.keys())
    days = list(range(inst.horizon_days))
    shifts = list(inst.shifts.keys())
    H = inst.horizon_days

    prob = pulp.LpProblem("NurseRostering", pulp.LpMinimize)

    # ----- Decision variables -----
    x = {
        (n, d, s): pulp.LpVariable(f"x_{n}_{d}_{s}", cat="Binary")
        for n in nurses
        for d in days
        for s in shifts
    }
    # y[n,d] = 1 iff nurse n works any shift on day d
    y = {(n, d): pulp.LpVariable(f"y_{n}_{d}", cat="Binary") for n in nurses for d in days}

    # Coverage slack vars
    under = {
        (d, s): pulp.LpVariable(f"under_{d}_{s}", lowBound=0, cat="Integer")
        for (d, s) in inst.cover
    }
    over = {
        (d, s): pulp.LpVariable(f"over_{d}_{s}", lowBound=0, cat="Integer")
        for (d, s) in inst.cover
    }

    # Weekend worked indicator
    num_weeks = H // 7
    weekends = list(range(num_weeks))
    w = {
        (n, wk): pulp.LpVariable(f"w_{n}_{wk}", cat="Binary")
        for n in nurses
        for wk in weekends
    }

    # ----- Hard constraints -----

    # H1: link y to x; at most one shift per day (binary y already <= 1)
    for n in nurses:
        for d in days:
            prob += pulp.lpSum(x[n, d, s] for s in shifts) == y[n, d], f"link_y_{n}_{d}"

    # H2: forbidden shift sequences
    for s, sd in inst.shifts.items():
        for s_next in sd.cannot_follow:
            if s_next not in inst.shifts:
                continue
            for d in range(H - 1):
                for n in nurses:
                    prob += (
                        x[n, d, s] + x[n, d + 1, s_next] <= 1,
                        f"noseq_{n}_{d}_{s}_{s_next}",
                    )

    # H3: max shifts per type
    for n in nurses:
        sd = inst.staff[n]
        for s in shifts:
            max_s = sd.max_shifts_per_type.get(s, 0)
            prob += (
                pulp.lpSum(x[n, d, s] for d in days) <= max_s,
                f"maxtype_{n}_{s}",
            )

    # H4: total minutes range
    for n in nurses:
        sd = inst.staff[n]
        total_minutes = pulp.lpSum(
            inst.shifts[s].length * x[n, d, s] for d in days for s in shifts
        )
        prob += total_minutes <= sd.max_total_minutes, f"maxmin_{n}"
        prob += total_minutes >= sd.min_total_minutes, f"minmin_{n}"

    # H5: max consecutive work
    for n in nurses:
        mc = inst.staff[n].max_consec_work
        if mc < H:
            for t in range(H - mc):
                prob += (
                    pulp.lpSum(y[n, t + i] for i in range(mc + 1)) <= mc,
                    f"maxconsec_{n}_{t}",
                )

    # H6: min consecutive work shifts (enforced at boundaries too)
    for n in nurses:
        mn = inst.staff[n].min_consec_work
        for length in range(1, mn):
            # Inner pattern: off, work^length, off
            for t in range(1, H - length):
                prob += (
                    pulp.lpSum(y[n, t + i] for i in range(length))
                    - y[n, t - 1]
                    - y[n, t + length]
                    <= length - 1,
                    f"minw_{n}_t{t}_L{length}",
                )
            # Boundary start: work^length at day 0 followed by off
            if length < H:
                t = 0
                if t + length <= H - 1:
                    prob += (
                        pulp.lpSum(y[n, t + i] for i in range(length))
                        - y[n, t + length]
                        <= length - 1,
                        f"minw_{n}_start_L{length}",
                    )
                # Boundary end: off followed by work^length at end
                t = H - length
                if t - 1 >= 0:
                    prob += (
                        pulp.lpSum(y[n, t + i] for i in range(length))
                        - y[n, t - 1]
                        <= length - 1,
                        f"minw_{n}_end_L{length}",
                    )

    # H7: min consecutive days off (only inside horizon; lenient at boundaries)
    for n in nurses:
        mo = inst.staff[n].min_consec_off
        for length in range(1, mo):
            for t in range(1, H - length):
                # Forbid pattern: work, off^length, work
                prob += (
                    y[n, t - 1]
                    + y[n, t + length]
                    - pulp.lpSum(y[n, t + i] for i in range(length))
                    <= 1,
                    f"mino_{n}_t{t}_L{length}",
                )

    # H8: max weekends worked
    for n in nurses:
        sd = inst.staff[n]
        for wk in weekends:
            sat = 7 * wk + 5
            sun = 7 * wk + 6
            if sat < H:
                prob += w[n, wk] >= y[n, sat], f"wkend_sat_{n}_{wk}"
            if sun < H:
                prob += w[n, wk] >= y[n, sun], f"wkend_sun_{n}_{wk}"
        prob += (
            pulp.lpSum(w[n, wk] for wk in weekends) <= sd.max_weekends,
            f"maxwkend_{n}",
        )

    # H9: forced days off
    for n in nurses:
        for d in inst.staff[n].days_off:
            if 0 <= d < H:
                prob += y[n, d] == 0, f"dayoff_{n}_{d}"

    # ----- Coverage linking -----
    for (d, s), cr in inst.cover.items():
        prob += (
            pulp.lpSum(x[n, d, s] for n in nurses) + under[d, s] - over[d, s]
            == cr.requirement,
            f"cover_{d}_{s}",
        )

    # ----- Objective -----
    obj_terms = []
    for (d, s), cr in inst.cover.items():
        obj_terms.append(cr.w_under * under[d, s])
        obj_terms.append(cr.w_over * over[d, s])
    # Shift-on requests: penalty when NOT assigned
    for (n, d, s), wt in inst.shift_on_requests.items():
        if (n, d, s) in x:
            obj_terms.append(wt * (1 - x[n, d, s]))
    # Shift-off requests: penalty when assigned
    for (n, d, s), wt in inst.shift_off_requests.items():
        if (n, d, s) in x:
            obj_terms.append(wt * x[n, d, s])

    prob += pulp.lpSum(obj_terms), "TotalSoftPenalty"

    # ----- Solve -----
    solver = pulp.PULP_CBC_CMD(msg=msg, timeLimit=time_limit, gapRel=mip_gap)
    prob.solve(solver)

    # ----- Extract -----
    assignments: Dict[Tuple[str, int], str] = {}
    for n in nurses:
        for d in days:
            for s in shifts:
                v = x[n, d, s].value()
                if v is not None and v > 0.5:
                    assignments[(n, d)] = s
                    break

    return {
        "status": pulp.LpStatus[prob.status],
        "objective": pulp.value(prob.objective),
        "assignments": assignments,
        "under": {k: (v.value() or 0) for k, v in under.items()},
        "over": {k: (v.value() or 0) for k, v in over.items()},
        "num_vars": len(prob.variables()),
        "num_constraints": len(prob.constraints),
    }
