[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EnvironmentFile,

    [ValidateRange(30, 300)]
    [int]$StartupTimeoutSeconds = 150
)

$ErrorActionPreference = "Stop"

function Invoke-DockerSafe {
    param([string[]]$DockerArgs)

    $previousErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & docker @DockerArgs 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorPreference
    }
    if ($exitCode -ne 0) {
        throw "Docker command '$($DockerArgs[0])' failed with exit code $exitCode. Output suppressed."
    }
    return $output
}

function Get-HttpResponse {
    param([string]$Uri)

    try {
        return Invoke-WebRequest -Uri $Uri -Method Get -TimeoutSec 5 -UseBasicParsing
    }
    catch {
        return $null
    }
}

$resolvedEnvFile = (Resolve-Path -LiteralPath $EnvironmentFile).Path
$uniqueId = [Guid]::NewGuid().ToString("N")
$containerName = "invoiceops-verify-$uniqueId"
$imageTag = "invoiceops:verify-$uniqueId"
$containerCreated = $false

try {
    Write-Host "Building the existing InvoiceOps Docker image..."
    $null = Invoke-DockerSafe @("build", "--tag", $imageTag, ".")
    Write-Host "Docker build: PASS"

    # Port publishing with an empty host port asks Docker to select an available port.
    # Production safety overrides prevent this verification container from seeding,
    # resetting, or enabling reviewer actions against the supplied application DB.
    # Mark the unique name for cleanup before invoking Docker in case the client
    # reports an error after the daemon has already created the container.
    $containerCreated = $true
    $null = Invoke-DockerSafe @(
        "run", "--detach", "--rm",
        "--name", $containerName,
        "--env-file", $resolvedEnvFile,
        "--env", "PORT=8501",
        "--env", "APP_ENV=production",
        "--env", "AUTO_SEED_REFERENCE_DATA=false",
        "--env", "ENABLE_DEMO_RESET=false",
        "--env", "ENABLE_REVIEWER_ACTIONS=false",
        "--publish", "127.0.0.1::8501",
        $imageTag
    )

    $portOutput = (Invoke-DockerSafe @("port", $containerName, "8501/tcp") | Out-String).Trim()
    $portMatch = [regex]::Match($portOutput, "127\.0\.0\.1:(\d+)")
    if (-not $portMatch.Success) {
        throw "Could not determine the temporary container's host port. Docker output suppressed."
    }
    $hostPort = $portMatch.Groups[1].Value
    $baseUri = "http://127.0.0.1:$hostPort"

    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $healthResponse = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        $running = (Invoke-DockerSafe @(
            "inspect", "--format", "{{.State.Running}}", $containerName
        ) | Out-String).Trim()
        if ($running -ne "true") {
            throw "Temporary Streamlit container exited before health check succeeded. Raw startup logs suppressed."
        }
        $healthResponse = Get-HttpResponse "$baseUri/_stcore/health"
        if ($healthResponse -and [int]$healthResponse.StatusCode -eq 200) {
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $healthResponse -or [int]$healthResponse.StatusCode -ne 200) {
        throw "Streamlit health endpoint did not return HTTP 200 within $StartupTimeoutSeconds seconds."
    }
    Write-Host "Streamlit health endpoint: PASS (HTTP 200)"

    $rootResponse = Get-HttpResponse $baseUri
    if (-not $rootResponse -or [int]$rootResponse.StatusCode -ne 200) {
        throw "Streamlit root page did not return HTTP 200."
    }
    if ($rootResponse.Content -notmatch "(?i)streamlit|stApp") {
        throw "Streamlit root page returned HTTP 200 but did not contain the expected app shell."
    }
    Write-Host "Streamlit root page: PASS (HTTP 200)"

    $dbOutput = (Invoke-DockerSafe @(
        "exec", $containerName, "python", "-m", "scripts.check_db_connection"
    ) | Out-String)
    if ($dbOutput -notmatch "Driver: postgresql/psycopg") {
        throw "Container did not verify a PostgreSQL psycopg connection; output suppressed."
    }
    if ($dbOutput -notmatch "All 9 application tables are available") {
        throw "Container did not verify all nine application tables; output suppressed."
    }
    Write-Host "Application database: PASS (PostgreSQL/psycopg; 9 tables)"

    $settingsCode = "from invoiceops.config import get_settings; s=get_settings(); ok=s.is_production and not s.auto_seed_reference_data and not s.enable_demo_reset and not s.enable_reviewer_actions; print('PRODUCTION_SAFE' if ok else 'PRODUCTION_UNSAFE')"
    $settingsOutput = (Invoke-DockerSafe @(
        "exec", $containerName, "python", "-c", $settingsCode
    ) | Out-String).Trim()
    if ($settingsOutput -ne "PRODUCTION_SAFE") {
        throw "Container production safety settings did not verify; output suppressed."
    }
    Write-Host "Production safety settings: PASS"

    $running = (Invoke-DockerSafe @(
        "inspect", "--format", "{{.State.Running}}", $containerName
    ) | Out-String).Trim()
    if ($running -ne "true") {
        throw "Temporary Streamlit container stopped during verification."
    }
    Write-Host "Container remains running: PASS"

    # Inspect logs in memory but never print raw output, which could contain secrets.
    $startupLogs = (Invoke-DockerSafe @("logs", $containerName) | Out-String)
    $errorPatterns = [ordered]@{
        "Python traceback" = "Traceback \(most recent call last\)"
        "module/import failure" = "ModuleNotFoundError|ImportError"
        "database operational failure" = "OperationalError|Database check failed"
        "application exception" = "(?im)^\s*(?:ERROR|Exception):"
    }
    $errorKinds = @(
        foreach ($entry in $errorPatterns.GetEnumerator()) {
            if ($startupLogs -match $entry.Value) { $entry.Key }
        }
    )
    if ($errorKinds.Count -gt 0) {
        throw "Startup logs contain application error indicators ($($errorKinds -join ', ')); raw logs suppressed."
    }
    Write-Host "Startup log inspection: PASS (no application error indicators)"
    Write-Host "Dockerized Streamlit verification: PASS"
}
finally {
    if ($containerCreated) {
        # The generated name is unique to this run. No other container is stopped or removed.
        try { $null = Invoke-DockerSafe @("rm", "--force", $containerName) } catch { }
        Write-Host "Temporary verification container cleaned up."
    }
}
