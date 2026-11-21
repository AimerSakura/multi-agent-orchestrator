[CmdletBinding()]
param(
    [string]$DshHost = "127.0.0.1",
    [int]$DshPort = 3080
)

$ErrorActionPreference = "Continue"
$results = [System.Collections.Generic.List[PSCustomObject]]::new()

function Add-CheckResult {
    param(
        [string]$Component,
        [ValidateSet("PASS", "WARN", "FAIL")]
        [string]$Status,
        [string]$Details
    )
    $results.Add([PSCustomObject]@{
        Component = $Component
        Status    = $Status
        Details   = $Details
    })
}

function Invoke-ProbeCommand {
    param(
        [string]$Command,
        [string[]]$Arguments
    )
    try {
        $output = & $Command @Arguments 2>&1 | Out-String
        return @{
            ExitCode = $LASTEXITCODE
            Output   = $output.Trim()
        }
    }
    catch {
        return @{
            ExitCode = -1
            Output   = $_.Exception.Message
        }
    }
}

function Test-TcpPort {
    param(
        [string]$TargetHost,
        [int]$Port,
        [int]$TimeoutMs = 1000
    )
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connectTask = $client.ConnectAsync($TargetHost, $Port)
        if ($connectTask.Wait($TimeoutMs) -and $client.Connected) {
            return $true
        }
        return $false
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

# 1. OS & PowerShell
$osDesc = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
Add-CheckResult -Component "OS / Shell" -Status "PASS" -Details "$osDesc | PowerShell $($PSVersionTable.PSVersion)"

# 2. Git
$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if ($gitCmd) {
    $gitRes = Invoke-ProbeCommand -Command "git" -Arguments @("--version")
    if ($gitRes.ExitCode -eq 0) {
        Add-CheckResult -Component "Git" -Status "PASS" -Details $gitRes.Output
    } else {
        Add-CheckResult -Component "Git" -Status "WARN" -Details $gitRes.Output
    }
} else {
    Add-CheckResult -Component "Git" -Status "FAIL" -Details "git not found in PATH"
}

# 3. uv & Python runtimes
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCmd) {
    $uvRes = Invoke-ProbeCommand -Command "uv" -Arguments @("--version")
    $pyRes = Invoke-ProbeCommand -Command "uv" -Arguments @("python", "list", "--only-installed")
    $pyLines = ($pyRes.Output -split "`r?`n" | Where-Object { $_ -and $_ -notmatch "^WARNING:" }) -join "; "
    if ($uvRes.ExitCode -eq 0) {
        Add-CheckResult -Component "uv" -Status "PASS" -Details $uvRes.Output
        Add-CheckResult -Component "Python (via uv)" -Status $(if ($pyLines) { "PASS" } else { "WARN" }) -Details $(if ($pyLines) { $pyLines } else { "No Python interpreters discovered by uv" })
    } else {
        Add-CheckResult -Component "uv" -Status "FAIL" -Details $uvRes.Output
    }
} else {
    Add-CheckResult -Component "uv" -Status "FAIL" -Details "uv not found in PATH"
}

# 4. Codex CLI & mcp-server -c flag
$codexCmd = Get-Command codex -ErrorAction SilentlyContinue
if ($codexCmd) {
    $codexVer = Invoke-ProbeCommand -Command "codex" -Arguments @("--version")
    $codexVerLine = ($codexVer.Output -split "`r?`n" | Where-Object { $_ -and $_ -notmatch "^WARNING:" } | Select-Object -Last 1)
    if ($codexVer.ExitCode -eq 0) {
        Add-CheckResult -Component "Codex CLI" -Status "PASS" -Details $codexVerLine
    } else {
        Add-CheckResult -Component "Codex CLI" -Status "WARN" -Details $codexVer.Output
    }

    $codexHelp = Invoke-ProbeCommand -Command "codex" -Arguments @("mcp-server", "--help")
    $supportsConfigFlag = ($codexHelp.ExitCode -eq 0) -and ($codexHelp.Output -match "-c,\s+--config")
    Add-CheckResult -Component "Codex MCP (-c model)" -Status $(if ($supportsConfigFlag) { "PASS" } else { "WARN" }) -Details $(if ($supportsConfigFlag) { "Supports -c/--config override (use -c model=`"luna`")" } else { "Could not confirm -c/--config flag" })
} else {
    Add-CheckResult -Component "Codex CLI" -Status "WARN" -Details "codex not found in PATH"
}

# 5. DeepSeek Harness (dsh) CLI
$dshCmd = Get-Command dsh -ErrorAction SilentlyContinue
if ($dshCmd) {
    $dshVer = Invoke-ProbeCommand -Command "dsh" -Arguments @("--version")
    if ($dshVer.ExitCode -eq 0) {
        Add-CheckResult -Component "dsh CLI" -Status "PASS" -Details "version $($dshVer.Output) ($($dshCmd.Source))"
    } else {
        Add-CheckResult -Component "dsh CLI" -Status "WARN" -Details $dshVer.Output
    }
} else {
    Add-CheckResult -Component "dsh CLI" -Status "WARN" -Details "dsh not found in PATH"
}

# 6. Local dsh HTTP Port (default 127.0.0.1:3080)
$portOpen = Test-TcpPort -TargetHost $DshHost -Port $DshPort -TimeoutMs 1000
Add-CheckResult -Component "dsh Web ($DshHost`:$DshPort)" -Status $(if ($portOpen) { "PASS" } else { "WARN" }) -Details $(if ($portOpen) { "Listening" } else { "Not listening (expected when dsh web is not running; headless CLI works standalone)" })

# 7. Optional DEEPSEEK_API_KEY
$hasApiKey = -not [string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)
Add-CheckResult -Component "DEEPSEEK_API_KEY" -Status $(if ($hasApiKey) { "PASS" } else { "WARN" }) -Details $(if ($hasApiKey) { "Configured (secret hidden)" } else { "Not set (only needed if enabling API fallback or API-key auth)" })

Write-Host ""
Write-Host "=== Multi-Agent MCP Orchestrator: Environment Check ===" -ForegroundColor Cyan
$results | Format-Table -AutoSize -Wrap
exit 0
