# code-with-quarkus

This project uses Quarkus, the Supersonic Subatomic Java Framework.

If you want to learn more about Quarkus, please visit its website: <https://quarkus.io/>.

## Running the application in dev mode

You can run your application in dev mode that enables live coding using:

```shell script
./mvnw compile quarkus:dev
```

> **_NOTE:_**  Quarkus now ships with a Dev UI, which is available in dev mode only at <http://localhost:8080/q/dev/>.

## Packaging and running the application

The application can be packaged using:

```shell script
./mvnw package
```

It produces the `quarkus-run.jar` file in the `target/quarkus-app/` directory.
Be aware that it’s not an _über-jar_ as the dependencies are copied into the `target/quarkus-app/lib/` directory.

The application is now runnable using `java -jar target/quarkus-app/quarkus-run.jar`.

If you want to build an _über-jar_, execute the following command:

```shell script
./mvnw package -Dquarkus.package.jar.type=uber-jar
```

The application, packaged as an _über-jar_, is now runnable using `java -jar target/*-runner.jar`.

## Creating a native executable

You can create a native executable using:

```shell script
./mvnw package -Dnative
```

Or, if you don't have GraalVM installed, you can run the native executable build in a container using:

```shell script
./mvnw package -Dnative -Dquarkus.native.container-build=true
```

You can then execute your native executable with: `./target/code-with-quarkus-1.0.0-SNAPSHOT-runner`

If you want to learn more about building native executables, please consult <https://quarkus.io/guides/maven-tooling>.

## Provided Code

### RESTEasy Reactive

Easily start your Reactive RESTful Web Services

[Related guide section...](https://quarkus.io/guides/getting-started-reactive#reactive-jax-rs-resources)

## Renovate AI migration pilot

The repository now includes the first pipeline step for a repeatable Renovate + AI migration flow.

- Workflow: `.github/workflows/renovate-ai-step1-showcase.yml`
- Script: `.github/scripts/renovate_step1_showcase.py`
- Goal: verify that `junit:junit` is a good showcase dependency (test-only with JUnit4 usage)

Run the scan locally:

```bash
python3 .github/scripts/renovate_step1_showcase.py \
  --repo-root . \
  --json-out showcase-result.json \
  --md-out showcase-result.md
```

Step 2 analyzes dependency changes between old and new versions.

- Workflow: `.github/workflows/renovate-ai-step2-analyze-changes.yml`
- Script: `.github/scripts/renovate_step2_analyze_changes.py`
- Goal: produce API and dependency tree deltas between two versions

Run Step 2 locally:

```bash
python3 .github/scripts/renovate_step2_analyze_changes.py \
  --dependency junit:junit \
  --old-version 4.12 \
  --new-version 4.13.2 \
  --json-out step2-result.json \
  --md-out step2-result.md
```

Step 3 maps dependency changes to affected source files and concrete code edits.

- Workflow: `.github/workflows/renovate-ai-step3-impact-analysis.yml`
- Script: `.github/scripts/renovate_step3_analyze_impact.py`
- Goal: list impacted classes and suggested migration changes

Run Step 3 locally (after Step 2):

```bash
python3 .github/scripts/renovate_step3_analyze_impact.py \
  --repo-root . \
  --step2-json step2-result.json \
  --json-out step3-result.json \
  --md-out step3-result.md
```

