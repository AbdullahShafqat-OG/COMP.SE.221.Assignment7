# COMP.SE.221 Assignment 7 — Nurse Rostering Optimiser

A clean, modular MIP solver for the **Second International Nurse Rostering
Competition** instance format, written in Python with [PuLP](https://coin-or.github.io/pulp/)
and the bundled CBC solver.

## Project layout

| File | Purpose |
|---|---|
| `problem_reader.py` | Parses the `SECTION_*` text instance format (`Instance2.txt`). |
| `model.py`          | Builds the PuLP MIP (vars, hard constraints, soft objective) and solves it. |
| `main.py`           | CLI runner. Parses, solves, and writes JSON / Markdown / XML outputs. |
| `heuristic_solver.py` | Greedy + Hill-Climbing heuristic. Same instance and same XML output format as `main.py`; reuses `validator.py` for the feasibility check and objective comparison. |
| `validator.py`      | Standalone (stdlib-only) feasibility checker and objective recomputer. |
| `Instance2.txt`     | Section-format instance (parsed by `problem_reader.py`). |
| `Instance2.ros`     | XML-format twin of the same instance (used to read `<StartDate>` for the Solution XML). |

Outputs (created on each run):

| File | Content |
|---|---|
| `solution.json` | Machine-readable solution: status, objective, list of `(nurse, day, shift)` assignments. |
| `solution.md`   | Human-readable Markdown roster, per-nurse summary, and coverage table. |
| `solution.xml`  | Staff Roster Solutions Roster XML (`Roster.xsd`). Drop this into the [RosterViewer](https://www.staffrostersolutions.com/rvw/1.4.1/) along with `Instance2.ros` to visualise. |

## Setup

The repo ships with a `venv/`. To create one from scratch:

```bash
python -m venv venv
# Windows (Git Bash / PowerShell)
./venv/Scripts/python.exe -m pip install pulp
# macOS / Linux
./venv/bin/python -m pip install pulp
```

PuLP brings its own CBC binary, so no separate solver install is needed.

## Run the solver

From the project root:

```bash
# Windows (Git Bash)
./venv/Scripts/python.exe main.py Instance2.txt --time-limit 180 --gap 0.005

# Windows (PowerShell / cmd)
venv\Scripts\python.exe main.py Instance2.txt --time-limit 180 --gap 0.005

# macOS / Linux
./venv/bin/python main.py Instance2.txt --time-limit 180 --gap 0.005
```

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--time-limit SECONDS` | `120`            | CBC wall-clock budget. |
| `--gap FRACTION`       | `0.01`           | Relative MIP gap (e.g. `0.005` = 0.5%). |
| `--out-json PATH`      | `solution.json`  | Where to write the JSON solution. |
| `--out-md PATH`        | `solution.md`    | Where to write the Markdown roster. |
| `--out-xml PATH`       | `solution.xml`   | Where to write the RosterViewer Roster XML. |
| `--ros-xml PATH`       | `<instance>.ros` | Path written into `<SchedulingPeriodFile>` (resolved relative to the XML output). |
| `--quiet`              | off              | Silence CBC's own log output. |

The Markdown roster is also echoed to stdout.

## Run the heuristic (Greedy + Hill Climbing)

`heuristic_solver.py` is an approximate alternative to the exact MIP. It
uses a strict greedy constructor (every assignment respects all hard
constraints checkable at construction time) followed by a hill-climbing
local search with three move types:

1. **Reassign** a single (nurse, day) cell to a different shift or to off.
2. **Swap** shifts between two nurses on the same day.
3. **Swap** shifts between two cells across different days
   (same nurse or different nurses).

Acceptance criterion: `total = soft_objective + 1_000_000 * hard_violations`,
so the climber drives any greedy-induced violations (typically `min consecutive`
or `min total minutes`) out before continuing on soft-cost reduction. Multi-start
restarts with different RNG seeds combat local optima.

```bash
./venv/Scripts/python.exe heuristic_solver.py Instance2.txt \
    --time-limit 120 --restarts 5 --seed 42 --quiet
```

It writes `solution_heur.json`, `solution_heur.md`, and `solution_heur.xml`
in exactly the same formats as the exact solver, then runs the
`validator.py` logic and prints a side-by-side comparison with the exact
PuLP run (it reads `solution.json` if present).

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--time-limit SECONDS` | `30`  | Total wall-clock budget across all restarts. |
| `--restarts N`         | `5`   | Number of greedy + hill-climb restarts. |
| `--seed S`             | `42`  | Base RNG seed; restart `r` uses `S + 1000*r`. |
| `--max-passes N`       | `50`  | Max HC sweeps per restart. |
| `--out-json/-md/-xml`  |       | Output paths (default `solution_heur.*`). |
| `--ros-xml PATH`       |       | Path embedded in `<SchedulingPeriodFile>`. |
| `--quiet`              | off   | Suppress per-pass HC progress lines. |

Sample comparison on `Instance2.txt`:

| Solver        | Objective | Feasible | Wall time |
|---|---|---|---|
| PuLP (CBC)    | **833**   | yes      | ~3 s |
| Heuristic     | 1728      | yes      | ~5 s (1 restart hits feasible) |

The heuristic is much faster per iteration but typically lands ~2x off the
optimum on this instance — exactly the trade-off expected from a
local-search method versus an exact MIP.

## Validate a solution

`validator.py` is fully independent — it does not import PuLP or anything from
`model.py` / `problem_reader.py`. It re-parses the instance from scratch,
checks every hard constraint, recomputes the objective, and compares it to
the value reported by the solver.

```bash
./venv/Scripts/python.exe validator.py Instance2.txt solution.json
```

Exit codes: `0` = feasible and objectives match, `1` = infeasible,
`2` = objective mismatch.

## Visualise in the RosterViewer

1. Open <https://www.staffrostersolutions.com/rvw/1.4.1/>.
2. Load the **instance** file: `Instance2.ros` (the XML form, *not* the `.txt`).
3. Load the **solution** file: `solution.xml` produced by this project.

The viewer follows the references inside `solution.xml`:

```xml
<Roster xsi:noNamespaceSchemaLocation="Roster.xsd">
    <SchedulingPeriodFile>Instance2.ros</SchedulingPeriodFile>
    <Penalty>833</Penalty>
    <Employee ID="A">
        <Assign><Day>0</Day><Shift>E</Shift></Assign>
        ...
    </Employee>
    ...
</Roster>
```

`<Day>` is the 0-based day index from the instance horizon; `<Shift>` is the
shift ID from `SECTION_SHIFTS`. Keep `solution.xml` and `Instance2.ros` in
the same folder (or pass `--ros-xml ../path/Instance2.ros`) so the
`<SchedulingPeriodFile>` reference resolves correctly when you upload.

## Model summary

**Decision variables** (binary unless noted)

- `x[n, d, s]` — nurse `n` works shift `s` on day `d`.
- `y[n, d]`    — nurse `n` works any shift on day `d` (= Σ_s x[n, d, s]).
- `w[n, wk]`   — nurse `n` works on weekend `wk` (Sat or Sun).
- `under[d, s]`, `over[d, s]` — non-negative integer coverage slacks.

**Hard constraints**

| ID | Constraint |
|---|---|
| H1 | At most one shift per nurse per day. |
| H2 | Forbidden shift sequences from `SECTION_SHIFTS` (e.g. `L → E` is banned). |
| H3 | `MaxShifts` per shift type per nurse. |
| H4 | `MinTotalMinutes ≤ Σ length·x ≤ MaxTotalMinutes`. |
| H5 | Max consecutive working days. |
| H6 | Min consecutive working days (enforced at horizon boundaries). |
| H7 | Min consecutive days off (relaxed at horizon boundaries — an off-block touching day 0 or day H−1 may extend outside the horizon). |
| H8 | Max weekends worked. |
| H9 | `SECTION_DAYS_OFF` — forced days off. |

**Soft objective (minimised)**

- `w_under · under[d,s] + w_over · over[d,s]` from `SECTION_COVER`.
- Unmet `SHIFT_ON_REQUESTS` weights.
- Violated `SHIFT_OFF_REQUESTS` weights.

## Reproducing the result on `Instance2.txt`

```text
Status:      Optimal
Objective:   833
Coverage:    800
Shift-on:    29
Shift-off:   4
Validator:   Feasible, objectives match.
```

## Note on the `.ros` vs `.txt` files

The `.ros` file is the XML form of the instance; `Instance2.txt` is the
section-text form. `problem_reader.py` parses the section-text form, so
**run the solver on `Instance2.txt`**. The `.ros` file is still used at
output time to read `<StartDate>` for the Solution XML.
