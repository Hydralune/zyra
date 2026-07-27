# Zyra release operations

`M3-S02B-02` freezes the release boundary around a committed Zyra revision.
The supported delivery is the archive emitted by
`scripts/run_release_pipeline.py`; a source checkout, `node_modules`, a wheel
cache, or any sibling source repository is not an install medium.

## Toolchain

- CPython 3.12 or newer.
- Bun 1.2.15.
- `uv` 0.9.28 is the CI dependency installer. Plain `pip` remains supported by
  the generated platform plan.
- Git is needed while producing an archive. It is not needed after extraction;
  the installed release reads its source revision from
  `release/manifest.json`.

Both dependency graphs are frozen. `requirements.txt` contains the complete
Python transitive closure and SHA-256 hashes. `bun.lock` and the root
`packageManager` field bind the JavaScript closure and Bun version.

## Build and admission

From a clean committed checkout:

```text
python scripts/run_release_pipeline.py \
  --release-id zyra-<commit> \
  --expected-commit <40-character-commit>
```

The command builds the same release twice, rejects a byte mismatch, promotes
one archive, verifies its manifest/checksums/wheel RECORD, and runs the CI gate
DAG. Its final authority is `pipeline-report.json` plus
`admission/ci/release-admission.json`. A report with `ready: false`, a missing
mandatory gate, or a commit mismatch is not releasable.

The bundle contains:

- source and runtime assets admitted by the release boundary;
- deterministic `zyra-<version>-py3-none-any.whl`;
- `requirements.txt` and `bun.lock`;
- CycloneDX SBOM, NOTICE, runtime inventory, configuration example;
- benchmark/evidence binding;
- checksums and the signed-by-digest release manifest.

## Install

After extracting the archive, run the commands returned by:

```text
python -m zyra_productization.release.cli platform-plan windows
python -m zyra_productization.release.cli platform-plan linux
```

Materialize `release/configuration.example.json` through the configuration
provisioner. Secret fields are environment references such as
`${OPENAI_API_KEY}`; literal secret material is rejected and is never copied
into the installed configuration.

The transactional archive installer is:

```text
python -m zyra_productization.release.cli \
  --state-root <release-state> install <archive> \
  --install-root <install-root> \
  --idempotency-key <unique-request-key> \
  --release-id <release-id> \
  --manifest-digest <manifest-digest>
```

It verifies/stages/migrates/doctors/activates under an install-root lease. The
`current` file is the atomic active pointer. Repeating the same idempotency key
with identical input returns the original receipt; reusing it with different
input is rejected.

## Lifecycle

```text
python -m zyra_productization.release.cli lifecycle start
python -m zyra_productization.release.cli health
python -m zyra_productization.release.cli lifecycle status
python -m zyra_productization.release.cli lifecycle stop
```

`health` executes semantic readiness, including the canonical runtime-owner
composition and, by default, the short task. A listening port without semantic
readiness is not a successful start.

Install-time schema migration defaults to version 1. Future committed
migrations use:

```text
python -m zyra_productization.release.cli migrate <transaction-id> \
  --target-version <version>
```

Restore the release that was active immediately before a committed install:

```text
python -m zyra_productization.release.cli rollback <transaction-id> \
  --target-version 0
```

Rollback verifies that the transaction still owns the active pointer, reverses
its migration journal, restores the prior pointer, removes only the new
product-owned release, and records a terminal `rolled_back` receipt.

Uninstall removes only paths recorded in the install receipt:

```text
python -m zyra_productization.release.cli uninstall <transaction-id>
```

Operator state is preserved unless `--purge-state` is explicitly supplied.

## Clean-install evidence

The clean-install gate extracts to a newly created temporary directory, creates
a new virtual environment, installs the hash-locked dependency closure and the
bundle wheel, installs the frozen Bun workspace, typechecks and builds the
TypeScript runtime and Web application, then executes:

```text
submission boundary -> release doctor -> start -> semantic health -> stop
-> transactional uninstall
```

Allocated ports are probed before start, the state root is isolated, and
failure diagnostics include bounded, secret-redacted log tails. The temporary
workspace is always removed. A successful receipt must have
`workspace_isolated: true`, `product_lifecycle_exercised: true`, committed
install state, uninstalled final state, and no parent source repository.

## Fail-closed conditions

Do not publish when any of the following occurs:

- dirty or wrong Git revision;
- missing/unhashed Python lock or mismatched Bun lock;
- cache, secret, sibling-source, editable path, or unsafe archive entry;
- checksum, wheel RECORD, SBOM/NOTICE/runtime inventory, or benchmark mismatch;
- migration/rollback/idempotency/lease conflict;
- failed Python tests, TypeScript typecheck, Web build, source custody, or
  submission boundary;
- clean install, canonical runtime-owner readiness, or semantic health failure;
- a missing, skipped, duplicated, blocked, or failed mandatory CI gate.

The workflow `.github/workflows/release.yml` runs the same admission graph on
Windows amd64 and Linux x86_64 and uploads the archive and exact machine
receipts. GitHub status alone is not the evidence authority; the uploaded
release-admission digest is.

The Python gate runs both `tests/unit` and `tests/integration`. Its only
exclusions are frozen in `config/release-python-tests.json`: six legacy
integration files, twelve bounded M1 scaffold/aggregate nodes, and the protected
M1-03B MCP source audit. The first groups assert superseded pre-E02 Python-owned API,
QuerySession, tool-loop, and compact/restore projection contracts, including an
M1 foundation CLI that imports the deliberately deleted Python tool owner.
Current TypeScript lifecycle, API, and owner-disconnect behavior remains in the
release suite. The five aggregate hardening exclusions rerun internalization,
live main-path, and disable audits already enforced by the dedicated
source-custody, formal benchmark, semantic-health, and admission gates. The MCP
audit intentionally inspects sibling source repositories that a clean release
must not contain. M3-03 owns the legacy-contract cleanup and source-audit
normalization. The policy validates every path and node id, and the exact
pytest arguments remain visible in the CI receipt; a missing exclusion path or
node-id file fails closed.

The Python gate has a 5,400-second ceiling. This is a fail-closed upper bound
for the M3-02 aggregate suite, not a target duration; Windows integration
measurements include real Bun runtimes, API servers, ledger audits, live
scenarios, and process cleanup and exceed the former 1,800-second ceiling while
still making forward progress.
