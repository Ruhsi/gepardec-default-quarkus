#!/usr/bin/env python3
import argparse
import json
import pathlib
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass


@dataclass
class ApiClassDiff:
    class_name: str
    added_signatures: list[str]
    removed_signatures: list[str]


@dataclass
class Step2Result:
    dependency: str
    old_version: str
    new_version: str
    old_jar: str
    new_jar: str
    added_classes: list[str]
    removed_classes: list[str]
    modified_classes: list[ApiClassDiff]
    added_tree_lines: list[str]
    removed_tree_lines: list[str]


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return completed.stdout


def _find_version_in_pom(pom_file: pathlib.Path, dependency: str) -> str:
    group_id, artifact_id = dependency.split(":", 1)
    tree = ET.parse(pom_file)
    root = tree.getroot()
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}

    for dep in root.findall(".//m:dependencies/m:dependency", ns):
        gid = dep.findtext("m:groupId", default="", namespaces=ns).strip()
        aid = dep.findtext("m:artifactId", default="", namespaces=ns).strip()
        if gid == group_id and aid == artifact_id:
            return dep.findtext("m:version", default="", namespaces=ns).strip()

    return ""


def _resolve_jar(dependency: str, version: str) -> pathlib.Path:
    group_id, artifact_id = dependency.split(":", 1)
    artifact = f"{group_id}:{artifact_id}:{version}"

    _run(
        [
            "mvn",
            "-q",
            "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:get",
            f"-Dartifact={artifact}",
            "-Dtransitive=true",
        ]
    )

    m2 = pathlib.Path.home() / ".m2" / "repository"
    jar_path = m2 / group_id.replace(".", "/") / artifact_id / version / f"{artifact_id}-{version}.jar"
    if not jar_path.exists():
        raise FileNotFoundError(f"Could not resolve jar: {jar_path}")
    return jar_path


def _class_entries(jar_path: pathlib.Path) -> list[str]:
    classes: list[str] = []
    with zipfile.ZipFile(jar_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".class"):
                continue
            if name.startswith("META-INF/"):
                continue
            # Keep inner classes in the analysis because they may be referenced.
            classes.append(name[:-6].replace("/", "."))
    return sorted(set(classes))


def _extract_signatures(jar_path: pathlib.Path, class_name: str) -> set[str]:
    output = _run(["javap", "-classpath", str(jar_path), "-public", class_name])
    signatures: set[str] = set()

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Compiled from"):
            continue
        if line in {"{", "}"}:
            continue
        # Normalize whitespace to keep diffs stable.
        line = re.sub(r"\s+", " ", line)
        signatures.add(line)

    return signatures


def _api_diff(old_jar: pathlib.Path, new_jar: pathlib.Path) -> tuple[list[str], list[str], list[ApiClassDiff]]:
    old_classes = set(_class_entries(old_jar))
    new_classes = set(_class_entries(new_jar))

    added_classes = sorted(new_classes - old_classes)
    removed_classes = sorted(old_classes - new_classes)

    modified: list[ApiClassDiff] = []
    for class_name in sorted(old_classes & new_classes):
        old_signatures = _extract_signatures(old_jar, class_name)
        new_signatures = _extract_signatures(new_jar, class_name)

        added_signatures = sorted(new_signatures - old_signatures)
        removed_signatures = sorted(old_signatures - new_signatures)

        if added_signatures or removed_signatures:
            modified.append(
                ApiClassDiff(
                    class_name=class_name,
                    added_signatures=added_signatures,
                    removed_signatures=removed_signatures,
                )
            )

    return added_classes, removed_classes, modified


def _write_temp_pom(pom_path: pathlib.Path, dependency: str, version: str) -> None:
    group_id, artifact_id = dependency.split(":", 1)
    pom_path.write_text(
        "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<project xmlns="http://maven.apache.org/POM/4.0.0"',
                '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
                '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">',
                "  <modelVersion>4.0.0</modelVersion>",
                "  <groupId>org.renovate.ai</groupId>",
                "  <artifactId>step2-analysis</artifactId>",
                "  <version>1.0.0-SNAPSHOT</version>",
                "  <dependencies>",
                "    <dependency>",
                f"      <groupId>{group_id}</groupId>",
                f"      <artifactId>{artifact_id}</artifactId>",
                f"      <version>{version}</version>",
                "      <scope>test</scope>",
                "    </dependency>",
                "  </dependencies>",
                "</project>",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _dependency_tree_lines(pom_path: pathlib.Path) -> list[str]:
    output_file = pom_path.with_suffix(".tree.txt")
    _run(
        [
            "mvn",
            "-q",
            "-f",
            str(pom_path),
            "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:tree",
            "-Dscope=test",
            "-DoutputType=text",
            f"-DoutputFile={output_file}",
        ]
    )

    lines = output_file.read_text(encoding="utf-8").splitlines()
    normalized = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("org.renovate.ai:step2-analysis"):
            continue
        normalized.append(stripped)
    return sorted(set(normalized))


def _as_markdown(result: Step2Result) -> str:
    lines: list[str] = [
        "# Renovate AI Step 2 - Dependency Change Analysis",
        "",
        f"- dependency: `{result.dependency}`",
        f"- old version: `{result.old_version}`",
        f"- new version: `{result.new_version}`",
        f"- added classes: `{len(result.added_classes)}`",
        f"- removed classes: `{len(result.removed_classes)}`",
        f"- modified classes: `{len(result.modified_classes)}`",
        f"- added transitive tree lines: `{len(result.added_tree_lines)}`",
        f"- removed transitive tree lines: `{len(result.removed_tree_lines)}`",
        "",
        "## Added classes",
    ]

    lines.extend([f"- `{name}`" for name in result.added_classes] or ["- none"])

    lines.append("")
    lines.append("## Removed classes")
    lines.extend([f"- `{name}`" for name in result.removed_classes] or ["- none"])

    lines.append("")
    lines.append("## Modified classes")
    if result.modified_classes:
        for cls in result.modified_classes:
            lines.append(f"- `{cls.class_name}`")
            for sig in cls.added_signatures[:10]:
                lines.append(f"  - + `{sig}`")
            for sig in cls.removed_signatures[:10]:
                lines.append(f"  - - `{sig}`")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("## Dependency tree delta")
    lines.append("### Added")
    lines.extend([f"- `{line}`" for line in result.added_tree_lines] or ["- none"])
    lines.append("### Removed")
    lines.extend([f"- `{line}`" for line in result.removed_tree_lines] or ["- none"])

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze dependency changes between two versions.")
    parser.add_argument("--dependency", required=True, help="Dependency in groupId:artifactId format")
    parser.add_argument("--old-version", default="", help="Old version")
    parser.add_argument("--new-version", default="", help="New version")
    parser.add_argument("--base-pom", default="", help="Base pom.xml path used to discover old version")
    parser.add_argument("--head-pom", default="pom.xml", help="Head pom.xml path used to discover new version")
    parser.add_argument("--json-out", default="step2-result.json", help="JSON output path")
    parser.add_argument("--md-out", default="step2-result.md", help="Markdown output path")
    args = parser.parse_args()

    old_version = args.old_version.strip()
    new_version = args.new_version.strip()

    if not old_version and args.base_pom:
        old_version = _find_version_in_pom(pathlib.Path(args.base_pom), args.dependency)
    if not new_version and args.head_pom:
        new_version = _find_version_in_pom(pathlib.Path(args.head_pom), args.dependency)

    if not old_version or not new_version:
        raise ValueError("Both old and new versions are required. Provide versions directly or via pom files.")

    old_jar = _resolve_jar(args.dependency, old_version)
    new_jar = _resolve_jar(args.dependency, new_version)

    added_classes, removed_classes, modified_classes = _api_diff(old_jar, new_jar)

    with tempfile.TemporaryDirectory(prefix="renovate-step2-") as tmp_dir:
        tmp = pathlib.Path(tmp_dir)
        old_pom = tmp / "old-pom.xml"
        new_pom = tmp / "new-pom.xml"
        _write_temp_pom(old_pom, args.dependency, old_version)
        _write_temp_pom(new_pom, args.dependency, new_version)

        old_tree = _dependency_tree_lines(old_pom)
        new_tree = _dependency_tree_lines(new_pom)

    added_tree_lines = sorted(set(new_tree) - set(old_tree))
    removed_tree_lines = sorted(set(old_tree) - set(new_tree))

    result = Step2Result(
        dependency=args.dependency,
        old_version=old_version,
        new_version=new_version,
        old_jar=str(old_jar),
        new_jar=str(new_jar),
        added_classes=added_classes,
        removed_classes=removed_classes,
        modified_classes=modified_classes,
        added_tree_lines=added_tree_lines,
        removed_tree_lines=removed_tree_lines,
    )

    json_path = pathlib.Path(args.json_out)
    md_path = pathlib.Path(args.md_out)
    json_path.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_as_markdown(result), encoding="utf-8")

    print(f"dependency={result.dependency}")
    print(f"oldVersion={result.old_version}")
    print(f"newVersion={result.new_version}")
    print(f"modifiedClasses={len(result.modified_classes)}")
    print(f"addedClasses={len(result.added_classes)}")
    print(f"removedClasses={len(result.removed_classes)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

