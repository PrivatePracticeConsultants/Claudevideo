# PracticeGroups.psm1 — maps outpatient-rehab practice GROUPS in a ZIP from the
# CMS "Revalidation Clinic Group Practice Reassignment" public file.
#
# What it answers: for a ZIP code, which multi-provider PT/OT/SLP practice
# groups operate there, each with its therapist roster (names, specialties),
# roster size, and how many members are currently Medicare eligible-to-order/
# refer (cross-referenced against the Order & Referring roster in the app).
#
# Why it exists: the shared-patient referral file barely contains private-
# practice ORGANIZATION NPIs, so private clinics were invisible there. This file
# lists which individual therapists reassign their Medicare benefits to which
# group practice — so a clinic finally appears as the group of its therapists.
#
# Honest scope: this file is CURRENT (updated ~monthly). It shows who practices
# where NOW; it is NOT referral/claims data. Solo practitioners reassign to
# themselves (blank business name) and are reported separately, not as groups.
#
# Source file is ISO-8859-1 (Latin-1) encoded, ~510 MB, RFC-4180 quoted CSV.
# Works on Windows PowerShell 5.1 and PowerShell 7+.

Set-StrictMode -Version Latest

# Ensure TLS 1.2 is available for the CMS/NPPES HTTPS endpoints. Windows
# PowerShell 5.1 on older .NET Framework defaults to TLS 1.0, which the .gov
# endpoints reject ("Could not create SSL/TLS secure channel"); OR-ing in Tls12
# only ADDS a stronger protocol (never removes one). No-op on PS 7 / .NET Core.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

$script:PgConfig = [ordered]@{
    # CMS catalog (data.json) — the reassignment CSV URL is discovered live so
    # it never goes stale. Overridable for tests.
    CatalogUrl  = if ($env:PG_CATALOG_URL) { $env:PG_CATALOG_URL } else { 'https://data.cms.gov/data.json' }
    DatasetTitle = 'Revalidation Clinic Group Practice Reassignment'
    # Direct-URL override for tests (skips catalog discovery).
    CsvUrlOverride = if ($env:PG_CSV_URL) { $env:PG_CSV_URL } else { $null }
    DataDir     = if ($env:PG_DATA_DIR) {
                      $env:PG_DATA_DIR
                  } elseif ($env:LOCALAPPDATA) {
                      Join-Path (Join-Path $env:LOCALAPPDATA 'OrderReferringTracker') 'practice-groups'
                  } else {
                      Join-Path (Join-Path $HOME '.order-referring-tracker') 'practice-groups'
                  }
    NppesUrl    = if ($env:PG_NPPES_URL) { $env:PG_NPPES_URL } else { 'https://npiregistry.cms.hhs.gov/api/' }
}

# NPPES taxonomy_description search terms → filtered to therapy specialties.
$script:PgSearchTerms = @('Physical Therapist', 'Occupational Therapist', 'Speech-Language Pathologist')
# Individual-specialty substrings (lowercased) that count as outpatient therapy
# in the reassignment file's "Individual Specialty Description" column.
$script:PgTherapySpecialties = @('physical therap', 'occupational therap', 'speech', 'physical medicine and rehab')

function Get-PgConfig { [pscustomobject]$script:PgConfig }

function Set-PgConfig {
    <# .SYNOPSIS Overrides practice-group configuration. #>
    [CmdletBinding()]
    param([string]$DataDir, [string]$CatalogUrl, [string]$CsvUrlOverride)
    if ($DataDir)        { $script:PgConfig.DataDir = $DataDir }
    if ($CatalogUrl)     { $script:PgConfig.CatalogUrl = $CatalogUrl }
    if ($CsvUrlOverride) { $script:PgConfig.CsvUrlOverride = $CsvUrlOverride }
}

# ---------------------------------------------------------------------------
# Fast engine (compiled once per process)
# ---------------------------------------------------------------------------

if (-not ('PgEngine' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

public class PgMember
{
    public string NPI;
    public string FirstName;
    public string LastName;
    public string Specialty;
}

public class PgGroup
{
    public string GroupPac;
    public string Name;
    public string State;
    public List<PgMember> Members = new List<PgMember>();
}

public static class PgEngine
{
    // Expected header columns (a subset is used; positions validated by name).
    private static int IdxGroupPac, IdxGroupName, IdxGroupState,
                       IdxNpi, IdxFirst, IdxLast, IdxSpecialty;

    private static List<string> SplitCsvLine(string line)
    {
        List<string> f = new List<string>(16);
        StringBuilder cur = new StringBuilder();
        bool q = false;
        for (int i = 0; i < line.Length; i++)
        {
            char c = line[i];
            if (q)
            {
                if (c == '"')
                {
                    if (i + 1 < line.Length && line[i + 1] == '"') { cur.Append('"'); i++; }
                    else q = false;
                }
                else cur.Append(c);
            }
            else
            {
                if (c == '"') q = true;
                else if (c == ',') { f.Add(cur.ToString()); cur.Length = 0; }
                else cur.Append(c);
            }
        }
        f.Add(cur.ToString());
        return f;
    }

    private static int Find(List<string> header, string name)
    {
        for (int i = 0; i < header.Count; i++)
            if (string.Equals(header[i].Trim(), name, StringComparison.OrdinalIgnoreCase)) return i;
        throw new InvalidDataException("The reassignment file is missing the expected column '" +
            name + "'. CMS may have changed the file layout.");
    }

    // Streams the Latin-1 file, keeping only therapy-specialty rows. Builds the
    // group table and an individual-NPI -> group-PAC-list index. therapyKeys are
    // lowercased specialty substrings.
    public static void Load(string path, string[] therapyKeys,
        out Dictionary<string, PgGroup> groups,
        out Dictionary<string, List<string>> npiToGroups)
    {
        groups = new Dictionary<string, PgGroup>(StringComparer.Ordinal);
        npiToGroups = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        Encoding latin1 = Encoding.GetEncoding("ISO-8859-1");
        using (StreamReader reader = new StreamReader(path, latin1, false, 1 << 20))
        {
            string headerLine = reader.ReadLine();
            if (headerLine == null) throw new InvalidDataException("The reassignment file is empty.");
            List<string> header = SplitCsvLine(headerLine.TrimStart('﻿'));
            IdxGroupPac   = Find(header, "Group PAC ID");
            IdxGroupName  = Find(header, "Group Legal Business Name");
            IdxGroupState = Find(header, "Group State Code");
            IdxNpi        = Find(header, "Individual NPI");
            IdxFirst      = Find(header, "Individual First Name");
            IdxLast       = Find(header, "Individual Last Name");
            IdxSpecialty  = Find(header, "Individual Specialty Description");
            int maxIdx = IdxSpecialty;
            foreach (int i in new int[] { IdxGroupPac, IdxGroupName, IdxGroupState, IdxNpi, IdxFirst, IdxLast })
                if (i > maxIdx) maxIdx = i;

            string line;
            long total = 0, bad = 0;
            while ((line = reader.ReadLine()) != null)
            {
                if (line.Length == 0) continue;
                total++;
                List<string> f = SplitCsvLine(line);
                if (f.Count <= maxIdx) { bad++; continue; }   // tolerate a STRAY short row (budgeted below)
                string spec = f[IdxSpecialty].Trim();
                string specLower = spec.ToLowerInvariant();
                bool therapy = false;
                for (int k = 0; k < therapyKeys.Length; k++)
                    if (specLower.Contains(therapyKeys[k])) { therapy = true; break; }
                if (!therapy) continue;

                string gpac = f[IdxGroupPac].Trim();
                string npi = f[IdxNpi].Trim();
                if (gpac.Length == 0 || npi.Length == 0) continue;

                PgGroup g;
                if (!groups.TryGetValue(gpac, out g))
                {
                    g = new PgGroup();
                    g.GroupPac = gpac;
                    g.Name = f[IdxGroupName].Trim();
                    g.State = f[IdxGroupState].Trim();
                    groups.Add(gpac, g);
                }
                PgMember m = new PgMember();
                m.NPI = npi; m.FirstName = f[IdxFirst].Trim();
                m.LastName = f[IdxLast].Trim(); m.Specialty = spec;
                g.Members.Add(m);

                List<string> gl;
                if (!npiToGroups.TryGetValue(npi, out gl)) { gl = new List<string>(1); npiToGroups.Add(npi, gl); }
                if (!gl.Contains(gpac)) gl.Add(gpac);
            }
            // Same malformed-line budget as the shared-patient engine: a
            // truncated or corrupted body must REFUSE to load, not silently
            // understate every roster it was asked about.
            if (total == 0) throw new InvalidDataException(
                "The reassignment file '" + path + "' has a header but no data rows.");
            if (bad > total / 100) throw new InvalidDataException(
                "The reassignment file '" + path + "' had " + bad + " malformed rows out of " + total +
                "; refusing to report rosters from a file that damaged. Re-download the dataset.");
        }
    }

    public static long CountLines(string path)
    {
        long n = 0;
        Encoding latin1 = Encoding.GetEncoding("ISO-8859-1");
        using (StreamReader reader = new StreamReader(path, latin1, false, 1 << 20))
            while (reader.ReadLine() != null) n++;
        return n;
    }
}
'@
}

# ---------------------------------------------------------------------------
# Internals (mirror the other modules' helpers)
# ---------------------------------------------------------------------------

function Get-PgProp([object]$Object, [string]$Name) {
    if ($null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]) {
        $Object.PSObject.Properties[$Name].Value
    } else { $null }
}

function ConvertTo-PgSafeCsvRecord {
    param([Parameter(ValueFromPipeline)]$Row)
    process {
        $dirty = $false
        $out = [ordered]@{}
        foreach ($p in $Row.PSObject.Properties) {
            $v = $p.Value
            if ($v -is [string] -and $v -match '^[=+\-@\t\r]') { $out[$p.Name] = "'" + $v; $dirty = $true }
            else { $out[$p.Name] = $v }
        }
        if ($dirty) { [pscustomobject]$out } else { $Row }
    }
}

function Write-PgCsvFile {
    param([object[]]$Rows, [string]$Path)
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $enc = New-Object System.Text.UTF8Encoding($true)
    $sw = New-Object System.IO.StreamWriter($full, $false, $enc)
    try {
        $Rows | ConvertTo-PgSafeCsvRecord | ConvertTo-Csv -NoTypeInformation |
            ForEach-Object { $sw.WriteLine($_) }
    } finally { $sw.Dispose() }
}

function Initialize-PgDataDir {
    $d = $script:PgConfig.DataDir
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    $d
}

function Clear-PgStaleTemp {
    [CmdletBinding()]
    param([int]$OlderThanMinutes = 360)
    $d = $script:PgConfig.DataDir
    if (-not (Test-Path -LiteralPath $d)) { return }
    $cutoff = (Get-Date).AddMinutes(-$OlderThanMinutes)
    Get-ChildItem -LiteralPath $d -Filter '*.tmp' -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

function Get-PgDatasetPath { Join-Path $script:PgConfig.DataDir 'clinic_group_reassignment.csv' }

function Assert-PgSafeUrl([string]$Url) {
    $u = $null
    if (-not [uri]::TryCreate($Url, [System.UriKind]::Absolute, [ref]$u)) {
        throw "Unusable dataset download URL: '$Url'."
    }
    $okHost = $u.Host -eq 'data.cms.gov' -or $u.Host -eq 'downloads.cms.gov' -or $u.IsLoopback
    if (($u.Scheme -ne 'https' -and -not ($u.Scheme -eq 'http' -and $u.IsLoopback)) -or -not $okHost) {
        throw "Refusing to download from '$Url': only https CMS hosts (or loopback for testing) are allowed."
    }
}

function Invoke-PgNppes([string]$Query) {
    $url = '{0}?version=2.1&{1}' -f $script:PgConfig.NppesUrl, $Query
    try { Invoke-RestMethod -Uri $url -TimeoutSec 60 -ErrorAction Stop }
    catch { throw ("The NPPES registry could not be reached. Details: $($_.Exception.Message)") }
}

# ---------------------------------------------------------------------------
# Catalog / download
# ---------------------------------------------------------------------------

function Get-PgCatalogCsvUrl {
    if ($script:PgConfig.CsvUrlOverride) { return $script:PgConfig.CsvUrlOverride }
    try {
        $resp = Invoke-WebRequest -Uri $script:PgConfig.CatalogUrl -UseBasicParsing -TimeoutSec 120 -ErrorAction Stop
        $catalog = $resp.Content | ConvertFrom-Json
    } catch {
        throw "Could not reach the CMS data catalog. Details: $($_.Exception.Message)"
    }
    $ds = @((Get-PgProp $catalog 'dataset') | Where-Object { (Get-PgProp $_ 'title') -eq $script:PgConfig.DatasetTitle })
    if ($ds.Count -eq 0) { throw "The CMS catalog no longer lists '$($script:PgConfig.DatasetTitle)'." }
    $csv = @((Get-PgProp $ds[0] 'distribution') |
        Where-Object { (Get-PgProp $_ 'format') -eq 'CSV' -and (Get-PgProp $_ 'downloadURL') } |
        Sort-Object { [string](Get-PgProp $_ 'modified') } -Descending)
    if ($csv.Count -eq 0) { throw "No CSV download listed for '$($script:PgConfig.DatasetTitle)'." }
    [string](Get-PgProp $csv[0] 'downloadURL')
}

function Save-PgDataset {
    <#
    .SYNOPSIS
      Downloads the CMS clinic-group reassignment CSV (~510 MB; one-time). Skips
      the download if already present. Atomic: a failed download never disturbs
      an existing copy.
    #>
    [CmdletBinding()]
    param([switch]$Force)

    Initialize-PgDataDir | Out-Null
    $target = Get-PgDatasetPath
    if ((Test-Path -LiteralPath $target) -and -not $Force) {
        return [pscustomobject]@{ Downloaded = $false; Path = $target; Message = 'Practice-group dataset already present.' }
    }
    $ProgressPreference = 'SilentlyContinue'
    Clear-PgStaleTemp
    $url = Get-PgCatalogCsvUrl
    Assert-PgSafeUrl $url
    # Fail a guaranteed-to-fail download NOW, not 510 MB in: advisory free-
    # space probe (silently skipped on exotic paths, never blocks real work).
    $free = $null; $driveRoot = ''
    try {
        $driveRoot = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($script:PgConfig.DataDir))
        if ($driveRoot) { $free = (New-Object System.IO.DriveInfo($driveRoot)).AvailableFreeSpace }
    } catch { }
    if ($null -ne $free -and $free -lt 1GB) {
        throw ("Not enough disk space to download the ~510 MB practice-group dataset: only " +
               "{0:N1} GB free on {1}. Free up space and try again." -f ($free / 1GB), $driveRoot)
    }
    $tmp = '{0}.{1}.tmp' -f $target, [guid]::NewGuid().ToString('N')
    try {
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -TimeoutSec 7200 -ErrorAction Stop
        $bytes = (Get-Item -LiteralPath $tmp).Length
        if ($bytes -gt 4GB) { throw "Downloaded file is implausibly large ($([int]($bytes/1MB)) MB); refusing it." }
        # Validate: header must contain the key columns.
        $enc = [System.Text.Encoding]::GetEncoding('ISO-8859-1')
        $sr = New-Object System.IO.StreamReader($tmp, $enc)
        try { $head = $sr.ReadLine() } finally { $sr.Close() }
        if (-not $head -or $head -notmatch 'Group PAC ID' -or $head -notmatch 'Individual NPI') {
            throw "The downloaded file does not look like the CMS reassignment file."
        }
        Move-Item -LiteralPath $tmp -Destination $target -Force
    } catch {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        throw "Downloading the practice-group dataset failed; nothing was changed. Details: $($_.Exception.Message)"
    }
    [pscustomobject]@{ Downloaded = $true; Path = $target
        Message = "Downloaded the CMS clinic-group reassignment dataset ($([int]((Get-Item $target).Length/1MB)) MB)." }
}

function Get-PgStatus {
    <# .SYNOPSIS Shows whether the practice-group dataset is on disk. #>
    $t = Get-PgDatasetPath
    [pscustomobject]@{
        DataDir      = $script:PgConfig.DataDir
        DatasetPath  = $t
        DatasetReady = (Test-Path -LiteralPath $t)
        DatasetBytes = if (Test-Path -LiteralPath $t) { (Get-Item -LiteralPath $t).Length } else { 0 }
    }
}

# ---------------------------------------------------------------------------
# In-memory index (loaded once, cached per process)
# ---------------------------------------------------------------------------

$script:PgGroups = $null       # Dictionary[string,PgGroup]
$script:PgNpiIndex = $null     # Dictionary[string,List[string]]
$script:PgLoadedFrom = $null

function Import-PgDataset {
    <#
    .SYNOPSIS
      Loads the reassignment file into memory (therapy rows only) and caches it.
      Returns a summary. Safe to call repeatedly — reloads only if the file changed.
    #>
    [CmdletBinding()]
    param([string]$Path, [switch]$Force)
    if (-not $Path) {
        $Path = Get-PgDatasetPath
        if (-not (Test-Path -LiteralPath $Path)) {
            throw "The practice-group dataset is not downloaded yet. Run Save-PgDataset first (one-time, ~510 MB)."
        }
    }
    $stamp = (Get-Item -LiteralPath $Path).LastWriteTimeUtc.ToString('o') + '|' + $Path
    if (-not $Force -and $script:PgLoadedFrom -eq $stamp -and $null -ne $script:PgGroups) {
        return [pscustomobject]@{ Groups = $script:PgGroups.Count; Reloaded = $false }
    }
    $groups = $null; $npiIdx = $null
    [PgEngine]::Load($Path, [string[]]$script:PgTherapySpecialties, [ref]$groups, [ref]$npiIdx)
    $script:PgGroups = $groups
    $script:PgNpiIndex = $npiIdx
    $script:PgLoadedFrom = $stamp
    [pscustomobject]@{ Groups = $groups.Count; Reloaded = $true }
}

# ---------------------------------------------------------------------------
# NPPES: therapists in a ZIP
# ---------------------------------------------------------------------------

function Get-PgTherapistNpiInZip {
    <# .SYNOPSIS Returns individual PT/OT/SLP NPIs with a practice location in the ZIP (or prefix). #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidatePattern('^\d{3,5}\*?$')][string]$Zip)
    $prefix = $Zip.TrimEnd('*')
    $found = @{}
    foreach ($term in $script:PgSearchTerms) {
        $skip = 0
        while ($true) {
            $q = 'postal_code={0}&taxonomy_description={1}&enumeration_type=NPI-1&limit=200&skip={2}' -f
                [uri]::EscapeDataString($Zip), [uri]::EscapeDataString($term), $skip
            $resp = Invoke-PgNppes $q
            $results = @(Get-PgProp $resp 'results')
            foreach ($r in $results) {
                $npi = [string](Get-PgProp $r 'number')
                if ($found.ContainsKey($npi)) { continue }
                $loc = @(@(Get-PgProp $r 'addresses') | Where-Object {
                    (Get-PgProp $_ 'address_purpose') -eq 'LOCATION' -and
                    ([string](Get-PgProp $_ 'postal_code')).StartsWith($prefix) })
                if ($loc.Count -eq 0) { continue }
                $basic = Get-PgProp $r 'basic'
                $found[$npi] = ('{0} {1}' -f (Get-PgProp $basic 'first_name'), (Get-PgProp $basic 'last_name')).Trim()
            }
            if ($results.Count -lt 200) { break }
            if ($skip -ge 1000) {
                Write-Warning "NPPES returned its maximum page depth for '$term' in '$Zip'; the list may be incomplete — use a full 5-digit ZIP."
                break
            }
            $skip += 200
        }
    }
    $found
}

function Get-PgTherapistNpiInZipList {
    <# .SYNOPSIS Individual PT/OT/SLP NPIs across MANY ZIPs (radius sweeps).
       One streaming pass over the local NPPES index when available (built on
       the Multi-site chains tab); falls back to the live registry per ZIP,
       which is only practical for small sweeps. #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateCount(1, 2000)][string[]]$ZipList,
        [string]$NppesIndexPath
    )
    $found = @{}
    if ($NppesIndexPath -and (Test-Path -LiteralPath $NppesIndexPath) -and ('RmEngine' -as [type])) {
        $want = New-Object 'System.Collections.Generic.HashSet[string]'
        foreach ($z in $ZipList) { if ($z -match '^\d{5}$') { [void]$want.Add($z) } }
        $pfx = New-Object 'System.Collections.Generic.List[string]'
        foreach ($line in @([RmEngine]::ScanIndexByZip($NppesIndexPath, 7, $want, $pfx))) {
            $f = $line.Split('|')
            if ($f.Count -lt 10 -or $f[1] -ne '1') { continue }
            $isTher = $false
            $taxEnd = [math]::Min($f.Count - 1, 23)
            foreach ($ti in @(8) + @(10..$taxEnd)) {
                if ($ti -ge $f.Count) { break }
                $c4 = if ($f[$ti].Length -ge 4) { $f[$ti].Substring(0, 4) } else { '' }
                if ($c4 -in '2251', '225X', '235Z') { $isTher = $true; break }
            }
            if ($isTher) { $found[$f[0]] = ("$($f[4]) $($f[3])").Trim() }
        }
        return $found
    }
    if ($ZipList.Count -gt 30) {
        throw ("A radius this wide covers $($ZipList.Count) ZIP codes - too many to sweep through the live registry. " +
               "Import the NPPES bulk zip on the Multi-site chains tab first (one time); radius searches then run locally in seconds.")
    }
    foreach ($z in $ZipList) {
        foreach ($kv in (Get-PgTherapistNpiInZip -Zip $z).GetEnumerator()) { $found[$kv.Key] = $kv.Value }
    }
    $found
}

# ---------------------------------------------------------------------------
# The main query
# ---------------------------------------------------------------------------

function Get-PgGroupsInZip {
    <#
    .SYNOPSIS
      For a ZIP (or prefix like 630*): finds the outpatient-rehab practice GROUPS
      operating there and returns them ranked by how many of their therapists
      practice in that ZIP, with full therapist rosters.
    .OUTPUTS
      An object with .Groups (ranked practice groups), .Rosters (per-member
      drill-down), .SoloCount (local therapists NOT in any named group), and
      .Notes (methodology).
    #>
    [CmdletBinding()]
    param(
        [ValidatePattern('^\d{3,5}\*?$')][string]$Zip,
        # Radius searches: the exact 5-digit ZIPs to sweep (the GUI computes
        # them from the Census centroid table) + the local NPPES index that
        # makes a wide sweep affordable.
        [ValidateCount(1, 2000)][string[]]$ZipList,
        [string]$NppesIndexPath,
        [string]$AreaLabel
    )
    if (-not $Zip -and -not $ZipList) { throw 'Provide -Zip or -ZipList.' }
    Import-PgDataset | Out-Null

    $areaText = if ($AreaLabel) { $AreaLabel } elseif ($ZipList) { "$(@($ZipList).Count) ZIP codes" } else { "ZIP $Zip" }
    Write-Verbose "Finding therapists in $areaText via NPPES..."
    $inZip = if ($ZipList) {
        Get-PgTherapistNpiInZipList -ZipList $ZipList -NppesIndexPath:$NppesIndexPath
    } else {
        Get-PgTherapistNpiInZip -Zip $Zip
    }
    if ($inZip.Count -eq 0) {
        throw $(if ($ZipList) { "NPPES lists no individual PT/OT/SLP providers with a practice location in $areaText. Try a larger radius." }
                else { "NPPES lists no individual PT/OT/SLP providers with a practice location in ZIP '$Zip'. Try a broader prefix like '$($Zip.Substring(0,3))*'." })
    }

    # Which groups do the in-ZIP therapists belong to?
    $groupHits = @{}   # groupPac -> count of in-zip therapists
    $groupNpis = @{}   # groupPac -> the in-area members' NPIs (for exports / combined analyses)
    $soloOrUnlisted = 0
    foreach ($npi in $inZip.Keys) {
        if (-not $script:PgNpiIndex.ContainsKey($npi)) { $soloOrUnlisted++; continue }
        # A "group" here means a multi-member practice with a business name.
        $named = New-Object System.Collections.Generic.List[string]
        foreach ($gpac in $script:PgNpiIndex[$npi]) {
            $g = $script:PgGroups[$gpac]
            if ($g.Name -and $g.Members.Count -ge 2) { $named.Add($gpac) }
        }
        if ($named.Count -eq 0) { $soloOrUnlisted++; continue }
        foreach ($g in $named) {
            if (-not $groupHits.ContainsKey($g)) {
                $groupHits[$g] = 0
                $groupNpis[$g] = New-Object System.Collections.Generic.List[string]
            }
            $groupHits[$g]++
            $groupNpis[$g].Add($npi)
        }
    }

    $rows = foreach ($gpac in $groupHits.Keys) {
        $g = $script:PgGroups[$gpac]
        [pscustomobject]@{
            GroupName        = $g.Name
            State            = $g.State
            TherapistsInZip  = $groupHits[$gpac]
            RosterSize       = $g.Members.Count
            GroupPacId       = $gpac
            # Space-separated so the whole cell pastes straight into the
            # Provider lookup for a combined group analysis.
            TherapistNpisInZip = (@($groupNpis[$gpac] | Sort-Object) -join ' ')
        }
    }
    $rows = @($rows | Sort-Object -Property @{Expression = 'TherapistsInZip'; Descending = $true},
                                            @{Expression = 'RosterSize'; Descending = $true})

    # Full rosters (one row per member) for drill-down / export.
    $rosters = foreach ($gpac in $groupHits.Keys) {
        $g = $script:PgGroups[$gpac]
        foreach ($m in $g.Members) {
            [pscustomobject]@{
                GroupName  = $g.Name
                GroupPacId = $gpac
                NPI        = $m.NPI
                FirstName  = $m.FirstName
                LastName   = $m.LastName
                Specialty  = $m.Specialty
                InThisZip  = if ($inZip.ContainsKey($m.NPI)) { 'Y' } else { '' }
            }
        }
    }
    $rosters = @($rosters | Sort-Object GroupName, LastName, FirstName)

    $notes = @(
        "Source: CMS 'Revalidation Clinic Group Practice Reassignment' public file (current; updated ~monthly)."
        "This shows which individual therapists reassign Medicare benefits to which group practice — i.e. who practices where NOW. It is NOT referral or claims data."
        "A 'group' here is a practice with a legal business name and 2+ therapist members. Solo/private-practice therapists (who reassign to themselves) are counted separately, not shown as groups."
        "TherapistsInZip = roster members with an NPPES practice location in the requested ZIP. RosterSize = the group's total PT/OT/SLP members nationwide (a group may span many locations, so a large roster with few in-ZIP members is a multi-site organization)."
        "Every listed therapist is Medicare-enrolled and reassigning benefits to the group (that is what this file records), so no separate eligibility check is needed. The Order & Referring roster is not used here — it covers ordering/referring, which therapists generally do not do."
        "$($inZip.Count) individual therapists found in $areaText; $soloOrUnlisted of them are solo or not in a named multi-member group."
        $(if ($ZipList) { "RADIUS SEARCH: therapists were swept from $(@($ZipList).Count) ZIP code(s)$(if ($NppesIndexPath -and (Test-Path -LiteralPath $NppesIndexPath)) { ' using the local NPPES index' } else { ' via the live NPPES registry' }). TherapistsInZip counts members anywhere in that area." })
        'TherapistNpisInZip lists the in-area members'' NPIs - paste a group''s cell into the Provider lookup tab for a combined referral analysis of that group.' 
    ) | Where-Object { $_ }

    [pscustomobject]@{
        Zip            = $(if ($AreaLabel) { $AreaLabel } elseif ($Zip) { $Zip } else { $areaText })
        TherapistCount = $inZip.Count
        SoloCount      = $soloOrUnlisted
        Groups         = $rows
        Rosters        = $rosters
        Notes          = @($notes)
    }
}

function Get-PgMembershipForNpi {
    <#
    .SYNOPSIS
      Returns the practice group(s) an individual NPI belongs to (name, state,
      roster size). Used by the Provider 360 lookup. Loads the dataset if needed.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Npi)
    Import-PgDataset | Out-Null
    if (-not $script:PgNpiIndex.ContainsKey($Npi)) { return @() }
    foreach ($gpac in $script:PgNpiIndex[$Npi]) {
        $g = $script:PgGroups[$gpac]
        [pscustomobject]@{
            GroupName  = if ($g.Name) { $g.Name } else { '(solo / no business name)' }
            State      = $g.State
            RosterSize = $g.Members.Count
            GroupPacId = $gpac
        }
    }
}

function Export-PgResult {
    <# .SYNOPSIS Exports practice-group rows to CSV with a methodology sidecar. #>
    [CmdletBinding()]
    param(
        [Parameter(ValueFromPipeline)][object[]]$InputObject,
        [Parameter(Mandatory)][string]$Path,
        [string[]]$Notes = @(),
        [string]$Description = ''
    )
    begin { $rows = New-Object System.Collections.Generic.List[object] }
    process { foreach ($o in $InputObject) { if ($null -ne $o) { $rows.Add($o) } } }
    end {
        $dir = Split-Path -Parent $Path
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        if ($rows.Count -eq 0) { Set-Content -LiteralPath $Path -Value '' -Encoding UTF8 }
        else { Write-PgCsvFile -Rows $rows -Path $Path }
        $sidecar = ($Path -replace '\.[Cc][Ss][Vv]$', '') + '.methodology.txt'
        @(
            'Export methodology'
            '=================='
            "Generated:      $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))"
            "Rows exported:  $($rows.Count)"
            $(if ($Description) { "Description:    $Description" })
            ''
        ) + @($Notes) | Where-Object { $null -ne $_ } | Set-Content -LiteralPath $sidecar -Encoding UTF8
        [pscustomobject]@{ Path = $Path; Rows = $rows.Count; Methodology = $sidecar }
    }
}

Export-ModuleMember -Function @(
    'Get-PgConfig', 'Set-PgConfig', 'Get-PgStatus', 'Get-PgDatasetPath',
    'Save-PgDataset', 'Import-PgDataset', 'Get-PgTherapistNpiInZip',
    'Get-PgGroupsInZip', 'Get-PgMembershipForNpi', 'Export-PgResult', 'Clear-PgStaleTemp'
)
