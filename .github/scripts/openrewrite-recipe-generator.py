#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

FQCN_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+$")
PLACEHOLDER_RE = re.compile(r"#\{(p\d+|select)\}")


def fail(msg):
    raise SystemExit(msg)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def yaml_scalar(v):
    return json.dumps(v, ensure_ascii=False)


def package_candidates(change):
    old_parts = change["oldType"].split(".")
    new_parts = change["newType"].split(".")
    out = set()
    for old_len in range(1, len(old_parts)):
        old_suffix = old_parts[old_len:]
        for new_len in range(1, len(new_parts)):
            if old_suffix == new_parts[new_len:]:
                a = ".".join(old_parts[:old_len]); b = ".".join(new_parts[:new_len])
                if a != b:
                    out.add((a,b))
    return out


def optimize_type_changes(changes):
    candidate_to_indexes = defaultdict(set)
    for i,c in enumerate(changes):
        for cand in package_candidates(c):
            candidate_to_indexes[cand].add(i)
    remaining = set(range(len(changes)))
    pkg=[]
    while True:
        viable=[]
        for (a,b), idxs in candidate_to_indexes.items():
            covered=sorted(idxs & remaining)
            if len(covered) < 2: continue
            specificity=a.count('.')+b.count('.')+2
            viable.append((len(covered), specificity, a,b,covered))
        if not viable: break
        viable.sort(key=lambda x:(-x[0],-x[1],x[2],x[3]))
        _,_,a,b,covered=viable[0]
        pkg.append({"kind":"CHANGE_PACKAGE","oldPackage":a,"newPackage":b,"recursive":True,"derivedFrom":[changes[i] for i in covered]})
        remaining.difference_update(covered)
    exact=[{"kind":"CHANGE_TYPE",**changes[i]} for i in sorted(remaining)]
    pkg.sort(key=lambda x:(x['oldPackage'],x['newPackage']))
    exact.sort(key=lambda x:(x['oldType'],x['newType']))
    return pkg+exact


def normalize(plan):
    decisions=plan.get('decisions')
    if not isinstance(decisions,list): fail('ai-migration-plan.json must contain decisions')
    type_changes=[]; builtins=[]; custom=[]
    seen=set()
    for i,d in enumerate(decisions):
        decision=d.get('decision')
        if decision=='NO_SOURCE_CHANGE':
            continue
        ow=d.get('openRewrite') or {}
        kind=ow.get('recipeKind')
        impact=d.get('impactId',f'index-{i}')
        if impact in seen: fail(f'duplicate impactId {impact}')
        seen.add(impact)
        if decision=='REPLACE':
            target=(d.get('target') or {}).get('symbol')
            repl=(d.get('replacement') or {}).get('symbol')
            if not target or not repl or target==repl: fail(f'{impact}: invalid replacement mapping')
            if not FQCN_RE.match(target) or not FQCN_RE.match(repl): fail(f'{impact}: replacement is not a type mapping')
            type_changes.append({'impactId':impact,'oldType':target,'newType':repl})
            continue
        if decision!='REWRITE': fail(f'{impact}: unsupported decision {decision!r}')
        if kind=='CHANGE_METHOD_NAME':
            if not ow.get('methodPattern') or not ow.get('newMethodName'): fail(f'{impact}: CHANGE_METHOD_NAME incomplete')
            builtins.append({'impactId':impact,'kind':kind,'methodPattern':ow['methodPattern'],'newMethodName':ow['newMethodName']})
        elif kind=='INLINE_METHOD_CALLS':
            if not ow.get('methodPattern') or not ow.get('replacement'): fail(f'{impact}: INLINE_METHOD_CALLS incomplete')
            builtins.append({'impactId':impact,'kind':kind,'methodPattern':ow['methodPattern'],'replacement':ow['replacement'],'imports':sorted(set(ow.get('imports') or [])),'staticImports':sorted(set(ow.get('staticImports') or []))})
        elif kind=='CUSTOM_JAVA_TEMPLATE':
            if not ow.get('methodPattern') or not ow.get('replacement'): fail(f'{impact}: CUSTOM_JAVA_TEMPLATE incomplete')
            custom.append({'impactId':impact,'kind':kind,'methodPattern':ow['methodPattern'],'replacement':ow['replacement'],'imports':sorted(set(ow.get('imports') or [])),'staticImports':sorted(set(ow.get('staticImports') or []))})
        else:
            fail(f'{impact}: REWRITE has unsupported openRewrite.recipeKind {kind!r}')
    return optimize_type_changes(type_changes), builtins, custom


def render_yaml(recipe_name, display_name, type_steps, builtins, custom_recipe_name=None):
    lines=['type: specs.openrewrite.org/v1beta/recipe',f'name: {recipe_name}',f'displayName: {yaml_scalar(display_name)}','description: "Generated from a validated AI migration plan."','recipeList:']
    count=0
    for s in type_steps:
        count+=1
        if s['kind']=='CHANGE_PACKAGE':
            lines += ['  - org.openrewrite.java.ChangePackage:',f"      oldPackageName: {yaml_scalar(s['oldPackage'])}",f"      newPackageName: {yaml_scalar(s['newPackage'])}",'      recursive: true']
        else:
            lines += ['  - org.openrewrite.java.ChangeType:',f"      oldFullyQualifiedTypeName: {yaml_scalar(s['oldType'])}",f"      newFullyQualifiedTypeName: {yaml_scalar(s['newType'])}"]
    for s in builtins:
        count+=1
        if s['kind']=='CHANGE_METHOD_NAME':
            lines += ['  - org.openrewrite.java.ChangeMethodName:',f"      methodPattern: {yaml_scalar(s['methodPattern'])}",f"      newMethodName: {yaml_scalar(s['newMethodName'])}"]
        elif s['kind']=='INLINE_METHOD_CALLS':
            lines += ['  - org.openrewrite.java.InlineMethodCalls:',f"      methodPattern: {yaml_scalar(s['methodPattern'])}",f"      replacement: {yaml_scalar(s['replacement'])}"]
            if s['imports']:
                lines += ['      imports:']+[f"        - {yaml_scalar(x)}" for x in s['imports']]
            if s['staticImports']:
                lines += ['      staticImports:']+[f"        - {yaml_scalar(x)}" for x in s['staticImports']]
    if custom_recipe_name:
        count+=1
        lines.append(f'  - {custom_recipe_name}')
    if count==0: lines.append('  []')
    return '\n'.join(lines)+'\n', count


def java_string(s):
    return json.dumps(s)


def custom_java_source(package, class_name, custom):
    blocks=[]
    for idx,op in enumerate(custom):
        repl=op['replacement']
        placeholders=PLACEHOLDER_RE.findall(repl)
        template=PLACEHOLDER_RE.sub('#{any()}',repl)
        params=[]
        for ph in placeholders:
            if ph=='select': params.append('m.getSelect()')
            else: params.append(f"m.getArguments().get({int(ph[1:])})")
        imports=op['imports']; statics=op['staticImports']
        add_imports='\n'.join(f'                    maybeAddImport({java_string(x)});' for x in imports)
        add_statics='\n'.join(f'                    maybeAddImport({java_string(x.rsplit(".",1)[0])}, {java_string(x.rsplit(".",1)[1])});' for x in statics if '.' in x)
        builder=f'JavaTemplate.builder({java_string(template)}).contextSensitive()'
        if imports:
            builder += '.imports(' + ', '.join(java_string(x) for x in imports) + ')'
        if statics:
            builder += '.staticImports(' + ', '.join(java_string(x) for x in statics) + ')'
        builder += '.build()'
        param_args = (', ' + ', '.join(params)) if params else ''
        blocks.append(f'''                if (MATCHER_{idx}.matches(m)) {{\n{add_imports}\n{add_statics}\n                    JavaTemplate t = {builder};\n                    return t.apply(getCursor(), m.getCoordinates().replace(){param_args});\n                }}''')
    matchers='\n'.join(f'            private final MethodMatcher MATCHER_{i} = new MethodMatcher({java_string(op["methodPattern"])});' for i,op in enumerate(custom))
    body='\n'.join(blocks)
    return f'''package {package};\n\nimport org.openrewrite.ExecutionContext;\nimport org.openrewrite.Recipe;\nimport org.openrewrite.TreeVisitor;\nimport org.openrewrite.java.JavaIsoVisitor;\nimport org.openrewrite.java.JavaTemplate;\nimport org.openrewrite.java.MethodMatcher;\nimport org.openrewrite.java.tree.J;\n\npublic class {class_name} extends Recipe {{\n    @Override public String getDisplayName() {{ return "Generated AI dependency migration"; }}\n    @Override public String getDescription() {{ return "Applies validated invocation-level migration templates."; }}\n    @Override public TreeVisitor<?, ExecutionContext> getVisitor() {{\n        return new JavaIsoVisitor<ExecutionContext>() {{\n{matchers}\n            @Override public J.MethodInvocation visitMethodInvocation(J.MethodInvocation method, ExecutionContext ctx) {{\n                J.MethodInvocation m = super.visitMethodInvocation(method, ctx);\n{body}\n                return m;\n            }}\n        }};\n    }}\n}}\n'''


def write_custom_module(outdir, custom, recipe_fqcn):
    if not custom: return None
    package, cls=recipe_fqcn.rsplit('.',1)
    module=outdir/'custom-recipe'
    src=module/'src/main/java'/Path(package.replace('.','/'))
    src.mkdir(parents=True,exist_ok=True)
    (src/f'{cls}.java').write_text(custom_java_source(package,cls,custom),encoding='utf-8')
    pom='''<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>com.gepardec.renovate</groupId>\n  <artifactId>generated-rewrite-recipe</artifactId>\n  <version>1.0.0</version>\n  <properties><maven.compiler.release>17</maven.compiler.release><project.build.sourceEncoding>UTF-8</project.build.sourceEncoding><rewrite.version>8.91.1</rewrite.version></properties>\n  <dependencies><dependency><groupId>org.openrewrite</groupId><artifactId>rewrite-java</artifactId><version>${rewrite.version}</version></dependency></dependencies>\n</project>\n'''
    (module/'pom.xml').write_text(pom,encoding='utf-8')
    return 'com.gepardec.renovate:generated-rewrite-recipe:1.0.0'


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--plan',required=True); ap.add_argument('--recipe',required=True); ap.add_argument('--manifest',required=True); ap.add_argument('--recipe-name',required=True); ap.add_argument('--display-name',required=True); ap.add_argument('--output-dir',required=True)
    a=ap.parse_args(); plan=load_json(a.plan); outdir=Path(a.output_dir); outdir.mkdir(parents=True,exist_ok=True)
    type_steps,builtins,custom=normalize(plan)
    custom_fqcn='com.gepardec.renovate.generated.GeneratedAiRewriteRecipe' if custom else None
    coords=write_custom_module(outdir,custom,custom_fqcn) if custom else None
    recipe, count=render_yaml(a.recipe_name,a.display_name,type_steps,builtins,custom_fqcn)
    Path(a.recipe).write_text(recipe,encoding='utf-8')
    manifest={
      'schemaVersion':3,'generator':'openrewrite-recipe-generator-v3.py',
      'input':{'path':Path(a.plan).name,'sha256':hashlib.sha256(Path(a.plan).read_bytes()).hexdigest()},
      'output':{'path':Path(a.recipe).name,'sha256':hashlib.sha256(recipe.encode()).hexdigest(),'recipeName':a.recipe_name},
      'customRecipe':{'present':bool(custom),'recipeClass':custom_fqcn,'artifactCoordinates':coords,'operationCount':len(custom)},
      'summary':{'generatedRecipeStepCount':count,'changePackageCount':sum(1 for x in type_steps if x['kind']=='CHANGE_PACKAGE'),'changeTypeCount':sum(1 for x in type_steps if x['kind']=='CHANGE_TYPE'),'builtinRewriteCount':len(builtins),'customJavaTemplateCount':len(custom)},
      'typeSteps':type_steps,'builtinRewrites':builtins,'customRewrites':custom
    }
    Path(a.manifest).write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print(json.dumps(manifest['summary'],sort_keys=True))
if __name__=='__main__': main()
