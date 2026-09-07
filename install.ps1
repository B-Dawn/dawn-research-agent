#Requires -Version 5.1
<#
    科研智能体 - Windows 安装器
    把 skills/research-agent 安装到 %USERPROFILE%\.workbuddy\skills\research-agent，
    并把科研画像模板复制到 %USERPROFILE%\.workbuddy\profile\。

    中文说明：本文件以 UTF-8 with BOM 保存，Windows PowerShell 5.1 才能正确读取中文。
    如果直接用记事本另存，务必保持 UTF-8 BOM 编码，否则中文会变乱码。
#>

param(
    [switch]$Uninstall,
    [switch]$NoProfile,
    [switch]$RunDoctor,
    [string]$Target = ""
)

$ErrorActionPreference = "Stop"

function Write-Step { param($msg) Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err  { param($msg) Write-Host "[X] $msg" -ForegroundColor Red }

$SourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillSrc   = Join-Path $SourceRoot "skills\research-agent"
$ProfileSrc = Join-Path $SourceRoot "profile"

if ([string]::IsNullOrWhiteSpace($Target)) {
    $Target = Join-Path $env:USERPROFILE ".workbuddy"
}
$SkillsDir   = Join-Path $Target "skills"
$SkillDest   = Join-Path $SkillsDir "research-agent"
$ProfileDest = Join-Path $Target "profile"

Write-Host ""
Write-Host "==============================================" -ForegroundColor DarkGray
Write-Host " 科研智能体 Research Agent  v1.0.0" -ForegroundColor White
Write-Host "==============================================" -ForegroundColor DarkGray
Write-Host ""
Write-Host "源目录   : $SourceRoot" -ForegroundColor Gray
Write-Host "安装目标 : $Target" -ForegroundColor Gray
Write-Host ""

# ---------------------------------------------------------------- 卸载
if ($Uninstall) {
    Write-Step "卸载科研智能体"
    if (Test-Path $SkillDest) {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $backup = "$SkillDest.bak-$stamp"
        Move-Item -Path $SkillDest -Destination $backup
        Write-Ok "已备份并移除: $backup"
    } else {
        Write-Warn "未找到已安装目录，跳过: $SkillDest"
    }
    Write-Host ""
    Write-Host "卸载完成。画像文件保留在: $ProfileDest" -ForegroundColor Gray
    exit 0
}

# ---------------------------------------------------------------- 前置检查
if (-not (Test-Path $SkillSrc)) {
    Write-Err "找不到 skills\research-agent 目录: $SkillSrc"
    Write-Host "请确认 install.ps1 与 skills 目录在同一层。" -ForegroundColor Gray
    exit 1
}
if (-not (Test-Path (Join-Path $SkillSrc "SKILL.md"))) {
    Write-Err "安装包不完整，缺少 SKILL.md"
    exit 1
}

# ---------------------------------------------------------------- 查找 Python
Write-Step "检测 Python 环境"
$python = $null
$pythonNote = ""
foreach ($cmd in @("python", "py", "python3")) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) {
        try {
            $ver = & $cmd -c "import sys;print('%d.%d.%d' % sys.version_info[:3])" 2>$null
            if ($ver -match "^(\d+)\.(\d+)\.(\d+)$") {
                $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 7) {
                    $python = $found.Source
                    $pythonNote = "$cmd  $ver"
                    break
                }
            }
        } catch { }
    }
}
if ($python -and $RunDoctor) {
    Write-Step "运行环境自检"
    Push-Location (Join-Path $SkillDest "scripts")
    try { & $python "ra.py" "doctor" } finally { Pop-Location }
} elseif ($python) {
    Write-Host "      自检可手动运行：cd `"$SkillDest\scripts`" ; python ra.py doctor" -ForegroundColor DarkGray
}
exit 0
