#!/usr/bin/env python3
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

SCHEMA_VERSION = 1


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def read_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def stable_id(prefix: str, *parts: str) -> str:
    raw = '\0'.join(str(p) for p in parts).encode('utf-8')
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:20]}"


def package_name(fqcn: str) -> str:
    return fqcn.rsplit('.', 1)[0] if '.' in fqcn else ''


def simple_name(fqcn: str) -> str:
    return fqcn.rsplit('.', 1)[-1].split('$', 1)[0]


def package_suffix_overlap(a: str, b: str) -> int:
    aa = package_name(a).split('.') if package_name(a) else []
    bb = package_name(b).split('.') if package_name(b) else []
    count = 0
    while aa and bb and aa[-1] == bb[-1]:
        count += 1
        aa.pop()
        bb.pop()
    return count


def candidate_score_for_type(old_type: str, candidate_type: str, old_scope: str, new_scope: str) -> int:
    score = 0
    old_simple = simple_name(old_type)
    new_simple = simple_name(candidate_type)
    if old_simple == new_simple:
        score += 100
    else:
        score += int(40 * SequenceMatcher(None, old_simple, new_simple).ratio())
    overlap = package_suffix_overlap(old_type, candidate_type)
    score += min(overlap, 4) * 20
    if old_scope and new_scope and old_scope == new_scope:
        score += 10
    return score


def build_removed_candidates(target, added_dependencies, max_candidates=20):
    old_type = target.get('signature') or target.get('className') or target.get('targetSignature')
    old_scope = target.get('dependencyScopeCategory', '')
    candidates = []
    for dep in added_dependencies:
        for class_name in dep.get('classNames', []):
            # A replacement must actually change the referenced symbol.
            # The same FQCN can appear again through another added dependency,
            # but mapping a type to itself is never a source migration.
            if class_name == old_type:
                continue
            if simple_name(class_name) != simple_name(old_type):
                continue
            score = candidate_score_for_type(old_type, class_name, old_scope, dep.get('scopeCategory', ''))
            candidate_id = stable_id('cand', dep['coordinate'], dep.get('newVersion', ''), class_name)
            candidates.append({
                'candidateId': candidate_id,
                'kind': 'CLASS',
                'coordinate': dep['coordinate'],
                'version': dep.get('newVersion', ''),
                'symbol': class_name,
                'scopeCategory': dep.get('scopeCategory', ''),
                'evidence': {
                    'simpleNameEqual': simple_name(class_name) == simple_name(old_type),
                    'packageSuffixOverlap': package_suffix_overlap(old_type, class_name),
                    'score': score,
                    'source': 'ADDED_DEPENDENCY_TYPE_INDEX'
                }
            })
    candidates.sort(key=lambda c: (-c['evidence']['score'], c['coordinate'], c['symbol']))
    return candidates[:max_candidates]


def api_candidate_score(target, candidate):
    score = 0
    if candidate.get('kind') == target.get('targetKind'):
        score += 30
    if candidate.get('className') == target.get('className'):
        score += 80
    elif package_name(candidate.get('className', '')) == package_name(target.get('className', '')):
        score += 20
    old_name = target.get('targetName') or target.get('name') or ''
    new_name = candidate.get('name') or ''
    if old_name and new_name:
        if old_name == new_name:
            score += 60
        else:
            score += int(30 * SequenceMatcher(None, old_name, new_name).ratio())
    return score


def build_japicmp_candidates(target, api_changes, max_candidates=30):
    candidates = []
    coordinate = target['coordinate']
    target_kind = target.get('targetKind')
    for candidate in api_changes:
        if candidate.get('coordinate') != coordinate:
            continue
        if candidate.get('changeStatus') not in {'NEW', 'MODIFIED'}:
            continue
        if target_kind and candidate.get('kind') != target_kind:
            continue
        score = api_candidate_score(target, candidate)
        if score < 30:
            continue
        candidate_symbol = candidate.get('signature', '')
        target_symbol = target.get('signature') or target.get('targetSignature') or ''
        # A modified/new API entry with the exact same symbol is useful evidence,
        # but it is not a replacement candidate.
        if candidate_symbol and target_symbol and candidate_symbol == target_symbol:
            continue
        candidate_id = stable_id('cand', coordinate, candidate.get('newVersion', ''), candidate_symbol)
        candidates.append({
            'candidateId': candidate_id,
            'kind': candidate.get('kind', ''),
            'coordinate': coordinate,
            'version': candidate.get('newVersion', ''),
            'symbol': candidate_symbol,
            'scopeCategory': candidate.get('scopeCategory', ''),
            'evidence': {
                'score': score,
                'changeStatus': candidate.get('changeStatus', ''),
                'source': 'JAPICMP_NEW_OR_MODIFIED_API'
            }
        })
    candidates.sort(key=lambda c: (-c['evidence']['score'], c['symbol']))
    dedup = []
    seen = set()
    for c in candidates:
        if c['candidateId'] in seen:
            continue
        seen.add(c['candidateId'])
        dedup.append(c)
    return dedup[:max_candidates]


def build(args):
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    metadata = read_json(artifact_dir / 'metadata.json')
    usage_summary = read_json(artifact_dir / 'api-usage-summary.json')
    usages = read_jsonl(artifact_dir / 'api-usages.jsonl')
    added_index = read_json(artifact_dir / 'added-dependency-type-index.json')
    api_changes = read_jsonl(artifact_dir / 'api-changes.jsonl')
    usage_impact = read_json(artifact_dir / 'api-usage-impact.json')

    targets_by_id = {}
    for impact in usage_impact.get('impacts', []):
        target = impact.get('target', {})
        if target.get('targetId'):
            targets_by_id[target['targetId']] = target

    # removed-dependency impacts are kept separately in step 5
    removed_impact_path = artifact_dir / 'removed-dependency-impact.json'
    if removed_impact_path.exists():
        for impact in read_json(removed_impact_path).get('impacts', []):
            target = impact.get('target', {})
            if target.get('targetId'):
                targets_by_id[target['targetId']] = target

    usages_by_target = defaultdict(list)
    for usage in usages:
        usages_by_target[usage['targetId']].append(usage)

    impacts = []
    for target_id in sorted(usages_by_target):
        target = targets_by_id.get(target_id)
        if not target:
            raise SystemExit(f'Missing target metadata for used target {target_id}')
        origin = target.get('targetOrigin', '')
        impact_id = stable_id('impact', target_id, origin, target.get('signature', ''))
        normalized_usages = []
        for u in sorted(usages_by_target[target_id], key=lambda x: (x['sourceFile'], x['line'], x['column'], x['usageKind'])):
            normalized_usages.append({
                'sourceFile': u['sourceFile'],
                'sourceSet': u.get('sourceSet', ''),
                'line': u['line'],
                'column': u.get('column', 0),
                'usageKind': u['usageKind'],
                'expression': u.get('expression', ''),
                'lineText': u.get('lineText', '')
            })

        normalized_target = {
            'targetId': target_id,
            'origin': origin,
            'changeType': target.get('dependencyChangeType', 'VERSION_CHANGED'),
            'coordinate': target.get('coordinate', ''),
            'oldVersion': target.get('oldVersion', ''),
            'newVersion': target.get('newVersion', ''),
            'kind': target.get('kind', ''),
            'symbol': target.get('signature', target.get('className', '')),
            'className': target.get('className', ''),
            'name': target.get('name', ''),
            'compatibilityTypes': target.get('compatibilityTypes', []),
            'sourceCompatible': target.get('sourceCompatible', False),
            'binaryCompatible': target.get('binaryCompatible', False),
            'scopeCategory': target.get('dependencyScopeCategory', target.get('scopeCategory', ''))
        }

        if origin == 'REMOVED_DEPENDENCY_TYPE':
            candidates = build_removed_candidates(target, added_index.get('dependencies', []))
            suggested_transformations = ['CHANGE_TYPE', 'MANUAL_REVIEW']
        elif origin == 'JAPICMP_INCOMPATIBLE_API':
            candidates = build_japicmp_candidates(target, api_changes)
            kind = target.get('kind', '')
            suggested_transformations = {
                'METHOD': ['CHANGE_METHOD_INVOCATION', 'NO_SOURCE_CHANGE', 'MANUAL_REVIEW'],
                'CONSTRUCTOR': ['CHANGE_CONSTRUCTOR', 'MANUAL_REVIEW'],
                'FIELD': ['CHANGE_FIELD', 'MANUAL_REVIEW'],
                'CLASS': ['CHANGE_TYPE', 'MANUAL_REVIEW']
            }.get(kind, ['MANUAL_REVIEW'])
        else:
            candidates = []
            suggested_transformations = ['MANUAL_REVIEW']

        impacts.append({
            'impactId': impact_id,
            'target': normalized_target,
            'usages': normalized_usages,
            'replacementCandidates': candidates,
            'allowedTransformations': suggested_transformations
        })

    planner_input = {
        'schemaVersion': SCHEMA_VERSION,
        'repository': metadata.get('repository', ''),
        'pullRequest': metadata.get('pullRequest'),
        'baseRef': metadata.get('base', {}).get('ref', ''),
        'baseSha': metadata.get('base', {}).get('comparisonSha', metadata.get('base', {}).get('branchSha', '')),
        'headRef': metadata.get('head', {}).get('ref', ''),
        'headSha': metadata.get('head', {}).get('sha', ''),
        'analysisQuality': usage_summary.get('summary', {}).get('analysisQuality', 'UNKNOWN'),
        'impactCount': len(impacts),
        'rules': {
            'candidatePolicy': 'A replacement may only reference a replacementCandidateId listed for the same impact, and the replacement symbol must differ from the source symbol.',
            'noInventedSymbols': True,
            'sourceEvidenceIsMinimal': True,
            'bytecodeInspection': False
        },
        'impacts': impacts
    }

    input_path = output_dir / 'ai-migration-input.json'
    input_path.write_text(json.dumps(planner_input, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    schema = {
        'type': 'object',
        'properties': {
            'summary': {'type': 'string'},
            'decisions': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'impactId': {'type': 'string'},
                        'decision': {'type': 'string', 'enum': ['REPLACE', 'NO_SOURCE_CHANGE', 'MANUAL_REVIEW']},
                        'replacementCandidateId': {'type': ['string', 'null']},
                        'transformation': {'type': 'string', 'enum': ['CHANGE_TYPE', 'CHANGE_METHOD_INVOCATION', 'CHANGE_CONSTRUCTOR', 'CHANGE_FIELD', 'NO_SOURCE_CHANGE', 'MANUAL_REVIEW']},
                        'confidence': {'type': 'string', 'enum': ['HIGH', 'MEDIUM', 'LOW']},
                        'rationale': {'type': 'string'}
                    },
                    'required': ['impactId', 'decision', 'replacementCandidateId', 'transformation', 'confidence', 'rationale'],
                    'additionalProperties': False
                }
            }
        },
        'required': ['summary', 'decisions'],
        'additionalProperties': False
    }
    (output_dir / 'ai-migration-output-schema.json').write_text(json.dumps(schema, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    instructions = '''You are a dependency migration planner. Use only the supplied evidence. Do not invent APIs, Maven coordinates, classes, methods, fields, or constructors. Return exactly one decision for every impactId. A REPLACE decision must use a replacementCandidateId listed under the same impact. Never map a symbol to itself; if no actual replacement is supported by the evidence, return MANUAL_REVIEW. If the evidence is ambiguous or no candidate is sufficient, return MANUAL_REVIEW. For a binary-only incompatibility that does not require a source edit, use NO_SOURCE_CHANGE only when the supplied compatibility evidence supports it. Prefer the smallest source transformation that restores compatibility. Keep rationale concise and evidence-based. Do not generate source code or OpenRewrite recipes.'''
    (output_dir / 'ai-migration-instructions.txt').write_text(instructions + '\n', encoding='utf-8')

    print(json.dumps({'impactCount': len(impacts), 'needsAi': bool(impacts)}, sort_keys=True))


def validate(args):
    input_data = read_json(Path(args.input))
    raw_plan = read_json(Path(args.raw_plan))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    impacts = {i['impactId']: i for i in input_data.get('impacts', [])}
    decisions = raw_plan.get('decisions')
    if not isinstance(decisions, list):
        raise SystemExit('AI output decisions must be an array')

    seen = set()
    errors = []
    enriched = []

    for d in decisions:
        impact_id = d.get('impactId')
        if impact_id not in impacts:
            errors.append(f'Unknown impactId: {impact_id}')
            continue
        if impact_id in seen:
            errors.append(f'Duplicate impactId: {impact_id}')
            continue
        seen.add(impact_id)
        impact = impacts[impact_id]
        candidate_by_id = {c['candidateId']: c for c in impact.get('replacementCandidates', [])}
        decision = d.get('decision')
        candidate_id = d.get('replacementCandidateId')
        transformation = d.get('transformation')

        if decision == 'REPLACE':
            if not candidate_id:
                errors.append(f'{impact_id}: REPLACE requires replacementCandidateId')
            elif candidate_id not in candidate_by_id:
                errors.append(f'{impact_id}: candidate {candidate_id} is not allowed for this impact')
            else:
                replacement_symbol = candidate_by_id[candidate_id].get('symbol', '')
                target_symbol = impact.get('target', {}).get('symbol', '')
                if replacement_symbol and target_symbol and replacement_symbol == target_symbol:
                    errors.append(f'{impact_id}: replacement maps symbol to itself: {target_symbol}')
            if transformation not in impact.get('allowedTransformations', []):
                errors.append(f'{impact_id}: transformation {transformation} not allowed for target')
        elif decision == 'NO_SOURCE_CHANGE':
            if candidate_id is not None:
                errors.append(f'{impact_id}: NO_SOURCE_CHANGE must not have replacementCandidateId')
            if transformation != 'NO_SOURCE_CHANGE':
                errors.append(f'{impact_id}: NO_SOURCE_CHANGE must use NO_SOURCE_CHANGE transformation')
            target = impact['target']
            if target.get('sourceCompatible') is False and target.get('origin') == 'JAPICMP_INCOMPATIBLE_API':
                errors.append(f'{impact_id}: source-incompatible JApiCmp target cannot be NO_SOURCE_CHANGE')
            if target.get('origin') == 'REMOVED_DEPENDENCY_TYPE':
                errors.append(f'{impact_id}: removed dependency type cannot be NO_SOURCE_CHANGE')
        elif decision == 'MANUAL_REVIEW':
            if candidate_id is not None:
                errors.append(f'{impact_id}: MANUAL_REVIEW must not have replacementCandidateId')
            if transformation != 'MANUAL_REVIEW':
                errors.append(f'{impact_id}: MANUAL_REVIEW must use MANUAL_REVIEW transformation')
        else:
            errors.append(f'{impact_id}: unsupported decision {decision}')

        enriched.append({
            'impactId': impact_id,
            'decision': decision,
            'transformation': transformation,
            'confidence': d.get('confidence'),
            'rationale': d.get('rationale', ''),
            'target': impact['target'],
            'replacement': candidate_by_id.get(candidate_id) if candidate_id else None,
            'usages': impact.get('usages', [])
        })

    missing = sorted(set(impacts) - seen)
    if missing:
        errors.append('Missing impactIds: ' + ', '.join(missing))

    validation = {
        'schemaVersion': SCHEMA_VERSION,
        'valid': not errors,
        'expectedImpactCount': len(impacts),
        'decisionCount': len(decisions),
        'errors': errors
    }
    (output_dir / 'ai-migration-validation.json').write_text(json.dumps(validation, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    if errors:
        for error in errors:
            print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(1)

    enriched.sort(key=lambda d: d['impactId'])
    summary_counts = defaultdict(int)
    confidence_counts = defaultdict(int)
    affected_files = set()
    for d in enriched:
        summary_counts[d['decision']] += 1
        confidence_counts[d['confidence']] += 1
        for u in d['usages']:
            affected_files.add(u['sourceFile'])

    final_plan = {
        'schemaVersion': SCHEMA_VERSION,
        'repository': input_data.get('repository'),
        'pullRequest': input_data.get('pullRequest'),
        'baseSha': input_data.get('baseSha'),
        'headSha': input_data.get('headSha'),
        'analysisQuality': input_data.get('analysisQuality'),
        'summary': {
            'text': raw_plan.get('summary', ''),
            'impactCount': len(enriched),
            'replaceCount': summary_counts['REPLACE'],
            'noSourceChangeCount': summary_counts['NO_SOURCE_CHANGE'],
            'manualReviewCount': summary_counts['MANUAL_REVIEW'],
            'affectedSourceFileCount': len(affected_files),
            'confidenceCounts': dict(sorted(confidence_counts.items()))
        },
        'decisions': enriched
    }
    (output_dir / 'ai-migration-plan.json').write_text(json.dumps(final_plan, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    lines = [
        '# AI migration plan', '',
        f"- Impacts: {len(enriched)}",
        f"- Replacements: {summary_counts['REPLACE']}",
        f"- No source change: {summary_counts['NO_SOURCE_CHANGE']}",
        f"- Manual review: {summary_counts['MANUAL_REVIEW']}",
        f"- Affected source files: {len(affected_files)}", '',
        '| Source target | Decision | Replacement | Transformation | Confidence |',
        '|---|---|---|---|---|'
    ]
    for d in enriched:
        source = d['target']['symbol'].replace('|', '\\|')
        replacement = (d['replacement'] or {}).get('symbol', '').replace('|', '\\|') or '—'
        lines.append(f"| `{source}` | {d['decision']} | `{replacement}` | {d['transformation']} | {d['confidence']} |")
    (output_dir / 'ai-migration-plan-summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def empty_plan(args):
    input_data = read_json(Path(args.input))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    final_plan = {
        'schemaVersion': SCHEMA_VERSION,
        'repository': input_data.get('repository'),
        'pullRequest': input_data.get('pullRequest'),
        'baseSha': input_data.get('baseSha'),
        'headSha': input_data.get('headSha'),
        'analysisQuality': input_data.get('analysisQuality'),
        'summary': {
            'text': 'No used incompatible dependency APIs were found; no AI call was required.',
            'impactCount': 0,
            'replaceCount': 0,
            'noSourceChangeCount': 0,
            'manualReviewCount': 0,
            'affectedSourceFileCount': 0,
            'confidenceCounts': {}
        },
        'decisions': []
    }
    (output_dir / 'ai-migration-plan.json').write_text(json.dumps(final_plan, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    (output_dir / 'ai-migration-validation.json').write_text(json.dumps({'schemaVersion': 1, 'valid': True, 'expectedImpactCount': 0, 'decisionCount': 0, 'errors': []}, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    (output_dir / 'ai-migration-plan-summary.md').write_text('# AI migration plan\n\nNo used incompatible dependency APIs were found; no AI call was required.\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)

    b = sub.add_parser('build')
    b.add_argument('--artifact-dir', required=True)
    b.add_argument('--output-dir', required=True)
    b.set_defaults(func=build)

    v = sub.add_parser('validate')
    v.add_argument('--input', required=True)
    v.add_argument('--raw-plan', required=True)
    v.add_argument('--output-dir', required=True)
    v.set_defaults(func=validate)

    e = sub.add_parser('empty-plan')
    e.add_argument('--input', required=True)
    e.add_argument('--output-dir', required=True)
    e.set_defaults(func=empty_plan)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
