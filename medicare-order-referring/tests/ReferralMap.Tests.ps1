# Pester tests for the ReferralMap module. Runs entirely against a local test
# double of the CMS FOIA host and NPPES API — no external traffic.

BeforeAll {
    $script:Root = Split-Path $PSScriptRoot -Parent
    $script:WorkDir = Join-Path ([System.IO.Path]::GetTempPath()) ("rm-tests-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $script:WorkDir -Force | Out-Null
    $script:SiteDir = Join-Path $script:WorkDir 'site'
    New-Item -ItemType Directory -Path $script:SiteDir -Force | Out-Null

    # --- Fixture shared-patient file (padded numbers, like the real file) ---
    # Direction: NPI1 saw the patient first, NPI2 second. Our clinics are
    # 9000000001 (org) and 9000000002 (individual PT).
    $lines = @(
        '8000000001,9000000001,120       ,45        ,5'    # doctor -> clinic
        '8000000002,9000000001,60        ,20        ,0'    # ortho  -> clinic
        '8000000001,9000000002,30        ,12        ,2'    # doctor -> individual PT
        '8000000003,9000000002,25        ,11        ,0'    # unknown NPI -> individual PT
        '9000000001,8000000001,99        ,99        ,0'    # OUTBOUND from clinic: must not count
        '8000000001,7000000000,50        ,50        ,0'    # unrelated pair: must not count
    )
    $psppTxt = Join-Path $script:WorkDir 'physician-shared-patient-patterns-2015-days30.txt'
    Set-Content -Path $psppTxt -Value ($lines -join "`n") -Encoding ascii -NoNewline
    Compress-Archive -Path $psppTxt -DestinationPath (Join-Path $script:SiteDir 'pspp-2015-days30.zip') -Force

    # Broken zip fixture: a txt with the wrong number of fields.
    $badTxt = Join-Path $script:WorkDir 'physician-shared-patient-patterns-2014-days30.txt'
    Set-Content -Path $badTxt -Value "1,2,3`n4,5,6" -Encoding ascii -NoNewline
    Compress-Archive -Path $badTxt -DestinationPath (Join-Path $script:SiteDir 'pspp-2014-days30.zip') -Force

    # --- Test double server -----------------------------------------------
    $script:Port = Get-Random -Minimum 20000 -Maximum 45000
    $base = "http://127.0.0.1:$($script:Port)"
    $python = if (Get-Command python3 -ErrorAction SilentlyContinue) { 'python3' } else { 'python' }
    $serverArgs = @{
        FilePath = $python
        ArgumentList = @((Join-Path $PSScriptRoot 'fake_cms_server.py'), $script:Port, $script:SiteDir)
        PassThru = $true
    }
    if ($env:OS -eq 'Windows_NT') { $serverArgs.WindowStyle = 'Hidden' }
    $script:Server = Start-Process @serverArgs
    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 200
        try {
            Invoke-WebRequest -Uri "$base/nppes/?version=2.1&number=8000000001" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $ready = $true
        } catch { $ready = $false }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) { throw "Fake CMS server failed to start on port $($script:Port)." }

    # --- Module under test, pointed at the doubles ------------------------
    $env:RM_FOIA_URL_TEMPLATE = "$base/foia/pspp-{0}-days{1}.zip"
    $env:RM_NPPES_URL = "$base/nppes/"
    $env:RM_DATA_DIR = Join-Path $script:WorkDir 'store'
    Import-Module (Join-Path $script:Root 'ReferralMap/ReferralMap.psm1') -Force
}

AfterAll {
    if ($script:Server -and -not $script:Server.HasExited) { $script:Server.Kill() }
    Remove-Item -Recurse -Force $script:WorkDir -ErrorAction SilentlyContinue
    Remove-Item Env:RM_FOIA_URL_TEMPLATE, Env:RM_NPPES_URL, Env:RM_DATA_DIR -ErrorAction SilentlyContinue
}

Describe 'Dataset download' {
    It 'downloads, extracts, validates, and counts the file' {
        $r = Save-RmDataset
        $r.Downloaded | Should -BeTrue
        $r.RowCount | Should -Be 6
        Test-Path $r.Path | Should -BeTrue
        (Get-RmStatus).DatasetReady | Should -BeTrue
    }
    It 'is idempotent when the file is already present' {
        (Save-RmDataset).Downloaded | Should -BeFalse
    }
    It 'rejects a malformed dataset and leaves nothing behind' {
        { Save-RmDataset -Year 2014 } | Should -Throw '*does not look like*'
        Test-Path (Get-RmDatasetPath -Year 2014 -Interval 30) | Should -BeFalse
        @(Get-ChildItem $env:RM_DATA_DIR -Filter '*.tmp*') | Should -BeNullOrEmpty
    }
}

Describe 'Clinic discovery via NPPES' {
    It 'finds the org clinic and individual PTs, excluding assistants and mailing-only matches' {
        $clinics = @(Find-RmClinic -Zip 99999)
        @($clinics | ForEach-Object NPI) | Sort-Object | Should -Be @('9000000001', '9000000002', '9000000005')
        ($clinics | Where-Object NPI -eq '9000000001').Type | Should -Be 'Organization'
        ($clinics | Where-Object NPI -eq '9000000001').Zip | Should -Be '99999'
        ($clinics | Where-Object NPI -eq '9000000001').Enumerated | Should -Be '2008-06-15'
    }
    It 'honors -OrganizationsOnly' {
        $clinics = @(Find-RmClinic -Zip 99999 -OrganizationsOnly)
        @($clinics).Count | Should -Be 1
        $clinics[0].NPI | Should -Be '9000000001'
    }
    It 'rejects malformed ZIP input' {
        { Find-RmClinic -Zip 'abc12' } | Should -Throw
        { Find-RmClinic -Zip '1' } | Should -Throw
    }
}

Describe 'Referral map' {
    BeforeAll { $script:Map = Get-RmReferralMap -Zip 99999 }

    It 'ranks clinics by inbound shared-patient volume' {
        $clinics = @($script:Map.Clinics)
        $clinics.Count | Should -Be 3
        $clinics[0].NPI | Should -Be '9000000001'   # 45+20 = 65 benes
        $clinics[0].SharedPatients | Should -Be 65
        $clinics[0].ReferralSources | Should -Be 2
        $clinics[1].NPI | Should -Be '9000000002'   # 12+11 = 23 benes
        $clinics[1].SharedPatients | Should -Be 23
    }
    It 'flags providers whose NPI did not exist in the data year' {
        $clinics = @($script:Map.Clinics)
        ($clinics | Where-Object NPI -eq '9000000001').ExistedInDataYear | Should -Be 'Yes'
        ($clinics | Where-Object NPI -eq '9000000005').ExistedInDataYear | Should -Match '^No \(NPI issued 2019'
        ($script:Map.Notes -join ' ') | Should -Match '1 of 3 providers'
    }
    It 'ignores outbound and unrelated pairs' {
        @($script:Map.Sources | Where-Object SourceNPI -eq '9000000001') | Should -BeNullOrEmpty
        @($script:Map.Sources | Where-Object ClinicNPI -eq '7000000000') | Should -BeNullOrEmpty
    }
    It 'returns per-source edges sorted by volume and enriched from NPPES' {
        $sources = @($script:Map.Sources)
        $sources.Count | Should -Be 4
        $sources[0].SourceNPI | Should -Be '8000000001'
        $sources[0].SharedPatients | Should -Be 45
        $sources[0].SourceName | Should -Be 'DAVID DOCTOR'
        $sources[0].SourceSpecialty | Should -Be 'Family Medicine'
        ($sources | Where-Object SourceNPI -eq '8000000003' | Select-Object -First 1).SourceName |
            Should -Be '(NPI deactivated or not found)'
    }
    It 'caches NPPES lookups on disk' {
        Test-Path (Join-Path $env:RM_DATA_DIR 'nppes-cache.json') | Should -BeTrue
        $cache = Get-Content (Join-Path $env:RM_DATA_DIR 'nppes-cache.json') -Raw | ConvertFrom-Json
        $cache.'8000000001'.Name | Should -Be 'DAVID DOCTOR'
    }
    It 'supports -SkipEnrichment for offline use' {
        $map = Get-RmReferralMap -Zip 99999 -SkipEnrichment
        @($map.Sources)[0].SourceName | Should -Be ''
        @($map.Clinics)[0].SharedPatients | Should -Be 65
    }
    It 'carries honest methodology notes' {
        ($script:Map.Notes -join ' ') | Should -Match '2015'
        ($script:Map.Notes -join ' ') | Should -Match 'NOT current volumes'
        ($script:Map.Notes -join ' ') | Should -Match 'fewer than 11'
    }
}

Describe 'Referral map export' {
    It 'writes CSV plus a methodology sidecar including the vintage warning' {
        $map = Get-RmReferralMap -Zip 99999
        $out = Join-Path $script:WorkDir 'clinics.csv'
        $r = @($map.Clinics) | Export-RmResult -Path $out -Notes $map.Notes -Description 'test'
        $r.Rows | Should -Be 3
        (Import-Csv $out).Count | Should -Be 3
        $sidecar = Get-Content $r.Methodology -Raw
        $sidecar | Should -Match 'Shared Patient Patterns'
        $sidecar | Should -Match 'NOT current volumes'
    }
}

Describe 'Scan engine safety' {
    It 'throws on a file that is not a shared-patient file' {
        $bogus = Join-Path $script:WorkDir 'bogus.txt'
        Set-Content $bogus "this,is,not`nright,at,all"
        $set = New-Object 'System.Collections.Generic.HashSet[string]'
        [void]$set.Add('123')
        { [RmEngine]::ScanInbound($bogus, $set) } | Should -Throw
    }
}
