[CmdletBinding()]
param(
    [string]$WorkspaceRoot = "",
    [string]$OutputPath = "",
    [string]$UvEnvDir = ""
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
    $UvEnvDir = Join-Path $env:LOCALAPPDATA "uv-envs\dsh-mcp-bridge"
}
$bridgeDir = Join-Path $repoRoot "packages\dsh-mcp-bridge"
$resolvedWorkspace = (Resolve-Path $WorkspaceRoot).Path

function Resolve-ExecutablePath {
    param(
        [string[]]$Candidates,
        [string]$Fallback
    )
    foreach ($name in $Candidates) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd -and $cmd.Source) {
            return $cmd.Source
        }
    }
    return $Fallback
}

$codexCommand = Resolve-ExecutablePath -Candidates @("codex.cmd", "codex.exe", "codex") -Fallback "codex"
$uvCommand = Resolve-ExecutablePath -Candidates @("uv.exe", "uv") -Fallback "uv"

$payload = [ordered]@{
    mcpServers = [ordered]@{
        "codex-worker" = [ordered]@{
            command = $codexCommand
            args    = @(
                "mcp-server",
                "-c",
                'model="luna"'
            )
        }
        "deepseek-harness" = [ordered]@{
            command = $uvCommand
            args    = @(
                "run",
                "--directory",
                $bridgeDir,
                "dsh-mcp-bridge"
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
Write-Host ""
Write-Host "Merge the 'mcpServers' entries into your client configuration:" -ForegroundColor Cyan
Write-Host "  - Antigravity : $HOME\.gemini\config\mcp_config.json"
Write-Host "  - Gemini CLI  : $HOME\.gemini\settings.json"
Write-Host "  - Antigravity UI check: Additional Options (...) > MCP Servers"
