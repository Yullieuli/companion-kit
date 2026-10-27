# companion-kit 一行安装（Windows / PowerShell）
#
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Yullieuli/companion-kit/main/install.ps1 | iex"
#
# 它会自动做完这些，不需要你手动做任何一步：
#   1. 找 Python 3.10+；找不到就下一份 uv（单文件），用 uv 拉 3.12
#   2. 建独立 venv（在 %LOCALAPPDATA%\companion\venv，不碰系统 Python）
#   3. 装 companion-kit（从 Release 的 whl，不需要 git；失败再回退 git+）
#   4. 初始化 store（%USERPROFILE%\.companion）
#   5. 把 companion 写进「用户 PATH」并设置 COMPANION_HOME（不用管理员，重启终端生效）
#   6. 如果机器上装了 dsh，顺手注册插件
#
# 参数：
#   -Store <dir>      store 目录（默认 ~\.companion）
#   -Python <path>   指定解释器，跳过自动查找
#   -NoDsh           不自动注册 DSH 插件
#   -DryRun          只打印将要做的事

param(
  [string]$Store = "$env:USERPROFILE\.companion",
  [string]$Python = "",
  [switch]$NoDsh,
  [switch]$DryRun,
  [switch]$NoEnv
)

$ErrorActionPreference = "Stop"
$Repo   = "https://github.com/Yullieuli/companion-kit"
$Ver    = "v0.0.1"
$Wheel  = "$Repo/releases/download/$Ver/companion_kit-0.0.1-py3-none-any.whl"
$Root   = Join-Path $env:LOCALAPPDATA "companion"
$Bin    = Join-Path $Root "bin"
$Venv   = Join-Path $Store "venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

function Say($m) { Write-Host "  $m" }

Write-Host "== companion-kit ==" -ForegroundColor Cyan
Say "store : $Store"
Say "venv  : $Venv"

# ── 1. 找 Python ────────────────────────────────────────────
function Test-Py($exe) {
  if (-not $exe) { return $false }
  try {
    $v = & $exe -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
    return ($LASTEXITCODE -eq 0 -and [int]$v -ge 310)
  } catch { return $false }
}

$py = ""
if ($Python -and (Test-Py $Python)) { $py = $Python }
if (-not $py) {
  foreach ($c in @("python", "python3", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Py $cmd.Source)) { $py = $cmd.Source; break }
  }
}

if (-not $py) {
  Say "没找到 Python 3.10+，改用 uv（单文件，不需要管理员）"
  if ($DryRun) { Say "[dry] 下载 uv → $Bin" } else {
    New-Item -ItemType Directory -Force -Path $Bin | Out-Null
    $uvExe = Join-Path $Bin "uv.exe"
    if (-not (Test-Path $uvExe)) {
      $zip = Join-Path $env:TEMP "uv-win.zip"
      $url = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
      $sysUv = Get-Command uv -ErrorAction SilentlyContinue
      if ($sysUv) {
        Say "用已有的 uv：$($sysUv.Source)"
        Copy-Item $sysUv.Source $uvExe -Force
      } else {
        Say "下载 uv（约 15MB，最多等 120 秒）…"
        try {
          Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 120
        } catch {
          throw "下载 uv 失败（网络问题）。两个办法：`n  1) 先手动装 Python 3.10+（python.org）再重跑本命令；`n  2) 开代理后重跑。原始错误：$($_.Exception.Message)"
        }
        Expand-Archive -Path $zip -DestinationPath $Bin -Force
        Remove-Item $zip -Force
      }
      $found = Get-ChildItem -Path $Bin -Filter uv.exe -Recurse | Select-Object -First 1
      if (-not $found) { throw "uv.exe 解压后没找到" }
      if ($found.FullName -ne $uvExe) { Move-Item $found.FullName $uvExe -Force }
    }
    Say "用 uv 拉 Python 3.12 …"
    & $uvExe python install 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv 装 Python 失败" }
    $uvPy = (& $uvExe python find 3.12) 2>$null
    if (-not $uvPy) { throw "uv 装完但找不到解释器" }
    $py = $uvPy
  }
}
Say "python: $(if ($py) { $py } else { '(dry)' })"

# ── 2. venv ────────────────────────────────────────────────
if ($DryRun) { Say "[dry] 建 venv：$Venv" }
else {
  if (-not (Test-Path $VenvPy)) {
    & $py -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "建 venv 失败（$py）" }
  }
}

# ── 3. 装包 ────────────────────────────────────────────────
if ($DryRun) { Say "[dry] pip install $Wheel" }
else {
  & $VenvPy -m pip install -q $Wheel
  if ($LASTEXITCODE -ne 0) {
    Say "whl 装失败，回退 git（需要 git）"
    & $VenvPy -m pip install -q "git+$Repo"
    if ($LASTEXITCODE -ne 0) { throw "安装失败：whl 与 git 都不通" }
  }
}

# ── 4. 初始化 store ────────────────────────────────────────
$env:COMPANION_HOME = $Store
if (-not $DryRun) {
  & (Join-Path $Venv "Scripts\companion.exe") init | Out-Null
  Say "store 已初始化：$Store"
}

# ── 5. PATH + 环境变量 ─────────────────────────────────────
$scripts = Join-Path $Venv "Scripts"
if ($NoEnv) { Say "跳过 PATH/环境变量（-NoEnv）" }
elseif ($DryRun) { Say "[dry] 把 $scripts 加进用户 PATH" }
else {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if (-not $userPath) { $userPath = "" }
  if ($userPath -notlike "*$scripts*") {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $scripts), "User")
    Say "已写入用户 PATH（新开终端生效）"
  } else {
    Say "PATH 里已有，跳过"
  }
  [Environment]::SetEnvironmentVariable("COMPANION_HOME", $Store, "User")
  $env:Path = "$scripts;$env:Path"
}

# ── 6. 顺手注册 DSH ────────────────────────────────────────
if (-not $NoDsh) {
  $dsh = Get-Command dsh -ErrorAction SilentlyContinue
  if ($dsh) {
    if ($DryRun) { Say "[dry] dsh plugin --profile desktop add $Repo" }
    else {
      Say "注册到 DSH …"
      & $dsh.Source plugin --profile desktop add $Repo
      if ($LASTEXITCODE -ne 0) { Say "DSH 注册没成功，可稍后在「添加插件」里填 $Repo" }
    }
  } else {
    Say "没检测到 dsh，跳过（桌面端点「添加插件」填 $Repo 即可）"
  }
}

Write-Host ""
if ($DryRun) { Write-Host "dry run 结束，没有改任何东西。" -ForegroundColor Yellow; exit 0 }

Write-Host "装好了。新开一个终端，然后试：" -ForegroundColor Green
Say "companion context --budget 1200"
Say "companion tick"
Say "companion cost"
Write-Host ""
Write-Host "先改人格：$Store\SOUL.md" -ForegroundColor DarkGray
