#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import sys
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

        if transformation == "CHANGE_TYPE":
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
        fail("Cannot generate a complete OpenRewrite recipe while MANUAL_REVIEW decisions exist: " + ", ".join(sorted(manual_review_ids)))

    normalized.sort(key=lambda x: (x["oldType"], x["newType"]))
    return normalized


def render_recipe(recipe_name: str, display_name: str, changes):
    lines = [
        "type: specs.openrewrite.org/v1beta/recipe",
        f"name: {recipe_name}",
        f"displayName: {yaml_scalar(display_name)}",
        "description: \"Generated deterministically from a validated AI migration plan.\"",
        "recipeList:",
    ]

    if not changes:
        lines.append("  []")
    else:
        for change in changes:
            if change["transformation"] == "CHANGE_TYPE":
                lines.extend([
                    "  - org.openrewrite.java.ChangeType:",
                    f"      oldFullyQualifiedTypeName: {yaml_scalar(change['oldType'])}",
                    f"      newFullyQualifiedTypeName: {yaml_scalar(change['newType'])}",
                ])

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
    recipe = render_recipe(args.recipe_name, args.display_name, changes)

    recipe_path.parent.mkdir(parents=True, exist_ok=True)
    recipe_path.write_text(recipe, encoding="utf-8")

    plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    recipe_sha = hashlib.sha256(recipe.encode("utf-8")).hexdigest()

    manifest = {
        "schemaVersion": 1,
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
            "generatedRecipeStepCount": len(changes),
            "changeTypeCount": sum(1 for c in changes if c["transformation"] == "CHANGE_TYPE"),
        },
        "changes": changes,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(manifest["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
