# ReferralMap.psm1 — regional referral-source mapping for outpatient rehab
# clinics, built on the CMS Physician Shared Patient Patterns FOIA data
# (2009–2015) joined with the live NPPES registry.
#
# What it answers: for a ZIP code, which providers were the top-volume FEEDERS
# of Medicare patients into each outpatient rehab clinic in that area — i.e.
# rows of the CMS file where the clinic is NPI-2 (saw the patient AFTER the
# source provider, within the interval window).
#
# Honesty notes baked into every output:
#  - The newest public release of this data covers Jan–Sep 2015. It maps the
#    STRUCTURE of a referral market, not current volumes.
#  - "Shared patients" is CMS's privacy-preserving referral proxy (same patient
#    seen by both providers within N days); pairs under 11 patients/year are
#    excluded by CMS, so small referrers are invisible.
#  - Same-day pairs are assigned by CMS to the lower NPI, so direction for
#    same-day activity is ambiguous; same-day counts are shown separately.
#
# Works on Windows PowerShell 5.1 and PowerShell 7+.

Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

$script:RmConfig = [ordered]@{
    # CMS FOIA download pattern; overridable for tests via RM_FOIA_URL_TEMPLATE
    # ({0}=year, {1}=interval days).
    FoiaUrlTemplate = if ($env:RM_FOIA_URL_TEMPLATE) { $env:RM_FOIA_URL_TEMPLATE }
                      else { 'https://downloads.cms.gov/foia/physician-shared-patient-patterns-{0}-days{1}.zip' }
    # NPPES registry API base; overridable for tests via RM_NPPES_URL.
    NppesUrl = if ($env:RM_NPPES_URL) { $env:RM_NPPES_URL }
               else { 'https://npiregistry.cms.hhs.gov/api/' }
    DataDir  = if ($env:RM_DATA_DIR) {
                   $env:RM_DATA_DIR
               } elseif ($env:LOCALAPPDATA) {
                   Join-Path (Join-Path $env:LOCALAPPDATA 'OrderReferringTracker') 'referral-map'
               } else {
                   Join-Path (Join-Path $HOME '.order-referring-tracker') 'referral-map'
               }
    Year     = 2015
    Interval = 30
    # NPPES lookups for source-provider names/specialties are capped per run;
    # results are cached on disk so repeat runs are cheap.
    EnrichCap = 400
}

# Taxonomy codes that define "outpatient rehab clinic" for this tool.
# COUPLING WARNING: if you add a code here, you MUST also make sure at least
# one entry in $script:RmSearchTerms below phrase-matches that taxonomy's NPPES
# description, or providers with the new code will silently never be found —
# NPPES only supports description phrase search, and e.g. 'Physical Therapist'
# and 'Physical Therapy' are DIFFERENT phrases (this exact miss happened once:
# 4 vs 45 providers found in one ZIP).
$script:RmClinicTaxonomies = @{
    '261QP2000X' = 'Clinic/Center: Physical Therapy'
    '261QR0400X' = 'Clinic/Center: Rehabilitation'
}
$script:RmIndividualTaxonomies = @{
    '225100000X' = 'Physical Therapist'
    '225X00000X' = 'Occupational Therapist'
    '235Z00000X' = 'Speech-Language Pathologist'
}
# NPPES taxonomy_description search terms used to sweep a ZIP (results are then
# filtered to the exact codes above). NPPES phrase-matches descriptions, so
# "Physical Therapist" (individuals) and "Physical Therapy" (clinics/centers)
# are DIFFERENT searches — both are needed.
$script:RmSearchTerms = @('Physical Therapist', 'Physical Therapy',
                          'Occupational Therapist', 'Speech-Language Pathologist',
                          'Rehabilitation')

function Get-RmConfig {
    <# .SYNOPSIS Returns the referral-map configuration. #>
    [pscustomobject]$script:RmConfig
}

function Set-RmConfig {
    <# .SYNOPSIS Overrides referral-map configuration. #>
    [CmdletBinding()]
    param(
        [string]$DataDir,
        [ValidateRange(2009, 2015)][int]$Year,
        [ValidateSet(30, 60, 90, 180)][int]$Interval,
        [ValidateRange(0, 100000)][int]$EnrichCap
    )
    if ($DataDir)  { $script:RmConfig.DataDir = $DataDir }
    if ($Year)     { $script:RmConfig.Year = $Year }
    if ($Interval) { $script:RmConfig.Interval = $Interval }
    if ($PSBoundParameters.ContainsKey('EnrichCap')) { $script:RmConfig.EnrichCap = $EnrichCap }
}

# ---------------------------------------------------------------------------
# Fast scan engine (compiled once per process)
# ---------------------------------------------------------------------------

if (-not ('RmEngine' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

public class RmEdge
{
    public string SourceNpi;
    public string TargetNpi;
    public int PairCount;
    public int BeneCount;
    public int SameDayCount;
}

public static class RmEngine
{
    // Streams the CMS shared-patient file (headerless CSV: NPI1,NPI2,PairCount,
    // BeneCount,SameDayCount with space-padded numbers) and returns every row
    // whose NPI2 (the provider who saw the patient SECOND) is in targets.
    // Throws with a plain message if the file does not look like the expected
    // format, rather than returning wrong numbers.
    public static List<RmEdge> ScanInbound(string path, HashSet<string> targets)
    {
        List<RmEdge> edges = new List<RmEdge>();
        long lineNo = 0;
        long badLines = 0;
        using (StreamReader reader = new StreamReader(path, Encoding.ASCII, false, 1 << 20))
        {
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                lineNo++;
                if (line.Length == 0) continue;
                string[] f = line.Split(',');
                if (f.Length != 5)
                {
                    badLines++;
                    if (lineNo <= 5)
                        throw new InvalidDataException(
                            "Line " + lineNo + " of '" + path + "' has " + f.Length +
                            " fields; expected 5 (NPI1,NPI2,PairCount,BeneCount,SameDayCount). " +
                            "This does not look like a CMS shared-patient file.");
                    continue;
                }
                string npi2 = f[1].Trim();
                if (!targets.Contains(npi2)) continue;
                RmEdge e = new RmEdge();
                e.SourceNpi = f[0].Trim();
                e.TargetNpi = npi2;
                int v;
                e.PairCount = int.TryParse(f[2].Trim(), out v) ? v : 0;
                e.BeneCount = int.TryParse(f[3].Trim(), out v) ? v : 0;
                e.SameDayCount = int.TryParse(f[4].Trim(), out v) ? v : 0;
                edges.Add(e);
            }
        }
        if (lineNo == 0)
            throw new InvalidDataException("The file '" + path + "' is empty.");
        if (badLines > lineNo / 100)
            throw new InvalidDataException(
                "The file '" + path + "' had " + badLines + " malformed lines out of " +
                lineNo + "; refusing to report numbers from a file that malformed.");
        return edges;
    }

    // Counts lines quickly (used to validate a freshly extracted dataset).
    public static long CountLines(string path)
    {
        long n = 0;
        using (StreamReader reader = new StreamReader(path, Encoding.ASCII, false, 1 << 20))
        {
            while (reader.ReadLine() != null) n++;
        }
        return n;
    }
}
'@
}

# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

function Get-RmProp([object]$Object, [string]$Name) {
    if ($null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]) {
        $Object.PSObject.Properties[$Name].Value
    } else {
        $null
    }
}

# Neutralizes CSV/Excel formula injection. NPPES names are self-declared by
# whoever registered the NPI, so an org named =HYPERLINK(...) could execute
# when the exported CSV is opened in Excel. Self-contained (no dependency on
# the OrderReferring module, which may not be loaded in a worker runspace).
function ConvertTo-RmSafeCsvRecord {
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

# CSV with a UTF-8 BOM on both PS 5.1 and 7 (see the OrderReferring twin for
# why); streams via ConvertTo-Csv.
function Write-RmCsvFile {
    param([object[]]$Rows, [string]$Path)
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $enc = New-Object System.Text.UTF8Encoding($true)
    $sw = New-Object System.IO.StreamWriter($full, $false, $enc)
    try {
        $Rows | ConvertTo-RmSafeCsvRecord | ConvertTo-Csv -NoTypeInformation |
            ForEach-Object { $sw.WriteLine($_) }
    } finally { $sw.Dispose() }
}

# The last date of MEDICARE SERVICE that can appear in a given release. A
# provider whose NPI was issued after this could not be in the file, so their
# zero counts mean "did not exist yet", not "no referrals". The 2015 file was
# cut off mid-year (services through ~Sep 1, 2015); earlier years span the full
# calendar year. Source: CMS shared-patient methodology date-range table.
function Get-RmDataWindowEnd([int]$Year) {
    if ($Year -eq 2015) { [datetime]'2015-09-01' } else { [datetime]("{0}-12-31" -f $Year) }
}

function Initialize-RmDataDir {
    $d = $script:RmConfig.DataDir
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    $d
}

function Clear-RmStaleTemp {
    <# .SYNOPSIS Removes abandoned *.tmp download/extract partials whose last
       write is older than the threshold (e.g. from a window closed mid-download).
       An actively-downloading file keeps being written, so it is spared. #>
    [CmdletBinding()]
    param([int]$OlderThanMinutes = 360)
    $d = $script:RmConfig.DataDir
    if (-not (Test-Path -LiteralPath $d)) { return }
    $cutoff = (Get-Date).AddMinutes(-$OlderThanMinutes)
    Get-ChildItem -LiteralPath $d -Filter '*.tmp' -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

function Get-RmDatasetPath {
    <# .SYNOPSIS Path where the extracted shared-patient file lives (may not exist yet). #>
    param([int]$Year = $script:RmConfig.Year, [int]$Interval = $script:RmConfig.Interval)
    Join-Path $script:RmConfig.DataDir ('pspp_{0}_days{1}.txt' -f $Year, $Interval)
}

function Get-RmNppesCachePath { Join-Path $script:RmConfig.DataDir 'nppes-cache.json' }

function Read-RmNppesCache {
    $p = Get-RmNppesCachePath
    $cache = @{}
    if (Test-Path -LiteralPath $p) {
        try {
            # -Encoding UTF8 so a BOM-less cache written under PS 7 is not
            # ANSI-misdecoded under a later Windows PowerShell 5.1 run (which
            # would permanently corrupt cached non-ASCII provider names).
            $json = Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($prop in $json.PSObject.Properties) { $cache[$prop.Name] = $prop.Value }
        } catch {
            Write-Warning "NPPES cache was unreadable and will be rebuilt: $($_.Exception.Message)"
        }
    }
    $cache
}

function Write-RmNppesCache([hashtable]$Cache) {
    Initialize-RmDataDir | Out-Null
    $p = Get-RmNppesCachePath
    $tmp = $p + '.tmp'
    $Cache | ConvertTo-Json -Depth 5 -Compress | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $p -Force
}

function Test-RmNpiShape([string]$Npi) { $Npi -match '^\d{10}$' }

# The FOIA download must come from CMS over https (or a loopback host for the
# test doubles) — the URL template is overridable, so validate before fetching.
function Assert-RmSafeUrl([string]$Url) {
    $u = $null
    if (-not [uri]::TryCreate($Url, [System.UriKind]::Absolute, [ref]$u)) {
        throw "Unusable dataset download URL: '$Url'."
    }
    $okHost = $u.Host -eq 'downloads.cms.gov' -or $u.Host -eq 'data.cms.gov' -or $u.IsLoopback
    if (($u.Scheme -ne 'https' -and -not ($u.Scheme -eq 'http' -and $u.IsLoopback)) -or -not $okHost) {
        throw "Refusing to download from '$Url': only https CMS hosts (or loopback for testing) are allowed."
    }
}

# Rejects zip-slip: any entry whose resolved path escapes the extraction dir.
function Assert-RmSafeZip([string]$ZipPath) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            if ($name -match '^[/\\]' -or $name -match '(^|[/\\])\.\.([/\\]|$)' -or $name -match '^[A-Za-z]:') {
                throw "The downloaded zip contains an unsafe entry path ('$name'); refusing to extract it."
            }
        }
    } finally { $zip.Dispose() }
}

function Invoke-RmNppes([string]$Query) {
    $url = '{0}?version=2.1&{1}' -f $script:RmConfig.NppesUrl, $Query
    try {
        Invoke-RestMethod -Uri $url -TimeoutSec 60 -ErrorAction Stop
    } catch {
        throw ("The NPPES registry (npiregistry.cms.hhs.gov) could not be reached. " +
               "Check your internet connection. Details: $($_.Exception.Message)")
    }
}

# ---------------------------------------------------------------------------
# Dataset download
# ---------------------------------------------------------------------------

function Save-RmDataset {
    <#
    .SYNOPSIS
      Downloads and extracts one CMS shared-patient file (default: 2015, 30-day
      window — the newest public release; ~356 MB download, ~1.7 GB on disk).
      Skips the download if the file is already present.
    #>
    [CmdletBinding()]
    param(
        [ValidateRange(2009, 2015)][int]$Year = $script:RmConfig.Year,
        [ValidateSet(30, 60, 90, 180)][int]$Interval = $script:RmConfig.Interval,
        [switch]$Force
    )

    Initialize-RmDataDir | Out-Null
    $target = Get-RmDatasetPath -Year $Year -Interval $Interval
    if ((Test-Path -LiteralPath $target) -and -not $Force) {
        return [pscustomobject]@{
            Downloaded = $false; Path = $target
            Message = "Dataset $Year/${Interval}-day already present."
        }
    }

    # WinPS 5.1's progress bar slows large -OutFile downloads ~10x; suppress it.
    # Unique temp names so two concurrent runs can never share a partial file.
    $ProgressPreference = 'SilentlyContinue'
    Clear-RmStaleTemp   # sweep partials abandoned by an earlier killed run
    $runId = [guid]::NewGuid().ToString('N')
    $url = $script:RmConfig.FoiaUrlTemplate -f $Year, $Interval
    Assert-RmSafeUrl $url
    $zipPath = Join-Path $script:RmConfig.DataDir ('pspp_{0}_days{1}.{2}.zip.tmp' -f $Year, $Interval, $runId)
    $extractDir = Join-Path $script:RmConfig.DataDir ('extract_{0}_{1}.{2}.tmp' -f $Year, $Interval, $runId)
    try {
        Write-Verbose "Downloading $url"
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing -TimeoutSec 7200 -ErrorAction Stop
        # Guard against a hostile/oversized payload (largest real file ~600 MB).
        $zipBytes = (Get-Item -LiteralPath $zipPath).Length
        if ($zipBytes -gt 2GB) {
            throw "The downloaded file is $([int]($zipBytes/1MB)) MB, far larger than any real CMS release; refusing it."
        }
        if (Test-Path -LiteralPath $extractDir) { Remove-Item -Recurse -Force $extractDir }
        Assert-RmSafeZip $zipPath   # reject zip-slip entry names before extracting
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
        $txt = @(Get-ChildItem -LiteralPath $extractDir -Filter '*.txt')
        if ($txt.Count -ne 1) {
            throw "Expected exactly one .txt inside the CMS zip, found $($txt.Count)."
        }
        # Validate before promoting: first lines must parse as 5-field rows.
        $probe = [System.IO.File]::OpenText($txt[0].FullName)
        try {
            for ($i = 0; $i -lt 3; $i++) {
                $line = $probe.ReadLine()
                if ($null -eq $line -or @($line -split ',').Count -ne 5) {
                    throw "Downloaded file does not look like a CMS shared-patient file (line $($i+1))."
                }
            }
        } finally { $probe.Close() }
        Move-Item -LiteralPath $txt[0].FullName -Destination $target -Force
    } catch {
        throw ("Downloading the CMS shared-patient dataset failed; nothing was changed. " +
               "Details: $($_.Exception.Message)")
    } finally {
        Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    $rows = [RmEngine]::CountLines($target)
    [pscustomobject]@{
        Downloaded = $true; Path = $target; RowCount = $rows
        Message = "Downloaded CMS shared-patient data $Year/${Interval}-day: $('{0:N0}' -f $rows) provider pairs."
    }
}

function Get-RmStatus {
    <# .SYNOPSIS Shows which shared-patient dataset files are on disk. #>
    $target = Get-RmDatasetPath
    [pscustomobject]@{
        DataDir      = $script:RmConfig.DataDir
        Year         = $script:RmConfig.Year
        Interval     = $script:RmConfig.Interval
        DatasetPath  = $target
        DatasetReady = (Test-Path -LiteralPath $target)
        DatasetBytes = if (Test-Path -LiteralPath $target) { (Get-Item -LiteralPath $target).Length } else { 0 }
    }
}

# ---------------------------------------------------------------------------
# NPPES: find rehab clinics in a ZIP, look up provider details
# ---------------------------------------------------------------------------

function Find-RmClinic {
    <#
    .SYNOPSIS
      Finds outpatient rehab providers in a ZIP code (or ZIP prefix like 630*)
      via the live NPPES registry: PT/rehab clinic organizations and, unless
      -OrganizationsOnly, individual PT/OT/SLP providers (solo practices bill
      under individual NPIs).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidatePattern('^\d{3,5}\*?$')][string]$Zip,
        [switch]$OrganizationsOnly
    )

    $zipPrefix = $Zip.TrimEnd('*')
    $allowed = @{}
    foreach ($kv in $script:RmClinicTaxonomies.GetEnumerator()) { $allowed[$kv.Key] = $kv.Value }
    if (-not $OrganizationsOnly) {
        foreach ($kv in $script:RmIndividualTaxonomies.GetEnumerator()) { $allowed[$kv.Key] = $kv.Value }
    }

    $found = @{}
    foreach ($term in $script:RmSearchTerms) {
        $skip = 0
        while ($true) {
            $q = 'postal_code={0}&taxonomy_description={1}&limit=200&skip={2}' -f
                [uri]::EscapeDataString($Zip), [uri]::EscapeDataString($term), $skip
            $resp = Invoke-RmNppes $q
            $results = @(Get-RmProp $resp 'results')
            foreach ($r in $results) {
                $npi = [string](Get-RmProp $r 'number')
                if ($found.ContainsKey($npi)) { continue }

                # Exact taxonomy filter (search terms are fuzzy — e.g. "Physical
                # Therapy" also returns PT Assistants, which we exclude).
                $taxes = @(Get-RmProp $r 'taxonomies')
                $matched = @($taxes | Where-Object { $allowed.ContainsKey([string](Get-RmProp $_ 'code')) })
                if ($matched.Count -eq 0) { continue }

                # Practice LOCATION must actually be in the requested ZIP
                # (NPPES also matches mailing addresses).
                $loc = @(@(Get-RmProp $r 'addresses') | Where-Object {
                    (Get-RmProp $_ 'address_purpose') -eq 'LOCATION' -and
                    ([string](Get-RmProp $_ 'postal_code')).StartsWith($zipPrefix)
                })
                if ($loc.Count -eq 0) { continue }

                $basic = Get-RmProp $r 'basic'
                $isOrg = ((Get-RmProp $r 'enumeration_type') -eq 'NPI-2')
                $name = if ($isOrg) { [string](Get-RmProp $basic 'organization_name') }
                        else { ('{0} {1}' -f (Get-RmProp $basic 'first_name'), (Get-RmProp $basic 'last_name')).Trim() }
                # NPPES postal codes are usually ZIP+4 but malformed/short
                # values exist in the live registry; never let one bad row
                # abort the whole query.
                $postal = [string](Get-RmProp $loc[0] 'postal_code')
                $found[$npi] = [pscustomobject]@{
                    NPI        = $npi
                    Name       = $name
                    Type       = if ($isOrg) { 'Organization' } else { 'Individual' }
                    Taxonomy   = $allowed[[string](Get-RmProp $matched[0] 'code')]
                    City       = [string](Get-RmProp $loc[0] 'city')
                    State      = [string](Get-RmProp $loc[0] 'state')
                    Zip        = $postal.Substring(0, [Math]::Min(5, $postal.Length))
                    Enumerated = [string](Get-RmProp $basic 'enumeration_date')
                }
            }
            if ($results.Count -lt 200) { break }
            if ($skip -ge 1000) {
                # NPPES refuses to page past skip=1000; a full last page means
                # there are probably more providers we cannot see.
                Write-Warning ("NPPES returned its maximum of 1,200 results for '$term' in '$Zip' — " +
                    "the provider list may be incomplete. Use a narrower ZIP (a full 5-digit ZIP " +
                    "instead of a prefix) to make sure nothing is missed.")
                break
            }
            $skip += 200
        }
    }
    $found.Values | Sort-Object Name
}

function Get-RmProviderDetail {
    <#
    .SYNOPSIS
      Looks up name/specialty/location for NPIs via NPPES, using the on-disk
      cache. Returns a hashtable NPI -> detail object.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Npi)

    $cache = Read-RmNppesCache
    $result = @{}
    # Only look up well-formed NPIs — the source NPIs come from an external data
    # file and must never be concatenated into a URL or cache key unvalidated.
    $missing = @($Npi | Sort-Object -Unique |
        Where-Object { (Test-RmNpiShape $_) -and -not $cache.ContainsKey($_) })
    $n = 0
    $consecutiveFailures = 0
    foreach ($id in $missing) {
        if ($consecutiveFailures -ge 3) {
            # The registry (or the connection) is down — stop hammering it.
            # Un-looked-up NPIs stay uncached so the next run retries them.
            Write-Warning ("NPPES lookups failed 3 times in a row; skipping the remaining " +
                "$(@($missing).Count - $n) lookups. Results show NPIs without names; run again later to fill them in.")
            break
        }
        $n++
        if ($n % 10 -eq 0) { Start-Sleep -Milliseconds 200 }   # be polite to NPPES
        try {
            $resp = Invoke-RmNppes ('number={0}' -f [uri]::EscapeDataString($id))
            $consecutiveFailures = 0
            $results = @(Get-RmProp $resp 'results')
            if ($results.Count -eq 0) {
                $cache[$id] = [pscustomobject]@{
                    Name = '(NPI deactivated or not found)'; Specialty = ''; City = ''; State = '' }
            } else {
                $r = $results[0]
                $basic = Get-RmProp $r 'basic'
                $isOrg = ((Get-RmProp $r 'enumeration_type') -eq 'NPI-2')
                $name = if ($isOrg) { [string](Get-RmProp $basic 'organization_name') }
                        else { ('{0} {1}' -f (Get-RmProp $basic 'first_name'), (Get-RmProp $basic 'last_name')).Trim() }
                $primary = @(@(Get-RmProp $r 'taxonomies') | Where-Object { (Get-RmProp $_ 'primary') -eq $true })
                $loc = @(@(Get-RmProp $r 'addresses') | Where-Object { (Get-RmProp $_ 'address_purpose') -eq 'LOCATION' })
                $cache[$id] = [pscustomobject]@{
                    Name      = $name
                    Specialty = if ($primary.Count) { [string](Get-RmProp $primary[0] 'desc') } else { '' }
                    City      = if ($loc.Count) { [string](Get-RmProp $loc[0] 'city') } else { '' }
                    State     = if ($loc.Count) { [string](Get-RmProp $loc[0] 'state') } else { '' }
                }
            }
        } catch {
            # Leave uncached so a later run can retry; report honestly for now.
            $consecutiveFailures++
            $result[$id] = [pscustomobject]@{ Name = '(lookup failed)'; Specialty = ''; City = ''; State = '' }
        }
    }
    if ($missing.Count -gt 0) { Write-RmNppesCache $cache }
    foreach ($id in $Npi) {
        if (-not $result.ContainsKey($id)) {
            $result[$id] = if ($cache.ContainsKey($id)) { $cache[$id] }
                           else { [pscustomobject]@{ Name = '(lookup failed)'; Specialty = ''; City = ''; State = '' } }
        }
    }
    $result
}

# ---------------------------------------------------------------------------
# The main query
# ---------------------------------------------------------------------------

function Get-RmReferralMap {
    <#
    .SYNOPSIS
      For a ZIP code (or prefix like 630*): finds the outpatient rehab
      providers there, scans the CMS shared-patient file for everyone who fed
      Medicare patients into them, and returns ranked results.
    .OUTPUTS
      One object with .Clinics (recipients ranked by inbound shared patients),
      .Sources (every source->clinic edge, enriched with names/specialties),
      and .Notes (methodology/limitations text used by exports).
    .EXAMPLE
      $map = Get-RmReferralMap -Zip 63017
      $map.Clinics | Format-Table
      $map.Sources | Where-Object ClinicNPI -eq 1234567893 | Format-Table
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidatePattern('^\d{3,5}\*?$')][string]$Zip,
        [switch]$OrganizationsOnly,
        [switch]$SkipEnrichment   # NPI-only output when offline
    )

    $dataset = Get-RmDatasetPath
    if (-not (Test-Path -LiteralPath $dataset)) {
        throw ("The CMS shared-patient dataset is not downloaded yet. Run Save-RmDataset " +
               "(or click 'Download CMS dataset' in the app) first — it is a one-time ~356 MB download.")
    }

    Write-Verbose "Finding rehab providers in ZIP $Zip via NPPES..."
    $clinics = @(Find-RmClinic -Zip $Zip -OrganizationsOnly:$OrganizationsOnly)
    if ($clinics.Count -eq 0) {
        throw ("NPPES lists no outpatient rehab providers (PT/rehab clinics" +
               $(if (-not $OrganizationsOnly) { ", individual PT/OT/SLPs" }) +
               ") with a practice location in ZIP '$Zip'. Try a broader prefix like '$($Zip.Substring(0,3))*'.")
    }
    Write-Verbose "Found $($clinics.Count) rehab providers. Scanning shared-patient file..."

    $targets = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($c in $clinics) { [void]$targets.Add($c.NPI) }
    $edges = [RmEngine]::ScanInbound($dataset, $targets)
    Write-Verbose "Scan complete: $($edges.Count) inbound edges."

    $clinicByNpi = @{}
    foreach ($c in $clinics) { $clinicByNpi[$c.NPI] = $c }

    # Enrich source providers (top by volume, capped; cache makes reruns cheap).
    $detail = @{}
    $enrichNote = ''
    if (-not $SkipEnrichment) {
        # Rank each source by its TOTAL shared patients across all its edges (a
        # source feeding several clinics 15 each must outrank a single 20-edge).
        $sourceTotals = @{}
        foreach ($e in $edges) {
            if (-not $sourceTotals.ContainsKey($e.SourceNpi)) { $sourceTotals[$e.SourceNpi] = 0 }
            $sourceTotals[$e.SourceNpi] += $e.BeneCount
        }
        $distinctSources = @($sourceTotals.GetEnumerator() | Sort-Object Value -Descending |
            ForEach-Object { $_.Key })
        $toEnrich = @($distinctSources | Select-Object -First $script:RmConfig.EnrichCap)
        if ($distinctSources.Count -gt $toEnrich.Count) {
            $enrichNote = ("Provider details were looked up for the top {0} of {1} distinct sources " +
                "(by total shared-patient volume); the rest show NPI only. Raise the cap with Set-RmConfig -EnrichCap.") -f
                $toEnrich.Count, $distinctSources.Count
            Write-Warning $enrichNote
        }
        if ($toEnrich.Count -gt 0) {
            Write-Verbose "Looking up $($toEnrich.Count) source providers in NPPES..."
            $detail = Get-RmProviderDetail -Npi $toEnrich
        }
    }

    $sources = @($edges | Sort-Object BeneCount -Descending | ForEach-Object {
        $d = if ($detail.ContainsKey($_.SourceNpi)) { $detail[$_.SourceNpi] } else { $null }
        $clinic = $clinicByNpi[$_.TargetNpi]
        [pscustomobject]@{
            SourceNPI       = $_.SourceNpi
            SourceName      = if ($d) { $d.Name } else { '' }
            SourceSpecialty = if ($d) { $d.Specialty } else { '' }
            SourceCity      = if ($d) { $d.City } else { '' }
            SourceState     = if ($d) { $d.State } else { '' }
            ClinicNPI       = $_.TargetNpi
            ClinicName      = $clinic.Name
            SharedPatients  = $_.BeneCount
            SharedEvents    = $_.PairCount
            SameDay         = $_.SameDayCount
        }
    })

    $byClinic = @{}
    foreach ($e in $edges) {
        if (-not $byClinic.ContainsKey($e.TargetNpi)) {
            $byClinic[$e.TargetNpi] = [pscustomobject]@{ Benes = 0; SameDay = 0; Sources = 0 }
        }
        $agg = $byClinic[$e.TargetNpi]
        $agg.Benes += $e.BeneCount
        $agg.SameDay += $e.SameDayCount
        $agg.Sources += 1
    }
    # NPPES reflects TODAY's providers; an NPI enumerated after the last date of
    # SERVICE in the file cannot appear in it. Flag those honestly instead of
    # letting their zero rows read as "no referrals". Dates are PARSED, not
    # string-compared — a format change from NPPES must yield '' (unknown),
    # never a wrong Yes/No. The 2015 file was cut off ~Sep 1, 2015, so a
    # provider enumerated in, say, Nov 2015 could not appear.
    $dataWindowEnd = Get-RmDataWindowEnd $script:RmConfig.Year
    $clinicRows = @($clinics | ForEach-Object {
        $enumDate = [datetime]::MinValue
        $parsed = -not [string]::IsNullOrEmpty($_.Enumerated) -and
                  [datetime]::TryParseExact($_.Enumerated, 'yyyy-MM-dd',
                      [System.Globalization.CultureInfo]::InvariantCulture,
                      [System.Globalization.DateTimeStyles]::None, [ref]$enumDate)
        $agg = if ($byClinic.ContainsKey($_.NPI)) { $byClinic[$_.NPI] } else { $null }
        $existed = if (-not $parsed) { '' }
                   elseif ($enumDate -le $dataWindowEnd) { 'Yes' }
                   else { "No (NPI issued $($_.Enumerated))" }
        [pscustomobject]@{
            NPI             = $_.NPI
            Name            = $_.Name
            Type            = $_.Type
            Taxonomy        = $_.Taxonomy
            City            = $_.City
            State           = $_.State
            Zip             = $_.Zip
            ReferralSources = if ($agg) { $agg.Sources } else { 0 }
            SharedPatients  = if ($agg) { $agg.Benes } else { 0 }
            SameDay         = if ($agg) { $agg.SameDay } else { 0 }
            ExistedInDataYear = $existed
        }
    } | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                              @{Expression = 'Name'; Descending = $false})
    $notYetEnumerated = @($clinicRows | Where-Object { $_.ExistedInDataYear -like 'No*' }).Count

    $notes = @(
        ("Source: CMS Physician Shared Patient Patterns (FOIA release), year {0}, {1}-day interval." -f
            $script:RmConfig.Year, $script:RmConfig.Interval)
        $(if ($script:RmConfig.Year -eq 2015) { 'The 2015 file covers claims from 2015-01-01 to 2015-09-01 — the NEWEST public release of this data. It shows the historical structure of the referral market, NOT current volumes.' }
          else { 'This is historical data; it shows the structure of the referral market at that time, NOT current volumes.' })
        'SharedPatients in the SOURCES table = unique Medicare beneficiaries that source provider shared with that one clinic within the interval window (CMS referral proxy; not billed referrals).'
        'SharedPatients in the CLINICS table = the SUM of those per-source counts, NOT a unique-patient total: a patient sent by three sources is counted three times. Treat it as relative referral VOLUME, not a headcount of distinct patients.'
        'Pairs sharing fewer than 11 patients within the file''s window are excluded by CMS, so low-volume referrers are invisible. (For 2015 that window is ~8 months, Jan–Sep, not a full year.)'
        'Same-day pairs are attributed by CMS to the LOWER NPI as the initiator. This scan only captures rows where the clinic is the SECOND provider, so same-day activity in which the clinic holds the lower NPI is not counted — the SameDay column is a partial, direction-ambiguous subset, useful only as a rough signal.'
        'Shared-patient pairs also capture co-occurring care — labs, imaging, and hospitals seen in the same window appear as "sources" without having referred anyone. Interpret sources by specialty: an orthopedic surgeon feeding a PT is referral-like; a lab is not.'
        'Clinic list = NPPES providers with a practice location in the requested ZIP holding taxonomies: ' +
            (@($script:RmClinicTaxonomies.Values) + $(if (-not $OrganizationsOnly) { @($script:RmIndividualTaxonomies.Values) } else { @() }) -join ', ') + '.'
        'NPPES reflects providers and addresses as of TODAY. Providers whose NPI was issued after the data year are flagged in ExistedInDataYear — their zero counts mean "did not exist yet", not "no referrals". Clinics that moved or re-enumerated since the data year can also show zero; a ZIP prefix search (e.g. 630*) widens the net.'
        'IMPORTANT: private-practice ORGANIZATION NPIs rarely appear in this file. CMS built the pairs from performing (rendering) provider NPIs on office claims and facility NPIs on institutional claims — a private clinic''s billing/group NPI is generally not included. Private practices therefore show up through their INDIVIDUAL therapists; hospital rehab departments show up as organizations. (Verified: 130 pre-2015 PT-chain org NPIs matched 0 rows, while a 244-therapist national sample matched 280 inbound rows.)'
        $(if ($notYetEnumerated -gt 0) { '{0} of {1} providers found in this ZIP were issued their NPI after the {2} file''s service window ended, so they cannot appear in it (ExistedInDataYear = No).' -f $notYetEnumerated, $clinicRows.Count, $script:RmConfig.Year })
        $(if ($enrichNote) { $enrichNote })
    ) | Where-Object { $_ }

    [pscustomobject]@{
        Zip     = $Zip
        Clinics = $clinicRows
        Sources = $sources
        Notes   = @($notes)
    }
}

function Export-RmResult {
    <#
    .SYNOPSIS
      Exports referral-map rows to CSV with a methodology sidecar that states
      the data vintage and limitations.
    #>
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
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        if ($rows.Count -eq 0) {
            Set-Content -LiteralPath $Path -Value '' -Encoding UTF8
        } else {
            Write-RmCsvFile -Rows $rows -Path $Path
        }
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
    'Get-RmConfig', 'Set-RmConfig', 'Get-RmStatus', 'Get-RmDatasetPath',
    'Save-RmDataset', 'Find-RmClinic', 'Get-RmProviderDetail',
    'Get-RmReferralMap', 'Export-RmResult', 'Clear-RmStaleTemp'
)
