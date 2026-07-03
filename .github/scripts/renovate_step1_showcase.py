#!/usr/bin/env python3
import argparse
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass


@dataclass
class ShowcaseResult:
    candidate: bool
    dependency: str
    version: str
    scope: str
    junit4_test_files: list[str]
    reason: str


def _find_junit_dependency(pom_file: pathlib.Path) -> tuple[str, str, str]:
    tree = ET.parse(pom_file)
    root = tree.getroot()
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}

    for dep in root.findall(".//m:dependencies/m:dependency", ns):
        group_id = dep.findtext("m:groupId", default="", namespaces=ns).strip()
        artifact_id = dep.findtext("m:artifactId", default="", namespaces=ns).strip()
        if group_id == "junit" and artifact_id == "junit":
            version = dep.findtext("m:version", default="", namespaces=ns).strip()
            scope = dep.findtext("m:scope", default="compile", namespaces=ns).strip()
            return group_id + ":" + artifact_id, version, scope

    return "", "", ""


def _find_junit4_tests(test_root: pathlib.Path) -> list[str]:
    junit4_import = re.compile(r"^\s*import\s+org\.junit\.(?!jupiter)", re.MULTILINE)
    files: list[str] = []

    for file_path in sorted(test_root.rglob("*.java")):
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        if junit4_import.search(content):
            files.append(str(file_path))

    return files


def _as_markdown(result: ShowcaseResult) -> str:
    lines = [
        "# Renovate AI Step 1 - Showcase Scan",
        "",
        f"- candidate: `{str(result.candidate).lower()}`",
        f"- dependency: `{result.dependency or 'not found'}`",
        f"- version: `{result.version or 'n/a'}`",
        f"- scope: `{result.scope or 'n/a'}`",
        f"- reason: {result.reason}",
        "",
        "## JUnit4 test files",
    ]

    if result.junit4_test_files:
        lines.extend([f"- `{file}`" for file in result.junit4_test_files])
    else:
        lines.append("- none")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan repository for step 1 showcase dependency.")
    parser.add_argument("--repo-root", default=".", help="Repository root")
    parser.add_argument("--json-out", default="showcase-result.json", help="Path for JSON output")
    parser.add_argument("--md-out", default="showcase-result.md", help="Path for Markdown output")
    args = parser.parse_args()

    repo_root = pathlib.Path(args.repo_root).resolve()
    pom_file = repo_root / "pom.xml"
    test_root = repo_root / "src" / "test" / "java"

    if not pom_file.exists():
        print(f"Missing pom.xml in {repo_root}", file=sys.stderr)
        return 2

    dependency, version, scope = _find_junit_dependency(pom_file)
    junit4_test_files = _find_junit4_tests(test_root) if test_root.exists() else []

    candidate = dependency == "junit:junit" and scope == "test" and bool(junit4_test_files)

    if not dependency:
        reason = "Dependency junit:junit was not found in pom.xml"
    elif scope != "test":
        reason = "Dependency exists but scope is not test"
    elif not junit4_test_files:
        reason = "Dependency exists but no JUnit4 test imports were found"
    else:
        reason = "Good pilot candidate: test-only dependency with direct JUnit4 API usage"

    result = ShowcaseResult(
        candidate=candidate,
        dependency=dependency,
        version=version,
        scope=scope,
        junit4_test_files=[str(pathlib.Path(f).relative_to(repo_root)) for f in junit4_test_files],
        reason=reason,
    )

    json_out = pathlib.Path(args.json_out)
    md_out = pathlib.Path(args.md_out)
    json_out.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    md_out.write_text(_as_markdown(result), encoding="utf-8")

    # GitHub Actions-friendly key/value output.
    print(f"candidate={str(result.candidate).lower()}")
    print(f"dependency={result.dependency}")
    print(f"version={result.version}")
    print(f"scope={result.scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

