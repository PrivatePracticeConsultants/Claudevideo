# ReferralMap.psm1 — regional referral-source mapping for outpatient rehab
# clinics, joined with the live NPPES registry. Two dataset sources:
#
#  - CMS Physician Shared Patient Patterns FOIA data (2009–2015): free public
#    download, headerless 5-field CSV, newest release Jan–Sep 2015.
#  - DocGraph Hop Teaming (CareSet Systems): annual releases years newer than
#    2015, licensed from CareSet and imported from a local file the user
#    obtained (6-field CSV with a header: from_npi,to_npi,patient_count,
#    transaction_count,average_day_wait,std_day_wait; full calendar year).
#
# What it answers: for a ZIP code, which providers were the top-volume FEEDERS
# of Medicare patients into each outpatient rehab clinic in that area — i.e.
# rows where the clinic is the SECOND provider in the pair (saw the patient
# after the source provider).
#
# Honesty notes baked into every output:
#  - Both sources map the STRUCTURE of a referral market for the file's year,
#    not this year's volumes; the notes state the active dataset's vintage.
#  - "Shared patients" is a privacy-preserving referral proxy (same patient
#    seen by both providers in sequence); pairs under 11 patients in the
#    window are excluded per CMS policy, so small referrers are invisible.
#  - Direction is claims sequence, not literal referrals — judge by specialty.
#
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
    public double AvgDayWait;   // Hop Teaming only; 0 for CMS files
}

public static class RmEngine
{
    // Dataset layouts. CMS: headerless 5-field CSV NPI1,NPI2,PairCount,
    // BeneCount,SameDayCount. HopTeaming (CareSet DocGraph): 6-field CSV with
    // a header row: from_npi,to_npi,patient_count,transaction_count,
    // average_day_wait,std_day_wait.
    public const int FormatCms = 0;
    public const int FormatHopTeaming = 1;

    // Parses one line into an edge, or returns null for a line that is not a
    // data row (the Hop Teaming header). Throws on a wrong field count.
    private static RmEdge ParseLine(string line, int format, string path, long lineNo)
    {
        string[] f = line.Split(',');
        int want = (format == FormatHopTeaming) ? 6 : 5;
        if (f.Length != want)
            throw new InvalidDataException(
                "Line " + lineNo + " of '" + path + "' has " + f.Length +
                " fields; expected " + want + ". This does not look like a " +
                (format == FormatHopTeaming ? "DocGraph Hop Teaming" : "CMS shared-patient") + " file.");
        if (format == FormatHopTeaming && lineNo == 1 && f[0].TrimStart('\uFEFF').Trim().ToLowerInvariant() == "from_npi")
            return null;   // header row
        RmEdge e = new RmEdge();
        e.SourceNpi = f[0].Trim();
        e.TargetNpi = f[1].Trim();
        int v;
        if (format == FormatHopTeaming)
        {
            e.BeneCount = int.TryParse(f[2].Trim(), out v) ? v : 0;         // patient_count
            e.PairCount = int.TryParse(f[3].Trim(), out v) ? v : 0;         // transaction_count
            e.SameDayCount = 0;                                             // not in this file
            double d;
            // InvariantCulture: the file uses '.' decimals; a machine with a
            // ','-decimal locale must not misread 52.3 as 523.
            e.AvgDayWait = double.TryParse(f[4].Trim(), System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out d) ? d : 0.0;
        }
        else
        {
            e.PairCount = int.TryParse(f[2].Trim(), out v) ? v : 0;
            e.BeneCount = int.TryParse(f[3].Trim(), out v) ? v : 0;
            e.SameDayCount = int.TryParse(f[4].Trim(), out v) ? v : 0;
            e.AvgDayWait = 0.0;
        }
        return e;
    }

    // Streams the dataset file and returns rows matching a set of NPIs on one
    // column. matchColumn 1 = the SECOND provider in the pair (inbound: the
    // recipient); 0 = the FIRST (outbound: the initiator). Throws with a
    // plain message on a malformed file rather than returning wrong numbers.
    private static List<RmEdge> Scan(string path, HashSet<string> match, int matchColumn, int format)
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
                RmEdge e;
                try { e = ParseLine(line, format, path, lineNo); }
                catch (InvalidDataException)
                {
                    badLines++;
                    if (lineNo <= 5) throw;
                    continue;
                }
                if (e == null) continue;   // header row
                string key = (matchColumn == 1) ? e.TargetNpi : e.SourceNpi;
                if (!match.Contains(key)) continue;
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

    // Rows where a target NPI is the SECOND provider (received the patient).
    public static List<RmEdge> ScanInbound(string path, HashSet<string> targets, int format)
    {
        return Scan(path, targets, 1, format);
    }
    public static List<RmEdge> ScanInbound(string path, HashSet<string> targets)
    {
        return Scan(path, targets, 1, FormatCms);
    }

    // Rows where a source NPI is the FIRST provider (initiated / sent onward).
    public static List<RmEdge> ScanOutbound(string path, HashSet<string> sources, int format)
    {
        return Scan(path, sources, 0, format);
    }
    public static List<RmEdge> ScanOutbound(string path, HashSet<string> sources)
    {
        return Scan(path, sources, 0, FormatCms);
    }

    // Rows where an NPI in 'set' appears in EITHER column — one pass for a
    // single-provider 360 view (inbound + outbound together).
    public static List<RmEdge> ScanEither(string path, HashSet<string> set, int format)
    {
        List<RmEdge> edges = new List<RmEdge>();
        long lineNo = 0, badLines = 0;
        using (StreamReader reader = new StreamReader(path, Encoding.ASCII, false, 1 << 20))
        {
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                lineNo++;
                if (line.Length == 0) continue;
                RmEdge e;
                try { e = ParseLine(line, format, path, lineNo); }
                catch (InvalidDataException)
                {
                    badLines++;
                    if (lineNo <= 5) throw;
                    continue;
                }
                if (e == null) continue;
                if (!set.Contains(e.SourceNpi) && !set.Contains(e.TargetNpi)) continue;
                edges.Add(e);
            }
        }
        if (lineNo == 0) throw new InvalidDataException("The file '" + path + "' is empty.");
        if (badLines > lineNo / 100)
            throw new InvalidDataException("The file '" + path + "' had too many malformed lines.");
        return edges;
    }
    public static List<RmEdge> ScanEither(string path, HashSet<string> set)
    {
        return ScanEither(path, set, FormatCms);
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
# zero counts mean "did not exist yet", not "no referrals". The CMS 2015 file
# was cut off mid-year (services through ~Sep 1, 2015); earlier CMS years and
# all Hop Teaming years span the full calendar year. Sources: CMS shared-
# patient methodology date-range table; CareSet DocGraph readme ("Shared
# patients in time Jan 1 - Dec 31").
function Get-RmDataWindowEnd([int]$Year, [string]$Source = 'cms-pspp') {
    if ($Source -eq 'cms-pspp' -and $Year -eq 2015) { [datetime]'2015-09-01' }
    else { [datetime]("{0}-12-31" -f $Year) }
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
    <# .SYNOPSIS Path where the extracted CMS shared-patient file lives (may not exist yet). #>
    param([int]$Year = $script:RmConfig.Year, [int]$Interval = $script:RmConfig.Interval)
    Join-Path $script:RmConfig.DataDir ('pspp_{0}_days{1}.txt' -f $Year, $Interval)
}

function Get-RmDatasetMetaPath { Join-Path $script:RmConfig.DataDir 'dataset-meta.json' }

# Records which dataset is ACTIVE (the one queries scan). Written atomically by
# Save-RmDataset (CMS download) and Import-RmDataset (CareSet file).
function Write-RmDatasetMeta([hashtable]$Meta) {
    Initialize-RmDataDir | Out-Null
    $p = Get-RmDatasetMetaPath
    $tmp = $p + '.tmp'
    $Meta | ConvertTo-Json -Compress | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $p -Force
}

function Read-RmDatasetMeta {
    $p = Get-RmDatasetMetaPath
    if (-not (Test-Path -LiteralPath $p)) { return $null }
    try { Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch {
        # A corrupt meta must never kill queries — fall back to the legacy CMS
        # dataset resolution below.
        Write-Warning "Dataset metadata was unreadable and will be ignored: $($_.Exception.Message)"
        $null
    }
}

function Get-RmDatasetInfo {
    <#
    .SYNOPSIS
      Resolves the ACTIVE shared-patient dataset: its path, source
      ('cms-pspp' or 'hop-teaming'), year, engine format id, and a
      display label. Falls back to the legacy CMS path (no metadata file)
      so installs that predate Hop Teaming support keep working.
    #>
    $meta = Read-RmDatasetMeta
    if ($null -ne $meta) {
        $file = [string](Get-RmProp $meta 'FileName')
        $path = if ($file) { Join-Path $script:RmConfig.DataDir $file } else { $null }
        if ($path -and (Test-Path -LiteralPath $path)) {
            $src  = [string](Get-RmProp $meta 'Source')
            $year = [int](Get-RmProp $meta 'Year')
            $isHop = $src -eq 'hop-teaming'
            return [pscustomobject]@{
                Ready  = $true
                Source = $src
                Year   = $year
                Path   = $path
                Format = if ($isHop) { [RmEngine]::FormatHopTeaming } else { [RmEngine]::FormatCms }
                Rows   = [long](Get-RmProp $meta 'RowCount')
                Label  = if ($isHop) { "DocGraph Hop Teaming $year (CareSet)" }
                         else { "CMS shared-patient $year/$([int](Get-RmProp $meta 'Interval'))-day" }
            }
        }
        # Meta points at a file that no longer exists — fall through.
    }
    # Legacy resolution: the configured CMS dataset file, if present.
    $legacy = Get-RmDatasetPath
    [pscustomobject]@{
        Ready  = (Test-Path -LiteralPath $legacy)
        Source = 'cms-pspp'
        Year   = [int]$script:RmConfig.Year
        Path   = $legacy
        Format = [RmEngine]::FormatCms
        Rows   = 0
        Label  = 'CMS shared-patient {0}/{1}-day' -f $script:RmConfig.Year, $script:RmConfig.Interval
    }
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
        # Re-activate the CMS dataset (a Hop Teaming import may be active) —
        # clicking "Download CMS dataset" is also how the user switches back.
        Write-RmDatasetMeta @{
            Source = 'cms-pspp'; Year = $Year; Interval = $Interval
            FileName = (Split-Path -Leaf $target)
            RowCount = 0; ActivatedAt = (Get-Date).ToString('s')
        }
        return [pscustomobject]@{
            Downloaded = $false; Path = $target
            Message = "Dataset $Year/${Interval}-day already present (now the active dataset)."
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
        # Extract via the .NET API, NOT Expand-Archive: on Windows PowerShell 5.1
        # Expand-Archive rejects any file whose extension is not literally .zip
        # (our temp ends in .zip.tmp), whereas ZipFile is extension-agnostic and
        # works identically on 5.1 and 7.
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
        [System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $extractDir)
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
    try { Set-Content -LiteralPath ($target + '.rows') -Value $rows -Encoding ascii } catch { }
    Write-RmDatasetMeta @{
        Source = 'cms-pspp'; Year = $Year; Interval = $Interval
        FileName = (Split-Path -Leaf $target)
        RowCount = $rows; ActivatedAt = (Get-Date).ToString('s')
    }
    [pscustomobject]@{
        Downloaded = $true; Path = $target; RowCount = $rows
        Message = "Downloaded CMS shared-patient data $Year/${Interval}-day: $('{0:N0}' -f $rows) provider pairs."
    }
}

function Get-RmAvailableDatasets {
    <#
    .SYNOPSIS
      Lists every shared-patient dataset present on disk — downloaded CMS
      files and imported CareSet Hop Teaming files — and marks the active one.
    #>
    $d = $script:RmConfig.DataDir
    if (-not (Test-Path -LiteralPath $d)) { return @() }
    $active = Get-RmDatasetInfo
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($f in @(Get-ChildItem -LiteralPath $d -File -ErrorAction SilentlyContinue)) {
        $src = $null; $year = 0; $interval = 0
        if ($f.Name -match '^pspp_(\d{4})_days(\d+)\.txt$') {
            $src = 'cms-pspp'; $year = [int]$Matches[1]; $interval = [int]$Matches[2]
        } elseif ($f.Name -match '^hop_teaming_(\d{4})\.csv$') {
            $src = 'hop-teaming'; $year = [int]$Matches[1]
        } else { continue }
        $rows = [long]0
        $rowsFile = $f.FullName + '.rows'
        if (Test-Path -LiteralPath $rowsFile) {
            try { $rows = [long](Get-Content -LiteralPath $rowsFile -First 1) } catch { $rows = 0 }
        }
        $out.Add([pscustomobject]@{
            Source   = $src
            Year     = $year
            Interval = $interval
            Path     = $f.FullName
            Bytes    = $f.Length
            RowCount = $rows
            Label    = if ($src -eq 'hop-teaming') { "DocGraph Hop Teaming $year (CareSet)" }
                       else { "CMS shared-patient $year/${interval}-day" }
            Active   = ($active.Ready -and $active.Path -eq $f.FullName)
        })
    }
    @($out | Sort-Object -Property @{Expression = 'Year'; Descending = $true},
                                   @{Expression = 'Source'; Descending = $false})
}

function Set-RmActiveDataset {
    <#
    .SYNOPSIS
      Switches which on-disk dataset the referral queries use, without any
      re-download or re-import. Pick one returned by Get-RmAvailableDatasets.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet('cms-pspp', 'hop-teaming')][string]$Source,
        [Parameter(Mandatory)][ValidateRange(2009, 2100)][int]$Year,
        [int]$Interval = $script:RmConfig.Interval
    )
    $file = if ($Source -eq 'hop-teaming') { 'hop_teaming_{0}.csv' -f $Year }
            else { 'pspp_{0}_days{1}.txt' -f $Year, $Interval }
    $path = Join-Path $script:RmConfig.DataDir $file
    if (-not (Test-Path -LiteralPath $path)) {
        throw "No $Source dataset for $Year is on disk ($file). Download or import it first."
    }
    $rows = [long]0
    if (Test-Path -LiteralPath ($path + '.rows')) {
        try { $rows = [long](Get-Content -LiteralPath ($path + '.rows') -First 1) } catch { $rows = 0 }
    }
    $meta = @{
        Source = $Source; Year = $Year; FileName = $file
        RowCount = $rows; ActivatedAt = (Get-Date).ToString('s')
    }
    if ($Source -eq 'cms-pspp') { $meta.Interval = $Interval }
    Write-RmDatasetMeta $meta
    [pscustomobject]@{
        Activated = $true; Source = $Source; Year = $Year; Path = $path
        Message = "Active referral dataset is now $((Get-RmDatasetInfo).Label)."
    }
}

function Import-RmDataset {
    <#
    .SYNOPSIS
      Imports a DocGraph Hop Teaming dataset (CareSet Systems) from a local
      file the user obtained from CareSet — either the delivery .zip or the
      extracted .csv. Validates the format, installs it into the data dir
      atomically, and makes it the ACTIVE dataset for all referral queries.
    .PARAMETER Path
      The CareSet file: DocGraph_<year>_....zip or DocGraph_Hop_Teaming_<year>.csv.
    .PARAMETER Year
      The data year. Usually detected from the file name; required if the
      file name contains no recognizable year.
    .NOTES
      The Hop Teaming CSV is large (the 2022 file is ~8 GB extracted, ~210M
      rows), so the import streams and never loads the file into memory.
      A failed import never damages the currently active dataset.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [ValidateRange(2009, 2100)][int]$Year
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "The file '$Path' does not exist. Pick the .zip you downloaded from CareSet (or the .csv inside it)."
    }
    Initialize-RmDataDir | Out-Null
    $ProgressPreference = 'SilentlyContinue'
    Clear-RmStaleTemp
    $runId = [guid]::NewGuid().ToString('N')
    $ext = [System.IO.Path]::GetExtension($Path).ToLowerInvariant()
    $csvTmp = $null          # temp copy this function owns and may delete
    $csvSource = $null       # the CSV to validate (may be the user's own file)
    $originalName = Split-Path -Leaf $Path

    try {
        if ($ext -eq '.zip') {
            # Extract ONLY the Hop Teaming CSV entry — the zip also carries
            # docs and macOS cruft, and the CSV alone can be ~8 GB.
            Assert-RmSafeZip $Path
            Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
            $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
            try {
                $candidates = @($zip.Entries | Where-Object {
                    $_.Name -match '\.csv$' -and $_.FullName -notmatch '(^|/)__MACOSX/' -and
                    $_.Name -notmatch '^\.'
                })
                $entry = @($candidates | Where-Object { $_.Name -match '(?i)hop[_ ]?teaming' })
                if ($entry.Count -eq 0) { $entry = @($candidates | Sort-Object Length -Descending) }
                if ($entry.Count -eq 0) {
                    throw "No .csv file found inside '$originalName' — this does not look like a CareSet DocGraph delivery zip."
                }
                $originalName = $entry[0].Name
                $csvTmp = Join-Path $script:RmConfig.DataDir ("import_{0}.csv.tmp" -f $runId)
                Write-Verbose "Extracting '$($entry[0].FullName)' ($([math]::Round($entry[0].Length/1GB,1)) GB)..."
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry[0], $csvTmp, $true)
            } finally { $zip.Dispose() }
            $csvSource = $csvTmp
        } elseif ($ext -in @('.csv', '.txt')) {
            $csvSource = $Path
        } else {
            throw "Unsupported file type '$ext'. Pick the CareSet .zip or the extracted .csv."
        }

        # Validate the format FIRST (before the year check): a user who picked
        # the wrong file entirely should hear "this is not a Hop Teaming file",
        # not a confusing complaint about the file name's missing year.
        $probe = [System.IO.File]::OpenText($csvSource)
        try {
            $header = $probe.ReadLine()
            if ($null -eq $header) { throw "The file is empty." }
            $cols = @(($header -replace "^\uFEFF", '').Trim().ToLowerInvariant() -split ',')
            $expected = @('from_npi', 'to_npi', 'patient_count', 'transaction_count',
                          'average_day_wait', 'std_day_wait')
            if (@(Compare-Object $cols $expected -SyncWindow 0).Count -ne 0) {
                throw ("The file's header is '$header' — expected '$($expected -join ',')'. " +
                       "This does not look like a DocGraph Hop Teaming file. " +
                       "(CMS shared-patient files are added with the Download button instead.)")
            }
            for ($i = 0; $i -lt 3; $i++) {
                $line = $probe.ReadLine()
                if ($null -eq $line) { break }   # tiny files are fine
                $f = @($line -split ',')
                if ($f.Count -ne 6 -or $f[0] -notmatch '^\d{10}$' -or $f[1] -notmatch '^\d{10}$') {
                    throw "Data line $($i+2) ('$line') does not look like a Hop Teaming row (from_npi,to_npi,counts...)."
                }
            }
        } finally { $probe.Close() }

        # Detect the year from the file name unless given explicitly.
        if (-not $PSBoundParameters.ContainsKey('Year')) {
            if ($originalName -match '(20\d{2})') { $Year = [int]$Matches[1] }
            else {
                throw ("Could not tell the data year from the file name '$originalName'. " +
                       "Re-run with -Year (e.g. Import-RmDataset -Path ... -Year 2022).")
            }
        }

        # Promote atomically. If the source is the user's own .csv, COPY it
        # (never move a file the user gave us); a zip extraction temp is ours
        # to move.
        $target = Join-Path $script:RmConfig.DataDir ('hop_teaming_{0}.csv' -f $Year)
        if ($csvSource -eq $csvTmp) {
            Move-Item -LiteralPath $csvTmp -Destination $target -Force
            $csvTmp = $null
        } else {
            $copyTmp = Join-Path $script:RmConfig.DataDir ("import_{0}.copy.tmp" -f $runId)
            Copy-Item -LiteralPath $csvSource -Destination $copyTmp -Force
            Move-Item -LiteralPath $copyTmp -Destination $target -Force
        }

        $lines = [RmEngine]::CountLines($target)
        $rows = [long]([math]::Max(0, $lines - 1))   # minus the header row
        # Cache the row count beside the file so a later dataset SWITCH does
        # not have to re-count 200M+ lines (cosmetic if it goes missing).
        try { Set-Content -LiteralPath ($target + '.rows') -Value $rows -Encoding ascii } catch { }
        Write-RmDatasetMeta @{
            Source = 'hop-teaming'; Year = $Year
            FileName = (Split-Path -Leaf $target)
            RowCount = $rows; OriginalFile = $originalName
            ActivatedAt = (Get-Date).ToString('s')
        }
        [pscustomobject]@{
            Imported = $true; Path = $target; Year = $Year; RowCount = $rows
            Message = "Imported DocGraph Hop Teaming $Year (CareSet): $('{0:N0}' -f $rows) provider pairs. It is now the active referral dataset."
        }
    } catch {
        throw ("Importing the CareSet dataset failed; the active dataset was not changed. " +
               "Details: $($_.Exception.Message)")
    } finally {
        if ($csvTmp -and (Test-Path -LiteralPath $csvTmp)) {
            Remove-Item -LiteralPath $csvTmp -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-RmStatus {
    <# .SYNOPSIS Shows the ACTIVE shared-patient dataset (CMS download or
       imported CareSet Hop Teaming file). #>
    $info = Get-RmDatasetInfo
    [pscustomobject]@{
        DataDir      = $script:RmConfig.DataDir
        Source       = $info.Source
        Year         = $info.Year
        Interval     = $script:RmConfig.Interval   # CMS download setting only
        Label        = $info.Label
        DatasetPath  = $info.Path
        DatasetReady = $info.Ready
        DatasetBytes = if ($info.Ready) { (Get-Item -LiteralPath $info.Path).Length } else { 0 }
        RowCount     = $info.Rows
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
    .PARAMETER RequireZip
      Treat cached entries that predate the Zip field as misses so they are
      re-fetched with their practice ZIP (the geography map needs it). Old
      caches upgrade in place; nothing is thrown away.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Npi, [switch]$RequireZip)

    $cache = Read-RmNppesCache
    $result = @{}
    # Only look up well-formed NPIs — the source NPIs come from an external data
    # file and must never be concatenated into a URL or cache key unvalidated.
    $missing = @($Npi | Sort-Object -Unique |
        Where-Object { (Test-RmNpiShape $_) -and (
            -not $cache.ContainsKey($_) -or
            ($RequireZip -and $null -eq $cache[$_].PSObject.Properties['Zip'])) })
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
                    Name = '(NPI deactivated or not found)'; Specialty = ''; City = ''; State = ''; Zip = '' }
            } else {
                $r = $results[0]
                $basic = Get-RmProp $r 'basic'
                $isOrg = ((Get-RmProp $r 'enumeration_type') -eq 'NPI-2')
                $name = if ($isOrg) { [string](Get-RmProp $basic 'organization_name') }
                        else { ('{0} {1}' -f (Get-RmProp $basic 'first_name'), (Get-RmProp $basic 'last_name')).Trim() }
                $primary = @(@(Get-RmProp $r 'taxonomies') | Where-Object { (Get-RmProp $_ 'primary') -eq $true })
                $loc = @(@(Get-RmProp $r 'addresses') | Where-Object { (Get-RmProp $_ 'address_purpose') -eq 'LOCATION' })
                $postal = if ($loc.Count) { [string](Get-RmProp $loc[0] 'postal_code') } else { '' }
                $cache[$id] = [pscustomobject]@{
                    Name      = $name
                    Specialty = if ($primary.Count) { [string](Get-RmProp $primary[0] 'desc') } else { '' }
                    City      = if ($loc.Count) { [string](Get-RmProp $loc[0] 'city') } else { '' }
                    State     = if ($loc.Count) { [string](Get-RmProp $loc[0] 'state') } else { '' }
                    Zip       = if ($postal.Length -ge 5) { $postal.Substring(0, 5) } else { $postal }
                }
            }
        } catch {
            # Leave uncached so a later run can retry; report honestly for now.
            $consecutiveFailures++
            $result[$id] = [pscustomobject]@{ Name = '(lookup failed)'; Specialty = ''; City = ''; State = ''; Zip = '' }
        }
    }
    if ($missing.Count -gt 0) { Write-RmNppesCache $cache }
    foreach ($id in $Npi) {
        if (-not $result.ContainsKey($id)) {
            $result[$id] = if ($cache.ContainsKey($id)) { $cache[$id] }
                           else { [pscustomobject]@{ Name = '(lookup failed)'; Specialty = ''; City = ''; State = ''; Zip = '' } }
        }
    }
    $result
}

# ---------------------------------------------------------------------------
# The main query
# ---------------------------------------------------------------------------

# The honesty contract in text form: methodology/limitation notes for the
# ACTIVE dataset, used by the map result and every export sidecar. Branches by
# source because the two files were built differently and mislead differently.
function Get-RmMethodologyNotes {
    param([Parameter(Mandatory)]$Info, [switch]$OrganizationsOnly)
    $y = $Info.Year
    $clinicTaxNote = 'Clinic list = NPPES providers with a practice location in the requested ZIP holding taxonomies: ' +
        (@($script:RmClinicTaxonomies.Values) + $(if (-not $OrganizationsOnly) { @($script:RmIndividualTaxonomies.Values) } else { @() }) -join ', ') + '.'
    if ($Info.Source -eq 'hop-teaming') {
        @(
            "Source: DocGraph Hop Teaming $y, produced by CareSet Systems from 100% of Medicare Fee-for-Service Part A and Part B claims (data 'DocGraph' from CareSet; CC BY-NC-SA 4.0 non-commercial license unless you hold a commercial license from CareSet)."
            "The file covers services from $y-01-01 to $y-12-31. It shows the structure of the referral market in $y, NOT this year's volumes."
            'SharedPatients in the SOURCES table = patient_count: distinct Medicare FFS patients who saw the source provider and then that clinic (a directed shared-patient "hop"; a referral proxy, not billed referrals). SharedEvents = transaction_count: total from->to switches, so one patient bouncing back and forth counts each time.'
            'SharedPatients in the CLINICS table = the SUM of those per-source counts, NOT a unique-patient total: a patient sent by three sources is counted three times. Treat it as relative referral VOLUME, not a headcount of distinct patients.'
            "Pairs sharing fewer than 11 distinct patients in $y are excluded per CMS privacy policy, so low-volume referrers are invisible."
            'AvgDayWait = average days from the source visit to the clinic visit. Short waits (days-weeks) look like referrals; waits of months look like loosely-related care. Direction is claims sequence, not a literal referral: CareSet notes a "referee" can appear to send patients to their "referrer". Judge pairs by specialty — an orthopedic surgeon feeding a PT is referral-like; a lab is not.'
            'Medicare FFS only: Medicare Advantage (Part C), Medicaid, and commercial/employer plans are NOT included — in most markets that is a large share of patients, so these volumes understate total flow. Pediatric/OB specialties barely appear.'
            $clinicTaxNote
            'NPPES reflects providers and addresses as of TODAY. Providers whose NPI was issued after the data year are flagged in ExistedInDataYear — their zero counts mean "did not exist yet", not "no referrals". Clinics that moved or re-enumerated since the data year can also show zero; a ZIP prefix search (e.g. 630*) widens the net.'
            'Unlike the 2015 CMS file, ORGANIZATION NPIs (including private-practice LLCs) do appear in Hop Teaming alongside individual therapists — verified on real 2022 data where rehab-practice org NPIs were top recipients. A clinic''s volume can still be SPLIT between its organization NPI and its therapists'' individual NPIs; the ZIP sweep includes both automatically, but per-provider numbers may understate a practice''s total.'
        )
    } else {
        @(
            ("Source: CMS Physician Shared Patient Patterns (FOIA release), year {0}, {1}-day interval." -f
                $y, $script:RmConfig.Interval)
            $(if ($y -eq 2015) { 'The 2015 file covers claims from 2015-01-01 to 2015-09-01 — the NEWEST free public release of this data (newer years exist as DocGraph Hop Teaming files from CareSet, which this app can import). It shows the historical structure of the referral market, NOT current volumes.' }
              else { 'This is historical data; it shows the structure of the referral market at that time, NOT current volumes.' })
            'SharedPatients in the SOURCES table = unique Medicare beneficiaries that source provider shared with that one clinic within the interval window (CMS referral proxy; not billed referrals).'
            'SharedPatients in the CLINICS table = the SUM of those per-source counts, NOT a unique-patient total: a patient sent by three sources is counted three times. Treat it as relative referral VOLUME, not a headcount of distinct patients.'
            'Pairs sharing fewer than 11 patients within the file''s window are excluded by CMS, so low-volume referrers are invisible. (For 2015 that window is ~8 months, Jan–Sep, not a full year.)'
            'Same-day pairs are attributed by CMS to the LOWER NPI as the initiator. This scan only captures rows where the clinic is the SECOND provider, so same-day activity in which the clinic holds the lower NPI is not counted — the SameDay column is a partial, direction-ambiguous subset, useful only as a rough signal.'
            'Shared-patient pairs also capture co-occurring care — labs, imaging, and hospitals seen in the same window appear as "sources" without having referred anyone. Interpret sources by specialty: an orthopedic surgeon feeding a PT is referral-like; a lab is not.'
            $clinicTaxNote
            'NPPES reflects providers and addresses as of TODAY. Providers whose NPI was issued after the data year are flagged in ExistedInDataYear — their zero counts mean "did not exist yet", not "no referrals". Clinics that moved or re-enumerated since the data year can also show zero; a ZIP prefix search (e.g. 630*) widens the net.'
            'IMPORTANT: private-practice ORGANIZATION NPIs rarely appear in this file. CMS built the pairs from performing (rendering) provider NPIs on office claims and facility NPIs on institutional claims — a private clinic''s billing/group NPI is generally not included. Private practices therefore show up through their INDIVIDUAL therapists; hospital rehab departments show up as organizations. (Verified: 130 pre-2015 PT-chain org NPIs matched 0 rows, while a 244-therapist national sample matched 280 inbound rows.)'
        )
    }
}

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

    $info = Get-RmDatasetInfo
    if (-not $info.Ready) {
        throw ("No shared-patient dataset is available yet. Either click 'Download CMS dataset' " +
               "(free 2015 data, ~356 MB) or import a CareSet DocGraph Hop Teaming file " +
               "with 'Import CareSet file' / Import-RmDataset.")
    }
    $dataset = $info.Path
    $isHop = $info.Source -eq 'hop-teaming'

    Write-Verbose "Finding rehab providers in ZIP $Zip via NPPES..."
    $clinics = @(Find-RmClinic -Zip $Zip -OrganizationsOnly:$OrganizationsOnly)
    if ($clinics.Count -eq 0) {
        throw ("NPPES lists no outpatient rehab providers (PT/rehab clinics" +
               $(if (-not $OrganizationsOnly) { ", individual PT/OT/SLPs" }) +
               ") with a practice location in ZIP '$Zip'. Try a broader prefix like '$($Zip.Substring(0,3))*'.")
    }
    Write-Verbose "Found $($clinics.Count) rehab providers. Scanning $($info.Label)..."

    $targets = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($c in $clinics) { [void]$targets.Add($c.NPI) }
    $edges = [RmEngine]::ScanInbound($dataset, $targets, $info.Format)
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

    # Column set differs honestly by source: the CMS file has a SameDay count;
    # Hop Teaming instead has AvgDayWait (mean days from source visit to
    # clinic visit — small waits look like referrals, long ones like
    # co-occurring care).
    $sources = @($edges | Sort-Object BeneCount -Descending | ForEach-Object {
        $d = if ($detail.ContainsKey($_.SourceNpi)) { $detail[$_.SourceNpi] } else { $null }
        $clinic = $clinicByNpi[$_.TargetNpi]
        $row = [ordered]@{
            SourceNPI       = $_.SourceNpi
            SourceName      = if ($d) { $d.Name } else { '' }
            SourceSpecialty = if ($d) { $d.Specialty } else { '' }
            SourceCity      = if ($d) { $d.City } else { '' }
            SourceState     = if ($d) { $d.State } else { '' }
            ClinicNPI       = $_.TargetNpi
            ClinicName      = $clinic.Name
            SharedPatients  = $_.BeneCount
            SharedEvents    = $_.PairCount
        }
        if ($isHop) { $row['AvgDayWait'] = $_.AvgDayWait } else { $row['SameDay'] = $_.SameDayCount }
        [pscustomobject]$row
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
    # never a wrong Yes/No. The CMS 2015 file was cut off ~Sep 1, 2015; Hop
    # Teaming files span the full calendar year.
    $dataWindowEnd = Get-RmDataWindowEnd $info.Year $info.Source
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
        $row = [ordered]@{
            NPI             = $_.NPI
            Name            = $_.Name
            Type            = $_.Type
            Taxonomy        = $_.Taxonomy
            City            = $_.City
            State           = $_.State
            Zip             = $_.Zip
            ReferralSources = if ($agg) { $agg.Sources } else { 0 }
            SharedPatients  = if ($agg) { $agg.Benes } else { 0 }
        }
        if (-not $isHop) { $row['SameDay'] = if ($agg) { $agg.SameDay } else { 0 } }
        $row['ExistedInDataYear'] = $existed
        [pscustomobject]$row
    } | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                              @{Expression = 'Name'; Descending = $false})
    $notYetEnumerated = @($clinicRows | Where-Object { $_.ExistedInDataYear -like 'No*' }).Count

    $notes = @(Get-RmMethodologyNotes -Info $info -OrganizationsOnly:$OrganizationsOnly) + @(
        $(if ($notYetEnumerated -gt 0) { '{0} of {1} providers found in this ZIP were issued their NPI after the {2} file''s service window ended, so they cannot appear in it (ExistedInDataYear = No).' -f $notYetEnumerated, $clinicRows.Count, $info.Year })
        $(if ($enrichNote) { $enrichNote })
    ) | Where-Object { $_ }

    [pscustomobject]@{
        Zip     = $Zip
        Clinics = $clinicRows
        Sources = $sources
        Notes   = @($notes)
    }
}

function Get-RmInboundByBucket {
    <#
    .SYNOPSIS
      Rolls 2015 inbound shared-patient volume up to arbitrary buckets. Given a
      map of provider NPI -> bucket label (e.g. therapist NPI -> practice-group
      name), returns per-bucket total shared patients and top referral sources.
      This is the bridge that gives a PRACTICE GROUP a referral footprint by
      summing its member therapists' inbound volume.
    .PARAMETER TargetToBucket
      Hashtable: individual NPI (string) -> bucket label, OR an array of labels
      when one NPI belongs to several buckets (its volume is credited to each).
      Use a STABLE UNIQUE key as the label (e.g. a group PAC ID), not a display
      name — two distinct entities can share a name.
    .PARAMETER TopPerBucket
      How many top sources to keep and name per bucket (default 10).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][hashtable]$TargetToBucket,
        [int]$TopPerBucket = 10,
        [switch]$SkipEnrichment
    )
    $info = Get-RmDatasetInfo
    if (-not $info.Ready) {
        throw "No shared-patient dataset is available yet (Referral map tab: download the CMS dataset or import a CareSet file)."
    }
    $targets = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($k in $TargetToBucket.Keys) { [void]$targets.Add([string]$k) }
    if ($targets.Count -eq 0) { return @() }
    $edges = [RmEngine]::ScanInbound($info.Path, $targets, $info.Format)

    # bucket -> @{ Benes; Sources = @{ srcNpi -> benes } }. One target NPI may
    # map to several buckets (a therapist in more than one group); credit each.
    $buckets = @{}
    foreach ($e in $edges) {
        foreach ($label in @($TargetToBucket[$e.TargetNpi])) {
            if (-not $label) { continue }
            $lbl = [string]$label
            if (-not $buckets.ContainsKey($lbl)) {
                $buckets[$lbl] = [pscustomobject]@{ Benes = 0; Sources = @{} }
            }
            $b = $buckets[$lbl]
            $b.Benes += $e.BeneCount
            if (-not $b.Sources.ContainsKey($e.SourceNpi)) { $b.Sources[$e.SourceNpi] = 0 }
            $b.Sources[$e.SourceNpi] += $e.BeneCount
        }
    }

    # Enrich the union of top sources across buckets (cached). Capped so a
    # prefix search spanning many groups can't fire thousands of NPPES lookups;
    # any beyond the cap show NPI only (SharedPatients stays correct regardless).
    $topByBucket = @{}
    $needEnrich = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($label in $buckets.Keys) {
        $top = @($buckets[$label].Sources.GetEnumerator() | Sort-Object Value -Descending |
            Select-Object -First $TopPerBucket)
        $topByBucket[$label] = $top
        foreach ($t in $top) { [void]$needEnrich.Add($t.Key) }
    }
    $detail = @{}
    if (-not $SkipEnrichment -and $needEnrich.Count -gt 0) {
        $enrichList = @($needEnrich) | Select-Object -First $script:RmConfig.EnrichCap
        if (@($needEnrich).Count -gt @($enrichList).Count) {
            Write-Warning ("Naming the top sources for the top $($script:RmConfig.EnrichCap) of " +
                "$(@($needEnrich).Count) providers; the rest show NPI only.")
        }
        $detail = Get-RmProviderDetail -Npi @($enrichList)
    }

    foreach ($label in ($buckets.Keys | Sort-Object { -$buckets[$_].Benes })) {
        $top = foreach ($t in $topByBucket[$label]) {
            $d = if ($detail.ContainsKey($t.Key)) { $detail[$t.Key] } else { $null }
            [pscustomobject]@{
                SourceNPI       = $t.Key
                SourceName      = if ($d) { $d.Name } else { '' }
                SourceSpecialty = if ($d) { $d.Specialty } else { '' }
                SharedPatients  = $t.Value
            }
        }
        [pscustomobject]@{
            Bucket         = $label
            SharedPatients = $buckets[$label].Benes
            SourceCount    = $buckets[$label].Sources.Count
            TopSources     = @($top)
        }
    }
}

function Get-RmProviderReferralActivity {
    <#
    .SYNOPSIS
      Full 2015 referral activity for ONE provider NPI: inbound (who shared
      patients into them) and outbound (who they shared patients onward to),
      each ranked by volume and enriched with names/specialties.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidatePattern('^\d{10}$')][string]$Npi,
        [int]$Top = 25,
        [switch]$SkipEnrichment
    )
    $info = Get-RmDatasetInfo
    if (-not $info.Ready) {
        throw "No shared-patient dataset is available yet (Referral map tab: download the CMS dataset or import a CareSet file)."
    }
    $isHop = $info.Source -eq 'hop-teaming'
    $set = New-Object 'System.Collections.Generic.HashSet[string]'
    [void]$set.Add($Npi)
    $edges = [RmEngine]::ScanEither($info.Path, $set, $info.Format)

    $inbound  = @($edges | Where-Object { $_.TargetNpi -eq $Npi } | Sort-Object BeneCount -Descending)
    $outbound = @($edges | Where-Object { $_.SourceNpi -eq $Npi } | Sort-Object BeneCount -Descending)
    $inbound  = @($inbound  | Select-Object -First $Top)
    $outbound = @($outbound | Select-Object -First $Top)

    $others = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($e in $inbound)  { [void]$others.Add($e.SourceNpi) }
    foreach ($e in $outbound) { [void]$others.Add($e.TargetNpi) }
    $detail = @{}
    if (-not $SkipEnrichment -and $others.Count -gt 0) { $detail = Get-RmProviderDetail -Npi @($others) }

    $mk = {
        param($otherNpi, $e)
        $d = if ($detail.ContainsKey($otherNpi)) { $detail[$otherNpi] } else { $null }
        $row = [ordered]@{
            NPI            = $otherNpi
            Name           = if ($d) { $d.Name } else { '' }
            Specialty      = if ($d) { $d.Specialty } else { '' }
            SharedPatients = $e.BeneCount
        }
        if ($isHop) { $row['AvgDayWait'] = $e.AvgDayWait } else { $row['SameDay'] = $e.SameDayCount }
        [pscustomobject]$row
    }
    [pscustomobject]@{
        Npi      = $Npi
        Year     = $info.Year
        Source   = $info.Source
        Inbound  = @($inbound  | ForEach-Object { & $mk $_.SourceNpi $_ })
        Outbound = @($outbound | ForEach-Object { & $mk $_.TargetNpi $_ })
    }
}

function Get-RmProviderTrend {
    <#
    .SYNOPSIS
      Year-over-year referral trend for ONE provider NPI, scanned across every
      imported DocGraph Hop Teaming year on disk. Returns one row per year:
      inbound/outbound totals, distinct partners, and the top inbound sources.
    .NOTES
      Uses ONLY Hop Teaming years — they share one methodology (full calendar
      year, directed hops), so years are comparable. The CMS 2015 file is
      deliberately excluded: its ~8-month window and 30-day interval produce
      numbers on a different scale, and mixing them in would fake a trend.
      Each year is a full streaming scan of that year's file, so expect
      minutes per year.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidatePattern('^\d{10}$')][string]$Npi,
        [int]$TopSources = 3,
        [switch]$SkipEnrichment
    )
    $years = @(Get-RmAvailableDatasets | Where-Object { $_.Source -eq 'hop-teaming' } | Sort-Object Year)
    if ($years.Count -lt 2) {
        throw ("A trend needs at least TWO imported Hop Teaming years (found $($years.Count)). " +
               "Import more years with 'Import CareSet file' / Import-RmDataset.")
    }
    $set = New-Object 'System.Collections.Generic.HashSet[string]'
    [void]$set.Add($Npi)

    $rows = New-Object System.Collections.Generic.List[object]
    $allTopNpis = New-Object 'System.Collections.Generic.HashSet[string]'
    $perYearTop = @{}
    foreach ($y in $years) {
        Write-Verbose "Scanning $($y.Label)..."
        $edges = [RmEngine]::ScanEither($y.Path, $set, [RmEngine]::FormatHopTeaming)
        $inb  = @($edges | Where-Object { $_.TargetNpi -eq $Npi })
        $outb = @($edges | Where-Object { $_.SourceNpi -eq $Npi })
        $inbVol = 0; foreach ($e in $inb) { $inbVol += $e.BeneCount }
        $outVol = 0; foreach ($e in $outb) { $outVol += $e.BeneCount }
        $top = @($inb | Sort-Object BeneCount -Descending | Select-Object -First $TopSources)
        $perYearTop[$y.Year] = $top
        foreach ($t in $top) { [void]$allTopNpis.Add($t.SourceNpi) }
        $rows.Add([pscustomobject]@{
            Year            = $y.Year
            InboundSources  = $inb.Count
            InboundPatients = $inbVol
            OutboundTargets = $outb.Count
            OutboundPatients = $outVol
            TopSources      = ''   # filled after enrichment below
        })
    }

    # One enrichment pass across all years' top sources (cached on disk).
    $detail = @{}
    if (-not $SkipEnrichment -and $allTopNpis.Count -gt 0) {
        $detail = Get-RmProviderDetail -Npi @($allTopNpis)
    }
    foreach ($r in $rows) {
        $tops = foreach ($t in $perYearTop[$r.Year]) {
            $d = if ($detail.ContainsKey($t.SourceNpi)) { $detail[$t.SourceNpi] } else { $null }
            $nm = if ($d -and $d.Name) { $d.Name } else { $t.SourceNpi }
            "$nm ($($t.BeneCount))"
        }
        $r.TopSources = @($tops) -join '; '
    }

    $notes = @(
        "Source: DocGraph Hop Teaming years $(@($years | ForEach-Object { $_.Year }) -join ', '), produced by CareSet Systems from Medicare FFS Part A+B claims (data 'DocGraph' from CareSet)."
        'One row per data year. InboundPatients = sum of patient_count over every pair where this NPI was seen SECOND (patients shared INTO this provider); OutboundPatients = seen FIRST. A referral proxy, not billed referrals.'
        'Years are comparable to each other (same methodology, full calendar years), but pairs under 11 patients are excluded per CMS policy in EVERY year — a partner dropping to zero may just mean they fell under 11.'
        'Medicare FFS only: Medicare Advantage growth over these years shifts patients OUT of this data — a declining trend can reflect MA enrollment shift as well as lost referrals. Read direction by specialty; co-occurring care (labs, hospitals) appears too.'
        'The CMS 2015 FOIA file is intentionally excluded from trends: different window (~8 months) and methodology — its numbers are not on the same scale.'
    )
    # NOTE: built with explicit arrays, not @($rows) — the @() to-object-array
    # binder can throw a spurious 'Argument types do not match' on a generic
    # List at this call site (engine binder edge case, observed on pwsh 7.6).
    $yearArr = foreach ($y in $years) { $y.Year }
    [pscustomobject]@{
        Npi   = $Npi
        Years = [int[]]$yearArr
        Rows  = $rows.ToArray()
        Notes = $notes
    }
}

function Find-RmPractice {
    <#
    .SYNOPSIS
      Searches the live NPPES registry for a practice by name — organizations
      (clinic/group names) and individual providers (last name) — or looks up
      a pasted 10-digit NPI directly. Returns candidate rows for the user to
      pick from before benchmarking.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateLength(2, 100)][string]$Name,
        [ValidatePattern('^[A-Za-z]{2}$')][string]$State
    )
    $term = $Name.Trim()
    $stateQ = if ($State) { '&state=' + $State.ToUpperInvariant() } else { '' }
    $raw = New-Object System.Collections.Generic.List[object]
    if ($term -match '^\d{10}$') {
        $resp = Invoke-RmNppes ('number={0}' -f $term)
        foreach ($r in @(Get-RmProp $resp 'results')) { $raw.Add($r) }
    } else {
        # NPPES wildcard: trailing * needs 2+ leading characters. Two queries —
        # practices are org NPIs, but solo practices live under the owner's
        # individual NPI, so search both and merge.
        $enc = [uri]::EscapeDataString($term.TrimEnd('*')) + '*'
        foreach ($field in 'organization_name', 'last_name') {
            $resp = Invoke-RmNppes ('{0}={1}{2}&limit=50' -f $field, $enc, $stateQ)
            foreach ($r in @(Get-RmProp $resp 'results')) { $raw.Add($r) }
        }
    }
    $seen = New-Object 'System.Collections.Generic.HashSet[string]'
    $rows = foreach ($r in $raw) {
        $npi = [string](Get-RmProp $r 'number')
        if (-not (Test-RmNpiShape $npi) -or -not $seen.Add($npi)) { continue }
        $basic = Get-RmProp $r 'basic'
        $isOrg = ((Get-RmProp $r 'enumeration_type') -eq 'NPI-2')
        $loc = @(@(Get-RmProp $r 'addresses') | Where-Object { (Get-RmProp $_ 'address_purpose') -eq 'LOCATION' })
        $postal = if ($loc.Count) { [string](Get-RmProp $loc[0] 'postal_code') } else { '' }
        $primary = @(@(Get-RmProp $r 'taxonomies') | Where-Object { (Get-RmProp $_ 'primary') -eq $true })
        [pscustomobject]@{
            NPI       = $npi
            Name      = if ($isOrg) { [string](Get-RmProp $basic 'organization_name') }
                        else { ('{0} {1}' -f (Get-RmProp $basic 'first_name'), (Get-RmProp $basic 'last_name')).Trim() }
            Type      = if ($isOrg) { 'Organization' } else { 'Individual' }
            Specialty = if ($primary.Count) { [string](Get-RmProp $primary[0] 'desc') } else { '' }
            City      = if ($loc.Count) { [string](Get-RmProp $loc[0] 'city') } else { '' }
            State     = if ($loc.Count) { [string](Get-RmProp $loc[0] 'state') } else { '' }
            Zip       = if ($postal.Length -ge 5) { $postal.Substring(0, 5) } else { $postal }
        }
    }
    @($rows | Sort-Object -Property @{Expression = 'Type'; Descending = $false},
                                    @{Expression = 'Name'; Descending = $false})
}

function Get-RmPracticeBenchmark {
    <#
    .SYNOPSIS
      Benchmarks ONE practice (org NPI or individual therapist NPI) against
      every outpatient rehab provider in its region on the active dataset:
      rank by inbound shared-patient volume, share of the region's measured
      volume, and the region's top sources that feed COMPETITORS but not the
      practice ("missed sources").
    .PARAMETER Npi
      The practice to benchmark. Its practice-location ZIP (from NPPES)
      defines the region unless -Zip is given.
    .PARAMETER WiderArea
      Use the ZIP's 3-digit prefix (e.g. 630*) instead of the exact ZIP.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidatePattern('^\d{10}$')][string]$Npi,
        [ValidatePattern('^\d{3,5}\*?$')][string]$Zip,
        [switch]$WiderArea,
        [switch]$OrganizationsOnly,
        [switch]$SkipEnrichment
    )
    $info = Get-RmDatasetInfo
    if (-not $info.Ready) {
        throw ("No shared-patient dataset is available yet. Download the CMS dataset or " +
               "import a CareSet file on the Referral map tab first.")
    }
    $isHop = $info.Source -eq 'hop-teaming'

    # Resolve the practice from NPPES.
    $resp = Invoke-RmNppes ('number={0}' -f $Npi)
    $hits = @(Get-RmProp $resp 'results')
    if ($hits.Count -eq 0) {
        throw "NPI $Npi was not found in the NPPES registry (deactivated, or a typo)."
    }
    $r0 = $hits[0]
    $basic = Get-RmProp $r0 'basic'
    $isOrg = ((Get-RmProp $r0 'enumeration_type') -eq 'NPI-2')
    $pracName = if ($isOrg) { [string](Get-RmProp $basic 'organization_name') }
                else { ('{0} {1}' -f (Get-RmProp $basic 'first_name'), (Get-RmProp $basic 'last_name')).Trim() }
    $loc = @(@(Get-RmProp $r0 'addresses') | Where-Object { (Get-RmProp $_ 'address_purpose') -eq 'LOCATION' })
    $postal = if ($loc.Count) { [string](Get-RmProp $loc[0] 'postal_code') } else { '' }
    $zip5 = if ($postal.Length -ge 5) { $postal.Substring(0, 5) } else { '' }
    $primary = @(@(Get-RmProp $r0 'taxonomies') | Where-Object { (Get-RmProp $_ 'primary') -eq $true })
    $enumerated = [string](Get-RmProp $basic 'enumeration_date')

    $regionZip = if ($Zip) { $Zip }
                 elseif ($zip5 -match '^\d{5}$') { if ($WiderArea) { $zip5.Substring(0, 3) + '*' } else { $zip5 } }
                 else { throw "NPPES lists no usable practice-location ZIP for $Npi — pass -Zip explicitly." }

    # The regional map (competitors + their sources) on the active dataset.
    $map = Get-RmReferralMap -Zip $regionZip -OrganizationsOnly:$OrganizationsOnly -SkipEnrichment:$SkipEnrichment
    $clinics = @($map.Clinics)
    $sources = @($map.Sources)
    $addedManually = $false

    # A practice outside the rehab-taxonomy sweep (e.g. a multi-specialty
    # clinic, or org type excluded by -OrganizationsOnly) must still be
    # benchmarkable: scan its own inbound volume and slot it into the table.
    if (-not @($clinics | Where-Object { $_.NPI -eq $Npi })) {
        $addedManually = $true
        $set = New-Object 'System.Collections.Generic.HashSet[string]'
        [void]$set.Add($Npi)
        $own = @([RmEngine]::ScanInbound($info.Path, $set, $info.Format))
        $ownVol = 0; foreach ($e in $own) { $ownVol += $e.BeneCount }
        $row = [ordered]@{
            NPI = $Npi; Name = $pracName
            Type = if ($isOrg) { 'Organization' } else { 'Individual' }
            Taxonomy = if ($primary.Count) { [string](Get-RmProp $primary[0] 'desc') } else { '' }
            City = if ($loc.Count) { [string](Get-RmProp $loc[0] 'city') } else { '' }
            State = if ($loc.Count) { [string](Get-RmProp $loc[0] 'state') } else { '' }
            Zip = $zip5
            ReferralSources = $own.Count; SharedPatients = $ownVol
        }
        if (-not $isHop) { $row['SameDay'] = 0 }
        $row['ExistedInDataYear'] = ''
        $clinics = @($clinics) + @([pscustomobject]$row)
        foreach ($e in ($own | Sort-Object BeneCount -Descending)) {
            $srow = [ordered]@{
                SourceNPI = $e.SourceNpi; SourceName = ''; SourceSpecialty = ''
                SourceCity = ''; SourceState = ''
                ClinicNPI = $Npi; ClinicName = $pracName
                SharedPatients = $e.BeneCount; SharedEvents = $e.PairCount
            }
            if ($isHop) { $srow['AvgDayWait'] = $e.AvgDayWait } else { $srow['SameDay'] = $e.SameDayCount }
            $sources = @($sources) + @([pscustomobject]$srow)
        }
    }

    # Rank + share, with a visible marker column for the practice's row.
    $ranked = @($clinics | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                                                 @{Expression = 'Name'; Descending = $false})
    $rank = 0; $total = 0
    for ($i = 0; $i -lt $ranked.Count; $i++) {
        $total += [int]$ranked[$i].SharedPatients
        if ($ranked[$i].NPI -eq $Npi) { $rank = $i + 1 }
    }
    $mine = @($ranked | Where-Object { $_.NPI -eq $Npi })[0]
    $share = if ($total -gt 0) { [math]::Round(100.0 * $mine.SharedPatients / $total, 1) } else { 0 }
    $rankedOut = @($ranked | ForEach-Object {
        $row = [ordered]@{ You = if ($_.NPI -eq $Npi) { '>> YOU' } else { '' } }
        foreach ($p in $_.PSObject.Properties) { $row[$p.Name] = $p.Value }
        [pscustomobject]$row
    })

    # Missed sources: providers feeding competitors in this region with NO
    # measured pair into the practice. Ranked by their volume to competitors.
    $mySources = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($s in @($sources | Where-Object { $_.ClinicNPI -eq $Npi })) { [void]$mySources.Add($s.SourceNPI) }
    $bySource = @{}
    foreach ($s in @($sources | Where-Object { $_.ClinicNPI -ne $Npi })) {
        if ($s.SourceNPI -eq $Npi -or $mySources.Contains($s.SourceNPI)) { continue }
        if (-not $bySource.ContainsKey($s.SourceNPI)) {
            $bySource[$s.SourceNPI] = [pscustomobject]@{
                SourceNPI = $s.SourceNPI; SourceName = $s.SourceName
                SourceSpecialty = $s.SourceSpecialty
                PatientsToCompetitors = 0
                Competitors = (New-Object 'System.Collections.Generic.HashSet[string]')
            }
        }
        $b = $bySource[$s.SourceNPI]
        $b.PatientsToCompetitors += [int]$s.SharedPatients
        [void]$b.Competitors.Add($s.ClinicNPI)
    }
    $missed = @($bySource.Values | ForEach-Object {
        [pscustomobject]@{
            SourceNPI             = $_.SourceNPI
            SourceName            = $_.SourceName
            SourceSpecialty       = $_.SourceSpecialty
            PatientsToCompetitors = $_.PatientsToCompetitors
            CompetitorsFed        = $_.Competitors.Count
        }
    } | Sort-Object -Property @{Expression = 'PatientsToCompetitors'; Descending = $true},
                              @{Expression = 'SourceNPI'; Descending = $false} |
        Select-Object -First 50)

    $notes = @($map.Notes) + @(
        ''
        "BENCHMARK METHOD: rank and share compare NPI $Npi ($pracName) against the NPPES-listed outpatient rehab providers in '$regionZip' on $($info.Label)."
        'MarketSharePct = this practice''s inbound shared-patient volume as a share of the SUM across all listed providers. Because that sum counts a patient once per source relationship, treat it as share of measured referral VOLUME, not share of patients.'
        'A practice''s volume is often SPLIT between its organization NPI and its therapists'' individual NPIs. Benchmark the org NPI and the key therapists separately for the full picture; the Practice groups tab lists a group''s therapist roster.'
        'Missed sources = providers with a measured pair (11+ patients) into at least one competitor and NO measured pair into this practice. "Missed" can also mean the pair exists but fell under the 11-patient privacy floor.'
        $(if ($addedManually) { "NOTE: NPI $Npi did not match the rehab-clinic taxonomy sweep for '$regionZip' (different taxonomy or location); its row was added from a direct scan, and ExistedInDataYear is blank." })
    ) | Where-Object { $null -ne $_ }

    [pscustomobject]@{
        Npi            = $Npi
        Practice       = [pscustomobject]@{
            Name = $pracName; Type = if ($isOrg) { 'Organization' } else { 'Individual' }
            City = if ($loc.Count) { [string](Get-RmProp $loc[0] 'city') } else { '' }
            State = if ($loc.Count) { [string](Get-RmProp $loc[0] 'state') } else { '' }
            Zip = $zip5; Enumerated = $enumerated
        }
        Zip            = $regionZip
        Year           = $info.Year
        Rank           = $rank
        OfTotal        = $ranked.Count
        InboundPatients = [int]$mine.SharedPatients
        ReferralSources = [int]$mine.ReferralSources
        MarketSharePct = $share
        Clinics        = $rankedOut
        MissedSources  = $missed
        Notes          = @($notes)
    }
}

# ---------------------------------------------------------------------------
# Referral geography (heat map)
# ---------------------------------------------------------------------------

# ZIP -> lat/lon centroids (US Census 2023 ZCTA gazetteer, public domain),
# shipped beside the module (~34k rows) and loaded once per process.
$script:RmCentroids = $null
function Get-RmCentroids([string]$CentroidPath) {
    if ($null -ne $script:RmCentroids -and -not $CentroidPath) { return $script:RmCentroids }
    $path = if ($CentroidPath) { $CentroidPath } else { Join-Path $PSScriptRoot 'zcta-centroids.csv' }
    $map = @{}
    if (Test-Path -LiteralPath $path) {
        $reader = New-Object System.IO.StreamReader($path)
        try {
            [void]$reader.ReadLine()   # header
            while ($null -ne ($line = $reader.ReadLine())) {
                $f = $line.Split(',')
                if ($f.Length -eq 3) { $map[$f[0]] = @([double]$f[1], [double]$f[2]) }
            }
        } finally { $reader.Dispose() }
    } else {
        Write-Warning "ZIP centroid table not found at '$path' — the density table still works, but nothing can be drawn on a map."
    }
    if (-not $CentroidPath) { $script:RmCentroids = $map }
    $map
}

function Get-RmMilesBetween([double]$Lat1, [double]$Lon1, [double]$Lat2, [double]$Lon2) {
    # Haversine, radius in statute miles.
    $rad = [math]::PI / 180
    $dLat = ($Lat2 - $Lat1) * $rad
    $dLon = ($Lon2 - $Lon1) * $rad
    $a = [math]::Sin($dLat / 2) * [math]::Sin($dLat / 2) +
         [math]::Cos($Lat1 * $rad) * [math]::Cos($Lat2 * $rad) *
         [math]::Sin($dLon / 2) * [math]::Sin($dLon / 2)
    [math]::Round(3958.8 * 2 * [math]::Atan2([math]::Sqrt($a), [math]::Sqrt(1 - $a)), 1)
}

function Get-RmReferralGeography {
    <#
    .SYNOPSIS
      For ONE provider/practice NPI: where its inbound referrals come from,
      geographically. Scans ALL of the NPI's inbound pairs on the active
      dataset, locates each source provider's practice ZIP via NPPES, and
      aggregates patient volume per ZIP with distance from the practice.
      Feed the result to Export-RmReferralMapHtml for an interactive map.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidatePattern('^\d{10}$')][string]$Npi,
        [string]$CentroidPath
    )
    $info = Get-RmDatasetInfo
    if (-not $info.Ready) {
        throw "No shared-patient dataset is available yet (Referral map tab: download the CMS dataset or import a CareSet file)."
    }
    $centroids = Get-RmCentroids $CentroidPath

    # The practice itself (map anchor).
    $resp = Invoke-RmNppes ('number={0}' -f $Npi)
    $hits = @(Get-RmProp $resp 'results')
    if ($hits.Count -eq 0) { throw "NPI $Npi was not found in the NPPES registry (deactivated, or a typo)." }
    $r0 = $hits[0]
    $basic = Get-RmProp $r0 'basic'
    $isOrg = ((Get-RmProp $r0 'enumeration_type') -eq 'NPI-2')
    $pracName = if ($isOrg) { [string](Get-RmProp $basic 'organization_name') }
                else { ('{0} {1}' -f (Get-RmProp $basic 'first_name'), (Get-RmProp $basic 'last_name')).Trim() }
    $loc = @(@(Get-RmProp $r0 'addresses') | Where-Object { (Get-RmProp $_ 'address_purpose') -eq 'LOCATION' })
    $postal = if ($loc.Count) { [string](Get-RmProp $loc[0] 'postal_code') } else { '' }
    $pracZip = if ($postal.Length -ge 5) { $postal.Substring(0, 5) } else { '' }
    $pracLoc = if ($pracZip -and $centroids.ContainsKey($pracZip)) { $centroids[$pracZip] } else { $null }

    Write-Verbose "Scanning $($info.Label) for all inbound pairs of $Npi..."
    $set = New-Object 'System.Collections.Generic.HashSet[string]'
    [void]$set.Add($Npi)
    $edges = @([RmEngine]::ScanInbound($info.Path, $set, $info.Format))

    # Locate every source (cached; capped like the other NPPES-heavy paths).
    $srcNpis = @($edges | ForEach-Object { $_.SourceNpi } | Sort-Object -Unique)
    $capNote = $null
    if ($srcNpis.Count -gt $script:RmConfig.EnrichCap) {
        # Cap by VOLUME so the biggest feeders are always located.
        $volByNpi = @{}
        foreach ($e in $edges) {
            if (-not $volByNpi.ContainsKey($e.SourceNpi)) { $volByNpi[$e.SourceNpi] = 0 }
            $volByNpi[$e.SourceNpi] += $e.BeneCount
        }
        $keep = @($volByNpi.GetEnumerator() | Sort-Object Value -Descending |
            Select-Object -First $script:RmConfig.EnrichCap | ForEach-Object { $_.Key })
        $capNote = ("Located the top {0} of {1} distinct sources by volume; the rest are grouped " +
            "under '(not located)'. Raise the cap with Set-RmConfig -EnrichCap.") -f @($keep).Count, $srcNpis.Count
        Write-Warning $capNote
        $srcNpis = $keep
    }
    $detail = if ($srcNpis.Count -gt 0) { Get-RmProviderDetail -Npi @($srcNpis) -RequireZip } else { @{} }

    # Aggregate per source ZIP.
    $byZip = @{}
    $totalPatients = 0
    foreach ($e in $edges) {
        $totalPatients += $e.BeneCount
        $d = if ($detail.ContainsKey($e.SourceNpi)) { $detail[$e.SourceNpi] } else { $null }
        $zipRaw = if ($d -and $null -ne $d.PSObject.Properties['Zip']) { [string]$d.Zip } else { '' }
        $zip = if ($zipRaw -match '^\d{5}$') { $zipRaw } else { '(not located)' }
        if (-not $byZip.ContainsKey($zip)) {
            $byZip[$zip] = [pscustomobject]@{
                Patients = 0; Sources = 0; City = ''; State = ''
                TopName = ''; TopVol = 0
            }
        }
        $b = $byZip[$zip]
        $b.Patients += $e.BeneCount
        $b.Sources += 1
        if ($d -and -not $b.City -and $d.City) { $b.City = $d.City; $b.State = $d.State }
        $srcVol = $e.BeneCount
        if ($srcVol -gt $b.TopVol) {
            $b.TopVol = $srcVol
            $b.TopName = if ($d -and $d.Name) { $d.Name } else { $e.SourceNpi }
        }
    }

    $rows = foreach ($zip in $byZip.Keys) {
        $b = $byZip[$zip]
        $hasGeo = $centroids.ContainsKey($zip)
        $dist = if ($hasGeo -and $pracLoc) {
            Get-RmMilesBetween $pracLoc[0] $pracLoc[1] $centroids[$zip][0] $centroids[$zip][1]
        } else { $null }
        [pscustomobject]@{
            Zip            = $zip
            City           = $b.City
            State          = $b.State
            Sources        = $b.Sources
            SharedPatients = $b.Patients
            PctOfVolume    = if ($totalPatients -gt 0) { [math]::Round(100.0 * $b.Patients / $totalPatients, 1) } else { 0 }
            DistanceMiles  = if ($null -ne $dist) { $dist } else { '' }
            TopSource      = $b.TopName
            Lat            = if ($hasGeo) { $centroids[$zip][0] } else { $null }
            Lon            = if ($hasGeo) { $centroids[$zip][1] } else { $null }
        }
    }
    $rows = @($rows | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                                            @{Expression = 'Zip'; Descending = $false})
    $mappable = @($rows | Where-Object { $null -ne $_.Lat })
    $mappedPatients = 0; foreach ($m in $mappable) { $mappedPatients += $m.SharedPatients }

    $notes = @(Get-RmMethodologyNotes -Info $info) + @(
        ''
        "GEOGRAPHY METHOD: every inbound pair of NPI $Npi ($pracName) in $($info.Label), aggregated by each SOURCE provider's practice-location ZIP from today's NPPES registry."
        'Locations are TODAY''s NPPES practice addresses — a source that moved since the data year is drawn where it is now, and NPPES addresses are self-reported (sometimes stale, sometimes an administrative office rather than the clinic).'
        'Coordinates are US Census 2023 ZCTA centroids (ZIP-code areas approximate USPS ZIPs). Sources whose ZIP could not be resolved or mapped are grouped under ''(not located)'' in the table and are not drawn.'
        $(if ($capNote) { $capNote })
    ) | Where-Object { $null -ne $_ -and $_ -ne $false }

    [pscustomobject]@{
        Npi             = $Npi
        Practice        = [pscustomobject]@{
            Name = $pracName; Zip = $pracZip
            City = if ($loc.Count) { [string](Get-RmProp $loc[0] 'city') } else { '' }
            State = if ($loc.Count) { [string](Get-RmProp $loc[0] 'state') } else { '' }
            Lat = if ($pracLoc) { $pracLoc[0] } else { $null }
            Lon = if ($pracLoc) { $pracLoc[1] } else { $null }
        }
        Year            = $info.Year
        Label           = $info.Label
        Rows            = $rows
        TotalPatients   = $totalPatients
        MappedPatients  = $mappedPatients
        Notes           = @($notes)
    }
}

function Export-RmReferralMapHtml {
    <#
    .SYNOPSIS
      Writes a Get-RmReferralGeography result as a self-viewing HTML heat map:
      one circle per source ZIP, sized and colored by referral volume, with
      the practice starred. Open it in any browser (the base map tiles load
      from OpenStreetMap, so viewing needs an internet connection).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Geography,
        [Parameter(Mandatory)][string]$Path
    )
    $g = $Geography
    $points = @($g.Rows | Where-Object { $null -ne $_.Lat } | ForEach-Object {
        [ordered]@{
            z = $_.Zip; lat = [double]$_.Lat; lon = [double]$_.Lon
            p = [int]$_.SharedPatients; s = [int]$_.Sources
            city = [string]$_.City; st = [string]$_.State
            d = [string]$_.DistanceMiles; top = [string]$_.TopSource
            pct = [double]$_.PctOfVolume
        }
    })
    $pointsJson = ConvertTo-Json -InputObject @($points) -Compress -Depth 4
    $practiceJson = ConvertTo-Json -InputObject ([ordered]@{
        name = [string]$g.Practice.Name; zip = [string]$g.Practice.Zip
        city = [string]$g.Practice.City; st = [string]$g.Practice.State
        lat = $g.Practice.Lat; lon = $g.Practice.Lon
    }) -Compress
    $titleText = "Referral sources of $($g.Practice.Name) ($($g.Npi)) — $($g.Label)"
    $notMapped = $g.TotalPatients - $g.MappedPatients
    $totSources = 0; foreach ($row in @($g.Rows)) { $totSources += $row.Sources }
    $topShare = if (@($g.Rows).Count -gt 0) { [double]@($g.Rows)[0].PctOfVolume } else { 0 }
    $topZipLabel = if (@($g.Rows).Count -gt 0) {
        $t = @($g.Rows)[0]
        if ($t.Zip -eq '(not located)') { 'not located' }
        elseif ($t.City) { "$($t.Zip) ($($t.City))" } else { $t.Zip }
    } else { '—' }
    $generated = (Get-Date).ToString('MMMM d, yyyy')
    $notesHtml = (@($g.Notes) | Where-Object { $_ } | ForEach-Object {
        '<li>' + ([System.Net.WebUtility]::HtmlEncode([string]$_)) + '</li>' }) -join "`n"
    $tableRows = (@($g.Rows) | Select-Object -First 30 | ForEach-Object {
        '<tr><td class="mono">{0}</td><td>{1}</td><td>{2}</td><td class="num">{3}</td><td class="num">{4}</td><td class="num">{5}%</td><td class="num">{6}</td><td>{7}</td></tr>' -f
            [System.Net.WebUtility]::HtmlEncode([string]$_.Zip),
            [System.Net.WebUtility]::HtmlEncode([string]$_.City),
            [System.Net.WebUtility]::HtmlEncode([string]$_.State),
            ('{0:N0}' -f $_.Sources), ('{0:N0}' -f $_.SharedPatients), $_.PctOfVolume, $_.DistanceMiles,
            [System.Net.WebUtility]::HtmlEncode([string]$_.TopSource) }) -join "`n"
    $tableNote = if (@($g.Rows).Count -gt 30) {
        "Showing the top 30 of $(@($g.Rows).Count) ZIP groups — the full table is in the CSV saved beside this map."
    } else { '' }

    # Leaflet is BUNDLED with the app and inlined here so the saved map is one
    # self-contained file (email-able; no CDN dependency). Only the background
    # tiles need internet. Inserted by LITERAL replacement after the
    # here-string expands — 147 KB of minified JS must never go through
    # PowerShell string interpolation. CDN fallback if the bundle is missing.
    $lfDir = Join-Path $PSScriptRoot 'leaflet'
    $lfJsPath = Join-Path $lfDir 'leaflet.min.js'
    $lfCssPath = Join-Path $lfDir 'leaflet.css'
    if ((Test-Path -LiteralPath $lfJsPath) -and (Test-Path -LiteralPath $lfCssPath)) {
        $cssBlock = '<style>' + [System.IO.File]::ReadAllText($lfCssPath) + '</style>'
        $jsBlock = '<script>' + [System.IO.File]::ReadAllText($lfJsPath) + '</script>'
    } else {
        $cssBlock = '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>'
        $jsBlock = '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>'
    }

    $html = @"
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>$([System.Net.WebUtility]::HtmlEncode($titleText))</title>
__LEAFLET_CSS_BLOCK__
__LEAFLET_JS_BLOCK__
<style>
  :root { --ink:#1c2b3a; --sub:#5b6b7a; --line:#dde3e9; --accent:#2c5f8a; }
  * { box-sizing:border-box; }
  body { margin:0; background:#eef1f4; color:var(--ink);
         font-family:"Segoe UI", -apple-system, "Helvetica Neue", Arial, sans-serif; }
  .wrap { max-width:1180px; margin:0 auto; padding:18px 20px 30px; }
  header { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 14px; margin-bottom:4px; }
  header h1 { margin:0; font-size:21px; font-weight:600; letter-spacing:-.2px; }
  .badge { background:var(--accent); color:#fff; font-size:11.5px; font-weight:600;
           padding:3px 9px; border-radius:99px; white-space:nowrap; }
  .sub { color:var(--sub); font-size:13px; margin:2px 0 14px; }
  .stats { display:flex; flex-wrap:wrap; gap:12px; margin:0 0 14px; }
  .stat { background:#fff; border:1px solid var(--line); border-radius:8px;
          padding:10px 16px; min-width:150px; box-shadow:0 1px 2px rgba(16,32,48,.05); }
  .stat b { display:block; font-size:20px; font-weight:650; letter-spacing:-.3px; }
  .stat span { font-size:11.5px; color:var(--sub); text-transform:uppercase; letter-spacing:.4px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:8px;
          box-shadow:0 1px 2px rgba(16,32,48,.05); overflow:hidden; margin-bottom:16px; }
  #map { height:60vh; min-height:420px; }
  .offline { padding:9px 14px; background:#fff6da; color:#6b5619; font-size:12.5px;
             border-bottom:1px solid #eadfb6; display:none; }
  .card h2 { margin:0; padding:12px 16px 10px; font-size:14.5px; font-weight:600; }
  table { border-collapse:collapse; width:100%; font-size:12.8px; }
  th, td { border-top:1px solid var(--line); padding:6px 14px; text-align:left; }
  th { background:#f2f5f8; color:#33475c; font-weight:600; font-size:11.5px;
       text-transform:uppercase; letter-spacing:.4px; border-top:none; }
  tr:nth-child(even) td { background:#f8fafc; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  td.mono { font-variant-numeric:tabular-nums; }
  .tablenote { padding:8px 16px 12px; color:var(--sub); font-size:12px; }
  details { margin:2px 0 0; }
  summary { cursor:pointer; padding:12px 16px; font-size:14.5px; font-weight:600; }
  .notes { font-size:12px; color:#4a5a68; line-height:1.55; margin:0; padding:0 20px 14px 34px; }
  .notes li { margin-bottom:5px; }
  footer { color:var(--sub); font-size:11.5px; margin-top:6px; }
  .legend { background:#fff; padding:9px 12px; border-radius:6px;
            box-shadow:0 1px 5px rgba(0,0,0,.25); font-size:12px; line-height:19px; }
  .legend i { width:12px; height:12px; display:inline-block; border-radius:50%;
              margin-right:6px; vertical-align:-2px; }
  .prac-pin { width:22px; height:22px; border-radius:50%; background:#c62828;
              border:3px solid #fff; box-shadow:0 1px 6px rgba(0,0,0,.45); }
  @media print { #map { height:480px; } .badge { border:1px solid var(--accent); } }
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>$([System.Net.WebUtility]::HtmlEncode([string]$g.Practice.Name))</h1>
  <span class="badge">$([System.Net.WebUtility]::HtmlEncode([string]$g.Label))</span>
</header>
<p class="sub">Where this practice's Medicare referrals came from in $($g.Year) &mdash;
NPI $($g.Npi), $([System.Net.WebUtility]::HtmlEncode("$($g.Practice.City), $($g.Practice.State) $($g.Practice.Zip)")).
Circle size and color show referral volume from each source ZIP.</p>
<div class="stats">
  <div class="stat"><b>$('{0:N0}' -f $g.TotalPatients)</b><span>Shared patients</span></div>
  <div class="stat"><b>$('{0:N0}' -f $totSources)</b><span>Source relationships</span></div>
  <div class="stat"><b>$('{0:N0}' -f @($g.Rows).Count)</b><span>Source ZIP areas</span></div>
  <div class="stat"><b>$topShare%</b><span>From top ZIP ($([System.Net.WebUtility]::HtmlEncode($topZipLabel)))</span></div>
</div>
<div class="card">
  <div id="offline" class="offline">The background map could not load (no internet connection?). The circles, popups, and table below still work.</div>
  <div id="map"></div>
</div>
<div class="card">
  <h2>Top source ZIP areas</h2>
  <table>
    <tr><th>ZIP</th><th>City</th><th>St</th><th class="num">Sources</th><th class="num">Patients</th><th class="num">% of volume</th><th class="num">Miles</th><th>Top source in ZIP</th></tr>
    $tableRows
  </table>
  $(if ($tableNote) { "<div class='tablenote'>$tableNote</div>" })
</div>
<div class="card">
  <details>
    <summary>How to read this map (methodology &amp; limitations)</summary>
    <ul class="notes">
      $notesHtml
    </ul>
  </details>
</div>
<footer>Generated $generated by the Medicare Order &amp; Referring Tracker &middot;
$('{0:N0}' -f $g.MappedPatients) of $('{0:N0}' -f $g.TotalPatients) patients mappable$(if ($notMapped -gt 0) { " ($('{0:N0}' -f $notMapped) from sources without a locatable ZIP)" }) &middot;
Base map &copy; OpenStreetMap contributors &middot; ZIP centroids: US Census 2023 ZCTA gazetteer</footer>
</div>
<script>
var pts = $pointsJson;
var prac = $practiceJson;
// If the map library itself failed to load (no internet), say so instead of
// showing an empty white box — the stats and table above still stand alone.
if (typeof L === 'undefined') {
  document.getElementById('offline').style.display = 'block';
  document.getElementById('map').style.height = '0';
  throw new Error('Leaflet unavailable (offline)');
}
var maxP = 1; pts.forEach(function(p){ if (p.p > maxP) maxP = p.p; });
var center = (prac.lat !== null) ? [prac.lat, prac.lon]
           : (pts.length ? [pts[0].lat, pts[0].lon] : [39.5, -98.35]);
var map = L.map('map').setView(center, 10);
var tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  { maxZoom: 18, attribution: '&copy; OpenStreetMap contributors' });
tiles.on('tileerror', function(){ document.getElementById('offline').style.display='block'; });
tiles.addTo(map);
function color(v) {
  var t = Math.sqrt(v / maxP);           // sqrt so mid-size ZIPs stay visible
  var r = Math.round(43 + t * (215 - 43));
  var g = Math.round(131 + t * (25 - 131));
  var b = Math.round(186 + t * (28 - 186));
  return 'rgb(' + r + ',' + g + ',' + b + ')';
}
function radius(v) { return 6 + 30 * Math.sqrt(v / maxP); }
var group = [];
pts.forEach(function(p) {
  var c = L.circleMarker([p.lat, p.lon], {
    radius: radius(p.p), color: '#26333e', weight: 1,
    fillColor: color(p.p), fillOpacity: 0.72
  }).addTo(map);
  c.bindPopup('<b>ZIP ' + p.z + '</b> &mdash; ' + p.city + ', ' + p.st +
    '<br/>' + p.p.toLocaleString() + ' patients (' + p.pct + '% of volume) from ' + p.s + ' source(s)' +
    (p.d ? '<br/>' + p.d + ' miles from the practice' : '') +
    (p.top ? '<br/>Top source: ' + p.top : ''));
  group.push(c);
});
if (prac.lat !== null) {
  var star = L.marker([prac.lat, prac.lon], { title: prac.name,
    icon: L.divIcon({ className: 'prac-pin', iconSize: [22, 22], iconAnchor: [11, 11] }) }).addTo(map);
  star.bindPopup('<b>' + prac.name + '</b><br/>' + prac.city + ', ' + prac.st + ' ' + prac.zip + '<br/>(the practice)');
  group.push(star);
}
// Frame the core market, not the outliers: fit the highest-volume ZIPs that
// cover ~90% of mapped volume (a single snowbird source 1,400 miles away
// must not zoom the whole map out to a national view). Outliers stay on the
// map — zoom out to see them; the table lists them regardless.
var fitPts = pts.slice().sort(function(a, b){ return b.p - a.p; });
var mappedTotal = 0; fitPts.forEach(function(p){ mappedTotal += p.p; });
var fitGroup = []; var acc = 0;
for (var i = 0; i < fitPts.length; i++) {
  fitGroup.push(L.latLng(fitPts[i].lat, fitPts[i].lon));
  acc += fitPts[i].p;
  if (acc >= mappedTotal * 0.9) break;
}
if (prac.lat !== null) { fitGroup.push(L.latLng(prac.lat, prac.lon)); }
if (fitGroup.length > 1) { map.fitBounds(L.latLngBounds(fitGroup).pad(0.18)); }
else if (group.length > 1) { map.fitBounds(L.featureGroup(group).getBounds().pad(0.15)); }
var legend = L.control({position:'bottomright'});
legend.onAdd = function() {
  var div = L.DomUtil.create('div', 'legend');
  div.innerHTML = '<b>Patients from ZIP</b><br/>' +
    '<i style="background:' + color(maxP) + '"></i>' + maxP.toLocaleString() + ' (max)<br/>' +
    '<i style="background:' + color(maxP/4) + '"></i>~' + Math.round(maxP/4).toLocaleString() + '<br/>' +
    '<i style="background:' + color(maxP/20) + '"></i>~' + Math.round(maxP/20).toLocaleString() + '<br/>' +
    'Marker = the practice';
  return div;
};
legend.addTo(map);
</script>
</body>
</html>
"@
    $html = $html.Replace('__LEAFLET_CSS_BLOCK__', $cssBlock).Replace('__LEAFLET_JS_BLOCK__', $jsBlock)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    # UTF-8 with BOM so browsers and Notepad agree about the encoding on WinPS 5.1 too.
    $enc = New-Object System.Text.UTF8Encoding($true)
    [System.IO.File]::WriteAllText(
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path), $html, $enc)
    [pscustomobject]@{ Path = $Path; Points = @($points).Count; TotalPatients = $g.TotalPatients }
}

function Get-RmSourceSpecialtyMix {
    <#
    .SYNOPSIS
      Rolls a set of referral-source rows up by SPECIALTY: per specialty, how
      many distinct sources and how much shared-patient volume, with each
      specialty's share of the total. Turns a long source list into an
      actionable referral-mix profile (e.g. "45% orthopedic surgery, 20%
      primary care"). Works on referral-map .Sources or Provider-360 inbound.
    .PARAMETER Rows
      The source rows.
    .PARAMETER SpecialtyField / VolumeField / IdField
      Property names to read (default to referral-map .Sources names).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Rows,
        [string]$SpecialtyField = 'SourceSpecialty',
        [string]$VolumeField = 'SharedPatients',
        [string]$IdField = 'SourceNPI'
    )
    $bySpec = @{}
    $total = 0
    foreach ($r in $Rows) {
        $spec = [string]$r.$SpecialtyField
        if ([string]::IsNullOrWhiteSpace($spec)) { $spec = '(specialty not looked up)' }
        $vol = [int]$r.$VolumeField
        $id  = [string]$r.$IdField
        if (-not $bySpec.ContainsKey($spec)) {
            $bySpec[$spec] = [pscustomobject]@{ Vol = 0; Ids = (New-Object 'System.Collections.Generic.HashSet[string]') }
        }
        $bySpec[$spec].Vol += $vol
        [void]$bySpec[$spec].Ids.Add($id)
        $total += $vol
    }
    $rowsOut = foreach ($spec in $bySpec.Keys) {
        $b = $bySpec[$spec]
        [pscustomobject]@{
            Specialty      = $spec
            Sources        = $b.Ids.Count
            SharedPatients = $b.Vol
            PctOfVolume    = if ($total -gt 0) { [math]::Round(100.0 * $b.Vol / $total, 1) } else { 0 }
        }
    }
    @($rowsOut | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                                        @{Expression = 'Specialty'; Descending = $false})
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
    'Get-RmDatasetInfo', 'Get-RmAvailableDatasets', 'Set-RmActiveDataset',
    'Save-RmDataset', 'Import-RmDataset',
    'Find-RmClinic', 'Find-RmPractice', 'Get-RmProviderDetail',
    'Get-RmReferralMap', 'Get-RmInboundByBucket', 'Get-RmProviderReferralActivity',
    'Get-RmProviderTrend', 'Get-RmPracticeBenchmark', 'Get-RmSourceSpecialtyMix',
    'Get-RmReferralGeography', 'Export-RmReferralMapHtml',
    'Export-RmResult', 'Clear-RmStaleTemp'
)
