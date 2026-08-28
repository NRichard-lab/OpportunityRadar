[CmdletBinding()]
param(
    [string]$PortalRepo = 'C:\Users\dog10\OneDrive\Documents\ChatGPT\Blue Ash Release',
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$radarRepo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$portalRepoPath = (Resolve-Path -LiteralPath $PortalRepo).Path
$composeFile = Join-Path $radarRepo 'compose.phase3.yaml'
$portalMigration = Join-Path $portalRepoPath 'backend\migrations\versions\20260827_0006_opportunity_radar_production_integration.py'
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$phase3Root = Join-Path $tempBase ('opportunity-radar-phase3-' + [guid]::NewGuid().ToString('N'))
$projectName = 'opportunity-radar-phase3-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
$environmentNames = @(
    'BLUEASH_PORTAL_REPO', 'PHASE3_ROOT', 'COMPOSE_PROJECT_NAME',
    'BLUEASH_AUTH_CLIENT_SECRET', 'PHASE3_PORTAL_SECRET_KEY',
    'PHASE3_PORTAL_SESSION_SECRET', 'PHASE3_EMAIL_ENCRYPTION_KEY',
    'PHASE3_RADAR_SECRET_KEY', 'PHASE3_SYNTHETIC_PASSWORD',
    'PHASE3_SYNTHETIC_MFA_CODE', 'PHASE3_SYNTHETIC_TEST_MODE',
    'PHASE3_BROWSER_CDP_PORT', 'PHASE3_BROWSER_CDP_URL',
    'PLAYWRIGHT_BROWSERS_PATH', 'PYTHONPYCACHEPREFIX', 'NO_PROXY', 'no_proxy'
)
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

function New-Phase3Secret {
    $bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    return [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')
}

function Get-EphemeralLoopbackPort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]]$ComposeArguments)
    & docker compose --file $composeFile @ComposeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed: $($ComposeArguments -join ' ')"
    }
}

function Assert-Phase3PortIsolation {
    $renderedJson = & docker compose --file $composeFile --profile tools config --format json
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to inspect the rendered Phase 3 Compose configuration.'
    }
    $rendered = $renderedJson | ConvertFrom-Json
    foreach ($serviceProperty in $rendered.services.PSObject.Properties) {
        $portsProperty = $serviceProperty.Value.PSObject.Properties['ports']
        $ports = @()
        if ($null -ne $portsProperty) {
            $ports = @($portsProperty.Value)
        }
        if ($serviceProperty.Name -eq 'browser') {
            if ($ports.Count -ne 1 -or
                $ports[0].host_ip -ne '127.0.0.1' -or
                $ports[0].target -ne 9222 -or
                $ports[0].published -ne $env:PHASE3_BROWSER_CDP_PORT) {
                throw 'The browser CDP port must be the only publication and must bind the selected loopback port.'
            }
        }
        elseif ($ports.Count -ne 0) {
            throw "Phase 3 service $($serviceProperty.Name) unexpectedly publishes a host port."
        }
    }
    if (-not $rendered.networks.'phase3-private'.internal) {
        throw 'The Phase 3 application network must remain internal.'
    }
    foreach ($serviceProperty in $rendered.services.PSObject.Properties) {
        $controlNetwork = $serviceProperty.Value.networks.PSObject.Properties['phase3-control']
        if ($serviceProperty.Name -eq 'browser') {
            if ($null -eq $controlNetwork) {
                throw 'The browser must attach to the isolated host-control bridge for loopback CDP.'
            }
        }
        elseif ($null -ne $controlNetwork) {
            throw "Phase 3 service $($serviceProperty.Name) unexpectedly attaches to the host-control bridge."
        }
    }
}

function Invoke-Python {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string[]]$PythonArguments
    )
    & $Python @PythonArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python failed: $($PythonArguments -join ' ')"
    }
}

function Invoke-PortalTool {
    param([Parameter(Mandatory)][string[]]$ToolArguments)
    Invoke-Compose -ComposeArguments (@('--profile', 'tools', 'run', '--rm', 'portal-tool') + $ToolArguments)
}

function Remove-Phase3Root {
    param([Parameter(Mandatory)][string]$Target)
    $resolved = [IO.Path]::GetFullPath($Target)
    $leaf = Split-Path -Leaf $resolved
    if (-not $resolved.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -or
        -not $leaf.StartsWith('opportunity-radar-phase3-', [StringComparison]::Ordinal)) {
        throw "Refusing to remove unexpected integration path: $resolved"
    }
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

if (-not (Test-Path -LiteralPath $portalMigration -PathType Leaf)) {
    throw "Portal migration 0006 is missing from $portalRepoPath"
}
if (-not (Test-Path -LiteralPath (Join-Path $radarRepo 'scripts\create_synthetic_database.py') -PathType Leaf)) {
    throw "Radar synthetic database generator is missing."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required for the isolated Phase 3 stack."
}

$pythonCandidates = @(
    (Join-Path $radarRepo '.codex-venv\Scripts\python.exe'),
    (Join-Path $radarRepo '.venv\Scripts\python.exe')
)
$python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction Stop
    $python = $pythonCommand.Source
}

$env:BLUEASH_PORTAL_REPO = $portalRepoPath
$env:PHASE3_ROOT = $phase3Root
$env:COMPOSE_PROJECT_NAME = $projectName
$env:BLUEASH_AUTH_CLIENT_SECRET = New-Phase3Secret
$env:PHASE3_PORTAL_SECRET_KEY = New-Phase3Secret
$env:PHASE3_PORTAL_SESSION_SECRET = New-Phase3Secret
$env:PHASE3_EMAIL_ENCRYPTION_KEY = New-Phase3Secret
$env:PHASE3_RADAR_SECRET_KEY = New-Phase3Secret
$env:PHASE3_SYNTHETIC_PASSWORD = 'phase3-Synthetic-Only-Password!'
$env:PHASE3_SYNTHETIC_MFA_CODE = '246810'
$env:PHASE3_SYNTHETIC_TEST_MODE = '1'
$env:PHASE3_BROWSER_CDP_PORT = [string](Get-EphemeralLoopbackPort)
$env:PHASE3_BROWSER_CDP_URL = "http://127.0.0.1:$($env:PHASE3_BROWSER_CDP_PORT)"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $radarRepo '.playwright-browsers'
$env:PYTHONPYCACHEPREFIX = Join-Path $phase3Root 'pycache'
$localNoProxy = '127.0.0.1,localhost,blueashdigital.tech,api.blueashdigital.tech,radar.blueashdigital.tech'
$env:NO_PROXY = $localNoProxy
$env:no_proxy = $localNoProxy

$stackWasStarted = $false
try {
    New-Item -ItemType Directory -Path $phase3Root -Force | Out-Null
    foreach ($relativePath in @('database', 'data\imports', 'exports', 'backups', 'logs')) {
        New-Item -ItemType Directory -Path (Join-Path $phase3Root $relativePath) -Force | Out-Null
    }

    Invoke-Python -Python $python -PythonArguments @(
        '-m', 'py_compile',
        (Join-Path $radarRepo 'tests\integration\phase3_radar_app.py'),
        (Join-Path $radarRepo 'tests\integration\phase3_browser_server.py'),
        (Join-Path $radarRepo 'tests\integration\test_phase3_handoff_e2e.py'),
        (Join-Path $portalRepoPath 'backend\tests\integration\phase3_app.py'),
        (Join-Path $portalRepoPath 'backend\tests\integration\seed_phase3.py'),
        (Join-Path $portalRepoPath 'backend\tests\integration\validate_phase3_schema.py')
    )
    Invoke-Compose -ComposeArguments @('--profile', 'tools', 'config', '--quiet')
    Assert-Phase3PortIsolation
    if ($ValidateOnly) {
        Write-Output 'Phase 3 integration static and Compose validation passed.'
        return
    }

    $databasePath = Join-Path $phase3Root 'database\opportunity_radar.db'
    Invoke-Python -Python $python -PythonArguments @(
        (Join-Path $radarRepo 'scripts\create_synthetic_database.py'),
        '--database', $databasePath
    )

    Invoke-Compose -ComposeArguments @('--profile', 'tools', 'build', 'portal-tool')
    $stackWasStarted = $true
    Invoke-Compose -ComposeArguments @('up', '-d', '--wait', '--wait-timeout', '90', 'portal-postgres')

    Invoke-PortalTool -ToolArguments @('alembic', 'upgrade', '20260825_0005')
    Invoke-PortalTool -ToolArguments @('python', 'tests/integration/validate_phase3_schema.py', '0005')
    Invoke-PortalTool -ToolArguments @('alembic', 'upgrade', '20260827_0006')
    Invoke-PortalTool -ToolArguments @('python', 'tests/integration/validate_phase3_schema.py', '0006')
    Invoke-PortalTool -ToolArguments @('alembic', 'downgrade', '20260825_0005')
    Invoke-PortalTool -ToolArguments @('python', 'tests/integration/validate_phase3_schema.py', '0005')
    Invoke-PortalTool -ToolArguments @('alembic', 'upgrade', '20260827_0006')
    Invoke-PortalTool -ToolArguments @('python', 'tests/integration/validate_phase3_schema.py', '0006')
    Invoke-PortalTool -ToolArguments @('python', 'tests/integration/seed_phase3.py')

    try {
        Invoke-Compose -ComposeArguments @(
            'up', '-d', '--build', '--wait', '--wait-timeout', '240',
            'portal-backend', 'portal-frontend',
            'opportunity-radar-backend', 'opportunity-radar-frontend', 'edge', 'browser'
        )
    }
    catch {
        & docker compose --file $composeFile logs --no-color --tail 100 opportunity-radar-backend browser
        throw
    }
    try {
        Invoke-Python -Python $python -PythonArguments @(
            '-m', 'unittest', 'discover', '-s', 'tests/integration',
            '-p', 'test_phase3_handoff_e2e.py', '-v'
        )
    }
    catch {
        & docker compose --file $composeFile logs --no-color --tail 200 portal-backend opportunity-radar-backend edge browser
        throw
    }
    Write-Output 'Phase 3 isolated migration and browser integration validation passed.'
}
finally {
    if ($stackWasStarted) {
        try {
            Invoke-Compose -ComposeArguments @('--profile', 'tools', 'down', '-v', '--remove-orphans')
        }
        catch {
            Write-Warning $_
        }
    }
    Remove-Phase3Root -Target $phase3Root
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
}
