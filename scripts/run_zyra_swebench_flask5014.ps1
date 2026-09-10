param(
    [string]$RunLabel = "run1",
    [string]$InstanceId = "pallets__flask-5014",
    [string]$ContainerImage = "swebench/sweb.eval.x86_64.pallets_1776_flask-5014:latest",
    [string]$BaseCommit = "7ee9ceb71e868944a46e1ff00b506772a53a4f1d",
    [string]$GoalFile = "",
    [ValidateRange(5, 45)]
    [int]$DeadlineMinutes = 45,
    [ValidateRange(1, 200)]
    [int]$MaxTurns = 80,
    [ValidateRange(1000, 10000000)]
    [int]$MaxTotalTokens = 600000,
    [ValidateRange(512, 131072)]
    [int]$MaxOutputTokens = 16384,
    [ValidateSet("calibration", "integration_calibration", "formal", "invalidated_development")]
    [string]$RunClass = "calibration",
    [string]$InvalidationReason = ""
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$stateRoot = Join-Path $repo ".tmp\zyra-swebench-formal-$RunLabel"
$logRoot = Join-Path $repo "paper\experiments\evidence\formal\zyra\swebench\$InstanceId\$RunLabel"
$container = "zyra-$($InstanceId -replace '_', '-')-bench"
$analysisClass = if ($RunClass -eq "integration_calibration") { "calibration" } else { $RunClass }
if ($analysisClass -eq "invalidated_development" -and -not $InvalidationReason.Trim()) {
    throw "Invalidated development runs require -InvalidationReason."
}

if (Test-Path -LiteralPath $stateRoot) {
    throw "Refusing to reuse an existing state directory: $stateRoot"
}

$env:DOCKER_HOST = "tcp://127.0.0.1:2375"
$dockerBridgeScript = Join-Path $repo "scripts\docker_wsl_bridge.py"
if (-not (Test-Path -LiteralPath $dockerBridgeScript)) {
    throw "The benchmark Docker bridge is missing: $dockerBridgeScript"
}
# A systemd service does not keep a WSL distribution alive on its own. Hold
# one explicit Linux process for the full run so idle WSL teardown cannot kill
# Docker and every benchmark container with exit code 255 between tool calls.
$wslKeeper = Start-Process -FilePath "C:\Windows\System32\wsl.exe" -ArgumentList "-d", "Ubuntu", "--", "sleep", "infinity" -PassThru -WindowStyle Hidden
try {
Start-Sleep -Seconds 2
if ($wslKeeper.HasExited) {
    throw "The Ubuntu WSL keepalive process exited during startup."
}
# The desktop environment has a localhost proxy.  API self-attestation of the
# CLI's ephemeral loopback terminal must never be sent through that proxy.
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = $env:NO_PROXY
$containerState = & "D:\Anaconda\python.exe" $dockerBridgeScript inspect $container --format '{{.State.Running}}' 2>$null
if ($LASTEXITCODE -ne 0 -or $containerState.Trim().ToLowerInvariant() -ne "true") {
    throw "The benchmark container $container is not running."
}
$dirty = & "D:\Anaconda\python.exe" $dockerBridgeScript exec $container sh -lc 'cd /testbed && git status --porcelain' 2>$null
if ($LASTEXITCODE -ne 0 -or $dirty) {
    throw "The benchmark container is not a clean baseline; refusing to run."
}
$containerHead = (& "D:\Anaconda\python.exe" $dockerBridgeScript exec $container sh -lc 'cd /testbed && git rev-parse HEAD').Trim()
if ($LASTEXITCODE -ne 0 -or -not $containerHead) {
    throw "Could not resolve the benchmark container HEAD."
}
& "D:\Anaconda\python.exe" $dockerBridgeScript exec $container sh -lc "cd /testbed && git merge-base --is-ancestor '$BaseCommit' HEAD"
if ($LASTEXITCODE -ne 0) {
    throw "Declared base $BaseCommit is not an ancestor of benchmark container HEAD $containerHead."
}
$rawBaseDelta = @(& "D:\Anaconda\python.exe" $dockerBridgeScript exec $container sh -lc "cd /testbed && git diff --raw '$BaseCommit'..HEAD")
if ($LASTEXITCODE -ne 0) {
    throw "Could not compare benchmark container HEAD $containerHead with declared base $BaseCommit."
}
# Some official SWE-bench images append a setup commit that changes every file
# from mode 0644 to 0755 while preserving every blob byte.  This is not source
# contamination.  Permit only that auditable mode-only shape; any changed blob,
# add/delete/rename, or malformed raw-diff record remains a hard failure.
$nonModeOnlyBaseDelta = @($rawBaseDelta | Where-Object {
    $_ -notmatch '^:\d{6} \d{6} ([0-9a-f]+) ([0-9a-f]+) M\t' -or $matches[1] -ne $matches[2]
})
# A second, distinct official setup shape: images built from an older project
# (e.g. Sphinx 4.1) pin dependency upper bounds and tune tox so the frozen
# environment actually installs.  Those content-level edits live in a single
# ``SWE-bench <setup@swebench.com>`` commit whose parent is exactly the declared
# base.  They are pre-seeded environment material, not agent contamination, and
# are auditable by the author/subject/parent triple below.
$headAuthorEmail = (& "D:\Anaconda\python.exe" $dockerBridgeScript exec $container sh -lc "cd /testbed && git log -1 --format='%ae' HEAD").Trim()
$headSubject = (& "D:\Anaconda\python.exe" $dockerBridgeScript exec $container sh -lc "cd /testbed && git log -1 --format='%s' HEAD").Trim()
$headParent = (& "D:\Anaconda\python.exe" $dockerBridgeScript exec $container sh -lc "cd /testbed && git log -1 --format='%P' HEAD").Trim()
$officialEnvSetupCommit = (
    $headAuthorEmail -eq "setup@swebench.com" -and
    $headSubject -eq "SWE-bench" -and
    $headParent -eq $BaseCommit
)
if ($nonModeOnlyBaseDelta.Count -gt 0 -and -not $officialEnvSetupCommit) {
    throw "Benchmark container HEAD $containerHead has source-content changes relative to declared base $BaseCommit."
}
$baseTreeEquivalent = ($rawBaseDelta.Count -eq 0)
$baseContentEquivalent = ($nonModeOnlyBaseDelta.Count -eq 0)
$modeOnlySetupDeltaCount = ($rawBaseDelta.Count - $nonModeOnlyBaseDelta.Count)
$environmentSetupCommitObserved = $officialEnvSetupCommit
$environmentSetupContentDeltaCount = if ($officialEnvSetupCommit) { $nonModeOnlyBaseDelta.Count } else { 0 }

New-Item -ItemType Directory -Force -Path $stateRoot, $logRoot | Out-Null
Get-Content (Join-Path $repo ".env.deepseek.local") | ForEach-Object {
    if ($_ -match '^\s*([^#=\s]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
    }
}

$env:ZYRA_STATE_ROOT = $stateRoot
# Physical CodeWorker evidence binds the canonical database to the parent of
# the artifact root, using the production filename below.
$env:ZYRA_SQLITE_PATH = Join-Path $stateRoot "zyra.sqlite3"
$env:ZYRA_EVENT_LOG = Join-Path $stateRoot "events.jsonl"
$env:ZYRA_ARTIFACT_ROOT = Join-Path $stateRoot "artifacts"
$env:ZYRA_WORKER_POOL_STORE = Join-Path $stateRoot "worker-pool.sqlite3"
$env:ZYRA_GRAPH_STATE_STORE = Join-Path $stateRoot "graph.sqlite3"
$env:ZYRA_WORKSPACE_STATE_ROOT = Join-Path $stateRoot "workspace-state"
$env:ZYRA_WORKSPACE_DATA_ROOT = Join-Path $stateRoot "workspace-data"
$env:ZYRA_CONTROL_STATE = Join-Path $stateRoot "control"
$env:ZYRA_SUBAGENT_STATE = Join-Path $stateRoot "subagents"
$env:ZYRA_DEEPSEEK_ENABLED = "true"
$env:ZYRA_MODEL_PROVIDER = "deepseek"
$env:ZYRA_MODEL = "deepseek-flash"
$env:ZYRA_API_HOST = "127.0.0.1"
$env:ZYRA_API_PORT = "8001"
# Separate the per-run deployment workers from stale development workers that
# may occupy the default 8310--8312 profile block.
$env:ZYRA_DEPLOYMENT_PROFILE_BASE_PORT = "8420"
$env:ZYRA_BENCHMARK_DOCKER_CONTAINER = $container
$env:ZYRA_BENCHMARK_DOCKER_WORKDIR = "/testbed"
$env:ZYRA_BENCHMARK_DOCKER_BRIDGE_SCRIPT = Join-Path $repo "scripts\docker_wsl_bridge.py"
$env:ZYRA_BENCHMARK_LONG_HORIZON = "true"
# This is an explicit harness limit, passed intact to the physical worker and
# recorded below.  It prevents an otherwise correct patch from being stranded
# in an unbounded orchestration loop.
$env:ZYRA_REASONING_MAX_TURNS = $MaxTurns.ToString()
# Whole-session cap enforced by the TypeScript provider boundary; it counts
# provider input, output, and cache-token receipts rather than prompt text.
$env:ZYRA_MAX_TOTAL_TOKENS = $MaxTotalTokens.ToString()
# Keep a separate, recorded per-request ceiling. Long-horizon code repair
# needs enough room for an atomic tool invocation; the whole-session cap above
# remains the authoritative shared resource limit.
$env:ZYRA_MAX_OUTPUT_TOKENS = $MaxOutputTokens.ToString()
# A formal sample must have one externally recorded, non-renewable total
# deadline.  The API forwards the same deadline to every physical dispatch.
$env:ZYRA_EXTERNAL_DEADLINE_EPOCH_MS = [DateTimeOffset]::UtcNow.AddMinutes($DeadlineMinutes).ToUnixTimeMilliseconds().ToString()

$apiOut = Join-Path $logRoot "api.stdout.log"
$apiErr = Join-Path $logRoot "api.stderr.log"
$taskCreateLog = Join-Path $logRoot "task-create.json"
$taskRunLog = Join-Path $logRoot "task-run.json"
$metadata = Join-Path $logRoot "run-metadata.json"
[ordered]@{
    schema = "zyra.swebench-formal-run.v1"
    instance_id = $InstanceId
    system = "ZYRA"
    run_class = $RunClass
    analysis_class = $analysisClass
    efficacy_eligible = ($analysisClass -eq "formal")
    invalidation_reason = if ($analysisClass -eq "invalidated_development") { $InvalidationReason.Trim() } else { "" }
    model = "deepseek-flash"
    temperature = 0
    sealed = $true
    container = $container
    container_image = $ContainerImage
    base_commit = $BaseCommit
    container_head = $containerHead
    base_tree_equivalent = $baseTreeEquivalent
    base_content_equivalent = $baseContentEquivalent
    mode_only_setup_delta_count = $modeOnlySetupDeltaCount
    environment_setup_commit_observed = $environmentSetupCommitObserved
    environment_setup_content_delta_count = $environmentSetupContentDeltaCount
    deadline_minutes = $DeadlineMinutes
    max_turns = $MaxTurns
    max_total_tokens = $MaxTotalTokens
    max_output_tokens_per_request = [int]$env:ZYRA_MAX_OUTPUT_TOKENS
    external_deadline_epoch_ms = [Int64]$env:ZYRA_EXTERNAL_DEADLINE_EPOCH_MS
    started_at = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json | Set-Content -Encoding utf8 $metadata

$taskId = ""
$runJob = $null
$api = Start-Process -FilePath "D:\Anaconda\python.exe" -ArgumentList "scripts\dev_api.py" -WorkingDirectory $repo -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr -PassThru -WindowStyle Hidden
try {
    $deadline = (Get-Date).AddMinutes(2)
    do {
        Start-Sleep -Milliseconds 500
        try { $ready = Invoke-RestMethod -Uri "http://127.0.0.1:8001/health" -TimeoutSec 2 } catch { $ready = $null }
    } while (-not $ready -and (Get-Date) -lt $deadline)
    if (-not $ready) { throw "ZYRA API did not become ready within two minutes." }

    $goal = @"
Work on the repository in the supplied benchmark container. A Flask Blueprint with an empty name is accepted but behaves incorrectly. Diagnose the issue, implement the smallest correct production fix, and add or adapt a focused regression test. Run relevant tests. Preserve unrelated behavior and deliver the code change in the repository; do not only describe a patch.
"@
    if ($GoalFile) {
        if (-not (Test-Path -LiteralPath $GoalFile)) {
            throw "GoalFile does not exist: $GoalFile"
        }
        $issueText = (Get-Content -Raw -LiteralPath $GoalFile).Trim()
        # A sealed benchmark container exists to produce a physical code change.
        # The delivery directive must be part of the goal the model receives, not
        # something the goal file happens to contain.  Some goal files already
        # carry the standard preamble (legacy calibration tasks); detect it so we
        # never double-inject, and add it when an issue is phrased as pure prose.
        $deliveryPreamble = "Work on the repository in the supplied benchmark container. Diagnose and fix the following issue using the smallest correct production change. Add or adapt a focused regression test, run the relevant tests, and leave the completed code changes in the repository. Do not only describe a patch."
        if ($issueText -match "leave the completed code changes|do not only describe a patch|deliver the code change") {
            $goal = $issueText
        } else {
            $goal = "$deliveryPreamble`n`n$issueText"
        }
    }
    # Sealed benchmark tasks own their Docker executor in the API.  Calling the
    # API directly avoids staging this development repository into the task
    # workspace, which would otherwise contaminate the benchmark input.
    $createBody = @{
        goal = $goal.Trim()
        auto_run = $false
        sealed = $true
        sealed_autonomous = $true
        competition_mode = "sealed_autonomous"
    } | ConvertTo-Json
    $created = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8001/tasks" -ContentType "application/json" -Body $createBody -TimeoutSec 120
    $created | ConvertTo-Json -Depth 100 | Set-Content -Encoding utf8 $taskCreateLog
    $taskId = [string]$created.task.task_id
    if (-not $taskId) { throw "Task creation response did not include task.task_id." }
    $runBody = @{ requested_by = "zyra-swebench-formal-harness"; resume_invocation_id = "formal-$RunLabel" } | ConvertTo-Json
    $runUri = "http://127.0.0.1:8001/tasks/$taskId/run"
    $runJob = Start-Job -ScriptBlock {
        param($Uri, $Body)
        Invoke-RestMethod -Method Post -Uri $Uri -ContentType "application/json" -Body $Body -TimeoutSec 4500
    } -ArgumentList $runUri, $runBody
    $progressDatabase = Join-Path $stateRoot "artifacts\.provider-control-plane\provider.sqlite3"
    $progressScript = Join-Path $repo "paper\experiments\formal\live_provider_progress.py"
    $lastProgress = ""
    while ($runJob.State -in @("NotStarted", "Running")) {
        Start-Sleep -Seconds 5
        $snapshot = & "D:\Anaconda\python.exe" $progressScript $progressDatabase 2>$null
        if ($LASTEXITCODE -eq 0 -and $snapshot -and $snapshot -ne $lastProgress) {
            $progress = $snapshot | ConvertFrom-Json
            if ($progress.ready) {
                $lastActivity = if ($progress.last_activity_ms) {
                    [DateTimeOffset]::FromUnixTimeMilliseconds([Int64]$progress.last_activity_ms).ToLocalTime().ToString("HH:mm:ss")
                } else { "waiting" }
                Write-Host ("[{0}] task={1} dispatches={2} ok={3} active={4} failed={5} last={6}" -f (Get-Date -Format "HH:mm:ss"), $taskId, $progress.dispatches, $progress.succeeded, $progress.active, $progress.failed, $lastActivity)
                $lastProgress = $snapshot
            }
        }
    }
    try {
        $completed = Receive-Job -Job $runJob -Wait -AutoRemoveJob -ErrorAction Stop
    }
    catch {
        $runError = $_
        Remove-Job -Job $runJob -Force -ErrorAction SilentlyContinue
        $errorText = if ($runError.ErrorDetails.Message) {
            [string]$runError.ErrorDetails.Message
        } else {
            [string]$runError.Exception.Message
        }
        $errorResponse = $null
        try { $errorResponse = $errorText | ConvertFrom-Json -ErrorAction Stop } catch { $errorResponse = $null }

        # POST /run is a long observer connection, not the canonical task
        # store.  A 5xx response or detached socket must never prevent the
        # benchmark harness from collecting the terminal task state.  GET
        # also invokes deadline reconciliation in the API, so an expired
        # in-flight task is projected to an auditable terminal failure.
        $canonicalResponse = $null
        $canonicalReadError = ""
        for ($attempt = 1; $attempt -le 3 -and -not $canonicalResponse; $attempt++) {
            try {
                $canonicalResponse = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8001/tasks/$taskId" -TimeoutSec 30
            }
            catch {
                $canonicalReadError = [string]$_.Exception.Message
                if ($attempt -lt 3) { Start-Sleep -Seconds 1 }
            }
        }
        $canonicalTask = if ($canonicalResponse -and $canonicalResponse.task) {
            $canonicalResponse.task
        } else {
            [ordered]@{
                task_id = $taskId
                run_id = [string]$created.task.run_id
                status = "unknown"
                metadata = @{}
            }
        }
        $completed = [ordered]@{
            schema = "zyra.swebench-task-run-envelope/v1"
            task = $canonicalTask
            canonical_task_record_observed = [bool]($canonicalResponse -and $canonicalResponse.task)
            run_transport = [ordered]@{
                ok = $false
                error = $errorText
                error_response = $errorResponse
                canonical_read_error = $canonicalReadError
            }
            canonical_response = $canonicalResponse
        }
    }
    $completed | ConvertTo-Json -Depth 100 | Set-Content -Encoding utf8 $taskRunLog
    $taskStatus = [string]$completed.task.status
    $terminalStateObserved = @("completed", "failed", "cancelled") -contains $taskStatus
    $canonicalTaskRecordObserved = if ($completed.PSObject.Properties.Name -contains "canonical_task_record_observed") {
        [bool]$completed.canonical_task_record_observed
    } else {
        [bool]$completed.task
    }
    $exitCode = if ($taskStatus -eq "completed") { 0 } else { 1 }
    $runMetadata = Get-Content -Raw $metadata | ConvertFrom-Json
    $runMetadata | Add-Member -Force -NotePropertyName task_id -NotePropertyValue $taskId
    $runMetadata | Add-Member -Force -NotePropertyName task_status -NotePropertyValue $taskStatus
    $runMetadata | Add-Member -Force -NotePropertyName canonical_task_record_observed -NotePropertyValue $canonicalTaskRecordObserved
    $runMetadata | Add-Member -Force -NotePropertyName terminal_state_observed -NotePropertyValue $terminalStateObserved
    $runMetadata | Add-Member -Force -NotePropertyName cli_exit_code -NotePropertyValue $exitCode
    $runMetadata | Add-Member -Force -NotePropertyName ended_at -NotePropertyValue (Get-Date).ToUniversalTime().ToString("o")
    $runMetadata | ConvertTo-Json | Set-Content -Encoding utf8 $metadata
    & "D:\Anaconda\python.exe" (Join-Path $repo "paper\experiments\formal\summarize_zyra_run.py") $logRoot --analysis-class $analysisClass
    if ($LASTEXITCODE -ne 0) { throw "Run evidence summary generation failed." }
    exit $exitCode
}
catch {
    $harnessError = $_
    if ($runJob) {
        Remove-Job -Job $runJob -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path -LiteralPath $taskRunLog)) {
        [ordered]@{
            schema = "zyra.swebench-task-run-envelope/v1"
            task = [ordered]@{
                task_id = $taskId
                run_id = ""
                status = "unknown"
                metadata = @{}
            }
            canonical_task_record_observed = $false
            run_transport = [ordered]@{
                ok = $false
                error = [string]$harnessError.Exception.Message
                phase = "formal_harness"
            }
        } | ConvertTo-Json -Depth 100 | Set-Content -Encoding utf8 $taskRunLog
    }
    $preservedTaskRun = Get-Content -Raw -LiteralPath $taskRunLog | ConvertFrom-Json
    $preservedStatus = [string]$preservedTaskRun.task.status
    if (-not $preservedStatus) { $preservedStatus = "unknown" }
    $preservedCanonical = if ($null -ne $preservedTaskRun.canonical_task_record_observed) {
        [bool]$preservedTaskRun.canonical_task_record_observed
    } else {
        [bool]$preservedTaskRun.task.task_id
    }
    $preservedTerminal = @("completed", "failed", "cancelled") -contains $preservedStatus
    if (Test-Path -LiteralPath $metadata) {
        $failureMetadata = Get-Content -Raw -LiteralPath $metadata | ConvertFrom-Json
        $failureMetadata | Add-Member -Force -NotePropertyName task_id -NotePropertyValue $taskId
        $failureMetadata | Add-Member -Force -NotePropertyName task_status -NotePropertyValue $preservedStatus
        $failureMetadata | Add-Member -Force -NotePropertyName canonical_task_record_observed -NotePropertyValue $preservedCanonical
        $failureMetadata | Add-Member -Force -NotePropertyName terminal_state_observed -NotePropertyValue $preservedTerminal
        $failureMetadata | Add-Member -Force -NotePropertyName cli_exit_code -NotePropertyValue 1
        $failureMetadata | Add-Member -Force -NotePropertyName harness_error -NotePropertyValue ([string]$harnessError.Exception.Message)
        $failureMetadata | Add-Member -Force -NotePropertyName ended_at -NotePropertyValue (Get-Date).ToUniversalTime().ToString("o")
        $failureMetadata | ConvertTo-Json | Set-Content -Encoding utf8 $metadata
        try {
            & "D:\Anaconda\python.exe" (Join-Path $repo "paper\experiments\formal\summarize_zyra_run.py") $logRoot --analysis-class $analysisClass | Out-Null
        } catch { }
    }
    throw
}
finally {
    if ($api -and -not $api.HasExited) { Stop-Process -Id $api.Id -Force }
}
}
finally {
    if ($wslKeeper -and -not $wslKeeper.HasExited) {
        Stop-Process -Id $wslKeeper.Id -Force
    }
}
