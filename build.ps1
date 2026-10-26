param([string]$Python = "python", [string]$BuildRoot = "", [string]$DistRoot = "")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if (-not $BuildRoot) { $BuildRoot = Join-Path $PSScriptRoot "build" }
if (-not $DistRoot) { $DistRoot = Join-Path $PSScriptRoot "dist" }
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
$TestLog = Join-Path $BuildRoot "source-checks.log"
# Windows PowerShell 5 会把 unittest 的正常 stderr 当作错误记录，局部放行后检查进程退出码。
$SavedErrorPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    & $Python -m unittest discover -s tests -v 2>&1 | Tee-Object -FilePath $TestLog
    $TestExitCode = $LASTEXITCODE
} finally { $ErrorActionPreference = $SavedErrorPreference }
if ($TestExitCode -ne 0) { throw "Tests failed" }
& $Python -m PyInstaller --noconfirm --clean --onedir --windowed --name ScreenQR --manifest (Join-Path $PSScriptRoot "app.manifest") --icon (Join-Path $PSScriptRoot "assets/screenqr.ico") --add-data "$PSScriptRoot/assets;assets" --exclude-module numpy --paths $PSScriptRoot --distpath $DistRoot --workpath $BuildRoot --specpath $BuildRoot main.py
if ($LASTEXITCODE -ne 0) { throw "Build failed" }
$PackageRoot = Join-Path $DistRoot "ScreenQR"
Copy-Item -LiteralPath README.md,LICENSE,THIRD_PARTY_NOTICES.md -Destination $PackageRoot
Copy-Item -LiteralPath licenses -Destination (Join-Path $PackageRoot "licenses") -Recurse -Force
$PublicDocs = @("架构与验收.md", "0.9.0-P1-环境适配.md", "0.10.0-P2-P3-功能与页面.md", "0.10.0-build-info.json", "0.10.1-发布候选验收.md", "build-info.json", "release-acceptance.json", "免费分发与签名方案.md", "发布前总清单.md", "开源发布检查.md", "Windows兼容性自测.md", "微信二维码分类与测试矩阵.md", "微信二维码全景与测试码生成能力-2026-10-25.md")
New-Item -ItemType Directory -Path (Join-Path $PackageRoot "docs") -Force | Out-Null
foreach ($Document in $PublicDocs) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "docs/$Document") -Destination (Join-Path $PackageRoot "docs/$Document") -Force
}
New-Item -ItemType Directory -Path (Join-Path $PackageRoot "tools") -Force | Out-Null
Copy-Item -LiteralPath tools/probe_environment.py,tools/环境检查.cmd -Destination (Join-Path $PackageRoot "tools")
& $Python (Join-Path $PSScriptRoot "tools/generate_qr_samples.py") --output (Join-Path $PackageRoot "测试样本")
if ($LASTEXITCODE -ne 0) { throw "Public format sample generation failed" }
& $Python (Join-Path $PSScriptRoot "tools/generate_size_samples.py") --output (Join-Path $PackageRoot "大小测试样本")
if ($LASTEXITCODE -ne 0) { throw "Public size sample generation failed" }
& $Python (Join-Path $PSScriptRoot "tools/build_metadata.py") $PackageRoot --test-log $TestLog
if ($LASTEXITCODE -ne 0) { throw "Build metadata or frozen source verification failed" }
& $Python (Join-Path $PSScriptRoot "tools/package_checksums.py") $PackageRoot
if ($LASTEXITCODE -ne 0) { throw "Package checksum generation failed" }
Write-Host "Portable app ready: $(Join-Path $PackageRoot 'ScreenQR.exe')"
