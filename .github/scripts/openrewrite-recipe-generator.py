#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

ALLOWED_TRANSFORMATIONS = {"CHANGE_TYPE"}
FQCN_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+$")


def fail(msg: str) -> None:
    raise SystemExit(msg)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"Failed to read JSON {path}: {exc}")


def yaml_scalar(value: str) -> str:
    # JSON string syntax is valid YAML and deterministic.
    return json.dumps(value, ensure_ascii=False)


def normalize_decisions(plan):
    decisions = plan.get("decisions")
    if not isinstance(decisions, list):
        fail("ai-migration-plan.json must contain a decisions array")

    normalized = []
    seen_old = set()
    manual_review_ids = []

    for index, item in enumerate(decisions):
        if not isinstance(item, dict):
            fail(f"decisions[{index}] must be an object")

        decision = item.get("decision")
        if decision == "MANUAL_REVIEW":
            manual_review_ids.append(str(item.get("impactId", index)))
            continue
        if decision == "NO_SOURCE_CHANGE":
            continue
        if decision != "REPLACE":
            fail(f"decisions[{index}] has unsupported decision {decision!r}")

        transformation = item.get("transformation")
        if transformation not in ALLOWED_TRANSFORMATIONS:
            fail(
                f"decisions[{index}] has unsupported transformation {transformation!r}; "
                f"allowed: {sorted(ALLOWED_TRANSFORMATIONS)}"
            )

        old_type = item.get("sourceSymbol") or item.get("oldType") or item.get("sourceTarget")
        new_type = item.get("replacementSymbol") or item.get("newType")

        # Support the validated planner shape where target/replacement are nested.
        if old_type is None and isinstance(item.get("target"), dict):
            old_type = item["target"].get("symbol")
        if new_type is None and isinstance(item.get("replacement"), dict):
            new_type = item["replacement"].get("symbol")

        if not isinstance(old_type, str) or not FQCN_RE.match(old_type):
            fail(f"decisions[{index}] has invalid source type: {old_type!r}")
        if not isinstance(new_type, str) or not FQCN_RE.match(new_type):
            fail(f"decisions[{index}] has invalid replacement type: {new_type!r}")
        if old_type == new_type:
            fail(f"decisions[{index}] maps a type to itself: {old_type}")
        if old_type in seen_old:
            fail(f"duplicate CHANGE_TYPE source type: {old_type}")
        seen_old.add(old_type)

        normalized.append({
            "transformation": "CHANGE_TYPE",
            "oldType": old_type,
            "newType": new_type,
        })

    if manual_review_ids:
        fail(
            "Cannot generate a complete OpenRewrite recipe while MANUAL_REVIEW decisions exist: "
            + ", ".join(sorted(manual_review_ids))
        )

    normalized.sort(key=lambda x: (x["oldType"], x["newType"]))
    return normalized


def package_candidates(change):
    """Return package rename candidates that preserve the complete type suffix.

    Example:
      javax.ws.rs.core.MediaType -> jakarta.ws.rs.core.MediaType
    contains candidate:
      javax.ws.rs -> jakarta.ws.rs   (suffix core.MediaType is unchanged)
    """
    old_parts = change["oldType"].split(".")
    new_parts = change["newType"].split(".")
    candidates = set()

    # Leave at least one segment as the type suffix.
    for old_len in range(1, len(old_parts)):
        old_suffix = old_parts[old_len:]
        for new_len in range(1, len(new_parts)):
            if old_suffix != new_parts[new_len:]:
                continue
            old_pkg = ".".join(old_parts[:old_len])
            new_pkg = ".".join(new_parts[:new_len])
            if old_pkg == new_pkg:
                continue
            candidates.add((old_pkg, new_pkg))

    return candidates


def optimize_changes(changes):
    """Collapse proven repeated type mappings into recursive ChangePackage steps.

    Safety rules:
    - at least two mappings must prove the same package rename;
    - the suffix below the package boundary must be identical for every mapping;
    - each input mapping is consumed by at most one package step;
    - ungrouped mappings remain exact ChangeType operations.
    """
    candidate_to_indexes = defaultdict(set)
    for index, change in enumerate(changes):
        for candidate in package_candidates(change):
            candidate_to_indexes[candidate].add(index)

    remaining = set(range(len(changes)))
    package_steps = []

    while True:
        viable = []
        for (old_pkg, new_pkg), indexes in candidate_to_indexes.items():
            covered = sorted(indexes & remaining)
            if len(covered) < 2:
                continue
            specificity = old_pkg.count(".") + new_pkg.count(".") + 2
            viable.append((len(covered), specificity, old_pkg, new_pkg, covered))

        if not viable:
            break

        # Prefer maximum coverage first, then the most specific package boundary.
        viable.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
        _, _, old_pkg, new_pkg, covered = viable[0]

        package_steps.append({
            "transformation": "CHANGE_PACKAGE",
            "oldPackage": old_pkg,
            "newPackage": new_pkg,
            "recursive": True,
            "derivedFrom": [changes[i] for i in covered],
        })
        remaining.difference_update(covered)

    type_steps = [changes[i] for i in sorted(remaining)]

    # Stable output: package steps first, then exact type changes.
    package_steps.sort(key=lambda x: (x["oldPackage"], x["newPackage"]))
    type_steps.sort(key=lambda x: (x["oldType"], x["newType"]))
    return package_steps + type_steps


def render_recipe(recipe_name: str, display_name: str, steps):
    lines = [
        "type: specs.openrewrite.org/v1beta/recipe",
        f"name: {recipe_name}",
        f"displayName: {yaml_scalar(display_name)}",
        'description: "Generated deterministically from a validated AI migration plan."',
        "recipeList:",
    ]

    if not steps:
        lines.append("  []")
    else:
        for step in steps:
            if step["transformation"] == "CHANGE_PACKAGE":
                lines.extend([
                    "  - org.openrewrite.java.ChangePackage:",
                    f"      oldPackageName: {yaml_scalar(step['oldPackage'])}",
                    f"      newPackageName: {yaml_scalar(step['newPackage'])}",
                    "      recursive: true",
                ])
            elif step["transformation"] == "CHANGE_TYPE":
                lines.extend([
                    "  - org.openrewrite.java.ChangeType:",
                    f"      oldFullyQualifiedTypeName: {yaml_scalar(step['oldType'])}",
                    f"      newFullyQualifiedTypeName: {yaml_scalar(step['newType'])}",
                ])
            else:
                fail(f"Internal error: unsupported rendered transformation {step['transformation']!r}")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--recipe-name", default="com.gepardec.renovate.GeneratedDependencyMigration")
    parser.add_argument("--display-name", default="Generated dependency migration")
    args = parser.parse_args()

    plan_path = Path(args.plan)
    recipe_path = Path(args.recipe)
    manifest_path = Path(args.manifest)

    plan = load_json(plan_path)
    changes = normalize_decisions(plan)
    recipe_steps = optimize_changes(changes)
    recipe = render_recipe(args.recipe_name, args.display_name, recipe_steps)

    recipe_path.parent.mkdir(parents=True, exist_ok=True)
    recipe_path.write_text(recipe, encoding="utf-8")

    plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    recipe_sha = hashlib.sha256(recipe.encode("utf-8")).hexdigest()

    manifest = {
        "schemaVersion": 2,
        "generator": "openrewrite-recipe-generator.py",
        "input": {
            "path": plan_path.name,
            "sha256": plan_sha,
        },
        "output": {
            "path": recipe_path.name,
            "sha256": recipe_sha,
            "recipeName": args.recipe_name,
        },
        "summary": {
            "inputChangeTypeCount": len(changes),
            "generatedRecipeStepCount": len(recipe_steps),
            "changePackageCount": sum(1 for s in recipe_steps if s["transformation"] == "CHANGE_PACKAGE"),
            "changeTypeCount": sum(1 for s in recipe_steps if s["transformation"] == "CHANGE_TYPE"),
        },
        "inputChanges": changes,
        "recipeSteps": recipe_steps,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(manifest["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
