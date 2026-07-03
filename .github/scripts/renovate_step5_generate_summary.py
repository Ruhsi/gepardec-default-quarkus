#!/usr/bin/env python3
import argparse
import json
import pathlib
from dataclasses import asdict, dataclass


@dataclass
class FileChangeSummary:
    file: str
    added_lines: int
    removed_lines: int


@dataclass
class Step5Summary:
    dependency: str
    old_version: str
    new_version: str
    why: str
    impacted_files: list[str]
    changed_files: list[FileChangeSummary]
    total_added_lines: int
    total_removed_lines: int
    recommendations: list[str]


def _parse_diff(diff_text: str) -> tuple[list[FileChangeSummary], int, int]:
    changed: list[FileChangeSummary] = []
    current_file = ""
    added = 0
    removed = 0
    total_added = 0
    total_removed = 0

    def flush() -> None:
        nonlocal current_file, added, removed
        if current_file:
            changed.append(FileChangeSummary(file=current_file, added_lines=added, removed_lines=removed))
        current_file = ""
        added = 0
        removed = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            parts = line.split(" ")
            if len(parts) >= 4:
                # Keep repository relative target path.
                current_file = parts[3].removeprefix("b/")
            continue

        if line.startswith("+++") or line.startswith("---"):
            continue

        if line.startswith("+"):
            added += 1
            total_added += 1
        elif line.startswith("-"):
            removed += 1
            total_removed += 1

    flush()
    return changed, total_added, total_removed


def _as_markdown(summary: Step5Summary) -> str:
    lines = [
        "# Renovate AI Step 5 - Migration Summary",
        "",
        "## Why this change",
        f"- Dependency update: `{summary.dependency}` from `{summary.old_version}` to `{summary.new_version}`",
        f"- Reason: {summary.why}",
        "",
        "## Where it affects the application",
    ]

    lines.extend([f"- `{item}`" for item in summary.impacted_files] or ["- none detected"])

    lines.extend(
        [
            "",
            "## What changed",
            f"- Changed files: `{len(summary.changed_files)}`",
            f"- Added lines: `{summary.total_added_lines}`",
            f"- Removed lines: `{summary.total_removed_lines}`",
        ]
    )

    if summary.changed_files:
        lines.append("")
        lines.append("### File breakdown")
        for item in summary.changed_files:
            lines.append(f"- `{item.file}` (+{item.added_lines}/-{item.removed_lines})")

    lines.append("")
    lines.append("## Recommendations")
    lines.extend([f"- {item}" for item in summary.recommendations])

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Step 5 summary from previous pipeline outputs.")
    parser.add_argument("--step2-json", default="step2-result.json", help="Path to Step 2 JSON")
    parser.add_argument("--step3-json", default="step3-result.json", help="Path to Step 3 JSON")
    parser.add_argument("--diff-file", default="step4-changes.diff", help="Path to git diff file")
    parser.add_argument("--json-out", default="step5-summary.json", help="Path to summary JSON")
    parser.add_argument("--md-out", default="step5-summary.md", help="Path to summary markdown")
    args = parser.parse_args()

    step2 = json.loads(pathlib.Path(args.step2_json).read_text(encoding="utf-8"))
    step3 = json.loads(pathlib.Path(args.step3_json).read_text(encoding="utf-8"))
    diff_text = pathlib.Path(args.diff_file).read_text(encoding="utf-8") if pathlib.Path(args.diff_file).exists() else ""

    changed_files, total_added, total_removed = _parse_diff(diff_text)

    dependency = step2.get("dependency", "")
    old_version = step2.get("old_version", "")
    new_version = step2.get("new_version", "")

    why = step3.get("summary", "Migration summary generated from impact analysis.")
    impacted_files = step3.get("impacted_files", [])

    recommendations = [
        "Run the full test suite before merge.",
        "Check the PR diff for semantic changes beyond imports and annotations.",
        "If CI is green, this migration is a good candidate for auto-merge.",
    ]

    summary = Step5Summary(
        dependency=dependency,
        old_version=old_version,
        new_version=new_version,
        why=why,
        impacted_files=impacted_files,
        changed_files=changed_files,
        total_added_lines=total_added,
        total_removed_lines=total_removed,
        recommendations=recommendations,
    )

    pathlib.Path(args.json_out).write_text(json.dumps(asdict(summary), indent=2) + "\n", encoding="utf-8")
    pathlib.Path(args.md_out).write_text(_as_markdown(summary), encoding="utf-8")

    print(f"dependency={summary.dependency}")
    print(f"oldVersion={summary.old_version}")
    print(f"newVersion={summary.new_version}")
    print(f"summaryFiles={len(summary.changed_files)}")
    print(f"summaryImpacted={len(summary.impacted_files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

