#!/usr/bin/env python3
"""Render moqtest conformance reports as markdown.

Input: the report files moxygen's conformance_test.sh writes, named
<draft>-<transport>.txt. Output: one row per run in the shape the MoQ
interop reports use — a box per section, then passed/total — followed by
the per-case detail, failures first.

A section the suite skipped has no cases and is neither passed nor
failed: it shows as a blank box and stays out of the totals, which is
why runs that skip fetch report a smaller denominator.
"""
import pathlib
import re
import sys

SECTION_RE = re.compile(r"^[✓◐] (\d+): (\d+)/(\d+)")
CASE_RE = re.compile(r"^([✓✗]) (PASS|FAIL): (.+)$")
REASON_RE = re.compile(r"^\s+Reason: (.+)$")
MAX_SECTION = 10


def parse(path):
    sections, cases, reason_for = {}, [], {}
    last_fail = None
    for line in path.read_text(errors="replace").splitlines():
        m = SECTION_RE.match(line)
        if m:
            sections[int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
            continue
        m = CASE_RE.match(line)
        if m:
            status, name = m.group(2), m.group(3)
            cases.append((status, name))
            last_fail = name if status == "FAIL" else None
            continue
        m = REASON_RE.match(line)
        if m and last_fail:
            reason_for[last_fail] = m.group(1)
    return sections, cases, reason_for


def row(sections):
    """Boxes per section: all passed, some passed, none passed, skipped."""
    boxes = []
    for n in range(1, MAX_SECTION + 1):
        passed, total = sections.get(n, (0, 0))
        if total == 0:
            boxes.append("⬜")
        elif passed == total:
            boxes.append("✅")
        elif passed:
            boxes.append("⚠️")
        else:
            boxes.append("❌")
    passed = sum(p for p, _ in sections.values())
    total = sum(t for _, t in sections.values())
    return " ".join(boxes), passed, total


def main(paths):
    out = ["## moq-test conformance score", "",
           "Sections 1–10: ✅ all passed, ⚠️ some passed, ❌ none passed, "
           "⬜ skipped by the suite (out of the totals).", "",
           "| run | 1 2 3 4 5 6 7 8 9 10 | score |",
           "|---|---|---|"]
    details = []
    for path in paths:
        sections, cases, reason_for = parse(path)
        boxes, passed, total = row(sections)
        out.append(f"| {path.stem} | {boxes} | {passed}/{total} |")
        fails = [n for s, n in cases if s == "FAIL"]
        body = [f"<details><summary>{path.stem} — "
                f"{len(fails)} failing of {len(cases)}</summary>", ""]
        for name in fails:
            reason = reason_for.get(name)
            body.append(f"- ❌ {name}" + (f" — {reason}" if reason else ""))
        for status, name in cases:
            if status == "PASS":
                body.append(f"- ✅ {name}")
        body += ["", "</details>", ""]
        details += body
    print("\n".join(out + [""] + details))


if __name__ == "__main__":
    files = sorted(pathlib.Path(p) for p in sys.argv[1:])
    if not files:
        sys.exit("usage: conformance_report.py REPORT...")
    main(files)
