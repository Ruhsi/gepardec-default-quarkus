import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.IdentifierTree;
import com.sun.source.tree.ImportTree;
import com.sun.source.tree.LambdaExpressionTree;
import com.sun.source.tree.MemberReferenceTree;
import com.sun.source.tree.MemberSelectTree;
import com.sun.source.tree.MethodInvocationTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.NewClassTree;
import com.sun.source.tree.Tree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePath;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;

import javax.lang.model.element.Element;
import javax.lang.model.element.ElementKind;
import javax.lang.model.element.ExecutableElement;
import javax.lang.model.element.Modifier;
import javax.lang.model.element.TypeElement;
import javax.lang.model.element.VariableElement;
import javax.lang.model.type.ArrayType;
import javax.lang.model.type.DeclaredType;
import javax.lang.model.type.NoType;
import javax.lang.model.type.PrimitiveType;
import javax.lang.model.type.TypeKind;
import javax.lang.model.type.TypeMirror;
import javax.lang.model.type.TypeVariable;
import javax.lang.model.type.WildcardType;
import javax.lang.model.util.Elements;
import javax.lang.model.util.Types;
import javax.tools.Diagnostic;
import javax.tools.DiagnosticCollector;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.StandardLocation;
import javax.tools.ToolProvider;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collection;
import java.util.Collections;
import java.util.Comparator;
import java.util.EnumMap;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.TreeMap;
import java.util.stream.Collectors;

/**
 * Source-only Java API usage mapper.
 *
 * <p>The analyzer parses and attributes project Java source through the public
 * JDK compiler APIs. It does not inspect method bytecode or dependency method
 * bodies. API targets are resolved against the supplied old dependency
 * classpath so removed members can still be matched precisely.</p>
 */
public final class JavaApiUsageAnalyzer {
    private static final char LIST_SEPARATOR = '\u001f';
    private static final Base64.Encoder BASE64_ENCODER = Base64.getUrlEncoder().withoutPadding();
    private static final Base64.Decoder BASE64_DECODER = Base64.getUrlDecoder();

    private JavaApiUsageAnalyzer() {
    }

    public static void main(String[] args) throws Exception {
        Map<String, String> arguments = parseArguments(args);

        Path repositoryRoot = requiredPath(arguments, "repo-root").toAbsolutePath().normalize();
        Path sourceListFile = requiredPath(arguments, "sources");
        Path classpathListFile = requiredPath(arguments, "classpath");
        Path targetFile = requiredPath(arguments, "targets");
        Path usageOutput = requiredPath(arguments, "usage-output");
        Path resolutionOutput = requiredPath(arguments, "resolution-output");
        Path diagnosticOutput = requiredPath(arguments, "diagnostic-output");
        Path metricsOutput = requiredPath(arguments, "metrics-output");
        String javaRelease = arguments.getOrDefault("release", "17").trim();

        List<Path> sourceFiles = readPathList(sourceListFile);
        List<Path> classpathEntries = readPathList(classpathListFile);
        List<Target> targets = readTargets(targetFile);

        Files.createDirectories(parentOf(usageOutput));
        Files.createDirectories(parentOf(resolutionOutput));
        Files.createDirectories(parentOf(diagnosticOutput));
        Files.createDirectories(parentOf(metricsOutput));

        AnalysisResult result = analyze(
                repositoryRoot,
                sourceFiles,
                classpathEntries,
                targets,
                javaRelease
        );

        writeUsages(usageOutput, result.usages);
        writeResolutions(resolutionOutput, targets, result.resolutions, result.usages);
        writeDiagnostics(diagnosticOutput, repositoryRoot, result.diagnostics);
        writeMetrics(metricsOutput, result);
    }

    private static AnalysisResult analyze(
            Path repositoryRoot,
            List<Path> sourceFiles,
            List<Path> classpathEntries,
            List<Target> targets,
            String javaRelease
    ) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            throw new IllegalStateException("No system Java compiler is available. Run with a JDK, not a JRE.");
        }

        DiagnosticCollector<JavaFileObject> diagnostics = new DiagnosticCollector<>();
        List<CompilationUnitTree> compilationUnits = new ArrayList<>();
        List<Usage> usages = new ArrayList<>();
        Map<String, TargetResolution> resolutions = new LinkedHashMap<>();
        ScanMetrics scanMetrics = new ScanMetrics();

        try (StandardJavaFileManager fileManager = compiler.getStandardFileManager(
                diagnostics,
                Locale.ROOT,
                StandardCharsets.UTF_8
        )) {
            if (!classpathEntries.isEmpty()) {
                fileManager.setLocationFromPaths(StandardLocation.CLASS_PATH, classpathEntries);
            }

            Iterable<? extends JavaFileObject> compilationInput =
                    fileManager.getJavaFileObjectsFromPaths(sourceFiles);

            List<String> compilerOptions = new ArrayList<>();
            compilerOptions.add("-proc:none");
            compilerOptions.add("-implicit:none");
            compilerOptions.add("-Xlint:none");
            compilerOptions.add("-encoding");
            compilerOptions.add("UTF-8");
            if (!javaRelease.isEmpty()) {
                compilerOptions.add("--release");
                compilerOptions.add(javaRelease);
            }

            JavacTask task = (JavacTask) compiler.getTask(
                    null,
                    fileManager,
                    diagnostics,
                    compilerOptions,
                    null,
                    compilationInput
            );

            for (CompilationUnitTree unit : task.parse()) {
                compilationUnits.add(unit);
            }

            // Ordinary source errors are collected as diagnostics. A compiler crash or
            // invalid compiler configuration is treated as a technical analysis failure.
            task.analyze();

            Trees trees = Trees.instance(task);
            Elements elements = task.getElements();
            Types types = task.getTypes();

            for (Target target : targets) {
                resolutions.put(target.id, resolveTarget(target, elements, types));
            }

            Map<Path, String> sourceContentCache = new HashMap<>();
            Set<String> usageKeys = new HashSet<>();

            for (CompilationUnitTree unit : compilationUnits) {
                Path sourcePath = sourcePathOf(unit);
                if (sourcePath == null) {
                    continue;
                }
                sourcePath = sourcePath.toAbsolutePath().normalize();
                String sourceContent = sourceContentCache.computeIfAbsent(
                        sourcePath,
                        path -> readTextUnchecked(path)
                );

                UsageScanner scanner = new UsageScanner(
                        repositoryRoot,
                        unit,
                        sourcePath,
                        sourceContent,
                        trees,
                        elements,
                        types,
                        targets,
                        resolutions,
                        usages,
                        usageKeys,
                        scanMetrics
                );
                scanner.scan(unit, null);
            }
        }

        usages.sort(Comparator
                .comparing((Usage usage) -> usage.sourceFile)
                .thenComparingLong(usage -> usage.startPosition)
                .thenComparing(usage -> usage.targetId)
                .thenComparing(usage -> usage.usageKind)
                .thenComparing(usage -> usage.resolvedSymbol));

        List<Diagnostic<? extends JavaFileObject>> sortedDiagnostics =
                new ArrayList<>(diagnostics.getDiagnostics());
        sortedDiagnostics.sort(Comparator
                .comparing((Diagnostic<? extends JavaFileObject> diagnostic) -> diagnosticSource(diagnostic))
                .thenComparingLong(Diagnostic::getLineNumber)
                .thenComparingLong(Diagnostic::getColumnNumber)
                .thenComparing(diagnostic -> diagnostic.getKind().name())
                .thenComparing(diagnostic -> diagnostic.getCode() == null ? "" : diagnostic.getCode()));

        return new AnalysisResult(
                sourceFiles.size(),
                classpathEntries.size(),
                compilationUnits.size(),
                usages,
                resolutions,
                sortedDiagnostics,
                scanMetrics
        );
    }

    private static TargetResolution resolveTarget(Target target, Elements elements, Types types) {
        TypeElement owner = findTypeElement(elements, target.className);
        if (owner == null) {
            return TargetResolution.unresolved("Target type is not present on the old dependency classpath");
        }

        if (target.kind.equals("CLASS")) {
            return TargetResolution.resolved(
                    List.of(binaryName(elements, owner)),
                    List.of(owner)
            );
        }

        List<Element> matchingElements = new ArrayList<>();
        for (Element enclosed : owner.getEnclosedElements()) {
            if (target.kind.equals("FIELD")) {
                if ((enclosed.getKind().isField() || enclosed.getKind() == ElementKind.ENUM_CONSTANT)
                        && enclosed.getSimpleName().contentEquals(target.name)) {
                    matchingElements.add(enclosed);
                }
                continue;
            }

            if (!(enclosed instanceof ExecutableElement executable)) {
                continue;
            }

            if (target.kind.equals("CONSTRUCTOR")) {
                if (executable.getKind() != ElementKind.CONSTRUCTOR) {
                    continue;
                }
            } else if (target.kind.equals("METHOD")) {
                if (executable.getKind() != ElementKind.METHOD
                        || !executable.getSimpleName().contentEquals(target.name)) {
                    continue;
                }
            } else {
                continue;
            }

            List<String> parameters = parameterTypes(executable, elements, types);
            if (parameters.equals(target.parameters)) {
                matchingElements.add(executable);
            }
        }

        if (matchingElements.isEmpty()) {
            return TargetResolution.unresolved(
                    "Target member was not found on the resolved old target type"
            );
        }

        List<String> resolvedSymbols = matchingElements.stream()
                .map(element -> symbolOf(element, elements, types))
                .distinct()
                .sorted()
                .toList();
        return TargetResolution.resolved(resolvedSymbols, matchingElements);
    }

    private static final class UsageScanner extends TreePathScanner<Void, Void> {
        private final Path repositoryRoot;
        private final CompilationUnitTree compilationUnit;
        private final Path sourcePath;
        private final String sourceContent;
        private final Trees trees;
        private final Elements elements;
        private final Types types;
        private final List<Target> targets;
        private final Map<String, TargetResolution> resolutions;
        private final List<Usage> usages;
        private final Set<String> usageKeys;
        private final ScanMetrics metrics;
        private final SourcePositions sourcePositions;

        private UsageScanner(
                Path repositoryRoot,
                CompilationUnitTree compilationUnit,
                Path sourcePath,
                String sourceContent,
                Trees trees,
                Elements elements,
                Types types,
                List<Target> targets,
                Map<String, TargetResolution> resolutions,
                List<Usage> usages,
                Set<String> usageKeys,
                ScanMetrics metrics
        ) {
            this.repositoryRoot = repositoryRoot;
            this.compilationUnit = compilationUnit;
            this.sourcePath = sourcePath;
            this.sourceContent = sourceContent;
            this.trees = trees;
            this.elements = elements;
            this.types = types;
            this.targets = targets;
            this.resolutions = resolutions;
            this.usages = usages;
            this.usageKeys = usageKeys;
            this.metrics = metrics;
            this.sourcePositions = trees.getSourcePositions();
        }

        @Override
        public Void visitMethodInvocation(MethodInvocationTree tree, Void unused) {
            Element element = trees.getElement(getCurrentPath());
            if (element instanceof ExecutableElement executable) {
                if (executable.getKind() == ElementKind.CONSTRUCTOR) {
                    matchExecutable(tree, executable, "CONSTRUCTOR_INVOCATION");
                } else {
                    matchExecutable(tree, executable, "METHOD_INVOCATION");
                }
            } else {
                metrics.unresolvedMethodInvocations++;
            }
            return super.visitMethodInvocation(tree, unused);
        }

        @Override
        public Void visitNewClass(NewClassTree tree, Void unused) {
            Element element = trees.getElement(getCurrentPath());
            if (element instanceof ExecutableElement executable) {
                matchExecutable(tree, executable, "CONSTRUCTOR_CALL");
            } else {
                metrics.unresolvedConstructorCalls++;
            }
            return super.visitNewClass(tree, unused);
        }

        @Override
        public Void visitMemberReference(MemberReferenceTree tree, Void unused) {
            Element element = trees.getElement(getCurrentPath());
            if (element instanceof ExecutableElement executable) {
                String usageKind = executable.getKind() == ElementKind.CONSTRUCTOR
                        ? "CONSTRUCTOR_REFERENCE"
                        : "METHOD_REFERENCE";
                matchExecutable(tree, executable, usageKind);
            } else {
                metrics.unresolvedMemberReferences++;
            }
            return super.visitMemberReference(tree, unused);
        }

        @Override
        public Void visitLambdaExpression(LambdaExpressionTree tree, Void unused) {
            TypeMirror lambdaType = trees.getTypeMirror(getCurrentPath());
            Element lambdaElement = lambdaType == null ? null : types.asElement(lambdaType);
            if (lambdaElement instanceof TypeElement functionalType) {
                for (Element member : elements.getAllMembers(functionalType)) {
                    if (!(member instanceof ExecutableElement executable)
                            || executable.getKind() != ElementKind.METHOD
                            || !executable.getModifiers().contains(Modifier.ABSTRACT)
                            || executable.getModifiers().contains(Modifier.STATIC)) {
                        continue;
                    }
                    for (Target target : targets) {
                        if (!target.kind.equals("METHOD")) {
                            continue;
                        }
                        Match match = executableMatch(
                                executable,
                                target,
                                resolutions.get(target.id)
                        );
                        if (match.matched) {
                            recordUsage(
                                    target,
                                    tree,
                                    "LAMBDA_IMPLEMENTATION",
                                    match.matchKind,
                                    "HIGH",
                                    symbolOf(executable, elements, types)
                            );
                        }
                    }
                }
            } else {
                metrics.unresolvedLambdaTypes++;
            }
            return super.visitLambdaExpression(tree, unused);
        }

        @Override
        public Void visitMethod(MethodTree tree, Void unused) {
            Element element = trees.getElement(getCurrentPath());
            if (element instanceof ExecutableElement executable
                    && executable.getKind() == ElementKind.METHOD) {
                TypeElement candidateOwner = enclosingType(executable);
                if (candidateOwner != null) {
                    for (Target target : targets) {
                        if (!target.kind.equals("METHOD")) {
                            continue;
                        }
                        TargetResolution resolution = resolutions.get(target.id);
                        if (resolution == null || !resolution.resolved) {
                            continue;
                        }
                        for (Element resolvedElement : resolution.elements) {
                            if (!(resolvedElement instanceof ExecutableElement targetMethod)) {
                                continue;
                            }
                            if (sameExecutable(executable, target, elements, types)) {
                                // The project source itself should not declare a dependency API
                                // owner. If it does, this is a declaration rather than a usage.
                                continue;
                            }
                            if (overrides(executable, targetMethod, candidateOwner, elements)) {
                                recordUsage(
                                        target,
                                        tree,
                                        "METHOD_OVERRIDE",
                                        "OVERRIDE",
                                        "HIGH",
                                        symbolOf(executable, elements, types)
                                );
                                break;
                            }
                        }
                    }
                }
            }
            return super.visitMethod(tree, unused);
        }

        @Override
        public Void visitMemberSelect(MemberSelectTree tree, Void unused) {
            Element element = trees.getElement(getCurrentPath());
            if (element instanceof VariableElement variable) {
                matchField(tree, variable);
            } else if (element instanceof TypeElement typeElement) {
                matchType(tree, typeElement);
            }
            return super.visitMemberSelect(tree, unused);
        }

        @Override
        public Void visitIdentifier(IdentifierTree tree, Void unused) {
            Element element = trees.getElement(getCurrentPath());
            if (element instanceof VariableElement variable) {
                matchField(tree, variable);
            } else if (element instanceof TypeElement typeElement) {
                matchType(tree, typeElement);
            }
            return super.visitIdentifier(tree, unused);
        }

        private void matchExecutable(Tree tree, ExecutableElement executable, String usageKind) {
            for (Target target : targets) {
                boolean constructorTarget = target.kind.equals("CONSTRUCTOR");
                boolean methodTarget = target.kind.equals("METHOD");
                if (!constructorTarget && !methodTarget) {
                    continue;
                }
                if (constructorTarget && executable.getKind() != ElementKind.CONSTRUCTOR) {
                    continue;
                }
                if (methodTarget && executable.getKind() != ElementKind.METHOD) {
                    continue;
                }

                Match match = executableMatch(executable, target, resolutions.get(target.id));
                if (match.matched) {
                    recordUsage(
                            target,
                            tree,
                            usageKind,
                            match.matchKind,
                            "HIGH",
                            symbolOf(executable, elements, types)
                    );
                }
            }
        }

        private Match executableMatch(
                ExecutableElement candidate,
                Target target,
                TargetResolution resolution
        ) {
            if (sameExecutable(candidate, target, elements, types)) {
                return Match.exact();
            }

            if (!target.kind.equals("METHOD") || resolution == null || !resolution.resolved) {
                return Match.none();
            }

            TypeElement candidateOwner = enclosingType(candidate);
            if (candidateOwner == null) {
                return Match.none();
            }

            for (Element element : resolution.elements) {
                if (!(element instanceof ExecutableElement targetMethod)) {
                    continue;
                }
                if (overrides(candidate, targetMethod, candidateOwner, elements)) {
                    return Match.override();
                }
            }
            return Match.none();
        }

        private void matchField(Tree tree, VariableElement variable) {
            TypeElement owner = enclosingType(variable);
            if (owner == null) {
                return;
            }
            String ownerName = binaryName(elements, owner);
            String fieldName = variable.getSimpleName().toString();

            for (Target target : targets) {
                if (!target.kind.equals("FIELD")) {
                    continue;
                }
                if (target.className.equals(ownerName) && target.name.equals(fieldName)) {
                    recordUsage(
                            target,
                            tree,
                            "FIELD_ACCESS",
                            "EXACT_SYMBOL",
                            "HIGH",
                            symbolOf(variable, elements, types)
                    );
                }
            }
        }

        private void matchType(Tree tree, TypeElement typeElement) {
            String resolvedType = binaryName(elements, typeElement);
            for (Target target : targets) {
                if (!target.kind.equals("CLASS") || !target.className.equals(resolvedType)) {
                    continue;
                }

                String usageKind = isInsideImport() ? "TYPE_IMPORT" : "TYPE_REFERENCE";
                String confidence = target.changeStatus.equals("REMOVED") ? "HIGH" : "MEDIUM";
                recordUsage(
                        target,
                        tree,
                        usageKind,
                        "TYPE_MATCH",
                        confidence,
                        resolvedType
                );
            }
        }

        private boolean isInsideImport() {
            TreePath parent = getCurrentPath() == null ? null : getCurrentPath().getParentPath();
            while (parent != null) {
                if (parent.getLeaf() instanceof ImportTree) {
                    return true;
                }
                if (parent.getLeaf() instanceof CompilationUnitTree) {
                    return false;
                }
                parent = parent.getParentPath();
            }
            return false;
        }

        private void recordUsage(
                Target target,
                Tree tree,
                String usageKind,
                String matchKind,
                String confidence,
                String resolvedSymbol
        ) {
            long start = sourcePositions.getStartPosition(compilationUnit, tree);
            long end = sourcePositions.getEndPosition(compilationUnit, tree);
            if (start == Diagnostic.NOPOS) {
                start = -1;
            }
            if (end == Diagnostic.NOPOS) {
                end = start;
            }

            String relativePath = relativize(repositoryRoot, sourcePath);
            String deduplicationKey = target.id + "\u0000" + relativePath + "\u0000"
                    + start + "\u0000" + usageKind + "\u0000" + resolvedSymbol;
            if (!usageKeys.add(deduplicationKey)) {
                return;
            }

            long line = start >= 0 && compilationUnit.getLineMap() != null
                    ? compilationUnit.getLineMap().getLineNumber(start)
                    : -1;
            long column = start >= 0 && compilationUnit.getLineMap() != null
                    ? compilationUnit.getLineMap().getColumnNumber(start)
                    : -1;
            long endLine = end >= 0 && compilationUnit.getLineMap() != null
                    ? compilationUnit.getLineMap().getLineNumber(Math.max(start, end - 1))
                    : line;
            long endColumn = end >= 0 && compilationUnit.getLineMap() != null
                    ? compilationUnit.getLineMap().getColumnNumber(Math.max(start, end - 1)) + 1
                    : column;

            String expression = slice(sourceContent, start, end, 1000);
            String lineText = lineText(sourceContent, line, 2000);
            String enclosingType = enclosingTypeName();
            String enclosingMethod = enclosingMethodName();

            usages.add(new Usage(
                    target.id,
                    usageKind,
                    matchKind,
                    confidence,
                    sourceSetOf(relativePath),
                    relativePath,
                    modulePathOf(relativePath),
                    line,
                    column,
                    endLine,
                    endColumn,
                    enclosingType,
                    enclosingMethod,
                    resolvedSymbol,
                    expression,
                    lineText,
                    start,
                    end
            ));
        }

        private String enclosingTypeName() {
            TreePath path = getCurrentPath();
            while (path != null) {
                if (path.getLeaf() instanceof ClassTree) {
                    Element element = trees.getElement(path);
                    if (element instanceof TypeElement typeElement) {
                        return binaryName(elements, typeElement);
                    }
                }
                path = path.getParentPath();
            }
            return "";
        }

        private String enclosingMethodName() {
            TreePath path = getCurrentPath();
            while (path != null) {
                if (path.getLeaf() instanceof MethodTree) {
                    Element element = trees.getElement(path);
                    if (element instanceof ExecutableElement executable) {
                        return symbolOf(executable, elements, types);
                    }
                }
                path = path.getParentPath();
            }
            return "";
        }
    }

    private static boolean sameExecutable(
            ExecutableElement candidate,
            Target target,
            Elements elements,
            Types types
    ) {
        TypeElement owner = enclosingType(candidate);
        if (owner == null || !binaryName(elements, owner).equals(target.className)) {
            return false;
        }

        if (target.kind.equals("CONSTRUCTOR")) {
            if (candidate.getKind() != ElementKind.CONSTRUCTOR) {
                return false;
            }
        } else {
            if (candidate.getKind() != ElementKind.METHOD
                    || !candidate.getSimpleName().contentEquals(target.name)) {
                return false;
            }
        }

        return parameterTypes(candidate, elements, types).equals(target.parameters);
    }

    private static boolean overrides(
            ExecutableElement candidate,
            ExecutableElement target,
            TypeElement candidateOwner,
            Elements elements
    ) {
        try {
            return elements.overrides(candidate, target, candidateOwner);
        } catch (IllegalArgumentException ignored) {
            return false;
        }
    }

    private static List<String> parameterTypes(
            ExecutableElement executable,
            Elements elements,
            Types types
    ) {
        List<String> parameters = new ArrayList<>();
        for (VariableElement parameter : executable.getParameters()) {
            parameters.add(binaryTypeName(parameter.asType(), elements, types));
        }
        return parameters;
    }

    private static String binaryTypeName(TypeMirror original, Elements elements, Types types) {
        TypeMirror type = original;
        try {
            type = types.erasure(original);
        } catch (IllegalArgumentException ignored) {
            // Error types and partially attributed trees can reject erasure.
        }

        if (type instanceof ArrayType arrayType) {
            return binaryTypeName(arrayType.getComponentType(), elements, types) + "[]";
        }
        if (type instanceof PrimitiveType || type instanceof NoType) {
            return type.toString();
        }
        if (type instanceof DeclaredType declaredType) {
            Element element = declaredType.asElement();
            if (element instanceof TypeElement typeElement) {
                return binaryName(elements, typeElement);
            }
        }
        if (type instanceof TypeVariable typeVariable) {
            return binaryTypeName(typeVariable.getUpperBound(), elements, types);
        }
        if (type instanceof WildcardType wildcardType) {
            TypeMirror bound = wildcardType.getExtendsBound();
            if (bound == null) {
                bound = wildcardType.getSuperBound();
            }
            return bound == null ? "java.lang.Object" : binaryTypeName(bound, elements, types);
        }
        return type.toString().replace('.', '.');
    }

    private static String symbolOf(Element element, Elements elements, Types types) {
        if (element instanceof ExecutableElement executable) {
            TypeElement owner = enclosingType(executable);
            String ownerName = owner == null ? "" : binaryName(elements, owner);
            String memberName = executable.getKind() == ElementKind.CONSTRUCTOR
                    ? "<init>"
                    : executable.getSimpleName().toString();
            return ownerName + "#" + memberName + "("
                    + String.join(",", parameterTypes(executable, elements, types)) + ")";
        }
        if (element instanceof VariableElement variable) {
            TypeElement owner = enclosingType(variable);
            String ownerName = owner == null ? "" : binaryName(elements, owner);
            return ownerName + "#" + variable.getSimpleName();
        }
        if (element instanceof TypeElement typeElement) {
            return binaryName(elements, typeElement);
        }
        return element.toString();
    }

    private static TypeElement enclosingType(Element element) {
        Element current = element == null ? null : element.getEnclosingElement();
        while (current != null) {
            if (current instanceof TypeElement typeElement) {
                return typeElement;
            }
            current = current.getEnclosingElement();
        }
        return null;
    }

    private static TypeElement findTypeElement(Elements elements, String binaryName) {
        TypeElement direct = elements.getTypeElement(binaryName);
        if (direct != null) {
            return direct;
        }
        if (binaryName.contains("$")) {
            return elements.getTypeElement(binaryName.replace('$', '.'));
        }
        return null;
    }

    private static String binaryName(Elements elements, TypeElement typeElement) {
        return elements.getBinaryName(typeElement).toString();
    }

    private static Path sourcePathOf(CompilationUnitTree unit) {
        if (unit.getSourceFile() == null || unit.getSourceFile().toUri() == null) {
            return null;
        }
        try {
            return Paths.get(unit.getSourceFile().toUri());
        } catch (Exception ignored) {
            return null;
        }
    }

    private static String sourceSetOf(String path) {
        String normalized = "/" + path.replace('\\', '/') + "/";
        if (normalized.contains("/src/main/java/")) {
            return "MAIN";
        }
        if (normalized.contains("/src/test/java/")) {
            return "TEST";
        }
        if (normalized.contains("/src/integration-test/java/")
                || normalized.contains("/src/integrationTest/java/")
                || normalized.contains("/src/it/java/")) {
            return "INTEGRATION_TEST";
        }
        if (normalized.contains("/src/testFixtures/java/")) {
            return "TEST_FIXTURE";
        }
        return "OTHER";
    }

    private static String modulePathOf(String path) {
        String normalized = path.replace('\\', '/');
        int marker = normalized.indexOf("/src/");
        if (marker < 0) {
            if (normalized.startsWith("src/")) {
                return ".";
            }
            return "";
        }
        String module = normalized.substring(0, marker);
        return module.isEmpty() ? "." : module;
    }

    private static String relativize(Path root, Path source) {
        try {
            if (source.startsWith(root)) {
                String value = root.relativize(source).toString();
                return value.replace('\\', '/');
            }
        } catch (IllegalArgumentException ignored) {
            // Different roots (for example Windows drives) fall back to absolute path.
        }
        return source.toString().replace('\\', '/');
    }

    private static String slice(String source, long start, long end, int maxLength) {
        if (start < 0 || end < start || start >= source.length()) {
            return "";
        }
        int beginIndex = Math.toIntExact(Math.min(start, source.length()));
        int endIndex = Math.toIntExact(Math.min(end, source.length()));
        String value = source.substring(beginIndex, endIndex).trim();
        if (value.length() > maxLength) {
            return value.substring(0, maxLength) + "...";
        }
        return value;
    }

    private static String lineText(String source, long oneBasedLine, int maxLength) {
        if (oneBasedLine <= 0) {
            return "";
        }
        int currentLine = 1;
        int lineStart = 0;
        for (int index = 0; index <= source.length(); index++) {
            boolean end = index == source.length();
            boolean newline = !end && source.charAt(index) == '\n';
            if (end || newline) {
                if (currentLine == oneBasedLine) {
                    int lineEnd = index;
                    if (lineEnd > lineStart && source.charAt(lineEnd - 1) == '\r') {
                        lineEnd--;
                    }
                    String value = source.substring(lineStart, lineEnd).trim();
                    return value.length() > maxLength
                            ? value.substring(0, maxLength) + "..."
                            : value;
                }
                currentLine++;
                lineStart = index + 1;
            }
        }
        return "";
    }

    private static void writeUsages(Path output, List<Usage> usages) throws IOException {
        try (BufferedWriter writer = Files.newBufferedWriter(output, StandardCharsets.UTF_8)) {
            for (Usage usage : usages) {
                writer.write(String.join("\t",
                        usage.targetId,
                        usage.usageKind,
                        usage.matchKind,
                        usage.confidence,
                        usage.sourceSet,
                        encode(usage.sourceFile),
                        encode(usage.modulePath),
                        Long.toString(usage.line),
                        Long.toString(usage.column),
                        Long.toString(usage.endLine),
                        Long.toString(usage.endColumn),
                        encode(usage.enclosingType),
                        encode(usage.enclosingMethod),
                        encode(usage.resolvedSymbol),
                        encode(usage.expression),
                        encode(usage.lineText),
                        Long.toString(usage.startPosition),
                        Long.toString(usage.endPosition)
                ));
                writer.newLine();
            }
        }
    }

    private static void writeResolutions(
            Path output,
            List<Target> targets,
            Map<String, TargetResolution> resolutions,
            List<Usage> usages
    ) throws IOException {
        Map<String, Long> usageCounts = usages.stream()
                .collect(Collectors.groupingBy(usage -> usage.targetId, TreeMap::new, Collectors.counting()));

        try (BufferedWriter writer = Files.newBufferedWriter(output, StandardCharsets.UTF_8)) {
            for (Target target : targets) {
                TargetResolution resolution = resolutions.getOrDefault(
                        target.id,
                        TargetResolution.unresolved("Target was not processed")
                );
                writer.write(String.join("\t",
                        target.id,
                        Boolean.toString(resolution.resolved),
                        encode(String.join(String.valueOf(LIST_SEPARATOR), resolution.resolvedSymbols)),
                        encode(resolution.message),
                        Long.toString(usageCounts.getOrDefault(target.id, 0L))
                ));
                writer.newLine();
            }
        }
    }

    private static void writeDiagnostics(
            Path output,
            Path repositoryRoot,
            List<Diagnostic<? extends JavaFileObject>> diagnostics
    ) throws IOException {
        try (BufferedWriter writer = Files.newBufferedWriter(output, StandardCharsets.UTF_8)) {
            for (Diagnostic<? extends JavaFileObject> diagnostic : diagnostics) {
                String source = diagnosticSource(diagnostic);
                if (!source.isEmpty()) {
                    try {
                        source = relativize(repositoryRoot, Paths.get(source).toAbsolutePath().normalize());
                    } catch (Exception ignored) {
                        source = source.replace('\\', '/');
                    }
                }
                writer.write(String.join("\t",
                        diagnostic.getKind().name(),
                        encode(diagnostic.getCode() == null ? "" : diagnostic.getCode()),
                        encode(source),
                        Long.toString(diagnostic.getLineNumber()),
                        Long.toString(diagnostic.getColumnNumber()),
                        encode(diagnostic.getMessage(Locale.ROOT))
                ));
                writer.newLine();
            }
        }
    }

    private static void writeMetrics(Path output, AnalysisResult result) throws IOException {
        Map<Diagnostic.Kind, Long> diagnosticCounts = new EnumMap<>(Diagnostic.Kind.class);
        for (Diagnostic<? extends JavaFileObject> diagnostic : result.diagnostics) {
            diagnosticCounts.merge(diagnostic.getKind(), 1L, Long::sum);
        }
        long resolvedTargets = result.resolutions.values().stream()
                .filter(resolution -> resolution.resolved)
                .count();

        List<String> lines = List.of(
                "sourceFileCount=" + result.sourceFileCount,
                "classpathEntryCount=" + result.classpathEntryCount,
                "compilationUnitCount=" + result.compilationUnitCount,
                "targetCount=" + result.resolutions.size(),
                "resolvedTargetCount=" + resolvedTargets,
                "unresolvedTargetCount=" + (result.resolutions.size() - resolvedTargets),
                "usageCount=" + result.usages.size(),
                "diagnosticCount=" + result.diagnostics.size(),
                "diagnosticErrorCount=" + diagnosticCounts.getOrDefault(Diagnostic.Kind.ERROR, 0L),
                "diagnosticWarningCount=" + (
                        diagnosticCounts.getOrDefault(Diagnostic.Kind.WARNING, 0L)
                                + diagnosticCounts.getOrDefault(Diagnostic.Kind.MANDATORY_WARNING, 0L)
                ),
                "unresolvedMethodInvocationCount=" + result.scanMetrics.unresolvedMethodInvocations,
                "unresolvedConstructorCallCount=" + result.scanMetrics.unresolvedConstructorCalls,
                "unresolvedMemberReferenceCount=" + result.scanMetrics.unresolvedMemberReferences,
                "unresolvedLambdaTypeCount=" + result.scanMetrics.unresolvedLambdaTypes
        );
        Files.write(output, lines, StandardCharsets.UTF_8);
    }

    private static String diagnosticSource(Diagnostic<? extends JavaFileObject> diagnostic) {
        JavaFileObject source = diagnostic.getSource();
        if (source == null || source.toUri() == null) {
            return "";
        }
        try {
            return Paths.get(source.toUri()).toString();
        } catch (Exception ignored) {
            return source.getName();
        }
    }

    private static List<Target> readTargets(Path path) throws IOException {
        List<Target> targets = new ArrayList<>();
        for (String line : Files.readAllLines(path, StandardCharsets.UTF_8)) {
            if (line.isBlank()) {
                continue;
            }
            String[] columns = line.split("\t", -1);
            if (columns.length != 19) {
                throw new IllegalArgumentException(
                        "Expected 19 target columns but found " + columns.length + " in: " + line
                );
            }
            targets.add(new Target(
                    columns[0],
                    decode(columns[1]),
                    decode(columns[2]),
                    decode(columns[3]),
                    decode(columns[4]),
                    decode(columns[5]),
                    decode(columns[6]),
                    decode(columns[7]),
                    columns[8],
                    decode(columns[9]),
                    decode(columns[10]),
                    decode(columns[11]),
                    decode(columns[12]),
                    Boolean.parseBoolean(columns[13]),
                    Boolean.parseBoolean(columns[14]),
                    splitList(decode(columns[15])),
                    splitList(decode(columns[16])),
                    decode(columns[17]),
                    decode(columns[18])
            ));
        }
        targets.sort(Comparator
                .comparing((Target target) -> target.coordinate)
                .thenComparing(target -> target.signature)
                .thenComparing(target -> target.id));
        return targets;
    }

    private static List<String> splitList(String value) {
        if (value.isEmpty()) {
            return List.of();
        }
        String[] parts = value.split(String.valueOf(LIST_SEPARATOR), -1);
        List<String> values = new ArrayList<>();
        Collections.addAll(values, parts);
        return List.copyOf(values);
    }

    private static List<Path> readPathList(Path path) throws IOException {
        List<Path> result = new ArrayList<>();
        for (String line : Files.readAllLines(path, StandardCharsets.UTF_8)) {
            if (!line.isBlank()) {
                result.add(Paths.get(line).toAbsolutePath().normalize());
            }
        }
        return result;
    }

    private static Map<String, String> parseArguments(String[] args) {
        Map<String, String> result = new LinkedHashMap<>();
        for (int index = 0; index < args.length; index++) {
            String argument = args[index];
            if (!argument.startsWith("--")) {
                throw new IllegalArgumentException("Unexpected argument: " + argument);
            }
            String key = argument.substring(2);
            if (key.isBlank() || index + 1 >= args.length) {
                throw new IllegalArgumentException("Missing value for argument: " + argument);
            }
            result.put(key, args[++index]);
        }
        return result;
    }

    private static Path requiredPath(Map<String, String> arguments, String key) {
        String value = arguments.get(key);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Missing required --" + key + " argument");
        }
        return Paths.get(value);
    }

    private static Path parentOf(Path path) {
        Path parent = path.toAbsolutePath().normalize().getParent();
        return parent == null ? Path.of(".").toAbsolutePath().normalize() : parent;
    }

    private static String readTextUnchecked(Path path) {
        try {
            return Files.readString(path, StandardCharsets.UTF_8);
        } catch (IOException exception) {
            throw new IllegalStateException("Failed to read source file: " + path, exception);
        }
    }

    private static String encode(String value) {
        return BASE64_ENCODER.encodeToString(value.getBytes(StandardCharsets.UTF_8));
    }

    private static String decode(String value) {
        if (value.isEmpty()) {
            return "";
        }
        return new String(BASE64_DECODER.decode(value), StandardCharsets.UTF_8);
    }

    private record Target(
            String id,
            String coordinate,
            String groupId,
            String artifactId,
            String oldVersion,
            String newVersion,
            String dependencyScopeCategory,
            String analysisMode,
            String kind,
            String className,
            String name,
            String signature,
            String changeStatus,
            boolean binaryCompatible,
            boolean sourceCompatible,
            List<String> compatibilityTypes,
            List<String> parameters,
            String reportPath,
            String markdownReportPath
    ) {
    }

    private static final class TargetResolution {
        private final boolean resolved;
        private final List<String> resolvedSymbols;
        private final List<Element> elements;
        private final String message;

        private TargetResolution(
                boolean resolved,
                List<String> resolvedSymbols,
                List<Element> elements,
                String message
        ) {
            this.resolved = resolved;
            this.resolvedSymbols = List.copyOf(resolvedSymbols);
            this.elements = List.copyOf(elements);
            this.message = message;
        }

        private static TargetResolution resolved(List<String> symbols, List<? extends Element> elements) {
            return new TargetResolution(true, symbols, new ArrayList<>(elements), "");
        }

        private static TargetResolution unresolved(String message) {
            return new TargetResolution(false, List.of(), List.of(), message);
        }
    }

    private record Usage(
            String targetId,
            String usageKind,
            String matchKind,
            String confidence,
            String sourceSet,
            String sourceFile,
            String modulePath,
            long line,
            long column,
            long endLine,
            long endColumn,
            String enclosingType,
            String enclosingMethod,
            String resolvedSymbol,
            String expression,
            String lineText,
            long startPosition,
            long endPosition
    ) {
    }

    private record Match(boolean matched, String matchKind) {
        private static Match exact() {
            return new Match(true, "EXACT_SYMBOL");
        }

        private static Match override() {
            return new Match(true, "OVERRIDE");
        }

        private static Match none() {
            return new Match(false, "");
        }
    }

    private static final class ScanMetrics {
        private long unresolvedMethodInvocations;
        private long unresolvedConstructorCalls;
        private long unresolvedMemberReferences;
        private long unresolvedLambdaTypes;
    }

    private record AnalysisResult(
            int sourceFileCount,
            int classpathEntryCount,
            int compilationUnitCount,
            List<Usage> usages,
            Map<String, TargetResolution> resolutions,
            List<Diagnostic<? extends JavaFileObject>> diagnostics,
            ScanMetrics scanMetrics
    ) {
    }
}
