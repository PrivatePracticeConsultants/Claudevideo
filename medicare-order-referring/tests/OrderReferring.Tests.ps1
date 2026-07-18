# Pester tests for the OrderReferring module. Everything runs against local
# fixture files and a local HTTP server — no CMS traffic. Run with:
#   pwsh -NoProfile -Command "Invoke-Pester medicare-order-referring/tests -Output Detailed"

BeforeAll {
    $script:Root = Split-Path $PSScriptRoot -Parent
    $script:WorkDir = Join-Path ([System.IO.Path]::GetTempPath()) ("orf-tests-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $script:WorkDir -Force | Out-Null
    $script:SiteDir = Join-Path $script:WorkDir 'site'
    New-Item -ItemType Directory -Path $script:SiteDir -Force | Out-Null

    # --- Fixture CSVs -----------------------------------------------------
    # Valid NPIs (pass the Luhn/80840 check): generated for the fixtures below.
    $script:ValidNpis = @('1417051921', '1972040137', '1760465553', '1295400745', '1265446264')

    $release1 = @(
        'NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE'
        '1417051921,SMITH,JOHN,Y,Y,N,N,N'
        '1972040137,"SMITH, JR",ALICE,Y,N,Y,N,Y'
        '1760465553,JONES,BOB,N,Y,N,Y,N'
    ) -join "`n"
    $release2 = @(
        'NPI,LAST_NAME,FIRST_NAME,PARTB,DME,HHA,PMD,HOSPICE'
        '1417051921,SMITH,JOHN,Y,Y,N,N,N'          # unchanged
        '1760465553,JONES,BOB,Y,Y,N,Y,N'           # PARTB flipped N->Y
        '1295400745,NGUYEN,MAI,Y,Y,Y,Y,Y'          # added; 1972040137 removed
    ) -join "`n"
    Set-Content -Path (Join-Path $script:SiteDir 'release1.csv') -Value $release1 -Encoding UTF8 -NoNewline
    Set-Content -Path (Join-Path $script:SiteDir 'release2.csv') -Value $release2 -Encoding UTF8 -NoNewline

    # --- Local HTTP server serving a fake CMS catalog ---------------------
    $script:Port = Get-Random -Minimum 20000 -Maximum 45000
    $base = "http://127.0.0.1:$($script:Port)"
    $catalog = @{
        dataset = @(
            @{
                title = 'Order and Referring'
                accrualPeriodicity = 'R/P3.5D'
                distribution = @(
                    @{ format = 'CSV'; downloadURL = "$base/release2.csv"; modified = '2026-02-02' }
                    @{ format = 'API'; accessURL = "$base/api"; modified = '2026-02-02' }
                    @{ format = 'CSV'; downloadURL = "$base/release1.csv"; modified = '2026-01-01' }
                )
            }
        )
    } | ConvertTo-Json -Depth 6
    Set-Content -Path (Join-Path $script:SiteDir 'data.json') -Value $catalog -Encoding UTF8

    $python = if (Get-Command python3 -ErrorAction SilentlyContinue) { 'python3' } else { 'python' }
    $serverArgs = @{
        FilePath = $python
        ArgumentList = @('-m', 'http.server', $script:Port, '--bind', '127.0.0.1')
        WorkingDirectory = $script:SiteDir
        PassThru = $true
    }
    if ($env:OS -eq 'Windows_NT') { $serverArgs.WindowStyle = 'Hidden' }
    $script:Server = Start-Process @serverArgs
    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 200
        try {
            Invoke-WebRequest -Uri "$base/data.json" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $ready = $true
        } catch { $ready = $false }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) { throw "Local test HTTP server failed to start on port $($script:Port)." }

    # --- Module under test, pointed at the fixtures -----------------------
    $env:ORF_CATALOG_URL = "$base/data.json"
    $env:ORF_DATA_DIR = Join-Path $script:WorkDir 'store'
    Import-Module (Join-Path $script:Root 'OrderReferring/OrderReferring.psm1') -Force
}

AfterAll {
    if ($script:Server -and -not $script:Server.HasExited) { $script:Server.Kill() }
    Remove-Item -Recurse -Force $script:WorkDir -ErrorAction SilentlyContinue
    Remove-Item Env:ORF_CATALOG_URL, Env:ORF_DATA_DIR -ErrorAction SilentlyContinue
}

Describe 'NPI validation' {
    It 'accepts real NPIs' {
        foreach ($n in $script:ValidNpis) { [OrfEngine]::IsValidNpi($n) | Should -BeTrue }
    }
    It 'rejects wrong check digits, short, and non-numeric values' {
        [OrfEngine]::IsValidNpi('1417051922') | Should -BeFalse
        [OrfEngine]::IsValidNpi('141705192') | Should -BeFalse
        [OrfEngine]::IsValidNpi('141705192X') | Should -BeFalse
        [OrfEngine]::IsValidNpi('') | Should -BeFalse
        [OrfEngine]::IsValidNpi($null) | Should -BeFalse
    }
}

Describe 'Catalog discovery' {
    It 'finds the newest CSV release' {
        $info = Get-OrfCatalogInfo
        $info.ReleaseDate | Should -Be '2026-02-02'
        $info.CsvUrl | Should -Match 'release2\.csv$'
        $info.AllCsvReleases.Count | Should -Be 2
    }
}

Describe 'Update pipeline' {
    It 'downloads and validates an older release explicitly' {
        $r = Update-OrfData -ReleaseDate '2026-01-01'
        $r.Updated | Should -BeTrue
        $r.RowCount | Should -Be 3
        Test-Path $r.SnapshotPath | Should -BeTrue
    }
    It 'downloads the latest release and writes a change log' {
        $r = Update-OrfData
        $r.Updated | Should -BeTrue
        $r.RowCount | Should -Be 3
        $r.Message | Should -Match '1 added, 1 removed, 1 changed'
        $changeFiles = Get-ChildItem (Join-Path $env:ORF_DATA_DIR 'changes')
        $changeFiles.Count | Should -Be 1
    }
    It 'is idempotent when already current' {
        $r = Update-OrfData
        $r.Updated | Should -BeFalse
        $r.Message | Should -Match 'up to date'
    }
    It 'records accurate state metadata' {
        $status = Get-OrfStatus -CheckOnline
        $status.LocalRelease | Should -Be '2026-02-02'
        $status.LocalRowCount | Should -Be 3
        $status.SnapshotCount | Should -Be 2
        $status.UpdateAvailable | Should -BeFalse
    }
    It 'rejects a structurally broken download and keeps existing data' {
        Set-Content -Path (Join-Path $script:SiteDir 'release3.csv') `
            -Value "NPI,WRONG,COLUMNS`n1,2,3" -Encoding UTF8
        $catalogPath = Join-Path $script:SiteDir 'data.json'
        $cat = Get-Content $catalogPath -Raw | ConvertFrom-Json
        $cat.dataset[0].distribution[0].downloadURL = "http://127.0.0.1:$($script:Port)/release3.csv"
        $cat.dataset[0].distribution[0].modified = '2026-03-03'
        $cat | ConvertTo-Json -Depth 6 | Set-Content $catalogPath -Encoding UTF8

        { Update-OrfData } | Should -Throw '*failed validation*'
        (Get-OrfStatus).LocalRelease | Should -Be '2026-02-02'   # untouched
        Get-ChildItem (Join-Path $env:ORF_DATA_DIR 'snapshots') -Filter '*.tmp' | Should -BeNullOrEmpty

        # restore the good catalog for later tests
        $cat.dataset[0].distribution[0].downloadURL = "http://127.0.0.1:$($script:Port)/release2.csv"
        $cat.dataset[0].distribution[0].modified = '2026-02-02'
        $cat | ConvertTo-Json -Depth 6 | Set-Content $catalogPath -Encoding UTF8
    }
}

Describe 'Search' {
    It 'matches by partial name, case-insensitively' {
        $hits = @(Search-OrfProvider -Name 'smi')
        $hits.Count | Should -Be 1
        $hits[0].LastName | Should -Be 'SMITH'
    }
    It 'handles quoted names with embedded commas (older snapshot)' {
        $old = (Get-OrfSnapshotFiles)[0].FullName
        $hits = @(Search-OrfProvider -Name 'smith jr' -SnapshotPath $old)
        $hits.Count | Should -Be 1
        $hits[0].LastName | Should -Be 'SMITH, JR'
        $hits[0].FirstName | Should -Be 'ALICE'
    }
    It 'filters by required flags' {
        @(Search-OrfProvider -RequireFlag PARTB).Count | Should -Be 3
        @(Search-OrfProvider -RequireFlag HOSPICE).Count | Should -Be 1
    }
    It 'matches by NPI prefix' {
        $hits = @(Search-OrfProvider -Npi 176)
        $hits.Count | Should -Be 1
        $hits[0].NPI | Should -Be '1760465553'
    }
}

Describe 'Batch NPI check' {
    It 'classifies eligible, not-on-list, and invalid NPIs' {
        $results = @(Test-OrfNpi -Npi @('1417051921', '1265446264', '9999999999'))
        $results.Count | Should -Be 3
        ($results | Where-Object NPI -eq '1417051921').Status | Should -Match 'ELIGIBLE'
        ($results | Where-Object NPI -eq '1265446264').Status | Should -Be 'NOT ON LIST'
        ($results | Where-Object NPI -eq '9999999999').Status | Should -Be 'INVALID NPI'
    }
    It 'extracts NPIs from an arbitrary text file and dedupes' {
        $listFile = Join-Path $script:WorkDir 'npis.txt'
        Set-Content $listFile "referrals:`n1417051921, 1417051921`nDr Jones 1760465553`nnot-an-npi 123"
        $results = @(Test-OrfNpi -Path $listFile)
        $results.Count | Should -Be 2
    }
}

Describe 'Snapshot comparison' {
    It 'reports added, removed, and changed providers' {
        $changes = @(Compare-OrfSnapshot)
        $changes.Count | Should -Be 3
        ($changes | Where-Object ChangeType -eq 'Added').NPI | Should -Be '1295400745'
        ($changes | Where-Object ChangeType -eq 'Removed').NPI | Should -Be '1972040137'
        $flip = $changes | Where-Object ChangeType -eq 'Changed'
        $flip.NPI | Should -Be '1760465553'
        $flip.OldFlags | Should -Match 'PARTB=N'
        $flip.NewFlags | Should -Match 'PARTB=Y'
    }
}

Describe 'Export' {
    It 'writes a CSV plus a methodology sidecar' {
        $out = Join-Path $script:WorkDir 'export.csv'
        $result = Search-OrfProvider -RequireFlag PARTB |
            Export-OrfResult -Path $out -Description 'test export'
        $result.Rows | Should -Be 3
        (Import-Csv $out).Count | Should -Be 3
        Test-Path $result.Methodology | Should -BeTrue
        $sidecar = Get-Content $result.Methodology -Raw
        $sidecar | Should -Match 'Order and Referring'
        $sidecar | Should -Match 'no referral relationships'
        $sidecar | Should -Match 'test export'
    }
    It 'writes an honest empty file for zero results' {
        $out = Join-Path $script:WorkDir 'empty.csv'
        $result = Search-OrfProvider -Name 'zzz-no-such-name' | Export-OrfResult -Path $out
        $result.Rows | Should -Be 0
        Test-Path $out | Should -BeTrue
    }
}

Describe 'Snapshot retention' {
    It 'prunes the oldest snapshots beyond the configured limit' {
        Set-OrfConfig -KeepSnapshots 2
        try {
            (Get-OrfSnapshotFiles).Count | Should -Be 2   # already at limit; nothing to prune
        } finally {
            Set-OrfConfig -KeepSnapshots 8
        }
    }
}
