# OrderReferring.psm1 — CMS "Order and Referring" dataset tracker.
#
# Downloads and keeps current the CMS Order & Referring eligibility file
# (refreshed by CMS roughly twice a week), and provides search, batch NPI
# verification, snapshot comparison, and CSV export with a methodology sidecar.
#
# What this data IS:   every provider currently eligible to order/refer for
#                      Medicare Part B (incl. outpatient therapy), DME, HHA,
#                      PMD, and Hospice — NPI, name, and five Y/N flags.
# What this data ISN'T: it contains NO claim/referral relationships. It cannot
#                      tell you which doctor referred patients to which clinic.
#
# Works on Windows PowerShell 5.1 and PowerShell 7+.

Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

$script:OrfLastLoaded = $null   # set by Import-OrfSnapshot; used by export sidecars

$script:OrfConfig = [ordered]@{
    # CMS open-data catalog (data.json). Overridable for tests via ORF_CATALOG_URL.
    CatalogUrl   = if ($env:ORF_CATALOG_URL) { $env:ORF_CATALOG_URL } else { 'https://data.cms.gov/data.json' }
    DatasetTitle = 'Order and Referring'
    DataDir      = if ($env:ORF_DATA_DIR) {
                       $env:ORF_DATA_DIR
                   } elseif ($env:LOCALAPPDATA) {
                       Join-Path $env:LOCALAPPDATA 'OrderReferringTracker'
                   } else {
                       Join-Path $HOME '.order-referring-tracker'
                   }
    KeepSnapshots = 8   # snapshots retained on disk (~70 MB each)
}

function Get-OrfConfig {
    <# .SYNOPSIS Returns the current tracker configuration. #>
    [pscustomobject]$script:OrfConfig
}

function Set-OrfConfig {
    <# .SYNOPSIS Overrides tracker configuration (data directory, catalog URL, retention). #>
    [CmdletBinding()]
    param(
        [string]$DataDir,
        [string]$CatalogUrl,
        [ValidateRange(2, 1000)][int]$KeepSnapshots
    )
    if ($DataDir)       { $script:OrfConfig.DataDir = $DataDir }
    if ($CatalogUrl)    { $script:OrfConfig.CatalogUrl = $CatalogUrl }
    if ($KeepSnapshots) { $script:OrfConfig.KeepSnapshots = $KeepSnapshots }
}

# ---------------------------------------------------------------------------
# Fast engine (compiled once per process; shared across runspaces)
# ---------------------------------------------------------------------------

if (-not ('OrfEngine' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

public class OrfProvider
{
    public string NPI;
    public string LastName;
    public string FirstName;
    public bool PartB;
    public bool DME;
    public bool HHA;
    public bool PMD;
    public bool Hospice;
}

public class OrfChange
{
    public string ChangeType;   // Added | Removed | Changed
    public string NPI;
    public string LastName;
    public string FirstName;
    public string OldFlags;     // e.g. "PARTB=Y DME=Y HHA=N PMD=N HOSPICE=N"
    public string NewFlags;
}

public class OrfDiffResult
{
    public int OldCount;
    public int NewCount;
    public List<OrfChange> Changes;
}

public static class OrfEngine
{
    public static readonly string[] ExpectedHeader = new string[] {
        "NPI", "LAST_NAME", "FIRST_NAME", "PARTB", "DME", "HHA", "PMD", "HOSPICE"
    };

    // RFC 4180 field splitter for one physical line. Fields in this file never
    // contain embedded newlines, but quoted fields with commas are handled.
    private static List<string> SplitCsvLine(string line)
    {
        List<string> fields = new List<string>(8);
        StringBuilder cur = new StringBuilder();
        bool inQuotes = false;
        for (int i = 0; i < line.Length; i++)
        {
            char c = line[i];
            if (inQuotes)
            {
                if (c == '"')
                {
                    if (i + 1 < line.Length && line[i + 1] == '"') { cur.Append('"'); i++; }
                    else { inQuotes = false; }
                }
                else { cur.Append(c); }
            }
            else
            {
                if (c == '"') { inQuotes = true; }
                else if (c == ',') { fields.Add(cur.ToString()); cur.Length = 0; }
                else { cur.Append(c); }
            }
        }
        fields.Add(cur.ToString());
        return fields;
    }

    private static bool Flag(string s)
    {
        return s == "Y" || s == "y";
    }

    // Validates the header and streams the file into memory (~1.6M rows, a few
    // seconds). Throws with a plain-language message on structural problems so
    // callers can surface it directly to the user.
    public static List<OrfProvider> Load(string path)
    {
        List<OrfProvider> rows = new List<OrfProvider>(1800000);
        using (StreamReader reader = new StreamReader(path, Encoding.UTF8, true))
        {
            string headerLine = reader.ReadLine();
            if (headerLine == null)
                throw new InvalidDataException("The file '" + path + "' is empty.");
            List<string> header = SplitCsvLine(headerLine.TrimStart('\uFEFF'));
            if (header.Count != ExpectedHeader.Length)
                throw new InvalidDataException(
                    "Unexpected column count in '" + path + "': expected " +
                    ExpectedHeader.Length + " columns (" + string.Join(",", ExpectedHeader) +
                    ") but found " + header.Count + ". CMS may have changed the file layout.");
            for (int i = 0; i < ExpectedHeader.Length; i++)
            {
                if (!string.Equals(header[i].Trim(), ExpectedHeader[i], StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException(
                        "Unexpected column " + (i + 1) + " in '" + path + "': expected '" +
                        ExpectedHeader[i] + "' but found '" + header[i] +
                        "'. CMS may have changed the file layout.");
            }

            string line;
            long lineNo = 1;
            while ((line = reader.ReadLine()) != null)
            {
                lineNo++;
                if (line.Length == 0) continue;
                List<string> f = SplitCsvLine(line);
                if (f.Count != ExpectedHeader.Length)
                    throw new InvalidDataException(
                        "Malformed row at line " + lineNo + " of '" + path + "': expected " +
                        ExpectedHeader.Length + " fields, found " + f.Count + ".");
                OrfProvider p = new OrfProvider();
                p.NPI = f[0].Trim();
                p.LastName = f[1].Trim();
                p.FirstName = f[2].Trim();
                p.PartB = Flag(f[3].Trim());
                p.DME = Flag(f[4].Trim());
                p.HHA = Flag(f[5].Trim());
                p.PMD = Flag(f[6].Trim());
                p.Hospice = Flag(f[7].Trim());
                rows.Add(p);
            }
        }
        return rows;
    }

    // Case-insensitive search. nameQuery matches "LAST FIRST", "FIRST LAST",
    // or a substring of either name; npiQuery is a digit prefix. Flag args:
    // true = require Y. limit <= 0 means unlimited.
    public static List<OrfProvider> Search(
        List<OrfProvider> data, string nameQuery, string npiQuery,
        bool reqPartB, bool reqDME, bool reqHHA, bool reqPMD, bool reqHospice,
        int limit)
    {
        List<OrfProvider> outRows = new List<OrfProvider>();
        string[] nameTerms = null;
        if (!string.IsNullOrEmpty(nameQuery))
        {
            nameTerms = nameQuery.Trim().Split(
                new char[] { ' ', ',' }, StringSplitOptions.RemoveEmptyEntries);
        }
        foreach (OrfProvider p in data)
        {
            if (reqPartB && !p.PartB) continue;
            if (reqDME && !p.DME) continue;
            if (reqHHA && !p.HHA) continue;
            if (reqPMD && !p.PMD) continue;
            if (reqHospice && !p.Hospice) continue;
            if (!string.IsNullOrEmpty(npiQuery) &&
                p.NPI.IndexOf(npiQuery, StringComparison.Ordinal) != 0) continue;
            if (nameTerms != null)
            {
                bool all = true;
                foreach (string t in nameTerms)
                {
                    if (p.LastName.IndexOf(t, StringComparison.OrdinalIgnoreCase) < 0 &&
                        p.FirstName.IndexOf(t, StringComparison.OrdinalIgnoreCase) < 0)
                    { all = false; break; }
                }
                if (!all) continue;
            }
            outRows.Add(p);
            if (limit > 0 && outRows.Count >= limit) break;
        }
        return outRows;
    }

    private static string FlagString(OrfProvider p)
    {
        return "PARTB=" + (p.PartB ? "Y" : "N") +
               " DME=" + (p.DME ? "Y" : "N") +
               " HHA=" + (p.HHA ? "Y" : "N") +
               " PMD=" + (p.PMD ? "Y" : "N") +
               " HOSPICE=" + (p.Hospice ? "Y" : "N");
    }

    // Keyed by NPI. If a file ever contained duplicate NPIs the first row wins,
    // matching how CMS's own lookup behaves. Public so batch checks against an
    // already-loaded snapshot can index at native speed.
    public static Dictionary<string, OrfProvider> BuildIndex(List<OrfProvider> data)
    {
        Dictionary<string, OrfProvider> map =
            new Dictionary<string, OrfProvider>(data.Count, StringComparer.Ordinal);
        foreach (OrfProvider p in data)
        {
            if (!map.ContainsKey(p.NPI)) map.Add(p.NPI, p);
        }
        return map;
    }

    public static OrfDiffResult Diff(List<OrfProvider> oldData, List<OrfProvider> newData)
    {
        Dictionary<string, OrfProvider> oldMap = BuildIndex(oldData);
        Dictionary<string, OrfProvider> newMap = BuildIndex(newData);
        OrfDiffResult result = new OrfDiffResult();
        result.OldCount = oldMap.Count;
        result.NewCount = newMap.Count;
        result.Changes = new List<OrfChange>();

        foreach (KeyValuePair<string, OrfProvider> kv in newMap)
        {
            OrfProvider oldP;
            OrfProvider p = kv.Value;
            if (!oldMap.TryGetValue(kv.Key, out oldP))
            {
                OrfChange c = new OrfChange();
                c.ChangeType = "Added"; c.NPI = p.NPI;
                c.LastName = p.LastName; c.FirstName = p.FirstName;
                c.OldFlags = ""; c.NewFlags = FlagString(p);
                result.Changes.Add(c);
            }
            else
            {
                string oldFlags = FlagString(oldP);
                string newFlags = FlagString(p);
                if (oldFlags != newFlags ||
                    oldP.LastName != p.LastName || oldP.FirstName != p.FirstName)
                {
                    OrfChange c = new OrfChange();
                    c.ChangeType = "Changed"; c.NPI = p.NPI;
                    c.LastName = p.LastName; c.FirstName = p.FirstName;
                    c.OldFlags = oldFlags; c.NewFlags = newFlags;
                    result.Changes.Add(c);
                }
            }
        }
        foreach (KeyValuePair<string, OrfProvider> kv in oldMap)
        {
            if (!newMap.ContainsKey(kv.Key))
            {
                OrfProvider p = kv.Value;
                OrfChange c = new OrfChange();
                c.ChangeType = "Removed"; c.NPI = p.NPI;
                c.LastName = p.LastName; c.FirstName = p.FirstName;
                c.OldFlags = FlagString(p); c.NewFlags = "";
                result.Changes.Add(c);
            }
        }
        return result;
    }

    // NPI check digit per the CMS algorithm: Luhn over the 9 identifier digits
    // with the constant 24 added for the implicit "80840" prefix.
    public static bool IsValidNpi(string npi)
    {
        if (string.IsNullOrEmpty(npi) || npi.Length != 10) return false;
        int sum = 24;
        for (int i = 0; i < 9; i++)
        {
            char c = npi[i];
            if (c < '0' || c > '9') return false;
            int d = c - '0';
            if (i % 2 == 0) { d *= 2; if (d > 9) d -= 9; }
            sum += d;
        }
        char last = npi[9];
        if (last < '0' || last > '9') return false;
        int check = (10 - (sum % 10)) % 10;
        return check == (last - '0');
    }
}
'@
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Safe property read for JSON-shaped external data: returns $null instead of a
# strict-mode error when the property does not exist.
function Get-OrfProp([object]$Object, [string]$Name) {
    if ($null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]) {
        $Object.PSObject.Properties[$Name].Value
    } else {
        $null
    }
}

function Get-OrfPaths {
    $dataDir = $script:OrfConfig.DataDir
    [pscustomobject]@{
        DataDir     = $dataDir
        SnapshotDir = Join-Path $dataDir 'snapshots'
        ChangeDir   = Join-Path $dataDir 'changes'
        StateFile   = Join-Path $dataDir 'state.json'
    }
}

function Initialize-OrfDataDir {
    $paths = Get-OrfPaths
    foreach ($d in @($paths.DataDir, $paths.SnapshotDir, $paths.ChangeDir)) {
        if (-not (Test-Path -LiteralPath $d)) {
            New-Item -ItemType Directory -Path $d -Force | Out-Null
        }
    }
    $paths
}

function Read-OrfState {
    $paths = Get-OrfPaths
    if (Test-Path -LiteralPath $paths.StateFile) {
        try {
            return Get-Content -LiteralPath $paths.StateFile -Raw | ConvertFrom-Json
        } catch {
            Write-Warning "State file was unreadable and will be rebuilt: $($_.Exception.Message)"
        }
    }
    $null
}

function Write-OrfState([object]$State) {
    $paths = Initialize-OrfDataDir
    $tmp = $paths.StateFile + '.tmp'
    $State | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $paths.StateFile -Force
}

function Get-OrfSnapshotFiles {
    <# .SYNOPSIS Lists downloaded snapshot CSVs, oldest first. #>
    $paths = Get-OrfPaths
    if (-not (Test-Path -LiteralPath $paths.SnapshotDir)) { return @() }
    @(Get-ChildItem -LiteralPath $paths.SnapshotDir -Filter 'OrderReferring_*.csv' |
        Sort-Object Name)
}

function Get-OrfLatestSnapshot {
    <# .SYNOPSIS Returns the newest downloaded snapshot file, or $null. #>
    $files = @(Get-OrfSnapshotFiles)
    if ($files.Count -gt 0) { $files[-1] } else { $null }
}

# ---------------------------------------------------------------------------
# Catalog / download / update
# ---------------------------------------------------------------------------

function Get-OrfCatalogInfo {
    <#
    .SYNOPSIS
      Queries the CMS open-data catalog and returns the latest Order & Referring
      release: its date and CSV download URL.
    #>
    [CmdletBinding()]
    param()

    $url = $script:OrfConfig.CatalogUrl
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 120 -ErrorAction Stop
        $catalog = $resp.Content | ConvertFrom-Json
    } catch {
        throw ("Could not reach the CMS data catalog at $url. " +
               "Check your internet connection and try again. Details: $($_.Exception.Message)")
    }

    $dataset = @((Get-OrfProp $catalog 'dataset') |
        Where-Object { (Get-OrfProp $_ 'title') -eq $script:OrfConfig.DatasetTitle })
    if ($dataset.Count -eq 0) {
        throw ("The CMS catalog no longer lists a dataset titled " +
               "'$($script:OrfConfig.DatasetTitle)'. CMS may have renamed it; " +
               "check https://data.cms.gov and update this tool's configuration.")
    }
    $dataset = $dataset[0]

    # Distributions come in (API, CSV) pairs per release, newest first. Pick the
    # CSV with the newest 'modified' date. The modified date becomes part of a
    # local filename, so only accept a strict yyyy-MM-dd prefix — a datetime or
    # junk value from the remote catalog must never reach the filesystem.
    $csvDist = @((Get-OrfProp $dataset 'distribution') |
        Where-Object { (Get-OrfProp $_ 'format') -eq 'CSV' -and (Get-OrfProp $_ 'downloadURL') } |
        ForEach-Object {
            $m = [regex]::Match([string](Get-OrfProp $_ 'modified'), '^\d{4}-\d{2}-\d{2}')
            if ($m.Success) {
                [pscustomobject]@{
                    ReleaseDate = $m.Value
                    CsvUrl      = [string](Get-OrfProp $_ 'downloadURL')
                }
            } else {
                Write-Warning "Skipping a catalog entry with an unrecognized date: '$(Get-OrfProp $_ 'modified')'"
            }
        } |
        Sort-Object ReleaseDate -Descending)
    if ($csvDist.Count -eq 0) {
        throw ("The '$(Get-OrfProp $dataset 'title')' dataset has no usable CSV download listed in the " +
               "CMS catalog. CMS may have changed how the data is published.")
    }

    [pscustomobject]@{
        ReleaseDate    = $csvDist[0].ReleaseDate
        CsvUrl         = $csvDist[0].CsvUrl
        AllCsvReleases = $csvDist
    }
}

function Update-OrfData {
    <#
    .SYNOPSIS
      Checks CMS for a newer Order & Referring release and downloads it if the
      local copy is out of date. Safe to run any time; does nothing when current.
    .PARAMETER Force
      Re-download even if the local snapshot matches the latest release.
    .PARAMETER ReleaseDate
      Download a specific catalog release (yyyy-MM-dd) instead of the newest.
    #>
    [CmdletBinding()]
    param(
        [switch]$Force,
        [string]$ReleaseDate
    )

    $paths = Initialize-OrfDataDir
    $catalog = Get-OrfCatalogInfo

    $release = if ($ReleaseDate) {
        $match = @($catalog.AllCsvReleases | Where-Object { $_.ReleaseDate -eq $ReleaseDate })
        if ($match.Count -eq 0) {
            $avail = ($catalog.AllCsvReleases | ForEach-Object { $_.ReleaseDate }) -join ', '
            throw "No release dated '$ReleaseDate' in the CMS catalog. Available: $avail"
        }
        $match[0]
    } else {
        [pscustomobject]@{ ReleaseDate = $catalog.ReleaseDate; CsvUrl = $catalog.CsvUrl }
    }

    $snapshotName = 'OrderReferring_{0}.csv' -f $release.ReleaseDate
    $snapshotPath = Join-Path $paths.SnapshotDir $snapshotName

    # On Windows PowerShell 5.1 the download progress bar slows large
    # Invoke-WebRequest -OutFile transfers by ~10x. Function-local override.
    $ProgressPreference = 'SilentlyContinue'

    $state = Read-OrfState
    if (-not $Force -and (Test-Path -LiteralPath $snapshotPath)) {
        Write-Verbose "Already current: $snapshotName"
        # Self-heal: if a previous run was killed between promoting the file
        # and writing state.json, the metadata still cites the older release.
        # Re-derive it from the file so status and export sidecars stay honest.
        $latestOnDisk = Get-OrfLatestSnapshot
        if ($latestOnDisk -and (-not $state -or $state.ReleaseDate -ne ($latestOnDisk.BaseName -replace '^OrderReferring_', ''))) {
            try {
                $repairRows = [OrfEngine]::Load($latestOnDisk.FullName)
                Write-OrfState ([ordered]@{
                    ReleaseDate  = ($latestOnDisk.BaseName -replace '^OrderReferring_', '')
                    CsvUrl       = $release.CsvUrl
                    Sha256       = (Get-FileHash -LiteralPath $latestOnDisk.FullName -Algorithm SHA256).Hash
                    RowCount     = $repairRows.Count
                    DownloadedAt = (Get-Date).ToString('o')
                })
                $state = Read-OrfState
                Write-Verbose "Repaired stale state metadata from $($latestOnDisk.Name)."
            } catch {
                Write-Warning "Could not repair state metadata: $($_.Exception.Message)"
            }
        }
        return [pscustomobject]@{
            Updated      = $false
            ReleaseDate  = $release.ReleaseDate
            SnapshotPath = $snapshotPath
            RowCount     = if ($state -and $state.ReleaseDate -eq $release.ReleaseDate) { $state.RowCount } else { $null }
            Message      = "Data is already up to date (release $($release.ReleaseDate))."
        }
    }

    $previousSnapshot = Get-OrfLatestSnapshot

    # Download to a unique temp name (concurrent runs — e.g. the scheduled task
    # and the GUI — must never share a partial file), validate, then atomically
    # promote. A crash or network failure at any point leaves the previous
    # snapshot untouched.
    $tmpPath = '{0}.{1}.tmp' -f $snapshotPath, [guid]::NewGuid().ToString('N')
    try {
        Write-Verbose "Downloading $($release.CsvUrl)"
        Invoke-WebRequest -Uri $release.CsvUrl -OutFile $tmpPath -UseBasicParsing `
            -TimeoutSec 1800 -ErrorAction Stop
    } catch {
        Remove-Item -LiteralPath $tmpPath -ErrorAction SilentlyContinue
        throw ("The download from CMS failed. Your existing data (if any) is " +
               "unchanged. Details: $($_.Exception.Message)")
    }

    # Validate structure by fully parsing (also gives us the row count).
    try {
        $rows = [OrfEngine]::Load($tmpPath)
    } catch {
        Remove-Item -LiteralPath $tmpPath -ErrorAction SilentlyContinue
        throw ("The downloaded file failed validation and was discarded. Your " +
               "existing data is unchanged. Details: $($_.Exception.Message)")
    }
    if ($rows.Count -eq 0) {
        Remove-Item -LiteralPath $tmpPath -ErrorAction SilentlyContinue
        throw "The downloaded file contained a header but zero data rows; it was discarded."
    }

    $sha = (Get-FileHash -LiteralPath $tmpPath -Algorithm SHA256).Hash
    try {
        Move-Item -LiteralPath $tmpPath -Destination $snapshotPath -Force
    } catch {
        Remove-Item -LiteralPath $tmpPath -ErrorAction SilentlyContinue
        throw ("The validated download could not be moved into place (another " +
               "update may be running). Your existing data is unchanged. " +
               "Details: $($_.Exception.Message)")
    }

    # Is the file we just downloaded the newest on disk? Explicitly requesting
    # an OLDER release (-ReleaseDate) must not rewrite current-state metadata,
    # must not produce a chronologically backwards change log, and must not be
    # treated as "the" data by queries (which always load the newest snapshot).
    $isNewest = (-not $previousSnapshot) -or ($snapshotName -ge $previousSnapshot.Name)

    # Change log vs. the previous snapshot (best effort — never blocks the update).
    $changeSummary = $null
    if ($isNewest -and $previousSnapshot -and $previousSnapshot.FullName -ne $snapshotPath) {
        try {
            $oldRows = [OrfEngine]::Load($previousSnapshot.FullName)
            $diff = [OrfEngine]::Diff($oldRows, $rows)
            $changeFile = Join-Path $paths.ChangeDir `
                ('changes_{0}_to_{1}.csv' -f ($previousSnapshot.BaseName -replace '^OrderReferring_', ''), $release.ReleaseDate)
            $diff.Changes |
                Select-Object ChangeType, NPI, LastName, FirstName, OldFlags, NewFlags |
                Export-Csv -LiteralPath $changeFile -NoTypeInformation -Encoding UTF8
            $added   = @($diff.Changes | Where-Object ChangeType -eq 'Added').Count
            $removed = @($diff.Changes | Where-Object ChangeType -eq 'Removed').Count
            $changed = @($diff.Changes | Where-Object ChangeType -eq 'Changed').Count
            $changeSummary = "$added added, $removed removed, $changed changed vs. previous snapshot."
        } catch {
            Write-Warning "Update succeeded, but the change log could not be built: $($_.Exception.Message)"
        }
    }

    if ($isNewest) {
        Write-OrfState ([ordered]@{
            ReleaseDate  = $release.ReleaseDate
            CsvUrl       = $release.CsvUrl
            Sha256       = $sha
            RowCount     = $rows.Count
            DownloadedAt = (Get-Date).ToString('o')
        })
    }

    # Retention: keep the most recent N snapshots — but never the one that was
    # just downloaded, even if it sorts older than everything else.
    $all = @(Get-OrfSnapshotFiles | Where-Object { $_.FullName -ne $snapshotPath })
    $keep = $script:OrfConfig.KeepSnapshots - 1   # the new file occupies one slot
    if ($all.Count -gt $keep) {
        $all | Select-Object -First ($all.Count - $keep) |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }

    [pscustomobject]@{
        Updated      = $true
        ReleaseDate  = $release.ReleaseDate
        SnapshotPath = $snapshotPath
        RowCount     = $rows.Count
        Sha256       = $sha
        Message      = ("Downloaded release {0}: {1:N0} providers.{2}" -f
                        $release.ReleaseDate, $rows.Count,
                        $(if ($changeSummary) { " $changeSummary" } else { '' }))
    }
}

function Get-OrfStatus {
    <# .SYNOPSIS Shows what data is on disk and whether CMS has something newer. #>
    [CmdletBinding()]
    param([switch]$CheckOnline)

    $state = Read-OrfState
    $latest = Get-OrfLatestSnapshot
    $status = [ordered]@{
        DataDir       = $script:OrfConfig.DataDir
        LocalRelease  = if ($state) { $state.ReleaseDate } else { $null }
        LocalRowCount = if ($state) { $state.RowCount } else { $null }
        SnapshotFile  = if ($latest) { $latest.FullName } else { $null }
        SnapshotCount = @(Get-OrfSnapshotFiles).Count
    }
    if ($CheckOnline) {
        $catalog = Get-OrfCatalogInfo
        $status.CmsRelease = $catalog.ReleaseDate
        $status.UpdateAvailable = ($status.LocalRelease -ne $catalog.ReleaseDate)
    }
    [pscustomobject]$status
}

# ---------------------------------------------------------------------------
# Query / verification / comparison
# ---------------------------------------------------------------------------

function Import-OrfSnapshot {
    <#
    .SYNOPSIS
      Loads a snapshot CSV into memory (defaults to the newest one) and returns
      the row list. Used internally; also handy for custom analysis.
    #>
    [CmdletBinding()]
    param([string]$Path)

    if (-not $Path) {
        $latest = Get-OrfLatestSnapshot
        if (-not $latest) {
            throw "No data downloaded yet. Run Update-OrfData (or click 'Check for updates' in the app) first."
        }
        $Path = $latest.FullName
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Snapshot file not found: $Path"
    }
    # Remember which snapshot was actually loaded so export sidecars can cite
    # the file the numbers really came from (not just the newest release).
    $leaf = Split-Path -Leaf $Path
    $script:OrfLastLoaded = [pscustomobject]@{
        File    = $leaf
        Release = ($leaf -replace '^OrderReferring_', '' -replace '\.csv$', '')
    }
    ,([OrfEngine]::Load($Path))
}

function ConvertTo-OrfRecord {
    param([Parameter(ValueFromPipeline)]$Row)
    process {
        [pscustomobject]@{
            NPI       = $Row.NPI
            LastName  = $Row.LastName
            FirstName = $Row.FirstName
            PartB     = if ($Row.PartB) { 'Y' } else { 'N' }
            DME       = if ($Row.DME) { 'Y' } else { 'N' }
            HHA       = if ($Row.HHA) { 'Y' } else { 'N' }
            PMD       = if ($Row.PMD) { 'Y' } else { 'N' }
            Hospice   = if ($Row.Hospice) { 'Y' } else { 'N' }
        }
    }
}

function Search-OrfProvider {
    <#
    .SYNOPSIS
      Searches the current snapshot by name and/or NPI, optionally requiring
      eligibility flags.
    .EXAMPLE
      Search-OrfProvider -Name 'smith john' -RequireFlag PARTB
    .EXAMPLE
      Search-OrfProvider -Npi 1417051921
    #>
    [CmdletBinding()]
    param(
        [string]$Name,
        [ValidatePattern('^\d{1,10}$')][string]$Npi,
        [ValidateSet('PARTB', 'DME', 'HHA', 'PMD', 'HOSPICE')][string[]]$RequireFlag = @(),
        [int]$Limit = 0,
        [string]$SnapshotPath
    )

    $data = Import-OrfSnapshot -Path $SnapshotPath
    $hits = [OrfEngine]::Search(
        $data, $Name, $Npi,
        ($RequireFlag -contains 'PARTB'), ($RequireFlag -contains 'DME'),
        ($RequireFlag -contains 'HHA'), ($RequireFlag -contains 'PMD'),
        ($RequireFlag -contains 'HOSPICE'), $Limit)
    $hits | ConvertTo-OrfRecord
}

function Get-OrfNpiFromText {
    <#
    .SYNOPSIS
      Extracts every distinct 10-digit NPI-shaped number from arbitrary text
      (a pasted spreadsheet column, a whole report, a raw list), preserving
      first-seen order. The single source of truth for NPI extraction — the
      GUI and Test-OrfNpi both use it.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)

    $seen = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($m in [regex]::Matches($Text, '(?<!\d)\d{10}(?!\d)')) {
        if ($seen.Add($m.Value)) { $m.Value }
    }
}

function Test-OrfNpi {
    <#
    .SYNOPSIS
      Batch-verifies NPIs against the current snapshot: whether each is a valid
      NPI, whether it is on the CMS eligible-to-order/refer list, and its flags.
      Feed it the referring-provider NPIs from your own EMR/billing records.
    .PARAMETER Npi
      One or more 10-digit NPIs.
    .PARAMETER Path
      A text or CSV file to read NPIs from. Every 10-digit number found in the
      file is checked, so a raw list, or a CSV with an NPI column, both work.
    .PARAMETER Data
      An already-loaded snapshot (from Import-OrfSnapshot). Skips the file
      reload — used by the GUI, which keeps the snapshot in memory.
    #>
    [CmdletBinding()]
    param(
        [Parameter(ValueFromPipeline)][string[]]$Npi,
        [string]$Path,
        [string]$SnapshotPath,
        [object]$Data
    )

    begin { $collected = New-Object System.Collections.Generic.List[string] }
    process { foreach ($n in $Npi) { if ($n) { $collected.Add($n) } } }
    end {
        if ($Path) {
            if (-not (Test-Path -LiteralPath $Path)) { throw "NPI list file not found: $Path" }
            foreach ($n in Get-OrfNpiFromText -Text (Get-Content -LiteralPath $Path -Raw)) {
                $collected.Add($n)
            }
        }
        # Normalize: trim, dedupe, preserve order.
        $seen = New-Object 'System.Collections.Generic.HashSet[string]'
        $list = @(foreach ($n in $collected) {
            $t = $n.Trim()
            if ($t -and $seen.Add($t)) { $t }
        })
        if ($list.Count -eq 0) {
            throw "No NPIs to check. Provide -Npi values or a -Path file containing 10-digit NPIs."
        }

        if ($null -eq $Data) { $Data = Import-OrfSnapshot -Path $SnapshotPath }
        $map = [OrfEngine]::BuildIndex($Data)

        foreach ($n in $list) {
            if (-not [OrfEngine]::IsValidNpi($n)) {
                [pscustomobject]@{
                    NPI = $n; Status = 'INVALID NPI'; LastName = ''; FirstName = ''
                    PartB = ''; DME = ''; HHA = ''; PMD = ''; Hospice = ''
                }
            } elseif ($map.ContainsKey($n)) {
                $p = $map[$n]
                [pscustomobject]@{
                    NPI = $n; Status = 'ELIGIBLE (on CMS list)'
                    LastName = $p.LastName; FirstName = $p.FirstName
                    PartB = if ($p.PartB) { 'Y' } else { 'N' }
                    DME = if ($p.DME) { 'Y' } else { 'N' }
                    HHA = if ($p.HHA) { 'Y' } else { 'N' }
                    PMD = if ($p.PMD) { 'Y' } else { 'N' }
                    Hospice = if ($p.Hospice) { 'Y' } else { 'N' }
                }
            } else {
                [pscustomobject]@{
                    NPI = $n; Status = 'NOT ON LIST'; LastName = ''; FirstName = ''
                    PartB = 'N'; DME = 'N'; HHA = 'N'; PMD = 'N'; Hospice = 'N'
                }
            }
        }
    }
}

function Compare-OrfSnapshot {
    <#
    .SYNOPSIS
      Compares two downloaded snapshots and returns Added/Removed/Changed rows.
      With no arguments, compares the two most recent snapshots.
    #>
    [CmdletBinding()]
    param(
        [string]$OldPath,
        [string]$NewPath
    )

    if (-not $OldPath -or -not $NewPath) {
        $files = @(Get-OrfSnapshotFiles)
        if ($files.Count -lt 2) {
            throw ("Need two downloaded snapshots to compare, but only $($files.Count) " +
                   "exist. Snapshots accumulate automatically as CMS releases updates " +
                   "(roughly twice a week).")
        }
        if (-not $OldPath) { $OldPath = $files[-2].FullName }
        if (-not $NewPath) { $NewPath = $files[-1].FullName }
    }
    $oldRows = Import-OrfSnapshot -Path $OldPath
    $newRows = Import-OrfSnapshot -Path $NewPath
    $diff = [OrfEngine]::Diff($oldRows, $newRows)
    Write-Verbose ("Old: {0:N0} rows; New: {1:N0} rows; {2:N0} differences." -f
                   $diff.OldCount, $diff.NewCount, $diff.Changes.Count)
    $diff.Changes | Select-Object ChangeType, NPI, LastName, FirstName, OldFlags, NewFlags
}

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

function Export-OrfResult {
    <#
    .SYNOPSIS
      Exports any tracker results (search hits, batch checks, comparisons) to a
      CSV, plus a .methodology.txt sidecar recording the data source, release
      date, and row count — so every export is self-documenting.
    .EXAMPLE
      Search-OrfProvider -Name smith -RequireFlag PARTB | Export-OrfResult -Path smiths.csv -Description 'Part B–eligible providers named Smith'
    #>
    [CmdletBinding()]
    param(
        [Parameter(ValueFromPipeline)][object[]]$InputObject,
        [Parameter(Mandatory)][string]$Path,
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
            # An empty result is a legitimate answer. With zero rows there is no
            # schema to emit, so the CSV is left empty; the sidecar records the
            # honest "0 rows" rather than inventing content.
            Set-Content -LiteralPath $Path -Value '' -Encoding UTF8
        } else {
            $rows | Export-Csv -LiteralPath $Path -NoTypeInformation -Encoding UTF8
        }

        # Provenance: prefer the snapshot actually loaded by this session's
        # queries (Import-OrfSnapshot records it); fall back to the newest
        # release named in state.json.
        $state = Read-OrfState
        $loaded = $script:OrfLastLoaded
        $sidecar = $Path -replace '\.[Cc][Ss][Vv]$', ''
        $sidecar = "$sidecar.methodology.txt"
        @(
            'Export methodology'
            '=================='
            "Generated:      $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))"
            "Rows exported:  $($rows.Count)"
            $(if ($Description) { "Description:    $Description" })
            ''
            'Source: CMS "Order and Referring" public dataset'
            '  https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/order-and-referring'
            $(if ($loaded) { "Data snapshot:  $($loaded.File) (release $($loaded.Release); the snapshot this session's queries loaded)" })
            $(if ($state -and (-not $loaded -or $state.ReleaseDate -eq $loaded.Release)) {
                "Data release:   $($state.ReleaseDate) ($('{0:N0}' -f $state.RowCount) providers)" })
            $(if ($state -and (-not $loaded -or $state.ReleaseDate -eq $loaded.Release)) { "File SHA-256:   $($state.Sha256)" })
            $(if (-not $state -and -not $loaded) { 'Data release:   (no local snapshot metadata available)' })
            ''
            'This dataset lists providers eligible to ORDER AND REFER within Medicare'
            '(Part B, DME, HHA, PMD, Hospice eligibility flags). It contains no claims'
            'and no referral relationships: it cannot show which provider referred'
            'patients to which practice.'
        ) | Where-Object { $null -ne $_ } | Set-Content -LiteralPath $sidecar -Encoding UTF8

        Write-Verbose "Wrote $($rows.Count) rows to $Path (+ methodology sidecar)."
        [pscustomobject]@{ Path = $Path; Rows = $rows.Count; Methodology = $sidecar }
    }
}

# ---------------------------------------------------------------------------
# Scheduled updates (Windows)
# ---------------------------------------------------------------------------

function Install-OrfUpdateTask {
    <#
    .SYNOPSIS
      Registers a Windows Scheduled Task that checks CMS for a new release every
      day at the given time (downloads only happen when CMS actually publishes,
      roughly twice a week). Requires Windows.
    #>
    [CmdletBinding()]
    param([string]$At = '07:00')

    if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
        throw "Scheduled tasks are only supported on Windows."
    }
    $moduleFile = Join-Path $PSScriptRoot 'OrderReferring.psm1'
    # Double any embedded single quotes (e.g. C:\Users\O'Brien\...) so the
    # command stays valid, and use the RESOLVED pwsh path — Task Scheduler does
    # not share this session's PATH, so a bare 'pwsh.exe' can fail at run time.
    $escaped = $moduleFile -replace "'", "''"
    $cmd = "Import-Module '$escaped'; Update-OrfData"
    $pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
    $pwshExe = if ($pwshCmd -and $pwshCmd.Source) { $pwshCmd.Source } else { 'powershell.exe' }
    $action = New-ScheduledTaskAction -Execute $pwshExe `
        -Argument "-NoProfile -WindowStyle Hidden -Command `"$cmd`""
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName 'OrderReferringTracker-Update' `
        -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "Scheduled task 'OrderReferringTracker-Update' registered: daily check at $At."
}

function Uninstall-OrfUpdateTask {
    <# .SYNOPSIS Removes the scheduled update task. #>
    [CmdletBinding()]
    param()
    if (-not (Get-Command Unregister-ScheduledTask -ErrorAction SilentlyContinue)) {
        throw "Scheduled tasks are only supported on Windows."
    }
    Unregister-ScheduledTask -TaskName 'OrderReferringTracker-Update' -Confirm:$false
    Write-Host "Scheduled task removed."
}

Export-ModuleMember -Function @(
    'Get-OrfConfig', 'Set-OrfConfig',
    'Get-OrfCatalogInfo', 'Update-OrfData', 'Get-OrfStatus',
    'Get-OrfSnapshotFiles', 'Get-OrfLatestSnapshot', 'Import-OrfSnapshot',
    'Search-OrfProvider', 'Test-OrfNpi', 'Compare-OrfSnapshot',
    'ConvertTo-OrfRecord', 'Get-OrfNpiFromText',
    'Export-OrfResult',
    'Install-OrfUpdateTask', 'Uninstall-OrfUpdateTask'
)
