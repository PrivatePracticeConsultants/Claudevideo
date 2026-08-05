# Pester tests for the PracticeGroups module. Runs against the local fake CMS
# server (NPPES double + /foia file serving) — no external traffic.

BeforeAll {
    $script:Root = Split-Path $PSScriptRoot -Parent
    $script:WorkDir = Join-Path ([System.IO.Path]::GetTempPath()) ("pg-tests-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $script:WorkDir -Force | Out-Null
    $script:SiteDir = Join-Path $script:WorkDir 'site'
    New-Item -ItemType Directory -Path $script:SiteDir -Force | Out-Null

    # --- Reassignment fixture CSV (Latin-1; quoted name w/ comma; accented name) ---
    $lines = @(
        '"Group PAC ID","Group Legal Business Name","Group State Code","Individual NPI","Individual First Name","Individual Last Name","Individual Specialty Description"'
        'GA,"Test Rehab, Inc",TX,7011111111,José,Memberone,Physical Therapist In Private Practice'
        'GA,"Test Rehab, Inc",TX,7022222222,Pat,Membertwo,Occupational Therapist In Private Practice'
        'GA,"Test Rehab, Inc",TX,7033333333,Ann,Memberthree,Physical Therapist In Private Practice'
        'GB,,TX,7044444444,Sam,Solo,Physical Therapist In Private Practice'
        'GC,"Heart Group Inc",TX,7055555555,Cardio,Doc,Cardiology'
        'GD,"Single PT LLC",TX,7066666666,One,Only,Physical Therapist In Private Practice'
    ) -join "`n"
    # Write as Latin-1 so the é byte matches what the module decodes.
    [System.IO.File]::WriteAllText((Join-Path $script:SiteDir 'reassign.csv'), $lines,
        [System.Text.Encoding]::GetEncoding('ISO-8859-1'))

    # --- Fake server (NPPES + /foia) ---
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
        try { Invoke-WebRequest -Uri "$base/nppes/?version=2.1&number=7011111111" -UseBasicParsing -TimeoutSec 2 | Out-Null; $ready = $true }
        catch { $ready = $false }
    } until ($ready -or (Get-Date) -gt $deadline)
    if (-not $ready) { throw "Fake CMS server failed to start on port $($script:Port)." }

    $env:PG_CSV_URL = "$base/foia/reassign.csv"
    $env:PG_NPPES_URL = "$base/nppes/"
    $env:PG_DATA_DIR = Join-Path $script:WorkDir 'store'
    Import-Module (Join-Path $script:Root 'PracticeGroups/PracticeGroups.psm1') -Force
}

AfterAll {
    if ($script:Server -and -not $script:Server.HasExited) { $script:Server.Kill() }
    Remove-Item -Recurse -Force $script:WorkDir -ErrorAction SilentlyContinue
    Remove-Item Env:PG_CSV_URL, Env:PG_NPPES_URL, Env:PG_DATA_DIR -ErrorAction SilentlyContinue
}

Describe 'Dataset download' {
    It 'downloads and validates the reassignment CSV' {
        $r = Save-PgDataset
        $r.Downloaded | Should -BeTrue
        Test-Path $r.Path | Should -BeTrue
        (Get-PgStatus).DatasetReady | Should -BeTrue
    }
    It 'is idempotent when already present' {
        (Save-PgDataset).Downloaded | Should -BeFalse
    }
    It 'rejects a non-CMS / non-loopback host' {
        $saved = $env:PG_CSV_URL
        try {
            $env:PG_CSV_URL = 'https://evil.example.com/reassign.csv'
            Import-Module (Join-Path $script:Root 'PracticeGroups/PracticeGroups.psm1') -Force
            { Save-PgDataset -Force } | Should -Throw '*only https CMS hosts*'
        } finally {
            $env:PG_CSV_URL = $saved
            Import-Module (Join-Path $script:Root 'PracticeGroups/PracticeGroups.psm1') -Force
        }
    }
}

Describe 'Dataset load' {
    It 'loads therapy groups only (non-therapy rows filtered out)' {
        $imp = Import-PgDataset
        # GA, GB, GD have >=1 therapy member; GC (Cardiology) is excluded.
        $imp.Groups | Should -Be 3
    }
    It 'decodes Latin-1 accented names correctly' {
        $r = Get-PgGroupsInZip -Zip 77777
        $jose = @($r.Rosters | Where-Object NPI -eq '7011111111')[0]
        $jose.FirstName | Should -Be 'José'
    }
}

Describe 'Practice group query' {
    BeforeAll { $script:R = Get-PgGroupsInZip -Zip 77777 }

    It 'returns only named multi-member groups, ranked by in-ZIP therapists' {
        $g = @($script:R.Groups)
        $g.Count | Should -Be 1
        $g[0].GroupName | Should -Be 'Test Rehab, Inc'
        $g[0].TherapistsInZip | Should -Be 2   # 7011111111 + 7022222222
        $g[0].RosterSize | Should -Be 3         # + 7033333333 (not in ZIP)
    }
    It 'counts solo/unlisted therapists separately' {
        # 7044444444 is in a blank-name group -> solo, not a listed group.
        $script:R.TherapistCount | Should -Be 3
        $script:R.SoloCount | Should -Be 1
    }
    It 'marks which roster members are in the queried ZIP' {
        $roster = @($script:R.Rosters | Where-Object GroupName -eq 'Test Rehab, Inc')
        $roster.Count | Should -Be 3
        (@($roster | Where-Object InThisZip -eq 'Y')).Count | Should -Be 2
        ($roster | Where-Object NPI -eq '7033333333').InThisZip | Should -Be ''
    }
    It 'carries honest methodology notes (current data, not referrals)' {
        ($script:R.Notes -join ' ') | Should -Match 'NOT referral'
        ($script:R.Notes -join ' ') | Should -Match 'who practices where NOW'
    }
    It 'throws a helpful error for a ZIP with no therapists' {
        { Get-PgGroupsInZip -Zip 10101 } | Should -Throw '*no individual PT/OT/SLP*'
    }
    It 'returns group membership for a single NPI (Provider 360 bridge)' {
        $m = @(Get-PgMembershipForNpi -Npi 7011111111)
        $m.Count | Should -Be 1
        $m[0].GroupName | Should -Be 'Test Rehab, Inc'
        $m[0].RosterSize | Should -Be 3
    }
    It 'returns nothing for an NPI not in any group' {
        @(Get-PgMembershipForNpi -Npi 9999999999) | Should -BeNullOrEmpty
    }
}

Describe 'Export' {
    It 'writes CSV + methodology sidecar and neutralizes formula injection' {
        $r = Get-PgGroupsInZip -Zip 77777
        $out = Join-Path $script:WorkDir 'groups.csv'
        # Inject a hostile group name to confirm sanitization.
        $rows = @($r.Groups) + @([pscustomobject]@{ GroupName = '=cmd|calc'; State = 'TX'
            TherapistsInZip = 1; RosterSize = 1; GroupPacId = 'GX' })
        $res = $rows | Export-PgResult -Path $out -Notes $r.Notes -Description 'test'
        $res.Rows | Should -Be 2
        $csv = Import-Csv $out
        ($csv | Where-Object GroupPacId -eq 'GX').GroupName | Should -Be "'=cmd|calc"
        Test-Path $res.Methodology | Should -BeTrue
        (Get-Content $res.Methodology -Raw) | Should -Match 'reassignment'
    }
}

Describe 'Malformed-file refusal' {
    It 'refuses a reassignment file whose body is mostly damaged rows' {
        $bad = Join-Path $script:WorkDir 'damaged-reassignment.csv'
        Set-Content -Path $bad -Encoding ascii -NoNewline -Value (@(
            '"Group PAC ID","Group Legal Business Name","Group State Code","Individual PAC ID","Individual NPI","Individual First Name","Individual Last Name","Individual Specialty Description"'
            '"1111","GOOD THERAPY GROUP","MO","2","1000000001","AMY","SMITH","Physical Therapy"'
            '"2222","TRUNCATED'
        ) -join "`n")
        $g = $null; $n = $null
        # 1 damaged row out of 2 blows the same 1% budget the shared-patient
        # engine applies: refuse loudly, never silently understate rosters.
        { [PgEngine]::Load($bad, @('physical therap'), [ref]$g, [ref]$n) } |
            Should -Throw '*malformed*'
    }

    It 'still tolerates a stray short row within the 1% budget' {
        $ok = Join-Path $script:WorkDir 'stray-row-reassignment.csv'
        $lines = New-Object System.Collections.Generic.List[string]
        $lines.Add('"Group PAC ID","Group Legal Business Name","Group State Code","Individual PAC ID","Individual NPI","Individual First Name","Individual Last Name","Individual Specialty Description"')
        for ($i = 0; $i -lt 200; $i++) {
            $lines.Add(('"1111","GOOD THERAPY GROUP","MO","2","10000000{0:00}","AMY","SMITH","Physical Therapy"' -f ($i % 100)))
        }
        $lines.Add('"2222","TRUNCATED')
        Set-Content -Path $ok -Encoding ascii -NoNewline -Value ($lines -join "`n")
        $g = $null; $n = $null
        [PgEngine]::Load($ok, @('physical therap'), [ref]$g, [ref]$n)
        $g.Count | Should -Be 1
        $g['1111'].Members.Count | Should -Be 200
    }
}

