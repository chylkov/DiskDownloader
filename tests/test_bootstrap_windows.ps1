# Windows PowerShell 5.1 smoke tests. No Pester, network, or real installers.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$bootstrap = Join-Path (Split-Path $PSScriptRoot -Parent) 'bootstrap-python.ps1'
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('DiskDownloader-bootstrap-test-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixtureRoot) | Out-Null
$originalLocalAppData = $env:LOCALAPPDATA
$env:LOCALAPPDATA = $fixtureRoot

function Assert-Equal($Actual, $Expected, [string]$Message) {
    if ($Actual -ne $Expected) {
        throw ($Message + ': expected [' + $Expected + '], got [' + $Actual + ']')
    }
}

function Assert-True([bool]$Value, [string]$Message) {
    if (-not $Value) { throw $Message }
}

function Invoke-Scenario([string]$Name, [hashtable]$Options, [int]$ExpectedCode, [scriptblock]$Check) {
    & {
        . $bootstrap
        $script:Fixture = @{
            ManagedPython = (Join-Path $fixtureRoot ([Guid]::NewGuid().ToString('N') + ' runtime & 100% ! (copy) ' + [char]0x0424 + '\python.exe'))
            RuntimeStates = @($false, $true)
            RuntimeCalls = 0
            Foreign = @()
            ForeignAfterDownload = $false
            Consent = $true
            ConsentCalls = 0
            Downloads = 0
            DownloadPath = $null
            DownloadFails = $false
            Verified = $true
            Installs = 0
            InstallerExit = 0
        }
        foreach ($key in $Options.Keys) { $script:Fixture[$key] = $Options[$key] }

        # Fail closed if the production flow unexpectedly bypasses a wrapper.
        function Invoke-WebRequest { throw 'UNEXPECTED NETWORK ACCESS' }
        function Start-Process { throw 'UNEXPECTED PROCESS EXECUTION' }
        function Get-ManagedPythonPath { return $script:Fixture.ManagedPython }
        function Test-PythonRuntime([string]$PythonPath) {
            Assert-Equal $PythonPath $script:Fixture.ManagedPython 'Runtime path must stay inside the fixture'
            $index = [Math]::Min($script:Fixture.RuntimeCalls, $script:Fixture.RuntimeStates.Count - 1)
            $script:Fixture.RuntimeCalls++
            return [bool]$script:Fixture.RuntimeStates[$index]
        }
        function Get-PythonInstaller {
            return [PSCustomObject]@{
                Version = '3.14.8'
                Url = 'https://www.python.org/ftp/python/3.14.8/python-3.14.8-amd64.exe'
                Sha256 = ('a' * 64)
                Architecture = 'x64'
            }
        }
        function Get-ExistingPython314Installations([string]$ManagedDirectory) {
            if ($script:Fixture.ForeignAfterDownload -and $script:Fixture.Downloads -gt 0) {
                return @('C:\Newly registered Python\python.exe')
            }
            return $script:Fixture.Foreign
        }
        function Request-PythonInstallConsent {
            $script:Fixture.ConsentCalls++
            return $script:Fixture.Consent
        }
        function Save-PythonInstaller([string]$Url, [string]$Destination) {
            $script:Fixture.Downloads++
            $script:Fixture.DownloadPath = $Destination
            [IO.File]::WriteAllBytes($Destination, [byte[]](1, 2, 3, 4))
            if ($script:Fixture.DownloadFails) { throw 'Fixture download interrupted' }
        }
        function Test-PythonInstaller([string]$Path, [string]$Sha256) {
            return $script:Fixture.Verified
        }
        function Install-ManagedPython([string]$InstallerPath, [string]$ManagedDirectory) {
            $script:Fixture.Installs++
            [IO.Directory]::CreateDirectory((Split-Path $script:Fixture.ManagedPython -Parent)) | Out-Null
            [IO.File]::WriteAllBytes($script:Fixture.ManagedPython, [byte[]](0))
            return $script:Fixture.InstallerExit
        }

        $code = Invoke-PythonBootstrap
        Assert-Equal $code $ExpectedCode $Name
        if ($script:Fixture.DownloadPath) {
            Assert-True (-not (Test-Path -LiteralPath $script:Fixture.DownloadPath)) ($Name + ': installer must be removed')
        }
        & $Check $script:Fixture
        Write-Host ('PASS: ' + $Name)
    }
}

try {
    Invoke-Scenario 'Existing managed runtime does not prompt or download' @{ RuntimeStates = @($true) } 0 {
        param($f)
        Assert-Equal $f.ConsentCalls 0 'Existing runtime consent'
        Assert-Equal $f.Downloads 0 'Existing runtime download'
        Assert-Equal $f.Installs 0 'Existing runtime installer'
    }
    Invoke-Scenario 'Declining consent never downloads or installs' @{ Consent = $false } 2 {
        param($f)
        Assert-Equal $f.Downloads 0 'Declined download'
        Assert-Equal $f.Installs 0 'Declined installer'
    }
    Invoke-Scenario 'A foreign registered Python 3.14 prevents installation' @{ Foreign = @('C:\Other Python\python.exe') } 1 {
        param($f)
        Assert-Equal $f.Downloads 0 'Foreign installation download'
        Assert-Equal $f.Installs 0 'Foreign installation installer'
    }
    Invoke-Scenario 'Accepted installation verifies the resulting runtime' @{} 0 {
        param($f)
        Assert-Equal $f.Downloads 1 'Accepted download'
        Assert-Equal $f.Installs 1 'Accepted installer'
        Assert-True ($f.RuntimeCalls -ge 2) 'Post-install runtime verification is required'
    }
    Invoke-Scenario 'An interrupted download is cleaned and never installed' @{ DownloadFails = $true } 1 {
        param($f)
        Assert-Equal $f.Installs 0 'Interrupted installer'
    }
    Invoke-Scenario 'Failed integrity verification prevents execution' @{ Verified = $false } 1 {
        param($f)
        Assert-Equal $f.Installs 0 'Unverified installer'
    }
    Invoke-Scenario 'Registration appearing during download prevents execution' @{ ForeignAfterDownload = $true } 1 {
        param($f)
        Assert-Equal $f.Downloads 1 'Race detection happens after download'
        Assert-Equal $f.Installs 0 'Do not alter the newly registered Python'
    }
    Invoke-Scenario 'Installer failure does not claim success' @{ InstallerExit = 1603 } 1 {
        param($f)
        Assert-Equal $f.Installs 1 'Failed installer invocation'
    }
    Invoke-Scenario 'An unusable installed runtime does not claim success' @{ RuntimeStates = @($false, $false) } 1 {
        param($f)
        Assert-Equal $f.Installs 1 'Unusable runtime installer'
    }
    Invoke-Scenario 'Restart-required installer status still checks the runtime' @{ InstallerExit = 3010 } 0 {
        param($f)
        Assert-True ($f.RuntimeCalls -ge 2) 'Restart-required runtime verification'
    }

    & {
        . $bootstrap
        function Invoke-WebRequest { throw 'UNEXPECTED NETWORK ACCESS' }
        function Start-Process { throw 'UNEXPECTED PROCESS EXECUTION' }
        $originalArchitecture = $env:PROCESSOR_ARCHITECTURE
        $originalWowArchitecture = $env:PROCESSOR_ARCHITEW6432
        try {
            $env:PROCESSOR_ARCHITEW6432 = ''
            foreach ($cpu in @('AMD64', 'X86', 'ARM64')) {
                $env:PROCESSOR_ARCHITECTURE = $cpu
                $metadata = Get-PythonInstaller
                Assert-Equal $metadata.Version '3.14.8' 'Pinned Python version'
                Assert-True ($metadata.Url -match '^https://www\.python\.org/ftp/python/3\.14\.8/python-3\.14\.8(-amd64|-arm64)?\.exe$') 'Installer must come from the pinned official directory'
                Assert-True ($metadata.Sha256 -match '^[0-9a-fA-F]{64}$') 'Installer metadata must pin SHA-256'
                $expectedSuffix = switch ($cpu) { 'AMD64' { '-amd64.exe' } 'X86' { '3.14.8.exe' } 'ARM64' { '-arm64.exe' } }
                Assert-True ($metadata.Url.EndsWith($expectedSuffix)) ('Correct installer architecture for ' + $cpu)
            }
            $env:PROCESSOR_ARCHITECTURE = 'X86'
            $env:PROCESSOR_ARCHITEW6432 = 'AMD64'
            Assert-Equal (Get-PythonInstaller).Architecture 'x64' 'Use the OS architecture from a 32-bit process'
        }
        finally {
            $env:PROCESSOR_ARCHITECTURE = $originalArchitecture
            $env:PROCESSOR_ARCHITEW6432 = $originalWowArchitecture
        }

        $installerPath = Join-Path $fixtureRoot 'fixture-installer.exe'
        [IO.File]::WriteAllBytes($installerPath, [byte[]](1, 2, 3, 4, 5, 6))
        # Hash the actual fixture bytes with the real cmdlet. Only certificate
        # validation is mocked; a fixture executable is never started.
        $correctHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash
        $script:SignatureFixture = @{
            Calls = 0
            Status = [System.Management.Automation.SignatureStatus]::Valid
            Subject = 'CN=Python Software Foundation, O=Python Software Foundation, C=US'
            HasCertificate = $true
        }
        function Get-AuthenticodeSignature {
            [CmdletBinding()]
            param([Alias('Path', 'LiteralPath')][string]$FilePath)
            $script:SignatureFixture.Calls++
            $certificate = $null
            if ($script:SignatureFixture.HasCertificate) {
                $certificate = [PSCustomObject]@{ Subject = $script:SignatureFixture.Subject }
            }
            return [PSCustomObject]@{
                Status = $script:SignatureFixture.Status
                SignerCertificate = $certificate
            }
        }
        Assert-Equal (Test-PythonInstaller -Path $installerPath -Sha256 ('0' * 64)) $false 'Wrong SHA-256 must be rejected'
        Assert-Equal $script:SignatureFixture.Calls 0 'Reject the wrong hash before certificate validation'
        foreach ($status in @('NotSigned', 'HashMismatch')) {
            $script:SignatureFixture.Status = [Enum]::Parse([System.Management.Automation.SignatureStatus], $status)
            Assert-Equal (Test-PythonInstaller -Path $installerPath -Sha256 $correctHash) $false ('Reject signature status ' + $status)
        }
        $script:SignatureFixture.Status = [System.Management.Automation.SignatureStatus]::Valid
        $script:SignatureFixture.Subject = 'CN=Unrelated Publisher, O=Unrelated Publisher, C=US'
        Assert-Equal (Test-PythonInstaller -Path $installerPath -Sha256 $correctHash) $false 'Reject a valid signature from another publisher'
        $script:SignatureFixture.Subject = 'CN=Python Software Foundation Evil, O=Unrelated Publisher, C=US'
        Assert-Equal (Test-PythonInstaller -Path $installerPath -Sha256 $correctHash) $false 'Match the complete publisher common name'
        $script:SignatureFixture.HasCertificate = $false
        Assert-Equal (Test-PythonInstaller -Path $installerPath -Sha256 $correctHash) $false 'Reject a missing signer certificate'
        $script:SignatureFixture.HasCertificate = $true
        $script:SignatureFixture.Subject = 'CN=Python Software Foundation, O=Python Software Foundation, C=US'
        Assert-Equal (Test-PythonInstaller -Path $installerPath -Sha256 $correctHash) $true 'Accept matching bytes and a valid PSF signature'
        Write-Host 'PASS: real installer verification, fixture bytes, and mocked certificate results'
    }

    & {
        . $bootstrap
        function Invoke-WebRequest { throw 'UNEXPECTED NETWORK ACCESS' }
        function Start-Process { throw 'UNEXPECTED PROCESS EXECUTION' }
        $script:RegistryFixture = @()
        function Get-PythonRegistryInstallations { return $script:RegistryFixture }
        $managedDirectory = Join-Path $fixtureRoot 'Managed Python'
        Assert-Equal @(Get-ExistingPython314Installations -ManagedDirectory $managedDirectory).Count 0 'No registered installation'
        $script:RegistryFixture = @([PSCustomObject]@{
            Directory = ($managedDirectory.ToLowerInvariant() + '\')
            Name = 'Python 3.14.8'
        })
        Assert-Equal @(Get-ExistingPython314Installations -ManagedDirectory $managedDirectory).Count 0 'The managed registration is allowed with case and trailing slash differences'
        $script:RegistryFixture = @([PSCustomObject]@{
            Directory = (Join-Path $fixtureRoot 'Other Python')
            Name = 'Python 3.14.8'
        })
        Assert-Equal @(Get-ExistingPython314Installations -ManagedDirectory $managedDirectory).Count 1 'A foreign installation must be protected'
        $script:RegistryFixture = @([PSCustomObject]@{
            Directory = $null
            Name = 'Python 3.14.8'
        })
        Assert-Equal @(Get-ExistingPython314Installations -ManagedDirectory $managedDirectory).Count 1 'An ambiguous registration must prevent installation'
        Assert-Equal (ConvertTo-PythonDirectoryIdentity -Directory 'relative\python') $null 'A relative installation directory is ambiguous'
        Write-Host 'PASS: real foreign-installation guard with fixture registry records; no registry writes'
    }

    & {
        . $bootstrap
        $script:ConsentAnswer = ''
        function Read-Host([string]$Prompt) { return $script:ConsentAnswer }
        foreach ($answer in @('', 'N', 'no', 'unexpected answer')) {
            $script:ConsentAnswer = $answer
            Assert-Equal (Request-PythonInstallConsent) $false 'No installation without explicit consent'
        }
        foreach ($answer in @('Y', ' yes ')) {
            $script:ConsentAnswer = $answer
            Assert-Equal (Request-PythonInstallConsent) $true 'Accept explicit affirmative consent'
        }
        Write-Host 'PASS: real consent parser with mocked console input, default is no'
    }

    & {
        . $bootstrap
        function Invoke-WebRequest { throw 'UNEXPECTED NETWORK ACCESS' }
        $script:CapturedInstallerProcess = $null
        function Start-Process {
            [CmdletBinding()]
            param([string]$FilePath, [object]$ArgumentList, [switch]$Wait,
                  [switch]$PassThru, [switch]$NoNewWindow, [string]$WindowStyle,
                  [string]$WorkingDirectory, [string]$Verb)
            $script:CapturedInstallerProcess = @{} + $PSBoundParameters
            return [PSCustomObject]@{ ExitCode = 3010 }
        }
        $installerPath = Join-Path $fixtureRoot 'fixture installer & 100% !.exe'
        [IO.File]::WriteAllBytes($installerPath, [byte[]](0))
        $managedDirectory = Join-Path $fixtureRoot ('Python with spaces & 100% ! (copy) ' + [char]0x0424)
        $status = Install-ManagedPython -InstallerPath $installerPath -ManagedDirectory $managedDirectory
        Assert-Equal $status 3010 'Preserve installer exit status for the caller'
        $process = $script:CapturedInstallerProcess
        Assert-True ($null -ne $process) 'Installer wrapper must use Start-Process'
        Assert-Equal $process.FilePath $installerPath 'Installer file path is passed separately'
        Assert-True ([bool]$process.Wait) 'Wait for installation to finish'
        Assert-True ([bool]$process.PassThru) 'Capture the installer status'
        Assert-True (-not $process.ContainsKey('Verb') -or $process.Verb -ne 'RunAs') 'Never request administrator elevation'
        $arguments = [string]::Join(' ', [string[]]$process.ArgumentList)
        foreach ($flag in @('/passive', '/norestart', 'InstallAllUsers=0', 'Include_tcltk=1', 'Include_launcher=0', 'PrependPath=0')) {
            Assert-True ($arguments.Contains($flag)) ('Required installer option ' + $flag)
        }
        $quotedValue = 'TargetDir="' + $managedDirectory + '"'
        $quotedArgument = '"TargetDir=' + $managedDirectory + '"'
        Assert-True ($arguments.Contains($quotedValue) -or $arguments.Contains($quotedArgument)) 'Quote the target directory including Unicode and shell punctuation'
        Write-Host 'PASS: real installer wrapper arguments with a mocked process; no executable started'
    }
    Write-Host 'All bootstrap smoke tests passed; no network or installer was executed.'
}
finally {
    $env:LOCALAPPDATA = $originalLocalAppData
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
}
