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
    It 'supports ZIP-prefix searches and survives malformed short postal codes' {
        $clinics = @(Find-RmClinic -Zip '999*')
        @($clinics | ForEach-Object NPI) | Sort-Object |
            Should -Be @('9000000001', '9000000002', '9000000005', '9000000006')
        ($clinics | Where-Object NPI -eq '9000000006').Zip | Should -Be '9999'
    }
    It 'pages through NPPES results past the 200-row page size' {
        $clinics = @(Find-RmClinic -Zip 88888)
        @($clinics).Count | Should -Be 201
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
    It 'flags providers enumerated after the file''s service cutoff (Sep 1, 2015)' {
        $clinics = @($script:Map.Clinics)
        ($clinics | Where-Object NPI -eq '9000000001').ExistedInDataYear | Should -Be 'Yes'
        # 2015-11-10 is within 2015 but after the ~Sep-1 cutoff → must be "No".
        ($clinics | Where-Object NPI -eq '9000000005').ExistedInDataYear | Should -Match '^No \(NPI issued 2015-11-10'
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

Describe 'Group referral footprint (bridge)' {
    It 'rolls inbound volume up to buckets with top sources' {
        # Map both clinics into one bucket 'GroupX'.
        $map = @{ '9000000001' = 'GroupX'; '9000000002' = 'GroupX' }
        $fp = @(Get-RmInboundByBucket -TargetToBucket $map -TopPerBucket 5)
        $fp.Count | Should -Be 1
        $fp[0].Bucket | Should -Be 'GroupX'
        $fp[0].SharedPatients | Should -Be 88   # 45+20+12+11
        $fp[0].SourceCount | Should -Be 3        # 8000000001, 8000000002, 8000000003
        $top = @($fp[0].TopSources)[0]
        $top.SourceNPI | Should -Be '8000000001' # 45+12 = 57, the biggest
        $top.SharedPatients | Should -Be 57
        $top.SourceName | Should -Be 'DAVID DOCTOR'
    }
    It 'keeps separate buckets separate' {
        $map = @{ '9000000001' = 'A'; '9000000002' = 'B' }
        $fp = @(Get-RmInboundByBucket -TargetToBucket $map)
        ($fp | Where-Object Bucket -eq 'A').SharedPatients | Should -Be 65
        ($fp | Where-Object Bucket -eq 'B').SharedPatients | Should -Be 23
    }
    It 'credits an NPI in multiple buckets to each (list-valued map)' {
        # 9000000001 (65) belongs to both PAC1 and PAC2; 9000000002 (23) only PAC2.
        $map = @{ '9000000001' = @('PAC1', 'PAC2'); '9000000002' = @('PAC2') }
        $fp = @(Get-RmInboundByBucket -TargetToBucket $map)
        ($fp | Where-Object Bucket -eq 'PAC1').SharedPatients | Should -Be 65
        ($fp | Where-Object Bucket -eq 'PAC2').SharedPatients | Should -Be 88   # 65 + 23
    }
}

Describe 'Provider 360 referral activity' {
    It 'returns inbound and outbound edges for one NPI, enriched' {
        $act = Get-RmProviderReferralActivity -Npi 9000000001
        $inb = @($act.Inbound); $outb = @($act.Outbound)
        $inb.Count | Should -Be 2         # from 8000000001 (45) and 8000000002 (20)
        $inb[0].NPI | Should -Be '8000000001'
        $inb[0].SharedPatients | Should -Be 45
        $inb[0].Name | Should -Be 'DAVID DOCTOR'
        $outb.Count | Should -Be 1        # 9000000001 -> 8000000001 (99)
        $outb[0].NPI | Should -Be '8000000001'
        $outb[0].SharedPatients | Should -Be 99
    }
    It 'rejects a non-10-digit NPI' {
        { Get-RmProviderReferralActivity -Npi 123 } | Should -Throw
    }
}

Describe 'Engine scans' {
    It 'ScanOutbound matches on the source column' {
        $set = New-Object 'System.Collections.Generic.HashSet[string]'
        [void]$set.Add('8000000001')
        $out = [RmEngine]::ScanOutbound((Get-RmDatasetPath), $set)
        @($out).Count | Should -Be 3     # 8000000001 -> 9000000001, 9000000002, 7000000000
    }
    It 'ScanEither matches either column in one pass' {
        $set = New-Object 'System.Collections.Generic.HashSet[string]'
        [void]$set.Add('9000000001')
        $e = [RmEngine]::ScanEither((Get-RmDatasetPath), $set)
        @($e).Count | Should -Be 3       # 2 inbound + 1 outbound
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
    It 'neutralizes CSV formula injection in NPPES-sourced names' {
        $rows = @([pscustomobject]@{ SourceNPI = '8000000001'; SourceName = '=HYPERLINK("http://evil")'; ClinicName = 'A' })
        $out = Join-Path $script:WorkDir 'rm-inject.csv'
        $rows | Export-RmResult -Path $out | Out-Null
        (Import-Csv $out)[0].SourceName | Should -Be "'=HYPERLINK(""http://evil"")"
    }
}

Describe 'Download safety' {
    It 'rejects a non-CMS / non-loopback download host' {
        $saved = $env:RM_FOIA_URL_TEMPLATE
        try {
            $env:RM_FOIA_URL_TEMPLATE = 'https://evil.example.com/pspp-{0}-days{1}.zip'
            Import-Module (Join-Path $script:Root 'ReferralMap/ReferralMap.psm1') -Force
            { Save-RmDataset -Year 2011 } | Should -Throw '*only https CMS hosts*'
        } finally {
            $env:RM_FOIA_URL_TEMPLATE = $saved
            Import-Module (Join-Path $script:Root 'ReferralMap/ReferralMap.psm1') -Force
        }
    }
    It 'rejects a zip whose entry path escapes the extraction dir (zip-slip)' {
        # Craft a zip with an entry named "../evil.txt".
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $slipZip = Join-Path $script:SiteDir 'pspp-2010-days30.zip'
        Remove-Item $slipZip -ErrorAction SilentlyContinue
        $z = [System.IO.Compression.ZipFile]::Open($slipZip, 'Create')
        try {
            $entry = $z.CreateEntry('../evil.txt')
            $w = New-Object System.IO.StreamWriter($entry.Open())
            $w.Write('1,2,3,4,5'); $w.Dispose()
        } finally { $z.Dispose() }
        { Save-RmDataset -Year 2010 } | Should -Throw '*unsafe entry path*'
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
