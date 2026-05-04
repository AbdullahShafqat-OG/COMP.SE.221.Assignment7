# -*- coding: utf-8 -*-
"""
CLI runner for the Nurse Rostering MIP.

Usage:
    python main.py Instance2.txt
    python main.py Instance2.txt --time-limit 60 --gap 0.005
"""

from __future__ import annotations
import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from problem_reader import parse_instance_from_file, Instance
from model import build_and_solve


def _resolve_ros_relpath(args, instance_path: Path, xml_out_path: Path) -> str:
    """Pick the path that goes inside <SchedulingPeriodFile>.

    Default: a relative path from the XML output directory to the sibling
    `<instance>.ros` file. CLI override via --ros-xml.
    """
    if args.ros_xml:
        ros_path = Path(args.ros_xml)
    else:
        ros_path = instance_path.with_suffix(".ros")
    try:
        rel = Path(__import__("os").path.relpath(ros_path, xml_out_path.parent))
        return rel.as_posix()
    except ValueError:
        return ros_path.as_posix()


def write_solution_json(result: dict, inst: Instance, path: str) -> None:
    nurses = list(inst.staff.keys())
    roster = []
    for n in nurses:
        for d in range(inst.horizon_days):
            s = result["assignments"].get((n, d))
            if s is not None:
                roster.append({"nurse": n, "day": d, "shift": s})
    data = {
        "status": result["status"],
        "objective": result["objective"],
        "horizon_days": inst.horizon_days,
        "nurses": nurses,
        "shifts": list(inst.shifts.keys()),
        "assignments": roster,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def write_solution_xml(
    result: dict,
    inst: Instance,
    path: str,
    scheduling_period_file: str,
) -> None:
    """Emit a RosterViewer-compatible Roster XML (Roster.xsd).

    Format (as accepted by https://www.staffrostersolutions.com/rvw/1.4.1/):

        <Roster xmlns:xsi="..." xsi:noNamespaceSchemaLocation="Roster.xsd">
            <SchedulingPeriodFile>../Instance2.ros</SchedulingPeriodFile>
            <Penalty>833</Penalty>

            <Employee ID="A">
                <Assign><Day>0</Day><Shift>E</Shift></Assign>
                <Assign><Day>1</Day><Shift>E</Shift></Assign>
                ...
            </Employee>
            ...
        </Roster>

    Day is the 0-based day index from the instance horizon (NOT a calendar
    date).  Shift is the shift ID from SECTION_SHIFTS.
    """
    root = ET.Element(
        "Roster",
        attrib={
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "Roster.xsd",
        },
    )
    ET.SubElement(root, "SchedulingPeriodFile").text = scheduling_period_file

    obj_val = result.get("objective")
    ET.SubElement(root, "Penalty").text = (
        str(int(round(obj_val))) if obj_val is not None else "0"
    )

    nurses = list(inst.staff.keys())
    for n in nurses:
        # Skip employees with no assignments rather than emitting an empty block.
        days_worked = [
            d for d in range(inst.horizon_days)
            if result["assignments"].get((n, d)) is not None
        ]
        if not days_worked:
            continue
        emp_el = ET.SubElement(root, "Employee", attrib={"ID": n})
        for d in days_worked:
            s = result["assignments"][(n, d)]
            assign_el = ET.SubElement(emp_el, "Assign")
            ET.SubElement(assign_el, "Day").text = str(d)
            ET.SubElement(assign_el, "Shift").text = s

    # Pretty-print
    rough = ET.tostring(root, encoding="utf-8")
    pretty = minidom.parseString(rough).toprettyxml(indent="    ", encoding="UTF-8")
    Path(path).write_bytes(pretty)


def render_markdown(result: dict, inst: Instance) -> str:
    nurses = list(inst.staff.keys())
    days = list(range(inst.horizon_days))
    shifts = list(inst.shifts.keys())

    lines = []
    lines.append("# Nurse Roster Solution")
    lines.append("")
    lines.append(f"- **Status:** {result['status']}")
    obj = result["objective"]
    lines.append(f"- **Objective value:** {obj:.2f}" if obj is not None else "- **Objective:** N/A")
    lines.append(
        f"- **Horizon:** {inst.horizon_days} days  |  "
        f"**Nurses:** {len(nurses)}  |  "
        f"**Shift types:** {', '.join(shifts)}"
    )
    lines.append(
        f"- **Model size:** {result['num_vars']} variables, "
        f"{result['num_constraints']} constraints"
    )
    lines.append("")
    lines.append("## Roster")
    lines.append("")
    lines.append("Rows = nurses, columns = days. Cell = shift code, `-` = day off.")
    lines.append("")

    header = "| Nurse | " + " | ".join(f"D{d:02d}" for d in days) + " |"
    sep = "|---|" + "|".join("---" for _ in days) + "|"
    lines.append(header)
    lines.append(sep)
    for n in nurses:
        row = [n]
        for d in days:
            s = result["assignments"].get((n, d))
            row.append(s if s is not None else "-")
        lines.append("| " + " | ".join(row) + " |")

    # Per-nurse summary
    lines.append("")
    lines.append("## Per-nurse summary")
    lines.append("")
    lines.append(
        "| Nurse | Shifts worked | Total mins | MinReq | MaxReq | Weekends | MaxWE |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for n in nurses:
        sd = inst.staff[n]
        worked = [
            result["assignments"][(n, d)]
            for d in days
            if (n, d) in result["assignments"]
        ]
        total_min = sum(inst.shifts[s].length for s in worked)
        wk_count = 0
        for wk in range(inst.horizon_days // 7):
            sat, sun = 7 * wk + 5, 7 * wk + 6
            in_sat = sat < inst.horizon_days and (n, sat) in result["assignments"]
            in_sun = sun < inst.horizon_days and (n, sun) in result["assignments"]
            if in_sat or in_sun:
                wk_count += 1
        lines.append(
            f"| {n} | {len(worked)} | {total_min} | {sd.min_total_minutes} | "
            f"{sd.max_total_minutes} | {wk_count} | {sd.max_weekends} |"
        )

    # Coverage table
    lines.append("")
    lines.append("## Coverage (assigned vs required)")
    lines.append("")
    lines.append("| Day | Shift | Required | Assigned | Under | Over |")
    lines.append("|---|---|---|---|---|---|")
    for (d, s) in sorted(inst.cover.keys()):
        cr = inst.cover[(d, s)]
        assigned = sum(
            1 for n in nurses if result["assignments"].get((n, d)) == s
        )
        u = int(round(result["under"].get((d, s), 0)))
        o = int(round(result["over"].get((d, s), 0)))
        lines.append(f"| {d} | {s} | {cr.requirement} | {assigned} | {u} | {o} |")

    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description="Solve a Nurse Rostering instance.")
    p.add_argument("instance", help="Path to .ros instance file")
    p.add_argument("--time-limit", type=int, default=120, help="Solver time limit (seconds)")
    p.add_argument("--gap", type=float, default=0.01, help="Relative MIP gap")
    p.add_argument("--out-json", default="solution.json", help="JSON output path")
    p.add_argument("--out-md", default="solution.md", help="Markdown output path")
    p.add_argument(
        "--out-xml",
        default="solution.xml",
        help="RosterViewer-compatible Roster XML output path",
    )
    p.add_argument(
        "--ros-xml",
        default=None,
        help="Path to the .ros XML file referenced by <SchedulingPeriodFile>. "
        "Defaults to <instance>.ros next to the instance file.",
    )
    p.add_argument("--quiet", action="store_true", help="Silence solver output")
    args = p.parse_args()

    instance_path = Path(args.instance)
    inst = parse_instance_from_file(args.instance)
    print(
        f"Parsed: horizon={inst.horizon_days}d, "
        f"nurses={len(inst.staff)}, shift types={len(inst.shifts)}, "
        f"cover entries={len(inst.cover)}, "
        f"on-requests={len(inst.shift_on_requests)}, "
        f"off-requests={len(inst.shift_off_requests)}"
    )

    result = build_and_solve(
        inst,
        time_limit=args.time_limit,
        mip_gap=args.gap,
        msg=not args.quiet,
    )

    md = render_markdown(result, inst)
    print()
    print(md)

    write_solution_json(result, inst, args.out_json)
    Path(args.out_md).write_text(md, encoding="utf-8")

    xml_out_path = Path(args.out_xml)
    sp_file = _resolve_ros_relpath(args, instance_path, xml_out_path)
    write_solution_xml(
        result,
        inst,
        args.out_xml,
        scheduling_period_file=sp_file,
    )
    print(f"Wrote: {args.out_json}, {args.out_md}, {args.out_xml}")
    print(f"  (SchedulingPeriodFile: {sp_file})")


if __name__ == "__main__":
    main()
