#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
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



def method_pattern_arity(method_pattern: str):
    """Return exact argument count for an OpenRewrite method pattern, or None for wildcard/unknown."""
    if not method_pattern or '(' not in method_pattern or ')' not in method_pattern:
        return None
    args = method_pattern.rsplit('(', 1)[1].split(')', 1)[0].strip()
    if args == '':
        return 0
    if '..' in args:
        return None
    return len([part for part in args.split(',') if part.strip()])


def replacement_placeholder_errors(method_pattern: str, replacement: str):
    if not replacement:
        return []
    arity = method_pattern_arity(method_pattern)
    if arity is None:
        return []
    indexes = sorted({int(m.group(1)) for m in re.finditer(r'#\{p(\d+)\}', replacement)})
    errors = []
    for index in indexes:
        if index >= arity:
            errors.append(
                f'replacement references #{{p{index}}}, but methodPattern declares {arity} parameter(s)'
            )
    return errors

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

    # Evidence-backed deterministic Java EE -> Jakarta namespace migration.
    # Only constrain the candidate set when the exact same type exists under
    # jakarta.* in the ADDED dependency type index. This prevents ambiguous
    # same-simple-name candidates such as org.hibernate.Version from being
    # selected for javax.persistence.Version.
    if old_type.startswith('javax.'):
        expected = 'jakarta.' + old_type[len('javax.'):]
        exact = [c for c in candidates if c.get('symbol') == expected]
        if exact:
            for c in exact:
                c['evidence']['deterministicNamespaceMigration'] = True
                c['evidence']['namespaceMigration'] = f'{old_type} -> {expected}'
                c['evidence']['score'] = max(int(c['evidence'].get('score', 0)), 1000)
            exact.sort(key=lambda c: (c['coordinate'], c['symbol']))
            return exact[:max_candidates]

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
            suggested_transformations = ['CHANGE_TYPE', 'CHANGE_PACKAGE', 'REWRITE_CODE']
        elif origin == 'JAPICMP_INCOMPATIBLE_API':
            candidates = build_japicmp_candidates(target, api_changes)
            kind = target.get('kind', '')
            suggested_transformations = {
                'METHOD': ['CHANGE_METHOD_INVOCATION', 'REWRITE_CODE', 'NO_SOURCE_CHANGE'],
                'CONSTRUCTOR': ['CHANGE_CONSTRUCTOR', 'REWRITE_CODE'],
                'FIELD': ['CHANGE_FIELD', 'REWRITE_CODE'],
                'CLASS': ['CHANGE_TYPE', 'CHANGE_PACKAGE', 'REWRITE_CODE']
            }.get(kind, ['REWRITE_CODE'])
        else:
            candidates = []
            suggested_transformations = ['REWRITE_CODE']

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
            'candidatePolicy': 'A replacement may only reference a replacementCandidateId listed for the same impact, and the replacement symbol must differ from the source symbol. If an exact evidence-backed javax.* -> jakarta.* type exists, the candidate set is deterministically restricted to that namespace migration.',
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
                        'decision': {'type': 'string', 'enum': ['REPLACE', 'REWRITE', 'NO_SOURCE_CHANGE']},
                        'replacementCandidateId': {'type': ['string', 'null']},
                        'transformation': {'type': 'string', 'enum': ['CHANGE_TYPE', 'CHANGE_PACKAGE', 'CHANGE_METHOD_INVOCATION', 'CHANGE_CONSTRUCTOR', 'CHANGE_FIELD', 'REWRITE_CODE', 'NO_SOURCE_CHANGE']},
                        'confidence': {'type': 'string', 'enum': ['HIGH', 'MEDIUM', 'LOW']},
                        'rationale': {'type': 'string'},
                        'solutionTitle': {'type': 'string'},
                        'solutionDescription': {'type': 'string'},
                        'targetApi': {'type': 'array', 'items': {'type': 'string'}},
                        'migrationSteps': {'type': 'array', 'items': {'type': 'string'}},
                        'beforeExample': {'type': ['string', 'null']},
                        'afterExample': {'type': ['string', 'null']},
                        'automationHint': {'type': 'string', 'enum': ['DETERMINISTIC_RECIPE', 'CUSTOM_RECIPE', 'CODE_TEMPLATE']},
                        'openRewrite': {
                            'type': ['object', 'null'],
                            'properties': {
                                'recipeKind': {'type': 'string', 'enum': ['CHANGE_TYPE', 'CHANGE_PACKAGE', 'CHANGE_METHOD_NAME', 'INLINE_METHOD_CALLS', 'CUSTOM_JAVA_TEMPLATE', 'NONE']},
                                'methodPattern': {'type': ['string', 'null']},
                                'replacement': {'type': ['string', 'null']},
                                'newMethodName': {'type': ['string', 'null']},
                                'imports': {'type': 'array', 'items': {'type': 'string'}},
                                'staticImports': {'type': 'array', 'items': {'type': 'string'}}
                            },
                            'required': ['recipeKind', 'methodPattern', 'replacement', 'newMethodName', 'imports', 'staticImports'],
                            'additionalProperties': False
                        }
                    },
                    'required': ['impactId', 'decision', 'replacementCandidateId', 'transformation', 'confidence', 'rationale', 'solutionTitle', 'solutionDescription', 'targetApi', 'migrationSteps', 'beforeExample', 'afterExample', 'automationHint', 'openRewrite'],
                    'additionalProperties': False
                }
            }
        },
        'required': ['summary', 'decisions'],
        'additionalProperties': False
    }
    (output_dir / 'ai-migration-output-schema.json').write_text(json.dumps(schema, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    instructions = '''You are a dependency migration planner. Return exactly one concrete migration solution for every impactId. Never return MANUAL_REVIEW and never leave an impact without a proposed solution. Use supplied replacement candidates whenever they represent the target API. IMPORTANT: choose REPLACE only when you also return a non-null replacementCandidateId from the same impact. If no supplied candidate is a safe one-to-one replacement, choose REWRITE instead and provide a concrete target API and source transformation in solutionDescription, targetApi, migrationSteps, beforeExample, and afterExample. You may use your software-migration knowledge to propose well-known successor APIs for removed APIs, but do not invent Maven coordinates and do not claim that an API exists unless you are reasonably confident. If confidence is limited, still provide the best concrete solution and mark confidence LOW. Never map a symbol to itself. Use NO_SOURCE_CHANGE only when the supplied compatibility evidence clearly shows no source edit is required. Prefer the smallest transformation that restores compatibility. IMPORTANT: when a removed javax.* type has an exact jakarta.* replacement candidate with the same remaining fully-qualified suffix, use that supplied candidate; do not substitute an unrelated same-simple-name type (for example javax.persistence.Version must map to jakarta.persistence.Version, never org.hibernate.Version). ALSO return an openRewrite object that is machine-actionable for Step 7. For REPLACE choose CHANGE_TYPE or CHANGE_PACKAGE as appropriate. For REWRITE prefer CHANGE_METHOD_NAME when only the method name changes; otherwise prefer INLINE_METHOD_CALLS when the migration can be expressed as a single method invocation replacement. Use OpenRewrite method pattern syntax such as 'org.hibernate.Session createCriteria(java.lang.Class)' and replacement templates using #{p0}, #{p1}, ... only for arguments actually declared by that exact method pattern. For a zero-argument pattern ending in (), do not reference any #{pN}. For a pattern with two explicit parameters, only #{p0} and #{p1} are valid. If an invocation-level migration needs a context-sensitive expression, choose CUSTOM_JAVA_TEMPLATE and provide methodPattern plus replacement. Use NONE only with NO_SOURCE_CHANGE. Do not put full Java classes in the openRewrite fields.'''
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

        # Normalize a common model mistake deterministically before validation:
        # REPLACE without a candidate id is only safe if exactly one supplied
        # candidate matches the proposed targetApi. Otherwise this is a REWRITE,
        # not a one-to-one replacement.
        if decision == 'REPLACE' and not candidate_id:
            proposed_targets = {str(x).strip() for x in (d.get('targetApi') or []) if str(x).strip()}
            matching = [
                c for c in candidate_by_id.values()
                if c.get('symbol') in proposed_targets
            ]
            if len(matching) == 1:
                candidate_id = matching[0]['candidateId']
                d['replacementCandidateId'] = candidate_id
            elif len(candidate_by_id) == 1:
                only = next(iter(candidate_by_id.values()))
                target_symbol = impact.get('target', {}).get('symbol', '')
                if only.get('symbol') and only.get('symbol') != target_symbol:
                    candidate_id = only['candidateId']
                    d['replacementCandidateId'] = candidate_id
            if not candidate_id:
                decision = 'REWRITE'
                d['decision'] = 'REWRITE'
                d['replacementCandidateId'] = None
                candidate_id = None
                if transformation in (None, '', 'CHANGE_TYPE'):
                    transformation = 'REWRITE_CODE'
                    d['transformation'] = transformation

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
        elif decision == 'REWRITE':
            if candidate_id is not None and candidate_id not in candidate_by_id:
                errors.append(f'{impact_id}: candidate {candidate_id} is not allowed for this impact')
            if transformation not in impact.get('allowedTransformations', []):
                errors.append(f'{impact_id}: transformation {transformation} not allowed for target')
            if transformation == 'NO_SOURCE_CHANGE':
                errors.append(f'{impact_id}: REWRITE cannot use NO_SOURCE_CHANGE transformation')
            if not d.get('targetApi'):
                errors.append(f'{impact_id}: REWRITE requires at least one targetApi entry')
            if not d.get('migrationSteps'):
                errors.append(f'{impact_id}: REWRITE requires migrationSteps')
            if not d.get('solutionDescription'):
                errors.append(f'{impact_id}: REWRITE requires solutionDescription')
        else:
            errors.append(f'{impact_id}: unsupported decision {decision}')

        open_rewrite = d.get('openRewrite')
        if not isinstance(open_rewrite, dict):
            errors.append(f'{impact_id}: openRewrite object is required')
            open_rewrite = {'recipeKind': 'NONE', 'methodPattern': None, 'replacement': None, 'newMethodName': None, 'imports': [], 'staticImports': []}
        recipe_kind = open_rewrite.get('recipeKind')
        if decision == 'NO_SOURCE_CHANGE':
            if recipe_kind != 'NONE':
                errors.append(f'{impact_id}: NO_SOURCE_CHANGE requires openRewrite.recipeKind NONE')
        elif decision == 'REPLACE':
            if recipe_kind not in {'CHANGE_TYPE', 'CHANGE_PACKAGE'}:
                errors.append(f'{impact_id}: REPLACE requires CHANGE_TYPE or CHANGE_PACKAGE OpenRewrite recipe')
        elif decision == 'REWRITE':
            if recipe_kind not in {'CHANGE_METHOD_NAME', 'INLINE_METHOD_CALLS', 'CUSTOM_JAVA_TEMPLATE'}:
                errors.append(f'{impact_id}: REWRITE requires CHANGE_METHOD_NAME, INLINE_METHOD_CALLS, or CUSTOM_JAVA_TEMPLATE')
            if recipe_kind in {'CHANGE_METHOD_NAME', 'INLINE_METHOD_CALLS', 'CUSTOM_JAVA_TEMPLATE'} and not open_rewrite.get('methodPattern'):
                errors.append(f'{impact_id}: {recipe_kind} requires openRewrite.methodPattern')
            if recipe_kind == 'CHANGE_METHOD_NAME' and not open_rewrite.get('newMethodName'):
                errors.append(f'{impact_id}: CHANGE_METHOD_NAME requires openRewrite.newMethodName')
            if recipe_kind in {'INLINE_METHOD_CALLS', 'CUSTOM_JAVA_TEMPLATE'} and not open_rewrite.get('replacement'):
                errors.append(f'{impact_id}: {recipe_kind} requires openRewrite.replacement')
            if recipe_kind in {'INLINE_METHOD_CALLS', 'CUSTOM_JAVA_TEMPLATE'} and open_rewrite.get('methodPattern') and open_rewrite.get('replacement'):
                for placeholder_error in replacement_placeholder_errors(open_rewrite.get('methodPattern'), open_rewrite.get('replacement')):
                    errors.append(f'{impact_id}: {recipe_kind} {placeholder_error}')

        enriched.append({
            'impactId': impact_id,
            'decision': decision,
            'transformation': transformation,
            'confidence': d.get('confidence'),
            'rationale': d.get('rationale', ''),
            'solutionTitle': d.get('solutionTitle', ''),
            'solutionDescription': d.get('solutionDescription', ''),
            'targetApi': d.get('targetApi', []),
            'migrationSteps': d.get('migrationSteps', []),
            'beforeExample': d.get('beforeExample'),
            'afterExample': d.get('afterExample'),
            'automationHint': d.get('automationHint', ''),
            'openRewrite': open_rewrite,
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
            'rewriteCount': summary_counts['REWRITE'],
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
        f"- Complex rewrites: {summary_counts['REWRITE']}",
        f"- Affected source files: {len(affected_files)}", '',
        '| Source target | Decision | Replacement | Transformation | Confidence |',
        '|---|---|---|---|---|'
    ]
    for d in enriched:
        source = d['target']['symbol'].replace('|', '\\|')
        replacement = (d['replacement'] or {}).get('symbol', '').replace('|', '\\|') or '—'
        lines.append(f"| `{source}` | {d['decision']} | `{replacement}` | {d['transformation']} | {d['confidence']} |")
    (output_dir / 'ai-migration-plan-summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')



def check_plan(args):
    input_data = read_json(Path(args.input))
    raw_plan = read_json(Path(args.raw_plan))
    impacts = {i['impactId']: i for i in input_data.get('impacts', [])}
    decisions = raw_plan.get('decisions')
    report = {
        'schemaVersion': SCHEMA_VERSION,
        'valid': False,
        'expectedImpactCount': len(impacts),
        'decisionCount': len(decisions) if isinstance(decisions, list) else 0,
        'invalidImpacts': [],
        'globalErrors': [],
    }
    if not isinstance(decisions, list):
        report['globalErrors'].append('AI output decisions must be an array')
        Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        return

    seen = set()
    invalid = {}

    def add(impact_id, message):
        invalid.setdefault(impact_id, []).append(message)

    for d in decisions:
        impact_id = d.get('impactId')
        if impact_id not in impacts:
            report['globalErrors'].append(f'Unknown impactId: {impact_id}')
            continue
        if impact_id in seen:
            add(impact_id, f'Duplicate impactId: {impact_id}')
            continue
        seen.add(impact_id)
        impact = impacts[impact_id]
        candidates = {c['candidateId']: c for c in impact.get('replacementCandidates', [])}
        decision = d.get('decision')
        candidate_id = d.get('replacementCandidateId')
        transformation = d.get('transformation')

        if decision == 'REPLACE':
            if not candidate_id:
                add(impact_id, 'REPLACE requires replacementCandidateId')
            elif candidate_id not in candidates:
                add(impact_id, f'candidate {candidate_id} is not allowed for this impact')
            else:
                repl = candidates[candidate_id].get('symbol', '')
                old = impact.get('target', {}).get('symbol', '')
                if repl and old and repl == old:
                    add(impact_id, f'replacement maps symbol to itself: {old}')
            if transformation not in impact.get('allowedTransformations', []):
                add(impact_id, f'transformation {transformation} not allowed for target')
        elif decision == 'NO_SOURCE_CHANGE':
            if candidate_id is not None:
                add(impact_id, 'NO_SOURCE_CHANGE must not have replacementCandidateId')
            if transformation != 'NO_SOURCE_CHANGE':
                add(impact_id, 'NO_SOURCE_CHANGE must use NO_SOURCE_CHANGE transformation')
            target = impact.get('target', {})
            if target.get('sourceCompatible') is False and target.get('origin') == 'JAPICMP_INCOMPATIBLE_API':
                add(impact_id, 'source-incompatible JApiCmp target cannot be NO_SOURCE_CHANGE')
            if target.get('origin') == 'REMOVED_DEPENDENCY_TYPE':
                add(impact_id, 'removed dependency type cannot be NO_SOURCE_CHANGE')
        elif decision == 'REWRITE':
            if candidate_id is not None and candidate_id not in candidates:
                add(impact_id, f'candidate {candidate_id} is not allowed for this impact')
            if transformation not in impact.get('allowedTransformations', []):
                add(impact_id, f'transformation {transformation} not allowed for target')
            if transformation == 'NO_SOURCE_CHANGE':
                add(impact_id, 'REWRITE cannot use NO_SOURCE_CHANGE transformation')
            if not d.get('targetApi'):
                add(impact_id, 'REWRITE requires at least one targetApi entry')
            if not d.get('migrationSteps'):
                add(impact_id, 'REWRITE requires migrationSteps')
            if not d.get('solutionDescription'):
                add(impact_id, 'REWRITE requires solutionDescription')
        else:
            add(impact_id, f'unsupported decision {decision!r}')

        ow = d.get('openRewrite')
        if not isinstance(ow, dict):
            add(impact_id, 'openRewrite object is required')
            ow = {}
        kind = ow.get('recipeKind')
        if decision == 'NO_SOURCE_CHANGE':
            if kind != 'NONE':
                add(impact_id, 'NO_SOURCE_CHANGE requires openRewrite.recipeKind NONE')
        elif decision == 'REPLACE':
            if kind not in {'CHANGE_TYPE', 'CHANGE_PACKAGE'}:
                add(impact_id, 'REPLACE requires CHANGE_TYPE or CHANGE_PACKAGE OpenRewrite recipe')
        elif decision == 'REWRITE':
            allowed = {'CHANGE_METHOD_NAME', 'INLINE_METHOD_CALLS', 'CUSTOM_JAVA_TEMPLATE'}
            if kind not in allowed:
                add(impact_id, 'REWRITE requires CHANGE_METHOD_NAME, INLINE_METHOD_CALLS, or CUSTOM_JAVA_TEMPLATE')
            if kind in allowed and not ow.get('methodPattern'):
                add(impact_id, f'{kind} requires openRewrite.methodPattern')
            if kind == 'CHANGE_METHOD_NAME' and not ow.get('newMethodName'):
                add(impact_id, 'CHANGE_METHOD_NAME requires openRewrite.newMethodName')
            if kind in {'INLINE_METHOD_CALLS', 'CUSTOM_JAVA_TEMPLATE'} and not ow.get('replacement'):
                add(impact_id, f'{kind} requires openRewrite.replacement')
            if kind in {'INLINE_METHOD_CALLS', 'CUSTOM_JAVA_TEMPLATE'} and ow.get('methodPattern') and ow.get('replacement'):
                for placeholder_error in replacement_placeholder_errors(ow.get('methodPattern'), ow.get('replacement')):
                    add(impact_id, f'{kind} {placeholder_error}')

    for missing in sorted(set(impacts) - seen):
        add(missing, 'Missing decision for impactId')

    by_id = {d.get('impactId'): d for d in decisions if isinstance(d, dict) and d.get('impactId')}
    for impact_id in sorted(invalid):
        report['invalidImpacts'].append({
            'impactId': impact_id,
            'errors': invalid[impact_id],
            'previousDecision': by_id.get(impact_id),
        })

    report['valid'] = not report['globalErrors'] and not report['invalidImpacts']
    Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')


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
            'rewriteCount': 0,
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

    c = sub.add_parser('check')
    c.add_argument('--input', required=True)
    c.add_argument('--raw-plan', required=True)
    c.add_argument('--report', required=True)
    c.set_defaults(func=check_plan)

    e = sub.add_parser('empty-plan')
    e.add_argument('--input', required=True)
    e.add_argument('--output-dir', required=True)
    e.set_defaults(func=empty_plan)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
