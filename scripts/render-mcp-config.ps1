[CmdletBinding()]
param(
    [string]$WorkspaceRoot = "",
    [string]$OutputPath = "",
    [string]$UvEnvDir = "",
    [string]$CodexModel = "gpt-6-luna"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    $WorkspaceRoot = $repoRoot
}
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $repoRoot "config\mcp-settings.local.json"
}
if ([string]::IsNullOrWhiteSpace($UvEnvDir)) {
    $UvEnvDir = Join-Path $env:LOCALAPPDATA "uv-envs\multi-agent-orchestrator"
}
$bridgeDir = Join-Path $repoRoot "packages\dsh-mcp-bridge"
$resolvedWorkspace = (Resolve-Path $WorkspaceRoot).Path

function Resolve-ExecutablePath {
    param(
        [string[]]$Candidates,
        [string]$Fallback
    )
    foreach ($name in $Candidates) {
        if ($name -and (Test-Path $name -PathType Leaf)) {
            return (Resolve-Path $name).Path
        }
        $cmd = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd -and $cmd.Source) {
            return $cmd.Source
        }
    }
    return $Fallback
}

function Resolve-CodexDesktopExe {
    $pkg = Get-AppxPackage -Name "OpenAI.Codex" -ErrorAction SilentlyContinue |
        Sort-Object Version -Descending |
        Select-Object -First 1
    if ($pkg -and $pkg.InstallLocation) {
        $candidate = Join-Path $pkg.InstallLocation "app\resources\codex.exe"
        if (Test-Path $candidate -PathType Leaf) {
            return $candidate
        }
    }

    $tomlPath = Join-Path $HOME ".codex\config.toml"
    if (Test-Path $tomlPath -PathType Leaf) {
        $match = Select-String -Path $tomlPath -Pattern "CODEX_CLI_PATH\s*=\s*'([^']+)'" | Select-Object -First 1
        if ($match) {
            $candidate = $match.Matches[0].Groups[1].Value
            if (Test-Path $candidate -PathType Leaf) {
                return $candidate
            }
        }
    }
    return $null
}

$desktopCodex = Resolve-CodexDesktopExe
$codexCandidates = @()
if ($desktopCodex) {
    $codexCandidates += $desktopCodex
}
$codexCandidates += @("codex.exe", "codex.cmd", "codex")

$codexCommand = Resolve-ExecutablePath -Candidates $codexCandidates -Fallback "codex"
$uvCommand = Resolve-ExecutablePath -Candidates @("uv.exe", "uv") -Fallback "uv"

$payload = [ordered]@{
    mcpServers = [ordered]@{
        "codex-worker" = [ordered]@{
            command = $uvCommand
            args    = @(
                "run",
                "--no-sync",
                "--directory",
                $bridgeDir,
                "python",
                "-m",
                "dsh_mcp_bridge.codex_server"
            )
            env     = [ordered]@{
                CODEX_BIN              = $codexCommand
                CODEX_DEFAULT_MODEL    = $CodexModel
                CODEX_ALLOWED_MODELS   = "gpt-6-luna,gpt-5.6-luna"
                CODEX_ALLOW_ESCALATION = "false"
                CODEX_ALLOWED_ROOT     = $resolvedWorkspace
                CODEX_DEFAULT_TIMEOUT  = "300"
                UV_PROJECT_ENVIRONMENT = $UvEnvDir
            }
        }
        "deepseek-harness" = [ordered]@{
            command = $uvCommand
            args    = @(
                "run",
                "--no-sync",
                "--directory",
                $bridgeDir,
                "python",
                "-m",
                "dsh_mcp_bridge.server"
            )
            env     = [ordered]@{
                DSH_BASE_URL           = "http://127.0.0.1:3080"
                DSH_ALLOWED_ROOT       = $resolvedWorkspace
                DSH_DEFAULT_TIMEOUT    = "300"
                DSH_APPROVAL_POLICY    = "never"
                UV_PROJECT_ENVIRONMENT = $UvEnvDir
            }
        }
    }
}

$outDir = Split-Path -Parent $OutputPath
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}

$json = $payload | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($OutputPath, $json + "`n", [System.Text.UTF8Encoding]::new($false))

Write-Host "Generated local MCP config: $OutputPath" -ForegroundColor Green
Write-Host "Resolved Codex binary     : $codexCommand" -ForegroundColor Gray
Write-Host ""
Write-Host "Merge the 'mcpServers' entries into your client configuration:" -ForegroundColor Cyan
Write-Host "  - Antigravity : $HOME\.gemini\config\mcp_config.json"
Write-Host "  - Gemini CLI  : $HOME\.gemini\settings.json"
Write-Host "  - Antigravity UI check: Additional Options (...) > MCP Servers"
