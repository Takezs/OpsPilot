param(
    [string]$CredentialsDirectory = 'E:\OpsPilot-release-20260908',
    [string]$DeepSeekProxy = 'http://host.docker.internal:7897',
    [switch]$CheckOnly,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-DockerChecked {
    param([string[]]$DockerArguments)
    & docker @DockerArguments
    if ($LASTEXITCODE -ne 0) { throw 'Docker command failed. No data was deleted.' }
}

try {
    $manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release-manifest.json') -Raw | ConvertFrom-Json
    $files = @{
        EVALUATION_RUNTIME_CREDENTIALS_PATH = 'runtime.json'
        EVALUATION_POSTGRES_CREDENTIALS_PATH = 'postgres.secret'
        EVALUATION_USER_CREDENTIALS_PATH = 'user.json'
        EVALUATION_REVIEWER_CREDENTIALS_PATH = 'reviewer.json'
    }
    foreach ($name in $files.Keys) {
        $path = Join-Path $CredentialsDirectory $files[$name]
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required external credential file is missing: $($files[$name])"
        }
        [Environment]::SetEnvironmentVariable($name, $path, 'Process')
    }
    foreach ($property in $manifest.images.PSObject.Properties) {
        [Environment]::SetEnvironmentVariable($property.Name, $property.Value, 'Process')
    }
    $env:DEEPSEEK_PROXY_URL = $DeepSeekProxy
    $env:BGE_BASE_URL = 'http://host.docker.internal:8080/v1'
    $env:BGE_EMBEDDING_MODEL = 'bge-m3'
    $env:BGE_RERANKER_MODEL = 'bge-reranker-v2-m3'
    Invoke-DockerChecked -DockerArguments @('info', '--format', '{{.ServerVersion}}')
    $missing = $false
    foreach ($property in $manifest.images.PSObject.Properties) {
        & docker image inspect $property.Value --format '{{.Id}}' *> $null
        if ($LASTEXITCODE -ne 0) { $missing = $true }
    }
    if ($missing) {
        if ($CheckOnly) { throw 'Required images are missing. Run START.cmd to load the package images.' }
        $archive = Join-Path $PSScriptRoot 'images.tar'
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.images_sha256) {
            throw 'Image archive integrity check failed.'
        }
        Invoke-DockerChecked -DockerArguments @('load', '--input', $archive)
        foreach ($property in $manifest.images.PSObject.Properties) {
            Invoke-DockerChecked -DockerArguments @('image', 'inspect', $property.Value, '--format', '{{.Id}}')
        }
    }
    $compose = @('compose', '-p', 'opspilot', '-f', (Join-Path $PSScriptRoot 'docker-compose.yml'),
        '-f', (Join-Path $PSScriptRoot 'deploy/compose.evaluation.yml'),
        '-f', (Join-Path $PSScriptRoot 'deploy/compose.local-images.yml'))
    Invoke-DockerChecked -DockerArguments ($compose + @('config', '--quiet'))
    if ($CheckOnly) {
        Write-Host 'Package configuration, external credential paths and images verified.'
        exit 0
    }
    & docker inspect opspilot-postgres-1 --format '{{.Id}}' *> $null
    if ($LASTEXITCODE -ne 0) { throw 'An existing OpsPilot database is required; this is not a fresh installer.' }
    Invoke-DockerChecked -DockerArguments @('start', 'opspilot-postgres-1')
    $databaseReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        & docker exec opspilot-postgres-1 pg_isready -U opspilot -d opspilot *> $null
        if ($LASTEXITCODE -eq 0) { $databaseReady = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $databaseReady) { throw 'The existing database did not become ready.' }
    $revision = & docker exec opspilot-postgres-1 psql -U opspilot -d opspilot -Atc 'SELECT version_num FROM alembic_version'
    if ($LASTEXITCODE -ne 0 -or ($revision -join '').Trim() -ne '0025_eval_msg_corr') {
        throw 'Database schema is incompatible. Expected 0025_eval_msg_corr; no migration was performed.'
    }
    # BGE data/models belong to the existing local installation, never to this package.
    & docker inspect opspilot-bge --format '{{.State.Running}}' *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Existing opspilot-bge service is required. See LOCAL-README.md.' }
    Invoke-DockerChecked -DockerArguments @('start', 'opspilot-bge')
    Invoke-DockerChecked -DockerArguments ($compose + @('up', '-d', '--no-build', '--wait', '--wait-timeout', '180'))
    Write-Host 'OpsPilot local application is ready: http://127.0.0.1:8088'
    if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8088' -WindowStyle Hidden }
} catch {
    Write-Error $_
    exit 1
}
