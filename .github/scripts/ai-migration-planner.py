#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

SCHEMA_VERSION = 2
FQCN_RE = re.compile(r'^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+$')
PLACEHOLDER_RE = re.compile(r'#\{p(\d+)\}')
REWRITE_UNIT_KINDS = {
    'CHANGE_METHOD_NAME',
    'INLINE_METHOD_CALLS',
    'CUSTOM_EXPRESSION_TEMPLATE',
    'REPLACE_METHOD_BODY',
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def read_jsonl(path: Path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            result.append(json.loads(line))
    return result


def write_json(path: Path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


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
    score += min(package_suffix_overlap(old_type, candidate_type), 4) * 20
    if old_scope and new_scope and old_scope == new_scope:
        score += 10
    return score


def build_removed_candidates(target, added_dependencies, max_candidates=20):
    old_type = target.get('signature') or target.get('className') or target.get('targetSignature') or ''
    old_scope = target.get('dependencyScopeCategory', '')
    candidates = []
    for dep in added_dependencies:
        for class_name in dep.get('classNames', []):
            if class_name == old_type:
                continue
            score = candidate_score_for_type(old_type, class_name, old_scope, dep.get('scopeCategory', ''))
            if score < 30:
                continue
            candidate_id = stable_id('cand', dep.get('coordinate', ''), dep.get('newVersion', ''), class_name)
            candidates.append({
                'candidateId': candidate_id,
                'kind': 'CLASS',
                'coordinate': dep.get('coordinate', ''),
                'version': dep.get('newVersion', ''),
                'symbol': class_name,
                'scopeCategory': dep.get('scopeCategory', ''),
                'evidence': {
                    'simpleNameEqual': simple_name(class_name) == simple_name(old_type),
                    'packageSuffixOverlap': package_suffix_overlap(old_type, class_name),
                    'score': score,
                    'source': 'ADDED_DEPENDENCY_TYPE_INDEX',
                },
            })
    candidates.sort(key=lambda c: (-c['evidence']['score'], c['coordinate'], c['symbol']))

    return candidates[:max_candidates]


def api_candidate_score(target, candidate):
    score = 0
    if candidate.get('kind') == target.get('kind'):
        score += 30
    if candidate.get('className') == target.get('className'):
        score += 80
    elif package_name(candidate.get('className', '')) == package_name(target.get('className', '')):
        score += 20
    old_name = target.get('name') or ''
    new_name = candidate.get('name') or ''
    if old_name and new_name:
        score += 60 if old_name == new_name else int(30 * SequenceMatcher(None, old_name, new_name).ratio())
    return score


def build_japicmp_candidates(target, api_changes, max_candidates=30):
    result = []
    coordinate = target.get('coordinate', '')
    target_symbol = target.get('signature') or target.get('targetSignature') or ''
    for candidate in api_changes:
        if candidate.get('coordinate') != coordinate:
            continue
        if candidate.get('changeStatus') not in {'NEW', 'MODIFIED'}:
            continue
        if target.get('kind') and candidate.get('kind') != target.get('kind'):
            continue
        symbol = candidate.get('signature', '')
        if symbol and target_symbol and symbol == target_symbol:
            continue
        score = api_candidate_score(target, candidate)
        if score < 30:
            continue
        result.append({
            'candidateId': stable_id('cand', coordinate, candidate.get('newVersion', ''), symbol),
            'kind': candidate.get('kind', ''),
            'coordinate': coordinate,
            'version': candidate.get('newVersion', ''),
            'symbol': symbol,
            'scopeCategory': candidate.get('scopeCategory', ''),
            'evidence': {
                'score': score,
                'changeStatus': candidate.get('changeStatus', ''),
                'source': 'JAPICMP_NEW_OR_MODIFIED_API',
            },
        })
    result.sort(key=lambda c: (-c['evidence']['score'], c['symbol']))
    dedup = []
    seen = set()
    for candidate in result:
        if candidate['candidateId'] not in seen:
            seen.add(candidate['candidateId'])
            dedup.append(candidate)
    return dedup[:max_candidates]


def usage_method_name(enclosing_method: str):
    if not enclosing_method or '#' not in enclosing_method:
        return None
    tail = enclosing_method.split('#', 1)[1]
    return tail.split('(', 1)[0] if '(' in tail else tail


def method_pattern_name(pattern: str):
    if not pattern or '(' not in pattern:
        return None
    left = pattern.split('(', 1)[0].strip()
    return left.rsplit(' ', 1)[-1] if left else None


def method_pattern_declaring_type(pattern: str):
    if not pattern or '(' not in pattern:
        return None
    left = pattern.split('(', 1)[0].strip()
    if ' ' not in left:
        return None
    return left.rsplit(' ', 1)[0].strip() or None


def enclosing_method_parts(enclosing_method: str):
    if not enclosing_method or '#' not in enclosing_method:
        return None, None
    declaring_type, tail = enclosing_method.split('#', 1)
    method_name = tail.split('(', 1)[0] if '(' in tail else tail
    return declaring_type or None, method_name or None


def method_pattern_arity(pattern: str):
    if not pattern or '(' not in pattern or ')' not in pattern:
        return None
    args = pattern.rsplit('(', 1)[1].split(')', 1)[0].strip()
    if not args:
        return 0
    if '..' in args:
        return None
    return len([part for part in args.split(',') if part.strip()])


def placeholder_errors(pattern: str, replacement: str):
    arity = method_pattern_arity(pattern)
    if arity is None or not replacement:
        return []
    errors = []
    for index in sorted({int(m.group(1)) for m in PLACEHOLDER_RE.finditer(replacement)}):
        if index >= arity:
            errors.append(f'replacement references #{{p{index}}}, but methodPattern declares {arity} parameter(s)')
    return errors


def normalize_usage(usage):
    return {
        'sourceFile': usage.get('sourceFile', ''),
        'sourceSet': usage.get('sourceSet', ''),
        'line': usage.get('line', 0),
        'column': usage.get('column', 0),
        'usageKind': usage.get('usageKind', ''),
        'expression': usage.get('expression', ''),
        'lineText': usage.get('lineText', ''),
        'enclosingType': usage.get('enclosingType', ''),
        'enclosingMethod': usage.get('enclosingMethod', ''),
    }


def output_schema():
    decision = {
        'type': 'object',
        'properties': {
            'impactId': {'type': 'string'},
            'decision': {'type': 'string', 'enum': ['REPLACE', 'REWRITE', 'NO_SOURCE_CHANGE']},
            'replacementCandidateId': {'type': ['string', 'null']},
            'transformation': {'type': 'string', 'enum': ['CHANGE_TYPE', 'CHANGE_PACKAGE', 'REWRITE_CODE', 'NO_SOURCE_CHANGE']},
            'confidence': {'type': 'string', 'enum': ['HIGH', 'MEDIUM', 'LOW']},
            'rationale': {'type': 'string'},
            'solutionTitle': {'type': 'string'},
            'solutionDescription': {'type': 'string'},
            'targetApi': {'type': 'array', 'items': {'type': 'string'}},
            'migrationSteps': {'type': 'array', 'items': {'type': 'string'}},
            'beforeExample': {'type': ['string', 'null']},
            'afterExample': {'type': ['string', 'null']},
            'automationHint': {'type': 'string', 'enum': ['DETERMINISTIC_RECIPE', 'CUSTOM_RECIPE', 'CODE_TEMPLATE']},
            'rewriteUnitIds': {'type': 'array', 'items': {'type': 'string'}},
        },
        'required': ['impactId', 'decision', 'replacementCandidateId', 'transformation', 'confidence', 'rationale', 'solutionTitle', 'solutionDescription', 'targetApi', 'migrationSteps', 'beforeExample', 'afterExample', 'automationHint', 'rewriteUnitIds'],
        'additionalProperties': False,
    }
    unit = {
        'type': 'object',
        'properties': {
            'unitId': {'type': 'string'},
            'coversImpactIds': {'type': 'array', 'items': {'type': 'string'}},
            'recipeKind': {'type': 'string', 'enum': sorted(REWRITE_UNIT_KINDS)},
            'methodPattern': {'type': 'string'},
            'replacement': {'type': ['string', 'null']},
            'newMethodName': {'type': ['string', 'null']},
            'imports': {'type': 'array', 'items': {'type': 'string'}},
            'staticImports': {'type': 'array', 'items': {'type': 'string'}},
        },
        'required': ['unitId', 'coversImpactIds', 'recipeKind', 'methodPattern', 'replacement', 'newMethodName', 'imports', 'staticImports'],
        'additionalProperties': False,
    }
    return {
        'type': 'object',
        'properties': {
            'summary': {'type': 'string'},
            'decisions': {'type': 'array', 'items': decision},
            'rewriteUnits': {'type': 'array', 'items': unit},
        },
        'required': ['summary', 'decisions', 'rewriteUnits'],
        'additionalProperties': False,
    }


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
    for source_name in ['api-usage-impact.json', 'removed-dependency-impact.json']:
        path = artifact_dir / source_name
        if not path.exists():
            continue
        for impact in read_json(path).get('impacts', []):
            target = impact.get('target', {})
            target_id = target.get('targetId')
            if target_id:
                targets_by_id[target_id] = target

    usages_by_target = defaultdict(list)
    for usage in usages:
        target_id = usage.get('targetId')
        if target_id:
            usages_by_target[target_id].append(usage)

    impacts = []
    for target_id in sorted(usages_by_target):
        target = targets_by_id.get(target_id)
        if not target:
            raise SystemExit(f'Missing target metadata for used target {target_id}')
        origin = target.get('targetOrigin', '')
        impact_id = stable_id('impact', target_id, origin, target.get('signature', ''))
        normalized_usages = [
            normalize_usage(u)
            for u in sorted(usages_by_target[target_id], key=lambda x: (x.get('sourceFile', ''), x.get('line', 0), x.get('column', 0), x.get('usageKind', '')))
        ]
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
            'scopeCategory': target.get('dependencyScopeCategory', target.get('scopeCategory', '')),
        }
        if origin == 'REMOVED_DEPENDENCY_TYPE':
            candidates = build_removed_candidates(target, added_index.get('dependencies', []))
            allowed = ['CHANGE_TYPE', 'CHANGE_PACKAGE', 'REWRITE_CODE']
        elif origin == 'JAPICMP_INCOMPATIBLE_API':
            candidates = build_japicmp_candidates(target, api_changes)
            kind = target.get('kind', '')
            allowed = {
                'METHOD': ['REWRITE_CODE', 'NO_SOURCE_CHANGE'],
                'CONSTRUCTOR': ['REWRITE_CODE'],
                'FIELD': ['REWRITE_CODE'],
                'CLASS': ['CHANGE_TYPE', 'CHANGE_PACKAGE', 'REWRITE_CODE'],
            }.get(kind, ['REWRITE_CODE'])
        else:
            candidates = []
            allowed = ['REWRITE_CODE']

        enclosing_methods = sorted({u['enclosingMethod'] for u in normalized_usages if u.get('enclosingMethod')})
        impact = {
            'impactId': impact_id,
            'target': normalized_target,
            'usages': normalized_usages,
            'affectedEnclosingMethods': enclosing_methods,
            'replacementCandidates': candidates,
            'allowedTransformations': allowed,
        }
        impacts.append(impact)

    method_groups = defaultdict(list)
    for impact in impacts:
        for enclosing_method in impact.get('affectedEnclosingMethods', []):
            method_groups[enclosing_method].append(impact['impactId'])
    source_method_groups = [
        {
            'enclosingMethod': method,
            'impactIds': sorted(set(impact_ids)),
            'impactSymbols': sorted({
                impact['target'].get('symbol', '')
                for impact in impacts
                if impact['impactId'] in impact_ids
            }),
        }
        for method, impact_ids in sorted(method_groups.items())
    ]

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
            'candidatePolicy': 'REPLACE may only use a candidate listed on that impact. Candidate ranking is heuristic evidence, never an authoritative migration decision.',
            'coherentRewritePolicy': 'The AI must decide from source context and sourceMethodGroups whether affected API usages can be rewritten independently or require one coordinated method-body rewrite.',
            'noLibrarySpecificRules': True,
            'noInventedMavenCoordinates': True,
            'sourceContextMayBeAttached': True,
            'bytecodeInspection': False,
        },
        'impacts': impacts,
        'sourceMethodGroups': source_method_groups,
        'sourceContexts': [],
    }
    write_json(output_dir / 'ai-migration-input.json', planner_input)
    write_json(output_dir / 'ai-migration-output-schema.json', output_schema())

    instructions = '''You are a Java dependency migration planner. Produce a library-agnostic, machine-actionable OpenRewrite plan, not a code review.

RULES:
1. Return exactly one decision for every impactId and no others. Never return MANUAL_REVIEW.
2. Do not apply framework-, vendor-, package-, or library-specific hardcoded assumptions. Infer the migration only from the supplied API-diff evidence, replacement candidates, exact PR-HEAD source context, and your Java/library knowledge.
3. REPLACE is only for a safe one-to-one supplied replacementCandidateId representing a type/package migration. Never invent a replacement candidate and never map a symbol to itself. Candidate scores are hints, not proof.
4. NO_SOURCE_CHANGE is only valid when the supplied compatibility evidence and source usage show that no source edit is required.
5. REWRITE is for source transformations without a safe one-to-one type/package candidate. Every REWRITE decision must reference one or more rewriteUnitIds.
6. rewriteUnits are shared transformation units. One unit may cover multiple impacts and one impact may reference multiple units.
7. Use CHANGE_METHOD_NAME only for a pure method rename. Use INLINE_METHOD_CALLS only when one invocation can be replaced independently while preserving expression semantics. Use CUSTOM_EXPRESSION_TEMPLATE only for one expression replacement. Use REPLACE_METHOD_BODY when a migration requires coordinated changes to multiple statements, variables, chained calls, control/data flow, or several impacted APIs in the same application method.
8. Inspect sourceMethodGroups before selecting rewrite granularity. Multiple impacts in the same method are not automatically coupled, but if independent rewrites could create inconsistent intermediate code, change types used by later calls, lose behavior, or require coordinated local variables/statements, create one shared REPLACE_METHOD_BODY unit covering all relevant impacts for that application method.
9. For REPLACE_METHOD_BODY, methodPattern must match the APPLICATION method declaration, not the dependency API being removed or changed. replacement is the complete new body content WITHOUT outer braces. Preserve observable behavior, parameters, return behavior, ordering, error handling, limits, state changes, and transaction/resource semantics visible in the source.
10. sourceContexts contains exact PR-HEAD source text when available. Base all code templates on actual names and structures from that source; do not invent fields or local variables that are not introduced by your replacement.
11. methodPattern uses OpenRewrite MethodMatcher syntax: fully.qualified.DeclaringType methodName(argument.Types).
12. REPLACE_METHOD_BODY replacement contains body statements only, without surrounding { } and without #{pN} placeholders. It may refer directly to parameters/fields visible in that application method and may declare required locals.
13. For INLINE_METHOD_CALLS and CUSTOM_EXPRESSION_TEMPLATE, #{p0}, #{p1}, ... refer to invocation arguments and must respect methodPattern arity. CUSTOM_EXPRESSION_TEMPLATE may also use #{select} for the invocation select.
14. imports/staticImports must list every non-java.lang type introduced by a template that is not already fully qualified. Keep them minimal.
15. This step plans source migration only. Do not invent or add Maven dependencies. If the supplied evidence cannot support a dependency change, solve the source migration using APIs available after the dependency update.
16. Prefer deterministic OpenRewrite built-ins over custom templates when they fully express the migration. Prefer the smallest safe rewrite granularity: type/package, then method rename/inline expression, then custom expression, then full method body.
17. A generated plan must be compilable in principle against the PR-HEAD dependency set. Do not emit source references to APIs identified by the evidence as removed/incompatible unless another supplied candidate/evidence proves they remain valid.
18. Return ONLY the JSON object described by the supplied schema. No Markdown fences or prose outside JSON.'''
    (output_dir / 'ai-migration-instructions.txt').write_text(instructions + '\n', encoding='utf-8')
    print(json.dumps({'schemaVersion': SCHEMA_VERSION, 'impactCount': len(impacts), 'needsAi': bool(impacts)}, sort_keys=True))


def normalize_plan(input_data, raw_plan):
    impacts = {i['impactId']: i for i in input_data.get('impacts', [])}
    raw_decisions = raw_plan.get('decisions') if isinstance(raw_plan.get('decisions'), list) else []
    by_id = {d.get('impactId'): d for d in raw_decisions if isinstance(d, dict) and d.get('impactId') in impacts}
    raw_units = raw_plan.get('rewriteUnits') if isinstance(raw_plan.get('rewriteUnits'), list) else []
    units = []
    kept_ids = set()
    for unit in raw_units:
        if not isinstance(unit, dict):
            continue
        clean = dict(unit)
        covers = [i for i in clean.get('coversImpactIds', []) if i in impacts]
        clean['coversImpactIds'] = list(dict.fromkeys(covers))
        if not clean['coversImpactIds']:
            continue
        unit_id = clean.get('unitId')
        if not unit_id:
            basis = clean.get('methodPattern', '') + '|' + clean.get('recipeKind', '')
            unit_id = stable_id('unit', basis, ','.join(clean['coversImpactIds']))
            clean['unitId'] = unit_id
        if unit_id in kept_ids:
            continue
        kept_ids.add(unit_id)
        units.append(clean)

    for impact_id, decision in list(by_id.items()):
        refs = decision.get('rewriteUnitIds') if isinstance(decision.get('rewriteUnitIds'), list) else []
        decision = dict(decision)
        decision['rewriteUnitIds'] = [r for r in dict.fromkeys(refs) if r in kept_ids]
        by_id[impact_id] = decision

    return {
        'summary': str(raw_plan.get('summary') or ''),
        'decisions': [by_id[k] for k in sorted(by_id)],
        'rewriteUnits': sorted(units, key=lambda x: x.get('unitId', '')),
    }


def validate_plan_structure(input_data, raw_plan):
    plan = normalize_plan(input_data, raw_plan)
    impacts = {i['impactId']: i for i in input_data.get('impacts', [])}
    decisions = plan.get('decisions', [])
    units = plan.get('rewriteUnits', [])
    errors_by_impact = defaultdict(list)
    global_errors = []

    decision_by_id = {}
    for d in decisions:
        impact_id = d.get('impactId')
        if impact_id not in impacts:
            global_errors.append(f'Unknown impactId: {impact_id}')
            continue
        if impact_id in decision_by_id:
            errors_by_impact[impact_id].append('Duplicate decision')
            continue
        decision_by_id[impact_id] = d

    for impact_id in sorted(set(impacts) - set(decision_by_id)):
        errors_by_impact[impact_id].append('Missing decision')

    unit_by_id = {}
    method_body_patterns = {}
    for unit in units:
        unit_id = unit.get('unitId')
        if not unit_id:
            global_errors.append('rewriteUnit without unitId')
            continue
        if unit_id in unit_by_id:
            global_errors.append(f'Duplicate rewriteUnit unitId: {unit_id}')
            continue
        unit_by_id[unit_id] = unit
        kind = unit.get('recipeKind')
        if kind not in REWRITE_UNIT_KINDS:
            global_errors.append(f'{unit_id}: unsupported recipeKind {kind!r}')
        if not unit.get('methodPattern'):
            global_errors.append(f'{unit_id}: methodPattern is required')
        elif kind == 'REPLACE_METHOD_BODY':
            pattern = unit.get('methodPattern')
            if pattern in method_body_patterns:
                global_errors.append(
                    f'{unit_id}: duplicate REPLACE_METHOD_BODY target {pattern!r}; '
                    f'use one shared unit with {method_body_patterns[pattern]}'
                )
            else:
                method_body_patterns[pattern] = unit_id
        covers = unit.get('coversImpactIds')
        if not isinstance(covers, list) or not covers:
            global_errors.append(f'{unit_id}: coversImpactIds must be non-empty')
            covers = []
        for impact_id in covers:
            if impact_id not in impacts:
                global_errors.append(f'{unit_id}: unknown covered impactId {impact_id}')
        if kind == 'CHANGE_METHOD_NAME':
            if not unit.get('newMethodName'):
                global_errors.append(f'{unit_id}: CHANGE_METHOD_NAME requires newMethodName')
            if unit.get('replacement') is not None:
                global_errors.append(f'{unit_id}: CHANGE_METHOD_NAME replacement must be null')
        elif kind in {'INLINE_METHOD_CALLS', 'CUSTOM_EXPRESSION_TEMPLATE'}:
            if not unit.get('replacement'):
                global_errors.append(f'{unit_id}: {kind} requires replacement')
            for err in placeholder_errors(unit.get('methodPattern', ''), unit.get('replacement') or ''):
                global_errors.append(f'{unit_id}: {kind} {err}')
        elif kind == 'REPLACE_METHOD_BODY':
            replacement = unit.get('replacement')
            if not isinstance(replacement, str) or not replacement.strip():
                global_errors.append(f'{unit_id}: REPLACE_METHOD_BODY requires replacement body content')
            elif replacement.strip().startswith('{') or replacement.strip().endswith('}'):
                global_errors.append(f'{unit_id}: REPLACE_METHOD_BODY replacement must not include outer braces')
            if replacement and PLACEHOLDER_RE.search(replacement):
                global_errors.append(f'{unit_id}: REPLACE_METHOD_BODY must not use #{{pN}} placeholders')
            if unit.get('newMethodName') is not None:
                global_errors.append(f'{unit_id}: REPLACE_METHOD_BODY newMethodName must be null')

        # Generic structural validation: a method-body unit may only claim impacts
        # that actually occur inside the application method targeted by the unit.
        if kind == 'REPLACE_METHOD_BODY':
            pattern_type = method_pattern_declaring_type(unit.get('methodPattern', ''))
            pattern_name = method_pattern_name(unit.get('methodPattern', ''))
            for impact_id in covers:
                impact = impacts.get(impact_id, {})
                matches_context = any(
                    enclosing_method_parts(value) == (pattern_type, pattern_name)
                    for value in impact.get('affectedEnclosingMethods', [])
                )
                if not matches_context:
                    errors_by_impact[impact_id].append(
                        f'rewriteUnit {unit_id} targets {pattern_type}#{pattern_name}, '
                        'but this impact has no usage in that application method'
                    )

        # Generic stale-API guard for rewritten code: do not explicitly re-import or
        # fully-qualify an API that the evidence says is the removed/incompatible target.
        textual_rewrite = unit.get('replacement') or ''
        declared_imports = set(unit.get('imports') or []) | set(unit.get('staticImports') or [])
        for impact_id in covers:
            impact = impacts.get(impact_id, {})
            old_symbol = (impact.get('target') or {}).get('symbol', '')
            if FQCN_RE.match(old_symbol) and (old_symbol in declared_imports or old_symbol in textual_rewrite):
                errors_by_impact[impact_id].append(
                    f'rewriteUnit {unit_id} explicitly reuses affected API {old_symbol}'
                )

    for impact_id, impact in impacts.items():
        d = decision_by_id.get(impact_id)
        if not d:
            continue
        candidates = {c['candidateId']: c for c in impact.get('replacementCandidates', [])}
        decision = d.get('decision')
        candidate_id = d.get('replacementCandidateId')
        transformation = d.get('transformation')
        refs = d.get('rewriteUnitIds') if isinstance(d.get('rewriteUnitIds'), list) else []

        if decision == 'REPLACE':
            if not candidate_id or candidate_id not in candidates:
                errors_by_impact[impact_id].append('REPLACE requires an allowed replacementCandidateId')
            else:
                old = impact.get('target', {}).get('symbol', '')
                new = candidates[candidate_id].get('symbol', '')
                if old == new:
                    errors_by_impact[impact_id].append('replacement maps the symbol to itself')
            if transformation not in {'CHANGE_TYPE', 'CHANGE_PACKAGE'}:
                errors_by_impact[impact_id].append('REPLACE requires CHANGE_TYPE or CHANGE_PACKAGE')
            elif transformation == 'CHANGE_PACKAGE' and candidate_id in candidates:
                old = impact.get('target', {}).get('symbol', '')
                new = candidates[candidate_id].get('symbol', '')
                if simple_name(old) != simple_name(new):
                    errors_by_impact[impact_id].append('CHANGE_PACKAGE requires the simple type name to stay unchanged')
            if refs:
                errors_by_impact[impact_id].append('REPLACE must not reference rewrite units')
        elif decision == 'NO_SOURCE_CHANGE':
            if candidate_id is not None:
                errors_by_impact[impact_id].append('NO_SOURCE_CHANGE must not have replacementCandidateId')
            if transformation != 'NO_SOURCE_CHANGE':
                errors_by_impact[impact_id].append('NO_SOURCE_CHANGE requires NO_SOURCE_CHANGE transformation')
            if refs:
                errors_by_impact[impact_id].append('NO_SOURCE_CHANGE must not reference rewrite units')
            if impact.get('target', {}).get('origin') == 'REMOVED_DEPENDENCY_TYPE':
                errors_by_impact[impact_id].append('truly removed dependency type cannot be NO_SOURCE_CHANGE')
        elif decision == 'REWRITE':
            if transformation != 'REWRITE_CODE':
                errors_by_impact[impact_id].append('REWRITE must use REWRITE_CODE transformation')
            if not d.get('solutionDescription'):
                errors_by_impact[impact_id].append('REWRITE requires solutionDescription')
            if not d.get('migrationSteps'):
                errors_by_impact[impact_id].append('REWRITE requires migrationSteps')
            if not d.get('targetApi'):
                errors_by_impact[impact_id].append('REWRITE requires targetApi')
            if not refs:
                errors_by_impact[impact_id].append('REWRITE requires at least one rewriteUnitId')
            for ref in refs:
                unit = unit_by_id.get(ref)
                if not unit:
                    errors_by_impact[impact_id].append(f'references unknown rewriteUnitId {ref}')
                elif impact_id not in unit.get('coversImpactIds', []):
                    errors_by_impact[impact_id].append(f'rewriteUnit {ref} does not cover this impact')
        else:
            errors_by_impact[impact_id].append(f'unsupported decision {decision!r}')

    for unit_id, unit in unit_by_id.items():
        for impact_id in unit.get('coversImpactIds', []):
            decision = decision_by_id.get(impact_id)
            if decision and unit_id not in (decision.get('rewriteUnitIds') or []):
                errors_by_impact[impact_id].append(f'rewriteUnit {unit_id} covers impact but decision does not reference it')

    report = {
        'schemaVersion': SCHEMA_VERSION,
        'valid': not global_errors and not errors_by_impact,
        'expectedImpactCount': len(impacts),
        'decisionCount': len(decisions),
        'rewriteUnitCount': len(units),
        'globalErrors': global_errors,
        'invalidImpacts': [
            {'impactId': impact_id, 'errors': errors, 'previousDecision': decision_by_id.get(impact_id)}
            for impact_id, errors in sorted(errors_by_impact.items())
        ],
    }
    return plan, report


def command_normalize(args):
    input_data = read_json(Path(args.input))
    raw_path = Path(args.raw_plan)
    normalized = normalize_plan(input_data, read_json(raw_path))
    write_json(raw_path, normalized)


def command_check(args):
    input_data = read_json(Path(args.input))
    _, report = validate_plan_structure(input_data, read_json(Path(args.raw_plan)))
    write_json(Path(args.report), report)


def command_validate(args):
    input_data = read_json(Path(args.input))
    raw_plan = read_json(Path(args.raw_plan))
    plan, report = validate_plan_structure(input_data, raw_plan)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / 'ai-migration-validation.json', report)
    if not report['valid']:
        for error in report['globalErrors']:
            print(f'ERROR: {error}', file=sys.stderr)
        for invalid in report['invalidImpacts']:
            for error in invalid['errors']:
                print(f"ERROR: {invalid['impactId']}: {error}", file=sys.stderr)
        raise SystemExit(1)

    impacts = {i['impactId']: i for i in input_data.get('impacts', [])}
    candidate_by_id = {
        c['candidateId']: c
        for impact in impacts.values()
        for c in impact.get('replacementCandidates', [])
    }
    enriched = []
    counts = defaultdict(int)
    confidence_counts = defaultdict(int)
    affected_files = set()
    for d in plan['decisions']:
        impact = impacts[d['impactId']]
        item = dict(d)
        candidate_id = d.get('replacementCandidateId')
        item['target'] = impact['target']
        item['replacement'] = candidate_by_id.get(candidate_id) if candidate_id else None
        item['usages'] = impact.get('usages', [])
        enriched.append(item)
        counts[d['decision']] += 1
        confidence_counts[d.get('confidence', 'UNKNOWN')] += 1
        affected_files.update(u.get('sourceFile') for u in impact.get('usages', []) if u.get('sourceFile'))

    final_plan = {
        'schemaVersion': SCHEMA_VERSION,
        'repository': input_data.get('repository'),
        'pullRequest': input_data.get('pullRequest'),
        'baseSha': input_data.get('baseSha'),
        'headSha': input_data.get('headSha'),
        'analysisQuality': input_data.get('analysisQuality'),
        'summary': {
            'text': plan.get('summary', ''),
            'impactCount': len(enriched),
            'replaceCount': counts['REPLACE'],
            'rewriteCount': counts['REWRITE'],
            'noSourceChangeCount': counts['NO_SOURCE_CHANGE'],
            'rewriteUnitCount': len(plan.get('rewriteUnits', [])),
            'methodBodyRewriteCount': sum(1 for u in plan.get('rewriteUnits', []) if u.get('recipeKind') == 'REPLACE_METHOD_BODY'),
            'affectedSourceFileCount': len(affected_files),
            'confidenceCounts': dict(sorted(confidence_counts.items())),
        },
        'decisions': sorted(enriched, key=lambda x: x['impactId']),
        'rewriteUnits': sorted(plan.get('rewriteUnits', []), key=lambda x: x['unitId']),
    }
    write_json(output_dir / 'ai-migration-plan.json', final_plan)

    lines = [
        '# AI migration plan', '',
        f"- Impacts: {len(enriched)}",
        f"- Replacements: {counts['REPLACE']}",
        f"- Complex rewrites: {counts['REWRITE']}",
        f"- No source change: {counts['NO_SOURCE_CHANGE']}",
        f"- Rewrite units: {len(plan.get('rewriteUnits', []))}",
        f"- Method-body rewrites: {final_plan['summary']['methodBodyRewriteCount']}",
        '', '| Source target | Decision | Replacement | Units | Confidence |',
        '|---|---|---|---|---|',
    ]
    for d in sorted(enriched, key=lambda x: x['impactId']):
        source = d['target'].get('symbol', '').replace('|', '\\|')
        replacement = (d.get('replacement') or {}).get('symbol', '').replace('|', '\\|') or '-'
        units = ', '.join(d.get('rewriteUnitIds', [])) or '-'
        lines.append(f"| `{source}` | {d['decision']} | `{replacement}` | `{units}` | {d.get('confidence', '')} |")
    (output_dir / 'ai-migration-plan-summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def command_empty(args):
    input_data = read_json(Path(args.input))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        'schemaVersion': SCHEMA_VERSION,
        'repository': input_data.get('repository'),
        'pullRequest': input_data.get('pullRequest'),
        'baseSha': input_data.get('baseSha'),
        'headSha': input_data.get('headSha'),
        'analysisQuality': input_data.get('analysisQuality'),
        'summary': {
            'text': 'No source impacts detected.',
            'impactCount': 0,
            'replaceCount': 0,
            'rewriteCount': 0,
            'noSourceChangeCount': 0,
            'rewriteUnitCount': 0,
            'methodBodyRewriteCount': 0,
            'affectedSourceFileCount': 0,
            'confidenceCounts': {},
        },
        'decisions': [],
        'rewriteUnits': [],
    }
    write_json(output_dir / 'ai-migration-plan.json', plan)
    write_json(output_dir / 'ai-migration-validation.json', {'schemaVersion': SCHEMA_VERSION, 'valid': True, 'expectedImpactCount': 0, 'decisionCount': 0, 'rewriteUnitCount': 0, 'globalErrors': [], 'invalidImpacts': []})
    (output_dir / 'ai-migration-plan-summary.md').write_text('# AI migration plan\n\n- Impacts: 0\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('build')
    p.add_argument('--artifact-dir', required=True)
    p.add_argument('--output-dir', required=True)
    p.set_defaults(func=build)

    p = sub.add_parser('normalize')
    p.add_argument('--input', required=True)
    p.add_argument('--raw-plan', required=True)
    p.set_defaults(func=command_normalize)

    p = sub.add_parser('check')
    p.add_argument('--input', required=True)
    p.add_argument('--raw-plan', required=True)
    p.add_argument('--report', required=True)
    p.set_defaults(func=command_check)

    p = sub.add_parser('validate')
    p.add_argument('--input', required=True)
    p.add_argument('--raw-plan', required=True)
    p.add_argument('--output-dir', required=True)
    p.set_defaults(func=command_validate)

    p = sub.add_parser('empty-plan')
    p.add_argument('--input', required=True)
    p.add_argument('--output-dir', required=True)
    p.set_defaults(func=command_empty)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
