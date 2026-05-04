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
| `validator.py`      | Standalone (stdlib-only) feasibility checker and objective recomputer. |
| `Instance2.txt`     | Section-format instance (parsed by `problem_reader.py`). |
| `Instance2.ros`     | XML-format twin of the same instance (used to read `<StartDate>` for the Solution XML). |

Outputs (created on each run):

| File | Content |
|---|---|
| `solution.json` | Machine-readable solution: status, objective, list of `(nurse, day, shift)` assignments. |
| `solution.md`   | Human-readable Markdown roster, per-nurse summary, and coverage table. |
| `solution.xml`  | Staff Roster Solutions / NRP-Competition compatible Solution XML. Drop this into the [RosterViewer](https://www.staffrostersolutions.com/rvw/1.4.1/) along with `Instance2.ros` to visualise. |

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
| `--out-xml PATH`       | `solution.xml`   | Where to write the RosterViewer Solution XML. |
| `--ros-xml PATH`       | `<instance>.ros` | Source for `<StartDate>`; auto-detected when a `.ros` sits next to the `.txt`. |
| `--start-date YYYY-MM-DD` | from `.ros`   | Override the first-day date used in the XML. |
| `--scheduling-period-id ID` | instance basename | Value for `<SchedulingPeriodID>`. |
| `--competitor NAME`    | `PuLP-MIP`       | Value for `<Competitor>`. |
| `--quiet`              | off              | Silence CBC's own log output. |

The Markdown roster is also echoed to stdout.

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

The viewer renders the schedule grid, highlights coverage shortfalls, and
shows the per-nurse statistics that should match `solution.md`.

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
