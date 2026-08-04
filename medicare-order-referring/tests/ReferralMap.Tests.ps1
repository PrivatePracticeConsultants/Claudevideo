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
    $env:RM_CMS_API_BASE = "$base/dataset"
    $env:RM_DATA_DIR = Join-Path $script:WorkDir 'store'
    Import-Module (Join-Path $script:Root 'ReferralMap/ReferralMap.psm1') -Force
}

AfterAll {
    if ($script:Server -and -not $script:Server.HasExited) { $script:Server.Kill() }
    Remove-Item -Recurse -Force $script:WorkDir -ErrorAction SilentlyContinue
    Remove-Item Env:RM_FOIA_URL_TEMPLATE, Env:RM_NPPES_URL, Env:RM_CMS_API_BASE, Env:RM_DATA_DIR -ErrorAction SilentlyContinue
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

Describe 'Source specialty mix' {
    It 'rolls sources up by specialty with correct shares' {
        $mix = @(Get-RmSourceSpecialtyMix -Rows @($script:Map.Sources))
        $fm = $mix | Where-Object Specialty -eq 'Family Medicine'
        $fm.SharedPatients | Should -Be 57    # 45 + 12 (8000000001 into both clinics)
        $fm.Sources | Should -Be 1
        ($mix | Where-Object Specialty -eq 'Orthopaedic Surgery').SharedPatients | Should -Be 20
        ($mix | Where-Object Specialty -eq '(specialty not looked up)').SharedPatients | Should -Be 11
        # Shares sum to ~100 and Family Medicine leads.
        $mix[0].Specialty | Should -Be 'Family Medicine'
        [math]::Round((@($mix) | Measure-Object PctOfVolume -Sum).Sum) | Should -Be 100
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

Describe 'Hop Teaming (CareSet) import and queries' {
    BeforeAll {
        # Fixture delivery zip shaped like the real one: a folder, macOS cruft,
        # docs, and the Hop Teaming CSV (header + 5 data rows).
        $script:HopRows = @(
            'from_npi,to_npi,patient_count,transaction_count,average_day_wait,std_day_wait'
            '8000000001,9000000001,45,50,12.5,10.0'    # doctor -> clinic org
            '8000000002,9000000001,20,20,30.0,20.0'    # ortho  -> clinic org
            '8000000001,9000000002,12,13,7.5,5.0'      # doctor -> individual PT
            '9000000001,8000000001,99,99,50.0,1.0'     # OUTBOUND from clinic
            '8000000001,7000000000,50,50,1.0,1.0'      # unrelated pair
        )
        $script:HopZip = Join-Path $script:WorkDir 'DocGraph_2022_NonCommercial.zip'
        Remove-Item $script:HopZip -ErrorAction SilentlyContinue
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $z = [System.IO.Compression.ZipFile]::Open($script:HopZip, 'Create')
        try {
            foreach ($pair in @(
                @('DocGraph_2022_NonCommercial/DocGraph_Hop_Teaming_2022.csv', ($script:HopRows -join "`n")),
                @('DocGraph_2022_NonCommercial/DocGraph Readme.pdf', 'not a real pdf'),
                @('__MACOSX/DocGraph_2022_NonCommercial/._DocGraph_Hop_Teaming_2022.csv', 'apple cruft')
            )) {
                $entry = $z.CreateEntry($pair[0])
                $w = New-Object System.IO.StreamWriter($entry.Open())
                $w.Write($pair[1]); $w.Dispose()
            }
        } finally { $z.Dispose() }
    }

    It 'imports the delivery zip, detects the year, and activates the dataset' {
        $r = Import-RmDataset -Path $script:HopZip
        $r.Imported | Should -BeTrue
        $r.Year | Should -Be 2022
        $r.RowCount | Should -Be 5          # header not counted
        $s = Get-RmStatus
        $s.Source | Should -Be 'hop-teaming'
        $s.Year | Should -Be 2022
        $s.DatasetReady | Should -BeTrue
        $s.Label | Should -BeLike '*Hop Teaming 2022*'
    }

    It 'maps a ZIP from the hop data with hop-specific columns and totals' {
        $map = Get-RmReferralMap -Zip 99999 -SkipEnrichment
        $org = @($map.Clinics | Where-Object NPI -eq '9000000001')[0]
        $org.SharedPatients | Should -Be 65                 # 45 + 20
        $org.ReferralSources | Should -Be 2
        $pt = @($map.Clinics | Where-Object NPI -eq '9000000002')[0]
        $pt.SharedPatients | Should -Be 12
        # Hop rows carry AvgDayWait, not SameDay
        $top = @($map.Sources)[0]
        $top.PSObject.Properties['AvgDayWait'] | Should -Not -BeNullOrEmpty
        $top.PSObject.Properties['SameDay'] | Should -BeNullOrEmpty
        $org.PSObject.Properties['SameDay'] | Should -BeNullOrEmpty
        @($map.Sources | Where-Object { $_.SourceNPI -eq '8000000001' -and $_.ClinicNPI -eq '9000000001' })[0].AvgDayWait |
            Should -Be 12.5
    }

    It 'uses the full-year window for the vintage flag (source-aware)' {
        # 9000000005 was enumerated 2015-11-10: AFTER the CMS 2015 Sep-1 cutoff
        # (flagged No elsewhere) but long before the 2022 hop window ends.
        $map = Get-RmReferralMap -Zip 99999 -SkipEnrichment
        @($map.Clinics | Where-Object NPI -eq '9000000005')[0].ExistedInDataYear | Should -Be 'Yes'
    }

    It 'carries CareSet-specific methodology notes' {
        $map = Get-RmReferralMap -Zip 99999 -SkipEnrichment
        (@($map.Notes) -join ' ') | Should -BeLike '*DocGraph Hop Teaming 2022*'
        (@($map.Notes) -join ' ') | Should -BeLike '*CareSet*'
        (@($map.Notes) -join ' ') | Should -BeLike '*Medicare Advantage*'
        (@($map.Notes) -join ' ') | Should -Not -BeLike '*Jan?Sep 2015*'
    }

    It 'returns provider-360 activity with AvgDayWait and the data year' {
        $act = Get-RmProviderReferralActivity -Npi 9000000001 -SkipEnrichment
        $act.Year | Should -Be 2022
        @($act.Inbound).Count | Should -Be 2
        @($act.Inbound)[0].SharedPatients | Should -Be 45
        @($act.Inbound)[0].AvgDayWait | Should -Be 12.5
        @($act.Outbound).Count | Should -Be 1
        @($act.Outbound)[0].NPI | Should -Be '8000000001'
    }

    It 'rolls hop volume up to buckets (group footprint bridge)' {
        $fp = @(Get-RmInboundByBucket -TargetToBucket @{ '9000000001' = 'G1'; '9000000002' = 'G1' } -SkipEnrichment)
        $fp.Count | Should -Be 1
        $fp[0].SharedPatients | Should -Be 77               # 45 + 20 + 12
    }

    It 'imports a bare csv only with an explicit -Year when the name has none, and copies (not moves) it' {
        $mystery = Join-Path $script:WorkDir 'mystery.csv'
        Set-Content -Path $mystery -Value ($script:HopRows -join "`n") -Encoding ascii -NoNewline
        { Import-RmDataset -Path $mystery } | Should -Throw '*-Year*'
        (Import-RmDataset -Path $mystery -Year 2021).Year | Should -Be 2021
        (Get-RmStatus).Year | Should -Be 2021
        Test-Path $mystery | Should -BeTrue                 # user's file untouched
    }

    It 'rejects a non-hop file and leaves the active dataset unchanged' {
        $bad = Join-Path $script:WorkDir 'bad-format.csv'
        Set-Content -Path $bad -Value "a,b,c`n1,2,3" -Encoding ascii -NoNewline
        { Import-RmDataset -Path $bad } | Should -Throw '*does not look like*'
        (Get-RmStatus).Year | Should -Be 2021               # unchanged
        @(Get-ChildItem $env:RM_DATA_DIR -Filter '*.tmp*') | Should -BeNullOrEmpty
    }

    It 'switches back to the CMS dataset via Save-RmDataset and restores CMS behavior' {
        (Save-RmDataset).Downloaded | Should -BeFalse       # file already on disk
        $s = Get-RmStatus
        $s.Source | Should -Be 'cms-pspp'
        $s.Year | Should -Be 2015
        $map = Get-RmReferralMap -Zip 99999 -SkipEnrichment
        # CMS columns are back...
        @($map.Sources)[0].PSObject.Properties['SameDay'] | Should -Not -BeNullOrEmpty
        # ...and the 2015 Sep-1 cutoff flags the late-2015 NPI again.
        @($map.Clinics | Where-Object NPI -eq '9000000005')[0].ExistedInDataYear |
            Should -Be 'No (NPI issued 2015-11-10)'
    }
}

Describe 'Dataset switcher and multi-year trend' {
    # State from the previous Describe: CMS 2015 active, with hop_teaming_2022
    # and hop_teaming_2021 also on disk.
    It 'lists every dataset on disk with exactly one active' {
        $sets = @(Get-RmAvailableDatasets)
        @($sets | Where-Object { $_.Source -eq 'hop-teaming' } | ForEach-Object Year) | Sort-Object |
            Should -Be @(2021, 2022)
        @($sets | Where-Object { $_.Source -eq 'cms-pspp' }).Count | Should -BeGreaterOrEqual 1
        @($sets | Where-Object Active).Count | Should -Be 1
        @($sets | Where-Object Active)[0].Source | Should -Be 'cms-pspp'
    }

    It 'switches the active dataset instantly and preserves the cached row count' {
        $r = Set-RmActiveDataset -Source hop-teaming -Year 2022
        $r.Activated | Should -BeTrue
        $s = Get-RmStatus
        $s.Source | Should -Be 'hop-teaming'
        $s.Year | Should -Be 2022
        $s.RowCount | Should -Be 5    # from the .rows sidecar, no re-count
        @(Get-RmAvailableDatasets | Where-Object Active)[0].Year | Should -Be 2022
    }

    It 'refuses to activate a dataset that is not on disk' {
        { Set-RmActiveDataset -Source hop-teaming -Year 2019 } | Should -Throw '*is on disk*'
        (Get-RmStatus).Year | Should -Be 2022   # unchanged
    }

    It 'builds a year-over-year trend across all imported hop years' {
        $t = Get-RmProviderTrend -Npi 9000000001 -SkipEnrichment
        @($t.Years) | Should -Be @(2021, 2022)
        $rows = @($t.Rows)
        $rows.Count | Should -Be 2
        foreach ($r in $rows) {
            $r.InboundSources | Should -Be 2
            $r.InboundPatients | Should -Be 65      # 45 + 20
            $r.OutboundTargets | Should -Be 1
            $r.OutboundPatients | Should -Be 99
            $r.TopSources | Should -BeLike '*8000000001*'
        }
        (@($t.Notes) -join ' ') | Should -BeLike '*intentionally excluded*'
        (@($t.Notes) -join ' ') | Should -BeLike '*Medicare Advantage*'
    }

    It 'requires at least two imported hop years' {
        $emptyDir = Join-Path $script:WorkDir 'empty-store'
        New-Item -ItemType Directory -Path $emptyDir -Force | Out-Null
        $savedDir = (Get-RmConfig).DataDir
        try {
            Set-RmConfig -DataDir $emptyDir
            { Get-RmProviderTrend -Npi 9000000001 -SkipEnrichment } | Should -Throw '*at least TWO*'
        } finally { Set-RmConfig -DataDir $savedDir }
    }

    It 'restores the CMS dataset as active for anything running after this file' {
        (Set-RmActiveDataset -Source cms-pspp -Year 2015).Activated | Should -BeTrue
        (Get-RmStatus).Source | Should -Be 'cms-pspp'
    }
}

Describe 'Practice search and benchmark' {
    # State from previous Describes: CMS 2015 active; hop 2021+2022 on disk.
    It 'finds a practice by organization name' {
        $rows = @(Find-RmPractice -Name 'REHAB' -State MO)
        @($rows | Where-Object NPI -eq '9000000001').Count | Should -Be 1
        $rows[0].Type | Should -Be 'Organization'
        $rows[0].Zip | Should -Be '99999'
    }
    It 'finds an individual by last name and dedupes across queries' {
        $rows = @(Find-RmPractice -Name 'THERAPIST')
        @($rows | Where-Object NPI -eq '9000000002').Count | Should -Be 1
        @($rows | ForEach-Object NPI | Group-Object | Where-Object Count -gt 1) | Should -BeNullOrEmpty
    }
    It 'accepts a pasted 10-digit NPI as the search term' {
        $rows = @(Find-RmPractice -Name '9000000001')
        $rows.Count | Should -Be 1
        $rows[0].Name | Should -Be 'TEST REHAB CLINIC LLC'
    }
    It 'retries a transiently-dropped NPPES connection instead of failing' {
        # The fake registry drops every odd request for this NPI without a
        # response — one retry must recover (a mid-sweep TLS blip once killed
        # a 10-minute competitive sweep; this guards the retry fix).
        $rows = @(Find-RmPractice -Name '4999999999')
        $rows.Count | Should -Be 1
        $rows[0].Name | Should -Be 'FLAKY NETWORK'
    }

    It 'benchmarks the org against its region: rank, share, and the You marker' {
        $bm = Get-RmPracticeBenchmark -Npi 9000000001 -SkipEnrichment
        $bm.Zip | Should -Be '99999'
        $bm.Rank | Should -Be 1
        $bm.InboundPatients | Should -Be 65
        # region volume: org 65 + individual PT (12+11=23) = 88
        $bm.MarketSharePct | Should -Be ([math]::Round(100.0 * 65 / 88, 1))
        @($bm.Clinics)[0].You | Should -Be '>> YOU'
        @($bm.Clinics | Where-Object { $_.You -and $_.NPI -ne '9000000001' }) | Should -BeNullOrEmpty
    }

    It 'reports missed sources: feeding competitors only, not shared feeders' {
        $bm = Get-RmPracticeBenchmark -Npi 9000000001 -SkipEnrichment
        $missed = @($bm.MissedSources)
        # 8000000003 feeds only the individual PT (11 patients) -> missed
        @($missed | Where-Object SourceNPI -eq '8000000003').Count | Should -Be 1
        # 8000000001 feeds BOTH the org and the PT -> not missed
        @($missed | Where-Object SourceNPI -eq '8000000001') | Should -BeNullOrEmpty
        # 8000000002 feeds only the org itself -> not missed for the org
        @($missed | Where-Object SourceNPI -eq '8000000002') | Should -BeNullOrEmpty
    }

    It 'benchmarks the underdog correctly (rank 2, its feeders excluded from missed)' {
        $bm = Get-RmPracticeBenchmark -Npi 9000000002 -SkipEnrichment
        $bm.Rank | Should -Be 2
        $bm.InboundPatients | Should -Be 23
        # 8000000002 feeds only the org -> a missed source for the PT
        @($bm.MissedSources | Where-Object SourceNPI -eq '8000000002').Count | Should -Be 1
        @($bm.MissedSources | Where-Object SourceNPI -eq '8000000001') | Should -BeNullOrEmpty
    }

    It 'works on the hop dataset with hop columns and carries benchmark notes' {
        Set-RmActiveDataset -Source hop-teaming -Year 2022 | Out-Null
        try {
            $bm = Get-RmPracticeBenchmark -Npi 9000000001 -SkipEnrichment
            # hop fixture: org 45+20=65, PT 12 -> total 77
            $bm.MarketSharePct | Should -Be ([math]::Round(100.0 * 65 / 77, 1))
            @($bm.Clinics)[0].PSObject.Properties['SameDay'] | Should -BeNullOrEmpty
            (@($bm.Notes) -join ' ') | Should -BeLike '*BENCHMARK METHOD*'
            (@($bm.Notes) -join ' ') | Should -BeLike '*11-patient privacy floor*'
        } finally { Set-RmActiveDataset -Source cms-pspp -Year 2015 | Out-Null }
    }

    It 'throws a friendly error for an NPI unknown to NPPES' {
        { Get-RmPracticeBenchmark -Npi 1234567890 -SkipEnrichment } | Should -Throw '*not found in the NPPES registry*'
    }

    It 'contract: the GUI OnDone reads these exact properties' {
        $bm = Get-RmPracticeBenchmark -Npi 9000000001 -SkipEnrichment
        foreach ($p in 'Npi','Practice','Zip','Year','Rank','OfTotal','InboundPatients',
                       'ReferralSources','MarketSharePct','Clinics','MissedSources','Notes') {
            $bm.PSObject.Properties[$p] | Should -Not -BeNullOrEmpty -Because $p
        }
        $bm.Practice.Name | Should -Be 'TEST REHAB CLINIC LLC'
    }
}

Describe 'Referral geography and heat map' {
    BeforeAll {
        # Tiny centroid fixture: the two source ZIPs plus the clinic's own.
        $script:CentroidCsv = Join-Path $script:WorkDir 'centroids.csv'
        Set-Content -Path $script:CentroidCsv -Encoding ascii -Value @(
            'zip,lat,lon'
            '99999,40.0000,-90.0000'
            '86442,35.1000,-114.6000'
        )
        # Wipe the NPPES cache so the RequireZip upgrade path is exercised
        # deterministically below.
        Remove-Item (Join-Path $env:RM_DATA_DIR 'nppes-cache.json') -ErrorAction SilentlyContinue
    }

    It 'upgrades old cache entries that lack a Zip (RequireZip re-fetch)' {
        # Seed a pre-Zip-era cache entry, as an old install would have.
        $old = @{ '8000000001' = [pscustomobject]@{ Name='DAVID DOCTOR'; Specialty='Family Medicine'; City='TESTVILLE'; State='MO' } }
        $old | ConvertTo-Json -Compress | Set-Content (Join-Path $env:RM_DATA_DIR 'nppes-cache.json') -Encoding UTF8
        $d = Get-RmProviderDetail -Npi @('8000000001') -RequireZip
        $d['8000000001'].Zip | Should -Be '99999'
    }

    It 'aggregates inbound volume by source ZIP with distance and share' {
        $g = Get-RmReferralGeography -Npi 9000000001 -CentroidPath $script:CentroidCsv
        $g.TotalPatients | Should -Be 65
        $g.MappedPatients | Should -Be 65
        $rows = @($g.Rows)
        $rows.Count | Should -Be 2
        $rows[0].Zip | Should -Be '99999'          # 45 > 20
        $rows[0].SharedPatients | Should -Be 45
        $rows[0].PctOfVolume | Should -Be 69.2
        $rows[0].DistanceMiles | Should -Be 0      # same ZIP as the practice
        $rows[1].Zip | Should -Be '86442'
        $rows[1].SharedPatients | Should -Be 20
        # 40.0,-90.0 to 35.1,-114.6 is ~1,400 miles — sanity band, not exact
        [double]$rows[1].DistanceMiles | Should -BeGreaterThan 1200
        [double]$rows[1].DistanceMiles | Should -BeLessThan 1600
        $g.Practice.Name | Should -Be 'TEST REHAB CLINIC LLC'
        $g.Practice.Lat | Should -Be 40.0
    }

    It 'groups sources that cannot be located and keeps totals honest' {
        # 9000000002's feeders: 8000000001 (12, ZIP 99999) + 8000000003 (11,
        # absent from NPPES -> no ZIP).
        $g = Get-RmReferralGeography -Npi 9000000002 -CentroidPath $script:CentroidCsv
        $g.TotalPatients | Should -Be 23
        $g.MappedPatients | Should -Be 12
        $unloc = @($g.Rows | Where-Object Zip -eq '(not located)')[0]
        $unloc.SharedPatients | Should -Be 11
        $unloc.Lat | Should -BeNullOrEmpty
    }

    It 'writes a self-viewing HTML map with the points and methodology' {
        $g = Get-RmReferralGeography -Npi 9000000001 -CentroidPath $script:CentroidCsv
        $out = Join-Path $script:WorkDir 'heatmap.html'
        $r = Export-RmReferralMapHtml -Geography $g -Path $out
        $r.Points | Should -Be 2
        $html = Get-Content $out -Raw
        $html | Should -BeLike '*circleMarker*'
        $html | Should -BeLike '*"z":"99999"*'
        $html | Should -BeLike '*"z":"86442"*'
        $html | Should -BeLike '*TEST REHAB CLINIC LLC*'
        $html | Should -BeLike '*GEOGRAPHY METHOD*'
        $html | Should -BeLike '*openstreetmap*'
        # Leaflet must be INLINED (self-contained file), not a CDN reference
        $html | Should -BeLike '*Leaflet 1.9.4*'
        $html | Should -Not -BeLike '*unpkg.com*'
    }

    It 'works on the hop dataset too' {
        Set-RmActiveDataset -Source hop-teaming -Year 2022 | Out-Null
        try {
            $g = Get-RmReferralGeography -Npi 9000000001 -CentroidPath $script:CentroidCsv
            $g.TotalPatients | Should -Be 65       # hop fixture: 45 + 20
            (@($g.Notes) -join ' ') | Should -BeLike '*Hop Teaming 2022*'
        } finally { Set-RmActiveDataset -Source cms-pspp -Year 2015 | Out-Null }
    }

    It 'returns an honest empty result for an NPI with no inbound pairs' {
        $g = Get-RmReferralGeography -Npi 8000000002 -CentroidPath $script:CentroidCsv
        $g.TotalPatients | Should -Be 0
        @($g.Rows).Count | Should -Be 0
    }
}

Describe 'Group referral benchmark' {
    # Synthetic groups over the fixture NPIs (CMS 2015 active from earlier):
    # G1 = the org clinic (inbound 45+20=65), G2 = the individual PT (12+11=23).
    BeforeAll {
        $script:GbMap = @{ '9000000001' = 'G1'; '9000000002' = 'G2' }
        $script:GbNames = @{ 'G1' = 'ALPHA REHAB GROUP'; 'G2' = 'BETA THERAPY' }
    }

    It 'ranks groups by rolled-up member volume with share of measured total' {
        $gb = Get-RmGroupBenchmark -TargetToBucket $script:GbMap -BucketNames $script:GbNames -SkipEnrichment
        $rows = @($gb.Buckets)
        $rows.Count | Should -Be 2
        $rows[0].Bucket | Should -Be 'G1'
        $rows[0].Rank | Should -Be 1
        $rows[0].GroupName | Should -Be 'ALPHA REHAB GROUP'
        $rows[0].InboundPatients | Should -Be 65
        $rows[0].SharePct | Should -Be ([math]::Round(100.0 * 65 / 88, 1))
        $rows[0].Sources | Should -Be 2
        $rows[1].Rank | Should -Be 2
        $rows[1].InboundPatients | Should -Be 23
    }

    It 'returns the full edge list with distinct members fed per source' {
        $gb = Get-RmGroupBenchmark -TargetToBucket $script:GbMap -BucketNames $script:GbNames -SkipEnrichment
        $edges = @($gb.Edges)
        # 2 sources feed G1, 2 feed G2 (8000000001 feeds both groups)
        $edges.Count | Should -Be 4
        @($edges | Where-Object { $_.Bucket -eq 'G1' -and $_.SourceNPI -eq '8000000001' })[0].SharedPatients | Should -Be 45
        @($edges | Where-Object { $_.Bucket -eq 'G2' -and $_.SourceNPI -eq '8000000003' })[0].SharedPatients | Should -Be 11
        # each fixture source feeds exactly one member NPI per group
        @($edges | ForEach-Object MembersFed | Sort-Object -Unique) | Should -Be @(1)
    }

    It 'credits an NPI in two groups to both (list-valued map)' {
        $map = @{ '9000000001' = @('G1', 'G3') }
        $gb = Get-RmGroupBenchmark -TargetToBucket $map -SkipEnrichment
        @($gb.Buckets).Count | Should -Be 2
        @($gb.Buckets | ForEach-Object InboundPatients | Sort-Object -Unique) | Should -Be @(65)
        # equal volumes -> deterministic rank by bucket key
        @($gb.Buckets)[0].Bucket | Should -Be 'G1'
    }

    It 'computes group-level missed sources from the edges' {
        $gb = Get-RmGroupBenchmark -TargetToBucket $script:GbMap -BucketNames $script:GbNames -SkipEnrichment
        # G2's missed: 8000000002 feeds only G1; 8000000001 feeds both -> not missed
        $missed = @(Get-RmGroupMissedSources -Edges @($gb.Edges) -Bucket 'G2')
        $missed.Count | Should -Be 1
        $missed[0].SourceNPI | Should -Be '8000000002'
        $missed[0].PatientsToOtherGroups | Should -Be 20
        $missed[0].GroupsFed | Should -Be 1
        # G1's missed: 8000000003 feeds only G2
        @(Get-RmGroupMissedSources -Edges @($gb.Edges) -Bucket 'G1')[0].SourceNPI | Should -Be '8000000003'
    }

    It 'carries group-specific methodology notes' {
        $gb = Get-RmGroupBenchmark -TargetToBucket $script:GbMap -SkipEnrichment
        (@($gb.Notes) -join ' ') | Should -BeLike '*GROUP BENCHMARK METHOD*'
        (@($gb.Notes) -join ' ') | Should -BeLike '*MembersFed*'
        (@($gb.Notes) -join ' ') | Should -BeLike '*ORGANIZATION NPI*'
    }

    It 'returns an empty result for an empty map' {
        $gb = Get-RmGroupBenchmark -TargetToBucket @{} -SkipEnrichment
        @($gb.Buckets).Count | Should -Be 0
        @($gb.Edges).Count | Should -Be 0
    }
}

Describe 'Group outbound, specialty mix, and group trend' {
    BeforeAll {
        $script:GtMap = @{ '9000000001' = 'G1'; '9000000002' = 'G2' }
        $script:GtNames = @{ 'G1' = 'ALPHA REHAB GROUP'; 'G2' = 'BETA THERAPY' }
    }

    It 'returns outbound edges from the same single scan (CMS active)' {
        $gb = Get-RmGroupBenchmark -TargetToBucket $script:GtMap -BucketNames $script:GtNames -SkipEnrichment
        # fixture outbound: 9000000001 -> 8000000001 (99 benes); 9000000002 sends nothing
        $out = @($gb.OutboundEdges)
        $out.Count | Should -Be 1
        $out[0].Bucket | Should -Be 'G1'
        $out[0].DestNPI | Should -Be '8000000001'
        $out[0].SharedPatients | Should -Be 99
        $out[0].MembersSending | Should -Be 1
        # inbound contract unchanged by the rework
        @($gb.Buckets)[0].InboundPatients | Should -Be 65
        (@($gb.Notes) -join ' ') | Should -BeLike '*OUTBOUND rows*'
    }

    It 'specialty mix rolls up directly from the benchmark edge rows' {
        $gb = Get-RmGroupBenchmark -TargetToBucket $script:GtMap -BucketNames $script:GtNames
        $g1 = @($gb.Edges | Where-Object { $_.Bucket -eq 'G1' })
        $mix = @(Get-RmSourceSpecialtyMix -Rows $g1)
        # G1 fed by Family Medicine (45) + Orthopaedic Surgery (20)
        $mix.Count | Should -Be 2
        $mix[0].Specialty | Should -Be 'Family Medicine'
        $mix[0].SharedPatients | Should -Be 45
        $mix[0].PctOfVolume | Should -Be ([math]::Round(100.0 * 45 / 65, 1))
    }

    It 'builds a group trend across all imported hop years' {
        # hop_teaming_2021 + 2022 fixtures are on disk from earlier describes
        $t = Get-RmGroupTrend -MemberNpi @('9000000001', '9000000002') -GroupName 'ALPHA REHAB GROUP' -SkipEnrichment
        @($t.Years) | Should -Be @(2021, 2022)
        $rows = @($t.Rows)
        $rows.Count | Should -Be 2
        foreach ($r in $rows) {
            $r.InboundPatients | Should -Be 77      # org 65 + PT 12 in the hop fixture
            $r.InboundSources | Should -Be 2
            $r.MembersWithVolume | Should -Be 2
            $r.TopSources | Should -BeLike '*8000000001*'
        }
        (@($t.Notes) -join ' ') | Should -BeLike '*TODAY''s roster*'
        (@($t.Notes) -join ' ') | Should -BeLike '*intentionally excluded*'
    }

    It 'group trend refuses bad input and single-year stores' {
        { Get-RmGroupTrend -MemberNpi @('not-an-npi') } | Should -Throw '*well-formed*'
        $emptyDir = Join-Path $script:WorkDir 'gt-empty'
        New-Item -ItemType Directory -Path $emptyDir -Force | Out-Null
        $savedDir = (Get-RmConfig).DataDir
        try {
            Set-RmConfig -DataDir $emptyDir
            { Get-RmGroupTrend -MemberNpi @('9000000001') } | Should -Throw '*at least TWO*'
        } finally { Set-RmConfig -DataDir $savedDir }
    }
}

Describe 'Radius search' {
    BeforeAll {
        # Fixture centroids: 99998 is ~6.9 straight-line miles from 99999
        # (0.1 deg latitude at same longitude); 86442 is ~1,400 miles away.
        $script:RadCsv = Join-Path $script:WorkDir 'radius-centroids.csv'
        Set-Content -Path $script:RadCsv -Encoding ascii -Value @(
            'zip,lat,lon'
            '99999,40.0000,-90.0000'
            '99998,40.1000,-90.0000'
            '86442,35.1000,-114.6000'
        )
    }

    It 'computes the ZIPs inside the circle (center always included)' {
        @(Get-RmZipsInRadius -Zip 99999 -RadiusMiles 0 -CentroidPath $script:RadCsv) | Should -Be @('99999')
        @(Get-RmZipsInRadius -Zip 99999 -RadiusMiles 10 -CentroidPath $script:RadCsv) | Should -Be @('99998', '99999')
        @(Get-RmZipsInRadius -Zip 99999 -RadiusMiles 5 -CentroidPath $script:RadCsv) | Should -Be @('99999')
        { Get-RmZipsInRadius -Zip 12345 -RadiusMiles 10 -CentroidPath $script:RadCsv } | Should -Throw '*not in the Census*'
    }

    It 'Find-RmClinic -ZipList sweeps each ZIP exactly and merges results' {
        $clinics = @(Find-RmClinic -ZipList @('99998', '99999'))
        @($clinics | ForEach-Object NPI) -contains '9000000007' | Should -BeTrue    # the neighbor
        @($clinics | ForEach-Object NPI) -contains '9000000001' | Should -BeTrue    # the 99999 org
        # exact-ZIP semantics: a plain 99999 search must NOT include the neighbor
        @(@(Find-RmClinic -Zip 99999) | ForEach-Object NPI) -contains '9000000007' | Should -BeFalse
    }

    It 'radius map includes the neighbor with a DistanceMiles column and radius notes' {
        $map = Get-RmReferralMap -Zip 99999 -RadiusMiles 10 -SkipEnrichment -CentroidPath $script:RadCsv
        $map.Zip | Should -Be '99999+10mi'
        $rows = @($map.Clinics)
        @($rows | Where-Object NPI -eq '9000000007').Count | Should -Be 1
        $n = @($rows | Where-Object NPI -eq '9000000007')[0]
        # 0.1 degree latitude is ~6.9 miles
        [double]$n.DistanceMiles | Should -BeGreaterThan 6
        [double]$n.DistanceMiles | Should -BeLessThan 8
        @($rows | Where-Object NPI -eq '9000000001')[0].DistanceMiles | Should -Be 0
        # ranking and totals unchanged for the 99999 crew
        @($rows | Where-Object NPI -eq '9000000001')[0].SharedPatients | Should -Be 65
        (@($map.Notes) -join ' ') | Should -BeLike '*RADIUS SEARCH*'
        (@($map.Notes) -join ' ') | Should -BeLike '*not driving distance*'
    }

    It 'rejects a prefix center and keeps plain searches column-stable' {
        { Get-RmReferralMap -Zip '999*' -RadiusMiles 10 -CentroidPath $script:RadCsv } | Should -Throw '*full 5-digit*'
        $plain = Get-RmReferralMap -Zip 99999 -SkipEnrichment
        @($plain.Clinics)[0].PSObject.Properties['DistanceMiles'] | Should -BeNullOrEmpty
        $plain.Zip | Should -Be '99999'
    }
}

Describe 'Supplemental market and billed-services data (CMS open data)' {
    BeforeAll {
        # ZIP->county fixture: the test ZIP maps to fixture county 99001.
        $script:XwCsv = Join-Path $script:WorkDir 'xw.csv'
        Set-Content -Path $script:XwCsv -Encoding ascii -Value @('zip,fips', '99999,99001')
        $script:MkSaCsv = Join-Path $script:WorkDir 'mk-centroids.csv'
        Set-Content -Path $script:MkSaCsv -Encoding ascii -Value @(
            'zip,lat,lon', '99999,40.0000,-90.0000', '86442,35.1000,-114.6000')
    }

    It 'county market: latest full year, MA share, and a disk cache' {
        $m = Get-RmCountyMarket -Zip 99999 -CrosswalkPath $script:XwCsv
        $m.County | Should -Be 'Test County'
        $m.Year | Should -Be 2025                      # picks the LATEST year row
        $m.TotalBenes | Should -Be 12000
        $m.FfsBenes | Should -Be 7000
        $m.MaPct | Should -Be 41.7                     # 5000/12000
        (Test-Path (Join-Path $env:RM_DATA_DIR 'market-cache.json')) | Should -BeTrue
        (Get-RmCountyMarket -Zip 99999 -CrosswalkPath $script:XwCsv).TotalBenes | Should -Be 12000
    }

    It 'service profile counts ONLY therapy codes and floors distinct patients' {
        $p = (Get-RmServiceProfile -Npi 9000000001)['9000000001']
        $p.TherapyServices | Should -Be 500            # 350 + 150; the 99213 E/M row is excluded
        $p.TherapyCodes | Should -Be 2
        $p.MinDistinctPatients | Should -Be 60         # max single-code benes = a floor, never a sum
        $p.HasAnyClaims | Should -BeTrue
        $none = (Get-RmServiceProfile -Npi 9000000002)['9000000002']
        $none.HasAnyClaims | Should -BeFalse
        $none.TherapyServices | Should -Be 0
    }

    It 'analysis carries both blocks and the report writes the findings' {
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -CentroidPath $script:MkSaCsv -CrosswalkPath $script:XwCsv
        $sa.Market.MaPct | Should -Be 41.7
        $sa.ServiceProfile.TherapyServices | Should -Be 500
        (@($sa.Notes) -join ' ') | Should -BeLike '*MARKET CONTEXT*'
        (@($sa.Notes) -join ' ') | Should -BeLike '*BILLED-SERVICES PROFILE*'
        $out = Join-Path $script:WorkDir 'market-report.html'
        Export-RmSourceReportHtml -Analysis $sa -Path $out | Out-Null
        $html = Get-Content $out -Raw
        $html | Should -BeLike '*41.7% were in Medicare Advantage*'
        $html | Should -BeLike '*500*billed Medicare therapy services*'
        $html | Should -BeLike '*per 1,000 Original-Medicare beneficiaries*'
    }

    It 'a dead API degrades to a note - the analysis never fails' {
        # A crosswalk mapping to an UNCACHED county, so the API is really hit
        # (the disk cache would otherwise — correctly — answer for 99001).
        $xw2 = Join-Path $script:WorkDir 'xw2.csv'
        Set-Content -Path $xw2 -Encoding ascii -Value @('zip,fips', '99999,99002')
        $savedBase = (Get-RmConfig).CmsApiBase
        try {
            Set-RmConfig -CmsApiBase 'http://127.0.0.1:1/dataset'
            $sa = Get-RmSourceAnalysis -Npi 9000000001 -CentroidPath $script:MkSaCsv -CrosswalkPath $xw2
            $sa.TotalPatients | Should -Be 65          # the core analysis is intact
            $sa.Market | Should -BeNullOrEmpty
            (@($sa.Notes) -join ' ') | Should -BeLike '*unavailable*'
        } finally { Set-RmConfig -CmsApiBase $savedBase }
    }
}

Describe 'Local rosters (NPPES bulk index + Care Compare groups)' {
    BeforeAll {
        # Tiny NPPES bulk fixture in the REAL dissemination layout (quoted
        # CSV, columns resolved by name).
        $script:NppesCsv = Join-Path $script:WorkDir 'npidata_pfile_20050523-20260712.csv'
        Set-Content -Path $script:NppesCsv -Encoding ascii -Value @(
            ('"NPI","Entity Type Code","Provider Organization Name (Legal Business Name)","Provider Last Name (Legal Name)","Provider First Name","Provider Business Practice Location Address City Name","Provider Business Practice Location Address State Name","Provider Business Practice Location Address Postal Code","Healthcare Provider Taxonomy Code_1","Provider Enumeration Date",' + ((2..15 | ForEach-Object { '"Healthcare Provider Taxonomy Code_' + $_ + '"' }) -join ','))
            '"7700000001","2","BULK REHAB PARTNERS, LLC","","","ROLLA","MO","654010000","261QP2000X","06/15/2008","",""'
            '"7700000002","1","","BULKPT","BOB","ROLLA","MO","654011234","225100000X","01/02/2015","",""'
            # therapy only as a SECONDARY taxonomy (primary is out of scope):
            # a live 10-mile sweep missed 43 real providers shaped like this.
            '"7700000007","1","","SECONDTAX","SAM","ROLLA","MO","654010000","208100000X","03/03/2019","225100000X",""'
        )
        $script:DacCsv = Join-Path $script:WorkDir 'DAC_fixture.csv'
        Set-Content -Path $script:DacCsv -Encoding ascii -Value @(
            'NPI,Ind_PAC_ID,Provider Last Name,Provider First Name,pri_spec,Facility Name,org_pac_id,City/Town,State'
            '7700000002,1111,BULKPT,BOB,PHYSICAL THERAPY,BULK REHAB PARTNERS,5555,ROLLA,MO'
            '7700000003,2222,SECOND,SUE,OCCUPATIONAL THERAPY,BULK REHAB PARTNERS,5555,ROLLA,MO'
            '7700000004,3333,THIRD,TOM,SPEECH LANGUAGE PATHOLOGIST,BULK REHAB PARTNERS,5555,ROLLA,MO'
            '7700000005,4444,DOCTOR,DAN,INTERNAL MEDICINE,BULK REHAB PARTNERS,5555,ROLLA,MO'
            '7700000006,5551,OTHER,OLA,PHYSICAL THERAPY,ELSEWHERE PT,7777,SALEM,MO'
        )
    }

    It 'builds the NPPES index and answers lookups OFFLINE with NUCC names' {
        $r = Import-RmNppesBulk -Path $script:NppesCsv
        $r.Rows | Should -Be 3
        # kill the live registry: bulk must answer alone
        try {
            $script:RmSaved = (Get-RmConfig).NppesUrl
            (Get-RmConfig).NppesUrl = 'http://127.0.0.1:1/nppes/'
            $d = Get-RmProviderDetail -Npi @('7700000001', '7700000002') -RequireZip
            $d['7700000001'].Name | Should -Be 'BULK REHAB PARTNERS, LLC'
            $d['7700000001'].Zip | Should -Be '65401'
            $d['7700000001'].Specialty | Should -BeLike '*Physical Therapy*'   # NUCC name, not a bare code
            $d['7700000002'].Name | Should -Be 'BOB BULKPT'
        } finally { (Get-RmConfig).NppesUrl = $script:RmSaved }
    }

    It 'suggests affiliated therapy clinicians from the Care Compare index' {
        (Import-RmCareCompare -Path $script:DacCsv).Rows | Should -Be 5
        $aff = @(Get-RmAffiliatedNpi -Npi 7700000002)
        # same group 5555, therapy only: SUE + TOM in; the internist and the
        # other-group PT stay out
        @($aff | ForEach-Object NPI) | Sort-Object | Should -Be @('7700000003', '7700000004')
        $aff[0].Group | Should -Be 'BULK REHAB PARTNERS'
        @(Get-RmAffiliatedNpi -Npi 7700000006).Count | Should -Be 0   # solo in its group
    }

    It 'peer discovery uses the bulk index (complete, offline, no API cap)' {
        # ZIP 65401 providers exist ONLY in the bulk index, never in the API
        # double — so finding them proves the local sweep is doing the work.
        (Get-RmConfig).NppesUrl | Out-Null
        $saved = (Get-RmConfig).NppesUrl
        try {
            (Get-RmConfig).NppesUrl = 'http://127.0.0.1:1/nppes/'
            $c = @(Find-RmClinic -Zip 65401)
            @($c | ForEach-Object NPI) | Sort-Object | Should -Be @('7700000001', '7700000002', '7700000007')
            # matched on a SECONDARY taxonomy, and labeled by the code that matched
            @($c | Where-Object NPI -eq '7700000007')[0].Taxonomy | Should -BeLike '*Physical Therapist*'
            @($c | Where-Object NPI -eq '7700000001')[0].Type | Should -Be 'Organization'
            @($c | Where-Object NPI -eq '7700000001')[0].Taxonomy | Should -BeLike '*Physical Therapy*'
            # enumeration date is normalized to the ISO form the vintage
            # check parses (NPPES bulk ships MM/DD/YYYY)
            @($c | Where-Object NPI -eq '7700000002')[0].Enumerated | Should -Be '2015-01-02'
            # ...and OrganizationsOnly still filters by clinic taxonomy
            @(@(Find-RmClinic -Zip 65401 -OrganizationsOnly) | ForEach-Object NPI) | Should -Be @('7700000001')
        } finally { (Get-RmConfig).NppesUrl = $saved }
    }

    It 'falls back to the live registry where the bulk index has no coverage' {
        # A stale monthly file must never make a real ZIP look empty: ZIP
        # 99999 is absent from the bulk index but present in the registry.
        $c = @(Find-RmClinic -Zip 99999)
        @($c | ForEach-Object NPI) -contains '9000000001' | Should -BeTrue
        $c.Count | Should -BeGreaterThan 1
    }

    It 'county market resolves from the local enrollment index (offline)' {
        $enr = Join-Path $script:WorkDir 'enrollment.csv'
        Set-Content -Path $enr -Encoding ascii -Value @(
            'BENE_FIPS_CD,YEAR,MONTH,BENE_COUNTY_DESC,BENE_STATE_ABRVTN,TOT_BENES,ORGNL_MDCR_BENES,MA_AND_OTH_BENES'
            '99003,2024,Year,Local County,MO,8000,5000,3000'
            '99003,2025,Year,Local County,MO,9000,5000,4000'
            '99003,2025,January,Local County,MO,8900,4990,3910'   # monthly rows ignored
        )
        (Import-RmEnrollment -Path $enr).Rows | Should -Be 3
        $xw = Join-Path $script:WorkDir 'xw3.csv'
        Set-Content -Path $xw -Encoding ascii -Value @('zip,fips', '99999,99003')
        $savedBase = (Get-RmConfig).CmsApiBase
        try {
            Set-RmConfig -CmsApiBase 'http://127.0.0.1:1/dataset'   # API dead: index must answer
            $m = Get-RmCountyMarket -Zip 99999 -CrosswalkPath $xw
            $m.County | Should -Be 'Local County'
            $m.Year | Should -Be 2025
            $m.TotalBenes | Should -Be 9000
            $m.MaPct | Should -Be 44.4
        } finally { Set-RmConfig -CmsApiBase $savedBase }
    }

    It 'the resource downloader writes a checksummed manifest' {
        $dest = Join-Path $script:WorkDir 'resources'
        $r = Save-RmLocalResources -Destination $dest
        Test-Path $r.Manifest | Should -BeTrue
        $man = Get-Content $r.Manifest -Raw
        $man | Should -BeLike '*NPPES bulk provider file*'
        $man | Should -BeLike '*Care Compare clinician file*'
        $man | Should -BeLike '*Medicare Monthly Enrollment*'
        $man | Should -BeLike '*sha256:*'
        # the NPPES file has no stable direct link: reported, never guessed
        @($r.Files | Where-Object Key -eq 'nppes')[0].Status | Should -BeLike 'MANUAL*'
        # a download failure is reported per-file, never thrown
        @($r.Files).Count | Should -Be 3
    }

    It 'the analysis note lists affiliated NPIs not yet combined' {
        # 9000000001 is not in the DAC fixture: no note, no error. Then a
        # fixture where the analyzed NPI has group-mates.
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -SkipCompetitors -CentroidPath $script:SaCsv
        (@($sa.Notes) -join ' ') | Should -Not -BeLike '*AFFILIATED CLINICIANS*'
        Set-Content -Path $script:DacCsv -Encoding ascii -Value @(
            'NPI,Ind_PAC_ID,Provider Last Name,Provider First Name,pri_spec,Facility Name,org_pac_id,City/Town,State'
            '9000000001,1111,CLINIC,TEST,PHYSICAL THERAPY,TEST REHAB,5555,TESTVILLE,MO'
            '9000000002,2222,THERAPIST,PAT,PHYSICAL THERAPY,TEST REHAB,5555,TESTVILLE,MO'
        )
        Import-RmCareCompare -Path $script:DacCsv | Out-Null
        $sa2 = Get-RmSourceAnalysis -Npi 9000000001 -SkipCompetitors -CentroidPath $script:SaCsv
        (@($sa2.Notes) -join ' ') | Should -BeLike '*AFFILIATED CLINICIANS*PAT THERAPIST (9000000002)*'
        # ...and combining them clears the suggestion
        $sa3 = Get-RmSourceAnalysis -Npi @('9000000001', '9000000002') -SkipCompetitors -CentroidPath $script:SaCsv
        (@($sa3.Notes) -join ' ') | Should -Not -BeLike '*AFFILIATED CLINICIANS*'
    }
}

Describe 'Taxonomy scope (outpatient PT/OT/speech only)' {
    # Audited against NUCC v25.1: subspecialty therapists are IN (they are
    # therapists), assistants/physicians/cardiac/substance rehab are OUT.
    It 'includes subspecialty PT and OT, SLPs, speech clinics, and CORFs' {
        $c = @(Find-RmClinic -Zip 66666)
        $npis = @($c | ForEach-Object NPI)
        foreach ($want in '6600000001', '6600000002', '6600000003', '6600000004', '6600000005') {
            $npis -contains $want | Should -BeTrue -Because "NPI $want fits the PT/OT/speech scope"
        }
        # subspecialty labels come through honestly from the registry
        @($c | Where-Object NPI -eq '6600000001')[0].Taxonomy | Should -Be 'Physical Therapist Orthopedic'
        # NPPES sends "Speech-Language Pathologist, " — no dangling separator
        @($c | Where-Object NPI -eq '6600000003')[0].Taxonomy | Should -Be 'Speech-Language Pathologist'
        @($c | Where-Object NPI -eq '6600000004')[0].Taxonomy | Should -BeLike '*Hearing and Speech*'
        @($c | Where-Object NPI -eq '6600000005')[0].Taxonomy | Should -BeLike '*CORF*'
    }
    It 'excludes assistants, physiatrists, cardiac/substance rehab, and counselors' {
        $c = @(Find-RmClinic -Zip 66666)
        $npis = @($c | ForEach-Object NPI)
        foreach ($no in '6600000006', '6600000007', '6600000008', '6600000009', '6600000010', '6600000011') {
            $npis -contains $no | Should -BeFalse -Because "NPI $no is outside the PT/OT/speech scope"
        }
        $c.Count | Should -Be 5
    }
    It 'OrganizationsOnly keeps only the clinic-taxonomy codes' {
        @(@(Find-RmClinic -Zip 66666 -OrganizationsOnly) | ForEach-Object NPI) | Sort-Object |
            Should -Be @('6600000004', '6600000005')
    }
}

Describe 'Source analysis report' {
    BeforeAll {
        $script:SaCsv = Join-Path $script:WorkDir 'sa-centroids.csv'
        Set-Content -Path $script:SaCsv -Encoding ascii -Value @(
            'zip,lat,lon'
            '99999,40.0000,-90.0000'
            '86442,35.1000,-114.6000'
        )
    }

    It 'computes shares, cumulative shares, and concentration (CMS active)' {
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -CentroidPath $script:SaCsv
        $sa.TotalPatients | Should -Be 65
        $sa.SourceCount | Should -Be 2
        $sa.Top1Pct | Should -Be 69.2
        $sa.Top5Pct | Should -Be 100
        # HHI from unrounded shares: (45/65*100)^2 + (20/65*100)^2 = 5740
        $sa.HHI | Should -Be 5740
        $sa.Concentration | Should -BeLike 'HIGH*'
        $rows = @($sa.Sources)
        $rows[0].Rank | Should -Be 1
        $rows[0].CumulativePct | Should -Be 69.2
        $rows[1].CumulativePct | Should -Be 100
        @($sa.SpecialtyMix).Count | Should -Be 2
    }

    It 'buckets volume into distance bands from real source ZIPs' {
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -CentroidPath $script:SaCsv
        $b = @($sa.DistanceBands)
        @($b | Where-Object Band -eq '0-5 mi')[0].SharedPatients | Should -Be 45
        @($b | Where-Object Band -eq '50+ mi')[0].SharedPatients | Should -Be 20
        @($b | Where-Object Band -eq 'Not locatable')[0].SharedPatients | Should -Be 0
        (@($b) | Measure-Object Pct -Sum).Sum | Should -Be 100
    }

    It 'adds the referral-lag profile on hop data' {
        Set-RmActiveDataset -Source hop-teaming -Year 2022 | Out-Null
        try {
            $sa = Get-RmSourceAnalysis -Npi 9000000001 -CentroidPath $script:SaCsv
            $sa.IsHop | Should -BeTrue
            $w = @($sa.WaitBands)
            @($w | Where-Object Band -eq '8-30 days')[0].SharedPatients | Should -Be 45   # 12.5d avg
            @($w | Where-Object Band -eq '31-90 days')[0].SharedPatients | Should -Be 20  # 30.0d avg
            @($sa.Sources)[0].PSObject.Properties['AvgDayWait'] | Should -Not -BeNullOrEmpty
        } finally { Set-RmActiveDataset -Source cms-pspp -Year 2015 | Out-Null }
    }

    It 'renders the self-contained report with charts, findings, and methodology' {
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -CentroidPath $script:SaCsv
        $out = Join-Path $script:WorkDir 'source-report.html'
        $r = Export-RmSourceReportHtml -Analysis $sa -Path $out
        $r.Sources | Should -Be 2
        $html = Get-Content $out -Raw
        $html | Should -BeLike '*Referral Source Analysis*'
        ([regex]::Matches($html, '<svg ')).Count | Should -BeGreaterOrEqual 4
        $html | Should -BeLike '*Concentration (HHI)*'
        $html | Should -BeLike '*DAVID DOCTOR*'
        $html | Should -BeLike '*Key findings*'
        $html | Should -BeLike '*SOURCE ANALYSIS METHOD*'
        # fully self-contained: no external fetches at all
        $html | Should -Not -BeLike '*http*://*unpkg*'
        $html | Should -Not -BeLike '*<script src*'
    }

    It 'reports an honest empty analysis for an NPI with no inbound pairs' {
        $sa = Get-RmSourceAnalysis -Npi 8000000002 -CentroidPath $script:SaCsv
        $sa.TotalPatients | Should -Be 0
        $sa.SourceCount | Should -Be 0
        # never call an empty scan "diversified" — there is nothing to spread
        $sa.Concentration | Should -BeLike 'n/a*'
        $out = Join-Path $script:WorkDir 'empty-report.html'
        (Export-RmSourceReportHtml -Analysis $sa -Path $out).Sources | Should -Be 0
        $html = Get-Content $out -Raw
        $html | Should -BeLike '*0*Shared patients*'
        # explains the situation instead of drawing empty charts
        $html | Should -BeLike '*No measured referral volume*'
        $html | Should -BeLike '*11-patient privacy floor*'
        $html | Should -BeLike '*billed under a different NPI*'
        $html | Should -Not -BeLike '*diversified referral base*'
        $html | Should -Not -BeLike '*Concentration curve*'
        $html | Should -Not -BeLike '*Specialty mix*'
        $html | Should -Not -BeLike '*Source detail*'
        # the concentration KPI cards read as not-applicable, not as 0%
        $html | Should -BeLike '*&mdash;*Top-5 dependence*'
    }

    It 'ranks the practice against every rehab provider within the radius' {
        # 99998 is ~6.9 mi from 99999, so the default 10-mile sweep must pull
        # in the neighbor; 86442 stays ~1,400 mi away.
        $script:CompCsv = Join-Path $script:WorkDir 'comp-centroids.csv'
        Set-Content -Path $script:CompCsv -Encoding ascii -Value @(
            'zip,lat,lon'
            '99999,40.0000,-90.0000'
            '99998,40.1000,-90.0000'
            '86442,35.1000,-114.6000'
            '88888,41.0000,-90.0000'
        )
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -CentroidPath $script:CompCsv
        $c = $sa.Competitive
        $c | Should -Not -BeNullOrEmpty
        $c.RadiusMiles | Should -Be 10
        $c.ZipCount | Should -Be 2            # 99999 + 99998
        $c.ProviderCount | Should -Be 4       # org, individual PT, new grad, neighbor
        $c.ProvidersWithVolume | Should -Be 2
        $c.Rank | Should -Be 1
        $c.RegionPatients | Should -Be 88     # 65 (org) + 23 (individual PT)
        $c.SharePct | Should -Be 73.9
        $peers = @($c.Peers)
        $peers[0].You | Should -Be '>> YOU'
        $peers[0].DistanceMiles | Should -Be 0
        $peers[1].NPI | Should -Be '9000000002'
        $peers[1].SharedPatients | Should -Be 23
        $peers[1].SharePct | Should -Be 26.1
        $nb = @($peers | Where-Object NPI -eq '9000000007')[0]
        [double]$nb.DistanceMiles | Should -BeGreaterThan 6      # the neighbor, located
        [double]$nb.DistanceMiles | Should -BeLessThan 8
        @($c.Competitors)[0].NPI | Should -Be '9000000002'       # top competitor excludes self
        (@($sa.Notes) -join ' ') | Should -BeLike '*COMPETITIVE LANDSCAPE*'
    }

    It 'renders the competitive card; -SkipCompetitors skips sweep and card' {
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -CentroidPath $script:CompCsv
        $out = Join-Path $script:WorkDir 'comp-report.html'
        Export-RmSourceReportHtml -Analysis $sa -Path $out | Out-Null
        $html = Get-Content $out -Raw
        $html | Should -BeLike '*Competitive landscape*'
        $html | Should -BeLike '*Rank within 10 mi*'
        $html | Should -BeLike '*>YOU<*'                         # highlighted own row
        $html | Should -BeLike '*ranks #1*'                      # auto-written finding
        ([regex]::Matches($html, '<svg ')).Count | Should -BeGreaterOrEqual 5
        $sk = Get-RmSourceAnalysis -Npi 9000000001 -SkipCompetitors -CentroidPath $script:CompCsv
        $sk.Competitive | Should -BeNullOrEmpty
        $out2 = Join-Path $script:WorkDir 'skip-report.html'
        Export-RmSourceReportHtml -Analysis $sk -Path $out2 | Out-Null
        (Get-Content $out2 -Raw) | Should -Not -BeLike '*Competitive landscape*'
    }

    It 'degrades honestly when the area has no other rehab providers' {
        # 8000000002 sits in ZIP 86442 where the registry lists no rehab
        # providers at all: the landscape is self-only, never an error.
        $sa = Get-RmSourceAnalysis -Npi 8000000002 -CentroidPath $script:CompCsv
        $c = $sa.Competitive
        $c.ProviderCount | Should -Be 1
        $c.Rank | Should -Be 1
        $c.RegionPatients | Should -Be 0
        $c.SharePct | Should -Be 0
        @($c.Competitors).Count | Should -Be 0
    }

    It 'combines multiple NPIs and excludes internal patient flows' {
        # 8000000001->9000000001 (45) and 9000000001->8000000001 (99) become
        # INTERNAL once both NPIs are members; only 8000000002's 20 remain.
        $sa = Get-RmSourceAnalysis -Npi @('9000000001', '8000000001') -CentroidPath $script:CompCsv
        $sa.NpiCount | Should -Be 2
        $sa.Npi | Should -Be '9000000001'                       # first = primary
        $sa.TotalPatients | Should -Be 20
        $sa.SourceCount | Should -Be 1
        @($sa.Sources)[0].SourceNPI | Should -Be '8000000002'
        (@($sa.Notes) -join ' ') | Should -BeLike '*COMBINED ANALYSIS*'
        (@($sa.Notes) -join ' ') | Should -BeLike '*SMALL-PRACTICE NOTE*'   # 20 < 1000
    }

    It 'merges a source feeding two member NPIs into one summed row' {
        $sa = Get-RmSourceAnalysis -Npi @('9000000001', '9000000002') -SkipCompetitors -CentroidPath $script:CompCsv
        # 8000000001 feeds both members (45 + 12 = 57); plus 20 and 11.
        $sa.TotalPatients | Should -Be 88
        $sa.SourceCount | Should -Be 3
        $top = @($sa.Sources)[0]
        $top.SourceNPI | Should -Be '8000000001'
        $top.SharedPatients | Should -Be 57
        $top.PctOfVolume | Should -Be 64.8
    }

    It 'ties share a rank instead of an arbitrary alphabetical position' {
        # 9000000005 (zero measured volume) must tie with the other
        # zero-volume providers at rank 3 (after 65 and 23), not sort-order.
        $sa = Get-RmSourceAnalysis -Npi 9000000005 -CentroidPath $script:CompCsv
        $sa.Competitive.Rank | Should -Be 3
        @(@($sa.Competitive.Peers) | Where-Object { $_.You }).Count | Should -Be 1
    }

    It 'always shows the practice row even when it ranks below the top 15' {
        # ZIP 88888 holds 201 fixture PTs, all with zero measured volume:
        # everyone ties at rank 1, the table shows top 15 + the practice.
        $sa = Get-RmSourceAnalysis -Npi 8600000199 -CentroidPath $script:CompCsv
        $c = $sa.Competitive
        $c.ProviderCount | Should -Be 201
        $c.Rank | Should -Be 1
        @($c.Peers).Count | Should -Be 16
        @($c.Peers)[15].You | Should -Be '>> YOU'
        $out = Join-Path $script:WorkDir 'zero-volume-report.html'
        Export-RmSourceReportHtml -Analysis $sa -Path $out | Out-Null
        $html = Get-Content $out -Raw
        $html | Should -BeLike '*no measured inbound volume*'
        # a market where nobody has measured volume: the peer table lists only
        # this practice and says plainly how many providers were left out.
        ([regex]::Matches($html, '<tr class="you">')).Count | Should -Be 1
        $html | Should -BeLike '*200*are not shown here*'
        $html | Should -BeLike '*privacy floor*'
    }

    It 'measures year-over-year performance across every imported year' {
        # Fixture store holds hop 2021 and 2022 with identical rows, so the
        # per-year numbers must match and retention must be a clean 100%.
        $t = Get-RmSourceTrend -Npi 9000000001 -SkipEnrichment
        $rows = @($t.Years)
        $rows.Count | Should -Be 2
        $rows[0].Year | Should -Be 2021
        $rows[1].Year | Should -Be 2022
        foreach ($r in $rows) {
            $r.SharedPatients | Should -Be 65      # 45 + 20, internal flows out
            $r.SourceCount | Should -Be 2
            $r.HHI | Should -Be 5740
        }
        # first year has no prior year: blank, never a measured-looking 0
        $rows[0].RetentionPct | Should -Be ''
        $rows[0].NewSources | Should -Be ''
        $rows[0].RetainedSources | Should -Be ''
        $rows[0].LostSources | Should -Be ''
        $rows[1].RetentionPct | Should -Be 100
        $rows[1].RetainedSources | Should -Be 2
        $rows[1].NewSources | Should -Be 0
        $rows[1].LostSources | Should -Be 0
        $t.VolumeChangePct | Should -Be 0
        $t.FirstYear | Should -Be 2021
        $t.LastYear | Should -Be 2022
        @($t.Movers | Where-Object Status -eq 'Steady').Count | Should -Be 2
        (@($t.Notes) -join ' ') | Should -BeLike '*YEAR-OVER-YEAR METHOD*'
    }

    It 'classifies gained, lost, and changed sources between the first and last year' {
        # A third hop year (2023) with one source grown, one dropped, one new.
        $src23 = Join-Path $script:WorkDir 'DocGraph_Hop_Teaming_2023.csv'
        Set-Content -Path $src23 -Encoding ascii -NoNewline -Value (@(
            'from_npi,to_npi,patient_count,transaction_count,average_day_wait,std_day_wait'
            '8000000001,9000000001,60,65,10.0,5.0'      # grew 45 -> 60
            '8000000009,9000000001,30,30,20.0,5.0'      # brand new
        ) -join "`n")                                   # 8000000002 (20) is gone
        Import-RmDataset -Path $src23 | Out-Null
        $y23 = Join-Path $env:RM_DATA_DIR 'DocGraph_Hop_Teaming_2023.csv'
        try {
            $t = Get-RmSourceTrend -Npi 9000000001 -SkipEnrichment
            $rows = @($t.Years)
            $rows.Count | Should -Be 3
            $last = $rows[2]
            $last.Year | Should -Be 2023
            $last.SharedPatients | Should -Be 90
            $last.NewSources | Should -Be 1
            $last.LostSources | Should -Be 1
            $last.RetainedSources | Should -Be 1
            $last.RetentionPct | Should -Be 50
            $t.VolumeChangePct | Should -Be 38.5      # 65 -> 90
            $byNpi = @{}; foreach ($m in @($t.Movers)) { $byNpi[$m.SourceNPI] = $m }
            $byNpi['8000000001'].Status | Should -Be 'Grew'
            $byNpi['8000000001'].Change | Should -Be 15
            $byNpi['8000000002'].Status | Should -Be 'Lost'
            $byNpi['8000000002'].Change | Should -Be -20
            $byNpi['8000000009'].Status | Should -Be 'New'
            @($t.Gained)[0].SourceNPI | Should -Be '8000000009'   # +30 tops +15
            @($t.Lost)[0].SourceNPI | Should -Be '8000000002'
            # movers must be NAMED even when they sit outside a year's top 25
            $named = Get-RmSourceTrend -Npi 9000000001
            @(@($named.Gained) | Where-Object SourceNPI -eq '8000000009')[0].SourceName |
                Should -Be 'NEWCOMER IMAGING LLC'
        } finally {
            Remove-Item $y23, "$y23.rows" -Force -ErrorAction SilentlyContinue
            Set-RmActiveDataset -Source cms-pspp -Year 2015 | Out-Null   # restore state for later tests
        }
    }

    It 'renders the year-over-year section with charts and mover tables' {
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -SkipCompetitors -CentroidPath $script:SaCsv
        $sa = Add-RmSourceTrend -Analysis $sa -SkipEnrichment
        $sa.Trend | Should -Not -BeNullOrEmpty
        $out = Join-Path $script:WorkDir 'trend-report.html'
        Export-RmSourceReportHtml -Analysis $sa -Path $out | Out-Null
        $html = Get-Content $out -Raw
        $html | Should -BeLike '*Year-over-year performance*'
        $html | Should -BeLike '*Referral-source retention*'
        $html | Should -BeLike '*Biggest gains*'
        $html | Should -BeLike '*Biggest declines*'
        $html | Should -BeLike '*YEAR-OVER-YEAR METHOD*'          # notes merged in
        ([regex]::Matches($html, '<svg ')).Count | Should -BeGreaterOrEqual 6
        $html | Should -Not -BeLike '*<script src*'
        # a report WITHOUT a trend must not grow the section
        $plain = Get-RmSourceAnalysis -Npi 9000000001 -SkipCompetitors -CentroidPath $script:SaCsv
        $out2 = Join-Path $script:WorkDir 'no-trend-report.html'
        Export-RmSourceReportHtml -Analysis $plain -Path $out2 | Out-Null
        (Get-Content $out2 -Raw) | Should -Not -BeLike '*Year-over-year performance*'
    }

    It 'handles a practice with no volume in its early years (newer practice)' {
        # 9000000005 (NEW GRAD) has no pairs in 2021/2022; a synthetic 2023
        # gives it its first volume. Leading zero years must be called out,
        # and growth reported from the first ACTIVE year rather than from a
        # zero base (which would otherwise be an undefined percentage).
        $src = Join-Path $script:WorkDir 'DocGraph_Hop_Teaming_2023.csv'
        Set-Content -Path $src -Encoding ascii -NoNewline -Value (@(
            'from_npi,to_npi,patient_count,transaction_count,average_day_wait,std_day_wait'
            '8000000001,9000000005,25,25,9.0,4.0'
        ) -join "`n")
        Import-RmDataset -Path $src | Out-Null
        $imported = Join-Path $env:RM_DATA_DIR 'DocGraph_Hop_Teaming_2023.csv'
        try {
            $t = Get-RmSourceTrend -Npi 9000000005 -SkipEnrichment
            $rows = @($t.Years)
            $rows[0].SharedPatients | Should -Be 0
            $rows[$rows.Count - 1].SharedPatients | Should -Be 25
            $t.VolumeChangePct | Should -Be ''          # undefined from a zero base
            $t.ActiveFromYear | Should -Be 2023
            $t.ActiveChangePct | Should -Be ''          # only one active year
            (@($t.Notes) -join ' ') | Should -BeLike '*NO MEASURED VOLUME IN 2021, 2022*'
            # the report must not claim a percentage it cannot compute
            $sa = Get-RmSourceAnalysis -Npi 9000000005 -SkipCompetitors -CentroidPath $script:SaCsv
            $sa.Trend = $t
            $out = Join-Path $script:WorkDir 'newer-practice-report.html'
            Export-RmSourceReportHtml -Analysis $sa -Path $out | Out-Null
            $html = Get-Content $out -Raw
            $html | Should -BeLike '*Year-over-year performance*'
            $html | Should -Not -BeLike '*volume grew *%*'
        } finally {
            Remove-Item $imported, "$imported.rows" -Force -ErrorAction SilentlyContinue
            Set-RmActiveDataset -Source cms-pspp -Year 2015 | Out-Null
        }
    }

    It 'rolls sources up by ZIP for the embedded heat map (no extra scan)' {
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -SkipCompetitors -CentroidPath $script:SaCsv
        $geo = @($sa.Geo)
        $geo.Count | Should -Be 2
        $geo[0].Zip | Should -Be '99999'                 # 45 patients beats 20
        $geo[0].SharedPatients | Should -Be 45
        $geo[0].Sources | Should -Be 1
        $geo[0].PctOfVolume | Should -Be 69.2
        $geo[0].TopSource | Should -Be 'DAVID DOCTOR'
        $geo[0].Lat | Should -Be 40.0                    # fixture centroid
        $geo[1].Zip | Should -Be '86442'
        $geo[1].SharedPatients | Should -Be 20
        $sa.GeoUnmappedPatients | Should -Be 0
        $sa.Practice.Lat | Should -Be 40.0               # the practice pin
    }

    It 'merges member NPIs into the same ZIP roll-up and counts unlocatable volume' {
        # 8000000003 has no NPPES record -> its 11 patients are unmappable.
        $sa = Get-RmSourceAnalysis -Npi @('9000000001', '9000000002') -SkipCompetitors -CentroidPath $script:SaCsv
        $geo = @($sa.Geo)
        @($geo | Where-Object Zip -eq '99999')[0].SharedPatients | Should -Be 57   # merged 45+12
        $sa.GeoUnmappedPatients | Should -Be 11
        $mapped = 0; foreach ($g in $geo) { $mapped += $g.SharedPatients }
        ($mapped + $sa.GeoUnmappedPatients) | Should -Be $sa.TotalPatients         # every patient accounted for
    }

    It 'embeds the heat map in the report with the bundled library and offline guard' {
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -SkipCompetitors -CentroidPath $script:SaCsv
        $out = Join-Path $script:WorkDir 'geo-report.html'
        Export-RmSourceReportHtml -Analysis $sa -Path $out | Out-Null
        $html = Get-Content $out -Raw
        $html | Should -BeLike '*Referral geography*'
        $html | Should -BeLike '*id="rm-map"*'
        $html | Should -BeLike '*typeof L === ''undefined''*'      # offline guard
        $html | Should -BeLike '*"z":"99999"*'                     # circle data embedded
        $html | Should -BeLike '*prac-pin*'
        $html | Should -Not -BeLike '*__RM_LEAFLET*'               # placeholders resolved
        $html | Should -Not -BeLike '*<script src*'                # library inlined, not fetched
        $html | Should -Not -BeLike '*unpkg*'
        $html.Length | Should -BeGreaterThan 150000                # the bundle is actually inside
        $html | Should -BeLike '*Patients from ZIP*'               # volume legend control
        $html | Should -BeLike '*Red pin = the practice*'
        $html | Should -BeLike '*legend.addTo(map)*'
        # the chart-svg rule must stay SCOPED: a global "svg {" rule collapses
        # Leaflet's attribute-sized overlay to 0x0 and hides every circle
        # (found by probing rendered geometry — the map looked empty).
        $html | Should -BeLike '*.body svg {*'
        ($html -split "`n" | Where-Object { $_ -match '^\s*svg \{' }).Count | Should -Be 0
        # zero-volume report: no map section, no library payload
        $sk = Get-RmSourceAnalysis -Npi 8000000002 -SkipCompetitors -CentroidPath $script:SaCsv
        $out2 = Join-Path $script:WorkDir 'geo-empty-report.html'
        Export-RmSourceReportHtml -Analysis $sk -Path $out2 | Out-Null
        $html2 = Get-Content $out2 -Raw
        $html2 | Should -Not -BeLike '*id="rm-map"*'
        $html2 | Should -Not -BeLike '*__RM_LEAFLET*'
        $html2.Length | Should -BeLessThan 150000
    }

    It 'puts a source at exactly 5.0 miles into the 5-10 band (edges are half-open)' {
        # 0.0724 deg of latitude ≈ 5.0 miles: the rounded distance lands
        # exactly on the band edge, and the contract is [min, max).
        $edgeCsv = Join-Path $script:WorkDir 'edge-centroids.csv'
        Set-Content -Path $edgeCsv -Encoding ascii -Value @(
            'zip,lat,lon'
            '99999,40.0000,-90.0000'
            '86442,40.0724,-90.0000'
        )
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -SkipCompetitors -CentroidPath $edgeCsv
        $ortho = @($sa.Sources | Where-Object SourceNPI -eq '8000000002')[0]
        [double]$ortho.DistanceMiles | Should -Be 5.0
        $b = @($sa.DistanceBands)
        @($b | Where-Object Band -eq '0-5 mi')[0].SharedPatients | Should -Be 45    # the 0.0-mile source
        @($b | Where-Object Band -eq '5-10 mi')[0].SharedPatients | Should -Be 20   # the 5.0-mile source
    }

    It 'reports a single-source practice as maximum concentration (HHI 10,000)' {
        Set-RmActiveDataset -Source hop-teaming -Year 2022 | Out-Null
        try {
            $sa = Get-RmSourceAnalysis -Npi 9000000002 -SkipCompetitors -CentroidPath $script:SaCsv
            $sa.SourceCount | Should -Be 1
            $sa.HHI | Should -Be 10000
            $sa.Top1Pct | Should -Be 100
            $sa.Concentration | Should -BeLike 'HIGH*'
            @($sa.Sources)[0].CumulativePct | Should -Be 100
        } finally { Set-RmActiveDataset -Source cms-pspp -Year 2015 | Out-Null }
    }

    It 'volume-weights AvgDayWait when merging a source across member NPIs' {
        Set-RmActiveDataset -Source hop-teaming -Year 2022 | Out-Null
        try {
            $sa = Get-RmSourceAnalysis -Npi @('9000000001', '9000000002') -SkipCompetitors -CentroidPath $script:SaCsv
            # 8000000001 feeds both members: (45x12.5 + 12x7.5) / 57 = 11.447 -> 11.4
            $m = @($sa.Sources | Where-Object SourceNPI -eq '8000000001')[0]
            [double]$m.AvgDayWait | Should -Be 11.4
            # and the merged edge lands in the 8-30 day band with its full 57
            $w = @($sa.WaitBands)
            @($w | Where-Object Band -eq '8-30 days')[0].SharedPatients | Should -Be 57
            @($w | Where-Object Band -eq '31-90 days')[0].SharedPatients | Should -Be 20
        } finally { Set-RmActiveDataset -Source cms-pspp -Year 2015 | Out-Null }
    }

    It 'scales to a full seven-year span (2016-2022) for large and small practices' {
        # The real deliverable spans 2016-2022. Build seven years in an
        # isolated store and check the whole chain: per-year rows in order,
        # six retention transitions, first-vs-last movers across the span,
        # and a report whose charts carry one column per year.
        $sevenDir = Join-Path $script:WorkDir 'seven-years'
        New-Item -ItemType Directory -Path $sevenDir -Force | Out-Null
        # Big practice 9000000001 grows 100 -> 400; small practice 9000000002
        # holds ~12; one source joins in 2019, another leaves after 2020.
        $plan = @{
            2016 = @('8000000001,9000000001,100,100,10.0,5.0', '8000000002,9000000001,40,40,20.0,5.0', '8000000003,9000000002,12,12,8.0,3.0')
            2017 = @('8000000001,9000000001,150,150,10.0,5.0', '8000000002,9000000001,45,45,20.0,5.0', '8000000003,9000000002,12,12,8.0,3.0')
            2018 = @('8000000001,9000000001,200,200,10.0,5.0', '8000000002,9000000001,50,50,20.0,5.0', '8000000003,9000000002,13,13,8.0,3.0')
            2019 = @('8000000001,9000000001,250,250,10.0,5.0', '8000000002,9000000001,55,55,20.0,5.0', '8000000009,9000000001,30,30,15.0,5.0', '8000000003,9000000002,14,14,8.0,3.0')
            2020 = @('8000000001,9000000001,300,300,10.0,5.0', '8000000002,9000000001,60,60,20.0,5.0', '8000000009,9000000001,35,35,15.0,5.0', '8000000003,9000000002,15,15,8.0,3.0')
            2021 = @('8000000001,9000000001,350,350,10.0,5.0', '8000000009,9000000001,40,40,15.0,5.0', '8000000003,9000000002,11,11,8.0,3.0')
            2022 = @('8000000001,9000000001,400,400,10.0,5.0', '8000000009,9000000001,45,45,15.0,5.0', '8000000003,9000000002,12,12,8.0,3.0')
        }
        foreach ($y in $plan.Keys) {
            Set-Content -Path (Join-Path $sevenDir "hop_teaming_$y.csv") -Encoding ascii -NoNewline -Value (@(
                'from_npi,to_npi,patient_count,transaction_count,average_day_wait,std_day_wait') + $plan[$y] -join "`n")
        }
        $saved = (Get-RmConfig).DataDir
        try {
            Set-RmConfig -DataDir $sevenDir
            $t = Get-RmSourceTrend -Npi 9000000001 -SkipEnrichment
            $rows = @($t.Years)
            $rows.Count | Should -Be 7
            @($rows | ForEach-Object Year) | Should -Be @(2016, 2017, 2018, 2019, 2020, 2021, 2022)
            $rows[0].SharedPatients | Should -Be 140          # 100 + 40
            $rows[6].SharedPatients | Should -Be 445          # 400 + 45
            $t.FirstYear | Should -Be 2016
            $t.LastYear | Should -Be 2022
            $t.VolumeChangePct | Should -Be 217.9             # 140 -> 445
            # six transitions, each with its own retention reading
            @($rows | Where-Object { $_.RetentionPct -ne '' }).Count | Should -Be 6
            @($rows | Where-Object Year -eq 2019)[0].NewSources | Should -Be 1     # 8000000009 joins
            @($rows | Where-Object Year -eq 2021)[0].LostSources | Should -Be 1    # 8000000002 leaves
            @($rows | Where-Object Year -eq 2021)[0].RetentionPct | Should -Be 66.7
            $byNpi = @{}; foreach ($m in @($t.Movers)) { $byNpi[$m.SourceNPI] = $m }
            $byNpi['8000000001'].Change | Should -Be 300      # 100 -> 400 across the span
            $byNpi['8000000002'].Status | Should -Be 'Lost'
            $byNpi['8000000009'].Status | Should -Be 'New'

            # a SMALL practice over the same seven years
            $ts = Get-RmSourceTrend -Npi 9000000002 -SkipEnrichment
            @($ts.Years).Count | Should -Be 7
            @($ts.Years)[0].SharedPatients | Should -Be 12
            @($ts.Years)[6].SharedPatients | Should -Be 12
            $ts.VolumeChangePct | Should -Be 0
            @(@($ts.Years) | Where-Object { $_.RetentionPct -eq 100 }).Count | Should -Be 6

            # and the report renders one column per year in both charts
            $sa = Get-RmSourceAnalysis -Npi 9000000001 -SkipCompetitors -CentroidPath $script:SaCsv
            $sa.Trend = $t
            $out = Join-Path $script:WorkDir 'seven-year-report.html'
            Export-RmSourceReportHtml -Analysis $sa -Path $out | Out-Null
            $html = Get-Content $out -Raw
            foreach ($y in 2016, 2017, 2018, 2019, 2020, 2021, 2022) {
                $html | Should -BeLike "*>$y<*"
            }
            $html | Should -BeLike '*Year-over-year performance &mdash; 2016 to 2022*'
        } finally { Set-RmConfig -DataDir $saved }
    }

    It 'recovers CareSet years from disk when the metadata file is gone' {
        # Meta deleted (or the data folder copied to a new machine): the app
        # must adopt the newest CareSet file on disk instead of claiming the
        # user has no data. Real case — it stranded a store during testing.
        $recDir = Join-Path $script:WorkDir 'meta-recover'
        New-Item -ItemType Directory -Path $recDir -Force | Out-Null
        foreach ($y in 2021, 2022) {
            Set-Content -Path (Join-Path $recDir "hop_teaming_$y.csv") -Encoding ascii -NoNewline `
                -Value ($script:HopRows -join "`n")
        }
        $saved = (Get-RmConfig).DataDir
        try {
            Set-RmConfig -DataDir $recDir                     # note: no dataset-meta.json
            $info = Get-RmDatasetInfo
            $info.Ready | Should -BeTrue
            $info.Source | Should -Be 'hop-teaming'
            $info.Year | Should -Be 2022                      # newest wins
            (Get-RmStatus).DatasetReady | Should -BeTrue
            # and real queries work off the recovered dataset
            $sa = Get-RmSourceAnalysis -Npi 9000000001 -SkipCompetitors -CentroidPath $script:SaCsv
            $sa.TotalPatients | Should -Be 65
        } finally { Set-RmConfig -DataDir $saved }
    }

    It 'refuses a year-over-year run with a single imported year' {
        $emptyDir = Join-Path $script:WorkDir 'st-empty'
        New-Item -ItemType Directory -Path $emptyDir -Force | Out-Null
        $saved = (Get-RmConfig).DataDir
        try {
            Set-RmConfig -DataDir $emptyDir
            { Get-RmSourceTrend -Npi 9000000001 } | Should -Throw '*at least TWO*'
        } finally { Set-RmConfig -DataDir $saved }
    }

    It 'peer chart and table drop zero-volume providers but keep the practice' {
        # In ZIP 99999 only 2 of 4 listed providers have measured volume.
        $sa = Get-RmSourceAnalysis -Npi 9000000001 -CentroidPath $script:CompCsv
        $out = Join-Path $script:WorkDir 'peer-filter-report.html'
        Export-RmSourceReportHtml -Analysis $sa -Path $out | Out-Null
        $html = Get-Content $out -Raw
        $html | Should -Not -BeLike '*NEXTDOOR NEIGHBOR*'     # listed, but no measured volume
        $html | Should -BeLike '*2*have measured referral volume*'
        $html | Should -BeLike '*2*are not shown here*'
        ([regex]::Matches($html, '<tr class="you">')).Count | Should -Be 1
    }
}
