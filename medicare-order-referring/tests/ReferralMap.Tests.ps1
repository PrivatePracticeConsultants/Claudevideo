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
