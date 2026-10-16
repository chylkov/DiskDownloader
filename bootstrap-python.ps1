# Windows PowerShell 5.1. This file is UTF-8 with BOM for Russian messages.
# Dot-source to inspect/test the functions without downloads or installation.
# Published pins: https://www.python.org/downloads/release/python-3148/
# Installer options: https://docs.python.org/3.14/using/windows.html#installing-without-ui

function Get-ManagedPythonPath {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'Не удалось определить папку LOCALAPPDATA.'
    }
    return (Join-Path $env:LOCALAPPDATA 'DiskDownloader\Python\python.exe')
}

function Get-PythonInstaller {
    $architecture = $env:PROCESSOR_ARCHITEW6432
    if ([string]::IsNullOrWhiteSpace($architecture)) {
        $architecture = $env:PROCESSOR_ARCHITECTURE
    }
    if ([string]::IsNullOrWhiteSpace($architecture)) {
        throw 'Не удалось определить архитектуру Windows.'
    }
    switch ($architecture.ToUpperInvariant()) {
        'AMD64' {
            return [PSCustomObject]@{
                Version = '3.14.8'; Architecture = 'x64'
                Url = 'https://www.python.org/ftp/python/3.14.8/python-3.14.8-amd64.exe'
                Sha256 = '759be887b96e736a3ca886daf8d575f18fcae1a09efab6902f42d59e8999f8ef'
            }
        }
        'X86' {
            return [PSCustomObject]@{
                Version = '3.14.8'; Architecture = 'x86'
                Url = 'https://www.python.org/ftp/python/3.14.8/python-3.14.8.exe'
                Sha256 = '963def30c7ebc6381a1ee9056d251ddc4520b18391da2cf0602cfb58f5b80568'
            }
        }
        'ARM64' {
            return [PSCustomObject]@{
                Version = '3.14.8'; Architecture = 'ARM64'
                Url = 'https://www.python.org/ftp/python/3.14.8/python-3.14.8-arm64.exe'
                Sha256 = '53ba74b5b4370eb823fe0005eedeab907479f7339cecb11efea0e75ab5f10cfc'
            }
        }
        default { throw 'Архитектура Windows не поддерживается автоматической установкой.' }
    }
}

function Test-PythonRuntime {
    param([Parameter(Mandatory = $true)][string]$PythonPath)
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        return $false
    }
    try {
        # Import alone does not detect missing Tcl scripts or a broken Tk DLL.
        # The isolated interpreter creates and immediately destroys a real Tk.
        $probe = 'import sys; sys.exit(1) if sys.version_info < (3, 10) else None; import tkinter as tk; window = tk.Tk(); window.withdraw(); window.update_idletasks(); window.destroy()'
        & $PythonPath -I -X utf8 -c $probe *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Get-PythonRegistryInstallations {
    # Read both registry views explicitly, including from a 32-bit PowerShell.
    # Never change/uninstall another application's Python installation.
    $installations = New-Object 'System.Collections.Generic.List[object]'
    $hives = @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryHive]::LocalMachine)
    $views = @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)
    foreach ($hive in $hives) {
        foreach ($view in $views) {
            $registryRoot = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
            try {
                $core = $registryRoot.OpenSubKey('SOFTWARE\Python\PythonCore')
                if ($null -ne $core) {
                    try {
                        foreach ($version in $core.GetSubKeyNames()) {
                            if ($version -notmatch '^3\.14(?:[^0-9]|$)') { continue }
                            $location = $null
                            $installKey = $core.OpenSubKey($version + '\InstallPath')
                            if ($null -ne $installKey) {
                                try {
                                    $location = [string]$installKey.GetValue('')
                                    if ([string]::IsNullOrWhiteSpace($location)) {
                                        $executable = [string]$installKey.GetValue('ExecutablePath')
                                        if (-not [string]::IsNullOrWhiteSpace($executable)) {
                                            $location = [IO.Path]::GetDirectoryName($executable)
                                        }
                                    }
                                }
                                finally { $installKey.Close() }
                            }
                            $installations.Add([PSCustomObject]@{
                                Kind = 'PythonCore'; Name = 'Python ' + $version
                                Directory = $location; Hive = [string]$hive; View = [string]$view
                            })
                        }
                    }
                    finally { $core.Close() }
                }
                $uninstall = $registryRoot.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')
                if ($null -ne $uninstall) {
                    try {
                        foreach ($entryName in $uninstall.GetSubKeyNames()) {
                            $entry = $uninstall.OpenSubKey($entryName)
                            if ($null -eq $entry) { continue }
                            try {
                                $displayName = [string]$entry.GetValue('DisplayName')
                                if ($displayName -notmatch '^Python 3\.14(?:[^0-9]|$)') { continue }
                                $installations.Add([PSCustomObject]@{
                                    Kind = 'Uninstall'; Name = $displayName
                                    Directory = [string]$entry.GetValue('InstallLocation')
                                    Hive = [string]$hive; View = [string]$view
                                })
                            }
                            finally { $entry.Close() }
                        }
                    }
                    finally { $uninstall.Close() }
                }
            }
            finally { $registryRoot.Close() }
        }
    }
    return $installations.ToArray()
}

function ConvertTo-PythonDirectoryIdentity {
    param([AllowNull()][string]$Directory)
    if ([string]::IsNullOrWhiteSpace($Directory)) { return $null }
    try {
        $expanded = [Environment]::ExpandEnvironmentVariables($Directory.Trim().Trim('"'))
        if (-not [IO.Path]::IsPathRooted($expanded)) { return $null }
        return [IO.Path]::GetFullPath($expanded).TrimEnd([char[]]'\/').ToUpperInvariant()
    }
    catch { return $null }
}

function Get-ExistingPython314Installations {
    param([Parameter(Mandatory = $true)][string]$ManagedDirectory)
    $managedIdentity = ConvertTo-PythonDirectoryIdentity -Directory $ManagedDirectory
    if ($null -eq $managedIdentity) { throw 'Не удалось проверить папку установки.' }
    $conflicts = New-Object 'System.Collections.Generic.List[string]'
    foreach ($installation in @(Get-PythonRegistryInstallations)) {
        $identity = ConvertTo-PythonDirectoryIdentity -Directory $installation.Directory
        if ($null -ne $identity -and $identity -eq $managedIdentity) { continue }
        # Missing location is ambiguous: fail safely instead of assuming that
        # a maintenance-mode installer will honor our different TargetDir.
        $description = if ($null -eq $identity) {
            $installation.Name + ' (папка установки не указана)'
        }
        else { $installation.Name + ': ' + $installation.Directory }
        if (-not $conflicts.Contains($description)) { $conflicts.Add($description) }
    }
    return $conflicts.ToArray()
}

function Request-PythonInstallConsent {
    $answer = Read-Host 'Установить Python с Tcl/Tk? [Y/Д = да, Enter = нет]'
    if ($null -eq $answer) { return $false }
    return @('y', 'yes', 'д', 'да') -contains $answer.Trim().ToLowerInvariant()
}

function Save-PythonInstaller {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    $source = [Uri]$Url
    if ($source.Scheme -ne 'https' -or $source.Host -ne 'www.python.org' -or $source.UserInfo) {
        throw 'Недопустимый источник установщика Python.'
    }
    $previousProtocol = [Net.ServicePointManager]::SecurityProtocol
    $previousProgress = $ProgressPreference
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 180 -ErrorAction Stop | Out-Null
    }
    finally {
        [Net.ServicePointManager]::SecurityProtocol = $previousProtocol
        $ProgressPreference = $previousProgress
    }
}

function Test-PythonInstaller {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Sha256
    )
    try {
        if ($Sha256 -notmatch '^[0-9a-fA-F]{64}$') { return $false }
        $actual = Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop
        if ($actual.Hash -ne $Sha256) { return $false }
        $signature = Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop
        if ([string]$signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) {
            return $false
        }
        # Issuer identifies the CA; the subject identifies the PSF publisher.
        return [string]$signature.SignerCertificate.Subject -match '(?:^|,\s*)CN\s*=\s*Python Software Foundation\s*(?:,|$)'
    }
    catch { return $false }
}

function Install-ManagedPython {
    param(
        [Parameter(Mandatory = $true)][string]$InstallerPath,
        [Parameter(Mandatory = $true)][string]$ManagedDirectory
    )
    if ($ManagedDirectory -match '["\r\n]') { throw 'Некорректная папка установки Python.' }
    $target = $ManagedDirectory.TrimEnd([char[]]'\/')
    $arguments = @(
        '/passive', '/norestart', 'InstallAllUsers=0', ('TargetDir="' + $target + '"'),
        'Include_tcltk=1', 'Include_launcher=0', 'InstallLauncherAllUsers=0',
        'PrependPath=0', 'AppendPath=0', 'AssociateFiles=0', 'Shortcuts=0',
        'Include_test=0', 'Include_doc=0', 'Include_pip=0', 'Include_freethreaded=0'
    )
    $process = Start-Process -FilePath $InstallerPath -ArgumentList $arguments -Wait -PassThru -ErrorAction Stop
    return [int]$process.ExitCode
}

function Invoke-PythonBootstrap {
    $temporaryDirectory = $null
    $failureMessage = 'Не удалось подготовить установку Python. Установите Python с Tcl/Tk с https://www.python.org/downloads/windows/ .'
    try {
        $managedPython = Get-ManagedPythonPath
        if (Test-PythonRuntime -PythonPath $managedPython) {
            Write-Host 'Python с Tcl/Tk уже готов к запуску DiskDownloader.'
            return 0
        }
        $managedDirectory = Split-Path -Parent $managedPython
        $failureMessage = 'Не удалось проверить существующие установки Python. Проверьте Python в параметрах приложений Windows.'
        $foreign = @(Get-ExistingPython314Installations -ManagedDirectory $managedDirectory)
        if ($foreign.Count -gt 0) {
            Write-Host 'Уже зарегистрирован другой Python 3.14. Автоматическая установка остановлена.'
            foreach ($installation in $foreign) { Write-Host ('  ' + $installation) }
            Write-Host 'Откройте параметры приложений Windows, выберите Python 3.14, затем Modify и включите Tcl/Tk and IDLE.'
            Write-Host 'После этого запустите run-gui.bat снова. Если установка повреждена, используйте Repair.'
            return 1
        }
        $failureMessage = 'Не удалось определить поддерживаемую архитектуру Windows. Установите Python с Tcl/Tk вручную с https://www.python.org/downloads/windows/ .'
        $installer = Get-PythonInstaller
        Write-Host ('Для формы нужен Python с Tcl/Tk. Будет установлен или восстановлен Python ' + $installer.Version + ' (' + $installer.Architecture + ').')
        Write-Host ('Папка для текущего пользователя: ' + $managedDirectory)
        Write-Host 'Загрузка: официальный сайт www.python.org. PATH, файловые ассоциации и глобальный launcher не меняются.'
        if (-not (Request-PythonInstallConsent)) {
            Write-Host 'Установка отменена. Python не скачивался.'
            return 2
        }
        $failureMessage = 'Не удалось создать временную папку для установщика Python. Проверьте доступ к папке Temp.'
        $temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ('disk-downloader-python-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $temporaryDirectory -ErrorAction Stop | Out-Null
        $download = Join-Path $temporaryDirectory 'python-installer.exe'
        $failureMessage = 'Не удалось скачать установщик Python. Проверьте интернет и повторите запуск. Источник: https://www.python.org/ .'
        Write-Host 'Скачивание установщика Python...'
        Save-PythonInstaller -Url $installer.Url -Destination $download | Out-Null
        $failureMessage = 'Не удалось проверить установщик Python. Установка не запускалась.'
        if (-not (Test-PythonInstaller -Path $download -Sha256 $installer.Sha256)) {
            Write-Host 'Установщик не прошёл проверку SHA-256 или подписи Python Software Foundation. Установка не запускалась.'
            return 1
        }
        # A separate installer could have appeared while the user was deciding
        # or while our download ran. Check again immediately before mutation.
        $foreign = @(Get-ExistingPython314Installations -ManagedDirectory $managedDirectory)
        if ($foreign.Count -gt 0) {
            Write-Host 'Обнаружена другая установка Python 3.14. Закройте установку и включите Tcl/Tk через Modify вручную.'
            return 1
        }
        $failureMessage = 'Не удалось запустить установщик Python. Установите Python с Tcl/Tk вручную с https://www.python.org/downloads/windows/ .'
        Write-Host 'Установка Python с Tcl/Tk...'
        $installerExit = Install-ManagedPython -InstallerPath $download -ManagedDirectory $managedDirectory
        if ($installerExit -notin @(0, 3010)) {
            Write-Host ('Установщик Python завершился с кодом ' + $installerExit + '. Запустите установку вручную или используйте Repair.')
            return 1
        }
        $failureMessage = 'Python установлен, но проверка Tcl/Tk не завершилась. Повторите запуск после проверки установки Python.'
        if (-not (Test-PythonRuntime -PythonPath $managedPython)) {
            if ($installerExit -eq 3010) {
                Write-Host 'Windows запросила перезагрузку. Перезагрузите компьютер и запустите run-gui.bat снова.'
            }
            else {
                Write-Host 'Python с рабочим Tcl/Tk не появился в папке DiskDownloader. Откройте установку Python и используйте Modify → Tcl/Tk and IDLE или Repair.'
            }
            return 1
        }
        Write-Host 'Python с Tcl/Tk установлен и проверен. Запускаем DiskDownloader.'
        return 0
    }
    catch {
        # Never expose raw exceptions, script stacks, process arguments, or
        # environment values: those can contain unrelated credentials.
        Write-Host $failureMessage
        return 1
    }
    finally {
        if ($null -ne $temporaryDirectory -and (Test-Path -LiteralPath $temporaryDirectory)) {
            try { Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force -ErrorAction Stop }
            catch { Write-Host 'Не удалось удалить временные файлы установщика из папки Temp.' }
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-PythonBootstrap)
}
