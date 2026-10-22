# One-command setup for tableau-builder on Windows (safe to re-run). EXPERIMENTAL: not yet confirmed on a
# Windows machine. Building workbooks is plain Python; live validation (open_in_tableau) is the untested part.
#   powershell -ExecutionPolicy Bypass -File install.ps1      (works from PowerShell, cmd and Git Bash alike)
# Kept Windows PowerShell 5.1 compatible: no pipeline chain operators, no ternary operator, no $IsWindows outside PowerShell 7.
$ErrorActionPreference = 'Stop'
$Dir = $PSScriptRoot

function Say([string]$Text) { Write-Host $Text -ForegroundColor Cyan }
function Warn([string]$Text) { Write-Host "! $Text" -ForegroundColor Yellow }

# Native programs do not throw on a non-zero exit code, and under 'Stop' Windows PowerShell 5.1 turns any
# stderr line of a redirected native program into a terminating error. So run them under 'Continue' and
# check $LASTEXITCODE instead.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$AllowFailure, [switch]$Quiet)
    $ErrorActionPreference = 'Continue'
    if ($Quiet) { & $Exe @Arguments 2>&1 | Out-Null } else { & $Exe @Arguments }
    $code = $LASTEXITCODE
    if (($code -ne 0) -and (-not $AllowFailure)) {
        throw "'$Exe $($Arguments -join ' ')' failed with exit code $code"
    }
}

Say '1/5 Checking prerequisites'
if ($PSVersionTable.PSEdition -eq 'Core') {
    if (-not $IsWindows) { throw 'install.ps1 is for Windows. On macOS run ./install.sh instead.' }
}
Warn 'Windows support is EXPERIMENTAL: building workbooks works; live validation (open_in_tableau) has not been confirmed on Windows yet.'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say '   installing uv (https://docs.astral.sh/uv/)'
    Invoke-Native 'powershell' @('-ExecutionPolicy', 'ByPass', '-c', 'irm https://astral.sh/uv/install.ps1 | iex')
    # The installer updates the user PATH for new shells only: add its install folders (and the registry PATHs)
    # to this session's PATH as well. Keep the session's own entries: claude may only be on those.
    $env:Path = @(
        (Join-Path $HOME '.local\bin'),
        (Join-Path $HOME '.cargo\bin'),
        $env:Path,
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        [Environment]::GetEnvironmentVariable('Path', 'Machine')
    ) -join ';'
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv is required: https://docs.astral.sh/uv/' }

# Same search as find_windows_app() in tabmcp/validate.py: the same exe names, newest version first (numeric,
# so 2026.10 beats 2026.2). server.py --check in step 3 prints the path the server itself will use.
$tableau = $null
$best = $null
foreach ($root in @($env:ProgramFiles, $env:ProgramW6432, ${env:ProgramFiles(x86)})) {
    if (-not $root) { continue }
    $dirs = Get-ChildItem -LiteralPath (Join-Path $root 'Tableau') -Directory -Filter 'Tableau Public*' -ErrorAction SilentlyContinue
    foreach ($d in $dirs) {
        $exe = $null
        foreach ($name in @('tabpublic.exe', 'tableaupublic.exe', 'tableau.exe')) {
            $candidate = Join-Path (Join-Path $d.FullName 'bin') $name
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $exe = $candidate; break }
        }
        if (-not $exe) { continue }
        $ver = ($d.Name -replace '^\D*', '') -as [version]
        if (-not $ver) { $ver = [version]'0.0' }
        if (($null -eq $best) -or ($ver -gt $best)) { $best = $ver; $tableau = $exe }
    }
}
if ($tableau) {
    Write-Host "   Tableau Public: $tableau"
} else {
    Warn 'Tableau Public not found under Program Files. Install it (free) from https://public.tableau.com/app/discover to open and validate workbooks. Building still works without it.'
}

Say '2/5 Self-test (stdlib only)'
Invoke-Native 'uv' @('run', '--quiet', '--no-project', 'python', (Join-Path $Dir 'test_tableau_mcp.py'))

Say '3/5 Resolving server dependencies (mcp, tableauhyperapi) - first run downloads ~100 MB'
$server = Join-Path $Dir 'server.py'
Invoke-Native 'uv' @('run', '--quiet', '--script', $server, '--check')

Say '4/5 Registering the MCP server with Claude Code'
# Use claude.exe / claude.cmd, never npm's claude.ps1 shim (a script can swallow the '--' separator) or its
# extensionless sh script.
$claude = Get-Command claude -All -ErrorAction SilentlyContinue |
    Where-Object { ($_.CommandType -eq 'Application') -and ($_.Source -match '\.(exe|cmd|bat)$') } |
    Select-Object -First 1
if ($claude) {
    Invoke-Native $claude.Source @('mcp', 'remove', 'tableau-builder', '--scope', 'user') -AllowFailure -Quiet
    Invoke-Native $claude.Source @('mcp', 'add', '--scope', 'user', 'tableau-builder', '--', 'uv', 'run', '--quiet', '--script', $server)
} else {
    Warn 'claude CLI not found. Add this MCP server to your client manually:'
    Write-Host "   command: uv   args: run --quiet --script $server"
}

Say '5/5 Installing the tableau-dashboard skill'
$skills = Join-Path $HOME '.claude\skills'
New-Item -ItemType Directory -Force -Path $skills | Out-Null
$link = Join-Path $skills 'tableau-dashboard'
$src = Join-Path $Dir 'skills\tableau-dashboard'
$old = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
if ($old) {
    if ($old.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        # Delete() removes the junction itself. Never Remove-Item -Recurse here: in Windows PowerShell 5.1
        # that can delete the files the junction points to (this repo's skill).
        $old.Delete()
    } else {
        Remove-Item -LiteralPath $link -Recurse -Force    # a copy from an earlier run
    }
}
try {
    New-Item -ItemType Junction -Path $link -Target $src | Out-Null
    Write-Host "   linked $link -> $src"
} catch {
    Copy-Item -LiteralPath $src -Destination $link -Recurse
    Write-Host "   copied $src to $link (a junction was not possible; re-run install.ps1 after updating the repo)"
}

Say 'Done. Restart Claude Code, then ask: "Build a Tableau dashboard from <path\to\data.csv>".'
