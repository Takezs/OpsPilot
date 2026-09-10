param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$WebImage
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$output = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $output) { throw 'Output directory must not already exist.' }

$containers = [ordered]@{
    EVALUATION_IMAGE = 'opspilot-api-1'
    LOCAL_WEB_IMAGE = 'opspilot-web-1'
    LOCAL_ORDER_IMAGE = 'opspilot-order-1'
    LOCAL_PAYMENT_IMAGE = 'opspilot-payment-1'
    LOCAL_EMAIL_IMAGE = 'opspilot-email-1'
    LOCAL_POSTGRES_IMAGE = 'opspilot-postgres-1'
    LOCAL_REDIS_IMAGE = 'opspilot-redis-1'
}
$images = [ordered]@{}
foreach ($name in $containers.Keys) {
    $id = & docker inspect $containers[$name] --format '{{.Image}}'
    if ($LASTEXITCODE -ne 0 -or $id -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Cannot resolve a running image.' }
    $images[$name] = $id.Trim()
}
if ($WebImage) {
    $id = & docker image inspect $WebImage --format '{{.Id}}'
    if ($LASTEXITCODE -ne 0 -or $id -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Cannot resolve web image.' }
    $images.LOCAL_WEB_IMAGE = $id.Trim()
}
if ($images.EVALUATION_IMAGE -ne 'sha256:665b4e348c03fe3b35f9fc37eea14d83041dbef80a632308c0860643e2db89b9') {
    throw 'The running backend is not the verified local release image.'
}
$commit = & git -C $repository rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve source commit.' }
$trackedSecrets = & git -C $repository ls-files
if ($trackedSecrets | Where-Object { $_ -match '(^|/)(\.env|runtime\.json|postgres\.secret|user\.json|reviewer\.json)$' }) {
    throw 'A credential file is tracked; packaging stopped.'
}
New-Item -ItemType Directory -Path $output | Out-Null
New-Item -ItemType Directory -Path (Join-Path $output 'deploy') | Out-Null
foreach ($file in @('Start-OpsPilot.ps1', 'START.cmd', 'LOCAL-README.md')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $output
}
Copy-Item -LiteralPath (Join-Path $repository 'docker-compose.yml') -Destination $output
foreach ($file in @('compose.evaluation.yml', 'compose.local-images.yml')) {
    Copy-Item -LiteralPath (Join-Path $repository "deploy/$file") -Destination (Join-Path $output 'deploy')
}
$sourceArchive = Join-Path $output 'source.zip'
& git -C $repository archive --format=zip "--output=$sourceArchive" HEAD
if ($LASTEXITCODE -ne 0) { throw 'Source archive failed.' }
$archive = Join-Path $output 'images.tar'
$imageIds = @($images.Values | Select-Object -Unique)
& docker image save --output $archive @imageIds
if ($LASTEXITCODE -ne 0) { throw 'Image export failed.' }
$manifest = [ordered]@{
    release = 'OpsPilot-local-2026.09.10'
    scope = 'existing-local-installation'
    application_commit = '13e923b195a75aebc6d00f1a9c278bbe97f89832'
    package_source_commit = $commit.Trim()
    created_at_utc = [DateTime]::UtcNow.ToString('o')
    images = $images
    images_sha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    source_sha256 = (Get-FileHash -LiteralPath (Join-Path $output 'source.zip') -Algorithm SHA256).Hash.ToLowerInvariant()
    verification = @{ backend_passed = 589; backend_skipped = 4; frontend_unit = 19; frontend_e2e = 8; live_exact_citation = $true }
    final_evaluation_approved = $false
    credentials_included = $false
    database_volumes_included = $false
    bge_models_included = $false
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $output 'release-manifest.json') -Encoding UTF8
Write-Host "Package created: $output"
