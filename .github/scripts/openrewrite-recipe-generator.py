#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

FQCN_RE = re.compile(r'^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+$')
PLACEHOLDER_RE = re.compile(r'#\{(p\d+|select)\}')


def fail(message):
    raise SystemExit(message)


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def yaml_scalar(value):
    return json.dumps(value, ensure_ascii=False)


def java_string(value):
    return json.dumps(value, ensure_ascii=False)


def normalize(plan):
    decisions = plan.get('decisions')
    units = plan.get('rewriteUnits')
    if not isinstance(decisions, list):
        fail('ai-migration-plan.json must contain decisions[]')
    if not isinstance(units, list):
        fail('ai-migration-plan.json must contain rewriteUnits[]')

    type_steps = []
    seen_package_steps = {}
    seen_impacts = set()
    for decision in decisions:
        impact_id = decision.get('impactId')
        if not impact_id or impact_id in seen_impacts:
            fail(f'duplicate or missing impactId {impact_id!r}')
        seen_impacts.add(impact_id)
        if decision.get('decision') != 'REPLACE':
            continue
        target = (decision.get('target') or {}).get('symbol')
        replacement = (decision.get('replacement') or {}).get('symbol')
        transformation = decision.get('transformation')
        if not target or not replacement or target == replacement:
            fail(f'{impact_id}: invalid replacement mapping')
        if not FQCN_RE.match(target) or not FQCN_RE.match(replacement):
            fail(f'{impact_id}: REPLACE mapping must contain fully qualified Java types')
        if transformation == 'CHANGE_TYPE':
            type_steps.append({
                'kind': 'CHANGE_TYPE',
                'impactId': impact_id,
                'oldType': target,
                'newType': replacement,
            })
        elif transformation == 'CHANGE_PACKAGE':
            old_simple = target.rsplit('.', 1)[-1]
            new_simple = replacement.rsplit('.', 1)[-1]
            if old_simple != new_simple:
                fail(f'{impact_id}: CHANGE_PACKAGE requires unchanged simple type name')
            old_package = target.rsplit('.', 1)[0]
            new_package = replacement.rsplit('.', 1)[0]
            key = (old_package, new_package)
            if key not in seen_package_steps:
                step = {
                    'kind': 'CHANGE_PACKAGE',
                    'oldPackage': old_package,
                    'newPackage': new_package,
                    'recursive': True,
                    'impactIds': [],
                }
                seen_package_steps[key] = step
                type_steps.append(step)
            seen_package_steps[key]['impactIds'].append(impact_id)
        else:
            fail(f'{impact_id}: REPLACE requires explicit CHANGE_TYPE or CHANGE_PACKAGE transformation')

    normalized_units = []
    seen_units = set()
    for unit in units:
        unit_id = unit.get('unitId')
        kind = unit.get('recipeKind')
        if not unit_id or unit_id in seen_units:
            fail(f'duplicate or missing rewrite unitId {unit_id!r}')
        seen_units.add(unit_id)
        if kind not in {'CHANGE_METHOD_NAME', 'INLINE_METHOD_CALLS', 'CUSTOM_EXPRESSION_TEMPLATE', 'REPLACE_METHOD_BODY'}:
            fail(f'{unit_id}: unsupported rewrite unit recipeKind {kind!r}')
        if not unit.get('methodPattern'):
            fail(f'{unit_id}: methodPattern is required')
        item = {
            'unitId': unit_id,
            'coversImpactIds': sorted(set(unit.get('coversImpactIds') or [])),
            'kind': kind,
            'methodPattern': unit['methodPattern'],
            'replacement': unit.get('replacement'),
            'newMethodName': unit.get('newMethodName'),
            'imports': sorted(set(unit.get('imports') or [])),
            'staticImports': sorted(set(unit.get('staticImports') or [])),
        }
        if kind == 'CHANGE_METHOD_NAME' and not item['newMethodName']:
            fail(f'{unit_id}: CHANGE_METHOD_NAME requires newMethodName')
        if kind in {'INLINE_METHOD_CALLS', 'CUSTOM_EXPRESSION_TEMPLATE', 'REPLACE_METHOD_BODY'} and not item['replacement']:
            fail(f'{unit_id}: {kind} requires replacement')
        normalized_units.append(item)

    normalized_units.sort(key=lambda x: x['unitId'])
    type_steps.sort(key=lambda x: (x['kind'], x.get('oldPackage', x.get('oldType', '')), x.get('newPackage', x.get('newType', ''))))
    return type_steps, normalized_units


def render_yaml(recipe_name, display_name, type_steps, units, custom_recipe_name=None):
    lines = [
        'type: specs.openrewrite.org/v1beta/recipe',
        f'name: {recipe_name}',
        f'displayName: {yaml_scalar(display_name)}',
        'description: "Generated deterministically from a validated AI migration plan."',
        'recipeList:',
    ]
    count = 0
    for step in type_steps:
        count += 1
        if step['kind'] == 'CHANGE_PACKAGE':
            lines += [
                '  - org.openrewrite.java.ChangePackage:',
                f"      oldPackageName: {yaml_scalar(step['oldPackage'])}",
                f"      newPackageName: {yaml_scalar(step['newPackage'])}",
                '      recursive: true',
            ]
        else:
            lines += [
                '  - org.openrewrite.java.ChangeType:',
                f"      oldFullyQualifiedTypeName: {yaml_scalar(step['oldType'])}",
                f"      newFullyQualifiedTypeName: {yaml_scalar(step['newType'])}",
            ]

    for unit in units:
        if unit['kind'] == 'CHANGE_METHOD_NAME':
            count += 1
            lines += [
                '  - org.openrewrite.java.ChangeMethodName:',
                f"      methodPattern: {yaml_scalar(unit['methodPattern'])}",
                f"      newMethodName: {yaml_scalar(unit['newMethodName'])}",
            ]
        elif unit['kind'] == 'INLINE_METHOD_CALLS':
            count += 1
            lines += [
                '  - org.openrewrite.java.InlineMethodCalls:',
                f"      methodPattern: {yaml_scalar(unit['methodPattern'])}",
                f"      replacement: {yaml_scalar(unit['replacement'])}",
            ]
            if unit['imports']:
                lines += ['      imports:'] + [f"        - {yaml_scalar(x)}" for x in unit['imports']]
            if unit['staticImports']:
                lines += ['      staticImports:'] + [f"        - {yaml_scalar(x)}" for x in unit['staticImports']]

    if custom_recipe_name:
        count += 1
        lines.append(f'  - {custom_recipe_name}')

    # Cleanup is part of the generated migration. It removes legacy imports left
    # behind by method-body rewrites after the source is again type-attributable.
    if count:
        lines.append('  - org.openrewrite.java.RemoveUnusedImports')
        count += 1
    else:
        lines.append('  []')
    return '\n'.join(lines) + '\n', count


def java_template_builder(replacement, imports, static_imports):
    # Convert our invocation placeholders to JavaTemplate typed placeholders.
    placeholders = PLACEHOLDER_RE.findall(replacement)
    template = PLACEHOLDER_RE.sub('#{any()}', replacement)
    builder = f'JavaTemplate.builder({java_string(template)}).contextSensitive()'
    if imports:
        builder += '.imports(' + ', '.join(java_string(x) for x in imports) + ')'
    if static_imports:
        builder += '.staticImports(' + ', '.join(java_string(x) for x in static_imports) + ')'
    builder += '.build()'
    parameters = []
    for placeholder in placeholders:
        if placeholder == 'select':
            parameters.append('m.getSelect()')
        else:
            parameters.append(f"m.getArguments().get({int(placeholder[1:])})")
    return builder, parameters


def custom_java_source(package, class_name, custom_expression_units, method_body_units):
    matcher_fields = []
    expr_blocks = []
    body_blocks = []

    for index, unit in enumerate(custom_expression_units):
        matcher_fields.append(f'            private final MethodMatcher EXPR_MATCHER_{index} = new MethodMatcher({java_string(unit["methodPattern"])});')
        builder, params = java_template_builder(unit['replacement'], unit['imports'], unit['staticImports'])
        add_imports = '\n'.join(f'                    maybeAddImport({java_string(value)});' for value in unit['imports'])
        add_static = '\n'.join(
            f'                    maybeAddImport({java_string(value.rsplit(".", 1)[0])}, {java_string(value.rsplit(".", 1)[1])});'
            for value in unit['staticImports'] if '.' in value
        )
        param_args = ', ' + ', '.join(params) if params else ''
        expr_blocks.append(f'''                if (EXPR_MATCHER_{index}.matches(m)) {{
{add_imports}
{add_static}
                    JavaTemplate template = {builder};
                    return template.apply(getCursor(), m.getCoordinates().replace(){param_args});
                }}''')

    for index, unit in enumerate(method_body_units):
        matcher_fields.append(f'            private final MethodMatcher BODY_MATCHER_{index} = new MethodMatcher({java_string(unit["methodPattern"])});')
        builder = f'JavaTemplate.builder({java_string(unit["replacement"])}).contextSensitive()'
        if unit['imports']:
            builder += '.imports(' + ', '.join(java_string(x) for x in unit['imports']) + ')'
        if unit['staticImports']:
            builder += '.staticImports(' + ', '.join(java_string(x) for x in unit['staticImports']) + ')'
        builder += '.build()'
        add_imports = '\n'.join(f'                    maybeAddImport({java_string(value)});' for value in unit['imports'])
        add_static = '\n'.join(
            f'                    maybeAddImport({java_string(value.rsplit(".", 1)[0])}, {java_string(value.rsplit(".", 1)[1])});'
            for value in unit['staticImports'] if '.' in value
        )
        body_blocks.append(f'''                if (BODY_MATCHER_{index}.matches(m.getMethodType())) {{
{add_imports}
{add_static}
                    JavaTemplate template = {builder};
                    J.MethodDeclaration rewritten = template.apply(updateCursor(m), m.getCoordinates().replaceBody());
                    return maybeAutoFormat(m, rewritten, ctx);
                }}''')

    fields = '\n'.join(matcher_fields)
    expressions = '\n'.join(expr_blocks)
    bodies = '\n'.join(body_blocks)
    expression_override = ''
    if custom_expression_units:
        expression_override = f'''
            @Override
            public J visitMethodInvocation(J.MethodInvocation method, ExecutionContext ctx) {{
                J visited = super.visitMethodInvocation(method, ctx);
                if (!(visited instanceof J.MethodInvocation)) {{
                    return visited;
                }}
                J.MethodInvocation m = (J.MethodInvocation) visited;
{expressions}
                return m;
            }}
'''
    body_override = ''
    if method_body_units:
        body_override = f'''
            @Override
            public J visitMethodDeclaration(J.MethodDeclaration method, ExecutionContext ctx) {{
                // Full-body rewrites take ownership of the original method body.
                // Match them before traversing children so nested expression recipes
                // cannot partially rewrite code that is about to be replaced wholesale.
                J.MethodDeclaration m = method;
{bodies}
                return super.visitMethodDeclaration(method, ctx);
            }}
'''

    return f'''package {package};

import org.openrewrite.ExecutionContext;
import org.openrewrite.Recipe;
import org.openrewrite.TreeVisitor;
import org.openrewrite.java.JavaTemplate;
import org.openrewrite.java.JavaVisitor;
import org.openrewrite.java.MethodMatcher;
import org.openrewrite.java.tree.J;

public class {class_name} extends Recipe {{
    @Override
    public String getDisplayName() {{
        return "Generated AI dependency migration";
    }}

    @Override
    public String getDescription() {{
        return "Applies validated expression and method-body migration templates.";
    }}

    @Override
    public TreeVisitor<?, ExecutionContext> getVisitor() {{
        return new JavaVisitor<ExecutionContext>() {{
{fields}
{expression_override}
{body_override}
        }};
    }}
}}
'''


def write_custom_module(outdir, units, recipe_fqcn):
    expressions = [u for u in units if u['kind'] == 'CUSTOM_EXPRESSION_TEMPLATE']
    bodies = [u for u in units if u['kind'] == 'REPLACE_METHOD_BODY']
    if not expressions and not bodies:
        return None
    package, class_name = recipe_fqcn.rsplit('.', 1)
    module = outdir / 'custom-recipe'
    src_dir = module / 'src/main/java' / Path(package.replace('.', '/'))
    src_dir.mkdir(parents=True, exist_ok=True)
    source = custom_java_source(package, class_name, expressions, bodies)
    (src_dir / f'{class_name}.java').write_text(source, encoding='utf-8')
    pom = '''<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.gepardec.renovate</groupId>
  <artifactId>generated-rewrite-recipe</artifactId>
  <version>1.0.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <rewrite.version>8.90.4</rewrite.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.openrewrite</groupId>
      <artifactId>rewrite-java</artifactId>
      <version>${rewrite.version}</version>
    </dependency>
  </dependencies>
</project>
'''
    (module / 'pom.xml').write_text(pom, encoding='utf-8')
    return 'com.gepardec.renovate:generated-rewrite-recipe:1.0.0'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--recipe', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--recipe-name', required=True)
    parser.add_argument('--display-name', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()

    plan_path = Path(args.plan)
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    plan = load_json(plan_path)
    type_steps, units = normalize(plan)

    custom_units = [u for u in units if u['kind'] in {'CUSTOM_EXPRESSION_TEMPLATE', 'REPLACE_METHOD_BODY'}]
    custom_fqcn = 'com.gepardec.renovate.generated.GeneratedAiRewriteRecipe' if custom_units else None
    coordinates = write_custom_module(outdir, units, custom_fqcn) if custom_fqcn else None
    recipe, step_count = render_yaml(args.recipe_name, args.display_name, type_steps, units, custom_fqcn)
    Path(args.recipe).write_text(recipe, encoding='utf-8')

    manifest = {
        'schemaVersion': 4,
        'generator': 'openrewrite-recipe-generator-v15.py',
        'input': {'path': plan_path.name, 'sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest()},
        'output': {'path': Path(args.recipe).name, 'sha256': hashlib.sha256(recipe.encode()).hexdigest(), 'recipeName': args.recipe_name},
        'customRecipe': {
            'present': bool(custom_units),
            'recipeClass': custom_fqcn,
            'artifactCoordinates': coordinates,
            'operationCount': len(custom_units),
        },
        'summary': {
            'generatedRecipeStepCount': step_count,
            'changePackageCount': sum(1 for x in type_steps if x['kind'] == 'CHANGE_PACKAGE'),
            'changeTypeCount': sum(1 for x in type_steps if x['kind'] == 'CHANGE_TYPE'),
            'changeMethodNameCount': sum(1 for x in units if x['kind'] == 'CHANGE_METHOD_NAME'),
            'inlineMethodCallsCount': sum(1 for x in units if x['kind'] == 'INLINE_METHOD_CALLS'),
            'customExpressionCount': sum(1 for x in units if x['kind'] == 'CUSTOM_EXPRESSION_TEMPLATE'),
            'methodBodyRewriteCount': sum(1 for x in units if x['kind'] == 'REPLACE_METHOD_BODY'),
            'removeUnusedImportsIncluded': bool(step_count),
        },
        'typeSteps': type_steps,
        'rewriteUnits': units,
    }
    Path(args.manifest).write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(manifest['summary'], sort_keys=True))


if __name__ == '__main__':
    main()
