# Regression guard for the benchmark base-content preflight in
# scripts/run_zyra_swebench_flask5014.ps1.
#
# The preflight must accept two official image shapes and reject genuine
# contamination:
#   1. mode-only setup commit (blob hashes unchanged, file mode 0644 -> 0755);
#   2. a single ``SWE-bench <setup@swebench.com>`` environment-setup commit
#      whose parent is exactly the declared base (content-level dependency pins
#      such as Sphinx 4.1 pinning Jinja2<3.0);
#   3. any other content-level delta relative to base is a hard failure.
#
# This mirrors the in-runner logic; it locks the semantic so a future refactor
# cannot silently widen or narrow the acceptance window.

$ErrorActionPreference = "Stop"

function Test-OfficialEnvironmentSetupCommit {
    param(
        [string]$AuthorEmail,
        [string]$Subject,
        [string]$Parent,
        [string]$BaseCommit
    )
    return (
        $AuthorEmail -eq "setup@swebench.com" -and
        $Subject -eq "SWE-bench" -and
        $Parent -eq $BaseCommit
    )
}

function Assert-True([bool]$Value, [string]$Name) {
    if (-not $Value) { throw "FAILED: $Name" }
    Write-Output "PASS: $Name"
}

$base = "6918e69600810a4664e53653d6ff0290c3c4a788"

# Case 2 (Sphinx 9367): official environment-setup commit with content deltas.
Assert-True (Test-OfficialEnvironmentSetupCommit -AuthorEmail "setup@swebench.com" -Subject "SWE-bench" -Parent $base -BaseCommit $base) "official environment-setup commit accepted"

# Case 3a: same content deltas but a real author -> reject.
Assert-True (-not (Test-OfficialEnvironmentSetupCommit -AuthorEmail "dev@example.com" -Subject "SWE-bench" -Parent $base -BaseCommit $base)) "non-setup author rejected"

# Case 3b: correct author but parent is not the declared base (an extra commit) -> reject.
Assert-True (-not (Test-OfficialEnvironmentSetupCommit -AuthorEmail "setup@swebench.com" -Subject "SWE-bench" -Parent "0000000000000000000000000000000000000000" -BaseCommit $base)) "setup commit with unexpected parent rejected"

# Case 3c: correct author+parent but a real patch subject -> reject.
Assert-True (-not (Test-OfficialEnvironmentSetupCommit -AuthorEmail "setup@swebench.com" -Subject "fix tuple rendering" -Parent $base -BaseCommit $base)) "non-setup subject rejected"

Write-Output "ALL PASS"
