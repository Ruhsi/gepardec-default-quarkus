#!/usr/bin/env python3
import argparse
import json
import pathlib
import re
from dataclasses import asdict, dataclass


@dataclass
class ChangeOccurrence:
    file: str
    line: int
    kind: str
    current: str
    suggested: str
    effort: str


@dataclass
class Step3Result:
    dependency: str
    old_version: str
    new_version: str
    total_files_scanned: int
    impacted_files: list[str]
    changes: list[ChangeOccurrence]
    summary: str


RULES: list[tuple[str, str, str, str]] = [
    (r"^\s*import\s+org\.junit\.Test\s*;", "import", "import org.junit.Test;", "import org.junit.jupiter.api.Test;"),
    (r"^\s*import\s+org\.junit\.Before\s*;", "import", "import org.junit.Before;", "import org.junit.jupiter.api.BeforeEach;"),
    (r"^\s*import\s+org\.junit\.After\s*;", "import", "import org.junit.After;", "import org.junit.jupiter.api.AfterEach;"),
    (r"^\s*import\s+org\.junit\.Ignore\s*;", "import", "import org.junit.Ignore;", "import org.junit.jupiter.api.Disabled;"),
    (r"^\s*import\s+org\.junit\.Assert\s*;", "import", "import org.junit.Assert;", "import static org.junit.jupiter.api.Assertions.*;"),
    (r"^\s*@Before\b", "annotation", "@Before", "@BeforeEach"),
    (r"^\s*@After\b", "annotation", "@After", "@AfterEach"),
    (r"^\s*@Ignore\b", "annotation", "@Ignore", "@Disabled"),
    (r"\borg\.junit\.Assert\.", "call", "org.junit.Assert.*", "org.junit.jupiter.api.Assertions.*"),
]


MANUAL_HINTS: list[tuple[str, str, str]] = [
    (r"@RunWith\b", "@RunWith(...)", "@ExtendWith(...) and add a Jupiter extension equivalent"),
    (r"\bExpectedException\b", "ExpectedException", "assertThrows(...)"),
    (r"\bTemporaryFolder\b", "TemporaryFolder", "@TempDir Path tempDir"),
]


def _as_markdown(result: Step3Result) -> str:
    lines = [
        "# Renovate AI Step 3 - Application Impact Analysis",
        "",
        f"- dependency: `{result.dependency}`",
        f"- old version: `{result.old_version}`",
        f"- new version: `{result.new_version}`",
        f"- scanned files: `{result.total_files_scanned}`",
        f"- impacted files: `{len(result.impacted_files)}`",
        f"- detected change points: `{len(result.changes)}`",
        f"- summary: {result.summary}",
        "",
        "## Impacted files",
    ]

    lines.extend([f"- `{file}`" for file in result.impacted_files] or ["- none"])

    lines.append("")
    lines.append("## Proposed changes")
    if result.changes:
        for change in result.changes:
            lines.append(f"- `{change.file}:{change.line}` [{change.effort}] `{change.current}` -> `{change.suggested}`")
    else:
        lines.append("- none")

    return "\n".join(lines) + "\n"


def _scan_file(file_path: pathlib.Path, repo_root: pathlib.Path) -> list[ChangeOccurrence]:
    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    occurrences: list[ChangeOccurrence] = []

    for idx, line in enumerate(lines, start=1):
        for pattern, kind, current, suggested in RULES:
            if re.search(pattern, line):
                occurrences.append(
                    ChangeOccurrence(
                        file=str(file_path.relative_to(repo_root)),
                        line=idx,
                        kind=kind,
                        current=current,
                        suggested=suggested,
                        effort="auto",
                    )
                )

        for pattern, current, suggested in MANUAL_HINTS:
            if re.search(pattern, line):
                occurrences.append(
                    ChangeOccurrence(
                        file=str(file_path.relative_to(repo_root)),
                        line=idx,
                        kind="manual",
                        current=current,
                        suggested=suggested,
                        effort="manual",
                    )
                )

    return occurrences


def _scan_repo(repo_root: pathlib.Path) -> tuple[int, list[ChangeOccurrence]]:
    java_roots = [repo_root / "src" / "main" / "java", repo_root / "src" / "test" / "java"]
    java_files: list[pathlib.Path] = []
    for root in java_roots:
        if root.exists():
            java_files.extend(sorted(root.rglob("*.java")))

    changes: list[ChangeOccurrence] = []
    for file_path in java_files:
        changes.extend(_scan_file(file_path, repo_root))

    return len(java_files), changes


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze how a dependency update affects project classes.")
    parser.add_argument("--repo-root", default=".", help="Repository root")
    parser.add_argument("--step2-json", default="step2-result.json", help="Step 2 JSON input path")
    parser.add_argument("--json-out", default="step3-result.json", help="Step 3 JSON output")
    parser.add_argument("--md-out", default="step3-result.md", help="Step 3 Markdown output")
    args = parser.parse_args()

    repo_root = pathlib.Path(args.repo_root).resolve()
    step2_path = pathlib.Path(args.step2_json)

    if not step2_path.is_absolute():
        step2_path = repo_root / step2_path

    step2_data = json.loads(step2_path.read_text(encoding="utf-8"))
    dependency = step2_data.get("dependency", "")
    old_version = step2_data.get("old_version", "")
    new_version = step2_data.get("new_version", "")

    scanned_files, changes = _scan_repo(repo_root)
    impacted_files = sorted({change.file for change in changes})

    if dependency == "junit:junit":
        summary = "JUnit4 usage detected. Tests should be migrated to JUnit5 annotations/imports."
    elif changes:
        summary = "Source-level impact markers detected; manual review is recommended."
    else:
        summary = "No direct source references matched the configured migration rules."

    result = Step3Result(
        dependency=dependency,
        old_version=old_version,
        new_version=new_version,
        total_files_scanned=scanned_files,
        impacted_files=impacted_files,
        changes=changes,
        summary=summary,
    )

    json_out = pathlib.Path(args.json_out)
    md_out = pathlib.Path(args.md_out)
    if not json_out.is_absolute():
        json_out = repo_root / json_out
    if not md_out.is_absolute():
        md_out = repo_root / md_out

    json_out.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    md_out.write_text(_as_markdown(result), encoding="utf-8")

    print(f"dependency={result.dependency}")
    print(f"oldVersion={result.old_version}")
    print(f"newVersion={result.new_version}")
    print(f"impactedFiles={len(result.impacted_files)}")
    print(f"changePoints={len(result.changes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

