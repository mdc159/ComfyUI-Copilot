param(
    [string]$Standalone = 'D:\ComfyUI_windows_portable',
    [string]$ServerUrl = 'http://127.0.0.1:8188'
)
$ErrorActionPreference = 'Stop'
$source = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$standaloneRoot = (Resolve-Path -LiteralPath $Standalone).Path
$nodesRoot = (Resolve-Path -LiteralPath (Join-Path $standaloneRoot 'ComfyUI\custom_nodes')).Path
$target = [IO.Path]::GetFullPath((Join-Path $nodesRoot 'ComfyUI-Copilot'))
$backupRoot = [IO.Path]::GetFullPath((Join-Path $standaloneRoot 'copilot-backups'))
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = [IO.Path]::GetFullPath((Join-Path $backupRoot "ComfyUI-Copilot-$stamp"))
$stage = [IO.Path]::GetFullPath((Join-Path $standaloneRoot "copilot-stage-$stamp"))
if (-not $target.StartsWith($nodesRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
    -not $backup.StartsWith($standaloneRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
    -not $stage.StartsWith($standaloneRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Installation paths escaped the standalone directory'
}
if (Test-Path -LiteralPath $stage) { throw 'Staging directory already exists' }
if (-not (Test-Path -LiteralPath (Join-Path $source 'dist\copilot_web\input.js'))) { throw 'Build the UI first' }
git clone --quiet --no-local -- $source $stage
if ($LASTEXITCODE -ne 0) { throw 'Could not stage the fork' }
$files = git -C $source -c core.quotepath=false ls-files -c -o --exclude-standard
if ($LASTEXITCODE -ne 0) { throw 'Could not enumerate source files' }
foreach ($relative in ($files | Select-Object -Unique)) {
    if ($relative -like 'outputs/*') { continue }
    $from = [IO.Path]::GetFullPath((Join-Path $source $relative))
    $to = [IO.Path]::GetFullPath((Join-Path $stage $relative))
    if (-not $to.StartsWith($stage + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid source file path' }
    if (Test-Path -LiteralPath $from -PathType Leaf) {
        New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null
        Copy-Item -LiteralPath $from -Destination $to -Force
    } elseif (Test-Path -LiteralPath $to -PathType Leaf) {
        Remove-Item -LiteralPath $to -Force
    }
}
$origin = git -C $source remote get-url origin
git -C $stage remote set-url origin $origin
git -C $stage remote add upstream https://github.com/ATH-MaaS/ComfyUI-Copilot.git
Push-Location (Join-Path $stage 'llm-runtime')
try {
    npm ci --omit=dev --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Provider dependency installation failed' }
} finally { Pop-Location }
foreach ($relative in @('.env', '.env.llm', 'db', 'backend\data')) {
    $old = Join-Path $target $relative
    if (Test-Path -LiteralPath $old) {
        $next = Join-Path $stage $relative
        if (Test-Path -LiteralPath $old -PathType Container) {
            New-Item -ItemType Directory -Force -Path $next | Out-Null
            Get-ChildItem -LiteralPath $old -Force | Copy-Item -Destination $next -Recurse -Force
        } else { Copy-Item -LiteralPath $old -Destination $next -Force }
    }
}
$queue = Invoke-RestMethod -Uri "$ServerUrl/queue" -TimeoutSec 10
if ($queue.queue_running.Count -or $queue.queue_pending.Count) { throw "Queue is busy; staged copy remains at $stage" }
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
$hadTarget = Test-Path -LiteralPath $target
if ($hadTarget) {
    # Some Windows hosts allow file writes but retain directory handles that prevent rename.
    robocopy $target $backup /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -gt 7) { throw 'Could not complete the backup; installation was not changed' }
}
robocopy $stage $target /E /COPY:DAT /XD .git /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
if ($LASTEXITCODE -gt 7) { throw "Copy failed. Restore the complete backup from $backup" }
if (-not $hadTarget) {
    robocopy (Join-Path $stage '.git') (Join-Path $target '.git') /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -gt 7) { throw 'Could not install fork metadata' }
}
git -C $target remote set-url origin $origin
$currentBranch = git -C $target branch --show-current
if ($currentBranch -ne 'codex/owned-llm-service') {
    git -C $target switch -c codex/owned-llm-service
    if ($LASTEXITCODE -ne 0) { throw 'Could not label the installed development branch' }
}
[ordered]@{ source=$source; revision=(git -C $source rev-parse HEAD); target=$target; backup=$backup;
    branch=(git -C $source branch --show-current); restartRequired=$true; installed=(Get-Date -Format o) } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $standaloneRoot 'copilot-installation.json')
Write-Output "Installed: $target"
Write-Output "Backup: $backup"
Write-Output 'Restart ComfyUI through Manager to load the replacement.'
