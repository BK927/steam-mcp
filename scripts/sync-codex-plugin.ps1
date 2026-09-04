[CmdletBinding()]
param(
  [ValidateSet("cloud", "local")]
  [string]$Profile = "cloud"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PluginId = "steam-mcp"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$UserDirectory = [Environment]::GetFolderPath("UserProfile")
$PluginsRoot = [IO.Path]::GetFullPath((Join-Path $UserDirectory "plugins"))
$SkillScripts = Join-Path $UserDirectory ".codex\skills\.system\plugin-creator\scripts"
$Validator = Join-Path $SkillScripts "validate_plugin.py"
$Cachebuster = Join-Path $SkillScripts "update_plugin_cachebuster.py"
$MarketplaceReader = Join-Path $SkillScripts "read_marketplace_name.py"

function Assert-PluginChildPath {
  param([Parameter(Mandatory = $true)][string]$Path)

  $root = [IO.Path]::GetFullPath($PluginsRoot).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
  )
  $candidate = [IO.Path]::GetFullPath($Path)
  $prefix = $root + [IO.Path]::DirectorySeparatorChar
  if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Plugin path escaped the expected plugins directory: $candidate"
  }
  return $candidate
}

function Remove-SafePluginDirectory {
  param([Parameter(Mandatory = $true)][string]$Path)

  $safePath = Assert-PluginChildPath -Path $Path
  if (Test-Path -LiteralPath $safePath) {
    Remove-Item -LiteralPath $safePath -Recurse -Force
  }
}

function Invoke-PluginPython {
  param(
    [Parameter(Mandatory = $true)][string]$Script,
    [string[]]$Arguments = @()
  )

  & uv run --quiet --no-project --with pyyaml python $Script @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "Plugin helper failed: $Script"
  }
}

foreach ($path in @($Validator, $Cachebuster, $MarketplaceReader)) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Required plugin-creator helper was not found: $path"
  }
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv is required to run the Codex plugin validation helpers."
}
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
  throw "codex is required to install the synchronized plugin."
}

Invoke-PluginPython -Script $Validator -Arguments @($ProjectRoot)
$MarketplaceName = (& uv run --quiet --no-project --with pyyaml python $MarketplaceReader).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($MarketplaceName)) {
  throw "Could not read the personal marketplace name."
}

New-Item -ItemType Directory -Path $PluginsRoot -Force | Out-Null
$nonce = [Guid]::NewGuid().ToString("N")
$Destination = Assert-PluginChildPath -Path (Join-Path $PluginsRoot $PluginId)
$Staging = Assert-PluginChildPath -Path (Join-Path $PluginsRoot ".$PluginId.staging.$nonce")
$Backup = Assert-PluginChildPath -Path (Join-Path $PluginsRoot ".$PluginId.backup.$nonce")
$movedExisting = $false

try {
  New-Item -ItemType Directory -Path (Join-Path $Staging ".codex-plugin") -Force | Out-Null
  Copy-Item -LiteralPath (Join-Path $ProjectRoot ".codex-plugin\plugin.json") `
    -Destination (Join-Path $Staging ".codex-plugin\plugin.json")
  Copy-Item -LiteralPath (Join-Path $ProjectRoot "icon.png") `
    -Destination (Join-Path $Staging "icon.png")

  if ($Profile -eq "cloud") {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot ".mcp.json") `
      -Destination (Join-Path $Staging ".mcp.json")
  }
  else {
    $config = [ordered]@{
      mcpServers = [ordered]@{
        $PluginId = [ordered]@{
          type = "stdio"
          command = "uv"
          args = @("run", "--directory", $ProjectRoot, "python", "-m", "steam_mcp.server")
        }
      }
    }
    $config | ConvertTo-Json -Depth 8 | Set-Content `
      -LiteralPath (Join-Path $Staging ".mcp.json") -Encoding utf8
  }

  $expectedFiles = @(".codex-plugin/plugin.json", ".mcp.json", "icon.png") | Sort-Object
  $actualFiles = @(
    Get-ChildItem -LiteralPath $Staging -Recurse -File | ForEach-Object {
      [IO.Path]::GetRelativePath($Staging, $_.FullName).Replace("\", "/")
    }
  ) | Sort-Object
  if (@(Compare-Object -ReferenceObject $expectedFiles -DifferenceObject $actualFiles).Count -ne 0) {
    throw "The staged plugin must contain only plugin.json and one .mcp.json."
  }
  if (Test-Path -LiteralPath (Join-Path $Staging "skills")) {
    throw "The staged plugin must not contain a skills directory."
  }
  if (Test-Path -LiteralPath (Join-Path $Staging "servers")) {
    throw "The staged plugin must not contain a servers directory."
  }

  $written = Get-Content -LiteralPath (Join-Path $Staging ".mcp.json") -Raw | ConvertFrom-Json
  $servers = @($written.mcpServers.PSObject.Properties)
  if ($servers.Count -ne 1 -or $servers[0].Name -ne $PluginId) {
    throw "The synchronized plugin must contain exactly the '$PluginId' MCP server."
  }

  Invoke-PluginPython -Script $Cachebuster -Arguments @($Staging)
  Invoke-PluginPython -Script $Validator -Arguments @($Staging)

  if (Test-Path -LiteralPath $Destination) {
    Move-Item -LiteralPath $Destination -Destination $Backup
    $movedExisting = $true
  }
  try {
    Move-Item -LiteralPath $Staging -Destination $Destination
  }
  catch {
    if ($movedExisting -and -not (Test-Path -LiteralPath $Destination)) {
      Move-Item -LiteralPath $Backup -Destination $Destination
      $movedExisting = $false
    }
    throw
  }
  if ($movedExisting) {
    Remove-SafePluginDirectory -Path $Backup
    $movedExisting = $false
  }
}
finally {
  if (Test-Path -LiteralPath $Staging) {
    Remove-SafePluginDirectory -Path $Staging
  }
  if ($movedExisting -and -not (Test-Path -LiteralPath $Destination) -and (Test-Path -LiteralPath $Backup)) {
    Move-Item -LiteralPath $Backup -Destination $Destination
  }
}

& codex plugin add "$PluginId@$MarketplaceName"
if ($LASTEXITCODE -ne 0) {
  throw "Codex plugin installation failed."
}

Write-Host "Synchronized $PluginId with the '$Profile' profile." -ForegroundColor Green
Write-Host "Start a new Codex task to load the updated MCP surface."
